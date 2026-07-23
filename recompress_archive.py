"""
Khabar — R2 archive re-compression (zstd -> snappy).
================================================================================
WHY

The cold archive is now readable directly from Postgres through Supabase's S3
foreign data wrapper, which turns each R2 day-file into an ordinary table. That
removes the need to run DuckDB locally, or to stand up a web service, just to
ask a question that spans more than the hot tier's retention window.

Everything in that path works except the final step. Verified against live R2
on 2026-07-23:

  * credentials and endpoint    OK  (a bad endpoint fails differently)
  * object paths                OK  (archived days fail at the PARQUET layer;
                                     unarchived days fail at the REQUEST layer,
                                     which is exactly the split you would
                                     expect if the files are found)
  * reading the parquet         FAIL "read parquet failed: External: service error"

The remaining difference between what we write and what the reader expects is
compression. archive.py and housekeeping.py both call
pq.write_table(..., compression="zstd"). zstd is excellent for cold storage —
noticeably smaller than snappy — but Arrow-based readers only decode it when
built with that codec enabled, and the wrapper's build appears not to be.
Snappy is the format's default and is universally supported.

IMPORTANT: this is a strong inference, not a proven fact. The wrapper reports
every S3-layer problem as the same opaque "service error", so compression could
not be isolated from the outside. Run with --probe first: it converts a SINGLE
day to a separate test key, changes nothing else, and lets the theory be tested
against the real reader before touching the archive.

--------------------------------------------------------------------------------
USAGE

  python recompress_archive.py --probe              one day -> _fdw_probe/
  python recompress_archive.py --probe --day D      pick the day yourself
  python recompress_archive.py --apply              convert the whole archive
  python recompress_archive.py --apply --keep-zstd  write .snappy alongside

SAFETY

  * --probe never writes into price_snapshots/ at all.
  * --apply verifies the row count of every rewritten file BEFORE replacing
    the original, and skips the file if the count differs.
  * Nothing is deleted. --apply overwrites a key only after its replacement
    has been read back and counted, which preserves the project's
    archive-before-delete invariant.
"""

import argparse
import io
import os
import sys

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.config import Config

R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_ACCOUNT_ID        = os.environ["R2_ACCOUNT_ID"]
R2_BUCKET_NAME       = os.environ["R2_BUCKET_NAME"]

PREFIX       = "price_snapshots"
PROBE_PREFIX = "_fdw_probe"

r2 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4", retries={"max_attempts": 5}),
)


def list_archive_keys(prefix):
    """Every .parquet key under a prefix, oldest first. Paginated: R2 caps a
    single list response at 1000 keys and the archive will pass that."""
    keys, token = [], None
    while True:
        kwargs = {"Bucket": R2_BUCKET_NAME, "Prefix": prefix + "/"}
        if token:
            kwargs["ContinuationToken"] = token
        resp = r2.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                keys.append((obj["Key"], obj["Size"]))
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return sorted(keys)


def read_table(key):
    body = r2.get_object(Bucket=R2_BUCKET_NAME, Key=key)["Body"].read()
    return pq.read_table(io.BytesIO(body))


def write_snappy(table, key):
    buf = io.BytesIO()
    # compression="snappy" is the Parquet default and the one codec every
    # Arrow build ships with. Explicit rather than implied so a future reader
    # of this file knows it was a decision, not an omission.
    pq.write_table(table, buf, compression="snappy")
    buf.seek(0)
    size = buf.getbuffer().nbytes
    r2.upload_fileobj(buf, R2_BUCKET_NAME, key)
    return size


def verify(key, expected_rows):
    """Read the object back and count it. A rewrite that cannot be re-read is
    worse than no rewrite at all, so this runs before anything is replaced."""
    try:
        return read_table(key).num_rows == expected_rows
    except Exception as e:
        print(f"    verify failed: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="convert one day to a scratch prefix and stop")
    ap.add_argument("--apply", action="store_true",
                    help="convert the whole archive in place")
    ap.add_argument("--day", help="specific day for --probe, e.g. 2026-07-08")
    ap.add_argument("--keep-zstd", action="store_true",
                    help="write alongside the original instead of replacing it")
    args = ap.parse_args()

    if not (args.probe or args.apply):
        sys.exit("Pick one: --probe (safe, one file) or --apply (whole archive).")

    keys = list_archive_keys(PREFIX)
    if not keys:
        sys.exit(f"No parquet files under {PREFIX}/ — nothing to do.")
    print(f"Archive holds {len(keys)} day-files "
          f"({sum(s for _, s in keys)/1e6:.1f} MB total).\n")

    if args.probe:
        if args.day:
            src = f"{PREFIX}/{args.day}.parquet"
            if src not in [k for k, _ in keys]:
                sys.exit(f"{src} not found in the archive.")
        else:
            src = keys[0][0]
        dst = f"{PROBE_PREFIX}/{os.path.basename(src)}"

        print(f"Probe: {src}  ->  {dst}")
        table = read_table(src)
        print(f"  read OK — {table.num_rows:,} rows, {table.num_columns} columns")
        print(f"  schema: {[f.name for f in table.schema]}")
        size = write_snappy(table, dst)
        print(f"  written as snappy — {size/1e6:.2f} MB")
        if verify(dst, table.num_rows):
            print("\n✅ Probe complete. Point a foreign table at:")
            print(f"   s3://{R2_BUCKET_NAME}/{dst}")
            print("   If that reads, compression was the blocker — run --apply.")
        else:
            print("\n❌ Probe wrote a file it could not read back. Stop here.")
        return

    converted = skipped = failed = 0
    saved_before = saved_after = 0

    for key, size in keys:
        print(f"{key} ({size/1e6:.2f} MB)")
        try:
            table = read_table(key)
        except Exception as e:
            print(f"    unreadable, skipping: {e}")
            failed += 1
            continue

        dst = key.replace(".parquet", ".snappy.parquet") if args.keep_zstd else key
        tmp = key.replace(".parquet", ".rewriting.parquet")

        try:
            # Write to a temporary key first, verify it, and only then move it
            # into place. Overwriting directly would leave a corrupt file at a
            # real archive path if the process died mid-upload.
            new_size = write_snappy(table, tmp)
            if not verify(tmp, table.num_rows):
                print("    verification failed — original left untouched")
                r2.delete_object(Bucket=R2_BUCKET_NAME, Key=tmp)
                failed += 1
                continue

            r2.copy_object(
                Bucket=R2_BUCKET_NAME,
                CopySource={"Bucket": R2_BUCKET_NAME, "Key": tmp},
                Key=dst,
            )
            r2.delete_object(Bucket=R2_BUCKET_NAME, Key=tmp)

            saved_before += size
            saved_after  += new_size
            converted += 1
            print(f"    ✅ {table.num_rows:,} rows -> {new_size/1e6:.2f} MB")
        except Exception as e:
            print(f"    ❌ {e}")
            failed += 1

    print(f"\nConverted {converted}, skipped {skipped}, failed {failed}.")
    if converted:
        delta = (saved_after - saved_before) / 1e6
        print(f"Archive size change: {delta:+.1f} MB "
              f"(snappy is larger than zstd; that is the cost of readability).")


if __name__ == "__main__":
    main()
