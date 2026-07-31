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
EVENT_WINDOW_DAYS = 90   # how much event history to stitch (hot + R2 cold).
                         # Caps the local event tables so they cannot grow
                         # unbounded as the archive deepens; must stay >= the
                         # largest window_days of any event-based signal (90).

# ── DATA QUARANTINE ──────────────────────────────────────────────────────────
# LC Waikiki price data before the fix is not trustworthy and is excluded from
# every signal, centrally, here — the same way the witnessed filter lives in one
# place so no signal has to remember it.
#
# Around mid-July 2026 LCW moved the live sale price into a nested campaign badge
# (CampaignBadges[].DiscountedPrice). Until scraper.py v14.50 (2026-07-26) the
# parser read only the flat list price (PriceValue), so every LCW item was
# recorded at its FULL price. Confirmed live: LCW snapshots were 99.4% frozen at
# list price every day through 2026-07-25, with ~zero price events. The 07-26 fix
# run then emitted ~6,100 one-off "down" events as ~6k products corrected from
# list to real sale price in a single day — an artifact, not a market move, that
# would otherwise read as a mass 50%-off event and wreck Discount-Honesty,
# Genuine Price Drop, First-Mover and Pricing Discipline for LCW.
#
# Boundary: LCW price data is trustworthy from 2026-07-27 (first clean scheduled
# run). Snapshots on/before 07-25 and price_events on/before 07-26 are excluded.
# first_observed_price is NOT touched — a list-price anchor is the correct honest
# baseline, so LCW's real discounts still measure correctly against it. To retire
# this quarantine once the bad days age out of every signal window, delete this
# block and the two `NOT (... QUARANTINE ...)` clauses that reference it.
QUARANTINE_BRAND         = "lc_waikiki"
QUARANTINE_SNAP_THROUGH  = "2026-07-25"   # snapshots on/before this date excluded
QUARANTINE_EVENT_THROUGH = "2026-07-26"   # price_events on/before this date excluded


def _snapshot_quarantine_sql(col="snapshot_date", brand_col="brand"):
    return (f"NOT ({brand_col} = '{QUARANTINE_BRAND}' "
            f"AND CAST({col} AS DATE) <= DATE '{QUARANTINE_SNAP_THROUGH}')")


def _event_quarantine_sql(col="recorded_at"):
    return (f"NOT (brand = '{QUARANTINE_BRAND}' "
            f"AND CAST({col} AS DATE) <= DATE '{QUARANTINE_EVENT_THROUGH}')")




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
_VARIANTS_READY   = False
_BASELINES_READY  = False
_PRODUCTS_READY   = False
_EVENTS_READY     = False
_GLOB_CACHE       = None
_EVENT_GLOB_CACHE = {}


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


def materialise_variants(con, force=False):
    """
    product_variants, pulled ONCE. Added 2026-07-21 after measuring that this
    table was crossing the wire FOUR times per run: twice inside khabar_lake
    (the hot_raw join and variant_baselines) and twice more from signal SQL
    that queried pg.public.product_variants directly (l1_01, l1_17). At 452K
    rows it is the single most expensive table in the pipeline, so paying for
    it four times was the largest avoidable cost in the whole system.

    Unfiltered on purpose: hot_raw joins snapshots against ALL variants, while
    variant_baselines wants only active/non-delisted ones. Pulling the superset
    once and filtering locally is strictly cheaper than two filtered pulls.
    """
    global _VARIANTS_READY
    if _VARIANTS_READY and not force:
        return con.execute("SELECT count(*) FROM variants_raw").fetchone()[0]

    con.execute("DROP TABLE IF EXISTS variants_raw")
    con.execute("""
        CREATE TABLE variants_raw AS
        SELECT
            id                                      AS variant_id,
            product_id                              AS product_id,
            external_sku                            AS external_sku,
            size                                    AS size,
            size_family                             AS size_family,
            size_system                             AS size_system,
            size_status                             AS size_status,
            color                                   AS color,
            CAST(first_observed_price AS DOUBLE)    AS first_observed_price,
            is_in_stock                             AS is_in_stock,
            delisted_at                             AS delisted_at
        FROM pg.public.product_variants
    """)
    _VARIANTS_READY = True
    return con.execute("SELECT count(*) FROM variants_raw").fetchone()[0]


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

    # Dimensions first, so the join below reads LOCAL tables. This previously
    # joined pg.public.products and pg.public.product_variants directly, which
    # dragged both across the wire again on top of the copies prefetch() was
    # already pulling. Both calls no-op if already materialised.
    materialise_products(con)
    materialise_variants(con)

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
            p.attributes_extracted    AS attributes_extracted,
            CAST(ps.price AS DOUBLE)             AS price,
            CAST(ps.compare_at_price AS DOUBLE)  AS compare_at_price,
            CAST(ps.discount_pct AS DOUBLE)      AS discount_pct,
            CAST(ps.snapshot_date AS VARCHAR)    AS snapshot_date,
            CAST(ps.recorded_at AS VARCHAR)      AS recorded_at
        FROM pg.public.price_snapshots ps
        LEFT JOIN products_dim p  ON p.product_id  = ps.product_id
        LEFT JOIN variants_raw pv ON pv.variant_id = ps.variant_id
        WHERE """ + _snapshot_quarantine_sql("ps.snapshot_date", "ps.brand") + """
    """)
    _HOT_READY = True
    return con.execute("SELECT count(*) FROM hot_raw").fetchone()[0]


def prefetch(con):
    """
    Call once, immediately after connect(), before computing anything.

    Every materialise/dimension function used by any signal's SQL must be
    called here. There is no on-demand fallback — a signal's SQL is a plain
    string; it cannot call a Python function to build a table it finds
    missing. (Corrected 2026-07-21: this docstring previously claimed
    variant_baselines() would materialise on demand if skipped here. It
    would not, and didn't — l1_10 and l1_11 failed in the field on exactly
    this, because the call below was missing. It no longer is.)
    """
    n_prod = materialise_products(con)
    n_var  = materialise_variants(con)
    n_snap = materialise_hot(con)
    n_base = variant_baselines(con)
    n_stock, n_price = materialise_events(con)
    print(f"  📥 Materialised once each — snapshots: {n_snap:,}, products: "
          f"{n_prod:,}, variants: {n_var:,}, baselines: {n_base:,}, "
          f"stockout events: {n_stock:,}, price events: {n_price:,}. "
          f"Exactly 5 Postgres reads this run.")
    return n_snap, n_prod, n_base, n_stock, n_price


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


def _event_files(con, prefix, start_day, end_day):
    """
    R2 day-files for an archived EVENT table (stockout_events / price_events),
    same layout housekeeping.py writes: {prefix}/YYYY-MM-DD.parquet. Listed
    once per prefix per process. A missing day is absent, not an error —
    identical contract to _day_files() for price_snapshots. Returns [] when the
    archive for this prefix is still empty (nothing has aged out yet), in which
    case the caller degrades cleanly to hot-only.
    """
    global _EVENT_GLOB_CACHE
    if prefix not in _EVENT_GLOB_CACHE:
        _EVENT_GLOB_CACHE[prefix] = con.execute(f"""
            SELECT file FROM glob('s3://{R2_BUCKET_NAME}/{prefix}/*.parquet')
        """).fetchall()
    wanted = []
    for (path,) in _EVENT_GLOB_CACHE[prefix]:
        stem = path.split("/")[-1].replace(".parquet", "")
        try:
            d = date.fromisoformat(stem)
        except ValueError:
            continue  # legacy or stray object — not a day-partition
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
    # The view below joins variant_baselines for the honest discount baseline
    # (MIN first_observed_price per product). Ensure it exists regardless of
    # call order; no-ops if prefetch() already built it.
    variant_baselines(con)

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
            -- THE HONEST BASELINE. Rewritten 2026-07-21.
            --
            -- This column previously derived from compare_at_price, which is
            -- the brand's own published RRP -- exactly the field the project's
            -- core invariant forbids measuring discounts against, because a
            -- brand can inflate it at will to manufacture a discount. It was
            -- named "honest" while measuring the opposite of honest.
            --
            -- Two consequences, both live and both bad:
            --   1. Every signal filtering on it (l1_10 Dead Stock) was ranking
            --      brands by their own marketing claims.
            --   2. Brands that publish no RRP at all were structurally
            --      INVISIBLE to those signals -- confirmed live: lc_waikiki
            --      (1,298 price events), just_sbr, mobaco and mlameh have
            --      compare_at_price on 0% of rows. LCW, one of the largest
            --      brands tracked, could never appear in Dead Stock.
            --
            -- Now derived from first_observed_price: the price Khabar itself
            -- first witnessed. A brand cannot retroactively edit our own
            -- observation, and every brand has one regardless of whether they
            -- publish an RRP.
            --
            -- COLLAPSE RULE: MIN of the VARIANT-level first_observed_price,
            -- matching l1_01 and l1_17 exactly (same filters: active product,
            -- non-delisted variant, positive price). products.first_observed_
            -- price is NOT used here even though it is product-level and would
            -- be simpler -- checked live, the two disagree on 11.8% of products
            -- (6,770 where the product-level value is higher, 1,501 lower). Had
            -- this view used the product-level column, l1_10/l1_13 would have
            -- reported a different discount depth than l1_01/l1_17 for the same
            -- product on the same day, and a client dashboard would show two
            -- contradictory numbers side by side.
            CASE
                WHEN pf.baseline_price IS NOT NULL
                 AND pf.baseline_price > 0
                 AND pf.baseline_price >= d.price
                THEN ROUND(100.0 * (pf.baseline_price - d.price)
                           / pf.baseline_price, 2)
                ELSE NULL
            END AS honest_discount_pct,
            -- The brand's OWN claim, kept but named for what it is. Useful for
            -- anchor-inflation work (comparing claim against reality); never
            -- to be used as a discount measure. NULL -- never 0 -- when the
            -- brand published no RRP, because "no RRP" is not "no discount".
            CASE
                WHEN d.compare_at_price IS NOT NULL
                 AND d.compare_at_price > 0
                 AND d.compare_at_price >= d.price
                THEN ROUND(100.0 * (d.compare_at_price - d.price)
                           / d.compare_at_price, 2)
                ELSE NULL
            END AS brand_claimed_discount_pct
        FROM deduped d
        LEFT JOIN (
            SELECT product_id, MIN(first_observed_price) AS baseline_price
            FROM variant_baselines
            WHERE first_observed_price IS NOT NULL AND first_observed_price > 0
            GROUP BY product_id
        ) pf ON pf.product_id = d.product_id
        WHERE d.rn = 1
          AND """ + _snapshot_quarantine_sql("d.snapshot_date", "d.brand") + """
    """)

    n = con.execute("SELECT count(*) FROM snapshots").fetchone()[0]
    return n, len(files), start_day, end_day


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

    Also exposes `size_level_reliable` (bool) — FALSE for every LC Waikiki row,
    TRUE otherwise. LCW's per-size is_in_stock is written once at row-creation
    time and never refreshed (see scraper.py v14.47 note in the LCW size pass),
    so any query that reads is_in_stock at size grain must filter on this first.
    Signals that only need color-level LCW stock (the parent row's is_in_stock,
    refreshed every run by the catalog pass) are unaffected — this flag only
    matters when a query is distinguishing between sizes.
    """
    # A TABLE, not a view — for the same reason as hot_raw. product_variants is
    # ~481K rows; any signal referencing this view more than once would have
    # pulled all of them from Postgres each time.
    global _BASELINES_READY
    if _BASELINES_READY:
        return con.execute("SELECT count(*) FROM variant_baselines").fetchone()[0]

    # Derived from the LOCAL variants_raw / products_dim copies rather than
    # re-querying pg.public. Same filter, same result, zero extra egress.
    materialise_products(con)
    materialise_variants(con)
    con.execute("DROP TABLE IF EXISTS variant_baselines")
    con.execute("""
        CREATE TABLE variant_baselines AS
        SELECT
            pv.variant_id           AS variant_id,
            pv.product_id           AS product_id,
            pv.external_sku         AS external_sku,
            pv.size                 AS size,
            pv.color                AS color,
            pv.first_observed_price AS first_observed_price,
            pv.is_in_stock          AS is_in_stock,
            pv.delisted_at          AS delisted_at,
            -- size_level_reliable — added 2026-07-25 after confirming live that
            -- LC Waikiki's per-size is_in_stock is not a fresh read. LCW's product
            -- page no longer exposes per-size stock (schema.org ld+json rewrite,
            -- June 2026) so the size-backfill pass falls back to the color-level
            -- flag ONCE, at the moment a size row is first created, and NEVER
            -- revisits it afterward (the backfill only selects rows where
            -- size IS NULL, so an already-sized row is invisible to every future
            -- pass). Confirmed live: within one LCW product+color, 17,868 groups
            -- of 2+ sizes show DIFFERING stock across sizes in only 12 of them
            -- (0.1%) -- vs. 78.1% for a real per-size brand (Ravin) in the same
            -- check. Every LCW size now also carries a size value (0 nulls left),
            -- so "size IS NOT NULL" can no longer be used to tell a fresh
            -- color-level row from a frozen one -- this flag is the replacement.
            --
            -- l1_11 (Size Asymmetry Stockout) doesn't need this: it reads
            -- stockout_events, which never carries a size for LCW (the scraper's
            -- catalog pass hardcodes size=NULL on every LCW stockout write), so
            -- it was already structurally safe. This flag exists for anything
            -- that reads product_variants / variant_baselines directly instead
            -- -- ad-hoc checks, the chatbot, or a future signal -- which had no
            -- guard until now. Brand-level, not row-level: LCW's own color-wide
            -- flag (on what used to be the sole "parent" row) is fine, but since
            -- every row now looks identical in the schema, there is no reliable
            -- way to single that row back out, so the whole brand is marked
            -- unreliable at the size grain. False for LCW, true otherwise.
            (p.brand != 'lc_waikiki')                          AS size_level_reliable
        FROM variants_raw pv
        JOIN products_dim p ON p.product_id = pv.product_id
        WHERE p.is_active = TRUE
          AND pv.delisted_at IS NULL
    """)
    _BASELINES_READY = True
    return con.execute("SELECT count(*) FROM variant_baselines").fetchone()[0]


# =============================================================================
# PRODUCTS DIM, STOCKOUT EVENTS, PRICE EVENTS — added when nine new L1 signals
# needed them and would otherwise have queried pg.public directly, once per
# signal, exactly the mistake THE ONE-READ RULE above exists to prevent. These
# tables are two orders of magnitude smaller than price_snapshots (products:
# ~70K rows, stockout_events: ~85K, price_events: ~12K) so the cost of getting
# this wrong is smaller — but it is the same wrong, and it compounds the same
# way as more signals are added. Fixed once, here, rather than per-signal.
#
# Neither price_events nor stockout_events is purged on a schedule the way
# price_snapshots is (per project convention, both stay fully in Postgres —
# the scraper's own last-price lookup and the bot's live-deals screen read
# price_events directly and would break if it were windowed). So unlike
# hot_raw, there is no hot/cold seam to reconcile here: one pull is the whole
# table, always.
# =============================================================================

def materialise_products(con, force=False):
    """
    Every product, unfiltered — deliberately NOT scoped to is_active like
    variant_baselines is. Product Delisted needs to see inactive products;
    New SKU Launch and lifecycle signals need first_seen_at/last_seen_at
    regardless of current state. Filtering here would silently hide the exact
    rows several of these signals exist to find.
    """
    global _PRODUCTS_READY
    if _PRODUCTS_READY and not force:
        return con.execute("SELECT count(*) FROM products_dim").fetchone()[0]

    con.execute("DROP TABLE IF EXISTS products_dim")
    con.execute("""
        CREATE TABLE products_dim AS
        SELECT
            id                                  AS product_id,
            brand                                AS brand,
            name                                 AS name,
            department                           AS department,
            category_raw                         AS category_raw,
            category_normalized                  AS category_normalized,
            subcategory                          AS subcategory,
            gender                               AS gender,
            CAST(attributes_extracted AS VARCHAR) AS attributes_extracted,
            CAST(first_observed_price AS DOUBLE) AS first_observed_price,
            CAST(first_seen_at AS VARCHAR)       AS first_seen_at,
            CAST(last_seen_at AS VARCHAR)        AS last_seen_at,
            is_active                            AS is_active,
            CAST(delisted_at AS VARCHAR)         AS delisted_at
        FROM pg.public.products
    """)
    _PRODUCTS_READY = True
    return con.execute("SELECT count(*) FROM products_dim").fetchone()[0]


def _parquet_columns(con, file_list_sql):
    """
    Column names present across a set of R2 parquet files (schema union).
    Lets the cold-tier SELECT degrade gracefully when older archive files
    predate a column — e.g. stockout_events files written before the
    2026-07-31 housekeeping witnessed/seed_reason fix. union_by_name only
    surfaces columns that exist in at least one file, so a column absent from
    EVERY file cannot be referenced at all; we emit a typed default for it
    instead of letting DuckDB raise a binder error mid-run.
    """
    rows = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet([{file_list_sql}], union_by_name=true)"
    ).fetchall()
    return {r[0] for r in rows}


def materialise_events(con, force=False):
    """
    stockout_events and price_events, pulled once each.

    THE WITNESSED FILTER IS APPLIED HERE, NOT PER-SIGNAL. Checked live
    (2026-07-21): stockouts are 78% witnessed=true (cleaner than previously
    documented), but RESTOCKS are only 21% witnessed=true — 79% carry a
    seed_reason (mass_event_artifact, orphan_restock, duplicate_transition,
    delist_cycle) meaning they were never a real observed transition. Any
    signal built on restock timing without this filter inherits that
    contamination silently. Delisted-type rows have witnessed=NULL by design
    (delisting isn't a stock transition) and are kept regardless.

    Filtering centrally here means every current and future signal gets the
    clean version automatically — nobody has to remember to add the WHERE
    clause themselves.
    """
    global _EVENTS_READY
    if _EVENTS_READY and not force:
        n1 = con.execute("SELECT count(*) FROM stockouts_raw").fetchone()[0]
        n2 = con.execute("SELECT count(*) FROM price_events_raw").fetchone()[0]
        return n1, n2

    materialise_products(con)   # stock_events below joins products_dim; ensure it exists
                                 # regardless of call order — no extra Postgres read if
                                 # already materialised, materialise_products() no-ops.

    # =========================================================================
    # HOT + COLD STITCH (added 2026-07-31). Both event tables age out of
    # Supabase into R2 on a rolling window (housekeeping.py: stockout_events
    # >21d, price_events >30d), exactly like price_snapshots. Reading only the
    # hot table silently capped every event-based signal at that window — a
    # 90-day window signal was really seeing 21-30 days. These now stitch the
    # R2 day-file archive back in, deduped on event_id (hot wins on overlap),
    # the same overlap rule snapshots() uses for price_snapshots.
    #
    # STRICT SUPERSET: while a table's R2 prefix is still empty (nothing has
    # aged out yet), the cold branch is skipped and the ORIGINAL hot-only query
    # runs unchanged — so deploying this is a no-op until the archive fills,
    # then it deepens automatically. Bounded to EVENT_WINDOW_DAYS so the local
    # tables cannot grow without limit as the archive deepens.
    # =========================================================================
    _ev_end   = date.today()
    _ev_start = _ev_end - timedelta(days=EVENT_WINDOW_DAYS - 1)

    # ---- stockout_events ----------------------------------------------------
    so_hot = f"""
        SELECT
            id AS event_id, variant_id, product_id, brand, size, color, event_type,
            CAST(price_at_event AS DOUBLE)        AS price_at_event,
            CAST(discount_pct_at_event AS DOUBLE) AS discount_pct_at_event,
            was_on_discount,
            CAST(recorded_at AS TIMESTAMP)        AS recorded_at,
            witnessed, seed_reason
        FROM pg.public.stockout_events
        WHERE CAST(recorded_at AS DATE) >= DATE '{_ev_start}'
    """
    so_files = _event_files(con, "stockout_events", _ev_start, _ev_end)
    con.execute("DROP TABLE IF EXISTS stockouts_raw")
    if not so_files:
        con.execute(f"""
            CREATE TABLE stockouts_raw AS
            SELECT * FROM ({so_hot})
            WHERE witnessed = TRUE OR witnessed IS NULL
        """)
    else:
        _so_list = ", ".join(f"'{f}'" for f in so_files)
        _cols = _parquet_columns(con, _so_list)
        # Columns added by the 2026-07-31 housekeeping fix are absent from any
        # file archived before it. Absent (or NULL) witnessed → treat as NOT
        # witnessed (FALSE), so unverifiable pre-fix history is excluded rather
        # than silently polluting stockouts_raw. Stock history therefore only
        # genuinely deepens as post-fix day-files accumulate.
        _wit  = ("COALESCE(CAST(witnessed AS BOOLEAN), FALSE)"
                 if "witnessed" in _cols else "FALSE")
        _seed = ("CAST(seed_reason AS VARCHAR)"
                 if "seed_reason" in _cols else "CAST(NULL AS VARCHAR)")
        so_cold = f"""
            SELECT
                event_id,
                CAST(variant_id AS BIGINT)            AS variant_id,
                CAST(product_id AS BIGINT)            AS product_id,
                CAST(brand AS VARCHAR)                AS brand,
                CAST(size AS VARCHAR)                 AS size,
                CAST(color AS VARCHAR)                AS color,
                CAST(event_type AS VARCHAR)           AS event_type,
                CAST(price_at_event AS DOUBLE)        AS price_at_event,
                CAST(discount_pct_at_event AS DOUBLE) AS discount_pct_at_event,
                CAST(was_on_discount AS BOOLEAN)      AS was_on_discount,
                CAST(recorded_at AS TIMESTAMP)        AS recorded_at,
                {_wit}                                AS witnessed,
                {_seed}                               AS seed_reason
            FROM read_parquet([{_so_list}], union_by_name=true)
        """
        con.execute(f"""
            CREATE TABLE stockouts_raw AS
            WITH hot AS ({so_hot}),
                 cold AS ({so_cold}),
                 unioned AS (
                     SELECT *, 1 AS tier_rank FROM hot
                     UNION ALL BY NAME
                     SELECT *, 2 AS tier_rank FROM cold
                 ),
                 deduped AS (
                     SELECT *, ROW_NUMBER() OVER (
                                 PARTITION BY event_id ORDER BY tier_rank
                             ) AS rn
                     FROM unioned
                 )
            SELECT event_id, variant_id, product_id, brand, size, color, event_type,
                   price_at_event, discount_pct_at_event, was_on_discount,
                   recorded_at, witnessed, seed_reason
            FROM deduped
            WHERE rn = 1
              AND (witnessed = TRUE OR witnessed IS NULL)
        """)

    # ---- price_events -------------------------------------------------------
    pe_hot = f"""
        SELECT
            id AS event_id, product_id, brand,
            CAST(price_before AS DOUBLE)     AS price_before,
            CAST(price_after AS DOUBLE)      AS price_after,
            CAST(compare_at_price AS DOUBLE) AS compare_at_price,
            CAST(discount_pct AS DOUBLE)     AS discount_pct,
            direction,
            CAST(sizes_in_stock AS VARCHAR)  AS sizes_in_stock,
            CAST(recorded_at AS TIMESTAMP)   AS recorded_at,
            is_statistical_deal, is_flash_sale
        FROM pg.public.price_events
        WHERE CAST(recorded_at AS DATE) >= DATE '{_ev_start}'
    """
    pe_files = _event_files(con, "price_events", _ev_start, _ev_end)
    con.execute("DROP TABLE IF EXISTS price_events_raw")
    if not pe_files:
        con.execute(f"""
            CREATE TABLE price_events_raw AS
            SELECT * FROM ({pe_hot})
            WHERE {_event_quarantine_sql()}
        """)
    else:
        _pe_list = ", ".join(f"'{f}'" for f in pe_files)
        _cols = _parquet_columns(con, _pe_list)
        _stat  = ("CAST(is_statistical_deal AS BOOLEAN)"
                  if "is_statistical_deal" in _cols else "CAST(NULL AS BOOLEAN)")
        _flash = ("CAST(is_flash_sale AS BOOLEAN)"
                  if "is_flash_sale" in _cols else "CAST(NULL AS BOOLEAN)")
        pe_cold = f"""
            SELECT
                event_id,
                CAST(product_id AS BIGINT)       AS product_id,
                CAST(brand AS VARCHAR)           AS brand,
                CAST(price_before AS DOUBLE)     AS price_before,
                CAST(price_after AS DOUBLE)      AS price_after,
                CAST(compare_at_price AS DOUBLE) AS compare_at_price,
                CAST(discount_pct AS DOUBLE)     AS discount_pct,
                CAST(direction AS VARCHAR)       AS direction,
                CAST(sizes_in_stock AS VARCHAR)  AS sizes_in_stock,
                CAST(recorded_at AS TIMESTAMP)   AS recorded_at,
                {_stat}                          AS is_statistical_deal,
                {_flash}                         AS is_flash_sale
            FROM read_parquet([{_pe_list}], union_by_name=true)
        """
        con.execute(f"""
            CREATE TABLE price_events_raw AS
            WITH hot AS ({pe_hot}),
                 cold AS ({pe_cold}),
                 unioned AS (
                     SELECT *, 1 AS tier_rank FROM hot
                     UNION ALL BY NAME
                     SELECT *, 2 AS tier_rank FROM cold
                 ),
                 deduped AS (
                     SELECT *, ROW_NUMBER() OVER (
                                 PARTITION BY event_id ORDER BY tier_rank
                             ) AS rn
                     FROM unioned
                 )
            SELECT event_id, product_id, brand, price_before, price_after,
                   compare_at_price, discount_pct, direction, sizes_in_stock,
                   recorded_at, is_statistical_deal, is_flash_sale
            FROM deduped
            WHERE rn = 1
              AND {_event_quarantine_sql()}
        """)
    _EVENTS_READY = True
    n1 = con.execute("SELECT count(*) FROM stockouts_raw").fetchone()[0]
    n2 = con.execute("SELECT count(*) FROM price_events_raw").fetchone()[0]

    # =========================================================================
    # stock_events — REBUILT 2026-07-21. This view already existed in
    # production before today's changes; l1_08 (Variant Stockout) and l1_09
    # (Variant Restock) depend on it directly by name and were broken by the
    # rewrite of this file until this was restored. Rebuilt from stockouts_raw
    # (already witnessed-filtered) joined to products_dim for the columns
    # those two signals expect on every row: category_normalized, gender,
    # event_date. No additional Postgres read — both source tables are
    # already local by this point in prefetch().
    #
    # Filtered to witnessed = TRUE strictly (not "OR NULL" like stockouts_raw
    # itself) — l1_08/l1_09 only ever touch event_type IN ('stockout',
    # 'restock'), and delisted-type rows (which carry witnessed = NULL by
    # design) have no place in an inventory-movement view. Matches the
    # contract documented in signals.py's BLOCKERS["seeded_stockout"].
    # =========================================================================
    con.execute("DROP VIEW IF EXISTS stock_events")
    con.execute("""
        CREATE VIEW stock_events AS
        SELECT
            so.event_id,
            so.variant_id,
            so.product_id,
            so.brand,
            pd.category_normalized,
            pd.gender,
            so.event_type,
            so.was_on_discount,
            so.recorded_at,
            CAST(so.recorded_at AS DATE) AS event_date
        FROM stockouts_raw so
        JOIN products_dim pd ON pd.product_id = so.product_id
        WHERE so.witnessed = TRUE
          AND so.event_type IN ('stockout', 'restock')
    """)

    return n1, n2


def stockout_events(con):
    """
    RESTORED 2026-07-21 — compute_signals.py calls this directly, once, right
    after prefetch(), and prints its return value as "witnessed transitions"
    in the run log. It is a separate entry point from prefetch() on purpose:
    it reads a different source table (stockout_events, not price_snapshots),
    and the witnessed filter belongs here rather than in every signal's SQL —
    a signal that forgot the filter would look like it was working while
    overstating sellout volume roughly 3x (57,171 of 85,205 raw rows are
    collection artefacts: orphan restocks, delist-cycle noise, duplicates).

    Idempotent and cheap on the common path: prefetch() already calls
    materialise_events() internally, so by the time this runs, stockouts_raw,
    products_dim, and stock_events already exist and this just counts a local
    table — no second Postgres read. Calling materialise_events() again here
    is what makes that true regardless of whether prefetch() ran first; the
    _EVENTS_READY guard inside it no-ops if it already has.
    """
    materialise_events(con)
    return con.execute("SELECT count(*) FROM stock_events").fetchone()[0]


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
