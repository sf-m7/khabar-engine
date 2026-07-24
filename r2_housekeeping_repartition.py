"""
Khabar — ONE-TIME R2 re-partition for the housekeeping.py archive tables.
================================================================================
WHY THIS EXISTS
Same story as r2_daily_backfill.py (which did this for price_snapshots), applied
to the five tables housekeeping.py archives: stockout_events, price_events,
weekly_product_summary, weekly_variant_exception, weekly_bestseller_summary.

Until now, housekeeping.py's archive_table() wrote ONE file per RUN, named for
whatever date range happened to be eligible that run (e.g.
weekly_product_summary/2026-06-01_to_2026-06-29.parquet). housekeeping.py has
been rewritten to write one file per calendar day (stockout_events,
price_events) or per ISO week (the three weekly_* tables) instead — see the
rewritten archive_table() for the ongoing behaviour. This script brings the
ALREADY-ARCHIVED legacy files into that same layout.

WHAT IT DOES, per table:
  1. Lists every object under {table}/
  2. Skips anything already named YYYY-MM-DD.parquet (idempotent — safe to re-run)
  3. Reads each legacy batch file, splits its rows by day (recorded_at) or by
     week (week_start), depending on the table
  4. Merges into the correct per-day/per-week file. The legacy files never
     carried a stable row id (archive_table() used to pop it before writing),
     so there is no id to de-dupe on — instead this dedupes on the FULL row
     content (every column matches = same event). That's safe here because
     the two existing stockout_events files have overlapping boundary dates
     (...to_2026-06-23 and 2026-06-23_to_2026-06-28 both touch June 23), and
     content-based dedup collapses any row that legitimately appears in both.
  5. Verifies every new file reads back with the expected row count
  6. ONLY THEN deletes the legacy batch file that fed it

SAFETY
Nothing is deleted until its replacement is confirmed readable with the right
row count. Run with DRY_RUN=true first (the default) — it does every step
except the actual write and delete. Each table is fully independent: one
table's failure never blocks or half-finishes another.

Delete this script (and its workflow) once every table it prints is clean.
"""

import io
import json
import os
import sys
from collections import defaultdict

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_ACCOUNT_ID        = os.environ["R2_ACCOUNT_ID"]
R2_BUCKET_NAME       = os.environ["R2_BUCKET_NAME"]

DRY_RUN = os.environ.get("BACKFILL_DRY_RUN", "true").lower() == "true"

# table -> the column whose value determines which file a row belongs in.
# stockout_events / price_events are timestamptz (recorded_at) — the first
# 10 characters of the ISO string are the calendar day. The three weekly_*
# tables are already one row per entity per week (week_start is a plain
# date) — partitioning by that same value gives one file per ISO week,
# which is this table's natural grain (the "day" equivalent).
TABLES = {
    "stockout_events":          "recorded_at",
    "price_events":             "recorded_at",
    "weekly_product_summary":   "week_start",
    "weekly_variant_exception": "week_start",
    "weekly_bestseller_summary": "week_start",
}

r2 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
)


def is_already_partitioned(key, prefix):
    """{table}/2026-07-06.parquet -> True. Anything with '_to_' -> False."""
    name = key[len(prefix):].replace(".parquet", "")
    parts = name.split("-")
    return (
        len(parts) == 3
        and len(parts[0]) == 4
        and all(p.isdigit() for p in parts)
    )


def list_all_objects(prefix):
    keys, token = [], None
    while True:
        kwargs = {"Bucket": R2_BUCKET_NAME, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = r2.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                keys.append(obj["Key"])
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return keys


def read_rows(key):
    body = r2.get_object(Bucket=R2_BUCKET_NAME, Key=key)["Body"].read()
    return pq.read_table(io.BytesIO(body)).to_pylist()


def write_rows(key, rows):
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buf, compression="zstd")
    buf.seek(0)
    r2.upload_fileobj(buf, R2_BUCKET_NAME, key)


def exists(key):
    try:
        r2.head_object(Bucket=R2_BUCKET_NAME, Key=key)
        return True
    except Exception:
        return False


def content_key(row):
    """Legacy rows carry no stable id (archive_table() used to discard it),
    so identity is the full row content — every column matching means it's
    the same event/summary row, safe as a de-dup key here since these are
    closed historical periods that will never be written to again."""
    return json.dumps(row, sort_keys=True, default=str)


def repartition_table(table, partition_field):
    prefix = f"{table}/"
    print(f"\n📦 [{table}] partitioning by '{partition_field}' ...")

    all_keys = list_all_objects(prefix)
    legacy    = [k for k in all_keys if not is_already_partitioned(k, prefix)]
    already   = [k for k in all_keys if is_already_partitioned(k, prefix)]
    print(f"  Found {len(all_keys)} object(s): {len(already)} already-partitioned, "
          f"{len(legacy)} legacy batch file(s).")

    if not legacy:
        print(f"  Nothing to re-partition for {table}. Already clean.")
        return

    # ---- 1. Read every legacy file, bucket its rows by day/week ----
    rows_by_partition = defaultdict(dict)   # partition -> {content_key: row}
    source_of         = defaultdict(set)    # partition -> which legacy files fed it
    total_read        = 0

    for key in sorted(legacy):
        try:
            rows = read_rows(key)
        except Exception as e:
            print(f"  ❌ Could not read {key}: {e}. Skipping this table — nothing deleted.")
            return
        total_read += len(rows)
        for r in rows:
            part = str(r[partition_field])[:10]
            rows_by_partition[part][content_key(r)] = r
            source_of[part].add(key)
        print(f"  Read {key}: {len(rows)} row(s).")

    print(f"  Total {total_read} row(s) across {len(rows_by_partition)} partition(s): "
          f"{min(rows_by_partition)} → {max(rows_by_partition)}")

    # ---- 2. Merge into per-partition files, verify each ----
    written, failed = [], []

    for part in sorted(rows_by_partition):
        target = f"{prefix}{part}.parquet"
        merged = dict(rows_by_partition[part])

        if exists(target):
            try:
                for r in read_rows(target):
                    merged.setdefault(content_key(r), r)
                print(f"  [{part}] merging into existing file.")
            except Exception as e:
                print(f"  ❌ [{part}] exists but unreadable: {e}. Skipping.")
                failed.append(part)
                continue

        out = list(merged.values())

        if DRY_RUN:
            print(f"  🧪 [{part}] would write {len(out)} row(s) → {target}")
            written.append(part)
            continue

        try:
            write_rows(target, out)
        except Exception as e:
            print(f"  ❌ [{part}] write failed: {e}. Skipping.")
            failed.append(part)
            continue

        try:
            back = read_rows(target)
        except Exception as e:
            print(f"  🛑 [{part}] wrote but could not read back: {e}. Skipping.")
            failed.append(part)
            continue

        if len(back) != len(out):
            print(f"  🛑 [{part}] row count mismatch: wrote {len(out)}, read {len(back)}. Skipping.")
            failed.append(part)
            continue

        print(f"  ✅ [{part}] {target} — {len(out)} row(s) verified.")
        written.append(part)

    # ---- 3. Delete legacy files ONLY if every partition they fed was verified ----
    safe_to_delete = []
    for key in legacy:
        fed = [p for p, srcs in source_of.items() if key in srcs]
        if all(p in written for p in fed):
            safe_to_delete.append(key)
        else:
            unverified = [p for p in fed if p not in written]
            print(f"  ⚠️  Keeping {key} — partition(s) not verified: {', '.join(unverified)}")

    if DRY_RUN:
        print(f"  🏁 [{table}] DRY RUN — would write {len(written)} file(s), "
              f"delete {len(safe_to_delete)} legacy file(s).")
        return

    deleted = 0
    for key in safe_to_delete:
        try:
            r2.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
            deleted += 1
            print(f"  🗑️  Deleted legacy file {key}")
        except Exception as e:
            print(f"  ⚠️  Could not delete {key}: {e} (harmless — data is safe, just a duplicate now).")

    print(f"  🏁 [{table}] complete. {len(written)} file(s) written and verified, "
          f"{deleted} legacy file(s) removed.")
    if failed:
        print(f"  ⚠️  {len(failed)} partition(s) failed: {', '.join(failed)}. Re-run to retry.")


if __name__ == "__main__":
    mode = "DRY RUN (nothing written, nothing deleted)" if DRY_RUN else "PRODUCTION"
    print(f"🚀 Housekeeping-tables R2 re-partition starting... mode={mode}")

    for table, partition_field in TABLES.items():
        repartition_table(table, partition_field)

    print("\n🏁 All tables processed.")
    sys.stdout.flush()
    os._exit(0)
