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
                    count(DISTINCT snapshot_date)                  AS days_observed,
                    median(price)                                  AS median_price,
                    quantile_cont(price, 0.25)                     AS q1,
                    quantile_cont(price, 0.75)                     AS q3,
                    min(price)                                     AS min_price
                FROM snapshots
                WHERE price IS NOT NULL AND price > 0
                GROUP BY product_id
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
                ROUND(t.price, 2)                                   AS current_price,
                ROUND(d.median_price, 2)                            AS median_price,
                ROUND(d.q1, 2)                                      AS q1_price,
                ROUND(d.q3, 2)                                      AS q3_price,
                ROUND(d.q3 - d.q1, 2)                               AS iqr,
                ROUND(d.q1 - 1.5 * (d.q3 - d.q1), 2)                AS lower_fence,
                ROUND(100.0 * (d.median_price - t.price)
                      / NULLIF(d.median_price, 0), 2)               AS deviation_pct,
                (t.price <= d.min_price)                            AS is_lowest_ever,
                d.days_observed,
                {window_days}                                       AS window_days
            FROM today t
            JOIN dist d ON d.product_id = t.product_id
            WHERE d.days_observed >= {min_days}          -- the honesty guard
              AND (d.q3 - d.q1) > 0                      -- flat history = no signal
              AND t.price < d.q1 - 1.5 * (d.q3 - d.q1)   -- the anomaly itself
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
