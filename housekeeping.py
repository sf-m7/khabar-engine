# ═══════════════════════════════════════════════════════
# KHABAR — Housekeeping (weekly maintenance, own workflow)
# ═══════════════════════════════════════════════════════
# Runs as its OWN scheduled job — Mondays 05:00 UTC, after the rollup
# (03:00) and the price_snapshots cold archive (04:00). Fully independent
# of scraper.py: a scraper failure never blocks maintenance again, and a
# maintenance failure never blocks tomorrow's scrape.
#
# WHY THIS FILE EXISTS
#   Maintenance chores used to live inside scraper.py, gated on the LCW
#   workflow — the least reliable scraper in the fleet. Worse, the
#   price_events purge there deleted WITHOUT archiving first, the exact
#   mistake already caught and removed once for price_snapshots. This file
#   rescues all of it and applies the proven export → upload → VERIFY →
#   delete gate from archive.py to every table that grows forever.
#
# WHAT THIS DOES, IN ORDER (each task independent — one failing
# does not stop the others):
#   1. stockout_events          — archive rows older than 60 days to R2,
#                                 verify readback, then delete from Supabase
#   2. price_events             — same, older than 30 days (replaces the
#                                 naked un-archived delete in scraper.py)
#   3. weekly_product_summary   — same, week_start older than 12 weeks.
#                                 The rollup was designed as "tiny, lives
#                                 forever" at 6 brands; at 19 brands it
#                                 grows ~7MB/week and must age out too.
#   4. stale-product delisting  — mark products/variants not seen in 14
#                                 days as delisted (state maintenance,
#                                 moved verbatim from scraper.py, no
#                                 longer hostage to the LCW workflow)
#   5. weekly_variant_exception — same, week_start older than 12 weeks.
#                                 Same shape and growth pattern as
#                                 weekly_product_summary (Task 3) — one row
#                                 per variant per week — and was missed when
#                                 Task 3 was added. Self-contained (already
#                                 carries size/color/price), no join needed.
#
# NOTHING here touches price_snapshots (archive.py owns that), and NOTHING
# touches products/product_variants rows themselves beyond flipping their
# is_active/delisted flags — first_observed_price and all baselines are
# untouched by design.
#
# File naming: every archive file is named {min_date}_to_{max_date} from
# the ACTUAL data inside it (fixing the reversed-name bug found in the
# first archive.py production run).
# ═══════════════════════════════════════════════════════

import io
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from supabase import create_client

sys.stdout.reconfigure(line_buffering=True)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_ACCOUNT_ID        = os.environ["R2_ACCOUNT_ID"]
R2_BUCKET_NAME       = os.environ["R2_BUCKET_NAME"]
R2_ENDPOINT_URL      = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

# Hot-window thresholds. Overridable per manual run (see housekeeping.yml),
# scheduled runs always use these defaults.
STOCKOUT_DAYS = int(os.environ.get("HK_STOCKOUT_DAYS_OVERRIDE", "21"))
PRICE_EVENT_DAYS = int(os.environ.get("HK_PRICE_EVENT_DAYS_OVERRIDE", "30"))
WEEKLY_SUMMARY_WEEKS = int(os.environ.get("HK_WEEKLY_SUMMARY_WEEKS_OVERRIDE", "2"))
WEEKLY_VARIANT_EXCEPTION_WEEKS = int(os.environ.get("HK_WEEKLY_VARIANT_EXCEPTION_WEEKS_OVERRIDE", "2"))
STALE_PRODUCT_DAYS = 14  # matches the scraper's original behaviour exactly

DRY_RUN = os.environ.get("HK_DRY_RUN", "false").lower() == "true"
# When true: every archive task runs its full export → upload → verify
# pipeline against real data, but the final delete is replaced with a
# print. The delisting task (state flags, not deletion) is SKIPPED
# entirely in dry-run mode — there is no safe "pretend" version of an
# UPDATE, so it simply reports what it would have flagged.

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

r2 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
)

failures = []  # collected per-task; job exits non-zero if any task failed


def safe_db_execute(query, retries=3, delay=2):
    """Same resilience pattern as scraper.py / archive.py."""
    for attempt in range(retries):
        try:
            return query.execute()
        except Exception as e:
            if attempt == retries - 1:
                print(f"  ❌ Supabase call permanently failed: {e}")
                return None
            print(f"  ⚠️ Supabase call failed, retrying in {delay}s... ({attempt+1}/{retries}): {e}")
            time.sleep(delay)
            delay *= 2


def fetch_all(table, columns, filter_col, cutoff_value):
    """Paginated fetch of every row where filter_col < cutoff_value.
    PostgREST silently caps at 1000 rows — same trap documented in
    archive.py and scraper.py, same .range() loop fix."""
    all_rows, offset = [], 0
    while True:
        chunk = safe_db_execute(
            supabase.table(table)
            .select(columns)
            .lt(filter_col, cutoff_value)
            .range(offset, offset + 999)
        )
        rows = (chunk.data or []) if chunk else []
        all_rows.extend(rows)
        if len(rows) < 1000:
            break
        offset += 1000
    return all_rows


def fetch_product_context(product_ids):
    """Dimension columns to flatten exports with — only for the products
    actually referenced, chunked to keep .in_() lists comfortable."""
    ctx = {}
    ids = [p for p in set(product_ids) if p is not None]
    for i in range(0, len(ids), 500):
        res = safe_db_execute(
            supabase.table("products")
            .select("id, name, category_normalized, gender")
            .in_("id", ids[i:i + 500])
        )
        for r in (res.data or []) if res else []:
            ctx[r["id"]] = r
    return ctx


def write_parquet(rows):
    table = pa.Table.from_pylist(rows)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf


def verify_upload(object_key, expected_row_count):
    """THE GATE — identical philosophy to archive.py: download the file
    BACK from R2 and count actual rows. Exceptions = failed gate."""
    try:
        response = r2.get_object(Bucket=R2_BUCKET_NAME, Key=object_key)
        downloaded = response["Body"].read()
        table = pq.read_table(io.BytesIO(downloaded))
        if table.num_rows != expected_row_count:
            print(f"  ❌ GATE FAILED: expected {expected_row_count} rows, "
                  f"R2 file has {table.num_rows}. Nothing will be deleted.")
            return False
        print(f"  ✅ GATE PASSED: {table.num_rows} rows confirmed in R2.")
        return True
    except Exception as e:
        print(f"  ❌ GATE FAILED with exception: {e}. Nothing will be deleted.")
        return False


def delete_by_ids(table, ids):
    """Delete archived rows by primary key, chunked."""
    deleted = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        res = safe_db_execute(supabase.table(table).delete().in_("id", chunk))
        if res is None:
            print(f"  ⚠️ Delete chunk failed for {table} at offset {i} — "
                  f"those rows stay hot and will be re-archived next run "
                  f"(the file name will differ; contents overlap is harmless).")
            continue
        deleted += len(chunk)
    return deleted


def archive_table(label, table, columns, filter_col, cutoff_value,
                  date_field, flatten_fn=None):
    """The whole export → upload → verify → delete pipeline for one table.
    Returns True on success (including 'nothing to do'), False on failure."""
    print(f"\n📦 [{label}] archiving rows where {filter_col} < {cutoff_value} ...")
    rows = fetch_all(table, columns, filter_col, cutoff_value)
    if not rows:
        print(f"  Nothing to archive — no rows older than the hot window.")
        return True
    print(f"  Found {len(rows)} rows eligible.")

    if flatten_fn:
        rows = flatten_fn(rows)

    ids = [r.pop("id") for r in rows]  # id used for delete, not archived

    dates = sorted(str(r[date_field])[:10] for r in rows)
    prefix = f"_pilot_dry_run/{table}" if DRY_RUN else table
    object_key = f"{prefix}/{dates[0]}_to_{dates[-1]}.parquet"

    buf = write_parquet(rows)
    print(f"  Parquet file: {round(buf.getbuffer().nbytes / 1024, 1)} KB "
          f"for {len(rows)} rows. Uploading as: {object_key}")
    try:
        buf.seek(0)
        r2.upload_fileobj(buf, R2_BUCKET_NAME, object_key)
    except Exception as e:
        print(f"  ❌ Upload to R2 failed: {e}. Nothing deleted.")
        return False

    if not verify_upload(object_key, expected_row_count=len(rows)):
        print("  🛑 Rows remain in Supabase, untouched. Next run retries.")
        return False

    if DRY_RUN:
        print(f"  🧪 DRY RUN — would have deleted {len(ids)} rows from "
              f"{table}. Skipping delete.")
        return True

    deleted = delete_by_ids(table, ids)
    print(f"  🗑️  Deleted {deleted} rows from {table} "
          f"(verified present in R2 first).")
    return True


# ─────────────────────────────────────────────
# Task 1 + 2 flattener — join product context in
# ─────────────────────────────────────────────
def flatten_with_product(rows):
    ctx = fetch_product_context([r.get("product_id") for r in rows])
    for r in rows:
        p = ctx.get(r.get("product_id"), {})
        r["product_name"]        = p.get("name")
        r["category_normalized"] = p.get("category_normalized")
        r["gender"]              = p.get("gender")
        # jsonb column can't ride into Parquet as a dict — stringify
        if isinstance(r.get("sizes_in_stock"), (dict, list)):
            r["sizes_in_stock"] = str(r["sizes_in_stock"])
    return rows


# ─────────────────────────────────────────────
# Task 4 — stale-product delisting (moved from
# scraper.py, no longer gated on the LCW run)
# ─────────────────────────────────────────────
def delist_stale_products():
    """
    Drains the FULL backlog each run rather than a fixed 500/200-row slice.
    The old scraper version capped at 500 products / 200 variants per call
    — fine if it ran daily without fail, silently inadequate once it
    started skipping days (a 22,843-variant backlog was found sitting
    behind that cap in production). This version keeps re-fetching and
    updating in batches of 500 until nothing stale remains, however large
    the backlog is. Each production batch flips is_active/delisted_at
    before the next fetch, so the same rows are never re-selected — the
    loop provably terminates.

    DRY_RUN never mutates state, so a repeated fetch would return the same
    500 rows forever. Instead it asks Supabase directly for an exact count
    — giving an honest total rather than a batch-sized guess.
    """
    print(f"\n🏷️  [delisting] flagging products unseen for "
          f"{STALE_PRODUCT_DAYS}+ days ...")
    now_iso     = datetime.now(timezone.utc).isoformat()
    cutoff_seen = (datetime.now(timezone.utc)
                   - timedelta(days=STALE_PRODUCT_DAYS)).isoformat()

    if DRY_RUN:
        p_count = safe_db_execute(
            supabase.table("products")
            .select("id", count="exact")
            .eq("is_active", True)
            .lt("last_seen_at", cutoff_seen)
        )
        v_count = safe_db_execute(
            supabase.table("product_variants")
            .select("id, products!inner(last_seen_at)", count="exact")
            .is_("delisted_at", "null")
            .lt("products.last_seen_at", cutoff_seen)
        )
        total_p = p_count.count if p_count and p_count.count is not None else 0
        total_v = v_count.count if v_count and v_count.count is not None else 0
        print(f"  🧪 DRY RUN — would delist {total_p} products and "
              f"{total_v} variants (full backlog, not a batch cap). "
              f"No flags changed.")
        return True

    # ---- Products: drain in batches of 500 until none remain ----
    total_products = 0
    while True:
        batch = safe_db_execute(
            supabase.table("products")
            .select("id, brand")
            .eq("is_active", True)
            .lt("last_seen_at", cutoff_seen)
            .limit(500)
        )
        rows = (batch.data or []) if batch else []
        if not rows:
            break
        pids = [r["id"] for r in rows]
        safe_db_execute(
            supabase.table("products")
            .update({"is_active": False, "delisted_at": now_iso})
            .in_("id", pids)
        )
        total_products += len(pids)
    if total_products:
        print(f"  Marked {total_products} stale products as delisted.")

    # ---- Variants: drain in batches of 500 until none remain ----
    total_variants = 0
    while True:
        batch = safe_db_execute(
            supabase.table("product_variants")
            .select("id, product_id, size, color, products!inner(brand, last_seen_at)")
            .is_("delisted_at", "null")
            .lt("products.last_seen_at", cutoff_seen)
            .limit(500)
        )
        vrows = (batch.data or []) if batch else []
        if not vrows:
            break

        event_rows = [{
            "variant_id":            v["id"],
            "product_id":            v.get("product_id"),
            "brand":                 (v.get("products") or {}).get("brand"),
            "size":                  v.get("size"),
            "color":                 v.get("color"),
            "event_type":            "delisted",
            "price_at_event":        None,
            "discount_pct_at_event": None,
            "was_on_discount":       False,
            "recorded_at":           now_iso,
        } for v in vrows]
        for i in range(0, len(event_rows), 100):
            safe_db_execute(supabase.table("stockout_events").insert(event_rows[i:i + 100]))

        vids = [v["id"] for v in vrows]
        safe_db_execute(
            supabase.table("product_variants")
            .update({"delisted_at": now_iso, "is_in_stock": False})
            .in_("id", vids)
        )
        total_variants += len(vids)
    if total_variants:
        print(f"  Recorded {total_variants} variant delisting events.")

    if not total_products and not total_variants:
        print("  Nothing stale — catalog is fully fresh.")
    return True


# ─────────────────────────────────────────────
# Main — each task isolated; one failure never
# blocks the rest, but any failure fails the job
# so it's visible in the Actions tab.
# ─────────────────────────────────────────────
def main():
    mode = "PILOT (dry run, no deletion)" if DRY_RUN else "PRODUCTION"
    print(f"🚀 Khabar housekeeping starting... mode={mode}")

    now = datetime.now(timezone.utc)

    # Task 1 — stockout_events (recorded_at, timestamptz)
    try:
        ok = archive_table(
            label="stockout_events",
            table="stockout_events",
            columns="id, variant_id, product_id, brand, size, color, "
                    "event_type, price_at_event, discount_pct_at_event, "
                    "was_on_discount, recorded_at",
            filter_col="recorded_at",
            cutoff_value=(now - timedelta(days=STOCKOUT_DAYS)).isoformat(),
            date_field="recorded_at",
            flatten_fn=flatten_with_product,
        )
        if not ok:
            failures.append("stockout_events")
    except Exception as e:
        print(f"  ❌ stockout_events task crashed: {e}")
        failures.append("stockout_events")

    # Task 2 — price_events (recorded_at, timestamptz)
    try:
        ok = archive_table(
            label="price_events",
            table="price_events",
            columns="id, product_id, brand, price_before, price_after, "
                    "compare_at_price, discount_pct, direction, "
                    "sizes_in_stock, is_statistical_deal, is_flash_sale, "
                    "recorded_at",
            filter_col="recorded_at",
            cutoff_value=(now - timedelta(days=PRICE_EVENT_DAYS)).isoformat(),
            date_field="recorded_at",
            flatten_fn=flatten_with_product,
        )
        if not ok:
            failures.append("price_events")
    except Exception as e:
        print(f"  ❌ price_events task crashed: {e}")
        failures.append("price_events")

    # Task 3 — weekly_product_summary (week_start, date). Rows already
    # carry brand/category/gender — self-contained by design, so no join.
    try:
        cutoff_week = (date.today()
                       - timedelta(weeks=WEEKLY_SUMMARY_WEEKS)).isoformat()
        ok = archive_table(
            label="weekly_product_summary",
            table="weekly_product_summary",
            columns="id, product_id, brand, category_normalized, gender, "
                    "week_start, iso_week, iso_year, price_median, "
                    "price_min, price_max, price_open, price_close, "
                    "first_observed_price, discount_event_count, "
                    "max_discount_pct, avg_discount_depth_pct, "
                    "days_on_discount, sizes_total, sizes_in_stock_avg, "
                    "stockout_count, restock_count, is_active, delisted, "
                    "sample_days, recorded_at",
            filter_col="week_start",
            cutoff_value=cutoff_week,
            date_field="week_start",
        )
        if not ok:
            failures.append("weekly_product_summary")
    except Exception as e:
        print(f"  ❌ weekly_product_summary task crashed: {e}")
        failures.append("weekly_product_summary")

    # Task 4 — stale-product delisting (state flags, no deletion)
    try:
        if not delist_stale_products():
            failures.append("delisting")
    except Exception as e:
        print(f"  ❌ delisting task crashed: {e}")
        failures.append("delisting")

    # Task 5 — weekly_variant_exception (week_start, date). Rows already
    # carry variant_id/product_id/size/color/price — self-contained by
    # design, same as Task 3, so no join needed.
    try:
        cutoff_week = (date.today()
                       - timedelta(weeks=WEEKLY_VARIANT_EXCEPTION_WEEKS)).isoformat()
        ok = archive_table(
            label="weekly_variant_exception",
            table="weekly_variant_exception",
            columns="id, variant_id, product_id, week_start, iso_week, "
                    "iso_year, size, color, price, is_in_stock, "
                    "days_in_stock, stockout_count, restock_count, "
                    "price_median, price_min, price_max, "
                    "discount_depth_pct, price_diverged, recorded_at",
            filter_col="week_start",
            cutoff_value=cutoff_week,
            date_field="week_start",
        )
        if not ok:
            failures.append("weekly_variant_exception")
    except Exception as e:
        print(f"  ❌ weekly_variant_exception task crashed: {e}")
        failures.append("weekly_variant_exception")

# Task 6 — weekly_bestseller_summary, same pattern as Task 5.
    try:
        cutoff_bs_week = (date.today()
                          - timedelta(weeks=2)).isoformat()
        ok = archive_table(
            label="weekly_bestseller_summary",
            table="weekly_bestseller_summary",
            columns="id, product_id, brand, week_start, iso_week, iso_year, "
                    "rank_best, rank_worst, rank_avg, rank_close, "
                    "rank_change_vs_prev_week, sample_days, recorded_at",
            filter_col="week_start",
            cutoff_value=cutoff_bs_week,
            date_field="week_start",
        )
        if not ok:
            failures.append("weekly_bestseller_summary")
    except Exception as e:
        print(f"  ❌ weekly_bestseller_summary task crashed: {e}")
        failures.append("weekly_bestseller_summary")
    
    
    if failures:
        print(f"\n🏁 Housekeeping finished WITH FAILURES: {', '.join(failures)}. "
              f"Failed tables remain fully intact in Supabase and will be "
              f"retried next run.")
        sys.exit(1)
    print("\n🏁 Housekeeping complete. All tasks succeeded.")


if __name__ == "__main__":
    main()
