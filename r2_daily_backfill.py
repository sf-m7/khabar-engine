"""
Khabar — ONE-TIME R2 re-partition: batch files → one file per calendar day.
================================================================================
WHY THIS EXISTS
Until now archive.py wrote one Parquet file per RUN, named for the date range it
happened to cover (e.g. 2026-06-18_to_2026-06-21.parquet). That made the bucket's
shape a function of when the job ran rather than what the data is: a missed Monday
produced a 13-day file, a normal week a 7-day one. Any query for "the last 30 days"
had to open every file in the bucket to discover which ones were even relevant.

archive.py now writes price_snapshots/YYYY-MM-DD.parquet — one file per day, so the
FILENAME is the index and DuckDB can skip whole files by name without reading them.
This script brings the ALREADY-ARCHIVED history into that same layout, so the lake
has exactly one shape instead of two.

WHAT IT DOES
  1. Lists every object under price_snapshots/
  2. Skips anything already named YYYY-MM-DD.parquet (idempotent — safe to re-run)
  3. Reads each legacy batch file, splits its rows by snapshot_date
  4. Merges into the correct per-day file (de-duplicated on snapshot_id — never
     overwrites rows already there)
  5. Verifies every day-file reads back with the expected row count
  6. ONLY THEN deletes the legacy batch file

SAFETY
Nothing is deleted until its rows are confirmed readable in their new per-day home.
Run with DRY_RUN=true first — it does every step except the delete.
Delete this script once the bucket is clean; it has no ongoing job.
"""

import io
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
PREFIX  = "price_snapshots/"

r2 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
)


def is_already_daily(key):
    """price_snapshots/2026-07-06.parquet → True. Anything else → False."""
    name = key[len(PREFIX):].replace(".parquet", "")
    parts = name.split("-")
    return (
        len(parts) == 3
        and len(parts[0]) == 4
        and all(p.isdigit() for p in parts)
    )


def list_all_objects():
    keys, token = [], None
    while True:
        kwargs = {"Bucket": R2_BUCKET_NAME, "Prefix": PREFIX}
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


if __name__ == "__main__":
    mode = "DRY RUN (nothing written, nothing deleted)" if DRY_RUN else "PRODUCTION"
    print(f"🚀 R2 daily re-partition starting... mode={mode}")

    all_keys = list_all_objects()
    legacy   = [k for k in all_keys if not is_already_daily(k)]
    daily    = [k for k in all_keys if is_already_daily(k)]

    print(f"  Found {len(all_keys)} object(s) under {PREFIX}")
    print(f"    already day-partitioned: {len(daily)}")
    print(f"    legacy batch files:      {len(legacy)}")

    if not legacy:
        print("  Nothing to re-partition. Bucket is already clean. Exiting.")
        sys.exit(0)

    # ---- 1. Read every legacy file, bucket its rows by snapshot_date ----
    rows_by_day = defaultdict(dict)   # day -> {snapshot_id: row}
    source_of   = defaultdict(set)    # day -> which legacy files fed it
    total_read  = 0

    for key in sorted(legacy):
        try:
            rows = read_rows(key)
        except Exception as e:
            print(f"  ❌ Could not read {key}: {e}. Aborting — nothing deleted.")
            sys.exit(1)
        total_read += len(rows)
        for r in rows:
            day = str(r["snapshot_date"])
            rows_by_day[day][r["snapshot_id"]] = r
            source_of[day].add(key)
        print(f"  Read {key}: {len(rows)} rows.")

    print(f"  Total {total_read} row(s) across {len(rows_by_day)} calendar day(s): "
          f"{min(rows_by_day)} → {max(rows_by_day)}")

    # ---- 2. Merge into per-day files, verify each ----
    written_days, failed_days = [], []

    for day in sorted(rows_by_day):
        target = f"{PREFIX}{day}.parquet"
        merged = dict(rows_by_day[day])

        # Merge with an existing day-file rather than clobbering it.
        if exists(target):
            try:
                for r in read_rows(target):
                    merged.setdefault(r["snapshot_id"], r)
                print(f"  [{day}] merging into existing day-file.")
            except Exception as e:
                print(f"  ❌ [{day}] exists but unreadable: {e}. Skipping.")
                failed_days.append(day)
                continue

        out = list(merged.values())

        if DRY_RUN:
            print(f"  🧪 [{day}] would write {len(out)} row(s) → {target}")
            written_days.append(day)
            continue

        try:
            write_rows(target, out)
        except Exception as e:
            print(f"  ❌ [{day}] write failed: {e}. Skipping.")
            failed_days.append(day)
            continue

        # THE GATE — read it back, confirm the row count, before anything is deleted.
        try:
            back = read_rows(target)
        except Exception as e:
            print(f"  🛑 [{day}] wrote but could not read back: {e}. Skipping.")
            failed_days.append(day)
            continue

        if len(back) != len(out):
            print(f"  🛑 [{day}] row count mismatch: wrote {len(out)}, read {len(back)}. Skipping.")
            failed_days.append(day)
            continue

        print(f"  ✅ [{day}] {target} — {len(out)} rows verified.")
        written_days.append(day)

    # ---- 3. Delete legacy files ONLY if every day they fed was verified ----
    safe_to_delete = []
    for key in legacy:
        days_fed = [d for d, srcs in source_of.items() if key in srcs]
        if all(d in written_days for d in days_fed):
            safe_to_delete.append(key)
        else:
            unverified = [d for d in days_fed if d not in written_days]
            print(f"  ⚠️  Keeping {key} — day(s) not verified: {', '.join(unverified)}")

    if DRY_RUN:
        print(f"\n🏁 DRY RUN complete. Would write {len(written_days)} day-file(s) "
              f"and delete {len(safe_to_delete)} legacy batch file(s). "
              f"Nothing was changed.")
        sys.exit(0)

    deleted = 0
    for key in safe_to_delete:
        try:
            r2.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
            deleted += 1
            print(f"  🗑️  Deleted legacy file {key}")
        except Exception as e:
            print(f"  ⚠️  Could not delete {key}: {e} (harmless — data is safe, "
                  f"it's just a duplicate now).")

    print(f"\n🏁 Re-partition complete. {len(written_days)} day-file(s) written and "
          f"verified, {deleted} legacy batch file(s) removed.")
    if failed_days:
        print(f"  ⚠️  {len(failed_days)} day(s) failed: {', '.join(failed_days)}. "
              f"Their source files were kept. Re-run to retry.")

    sys.stdout.flush()
    os._exit(0)
