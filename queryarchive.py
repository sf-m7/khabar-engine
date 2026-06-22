# ═══════════════════════════════════════════════════════
# KHABAR — Archive Query Tool (DuckDB over R2)
# ═══════════════════════════════════════════════════════
# This is the "Supabase SQL box, but for the R2 archive" piece. R2 itself
# is plain file storage — it has no query interface of its own. This script
# is what turns "a folder full of Parquet files" into "something you can
# ask SQL questions of," using DuckDB as the engine.
#
# HOW IT STAYS ZERO-MAINTENANCE AS NEW FILES LAND:
# Every archive run (archive.py, the Monday job) writes one new .parquet
# file into price_snapshots/ in the R2 bucket, named by the date range it
# covers. This script never lists or hardcodes filenames — it points DuckDB
# at the WHOLE FOLDER using a wildcard (price_snapshots/*.parquet) and reads
# every matching file as if it were one combined table. The first real
# archive file that lands in July needs zero changes here: next time this
# script runs, it simply sees one more file in that folder than it did
# before. The same is true for the 2nd, 10th, or 100th file.
#
# WHAT THIS DOES NOT DO:
# This does not touch Supabase and does not modify anything in R2 — it only
# READS. It is safe to run as often as you like; there is no delete/write
# path in this file at all.
# ═══════════════════════════════════════════════════════

import os
import sys

import duckdb

R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_ACCOUNT_ID        = os.environ["R2_ACCOUNT_ID"]
R2_BUCKET_NAME       = os.environ["R2_BUCKET_NAME"]

# Which subfolder in the bucket to read. Real archive runs write here.
# (The one-off pilot test file lives under _pilot_dry_run/ instead — pass
# that as ARCHIVE_QUERY_PREFIX if you ever want to query the test file
# specifically; it defaults to the real folder real data will use.)
PREFIX = os.environ.get("ARCHIVE_QUERY_PREFIX", "price_snapshots")


def connect():
    """
    Configures DuckDB to talk to R2 (R2 speaks the same protocol as Amazon
    S3, so DuckDB's S3 support works against it directly — just pointed at
    Cloudflare's endpoint instead of Amazon's).
    """
    con = duckdb.connect()
    con.execute("INSTALL httpfs;")
    con.execute("LOAD httpfs;")
    con.execute(f"""
        CREATE SECRET r2_secret (
            TYPE r2,
            KEY_ID '{R2_ACCESS_KEY_ID}',
            SECRET '{R2_SECRET_ACCESS_KEY}',
            ACCOUNT_ID '{R2_ACCOUNT_ID}'
        );
    """)
    return con


def archive_glob():
    """The wildcard path covering every Parquet file in the target folder,
    present or future — this is the entire reason new files need no code
    change here."""
    return f"r2://{R2_BUCKET_NAME}/{PREFIX}/*.parquet"


def run_query(con, sql):
    """Runs SQL where the table name `archive` means 'every Parquet file
    currently in the folder, combined.' Returns rows as a list of dicts."""
    full_sql = f"""
        WITH archive AS (
            SELECT * FROM read_parquet('{archive_glob()}')
        )
        {sql}
    """
    return con.execute(full_sql).fetchdf()


if __name__ == "__main__":
    print(f"🔍 Khabar archive query — reading {archive_glob()}")
    con = connect()

    # A first sanity query: how many rows are visible right now, and what
    # date range do they span. This works whether there's 1 file or 100.
    try:
        summary = run_query(con, """
            SELECT
                count(*)            AS total_rows,
                count(DISTINCT brand) AS distinct_brands,
                min(snapshot_date)  AS earliest_date,
                max(snapshot_date)  AS latest_date
            FROM archive
        """)
        print("\n📦 Archive summary:")
        print(summary.to_string(index=False))
    except Exception as e:
        print(f"\n⚠️ Could not read the archive folder: {e}")
        print("   If this is the very first run and no files have landed yet,")
        print("   this is expected — there's nothing to summarize until the")
        print("   first real archive run (or a pilot test) writes a file.")
        sys.exit(0)

    # A second example query, demonstrating this is real SQL, not just a
    # fixed report — change this freely for whatever question comes up.
    try:
        by_brand = run_query(con, """
            SELECT brand, count(*) AS rows, round(avg(price), 2) AS avg_price
            FROM archive
            GROUP BY brand
            ORDER BY rows DESC
        """)
        print("\n📊 Rows per brand in the archive:")
        print(by_brand.to_string(index=False))
    except Exception as e:
        print(f"\n⚠️ Breakdown query failed: {e}")

    print("\n✅ Done. Edit the SQL in this script's run_query() calls to ask "
          "anything else — the `archive` table always means 'every file "
          "currently in the folder, combined.'")
