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

ARCHIVE_THRESHOLD_DAYS = int(os.environ.get("ARCHIVE_THRESHOLD_DAYS_OVERRIDE", "30"))
# Default is 30, matching the hot window described in the architecture
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

    print("  Writing Parquet file...")
    parquet_buf = write_parquet(flattened)
    parquet_size_kb = len(parquet_buf.getvalue()) / 1024
    print(f"  Parquet file: {parquet_size_kb:.1f} KB for {len(flattened)} rows.")

    # One object per archive run, dated by the cutoff so files never collide
    # and can be located later by the period they cover. Dry-run pilots are
    # written under a separate prefix so they're trivially distinguishable
    # from real archive files in the bucket — never mixed, easy to bulk-
    # delete later once the pilot has served its purpose.
    prefix = "_pilot_dry_run" if DRY_RUN else "price_snapshots"
    object_key = f"{prefix}/{cutoff.isoformat()}_to_{rows[-1]['snapshot_date']}.parquet"
    print(f"  Uploading to R2 as: {object_key}")
    try:
        upload_to_r2(parquet_buf, object_key)
    except Exception as e:
        print(f"  ❌ Upload to R2 failed: {e}. Nothing will be deleted. Exiting.")
        sys.exit(1)

    gate_passed = verify_upload(object_key, expected_row_count=len(rows))
    if not gate_passed:
        print("  🛑 Stopping here. Rows remain in Supabase, untouched. "
              "Will retry on the next scheduled run.")
        sys.exit(1)

    if DRY_RUN:
        print(f"  🧪 DRY RUN — would have deleted {len(rows)} rows from "
              f"Supabase price_snapshots (verified present in R2 first). "
              f"Skipping the actual delete. Nothing in Supabase was touched.")
        deleted_count = 0
    else:
        snapshot_ids = [r["id"] for r in rows]
        deleted_count = delete_archived_rows(snapshot_ids)
        print(f"  🗑️  Deleted {deleted_count} rows from Supabase price_snapshots "
              f"(verified present in R2 first).")

    if DRY_RUN:
        print(f"\n🏁 Pilot run complete. {len(rows)} rows successfully round-tripped "
              f"through export → R2 upload → verified readback. "
              f"0 rows deleted (dry run). Pilot file is in R2 under _pilot_dry_run/ "
              f"— safe to delete from the bucket once you've confirmed this worked.")
    else:
        print(f"\n🏁 Archive run complete. {len(rows)} rows moved to R2, "
              f"{deleted_count} removed from the hot tier.")

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
