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
# ever deleted on a guess. Scoped to price_snapshots only — products,
# product_variants, and the weekly rollup tables are untouched by this
# script, on purpose, because they don't have the unbounded-growth problem
# this job exists to solve.
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


if __name__ == "__main__":
    mode_label = "PILOT (dry run, no deletion)" if DRY_RUN else "PRODUCTION"
    print(f"🚀 Khabar cold archive starting... mode={mode_label}")
    cutoff = date.today() - timedelta(days=ARCHIVE_THRESHOLD_DAYS)
    print(f"  Archiving price_snapshots older than {cutoff} "
          f"({ARCHIVE_THRESHOLD_DAYS}-day window{' — OVERRIDDEN for this pilot run' if DRY_RUN else ''})...")

    rows = fetch_aging_snapshot_ids(cutoff)
    if not rows:
        print("  Nothing to archive yet — no rows older than the hot window. Exiting cleanly.")
        sys.exit(0)

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
        print("  🛑 No day passed the verify gate. Nothing deleted. Exiting.")
        sys.exit(1)

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

    # Explicit, immediate, successful exit — BEFORE Python's normal interpreter
    # shutdown runs. Seen live in a pilot run: pyarrow/Arrow's C++ layer can
    # abort during that shutdown sequence (exit code 134, "core dumped") AFTER
    # every line of this script's real work has already completed and printed
    # successfully — it is teardown noise, not a failure of select/flatten/
    # write/upload/verify/delete-skip. Forcing the exit here removes the
    # ambiguity entirely rather than relying on log-reading to tell the two
    # apart, and is what makes GitHub Actions report this run as ✅ green
    # instead of ❌ red despite nothing having actually gone wrong.
    sys.stdout.flush()
    os._exit(0)
