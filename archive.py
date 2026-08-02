# ═══════════════════════════════════════════════════════
# KHABAR — Cold Archive (price_snapshots → Cloudflare R2)
# ═══════════════════════════════════════════════════════
# Runs as its OWN scheduled job, fully independent of scraper.py and bot.py.
# A failure here has zero effect on price scraping — it just means today's
# batch of aging rows wait for the next run, which is a non-event.
#
# WHAT THIS DOES, IN STRICT ORDER (see archive-architecture report):
#   1. SELECT  — rows in price_snapshots older than ARCHIVE_THRESHOLD_DAYS
#   2. FLATTEN — join in brand/category/gender/size/color so each row is
#                self-contained (no foreign keys once it leaves Supabase)
#   3. WRITE   — save as Parquet (queryable in place via DuckDB later,
#                unlike a plain CSV dump)
#   4. UPLOAD  — push the Parquet file to the R2 bucket
#   5. VERIFY  — download the file BACK from R2 and check the row count
#                matches exactly what was selected in step 1
#   6. DELETE  — ONLY if step 5 passes exactly, delete those rows from
#                Supabase by their primary key (id), nothing else
#
# Any failure or mismatch at any step aborts BEFORE deletion. Nothing is
# ever deleted on a guess.
#
# PHASE 1 (below): price_snapshots — unchanged, proven.
# PHASE 2 (further below): the append-only signal_l1_*/signal_l2_*/product_l2_*
#   and weekly rollup tables, which DO have the unbounded-growth problem now.
#   Same verify-before-delete contract, reusing the same guard functions. It is
#   OFF by default (ARCHIVE_SIGNALS_ENABLED) so this file can be merged safely
#   and piloted with ARCHIVE_DRY_RUN=true before it deletes anything.
#   Still untouched by BOTH phases: products, product_variants, price_events,
#   stockout_events, bestseller_rank (live/current-state or feeds the rollup).
# ═══════════════════════════════════════════════════════

import io
import os
import sys
from datetime import date, timedelta

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

ARCHIVE_THRESHOLD_DAYS = int(os.environ.get("ARCHIVE_THRESHOLD_DAYS_OVERRIDE", "8"))
# Default is 8, matching the hot window described in the architecture
# report and the scraper's own purge logic elsewhere in the codebase.
# ARCHIVE_THRESHOLD_DAYS_OVERRIDE exists ONLY so a manual workflow_dispatch
# run can pilot the full export -> upload -> verify pipeline against real
# (but young) data before 35 real days have ever passed — see DRY_RUN below
# for why this is still safe to do even with a shorter window.

DRY_RUN = os.environ.get("ARCHIVE_DRY_RUN", "false").lower() == "true"
# When true: every step runs for real EXCEPT the final delete — step 6 is
# replaced with a print statement. This lets the riskiest, never-yet-tested
# parts of this script (the actual R2 upload, the actual download-and-verify
# readback) be exercised against real data with zero chance of deleting
# anything, regardless of outcome. Intended for manual workflow_dispatch
# pilot runs, never for the scheduled run.

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

r2 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
)


def safe_db_execute(query, retries=3, delay=2):
    """Same resilience pattern already used in scraper.py / rollup.py —
    retry transient Supabase/PostgREST failures before giving up."""
    import time
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


def fetch_aging_snapshot_ids(cutoff_date):
    """
    Step 1 — find every price_snapshots row older than the cutoff.
    Paginated via .range() — PostgREST silently caps responses at 1000 rows
    regardless of table size, the same trap documented (and fixed) elsewhere
    in this codebase (see load_last_prices in scraper.py).
    Returns the raw rows AND the set of ids, since the id set is what step 6
    deletes by, and the row count is what step 5 verifies against.
    """
    all_rows, offset = [], 0
    while True:
        chunk = safe_db_execute(
            supabase.table("price_snapshots")
            .select("id, product_id, variant_id, brand, price, compare_at_price, discount_pct, snapshot_date, recorded_at")
            .lt("snapshot_date", str(cutoff_date))
            .range(offset, offset + 999)
        )
        rows = (chunk.data or []) if chunk else []
        all_rows.extend(rows)
        if len(rows) < 1000:
            break
        offset += 1000
    return all_rows


def fetch_lookup_tables(product_ids, variant_ids):
    """
    Pulls just the dimension columns needed to flatten the export, for only
    the products/variants actually referenced by the aging rows — not a
    full-table read. Paginated the same way, since product_ids/variant_ids
    can exceed PostgREST's .in_() comfortable size in one shot for a large
    archive batch (chunked into groups of 500 ids per call to stay safe).
    """
    products_map, variants_map = {}, {}

    product_ids = sorted(p for p in set(product_ids) if p is not None)
    for i in range(0, len(product_ids), 500):
        chunk_ids = product_ids[i:i + 500]
        res = safe_db_execute(
            supabase.table("products")
            .select("id, name, category_normalized, category_raw, gender, attributes_extracted")
            .in_("id", chunk_ids)
        )
        for row in (res.data or []) if res else []:
            products_map[row["id"]] = row

    variant_ids = sorted(v for v in set(variant_ids) if v is not None)
    for i in range(0, len(variant_ids), 500):
        chunk_ids = variant_ids[i:i + 500]
        res = safe_db_execute(
            supabase.table("product_variants")
            .select("id, size, color")
            .in_("id", chunk_ids)
        )
        for row in (res.data or []) if res else []:
            variants_map[row["id"]] = row

    return products_map, variants_map


def flatten_rows(rows, products_map, variants_map):
    """
    Step 2 — widen each row so it stands alone with no foreign keys.
    variant_id is nullable (product-level snapshots, see build_snapshot_rows
    in scraper.py for why): size/color are only pulled when a variant_id
    is actually present, never guessed.

    category_raw and attributes_extracted were added after a direct
    comparison against the live Supabase schema showed they were the only
    two product-level fields missing from the archive — everything else
    (price/compare_at_price/discount_pct/snapshot_date/recorded_at/brand
    from price_snapshots; name/category_normalized/gender from products;
    size/color from product_variants) already matched the source column
    names exactly. attributes_extracted is JSONB in Postgres (a small dict
    like {"fit": "wide leg"} or {} when nothing was extracted) — Parquet has
    no native dict type, so it's stored here as a JSON STRING column, which
    is the standard lossless way to carry a variable-shaped object through
    Parquet and is exactly the form DuckDB's json_extract() expects to query
    directly (e.g. json_extract(attributes_extracted, '$.fit')).
    """
    import json as _json

    flattened = []
    for r in rows:
        product = products_map.get(r.get("product_id")) or {}
        variant = variants_map.get(r.get("variant_id")) if r.get("variant_id") else {}
        variant = variant or {}
        attrs = product.get("attributes_extracted")
        flattened.append({
            "snapshot_id":          r["id"],
            "product_id":           r.get("product_id"),
            "variant_id":           r.get("variant_id"),
            "brand":                r.get("brand"),
            "product_name":         product.get("name"),
            "category_normalized":  product.get("category_normalized"),
            "category_raw":         product.get("category_raw"),
            "gender":               product.get("gender"),
            "size":                 variant.get("size"),
            "color":                variant.get("color"),
            "attributes_extracted": _json.dumps(attrs) if attrs is not None else None,
            "price":                float(r["price"]) if r.get("price") is not None else None,
            "compare_at_price":     float(r["compare_at_price"]) if r.get("compare_at_price") is not None else None,
            "discount_pct":         float(r["discount_pct"]) if r.get("discount_pct") is not None else None,
            "snapshot_date":        str(r.get("snapshot_date")),
            "recorded_at":          r.get("recorded_at"),
        })
    return flattened


def write_parquet(flattened_rows):
    """Step 3 — write to an in-memory Parquet buffer (no temp file needed,
    GitHub Actions runners are ephemeral anyway)."""
    table = pa.Table.from_pylist(flattened_rows)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    buf.seek(0)
    return buf


def upload_to_r2(buf, object_key):
    """Step 4 — push the Parquet file to R2."""
    buf.seek(0)
    r2.upload_fileobj(buf, R2_BUCKET_NAME, object_key)


def r2_object_exists(object_key):
    """True if this day's Parquet file is already in the bucket."""
    try:
        r2.head_object(Bucket=R2_BUCKET_NAME, Key=object_key)
        return True
    except Exception:
        return False


def read_existing_rows(object_key):
    """
    Pull back the rows already stored in a day's file, as plain dicts.
    Used to MERGE rather than overwrite: a day can legitimately be touched
    by more than one archive run (a partial/failed run, a re-run, or the
    first daily run after a backlog sweep). Overwriting would silently drop
    whatever was already archived for that date — merging cannot.
    """
    response = r2.get_object(Bucket=R2_BUCKET_NAME, Key=object_key)
    table = pq.read_table(io.BytesIO(response["Body"].read()))
    return table.to_pylist()


def verify_upload(object_key, expected_row_count):
    """
    Step 5 — THE GATE. Download the file back from R2 (not just trust the
    upload's HTTP 200) and check its actual row count matches what was
    selected in step 1, exactly. Returns True only on an exact match.
    Any exception here (network blip, missing object, corrupt file) is
    treated as a failed gate, not retried-into-a-pass.
    """
    try:
        response = r2.get_object(Bucket=R2_BUCKET_NAME, Key=object_key)
        downloaded = response["Body"].read()
        table = pq.read_table(io.BytesIO(downloaded))
        actual_row_count = table.num_rows
        if actual_row_count != expected_row_count:
            print(f"  ❌ GATE FAILED: expected {expected_row_count} rows, "
                  f"R2 file has {actual_row_count}. Nothing will be deleted.")
            return False
        print(f"  ✅ GATE PASSED: {actual_row_count} rows confirmed in R2, "
              f"matches Supabase selection exactly.")
        return True
    except Exception as e:
        print(f"  ❌ GATE FAILED: could not verify R2 object ({e}). "
              f"Nothing will be deleted.")
        return False


def delete_archived_rows(snapshot_ids):
    """
    Step 6 — delete ONLY by the exact primary keys that were archived and
    verified. Never a date-range delete here — id-based, so there is no
    way to delete a row that wasn't individually confirmed present in the
    verified R2 file. Chunked to stay well under any request size limits.
    """
    deleted = 0
    for i in range(0, len(snapshot_ids), 500):
        chunk = snapshot_ids[i:i + 500]
        res = safe_db_execute(
            supabase.table("price_snapshots").delete().in_("id", chunk)
        )
        if res is not None:
            deleted += len(chunk)
    return deleted


def archive_price_snapshots():
    """The original, proven price_snapshots archive — logic UNCHANGED. Only the
    terminal sys.exit()/os._exit() calls became `return`s so a second phase can
    run after it. Returns 'nothing' | 'ok' | 'failed'."""
    mode_label = "PILOT (dry run, no deletion)" if DRY_RUN else "PRODUCTION"
    print(f"🚀 Khabar cold archive starting... mode={mode_label}")
    cutoff = date.today() - timedelta(days=ARCHIVE_THRESHOLD_DAYS)
    print(f"  Archiving price_snapshots older than {cutoff} "
          f"({ARCHIVE_THRESHOLD_DAYS}-day window{' — OVERRIDDEN for this pilot run' if DRY_RUN else ''})...")

    rows = fetch_aging_snapshot_ids(cutoff)
    if not rows:
        print("  Nothing to archive yet — no rows older than the hot window.")
        return "nothing"

    print(f"  Found {len(rows)} rows eligible for archiving.")

    product_ids = [r.get("product_id") for r in rows]
    variant_ids = [r.get("variant_id") for r in rows]
    print("  Pulling product/variant context for flattening...")
    products_map, variants_map = fetch_lookup_tables(product_ids, variant_ids)

    print("  Flattening rows (joining in brand, category, gender, size, color)...")
    flattened = flatten_rows(rows, products_map, variants_map)

    # ------------------------------------------------------------------
    # v2: ONE PARQUET FILE PER CALENDAR DAY — price_snapshots/YYYY-MM-DD.parquet
    #
    # Previously this wrote a single batch file per run, named by the range
    # it happened to cover. That made the bucket's layout a function of WHEN
    # the job ran rather than WHAT the data is, so a missed Monday produced a
    # 13-day file while a normal week produced a 7-day one — and any query for
    # "the last 30 days" had to open and scan every file ever written to find
    # out which ones were relevant.
    #
    # Day-partitioning makes the filename the index: DuckDB can skip whole
    # files by name (price_snapshots/2026-07-*.parquet) instead of reading
    # them, which is what keeps the L1/L2 queries fast as the lake grows.
    #
    # MERGE, NEVER OVERWRITE: a given day can legitimately be touched by more
    # than one run (a partial failure, a re-run, or the first daily run after
    # a backlog sweep). If a file already exists for that date, its rows are
    # read back and merged, de-duplicated on snapshot_id. Blindly overwriting
    # would silently destroy already-archived rows — the one thing this
    # pipeline must never do.
    #
    # The verify-before-delete gate is now PER DAY: rows are only deleted from
    # Supabase for days whose file was successfully uploaded AND read back with
    # a matching row count. A failure on one day leaves that day's rows fully
    # intact in Supabase for the next run, without blocking the days that did
    # succeed.
    # ------------------------------------------------------------------
    prefix = "_pilot_dry_run" if DRY_RUN else "price_snapshots"

    by_day = {}
    for row in flattened:
        by_day.setdefault(row["snapshot_date"], []).append(row)

    print(f"  Grouped into {len(by_day)} calendar day(s): "
          f"{min(by_day)} → {max(by_day)}")

    verified_ids = []      # snapshot_ids safe to delete (their day passed the gate)
    failed_days = []

    for day in sorted(by_day):
        day_rows = by_day[day]
        object_key = f"{prefix}/{day}.parquet"

        # --- merge with anything already archived for this date ---
        rows_to_write = day_rows
        if r2_object_exists(object_key):
            try:
                existing = read_existing_rows(object_key)
                merged = {r["snapshot_id"]: r for r in existing}
                merged.update({r["snapshot_id"]: r for r in day_rows})
                rows_to_write = list(merged.values())
                print(f"  [{day}] file exists with {len(existing)} row(s) — "
                      f"merging to {len(rows_to_write)} total.")
            except Exception as e:
                print(f"  ❌ [{day}] could not read existing file to merge: {e}. "
                      f"Skipping this day — its rows stay in Supabase.")
                failed_days.append(day)
                continue

        try:
            buf = write_parquet(rows_to_write)
            size_kb = len(buf.getvalue()) / 1024
            upload_to_r2(buf, object_key)
        except Exception as e:
            print(f"  ❌ [{day}] upload failed: {e}. Rows stay in Supabase.")
            failed_days.append(day)
            continue

        if not verify_upload(object_key, expected_row_count=len(rows_to_write)):
            print(f"  🛑 [{day}] verification failed. Rows stay in Supabase.")
            failed_days.append(day)
            continue

        print(f"  ✅ [{day}] {object_key} — {len(rows_to_write)} rows, {size_kb:.1f} KB.")
        verified_ids.extend(r["snapshot_id"] for r in day_rows)

    if not verified_ids:
        print("  🛑 No day passed the verify gate. Nothing deleted.")
        return "failed"

    if DRY_RUN:
        print(f"  🧪 DRY RUN — would have deleted {len(verified_ids)} rows across "
              f"{len(by_day) - len(failed_days)} verified day(s). Nothing touched.")
        deleted_count = 0
    else:
        deleted_count = delete_archived_rows(verified_ids)
        print(f"  🗑️  Deleted {deleted_count} rows from Supabase price_snapshots "
              f"(each verified present in R2 first).")

    if failed_days:
        print(f"  ⚠️  {len(failed_days)} day(s) failed and were NOT deleted: "
              f"{', '.join(failed_days)}. They remain intact in Supabase and "
              f"will be retried on the next run.")

    if DRY_RUN:
        print(f"\n🏁 Pilot run complete. {len(verified_ids)} rows round-tripped "
              f"through export → per-day R2 upload → verified readback. "
              f"0 rows deleted (dry run).")
    else:
        print(f"\n🏁 Archive run complete. {deleted_count} rows moved to R2 across "
              f"{len(by_day) - len(failed_days)} day-file(s), removed from the hot tier.")

    return "ok"


# ═══════════════════════════════════════════════════════
# PHASE 2 — append-only signal / product / rollup tables → R2
# ═══════════════════════════════════════════════════════
# Same safety contract as phase 1: SELECT the aging rows, WRITE them to R2 as
# Parquet, VERIFY the file's row count matches, and only THEN delete — from
# Supabase. Two deliberate differences from the price path, both because these
# tables behave differently:
#
#   • DELETE BY DATE, not by id. A past snapshot_date/report_date/week_start is
#     IMMUTABLE here — compute_signals only ever writes TODAY's partition and
#     never rewrites an older one. So a whole past day is a safe, complete unit
#     to verify-then-delete. (price_snapshots deletes by id because its current
#     day is still being written; these tables have no such live day among the
#     aging rows.)
#   • OVERWRITE the day file, don't merge. Because the day is immutable, the
#     Supabase rows for it ARE the complete truth; re-archiving writes the same
#     set. And since the delete is atomic per day (one filtered DELETE), a day
#     is never half-in-Supabase / half-in-R2.
#
# ROLLOUT SAFETY: this whole phase is OFF unless ARCHIVE_SIGNALS_ENABLED=true,
# so dropping the file into the repo changes NOTHING until you pilot it. Pilot
# with ARCHIVE_DRY_RUN=true first (writes + verifies R2, deletes nothing), read
# the log, THEN set ARCHIVE_SIGNALS_ENABLED=true.
#
# KNOWN DEPENDENCY TO CONFIRM BEFORE ENABLING: the reports only read the LATEST
# day of each table, so 7 days is ample for them. But if the PRODUCT computation
# or the Telegram BOT read signal history further back than the keep window,
# they'll be starved. Confirm those read windows first. Worst case is anyway
# recoverable — everything is in R2 before a single row is deleted.
# ═══════════════════════════════════════════════════════

ARCHIVE_SIGNALS_ENABLED = os.environ.get("ARCHIVE_SIGNALS_ENABLED", "false").lower() == "true"
SIGNAL_KEEP_DAYS = int(os.environ.get("SIGNAL_ARCHIVE_KEEP_DAYS", "7"))    # l1/l2 signals + l2 products
ROLLUP_KEEP_DAYS = int(os.environ.get("ROLLUP_ARCHIVE_KEEP_DAYS", "28"))   # weekly rollups (4 weeks)

# Table families discovered by pattern so a NEW signal is covered automatically
# (an unarchived append-only table is exactly the silent-growth trap this fixes).
SIGNAL_TABLE_PATTERNS = ("signal_l1_", "signal_l2_", "product_l2_")

# Rollups are named individually (no shared pattern) — explicit list, keep 28d.
ROLLUP_TABLES = ("weekly_bestseller_summary", "weekly_product_summary",
                 "weekly_variant_exception")

# NEVER archive these, whatever a pattern might match: live current-state tables
# the scraper/bot read, the price path handled above, run logs, and the raw
# bestseller feed the weekly rollup still reads.
ARCHIVE_BLOCKLIST = {
    "products", "product_variants", "price_snapshots", "price_events",
    "stockout_events", "signal_runs", "product_runs", "bestseller_rank",
}


# Fallback used only if live discovery can't run (no SUPABASE_DB_URL in this
# job's env). Auto-discovery is preferred so a NEWLY added signal is covered
# without editing this file; this list is the safety net, not the source of
# truth. Keep it in sync if you add signals AND can't enable discovery.
_FALLBACK_TARGETS = (
    [(f"signal_{s}", "snapshot_date", None) for s in (
        "l1_01_genuine_price_drop", "l1_03_price_staircase", "l1_04_anchor_inflation",
        "l1_06_discount_recovery", "l1_07_price_anomaly", "l1_08_variant_stockout",
        "l1_09_variant_restock", "l1_10_dead_stock", "l1_11_size_asymmetry",
        "l1_12_new_sku_launch", "l1_13_product_delisted", "l1_14_launch_to_discount",
        "l1_17_depth_escalation", "l1_22_discount_velocity", "l1_24_restock_density",
        "l2_discount_honesty", "l2_first_mover", "l2_market_velocity",
        "l2_pricing_discipline", "l2_replenishment_benchmark", "l2_share_of_launch",
        "l2_size_demand_curve", "l2_trained_customer")]
    + [(f"product_{p}", "report_date", None) for p in (
        "l2_01_price_elasticity", "l2_02_production_blueprint", "l2_08_brand_health",
        "l2_09_revealed_demand", "l2_10_market_entry", "l2_12_liquidation_calendar",
        "l2_13_wallet_allocator")]
)


def discover_targets():
    """Return [(table, date_col, keep_days), ...] to archive. Signal/product
    tables auto-discovered by pattern; rollups explicit. Blocklist always wins.
    Falls back to a built-in list if the DB can't be introspected here."""
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        print("  ⚠️ SUPABASE_DB_URL not set — using built-in target list "
              "(new signals won't be auto-covered until it is).")
        out = [(t, dc, SIGNAL_KEEP_DAYS) for t, dc, _ in _FALLBACK_TARGETS
               if t not in ARCHIVE_BLOCKLIST]
        out += [(t, "week_start", ROLLUP_KEEP_DAYS) for t in ROLLUP_TABLES]
        return sorted(out)

    import psycopg2
    targets = []
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name, string_agg(column_name, ',') AS cols
                FROM information_schema.columns
                WHERE table_schema = 'public'
                GROUP BY table_name
            """)
            colmap = {t: set(c.split(",")) for t, c in cur.fetchall()}
    finally:
        conn.close()

    for table, cols in colmap.items():
        if table in ARCHIVE_BLOCKLIST:
            continue
        if table in ROLLUP_TABLES:
            if "week_start" in cols:
                targets.append((table, "week_start", ROLLUP_KEEP_DAYS))
            continue
        if any(table.startswith(p) for p in SIGNAL_TABLE_PATTERNS):
            date_col = ("snapshot_date" if "snapshot_date" in cols
                        else "report_date" if "report_date" in cols else None)
            if date_col:
                targets.append((table, date_col, SIGNAL_KEEP_DAYS))
    return sorted(targets)


def _sanitize(row):
    """Parquet has no dict/list type. Any nested value (e.g. l1_11's
    sizes_still_in_stock array, JSONB columns) is stored as a JSON string —
    lossless and queryable via DuckDB json_extract() later."""
    import json as _json
    out = {}
    for k, v in row.items():
        out[k] = _json.dumps(v) if isinstance(v, (dict, list)) else v
    return out


def fetch_aging_table_rows(table, date_col, cutoff):
    """All rows of `table` older than cutoff, paginated past PostgREST's 1000 cap."""
    all_rows, offset = [], 0
    while True:
        chunk = safe_db_execute(
            supabase.table(table).select("*")
            .lt(date_col, str(cutoff))
            .order(date_col)
            .range(offset, offset + 999)
        )
        rows = (chunk.data or []) if chunk else []
        all_rows.extend(rows)
        if len(rows) < 1000:
            break
        offset += 1000
    return all_rows


def delete_table_day(table, date_col, day):
    """Delete one immutable past partition, atomically, by its date value only."""
    res = safe_db_execute(supabase.table(table).delete().eq(date_col, str(day)))
    return res is not None


def archive_one_table(table, date_col, keep_days):
    """SELECT aging rows → per-day Parquet to R2 → verify count → delete that
    day. Returns (archived_days, deleted_rows, failed_days)."""
    cutoff = date.today() - timedelta(days=keep_days)
    rows = fetch_aging_table_rows(table, date_col, cutoff)
    if not rows:
        print(f"  · {table}: nothing older than {cutoff} (keep {keep_days}d).")
        return 0, 0, 0

    by_day = {}
    for r in rows:
        by_day.setdefault(str(r[date_col]), []).append(_sanitize(r))

    prefix = "_pilot_dry_run/tables" if DRY_RUN else "tables"
    ok_days, deleted, failed = 0, 0, 0
    for day in sorted(by_day):
        day_rows = by_day[day]
        object_key = f"{prefix}/{table}/{day}.parquet"
        try:
            buf = write_parquet(day_rows)          # reused guard
            upload_to_r2(buf, object_key)          # reused guard
        except Exception as e:
            print(f"  ❌ [{table} {day}] upload failed: {e}. Rows stay in Supabase.")
            failed += 1
            continue
        if not verify_upload(object_key, expected_row_count=len(day_rows)):   # reused gate
            print(f"  🛑 [{table} {day}] verify failed. Rows stay in Supabase.")
            failed += 1
            continue
        if DRY_RUN:
            print(f"  🧪 [{table} {day}] {len(day_rows)} rows verified in R2 — "
                  f"would delete. Nothing touched.")
            ok_days += 1
            continue
        if delete_table_day(table, date_col, day):
            print(f"  ✅ [{table} {day}] {len(day_rows)} rows → R2, deleted from Supabase.")
            ok_days += 1
            deleted += len(day_rows)
        else:
            print(f"  ⚠️ [{table} {day}] archived to R2 but delete call failed — "
                  f"rows remain, will retry next run (already safe in R2).")
            failed += 1
    return ok_days, deleted, failed


def archive_generic_tables():
    """Phase 2 driver. Returns 'skipped' | 'nothing' | 'ok' | 'failed'."""
    if not (ARCHIVE_SIGNALS_ENABLED or DRY_RUN):
        print("\n⏭  Phase 2 (signals/products/rollups) disabled "
              "(set ARCHIVE_SIGNALS_ENABLED=true to enable).")
        return "skipped"

    print(f"\n📦 Phase 2 — archiving append-only tables "
          f"(signals/products keep {SIGNAL_KEEP_DAYS}d, rollups keep {ROLLUP_KEEP_DAYS}d)"
          f"{' — DRY RUN' if DRY_RUN else ''}...")
    targets = discover_targets()
    print(f"  {len(targets)} table(s) in scope.")

    tot_days = tot_rows = tot_failed = 0
    for table, date_col, keep in targets:
        d, r, f = archive_one_table(table, date_col, keep)
        tot_days += d; tot_rows += r; tot_failed += f

    if DRY_RUN:
        print(f"\n🏁 Phase 2 pilot: {tot_days} day-file(s) verified in R2. "
              f"0 rows deleted (dry run).")
        return "ok"
    print(f"\n🏁 Phase 2: {tot_rows} rows across {tot_days} day-file(s) moved to R2 "
          f"and removed from Supabase. {tot_failed} day(s) failed (kept for retry).")
    return "failed" if tot_failed and tot_days == 0 else "ok"


if __name__ == "__main__":
    price_status = archive_price_snapshots()
    generic_status = archive_generic_tables()

    print(f"\n══ Archive summary — price_snapshots: {price_status}, "
          f"phase 2: {generic_status} ══")

    # os._exit avoids a pyarrow/Arrow C++ teardown abort (exit 134) that can fire
    # during normal interpreter shutdown AFTER all real work has succeeded. Exit
    # non-zero only on a genuine failure so CI stays honest.
    sys.stdout.flush()
    hard_fail = price_status == "failed" or generic_status == "failed"
    os._exit(1 if hard_fail else 0)
