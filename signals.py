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
    # RESOLVED 2026-07-20. stockout_events now carries a `witnessed` boolean and
    # a `seed_reason`, applied retroactively to all 84,841 historical rows and
    # written forward by the scraper. khabar_lake.stockout_events() filters to
    # witnessed=TRUE by default, so an inventory signal cannot forget to.
    #
    # The investigation also corrected the original diagnosis recorded here. The
    # contamination was real but the mechanism was NOT first-observation
    # seeding: only 360 of 82,708 first-stockouts occurred within a day of a
    # product's first sighting. The true breakdown of 44,689 transitions is
    # 27,670 trustworthy (62%), 14,439 orphan restocks, 1,405 delist-cycle
    # artefacts, 1,175 duplicates.
    #
    # Stockouts were always cleaner than feared (96% survive). RESTOCKS were the
    # rotten half (29% survive). Since restock velocity — not sellout count — is
    # the primary input to the Supply Chain Stress Index, the practical impact
    # on L2-06 is unchanged from the original estimate.
    #
    # CAVEAT for any signal that switches on here: LC Waikiki and DeFacto
    # contribute ZERO witnessed inventory events. Neither publishes per-size
    # stock, and DeFacto's catalogue never reports out-of-stock at all — it
    # delists instead. Any inventory signal covers the other 20 brands. State
    # that coverage explicitly in client-facing output rather than implying a
    # market-wide view.
    "seeded_stockout": {
        "resolved": True,
        "why": "RESOLVED — stockout_events.witnessed separates observed "
               "transitions from collection artefacts; the lake filters on it "
               "by default. Note LCW and DeFacto contribute zero inventory "
               "events: neither publishes per-size stock.",
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
    FROM variants_raw pv
    JOIN products_dim p ON p.product_id = pv.product_id
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
        pe.price_before, pe.price_after, pe.direction,
        CAST(pe.recorded_at AS TIMESTAMP) AS recorded_at,
        lag(pe.price_after) OVER (
            PARTITION BY pe.product_id ORDER BY pe.recorded_at
        ) AS prev_after
    FROM price_events_raw pe
    WHERE CAST(pe.recorded_at AS TIMESTAMP) >= CURRENT_TIMESTAMP - INTERVAL '21 days'
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
LEFT JOIN products_dim p ON p.product_id = a.product_id
WHERE a.last_price < a.first_price
ORDER BY total_descent_pct DESC
""",
        "suppressed_sql": None,
    },
  # -------------------------------------------------------------------------
    # L1 · 17 — DISCOUNT DEPTH ESCALATION
    #
    # "Is this product's discount getting DEEPER over time, without selling?"
    #
    # A product cut once, then cut deeper again — each new low further below its
    # honest baseline (first_observed_price, MIN across variants). Threshold: 2+
    # deepenings (chosen over 3 because 30 days of history can't yet show three).
    # No stockout filter yet — that refinement waits on the seeded_stockout fix;
    # for now every escalation counts, which slightly over-includes.
    #
    # Depth is measured against the honest baseline, NEVER compare_at_price. A
    # "deepening" is a new running-maximum discount depth — intervening shallow
    # moves don't break the count, they just don't add to it.
    # -------------------------------------------------------------------------
    {
        "id":       "l1_17",
        "name":     "Discount Depth Escalation",
        "level":    "L1",
        "enabled":  True,
        "requires": [],
        "table":    "signal_l1_17_depth_escalation",
        "window_days": 30,
        "min_days":    1,
        "unique_on": ["product_id", "snapshot_date"],
        "sql": """
WITH baselines AS (
    SELECT product_id,
           MIN(CAST(first_observed_price AS DOUBLE)) AS baseline
    FROM variants_raw
    WHERE first_observed_price IS NOT NULL AND first_observed_price > 0
    GROUP BY product_id
),
ev AS (
    SELECT
        pe.product_id, pe.brand, pe.price_after,
        CAST(pe.recorded_at AS TIMESTAMP) AS recorded_at,
        b.baseline
    FROM price_events_raw pe
    JOIN baselines b ON b.product_id = pe.product_id
    WHERE CAST(pe.recorded_at AS TIMESTAMP) >= CURRENT_TIMESTAMP - INTERVAL '30 days'
      AND pe.price_after < b.baseline
),
-- Depth of each cut, plus the deepest cut seen BEFORE it. A row is a genuine
-- escalation only when it sets a new running maximum depth.
depth AS (
    SELECT
        product_id, brand, recorded_at,
        100.0 * (baseline - price_after) / baseline AS depth_pct,
        max(100.0 * (baseline - price_after) / baseline) OVER (
            PARTITION BY product_id ORDER BY recorded_at
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS running_max_prev
    FROM ev
),
esc AS (
    SELECT
        product_id, brand, recorded_at, depth_pct,
        CASE WHEN running_max_prev IS NULL OR depth_pct > running_max_prev
             THEN 1 ELSE 0 END AS is_escalation
    FROM depth
),
agg AS (
    SELECT
        product_id, brand,
        sum(is_escalation)                               AS escalation_count,
        min(recorded_at) FILTER (WHERE is_escalation=1)  AS first_step_at,
        max(recorded_at) FILTER (WHERE is_escalation=1)  AS last_step_at,
        (array_agg(depth_pct ORDER BY recorded_at)
            FILTER (WHERE is_escalation=1))[1]           AS first_depth_pct,
        (array_agg(depth_pct ORDER BY recorded_at DESC)
            FILTER (WHERE is_escalation=1))[1]           AS last_depth_pct
    FROM esc
    GROUP BY product_id, brand
    HAVING sum(is_escalation) >= 2
)
SELECT
    a.product_id,
    a.brand,
    p.name                AS product_name,
    p.category_normalized AS category_normalized,
    p.gender              AS gender,
    CURRENT_DATE          AS snapshot_date,
    a.escalation_count,
    ROUND(a.first_depth_pct, 2) AS first_depth_pct,
    ROUND(a.last_depth_pct, 2)  AS last_depth_pct,
    a.first_step_at,
    a.last_step_at,
    CAST(date_diff('day', a.first_step_at, a.last_step_at) AS INTEGER) AS span_days
FROM agg a
LEFT JOIN products_dim p ON p.product_id = a.product_id
WHERE a.last_depth_pct > a.first_depth_pct
ORDER BY a.last_depth_pct DESC
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
        "enabled": True, "requires": [],
        "table": "signal_l1_08_variant_stockout",
        "window_days": 30, "min_days": 14,
        "unique_on": ["snapshot_date", "brand", "category_normalized", "gender"],
        # WITNESSED ONLY. khabar_lake.stockout_events() already filters to
        # witnessed=TRUE before this SQL sees a row, so there is no filter here
        # to forget. 57,171 of 85,205 raw events are collection artefacts, not
        # market behaviour; counting them would inflate sellout volume most for
        # the brands with the shortest history.
        #
        # COVERAGE IS NOT 22 BRANDS. DeFacto contributes ZERO stockouts (its
        # catalogue never reports out-of-stock -- items are delisted instead)
        # and LC Waikiki contributes ~124 (colour-grain only; it publishes no
        # per-size stock). Any client-facing use of this signal must name the
        # brands covered rather than implying a market-wide view.
        "sql": """
            WITH ev AS (
                SELECT * FROM stock_events
                WHERE event_type = 'stockout'
                  AND event_date > CURRENT_DATE - {window_days}
            )
            SELECT
                CURRENT_DATE                              AS snapshot_date,
                brand,
                COALESCE(category_normalized, 'uncategorized') AS category_normalized,
                COALESCE(gender, 'unknown')               AS gender,
                COUNT(*)                                  AS stockout_events,
                COUNT(DISTINCT variant_id)                AS variants_affected,
                COUNT(DISTINCT product_id)                AS products_affected,
                COUNT(*) FILTER (WHERE was_on_discount)   AS on_discount_events,
                ROUND(100.0 * COUNT(*) FILTER (WHERE was_on_discount)
                      / NULLIF(COUNT(*), 0), 2)           AS on_discount_pct,
                COUNT(DISTINCT event_date)                AS observed_days,
                {window_days}                             AS window_days
            FROM ev
            GROUP BY brand, COALESCE(category_normalized, 'uncategorized'),
                     COALESCE(gender, 'unknown')
            HAVING COUNT(*) >= 3
            ORDER BY stockout_events DESC
        """,
        # Groups withheld for thin volume. Reporting this is the difference
        # between "this category had no sellouts" and "too few to be meaningful".
        "suppressed_sql": """
            SELECT COUNT(*) FROM (
                SELECT 1 FROM stock_events
                WHERE event_type = 'stockout'
                  AND event_date > CURRENT_DATE - {window_days}
                GROUP BY brand, COALESCE(category_normalized, 'uncategorized'),
                         COALESCE(gender, 'unknown')
                HAVING COUNT(*) < 3
            )
        """,
    },
    {
        "id": "l1_09", "name": "Variant Restock", "level": "L1",
        "enabled": True, "requires": [],
        "table": "signal_l1_09_variant_restock",
        "window_days": 30, "min_days": 14,
        "unique_on": ["snapshot_date", "brand", "category_normalized"],
        # ====================================================================
        # THE CENSORING PROBLEM — read before using this signal for anything.
        #
        # Only ~30% of witnessed stockouts have restocked yet (6,523 of 21,395
        # as of 2026-07-20). The other 70% are still out of stock. A variant
        # that sells out on the 18th and restocks 20 days later is INVISIBLE
        # in a 22-day window -- so any average computed from closed pairs
        # alone is biased DOWNWARD, and biased worst for the SLOWEST brands,
        # because their slow restocks are exactly the ones that fall outside
        # the window.
        #
        # That is the inverse of what a Supply Chain Stress Index is supposed
        # to say. Publishing median_restock_days on its own would make the
        # most stressed supply chains look the healthiest.
        #
        # The mitigation is structural, not cosmetic: every row carries
        # closed_pairs, open_stockouts and completion_rate_pct alongside the
        # median. A brand with a 2-day median and 25% completion is NOT
        # faster than one with a 5-day median and 90% completion — it is
        # slower, and the completion rate is what reveals it.
        #
        # This resolves properly once the window exceeds typical restock
        # time. Revisit the interpretation, not the SQL, at ~90 days.
        # ====================================================================
        "sql": """
            WITH seq AS (
                SELECT variant_id, product_id, brand, category_normalized,
                       event_type, recorded_at,
                       LEAD(event_type)  OVER (PARTITION BY variant_id
                                               ORDER BY recorded_at) AS nxt,
                       LEAD(recorded_at) OVER (PARTITION BY variant_id
                                               ORDER BY recorded_at) AS nxt_at
                FROM stock_events
            ),
            stockouts AS (
                SELECT brand,
                       COALESCE(category_normalized, 'uncategorized') AS cat,
                       recorded_at,
                       CASE WHEN nxt = 'restock'
                            THEN DATE_DIFF('second', recorded_at, nxt_at) / 86400.0
                       END AS restock_days
                FROM seq
                WHERE event_type = 'stockout'
                  AND CAST(recorded_at AS DATE) > CURRENT_DATE - {window_days}
            )
            SELECT
                CURRENT_DATE AS snapshot_date,
                brand,
                cat AS category_normalized,
                COUNT(*) FILTER (WHERE restock_days IS NOT NULL) AS closed_pairs,
                COUNT(*) FILTER (WHERE restock_days IS NULL)     AS open_stockouts,
                ROUND(100.0 * COUNT(*) FILTER (WHERE restock_days IS NOT NULL)
                      / NULLIF(COUNT(*), 0), 2)                  AS completion_rate_pct,
                -- Median, not mean: restock times are right-skewed, and one
                -- variant restocked after three weeks would drag a mean well
                -- past anything a buyer would recognise.
                ROUND(MEDIAN(restock_days), 2)                   AS median_restock_days,
                ROUND(QUANTILE_CONT(restock_days, 0.25), 2)      AS p25_restock_days,
                ROUND(QUANTILE_CONT(restock_days, 0.75), 2)      AS p75_restock_days,
                ROUND(MIN(restock_days), 2)                      AS fastest_restock_days,
                COUNT(DISTINCT CAST(recorded_at AS DATE))        AS observed_days,
                {window_days}                                    AS window_days
            FROM stockouts
            GROUP BY brand, cat
            HAVING COUNT(*) FILTER (WHERE restock_days IS NOT NULL) >= 5
            ORDER BY closed_pairs DESC
        """,
        # Groups with fewer than 5 completed pairs are withheld: a median over
        # 2 or 3 observations is not a median, it is an anecdote.
        "suppressed_sql": """
            SELECT COUNT(*) FROM (
                SELECT 1
                FROM (
                    SELECT variant_id, brand, category_normalized, event_type,
                           recorded_at,
                           LEAD(event_type) OVER (PARTITION BY variant_id
                                                  ORDER BY recorded_at) AS nxt
                    FROM stock_events
                ) s
                WHERE s.event_type = 'stockout'
                  AND CAST(s.recorded_at AS DATE) > CURRENT_DATE - {window_days}
                GROUP BY s.brand, COALESCE(s.category_normalized, 'uncategorized')
                HAVING COUNT(*) FILTER (WHERE s.nxt = 'restock') < 5
            )
        """,
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


    # -------------------------------------------------------------------
    # #12 — New SKU Launch. Marked "Done" in planning; simply never wired
    # into the new Signal Engine.
    #
    # EXCLUDES EACH BRAND'S ONBOARDING DAY. Confirmed live: every large
    # first_seen_at spike (Town Team 2,869 on May 28; LC Waikiki 7,151 on
    # Jun 6; DeFacto 5,062 on Jun 9; Andora 4,371 on Jun 11 — 25,000+ rows
    # total) is the day that brand was first added to the scraper, not a
    # real product launch. Without this filter the signal was mostly
    # reporting catalog backfill as new arrivals. Each brand's own MIN
    # (first_seen_at) day is excluded — everything after is a genuine
    # first-sighting.
    # -------------------------------------------------------------------
    {
        "id": "l1_12",
        "name": "New SKU Launch",
        "level": "L1",
        "table": "signal_l1_12_new_sku_launch",
        "unique_on": ["product_id", "snapshot_date"],
        "window_days": 90,
        "min_days": 1,
        "enabled": True,
        "requires": [],
        "sql": """
            WITH onboarding_day AS (
                SELECT brand, min(CAST(first_seen_at AS DATE)) AS brand_onboarded_on
                FROM products_dim
                GROUP BY brand
            )
            SELECT
                pd.product_id,
                pd.brand,
                pd.name                        AS product_name,
                pd.department,
                pd.category_normalized,
                pd.subcategory,
                pd.first_observed_price        AS launch_price,
                CAST(pd.first_seen_at AS DATE) AS launch_date,
                CAST(pd.first_seen_at AS DATE) AS snapshot_date
            FROM products_dim pd
            JOIN onboarding_day ob ON ob.brand = pd.brand
            WHERE CAST(pd.first_seen_at AS DATE) > ob.brand_onboarded_on
              AND CAST(pd.first_seen_at AS DATE) >= CURRENT_DATE - INTERVAL '{window_days} days'
        """,
    },

    # -------------------------------------------------------------------
    # #13 — Product Delisted. Also marked "Done", also never wired in.
    # Deliberately reads products_dim.is_active/last_seen_at — NOT
    # product_variants.delisted_at, which has a known bug (set by
    # housekeeping's stale-check, never cleared by the scraper on restock;
    # confirmed live: 1,010 variants flagged delisted with is_in_stock=true).
    # Using the product-level field sidesteps that bug entirely.
    #
    # EXCLUDES MASS-DELIST ANOMALY DAYS. Investigated 2026-07-21: Dalydress
    # showed 3,737 products (68% of its catalogue) "delisting" on 2026-06-18,
    # exactly one day after the brand was onboarded on 06-17. Evidence it is
    # a collection failure, not brand behaviour:
    #   • 68.3% died within 3 days of onboarding; next-worst brand is DeFacto
    #     at 1.6% — a 43x outlier.
    #   • Esla was onboarded the SAME DAY and shows 0.0% early death, so it
    #     was not a platform-wide event.
    #   • The dead cohort averages 2.36 products per distinct product name vs
    #     1.22 for the surviving cohort — the duplication signature of an
    #     over-collecting pagination walk (see the known Shopify page-echo
    #     bug: the API repeats the last real page past catalogue end).
    #   • is_active itself is NOT stale: 1,626/1,635 active products appear in
    #     the current hot window, 0/3,852 inactive ones do. The products
    #     genuinely never returned — they were never real.
    #
    # Rule is deliberately GENERAL, not a Dalydress special case: any single
    # day where >20% of a brand's whole catalogue delists is a collection
    # anomaly. Real lifecycle delisting is a trickle; a two-thirds cliff in
    # 24 hours is a bug by definition. Across all history this currently
    # catches exactly one event (Dalydress 06-18). Tree 06-18 at 19.7% and
    # Mobaco 06-17 at 10.8% fall below the line and are retained — Tree sits
    # close enough, and shares the date, that it may be a milder instance of
    # the same fault. Left in pending review rather than silently swept up by
    # a threshold tuned to hide it.
    # -------------------------------------------------------------------
    {
        "id": "l1_13",
        "name": "Product Delisted",
        "level": "L1",
        "table": "signal_l1_13_product_delisted",
        "unique_on": ["product_id", "snapshot_date"],
        "window_days": 90,
        "min_days": 1,
        "enabled": True,
        "requires": [],
        "sql": """
            WITH brand_catalogue AS (
                SELECT brand, count(*) AS catalogue_size
                FROM products_dim
                GROUP BY brand
            ),
            delist_by_day AS (
                SELECT brand, CAST(last_seen_at AS DATE) AS delist_day,
                       count(*) AS n_delisted
                FROM products_dim
                WHERE is_active = FALSE
                GROUP BY brand, CAST(last_seen_at AS DATE)
            ),
            anomaly_days AS (
                -- Brand-days to suppress entirely. See header comment.
                SELECT d.brand, d.delist_day
                FROM delist_by_day d
                JOIN brand_catalogue c ON c.brand = d.brand
                WHERE 100.0 * d.n_delisted / c.catalogue_size > 20
            ),
            last_price AS (
                SELECT product_id, price, honest_discount_pct,
                       ROW_NUMBER() OVER (
                           PARTITION BY product_id ORDER BY snapshot_date DESC
                       ) AS rn
                FROM snapshots
            )
            SELECT
                pd.product_id,
                pd.brand,
                pd.name                        AS product_name,
                pd.department,
                pd.category_normalized,
                pd.subcategory,
                pd.first_observed_price,
                lp.price                       AS final_price,
                lp.honest_discount_pct         AS final_discount_pct,
                CAST(pd.last_seen_at AS DATE)  AS delisted_date,
                CAST(pd.last_seen_at AS DATE)  AS snapshot_date
            FROM products_dim pd
            LEFT JOIN last_price lp ON lp.product_id = pd.product_id AND lp.rn = 1
            LEFT JOIN anomaly_days ad
                   ON ad.brand = pd.brand
                  AND ad.delist_day = CAST(pd.last_seen_at AS DATE)
            WHERE pd.is_active = FALSE
              AND ad.brand IS NULL   -- drop mass-delist anomaly days entirely
              AND CAST(pd.last_seen_at AS DATE) >= CURRENT_DATE - INTERVAL '{window_days} days'
        """,
    },

    # -------------------------------------------------------------------
    # #10 — Velocity Decay (Dead Stock). Pure state check: all variants
    # still in stock while deeply discounted. No stockout/restock timing
    # involved, so it's unaffected by the witnessed-data issue — the
    # cleanest of the nine.
    # -------------------------------------------------------------------
    {
        "id": "l1_10",
        "name": "Dead Stock",
        "level": "L1",
        "table": "signal_l1_10_dead_stock",
        "unique_on": ["product_id", "snapshot_date"],
        "window_days": 90,
        "min_days": 7,
        "enabled": True,
        "requires": [],
        "sql": """
            -- FIXED 2026-07-21. Two problems, both now closed:
            --
            -- 1. DATING. This previously dated each row by the last day that
            --    product was seen deeply discounted -- while the stock check
            --    (all variants still in stock) always reflects RIGHT NOW. So a
            --    row dated 2026-06-13 paired June's discount with July's stock
            --    levels, and claimed to be a June observation. Confirmed live:
            --    163 rows carried stale historical dates. Rows are now dated
            --    by the day we OBSERVED them, which is what makes them true,
            --    and each run cleanly replaces the previous day rather than
            --    accumulating undeletable stale rows.
            --
            -- 2. BASELINE. honest_discount_pct now derives from
            --    first_observed_price rather than the brand's own RRP (see
            --    khabar_lake.py). Brands publishing no RRP -- lc_waikiki above
            --    all -- were previously unable to appear here at all.
            WITH latest_day AS (
                SELECT max(snapshot_date) AS d FROM snapshots
            ),
            discounted_today AS (
                SELECT s.product_id, s.brand, s.honest_discount_pct, s.snapshot_date
                FROM snapshots s, latest_day l
                WHERE s.snapshot_date = l.d
                  AND s.honest_discount_pct >= 40
            ),
            stock_check AS (
                SELECT product_id, count(*) AS variant_count,
                       count(*) FILTER (WHERE is_in_stock) AS in_stock_count
                FROM variant_baselines
                GROUP BY product_id
            )
            SELECT
                t.product_id,
                t.brand,
                pd.name                AS product_name,
                pd.department,
                pd.category_normalized,
                pd.subcategory,
                t.honest_discount_pct,
                sc.variant_count,
                sc.in_stock_count,
                t.snapshot_date
            FROM discounted_today t
            JOIN products_dim pd ON pd.product_id = t.product_id
            JOIN stock_check sc ON sc.product_id = t.product_id
            WHERE sc.variant_count > 0
              AND sc.in_stock_count = sc.variant_count   -- ALL variants still in stock
        """,
    },

    # -------------------------------------------------------------------
    # #4 — Anchor Inflation. compare_at_price rises while actual price is
    # unchanged. Coverage verified live: solid for 15/19 brands, zero for
    # LC Waikiki/Mobaco/Just SBR (their engines don't capture compare_at_
    # price at all — output correctly shows nothing for them, not a bug),
    # thin for dott_jeans/esla/khotwh (33-66% populated).
    # -------------------------------------------------------------------
    {
        "id": "l1_04",
        "name": "Anchor Inflation",
        "level": "L1",
        "table": "signal_l1_04_anchor_inflation",
        "unique_on": ["product_id", "snapshot_date"],
        "window_days": 90,
        "min_days": 1,
        "enabled": True,
        "requires": [],
        "sql": """
            WITH ordered AS (
                SELECT
                    product_id, brand, recorded_at, price_after, compare_at_price,
                    LAG(compare_at_price) OVER (
                        PARTITION BY product_id ORDER BY recorded_at
                    ) AS prev_compare_at,
                    LAG(price_after) OVER (
                        PARTITION BY product_id ORDER BY recorded_at
                    ) AS prev_price
                FROM price_events_raw
                WHERE compare_at_price IS NOT NULL
                  AND CAST(recorded_at AS DATE) >= CURRENT_DATE - INTERVAL '{window_days} days'
            ),
            ranked AS (
                -- FIXED 2026-07-21: a product can have more than one qualifying
                -- inflation event on the same calendar day (confirmed live: 46
                -- product-days). snapshot_date is a day, not an instant, so the
                -- PK (product_id, snapshot_date) needs exactly one row per day.
                -- Keep the largest inflation of the day; drop the rest.
                SELECT
                    o.product_id,
                    o.brand,
                    pd.name              AS product_name,
                    pd.department,
                    pd.category_normalized,
                    pd.subcategory,
                    o.prev_compare_at,
                    o.compare_at_price   AS new_compare_at,
                    o.price_after        AS actual_price,
                    ROUND(100.0 * (o.compare_at_price - o.prev_compare_at)
                          / NULLIF(o.prev_compare_at, 0), 2) AS anchor_inflation_pct,
                    CAST(o.recorded_at AS DATE) AS snapshot_date,
                    ROW_NUMBER() OVER (
                        PARTITION BY o.product_id, CAST(o.recorded_at AS DATE)
                        ORDER BY (o.compare_at_price - o.prev_compare_at) DESC
                    ) AS rn
                FROM ordered o
                JOIN products_dim pd ON pd.product_id = o.product_id
                WHERE o.prev_compare_at IS NOT NULL
                  AND o.compare_at_price > o.prev_compare_at   -- anchor moved UP
                  AND o.price_after = o.prev_price             -- actual price UNCHANGED
            )
            SELECT product_id, brand, product_name, department, category_normalized,
                   subcategory, prev_compare_at, new_compare_at, actual_price,
                   anchor_inflation_pct, snapshot_date
            FROM ranked
            WHERE rn = 1
        """,
    },

    # -------------------------------------------------------------------
    # #22 — Discount Velocity Anomaly. SKU discount bursts within a 6-hour
    # window. Only fully resolvable for Shopify brands (3 scrapes/day,
    # ~8hr spacing); LC Waikiki's 1x/day cadence can't see sub-day bursts —
    # its rows will simply be sparse, not wrong.
    # -------------------------------------------------------------------
    {
        "id": "l1_22",
        "name": "Discount Velocity Anomaly",
        "level": "L1",
        "table": "signal_l1_22_discount_velocity",
        "unique_on": ["brand", "category_normalized", "subcategory", "window_start"],
        "window_days": 90,
        "min_days": 1,
        "enabled": True,
        "requires": [],
        "sql": """
            WITH drops AS (
                SELECT
                    pe.product_id, pe.brand, pd.department,
                    pd.category_normalized, pd.subcategory,
                    pe.recorded_at,
                    date_trunc('hour', CAST(pe.recorded_at AS TIMESTAMP))
                        - INTERVAL (EXTRACT(hour FROM CAST(pe.recorded_at AS TIMESTAMP))::INT % 6) HOUR
                        AS window_start
                FROM price_events_raw pe
                JOIN products_dim pd ON pd.product_id = pe.product_id
                WHERE pe.direction = 'down'
                  AND CAST(pe.recorded_at AS DATE) >= CURRENT_DATE - INTERVAL '{window_days} days'
            )
            -- FIXED 2026-07-21 (round 1): subcategory was grouped on but not
            -- part of the table's primary key -- crashed the production run
            -- on its first real write. Key widened to include subcategory.
            --
            -- FIXED 2026-07-21 (round 2): department has the IDENTICAL problem
            -- and was still latent here -- 'uncategorized' category pools
            -- products that failed real categorisation, so it can legitimately
            -- span several different departments. This didn't fail this run
            -- only because the categories it happened to touch didn't collide;
            -- l1_24 hit exactly this same shape of bug on 'uncategorized' minutes
            -- earlier. Fix: department is no longer a GROUP BY key, only a
            -- representative value (MIN, so NULL never wins over a real one).
            SELECT
                brand,
                MIN(department) AS department,
                category_normalized,
                COALESCE(subcategory, '(none)') AS subcategory,
                window_start,
                count(DISTINCT product_id) AS skus_dropped,
                CAST(window_start AS DATE) AS snapshot_date
            FROM drops
            GROUP BY brand, category_normalized, COALESCE(subcategory, '(none)'), window_start
            HAVING count(DISTINCT product_id) >= 15   -- burst threshold; tune after first real output
        """,
    },

    # -------------------------------------------------------------------
    # #24 — Restock Density. Variants restocked simultaneously in a window.
    # Reads stockouts_raw, which already excludes the 79% of restocks that
    # aren't witnessed — without that filter this signal would have been
    # counting mostly artifacts. Broken down by category, not just brand —
    # a brand-level-only number can't answer "which category is recovering
    # fastest", which is the actual question this signal exists to answer.
    # -------------------------------------------------------------------
    {
        "id": "l1_24",
        "name": "Restock Density",
        "level": "L1",
        "table": "signal_l1_24_restock_density",
        "unique_on": ["brand", "category_normalized", "subcategory", "color", "restock_date"],
        "window_days": 90,
        "min_days": 1,
        "enabled": True,
        "requires": [],
        "sql": """
            -- FIXED 2026-07-21 (round 1): subcategory/color grouped on but not
            -- in the original key -- crashed on first real write. Key widened.
            --
            -- FIXED 2026-07-21 (round 2): department has the same problem,
            -- confirmed live -- (dalydress, uncategorized, (none), Brown,
            -- 2026-07-19) collided because 'uncategorized' spans real
            -- departments. department is now a representative value (MIN),
            -- not a grouping key. (Also dropped a redundant bare so.color
            -- left in GROUP BY alongside its own COALESCE version.)
            SELECT
                so.brand,
                MIN(pd.department) AS department,
                pd.category_normalized,
                COALESCE(pd.subcategory, '(none)') AS subcategory,
                COALESCE(so.color, '(none)') AS color,
                CAST(so.recorded_at AS DATE) AS restock_date,
                count(*)                     AS variants_restocked,
                count(DISTINCT so.product_id) AS products_affected,
                CAST(so.recorded_at AS DATE) AS snapshot_date
            FROM stockouts_raw so
            JOIN products_dim pd ON pd.product_id = so.product_id
            WHERE so.event_type = 'restock'
              AND so.witnessed = TRUE
              AND CAST(so.recorded_at AS DATE) >= CURRENT_DATE - INTERVAL '{window_days} days'
            GROUP BY so.brand, pd.category_normalized,
                     COALESCE(pd.subcategory, '(none)'), COALESCE(so.color, '(none)'),
                     CAST(so.recorded_at AS DATE)
        """,
    },

    # -------------------------------------------------------------------
    # #11 — Size Asymmetry Stockout. Specific sizes sell out while others
    # don't, during a discount. Uses the witnessed-filtered stockout table.
    # No per-size data for LC Waikiki (lost June 2026, color-level only) —
    # LCW rows simply won't appear here, not an error.
    # -------------------------------------------------------------------
    {
        "id": "l1_11",
        "name": "Size Asymmetry Stockout",
        "level": "L1",
        "table": "signal_l1_11_size_asymmetry",
        "unique_on": ["product_id", "size", "snapshot_date"],
        "window_days": 90,
        "min_days": 1,
        "enabled": True,
        "requires": [],
        "sql": """
            -- FIXED 2026-07-21. Same dating problem as l1_10, and a sharper
            -- version of it: this pairs a HISTORICAL stockout event with
            -- CURRENT stock levels for the other sizes. A stockout from three
            -- weeks ago said nothing reliable about which sizes were in stock
            -- back then -- only about which are in stock now -- yet the row
            -- was dated to the old event, presenting today's stock as history.
            --
            -- Properly reconstructing stock-state-as-of-date would need a
            -- per-day stock history we do not keep, and deriving it from the
            -- event log is unsafe while restock events are only ~21-29%
            -- witnessed. So instead: restrict to a SHORT recent window, where
            -- "other sizes in stock now" is a fair proxy for their state at the
            -- event, and date the row by observation day. Confirmed live: 120
            -- qualifying events in the last 2 days -- comparable per-day volume
            -- to the old 21-day backfill (~45/day), so the signal keeps its
            -- strength while becoming honestly dated. History accrues forward
            -- from here rather than being fabricated backwards.
            WITH so AS (
                SELECT product_id, brand, size, color, was_on_discount,
                       CAST(recorded_at AS DATE) AS event_date
                FROM stockouts_raw
                WHERE event_type = 'stockout'
                  AND witnessed = TRUE
                  AND was_on_discount = TRUE
                  AND size IS NOT NULL
                  AND CAST(recorded_at AS DATE) >= CURRENT_DATE - INTERVAL '2 days'
            ),
            still_stocked AS (
                SELECT product_id, array_agg(DISTINCT size) AS sizes_still_in_stock
                FROM variant_baselines
                WHERE is_in_stock = TRUE AND size IS NOT NULL
                GROUP BY product_id
            ),
            ranked AS (
                SELECT
                    so.product_id,
                    so.brand,
                    pd.name              AS product_name,
                    pd.department,
                    pd.category_normalized,
                    pd.subcategory,
                    so.size              AS stocked_out_size,
                    so.color             AS stocked_out_color,
                    ss.sizes_still_in_stock,
                    so.event_date,
                    CURRENT_DATE         AS snapshot_date,
                    ROW_NUMBER() OVER (
                        PARTITION BY so.product_id, so.size
                        ORDER BY so.event_date DESC, so.color NULLS LAST
                    ) AS rn
                FROM so
                JOIN products_dim pd ON pd.product_id = so.product_id
                LEFT JOIN still_stocked ss ON ss.product_id = so.product_id
                WHERE ss.sizes_still_in_stock IS NOT NULL
                  AND len(ss.sizes_still_in_stock) > 0   -- other sizes genuinely remain
            )
            SELECT product_id, brand, product_name, department, category_normalized,
                   subcategory, stocked_out_size, stocked_out_color,
                   sizes_still_in_stock, event_date, snapshot_date
            FROM ranked
            WHERE rn = 1
        """,
    },

    # -------------------------------------------------------------------
    # #6 — Discount Recovery Pattern. Price drops then returns to a level
    # ABOVE the drop but BELOW first_observed_price. Output will be thin —
    # few products have completed a full markdown-and-recovery cycle in
    # 54 days. Included anyway so it starts accumulating.
    # -------------------------------------------------------------------
    {
        "id": "l1_06",
        "name": "Discount Recovery Pattern",
        "level": "L1",
        "table": "signal_l1_06_discount_recovery",
        "unique_on": ["product_id", "recovery_date"],
        "window_days": 90,
        "min_days": 7,
        "enabled": True,
        "requires": [],
        "sql": """
            WITH seq AS (
                SELECT
                    pe.product_id, pe.brand, pe.price_after, pe.direction,
                    CAST(pe.recorded_at AS DATE) AS event_date,
                    LAG(pe.price_after) OVER (
                        PARTITION BY pe.product_id ORDER BY pe.recorded_at
                    ) AS prev_price,
                    LAG(pe.direction) OVER (
                        PARTITION BY pe.product_id ORDER BY pe.recorded_at
                    ) AS prev_direction
                FROM price_events_raw pe
                WHERE CAST(pe.recorded_at AS DATE) >= CURRENT_DATE - INTERVAL '{window_days} days'
            )
            SELECT
                s.product_id,
                s.brand,
                pd.name                    AS product_name,
                pd.department,
                pd.category_normalized,
                pd.subcategory,
                pd.first_observed_price,
                s.prev_price                AS low_price,
                s.price_after                AS recovered_price,
                ROUND(100.0 * (pd.first_observed_price - s.price_after)
                      / NULLIF(pd.first_observed_price, 0), 2)
                                              AS structural_reprice_pct,
                s.event_date                 AS recovery_date,
                s.event_date                 AS snapshot_date
            FROM seq s
            JOIN products_dim pd ON pd.product_id = s.product_id
            WHERE s.direction = 'up'
              AND s.prev_direction = 'down'
              AND s.price_after > s.prev_price                        -- recovered from the low
              AND s.price_after < pd.first_observed_price              -- but NOT back to full price
        """,
    },

    # -------------------------------------------------------------------
    # #14 — Launch-to-First-Discount Duration. Only the SHORT half of the
    # distribution is currently observable — zero products have reached
    # the 120+ day "held at full price" case yet (max history is 54 days).
    # Values will skew short until more time passes; that's expected, not
    # a bug in the signal.
    # -------------------------------------------------------------------
    {
        "id": "l1_14",
        "name": "Launch-to-First-Discount Duration",
        "level": "L1",
        "table": "signal_l1_14_launch_to_discount",
        "unique_on": ["product_id"],
        "window_days": 90,
        "min_days": 1,
        "enabled": True,
        "requires": [],
        "sql": """
            WITH first_discount AS (
                SELECT product_id, min(CAST(recorded_at AS DATE)) AS first_discount_date
                FROM price_events_raw
                WHERE direction = 'down'
                GROUP BY product_id
            )
            SELECT
                pd.product_id,
                pd.brand,
                pd.name                     AS product_name,
                pd.department,
                pd.category_normalized,
                pd.subcategory,
                CAST(pd.first_seen_at AS DATE) AS launch_date,
                fd.first_discount_date,
                (fd.first_discount_date - CAST(pd.first_seen_at AS DATE)) AS days_to_first_discount,
                fd.first_discount_date       AS snapshot_date
            FROM products_dim pd
            JOIN first_discount fd ON fd.product_id = pd.product_id
            WHERE fd.first_discount_date >= CAST(pd.first_seen_at AS DATE)
              AND CAST(pd.first_seen_at AS DATE) >= CURRENT_DATE - INTERVAL '{window_days} days'
        """,
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
