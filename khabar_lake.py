"""
Khabar — the query layer over the two-tier price history.
================================================================================
THE PROBLEM THIS SOLVES

Khabar's price history now lives in two places:

  • HOT  — Supabase `price_snapshots`, the most recent ~8 days. Fast, indexed,
           what the scraper writes into every run.
  • COLD — Cloudflare R2, one Parquet file per calendar day, going back to the
           beginning. Cheap, permanent, but not a database.

Every analytical question worth selling spans BOTH. "Is this discount unusual
versus the last 30 days?" needs ~22 days from R2 and ~8 from Supabase. Until
now, answering that meant a human opening DuckDB and stitching it by hand,
which is why no L1/L2 product could be computed on a schedule.

This module makes the seam invisible. Call `snapshots(days=30)` and you get one
table, correctly de-duplicated, regardless of which tier each row came from.

--------------------------------------------------------------------------------
HOW IT WORKS

DuckDB is an in-process analytical database — no server, no hosting, it just
runs inside this Python process on the GitHub Actions runner and disappears
when the job ends. It can read Parquet straight out of R2 over S3-compatible
HTTP, and it can read Postgres directly. So it can join both tiers in one SQL
statement without anything being downloaded to disk first.

Two details that matter:

  1. DAY-PARTITION PRUNING. The lake is laid out as price_snapshots/YYYY-MM-DD
     .parquet precisely so DuckDB can decide which files to open FROM THE
     FILENAME ALONE. We pass an explicit list of day-file paths rather than a
     wildcard, so a 30-day query opens ~22 small files instead of scanning the
     entire history. This is what keeps queries fast as the lake grows to years.

  2. THE OVERLAP RULE. The hot and cold tiers can briefly hold the SAME day: a
     day archived to R2 is deleted from Supabase, but between the archive run
     and the delete there is a window where it exists in both. Worse, if an
     archive run half-fails, a day can sit in both tiers indefinitely.
     Silently double-counting a day would corrupt every average, median and
     IQR downstream — so rows are de-duplicated on snapshot_id, with the HOT
     tier winning any conflict (it is the more recently written copy).

--------------------------------------------------------------------------------
IMPORTANT — discount_pct IS ALWAYS NULL. NEVER READ IT.

`price_snapshots.discount_pct` was never populated by the scraper. It is NULL
in Supabase and NULL in every Parquet file in the lake. Reading it gives you
nothing, silently.

`honest_discount_pct` below derives discount at query time instead. It measures
against `compare_at_price` ONLY where the brand actually published one — and it
is deliberately NULL, not 0, where they didn't. Silence is not a zero discount;
conflating the two would understate discounting for exactly the brands that
publish no RRP (LC Waikiki and Mobaco publish none at all).

The stronger, manipulation-resistant baseline is `first_observed_price` on
product_variants — the price Khabar itself first witnessed, which no brand can
retroactively inflate. It is not carried in price_snapshots (which is
product-level; snapshots have no variant_id), so any signal that needs the
honest baseline must join product_variants explicitly. `variant_baselines()`
below is provided for that.
"""

import os
import sys
from datetime import date, timedelta

import duckdb

R2_ACCESS_KEY_ID     = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_ACCOUNT_ID        = os.environ["R2_ACCOUNT_ID"]
R2_BUCKET_NAME       = os.environ["R2_BUCKET_NAME"]
SUPABASE_DB_URL      = os.environ["SUPABASE_DB_URL"]

LAKE_PREFIX = "price_snapshots"


def connect():
    """
    Spin up an in-process DuckDB with both tiers attached.
      • httpfs  → lets DuckDB read Parquet directly from R2 over S3-compatible HTTP
      • postgres→ lets DuckDB read the live Supabase tables in the same query
    Nothing is persisted; the whole database evaporates when the process exits.
    """
    con = duckdb.connect()

    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL postgres; LOAD postgres;")

    # R2 speaks the S3 API. 'auto' region + the account-specific endpoint.
    con.execute(f"""
        CREATE OR REPLACE SECRET r2 (
            TYPE S3,
            KEY_ID     '{R2_ACCESS_KEY_ID}',
            SECRET     '{R2_SECRET_ACCESS_KEY}',
            ENDPOINT   '{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
            REGION     'auto',
            URL_STYLE  'path'
        );
    """)

    # Read-only: this layer computes signals, it must never mutate the source.
    con.execute(f"ATTACH '{SUPABASE_DB_URL}' AS pg (TYPE POSTGRES, READ_ONLY);")

    return con


# =============================================================================
# THE ONE-READ RULE — why this module materialises the hot tier.
#
# `snapshots()` used to build a DuckDB VIEW over `pg.public.price_snapshots`.
# A view is not data; it is stored SQL. Every query that touched `snapshots`
# re-ran that SQL against Supabase from scratch.
#
# The signal runner touches it 4–8 times per signal (the row count here, the
# days_available count, the signal's own SQL — which often references it in two
# or three CTEs — and the suppressed_sql). Across ~15 runnable signals that is
# 75–120 full reads of the hot window PER RUN.
#
# Worse, the de-duplication below uses ROW_NUMBER() OVER (PARTITION BY ...),
# which is an opaque barrier: DuckDB cannot push column-pruning or date filters
# down through it into Postgres. So each of those reads dragged all 16 columns
# of price_snapshots across the wire AND the entirety of products and
# product_variants (481K rows) to satisfy the joins.
#
# Measured effect: ~300–500 MB/day of Supabase egress from one daily job, and
# ~3.6 GB on a day it was run 13 times while debugging (2026-07-14).
#
# The fix is one line of principle: pull the hot tier ONCE into a local DuckDB
# table, then let every subsequent query read local memory. Postgres is touched
# exactly once per process.
#
# The COLD tier is deliberately NOT materialised. R2 egress is free and
# unmetered, so re-scanning Parquet costs nothing, while materialising 90 days
# of it could exhaust the runner's memory. Trading a free resource for a scarce
# one would be the wrong direction.
# =============================================================================

# Process-local. Reset per job because the DuckDB connection dies with it.
_HOT_READY        = False
_BASELINES_READY  = False
_PRODBASE_READY   = False
_EVENTS_READY     = False
_GLOB_CACHE       = None


def _lake_files(con):
    """
    Every day-file in the lake, listed ONCE per process.

    The glob is an R2 LIST call — cheap, but it was being re-issued for every
    signal. Cached here because the lake cannot gain files mid-run: archive.py
    is a separate weekly job.
    """
    global _GLOB_CACHE
    if _GLOB_CACHE is None:
        _GLOB_CACHE = con.execute(f"""
            SELECT file FROM glob('s3://{R2_BUCKET_NAME}/{LAKE_PREFIX}/*.parquet')
        """).fetchall()
    return _GLOB_CACHE


def materialise_hot(con, force=False):
    """
    Pull the ENTIRE Supabase hot tier into a local DuckDB table, once.

    No date filter, deliberately. The hot tier is already bounded by the
    retention window housekeeping enforces (~8 days), so "all of it" is small
    and predictable — a few hundred thousand rows. Filtering by window here
    would mean a second Postgres read the moment a signal asked for a wider
    lookback, which is the exact behaviour this function exists to remove.

    After this returns, `hot_raw` is local data. Nothing downstream touches
    Postgres again for the life of the process.
    """
    global _HOT_READY
    if _HOT_READY and not force:
        return con.execute("SELECT count(*) FROM hot_raw").fetchone()[0]

    con.execute("DROP TABLE IF EXISTS hot_raw")
    con.execute(f"""
        CREATE TABLE hot_raw AS
        SELECT
            ps.id                     AS snapshot_id,
            ps.product_id             AS product_id,
            ps.variant_id             AS variant_id,
            ps.brand                  AS brand,
            p.name                    AS product_name,
            p.category_normalized     AS category_normalized,
            p.category_raw            AS category_raw,
            p.gender                  AS gender,
            pv.size                   AS size,
            pv.color                  AS color,
            CAST(p.attributes_extracted AS VARCHAR) AS attributes_extracted,
            CAST(ps.price AS DOUBLE)             AS price,
            CAST(ps.compare_at_price AS DOUBLE)  AS compare_at_price,
            CAST(ps.discount_pct AS DOUBLE)      AS discount_pct,
            CAST(ps.snapshot_date AS VARCHAR)    AS snapshot_date,
            CAST(ps.recorded_at AS VARCHAR)      AS recorded_at
        FROM pg.public.price_snapshots ps
        LEFT JOIN pg.public.products         p  ON p.id  = ps.product_id
        LEFT JOIN pg.public.product_variants pv ON pv.id = ps.variant_id
    """)
    _HOT_READY = True
    return con.execute("SELECT count(*) FROM hot_raw").fetchone()[0]


def prefetch(con):
    """
    Call once, immediately after connect(), before computing anything.

    Optional — snapshots() will materialise on demand if this was skipped — but
    calling it explicitly makes the single Postgres read visible in the log
    rather than hidden inside the first signal's timing.
    """
    n = materialise_hot(con)
    print(f"  📥 Hot tier materialised once: {n:,} rows. "
          f"Supabase will not be read again this run.")
    return n


def _day_files(con, start_day, end_day):
    """
    The day-files that actually exist in the lake for [start_day, end_day].

    We filter by NAME rather than handing DuckDB a wildcard. Two reasons: a
    wildcard over a growing lake gets slower every month, and a missing day (a
    brand outage, a failed archive) would otherwise raise instead of simply
    being absent. Absent days are a fact about the data, not an error.
    """
    rows = _lake_files(con)

    wanted = []
    for (path,) in rows:
        stem = path.split("/")[-1].replace(".parquet", "")
        try:
            d = date.fromisoformat(stem)
        except ValueError:
            continue  # legacy batch file or stray object — not part of the lake
        if start_day <= d <= end_day:
            wanted.append(path)
    return sorted(wanted)


def snapshots(con, days=30, end_day=None):
    """
    One unified price_snapshots view spanning both tiers, as a DuckDB view
    named `snapshots`. Returns the number of rows in it.

    days    — how far back to look (inclusive of today)
    end_day — defaults to today; override for backtesting a past window
    """
    end_day   = end_day or date.today()
    start_day = end_day - timedelta(days=days - 1)

    # One Postgres read per process. No-op on every call after the first.
    materialise_hot(con)

    files = _day_files(con, start_day, end_day)

    # The cold half. If no day-files fall in the window (very short lookback,
    # or a brand-new lake), fall back to an empty-but-correctly-typed table so
    # downstream SQL doesn't need to special-case it.
    if files:
        file_list = ", ".join(f"'{f}'" for f in files)
        cold_sql = f"SELECT * FROM read_parquet([{file_list}])"
    else:
        cold_sql = """
            SELECT NULL::BIGINT  AS snapshot_id,
                   NULL::BIGINT  AS product_id,
                   NULL::BIGINT  AS variant_id,
                   NULL::VARCHAR AS brand,
                   NULL::VARCHAR AS product_name,
                   NULL::VARCHAR AS category_normalized,
                   NULL::VARCHAR AS category_raw,
                   NULL::VARCHAR AS gender,
                   NULL::VARCHAR AS size,
                   NULL::VARCHAR AS color,
                   NULL::VARCHAR AS attributes_extracted,
                   NULL::DOUBLE  AS price,
                   NULL::DOUBLE  AS compare_at_price,
                   NULL::DOUBLE  AS discount_pct,
                   NULL::VARCHAR AS snapshot_date,
                   NULL::VARCHAR AS recorded_at
            WHERE FALSE
        """

    # The hot half — now read from the LOCAL materialised copy, not Postgres.
    # The window filter is applied here rather than in the pull, so narrowing
    # the lookback is free and never costs another round trip to Supabase.
    hot_sql = f"""
        SELECT
            snapshot_id, product_id, variant_id, brand, product_name,
            category_normalized, category_raw, gender, size, color,
            attributes_extracted, price, compare_at_price, discount_pct,
            snapshot_date, recorded_at
        FROM hot_raw
        WHERE CAST(snapshot_date AS DATE) >= DATE '{start_day}'
          AND CAST(snapshot_date AS DATE) <= DATE '{end_day}'
    """

    # THE OVERLAP RULE (see module docstring): a day can exist in both tiers.
    # De-duplicate on snapshot_id, hot tier wins. Without this, any day caught
    # mid-archive is counted twice and every median/IQR downstream is wrong.
    # The honest baseline must be available before the view is defined. Guarded,
    # so this is one narrow two-column Postgres read per run, not per signal.
    product_baselines(con)

    con.execute(f"""
        CREATE OR REPLACE VIEW snapshots AS
        WITH cold AS ({cold_sql}),
             hot  AS ({hot_sql}),
             unioned AS (
                 SELECT *, 1 AS tier_rank FROM hot
                 UNION ALL
                 SELECT *, 2 AS tier_rank FROM cold
             ),
             deduped AS (
                 SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY snapshot_id
                            ORDER BY tier_rank
                        ) AS rn
                 FROM unioned
             )
        SELECT
            d.snapshot_id, d.product_id, d.variant_id, d.brand, d.product_name,
            d.category_normalized, d.category_raw, d.gender, d.size, d.color,
            d.attributes_extracted, d.price, d.compare_at_price, d.discount_pct,
            CAST(d.snapshot_date AS DATE) AS snapshot_date,
            d.recorded_at,

            -- ================================================================
            -- honest_discount_pct — measured against first_observed_price.
            --
            -- THIS DEFINITION CHANGED 2026-07-20. It previously computed from
            -- compare_at_price, which is the brand-manipulable "was" price. A
            -- product parked forever at 499 "was 799" is not on sale; it is
            -- priced at 499. Reading that as a 37% discount is precisely the
            -- error Khabar sells immunity to, and the column carried the name
            -- of the invariant it was breaking.
            --
            -- It was defensible when written: price_snapshots.discount_pct was
            -- NULL on all 455,947 rows, so compare_at_price was the only thing
            -- available. That column is now populated at write time by the
            -- scraper and backfilled across live rows.
            --
            -- COALESCE order matters. discount_pct is authoritative where it
            -- exists (hot rows, and cold days archived after the fix). Older
            -- Parquet files predate the column and hold NULL — those are
            -- recomputed here from product_baselines, which is why that table
            -- must be UNFILTERED by is_active/delisted_at.
            --
            -- NULL, never 0, when there is no baseline: "we cannot measure"
            -- and "there is no discount" must stay distinguishable.
            -- ================================================================
            COALESCE(
                d.discount_pct,
                CASE
                    WHEN pb.baseline_price IS NOT NULL
                     AND pb.baseline_price > 0
                     AND d.price < pb.baseline_price
                    THEN ROUND(100.0 * (pb.baseline_price - d.price) / pb.baseline_price, 2)
                    ELSE NULL
                END
            ) AS honest_discount_pct,

            pb.baseline_price AS baseline_price,

            -- The discount the brand CLAIMS, from its own RRP. Deliberately
            -- kept and deliberately renamed: it is not a measure of savings,
            -- it is evidence of anchoring behaviour, and it is the raw input
            -- to the Anchor Inflation signal. Never substitute it for the
            -- honest figure above. NULL — never 0 — when no RRP was published.
            CASE
                WHEN d.compare_at_price IS NOT NULL
                 AND d.compare_at_price > 0
                 AND d.compare_at_price >= d.price
                THEN ROUND(100.0 * (d.compare_at_price - d.price) / d.compare_at_price, 2)
                ELSE NULL
            END AS claimed_discount_pct
        FROM deduped d
        LEFT JOIN product_baselines pb ON pb.product_id = d.product_id
        WHERE d.rn = 1
    """)

    n = con.execute("SELECT count(*) FROM snapshots").fetchone()[0]
    return n, len(files), start_day, end_day


def product_baselines(con):
    """
    Product-level honest baseline: the lowest first_observed_price across a
    product's variants. Materialised once per run, two columns only.

    WHY THIS EXISTS SEPARATELY FROM variant_baselines():
    variant_baselines filters to `p.is_active AND pv.delisted_at IS NULL`,
    which is right for "what can I sell today" but WRONG for a historical
    baseline. A snapshot from three weeks ago must still be measurable even if
    the product has since been delisted -- otherwise the honest discount
    silently disappears from history the moment a product dies, and every
    trailing median shifts. This pull is deliberately unfiltered.

    COLLAPSE RULE: MIN, not AVG or MAX. first_observed_price is variant-level;
    snapshots are product-level. 96.5% of products carry one baseline across
    all variants; where they differ, MIN yields the SMALLEST possible discount.
    A signal sold on the honesty of its baseline must never round in its own
    favour. Same rule as L1-01 in the registry -- kept identical on purpose.
    """
    global _PRODBASE_READY
    if _PRODBASE_READY:
        return con.execute("SELECT count(*) FROM product_baselines").fetchone()[0]

    con.execute("DROP TABLE IF EXISTS product_baselines")
    con.execute("""
        CREATE TABLE product_baselines AS
        SELECT pv.product_id AS product_id,
               MIN(CAST(pv.first_observed_price AS DOUBLE)) AS baseline_price
        FROM pg.public.product_variants pv
        WHERE pv.first_observed_price IS NOT NULL
        GROUP BY pv.product_id
    """)
    _PRODBASE_READY = True
    return con.execute("SELECT count(*) FROM product_baselines").fetchone()[0]


def stockout_events(con, witnessed_only=True):
    """
    Inventory transitions, exposed as the table `stock_events`.

    WITNESSED-ONLY BY DEFAULT, AND THAT DEFAULT IS THE WHOLE POINT.
    Of 44,689 real stock transitions, only 27,670 (62%) are trustworthy. The
    rest are artefacts of how the data was collected, not of what the market
    did:
      orphan_restock       a restock with no sellout ever recorded before it,
                           so the interval it implies is meaningless. 69% of
                           all restock volume.
      delist_cycle         housekeeping wrote "out of stock" when a product
                           merely left the catalogue; its reappearance then
                           read as a restock. Fixed at source 2026-07-20, but
                           historical rows remain.
      duplicate_transition the same event type twice in a row.

    Counting those as demand overstates sellout velocity, and overstates it
    WORST for the newest brands. Filtering belongs here, once, rather than in
    each signal's SQL where one omission silently poisons a client-facing
    number.

    Set witnessed_only=False ONLY to audit the contamination itself. Never for
    a signal.

    Note this reads Postgres directly: stockout_events is not archived to R2,
    so there is no cold tier to stitch. If retention is ever added to that
    table, this needs the same two-tier treatment as snapshots().
    """
    global _EVENTS_READY
    if _EVENTS_READY:
        return con.execute("SELECT count(*) FROM stock_events").fetchone()[0]

    where = "AND se.witnessed = TRUE" if witnessed_only else ""
    con.execute("DROP TABLE IF EXISTS stock_events")
    con.execute(f"""
        CREATE TABLE stock_events AS
        SELECT se.id, se.variant_id, se.product_id, se.brand,
               se.size, se.color, se.event_type,
               CAST(se.price_at_event AS DOUBLE) AS price_at_event,
               se.was_on_discount,
               se.witnessed, se.seed_reason,
               se.recorded_at,
               CAST(se.recorded_at AS DATE) AS event_date,
               p.category_normalized, p.department, p.gender, p.product_name
        FROM pg.public.stockout_events se
        JOIN pg.public.products p ON p.id = se.product_id
        WHERE se.event_type IN ('stockout','restock') {where}
    """)
    _EVENTS_READY = True
    return con.execute("SELECT count(*) FROM stock_events").fetchone()[0]


def variant_baselines(con):
    """
    `first_observed_price` — the price Khabar itself first witnessed for a
    variant. This is the manipulation-resistant baseline: unlike
    compare_at_price, a brand cannot retroactively inflate it to manufacture a
    discount. Exposed as a view `variant_baselines`.

    Note it is VARIANT-level while price_snapshots is PRODUCT-level, so joining
    the two is one-to-many. 96.5% of products have a single baseline across all
    their variants; the rest genuinely differ. Any signal that collapses them
    must state its rule explicitly rather than picking one silently.
    """
    # A TABLE, not a view — for the same reason as hot_raw. product_variants is
    # ~481K rows; any signal referencing this view more than once would have
    # pulled all of them from Postgres each time.
    global _BASELINES_READY
    if _BASELINES_READY:
        return con.execute("SELECT count(*) FROM variant_baselines").fetchone()[0]

    con.execute("DROP TABLE IF EXISTS variant_baselines")
    con.execute("""
        CREATE TABLE variant_baselines AS
        SELECT
            pv.id                   AS variant_id,
            pv.product_id           AS product_id,
            pv.external_sku         AS external_sku,
            pv.size                 AS size,
            pv.color                AS color,
            CAST(pv.first_observed_price AS DOUBLE) AS first_observed_price,
            pv.is_in_stock          AS is_in_stock,
            pv.delisted_at          AS delisted_at
        FROM pg.public.product_variants pv
        JOIN pg.public.products p ON p.id = pv.product_id
        WHERE p.is_active = TRUE
          AND pv.delisted_at IS NULL
    """)
    _BASELINES_READY = True
    return con.execute("SELECT count(*) FROM variant_baselines").fetchone()[0]


# ---------------------------------------------------------------------------
# Smoke test — run this file directly to prove the seam is sound.
# It checks the things that would silently corrupt every downstream signal:
# that both tiers are actually reachable, that no day is double-counted, and
# that the day coverage has no unexplained holes.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("🚀 Khabar lake — connectivity + seam integrity check\n")

    con = connect()
    print("  ✅ DuckDB up; R2 + Supabase both attached.\n")

    n, n_files, start_day, end_day = snapshots(con, days=30)
    print(f"  Window: {start_day} → {end_day} (30 days)")
    print(f"  Cold day-files opened: {n_files}")
    print(f"  Unified row count:     {n:,}\n")

    # Where is each day coming from, and is any day in both tiers?
    per_day = con.execute("""
        SELECT snapshot_date, count(*) AS rows, count(DISTINCT brand) AS brands
        FROM snapshots
        GROUP BY snapshot_date
        ORDER BY snapshot_date
    """).fetchall()

    print("  Day coverage:")
    for d, rows, brands in per_day:
        print(f"    {d}  {rows:>7,} rows  {brands:>2} brands")

    # THE CRITICAL CHECK. If a snapshot_id appears twice, the de-dup failed and
    # every average/median/IQR computed on top of this is silently wrong.
    dupes = con.execute("""
        SELECT count(*) FROM (
            SELECT snapshot_id FROM snapshots
            GROUP BY snapshot_id HAVING count(*) > 1
        )
    """).fetchone()[0]

    print(f"\n  Duplicate snapshot_ids across the tier seam: {dupes}")
    if dupes:
        print("  🛑 FAIL — the same row is being counted twice. Do not trust "
              "any signal built on this until fixed.")
        sys.exit(1)
    print("  ✅ No double-counting across the seam.")

    # Gaps are worth SEEING, not failing on: an absent day may be a real
    # outage, not a bug. But a silent gap under a 30-day IQR is a distortion.
    days_present = {d for d, _, _ in per_day}
    expected = {start_day + timedelta(days=i)
                for i in range((end_day - start_day).days + 1)}
    missing = sorted(expected - days_present)
    if missing:
        print(f"\n  ⚠️  {len(missing)} day(s) with NO data in the window: "
              f"{', '.join(str(d) for d in missing)}")
        print("     Not necessarily a bug — but any rolling-window signal will "
              "be computed over fewer days than its name implies.")
    else:
        print("  ✅ Every day in the window has data.")

    nb = variant_baselines(con)
    print(f"\n  Active variant baselines (first_observed_price): {nb:,}")

    # discount_pct is dead. Prove it, so nobody is tempted to use it later.
    dead = con.execute("""
        SELECT count(*) FROM snapshots WHERE discount_pct IS NOT NULL
    """).fetchone()[0]
    honest = con.execute("""
        SELECT count(*) FROM snapshots WHERE honest_discount_pct IS NOT NULL
    """).fetchone()[0]
    print(f"  Rows with stored discount_pct:     {dead:,}  (expected 0 — the "
          f"column was never written)")
    print(f"  Rows with honest_discount_pct:     {honest:,}  (derived at query "
          f"time from compare_at_price)")

    print("\n🏁 Lake is queryable across both tiers. Seam is sound.")
