"""
Khabar — THE L2 PRODUCT RUNNER.
================================================================================
Mirrors compute_signals.py's structure and philosophy on purpose: same
DRY_RUN flag, same per-product isolation (one broken product must never block
the other six), same replace-not-append write pattern, same product_runs log
table mirroring signal_runs.

WHAT'S DIFFERENT FROM compute_signals.py, AND WHY
No khabar_lake, no DuckDB, no prefetch(). L2 reads only the signal_l1_*
tables — the largest of them is a few hundred KB — so there is nothing to
materialise and no lake-scale egress concern. Every query in products.py is
plain Postgres, run directly over the same psycopg2 connection used to write
results. One connection, not two engines.

PRECONDITION CHECK
Each product declares "requires": a list of L1 signal ids whose tables must
have at least one row. This is deliberately about DATA PRESENCE, not about
whether the signal is "enabled" in signals.py — that distinction already
lives in signals.py and doesn't need duplicating here. If a required table is
empty (a signal that's live in the registry but genuinely hasn't produced
output yet, or hasn't been deployed at all), the product is skipped with a
specific reason rather than silently computing over an empty JOIN and writing
a hollow or misleading row.
"""

import os
import sys
import time
from datetime import date, timezone, datetime

import psycopg2
from khabar_db import CA_BUNDLE
import psycopg2.extras

from products import PRODUCTS

SUPABASE_DB_URL = os.environ.get("KHABAR_DB_URL", "").strip() or os.environ["SUPABASE_DB_URL"]

DRY_RUN = os.environ.get("PRODUCTS_DRY_RUN", "false").lower() == "true"

# Maps an L1 signal id (as used in "requires") to its actual Postgres table.
# Hardcoded rather than imported from signals.py on purpose: importing would
# make compute_products.py depend on the DuckDB-only pieces of signals.py's
# import chain (khabar_lake) for something that never touches DuckDB. This
# list only needs to grow when a NEW L1 signal becomes an L2 dependency.
L1_TABLES = {
    "l1_01": "signal_l1_01_genuine_price_drop",
    "l1_03": "signal_l1_03_price_staircase",
    "l1_04": "signal_l1_04_anchor_inflation",
    "l1_06": "signal_l1_06_discount_recovery",
    "l1_07": "signal_l1_07_price_anomaly",
    "l1_08": "signal_l1_08_variant_stockout",
    "l1_09": "signal_l1_09_variant_restock",
    "l1_10": "signal_l1_10_dead_stock",
    "l1_11": "signal_l1_11_size_asymmetry",
    "l1_12": "signal_l1_12_new_sku_launch",
    "l1_13": "signal_l1_13_product_delisted",
    "l1_14": "signal_l1_14_launch_to_discount",
    "l1_17": "signal_l1_17_depth_escalation",
    "l1_22": "signal_l1_22_discount_velocity",
    "l1_24": "signal_l1_24_restock_density",
}


def log_run(pg, product, status, **kw):
    with pg.cursor() as cur:
        cur.execute("""
            INSERT INTO product_runs (
                product_id, product_name, status, skip_reason,
                rows_written, duration_seconds, error_message
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (
            product["id"], product["name"], status,
            kw.get("skip_reason"), kw.get("rows_written", 0),
            kw.get("duration_seconds"), kw.get("error_message"),
        ))
    pg.commit()


def missing_requirements(pg, product):
    """
    Which required L1 signals have NOT successfully completed TODAY.

    FIXED 2026-09-26 -- this used to check `SELECT count(*) FROM {table}`,
    i.e. "has this L1 output table EVER had any rows", not whether it has
    TODAY's. Confirmed live: on all three days the signal engine's DuckDB
    lake bootstrap crashed outright before any L1 signal ran (2026-09-09,
    2026-09-19, 2026-09-25), every required L1 table still held YESTERDAY's
    rows from the last successful run, so this check passed anyway --
    compute_products.py went ahead and computed a full day of "current"
    intelligence products (l2_01, l2_02, l2_08, l2_09, l2_10, l2_12, l2_13 --
    confirmed non-zero rows written every one of those three times) built
    entirely from stale, day-old L1 numbers, stamped with report_date =
    today. Nothing anywhere flagged it as stale.

    This checks signal_runs -- the run LOG -- instead of the L1 output table
    itself, on purpose. A signal that ran fine today but genuinely found zero
    qualifying rows (a real, valid "nothing to report") also leaves the
    output table's own latest snapshot_date at yesterday's: replace_rows()
    in compute_signals.py has no day to DELETE/INSERT against when its query
    returns zero rows. signal_runs still gets an "ok" row for today in that
    case (log_run() fires regardless of row count), so checking the log
    correctly tells "L1 ran today and found nothing" apart from "L1 never
    ran today at all" -- something the table-row-count check could not
    distinguish, and got wrong in exactly the cases that mattered.
    """
    missing = []
    with pg.cursor() as cur:
        for l1_id in product.get("requires", []):
            if l1_id not in L1_TABLES:
                missing.append(f"{l1_id} (unknown table mapping)")
                continue
            cur.execute("""
                SELECT count(*) FROM signal_runs
                WHERE signal_id = %s
                  AND status = 'ok'
                  AND run_at::date = CURRENT_DATE
            """, (l1_id,))
            if cur.fetchone()[0] == 0:
                missing.append(f"{l1_id} (no successful signal_runs row for "
                                f"today — L1 either failed or hasn't run yet)")
    return missing


def replace_rows(pg, table, rows, columns):
    """
    Same rule as L1's replace_rows: delete today's report_date, then insert
    fresh — one transaction, so the day is never momentarily empty. Every L2
    product's SQL selects CURRENT_DATE AS report_date, so this is always a
    single-day replace, simpler than L1's multi-day backfill case.
    """
    if not rows:
        return 0
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    insert_sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
    with pg.cursor() as cur:
        cur.execute(f"DELETE FROM {table} WHERE report_date = CURRENT_DATE")
        psycopg2.extras.execute_batch(cur, insert_sql, rows, page_size=500)
    pg.commit()
    return len(rows)


def run_product(pg, product):
    started = time.time()
    pid = product["id"]
    print(f"\n  ── {pid} · {product['name']} ──")

    missing = missing_requirements(pg, product)
    if missing:
        reason = f"required L1 data not yet present: {', '.join(missing)}"
        print(f"     ⏸  SKIPPED — {reason}")
        log_run(pg, product, "skipped", skip_reason=reason,
                duration_seconds=round(time.time() - started, 2))
        return "skipped"

    try:
        with pg.cursor() as cur:
            cur.execute(product["sql"])
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()

        print(f"     computed: {len(rows)} row(s)")

        if DRY_RUN:
            print(f"     🧪 DRY RUN — would write {len(rows)} row(s) to "
                  f"{product['table']}. Nothing written.")
            for r in rows[:5]:
                print(f"        {dict(zip(columns, r))}")
            log_run(pg, product, "ok", rows_written=0,
                    duration_seconds=round(time.time() - started, 2))
            return "ok"

        written = replace_rows(pg, product["table"], rows, columns)
        print(f"     ✅ wrote {written} row(s) → {product['table']}")
        log_run(pg, product, "ok", rows_written=written,
                duration_seconds=round(time.time() - started, 2))
        return "ok"

    except Exception as e:
        print(f"     ❌ FAILED: {e}")
        pg.rollback()
        log_run(pg, product, "failed", error_message=str(e)[:2000],
                duration_seconds=round(time.time() - started, 2))
        return "failed"


if __name__ == "__main__":
    mode = "DRY RUN (nothing written)" if DRY_RUN else "PRODUCTION"
    print(f"🚀 Khabar L2 product engine — mode={mode}")
    print(f"   Registry: {len(PRODUCTS)} product(s) declared.\n")

    pg = psycopg2.connect(SUPABASE_DB_URL, sslrootcert=CA_BUNDLE)

    tally = {"ok": 0, "skipped": 0, "failed": 0}
    for product in PRODUCTS:
        tally[run_product(pg, product)] += 1

    print(f"\n🏁 Product run complete. "
          f"{tally['ok']} computed, {tally['skipped']} skipped, "
          f"{tally['failed']} failed.")

    pg.close()

    attempted = tally["ok"] + tally["failed"]
    if attempted and tally["ok"] == 0:
        print("   🛑 Every attempted product failed. Exiting non-zero.")
        sys.exit(1)

    sys.stdout.flush()
    os._exit(0)
