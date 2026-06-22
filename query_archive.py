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

# The actual question to ask, supplied at run time (e.g. typed into the
# GitHub Actions "Run workflow" box) rather than fixed inside this file.
# When empty, falls back to the same two example queries this script
# always ran — so it still does something useful with zero input, but is
# no longer LIMITED to those two questions.
CUSTOM_QUERY = os.environ.get("ARCHIVE_QUERY_SQL", "").strip()


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
    """
    Runs SQL where the table name `archive` means 'every Parquet file
    currently in the folder, combined.' Returns (column_names, rows) —
    deliberately using fetchall() + description rather than fetchdf().
    fetchdf() pulls in pandas/numpy as a dependency for a conversion this
    script doesn't actually need; fetchall() returns plain Python tuples
    directly from DuckDB with nothing extra required, which removes that
    whole category of "missing module" failure rather than chasing each
    transitive dependency one at a time.
    """
    full_sql = f"""
        WITH archive AS (
            SELECT * FROM read_parquet('{archive_glob()}')
        )
        {sql}
    """
    result = con.execute(full_sql)
    columns = [d[0] for d in result.description]
    rows = result.fetchall()
    return columns, rows


def print_table(columns, rows):
    """Plain-text table printer — no pandas needed just to display results
    readably in a GitHub Actions log."""
    if not rows:
        print("   (no rows returned)")
        return
    widths = [max(len(str(c)), max((len(str(r[i])) for r in rows), default=0)) for i, c in enumerate(columns)]
    header = "  ".join(str(c).ljust(w) for c, w in zip(columns, widths))
    print("   " + header)
    print("   " + "-" * len(header))
    for r in rows:
        print("   " + "  ".join(str(v).ljust(w) for v, w in zip(r, widths)))


if __name__ == "__main__":
    print(f"🔍 Khabar archive query — reading {archive_glob()}")
    con = connect()

    # A first sanity query: how many rows are visible right now, and what
    # date range do they span. This works whether there's 1 file or 100.
    try:
        cols, rows = run_query(con, """
            SELECT
                count(*)            AS total_rows,
                count(DISTINCT brand) AS distinct_brands,
                min(snapshot_date)  AS earliest_date,
                max(snapshot_date)  AS latest_date
            FROM archive
        """)
        print("\n📦 Archive summary:")
        print_table(cols, rows)
    except Exception as e:
        print(f"\n⚠️ Could not read the archive folder: {e}")
        print("   If this is the very first run and no files have landed yet,")
        print("   this is expected — there's nothing to summarize until the")
        print("   first real archive run (or a pilot test) writes a file.")
        sys.exit(0)

    if CUSTOM_QUERY:
        # A real question, typed in at run time (e.g. into the GitHub Actions
        # "Run workflow" box), not fixed in this file. Write it as you would
        # any SQL SELECT — the table is always called `archive` and always
        # means "every Parquet file currently in the folder, combined."
        print(f"\n❓ Running your question:\n   {CUSTOM_QUERY}")
        try:
            cols, rows = run_query(con, CUSTOM_QUERY)
            print("\n📊 Result:")
            print_table(cols, rows)
        except Exception as e:
            print(f"\n⚠️ Your query failed: {e}")
            print("   Check the SQL — remember the table is always called `archive`,")
            print("   e.g.: SELECT brand, avg(price) FROM archive GROUP BY brand")
            sys.exit(1)
    else:
        # No question supplied — fall back to one more illustrative example
        # so the script still demonstrates something useful on its own.
        try:
            cols, rows = run_query(con, """
                SELECT brand, count(*) AS rows, round(avg(price), 2) AS avg_price
                FROM archive
                GROUP BY brand
                ORDER BY rows DESC
            """)
            print("\n📊 No question supplied — showing rows per brand as a default:")
            print_table(cols, rows)
            print("\n   To ask your own question instead, supply it via the "
                  "ARCHIVE_QUERY_SQL input next time you run this.")
        except Exception as e:
            print(f"\n⚠️ Default breakdown query failed: {e}")

    print("\n✅ Done. The `archive` table always means 'every file currently "
          "in the folder, combined' — ask it anything a SELECT can answer.")
