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
        pe.price_before, pe.price_after, pe.direction,
        CAST(pe.recorded_at AS TIMESTAMP) AS recorded_at,
        lag(pe.price_after) OVER (
            PARTITION BY pe.product_id ORDER BY pe.recorded_at
        ) AS prev_after
    FROM pg.public.price_events pe
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
LEFT JOIN pg.public.products p ON p.id = a.product_id
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
    FROM pg.public.product_variants
    WHERE first_observed_price IS NOT NULL AND first_observed_price > 0
    GROUP BY product_id
),
ev AS (
    SELECT
        pe.product_id, pe.brand, pe.price_after,
        CAST(pe.recorded_at AS TIMESTAMP) AS recorded_at,
        b.baseline
    FROM pg.public.price_events pe
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
LEFT JOIN pg.public.products p ON p.id = a.product_id
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
]


def blocked_by(signal):
    """Which of this signal's declared blockers are still unresolved."""
    return [b for b in signal.get("requires", [])
            if not BLOCKERS.get(b, {}).get("resolved", False)]


def runnable():
    """Signals that are enabled, unblocked, and actually have SQL to run."""
    return [s for s in SIGNALS
            if s.get("enabled") and not blocked_by(s) and s.get("sql")]
