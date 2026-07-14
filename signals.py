"""
Khabar — THE SIGNAL REGISTRY.
================================================================================
This file is the single place where a signal is defined. It is data, not
machinery: each entry declares WHAT a signal is, WHAT it needs, and the SQL that
computes it. The runner (compute_signals.py) knows nothing about any specific
signal — it just walks this list.

Adding signal #25 means adding one entry here. No new file, no new workflow,
no new plumbing. That is the entire point: 24 L1 signals and 14 L2 products
cannot be 38 hand-maintained scripts.

--------------------------------------------------------------------------------
HOW A SIGNAL DEGRADES HONESTLY

Khabar's data is young and uneven. Brands were onboarded progressively (12 live
on 15 Jun, 20 by 7 Jul). Scrapers fail (LC Waikiki's proxy pool died on 13 Jul,
and the market did NOT suddenly shrink by one brand that day). Some source data
is known-contaminated (see SEEDED_STOCKOUT below).

A signal that quietly produces a number anyway is worse than one that produces
nothing, because a hollow number gets sold to a client. So every signal declares
its preconditions and the runner enforces them:

  min_days     A 30-day IQR computed over 8 days of data is an 8-day IQR wearing
               a false label. Rows without enough real history are SUPPRESSED and
               counted, not silently averaged over whatever happens to exist.

  requires     A named blocker from BLOCKERS below. While unresolved, the signal
               is skipped with a stated reason. Resolve it in ONE place and every
               signal waiting on it goes live together.

  enabled      Hard off-switch for signals deliberately deferred.

The result: signals switch THEMSELVES on as data accumulates or blockers clear.
Nothing needs to be remembered or re-triggered by hand.
"""

# =============================================================================
# BLOCKERS — known data problems that make specific signals untrustworthy.
#
# Set to False only when the underlying problem is genuinely fixed AND verified.
# Flipping one of these to False is a claim that a client can safely be sold a
# number built on it. Treat it that way.
# =============================================================================
BLOCKERS = {
    # 60–64% of rows in stockout_events are "seeded": the SKU was ALREADY out of
    # stock the first time Khabar ever saw it. That is not a witnessed sellout —
    # it is our own blind spot at onboarding. Counting seeded events as demand
    # would systematically overstate how fast things sell out, and would do so
    # WORST for the newest brands (whose entire catalog was seeded on day one).
    # Every inventory signal is poisoned by this until the scraper distinguishes
    # "was in stock, then wasn't" from "never seen in stock".
    "seeded_stockout": {
        "resolved": False,
        "why": "60-64% of stockout_events are SKUs already out of stock on first "
               "observation, not witnessed sellouts. Inventory signals built on "
               "this would overstate demand velocity, worst for newest brands.",
    },

    # price_events fires only on an actual price CHANGE, so intraday movement
    # exists there — but price_snapshots is one row per product per DAY
    # (ON CONFLICT product_id, snapshot_date). The 2nd and 3rd scrape of a day
    # overwrite the 1st. A flash sale that drops and reverts inside one day is
    # therefore invisible in snapshots by construction, not by accident.
    "intraday_history": {
        "resolved": False,
        "why": "price_snapshots is one row per product per day by design. "
               "Intraday drop-and-revert is not recoverable from it. Needs a "
               "price_events-sourced signal instead.",
    },

    # LC Waikiki discounted on 24 of 29 observed days. Against a brand that
    # discounts almost daily, EVERY other brand appears to 'co-move' with it —
    # the correlation is an artifact of base rate, not a price war. More history
    # is the only fix; a cleverer query cannot rescue this.
    "cross_brand_history": {
        "resolved": False,
        "why": "LCW discounts near-daily (24 of 29 days), creating false "
               "co-occurrence with every other brand. Needs 8-12 weeks of price "
               "history before co-movement means anything.",
    },
}


# =============================================================================
# THE REGISTRY
# =============================================================================
SIGNALS = [

    # -------------------------------------------------------------------------
    # L1 · 07 — STATISTICAL PRICE ANOMALY (rolling IQR)
    #
    # "Is this price genuinely unusual for this product, or just normal noise?"
    #
    # IQR (inter-quartile range) rather than mean/standard-deviation on purpose:
    # retail prices are not normally distributed — they sit at one level for
    # weeks then jump. A mean gets dragged around by the very outliers we are
    # trying to detect; quartiles do not. A price below Q1 - 1.5*IQR is the
    # textbook definition of a low outlier, and it is computed per product
    # against ITS OWN history, so a 200 EGP t-shirt and a 2,000 EGP coat are
    # judged on the same footing.
    #
    # Guards that matter here:
    #   • days_observed is computed per PRODUCT, not per brand, and carried onto
    #     every output row. A product added yesterday cannot have a 30-day
    #     baseline no matter how long its brand has been live.
    #   • Products with a flat price history (IQR = 0) are excluded. With no
    #     variation, the fence collapses onto the price itself and every tiny
    #     move looks infinitely anomalous. Mathematically true, analytically
    #     worthless.
    # -------------------------------------------------------------------------
    {
        "id":       "l1_07",
        "name":     "Statistical Price Anomaly",
        "level":    "L1",
        "enabled":  True,
        "requires": [],
        "table":    "signal_l1_07_price_anomaly",
        "window_days": 30,
        "min_days":    14,   # below this, an "IQR" is noise dressed as statistics
        "unique_on": ["product_id", "snapshot_date"],
        "sql": """
WITH latest_day AS (
    SELECT max(snapshot_date) AS d FROM snapshots
),
-- Per-product distribution over the whole window. Collection gaps
-- shrink days_observed rather than being silently treated as
-- "no price change" — an absent day is unknown, not stable.
dist AS (
    SELECT
        product_id,
        count(DISTINCT snapshot_date) AS days_observed,
        median(price)                 AS median_price,
        quantile_cont(price, 0.25)    AS q1,
        quantile_cont(price, 0.75)    AS q3,
        min(price)                    AS min_price
    FROM snapshots
    WHERE price IS NOT NULL AND price > 0
    GROUP BY product_id
),
today AS (
    SELECT s.*
    FROM snapshots s, latest_day l
    WHERE s.snapshot_date = l.d
      AND s.price IS NOT NULL AND s.price > 0
),
-- Every product that clears the anomaly test today.
flagged AS (
    SELECT
        t.product_id, t.brand, t.product_name,
        t.category_normalized, t.gender, t.snapshot_date,
        t.price AS current_price,
        d.median_price, d.q1, d.q3, d.min_price, d.days_observed
    FROM today t
    JOIN dist d ON d.product_id = t.product_id
    WHERE d.days_observed >= {min_days}                -- the honesty guard
      AND (d.q3 - d.q1) > 0                            -- flat history = no signal
      AND t.price < d.q1 - 1.5 * (d.q3 - d.q1)         -- the anomaly itself
),
-- COORDINATION DETECTION.
-- Brands price in TIERS, not per-SKU. Seen live 2026-07-14: eight unrelated
-- Dalydress products (t-shirts, a belt, pyjamas) all landed on 950 the same
-- day; Men's Club moved five onto 438. Product-by-product that reads as
-- thirteen independent collapses; it is TWO decisions applied to price bands.
--
-- Grouped on (brand, landing price) ONLY. An earlier version also grouped on
-- median_price — that was wrong: median is each product's OWN history, so two
-- products moving together but with different history shapes were split into
-- singletons and no band was ever detected. The landing price is the shared
-- artefact of a tier decision; the origin price is not observable per-product
-- from a median.
bands AS (
    SELECT brand, current_price, count(*) AS band_move_size
    FROM flagged
    GROUP BY brand, current_price
)
SELECT
    f.product_id,
    f.brand,
    f.product_name,
    f.category_normalized,
    f.gender,
    f.snapshot_date,
    ROUND(f.current_price, 2) AS current_price,
    ROUND(f.median_price, 2)  AS median_price,
    ROUND(f.q1, 2)            AS q1_price,
    ROUND(f.q3, 2)            AS q3_price,
    ROUND(f.q3 - f.q1, 2)     AS iqr,
    ROUND(f.q1 - 1.5 * (f.q3 - f.q1), 2) AS lower_fence,
    ROUND(100.0 * (f.median_price - f.current_price)
          / NULLIF(f.median_price, 0), 2) AS deviation_pct,
    (f.current_price <= f.min_price) AS is_lowest_ever,
    f.days_observed,
    {window_days} AS window_days,
    b.band_move_size,
    (b.band_move_size >= 3) AS is_band_move
FROM flagged f
JOIN bands b
  ON b.brand = f.brand
 AND b.current_price = f.current_price
ORDER BY deviation_pct DESC
""",
        # Same shape as `sql` but WITHOUT the min_days filter — lets the runner
        # report how many products were withheld for thin history, instead of
        # them simply vanishing.
        "suppressed_sql": """
WITH latest_day AS (
    SELECT max(snapshot_date) AS d FROM snapshots
),
dist AS (
    SELECT product_id,
           count(DISTINCT snapshot_date) AS days_observed,
           quantile_cont(price, 0.25)    AS q1,
           quantile_cont(price, 0.75)    AS q3
    FROM snapshots
    WHERE price IS NOT NULL AND price > 0
    GROUP BY product_id
),
today AS (
    SELECT s.* FROM snapshots s, latest_day l
    WHERE s.snapshot_date = l.d AND s.price IS NOT NULL AND s.price > 0
)
SELECT count(*)
FROM today t
JOIN dist d ON d.product_id = t.product_id
WHERE d.days_observed < {min_days}
  AND (d.q3 - d.q1) > 0
  AND t.price < d.q1 - 1.5 * (d.q3 - d.q1)
""",
    },

    # -------------------------------------------------------------------------
    # L1 · 01 — GENUINE PRICE DROP
    #
    # "Has the price actually fallen from where it started?"
    #
    # Baseline is first_observed_price — the price Khabar itself first witnessed.
    # NEVER compare_at_price: that is whatever the brand types in, and a product
    # permanently listed at 499 "was 799" is not on sale, it is priced at 499.
    # This signal is immune to that by construction.
    #
    # COLLAPSE RULE: first_observed_price is VARIANT-level; snapshots are
    # PRODUCT-level. 96.5% of products carry one baseline across all variants;
    # where they differ we take the MIN, which yields the SMALLEST possible
    # drop_pct. A signal sold on the honesty of its baseline must never round in
    # its own favour.
    #
    # MIN_DROP is the one judgment call. Below it, a move is rounding or noise,
    # not a markdown decision. Currently 10% — provisional, to be tuned against
    # real Egyptian mid-range behaviour.
    # -------------------------------------------------------------------------
    {
        "id":       "l1_01",
        "name":     "Genuine Price Drop",
        "level":    "L1",
        "enabled":  True,
        "requires": [],
        "table":    "signal_l1_01_genuine_price_drop",
        "window_days": 1,
        "min_days":    1,
        "unique_on": ["product_id", "snapshot_date"],
        "sql": """
WITH latest_day AS (
    SELECT max(snapshot_date) AS d FROM snapshots
),
baselines AS (
    SELECT
        pv.product_id,
        MIN(CAST(pv.first_observed_price AS DOUBLE)) AS baseline_price
    FROM pg.public.product_variants pv
    JOIN pg.public.products p ON p.id = pv.product_id
    WHERE p.is_active = TRUE
      AND pv.delisted_at IS NULL
      AND pv.first_observed_price IS NOT NULL
      AND pv.first_observed_price > 0
    GROUP BY pv.product_id
),
today AS (
    SELECT s.*
    FROM snapshots s, latest_day l
    WHERE s.snapshot_date = l.d
      AND s.price IS NOT NULL AND s.price > 0
)
SELECT
    t.product_id,
    t.brand,
    t.product_name,
    t.category_normalized,
    t.gender,
    t.snapshot_date,
    ROUND(t.price, 2)          AS current_price,
    ROUND(b.baseline_price, 2) AS baseline_price,
    ROUND(100.0 * (b.baseline_price - t.price)
          / NULLIF(b.baseline_price, 0), 2) AS drop_pct
FROM today t
JOIN baselines b ON b.product_id = t.product_id
WHERE t.price < b.baseline_price
  AND (b.baseline_price - t.price) / b.baseline_price >= 0.10
ORDER BY drop_pct DESC
""",
        "suppressed_sql": None,
    },
# -------------------------------------------------------------------------
    # L1 · 03 — PRICE STAIRCASE
    #
    # "Is this product being walked down in steps, not one announced sale?"
    #
    # 3+ CONSECUTIVE down-moves, each price lower than the last, inside 21 days.
    # A single up-move breaks the chain — the run must be strictly monotonic, so
    # what we report is a genuine unbroken descent, not "3 drops that happened
    # near each other". Reads price_events (every change), NOT snapshots (daily),
    # because the sequence IS the signal and snapshots can't see intra-run steps.
    #
    # No step-size or net-depth floor: user chose "every drop lower than the
    # last" as the whole definition. If tiny-step noise shows up in output, a
    # per-step or total_descent floor is the tuning knob to add later.
    # -------------------------------------------------------------------------
    {
        "id":       "l1_03",
        "name":     "Price Staircase",
        "level":    "L1",
        "enabled":  True,
        "requires": [],
        "table":    "signal_l1_03_price_staircase",
        "window_days": 21,
        "min_days":    1,
        "unique_on": ["product_id", "snapshot_date"],
        "sql": """
WITH ev AS (
    SELECT
        pe.product_id, pe.brand,
        pe.price_before, pe.price_after, pe.direction, pe.recorded_at,
        lag(pe.price_after) OVER (
            PARTITION BY pe.product_id ORDER BY pe.recorded_at
        ) AS prev_after
    FROM pg.public.price_events pe
    WHERE pe.recorded_at >= CURRENT_TIMESTAMP - INTERVAL '21 days'
),
-- Mark each event as a valid staircase step: a DOWN move that is strictly
-- lower than the previous recorded price. Anything else (an up-move, a flat
-- move, a down-move that isn't actually lower) scores 0 and breaks the run.
steps AS (
    SELECT
        product_id, brand, price_after, recorded_at,
        CASE
            WHEN direction = 'down'
             AND (prev_after IS NULL OR price_after < prev_after)
            THEN 1 ELSE 0
        END AS good_step
    FROM ev
),
agg AS (
    SELECT
        product_id, brand,
        sum(good_step)                                   AS step_count,
        min(recorded_at) FILTER (WHERE good_step = 1)    AS first_step_at,
        max(recorded_at) FILTER (WHERE good_step = 1)    AS last_step_at,
        (array_agg(price_after ORDER BY recorded_at)
            FILTER (WHERE good_step = 1))[1]             AS first_price,
        (array_agg(price_after ORDER BY recorded_at DESC)
            FILTER (WHERE good_step = 1))[1]             AS last_price
    FROM steps
    GROUP BY product_id, brand
    HAVING sum(good_step) >= 3
)
SELECT
    a.product_id,
    a.brand,
    p.name                 AS product_name,
    p.category_normalized  AS category_normalized,
    p.gender               AS gender,
    CURRENT_DATE           AS snapshot_date,
    a.step_count,
    ROUND(a.first_price, 2) AS first_price,
    ROUND(a.last_price, 2)  AS last_price,
    ROUND(100.0 * (a.first_price - a.last_price)
          / NULLIF(a.first_price, 0), 2) AS total_descent_pct,
    a.first_step_at,
    a.last_step_at,
    CAST(date_diff('day', a.first_step_at, a.last_step_at) AS INTEGER) AS span_days
FROM agg a
LEFT JOIN pg.public.products p ON p.id = a.product_id
ORDER BY total_descent_pct DESC
""",
        "suppressed_sql": None,
    },
    # -------------------------------------------------------------------------
    # DEFERRED — declared here so they are VISIBLE and switch themselves on the
    # moment their blocker clears, rather than living in someone's memory.
    # The runner prints these on every run with the reason they're held back.
    # -------------------------------------------------------------------------
    {
        "id": "l1_08", "name": "Variant Stockout", "level": "L1",
        "enabled": True, "requires": ["seeded_stockout"],
        "table": None, "window_days": 30, "min_days": 14,
        "unique_on": [], "sql": None, "suppressed_sql": None,
    },
    {
        "id": "l1_09", "name": "Variant Restock", "level": "L1",
        "enabled": True, "requires": ["seeded_stockout"],
        "table": None, "window_days": 30, "min_days": 14,
        "unique_on": [], "sql": None, "suppressed_sql": None,
    },
    {
        "id": "l1_02", "name": "Intraday Flash Sale", "level": "L1",
        "enabled": True, "requires": ["intraday_history"],
        "table": None, "window_days": 30, "min_days": 14,
        "unique_on": [], "sql": None, "suppressed_sql": None,
    },
    {
        "id": "l1_16", "name": "Cross-Brand Simultaneous Discount", "level": "L1",
        "enabled": True, "requires": ["cross_brand_history"],
        "table": None, "window_days": 60, "min_days": 56,
        "unique_on": [], "sql": None, "suppressed_sql": None,
    },
]


def blocked_by(signal):
    """Which of this signal's declared blockers are still unresolved."""
    return [b for b in signal.get("requires", [])
            if not BLOCKERS.get(b, {}).get("resolved", False)]


def runnable():
    """Signals that are enabled, unblocked, and actually have SQL to run."""
    return [s for s in SIGNALS
            if s.get("enabled") and not blocked_by(s) and s.get("sql")]
