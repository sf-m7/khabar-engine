"""
Khabar — THE L2 PRODUCT REGISTRY.
================================================================================
Mirrors signals.py's shape deliberately. Where L1 reads the DuckDB lake
(millions of rows, needs materialisation, needs windowing), L2 reads straight
Postgres — the signal_l1_* tables are kilobytes each, so this is plain
psycopg2, no DuckDB, no lake, no egress concern. compute_products.py is the
runner that walks this list, same isolation-per-product philosophy as
compute_signals.py: one broken product must never block the other six.

GRAIN. Every product here outputs at brand + category_normalized at minimum —
never a single number. Where the underlying L1 signal carries size or colour
(l1_11, l1_24), that grain is preserved rather than collapsed away, because
that is specifically what a size/colour dashboard question needs to answer.
A brand- or category-level headline, where one exists, is a column on the
row, never the whole row.

WINDOWING RULE, so every product here is self-consistent:
  - l1_08, l1_09 are ALREADY rolling 30-day aggregates, recomputed fresh each
    day. Using anything but their LATEST snapshot_date would be averaging
    the same 30-day window against itself across multiple days — read only
    the latest row per group.
  - Every other L1 table (01, 03, 04, 06, 10, 11, 12, 13, 14, 17, 22, 24) is
    read across its FULL available history (no date filter). These are
    product-level events/current-states; aggregating over everything
    collected so far is what gives L2 a real sample size at 48 days of
    history, and the sample only grows as more days accumulate — no code
    change needed later, just more rows under the same query.

CAVEATS THAT TRAVEL WITH SPECIFIC PRODUCTS, stated once here rather than
repeated in every docstring below:
  - Anything touching l1_08, l1_09, or l1_11 inherits their known brand
    coverage gap: DeFacto contributes zero witnessed inventory events (it
    delists instead of showing out-of-stock), LC Waikiki contributes almost
    none (colour-grain only, no per-size stock). Products #01, #09, #13, #02
    below must state this in client-facing output, not imply a market-wide
    read on inventory.
  - l1_04 Anchor Inflation currently returns 0 rows (unresolved as of
    2026-07-21 — plausibly a float-equality bug in the L1 SQL, not
    necessarily "no inflation happening"). Product #08 below depends on it
    and will show 0 anchor-inflation events until that's resolved one way
    or the other. Not blocking — the rest of #08 still computes.
"""

PRODUCTS = [

    # =========================================================================
    # #01 — Egyptian Fashion Price Elasticity Index
    # "When brands cut prices in category X, does demand actually respond?"
    # Grain: brand, category_normalized, gender
    # =========================================================================
    {
        "id": "l2_01",
        "name": "Egyptian Fashion Price Elasticity Index",
        "table": "product_l2_01_price_elasticity",
        "unique_on": ["brand", "category_normalized", "gender", "report_date"],
        "requires": ["l1_01", "l1_08"],
        "sql": """
            WITH latest_drop AS (SELECT max(snapshot_date) AS d FROM signal_l1_01_genuine_price_drop),
            drops AS (
                SELECT brand, category_normalized, gender,
                       count(*)                    AS products_with_drops,
                       round(avg(drop_pct)::numeric, 2) AS avg_drop_pct,
                       round(max(drop_pct)::numeric, 2) AS max_drop_pct
                FROM signal_l1_01_genuine_price_drop, latest_drop
                WHERE snapshot_date = latest_drop.d
                GROUP BY brand, category_normalized, gender
            ),
            latest_stockout AS (
                SELECT max(snapshot_date) AS d FROM signal_l1_08_variant_stockout
            ),
            stockouts AS (
                SELECT brand, category_normalized, gender,
                       stockout_events, on_discount_pct, products_affected
                FROM signal_l1_08_variant_stockout, latest_stockout
                WHERE snapshot_date = latest_stockout.d
            )
            SELECT
                d.brand,
                d.category_normalized,
                d.gender,
                d.products_with_drops,
                d.avg_drop_pct,
                d.max_drop_pct,
                s.stockout_events,
                s.products_affected            AS products_stocking_out,
                s.on_discount_pct              AS pct_stockouts_while_discounted,
                CASE
                    WHEN s.on_discount_pct >= 15 THEN 'elastic — discounting is moving inventory'
                    WHEN s.on_discount_pct < 5   THEN 'inelastic — discounting is not triggering sellout'
                    ELSE 'moderate'
                END                             AS elasticity_read,
                CURRENT_DATE                    AS report_date
            FROM drops d
            JOIN stockouts s
              ON s.brand = d.brand AND s.category_normalized = d.category_normalized
             AND s.gender = d.gender
            ORDER BY s.on_discount_pct DESC
        """,
    },

    # =========================================================================
    # #08 — Brand Health Dashboard
    # "Is this brand under pressure, and specifically where?"
    # Grain: brand, category_normalized
    # =========================================================================
    {
        "id": "l2_08",
        "name": "Brand Health Dashboard",
        "table": "product_l2_08_brand_health",
        "unique_on": ["brand", "category_normalized", "report_date"],
        "requires": ["l1_01", "l1_04", "l1_10", "l1_09", "l1_17"],
        "sql": """
            WITH latest_drop AS (SELECT max(snapshot_date) AS d FROM signal_l1_01_genuine_price_drop),
            drops AS (
                SELECT brand, category_normalized,
                       count(*) AS products_dropping,
                       round(avg(drop_pct)::numeric, 1) AS avg_drop_pct
                FROM signal_l1_01_genuine_price_drop, latest_drop
                WHERE snapshot_date = latest_drop.d
                GROUP BY brand, category_normalized
            ),
            anchors AS (
                SELECT brand, category_normalized,
                       count(*) AS anchor_inflation_events
                FROM signal_l1_04_anchor_inflation
                GROUP BY brand, category_normalized
            ),
            latest_dead AS (SELECT max(snapshot_date) AS d FROM signal_l1_10_dead_stock),
            dead AS (
                SELECT brand, category_normalized,
                       count(*) AS dead_stock_products,
                       round(avg(honest_discount_pct)::numeric, 1) AS avg_dead_discount_pct
                FROM signal_l1_10_dead_stock, latest_dead
                WHERE snapshot_date = latest_dead.d
                GROUP BY brand, category_normalized
            ),
            latest_restock AS (SELECT max(snapshot_date) AS d FROM signal_l1_09_variant_restock),
            restocks AS (
                SELECT brand, category_normalized,
                       completion_rate_pct, median_restock_days
                FROM signal_l1_09_variant_restock, latest_restock
                WHERE snapshot_date = latest_restock.d
            ),
            latest_escalation AS (SELECT max(snapshot_date) AS d FROM signal_l1_17_depth_escalation),
            escalations AS (
                SELECT brand, category_normalized,
                       count(*) AS escalating_products
                FROM signal_l1_17_depth_escalation, latest_escalation
                WHERE snapshot_date = latest_escalation.d
                GROUP BY brand, category_normalized
            )
            SELECT
                COALESCE(dead.brand, drops.brand, anchors.brand,
                         restocks.brand, escalations.brand)              AS brand,
                COALESCE(dead.category_normalized, drops.category_normalized,
                         anchors.category_normalized, restocks.category_normalized,
                         escalations.category_normalized)                AS category_normalized,
                COALESCE(drops.products_dropping, 0)                     AS products_dropping,
                drops.avg_drop_pct,
                COALESCE(anchors.anchor_inflation_events, 0)             AS anchor_inflation_events,
                COALESCE(dead.dead_stock_products, 0)                    AS dead_stock_products,
                dead.avg_dead_discount_pct,
                restocks.completion_rate_pct                             AS restock_completion_pct,
                restocks.median_restock_days,
                COALESCE(escalations.escalating_products, 0)             AS escalating_products,
                -- 0-100, simple and legible on purpose: penalise dead stock and
                -- escalating clearance, reward high restock completion. Deliberately
                -- NOT the whole product — this is the headline number the category
                -- breakdown above it explains.
                GREATEST(0, LEAST(100, round((
                    50
                    - COALESCE(dead.dead_stock_products, 0) * 2
                    - COALESCE(escalations.escalating_products, 0) * 3
                    + COALESCE(restocks.completion_rate_pct, 50) * 0.5
                )::numeric)))                                            AS health_score_0_100,
                CURRENT_DATE                                             AS report_date
            FROM dead
            FULL OUTER JOIN drops
                   ON drops.brand = dead.brand AND drops.category_normalized = dead.category_normalized
            FULL OUTER JOIN anchors
                   ON anchors.brand = COALESCE(dead.brand, drops.brand)
                  AND anchors.category_normalized = COALESCE(dead.category_normalized, drops.category_normalized)
            FULL OUTER JOIN restocks
                   ON restocks.brand = COALESCE(dead.brand, drops.brand, anchors.brand)
                  AND restocks.category_normalized = COALESCE(dead.category_normalized, drops.category_normalized, anchors.category_normalized)
            FULL OUTER JOIN escalations
                   ON escalations.brand = COALESCE(dead.brand, drops.brand, anchors.brand, restocks.brand)
                  AND escalations.category_normalized = COALESCE(dead.category_normalized, drops.category_normalized, anchors.category_normalized, restocks.category_normalized)
            ORDER BY health_score_0_100 ASC
        """,
    },

    # =========================================================================
    # #09 — Revealed Consumer Demand Profile
    # "What size/category combinations does the MARKET actually want, proven
    #  by what sells out — not what brands say they're selling."
    # Grain: brand, category_normalized, stocked_out_size
    # COVERAGE: DeFacto and LC Waikiki are structurally under-represented —
    # see module docstring.
    # =========================================================================
    {
        "id": "l2_09",
        "name": "Revealed Consumer Demand Profile",
        "table": "product_l2_09_revealed_demand",
        "unique_on": ["brand", "category_normalized", "stocked_out_size", "report_date"],
        "requires": ["l1_08", "l1_07", "l1_11"],
        "sql": """
            WITH size_demand AS (
                SELECT brand, category_normalized, stocked_out_size,
                       count(*)                       AS stockout_count,
                       count(DISTINCT product_id)      AS products_affected,
                       count(*) FILTER (WHERE array_length(sizes_still_in_stock,1) > 0)
                                                        AS while_other_sizes_available
                FROM signal_l1_11_size_asymmetry
                GROUP BY brand, category_normalized, stocked_out_size
            ),
            latest_stockout AS (SELECT max(snapshot_date) AS d FROM signal_l1_08_variant_stockout),
            category_stockout_context AS (
                SELECT brand, category_normalized, stockout_events, on_discount_pct
                FROM signal_l1_08_variant_stockout, latest_stockout
                WHERE snapshot_date = latest_stockout.d
            ),
            anomalies AS (
                SELECT brand, category_normalized, count(*) AS statistical_anomalies
                FROM signal_l1_07_price_anomaly, (SELECT max(snapshot_date) AS d FROM signal_l1_07_price_anomaly) latest
                WHERE snapshot_date = latest.d
                GROUP BY brand, category_normalized
            )
            SELECT
                sd.brand,
                sd.category_normalized,
                sd.stocked_out_size,
                sd.stockout_count,
                sd.products_affected,
                sd.while_other_sizes_available,
                round(100.0 * sd.while_other_sizes_available / NULLIF(sd.stockout_count,0), 1)
                                                    AS pct_size_specific_demand,
                ctx.stockout_events                AS category_stockout_events,
                ctx.on_discount_pct                AS category_pct_stockouts_while_discounted,
                COALESCE(an.statistical_anomalies, 0) AS category_price_anomalies,
                CURRENT_DATE                        AS report_date
            FROM size_demand sd
            LEFT JOIN category_stockout_context ctx
                   ON ctx.brand = sd.brand AND ctx.category_normalized = sd.category_normalized
            LEFT JOIN anomalies an
                   ON an.brand = sd.brand AND an.category_normalized = sd.category_normalized
            WHERE sd.stockout_count >= 2
            ORDER BY sd.stockout_count DESC
        """,
    },

    # =========================================================================
    # #13 — Egyptian Consumer Wallet Allocator
    # "Which brand/category/size combos are proven winners right now — where
    #  would a buyer's next EGP actually convert."
    # Grain: brand, category_normalized, stocked_out_size
    # COVERAGE: same DeFacto/LCW gap as #09 — inherited via l1_11/l1_08.
    # =========================================================================
    {
        "id": "l2_13",
        "name": "Egyptian Consumer Wallet Allocator",
        "table": "product_l2_13_wallet_allocator",
        "unique_on": ["brand", "category_normalized", "stocked_out_size", "report_date"],
        "requires": ["l1_08", "l1_12", "l1_11"],
        "sql": """
            WITH demand AS (
                SELECT brand, category_normalized, stocked_out_size,
                       count(*) AS stockout_count
                FROM signal_l1_11_size_asymmetry
                GROUP BY brand, category_normalized, stocked_out_size
            ),
            launches AS (
                SELECT brand, category_normalized,
                       count(*)                              AS new_skus_launched,
                       round(avg(launch_price)::numeric, 2)   AS avg_launch_price
                FROM signal_l1_12_new_sku_launch
                GROUP BY brand, category_normalized
            ),
            latest_stockout AS (SELECT max(snapshot_date) AS d FROM signal_l1_08_variant_stockout),
            momentum AS (
                SELECT brand, category_normalized, on_discount_pct
                FROM signal_l1_08_variant_stockout, latest_stockout
                WHERE snapshot_date = latest_stockout.d
            )
            SELECT
                d.brand,
                d.category_normalized,
                d.stocked_out_size,
                d.stockout_count,
                COALESCE(l.new_skus_launched, 0)   AS new_skus_launched_in_category,
                l.avg_launch_price,
                m.on_discount_pct                  AS category_pct_stockouts_while_discounted,
                CASE
                    WHEN d.stockout_count >= 5 AND COALESCE(l.new_skus_launched,0) >= 3
                        THEN 'proven demand, active investment — strong allocation case'
                    WHEN d.stockout_count >= 5
                        THEN 'proven demand, brand under-launching — allocation opportunity'
                    ELSE 'emerging'
                END                                 AS allocation_read,
                CURRENT_DATE                        AS report_date
            FROM demand d
            LEFT JOIN launches l
                   ON l.brand = d.brand AND l.category_normalized = d.category_normalized
            LEFT JOIN momentum m
                   ON m.brand = d.brand AND m.category_normalized = d.category_normalized
            WHERE d.stockout_count >= 2
            ORDER BY d.stockout_count DESC
        """,
    },

    # =========================================================================
    # #10 — Market Entry Intelligence Briefing
    # "Is this brand/category expanding or retreating?"
    # Grain: brand, category_normalized
    # =========================================================================
    {
        "id": "l2_10",
        "name": "Market Entry Intelligence Briefing",
        "table": "product_l2_10_market_entry",
        "unique_on": ["brand", "category_normalized", "report_date"],
        "requires": ["l1_12", "l1_13", "l1_10"],
        "sql": """
            WITH launches AS (
                SELECT brand, category_normalized,
                       count(*)                            AS new_skus,
                       round(avg(launch_price)::numeric, 2) AS avg_launch_price,
                       min(launch_date)                     AS earliest_launch,
                       max(launch_date)                     AS latest_launch
                FROM signal_l1_12_new_sku_launch
                GROUP BY brand, category_normalized
            ),
            delistings AS (
                SELECT brand, category_normalized,
                       count(*) AS skus_delisted
                FROM signal_l1_13_product_delisted
                GROUP BY brand, category_normalized
            ),
            latest_dead AS (SELECT max(snapshot_date) AS d FROM signal_l1_10_dead_stock),
            dead AS (
                SELECT brand, category_normalized, count(*) AS dead_stock_products
                FROM signal_l1_10_dead_stock, latest_dead
                WHERE snapshot_date = latest_dead.d
                GROUP BY brand, category_normalized
            )
            SELECT
                COALESCE(l.brand, del.brand, d.brand)                       AS brand,
                COALESCE(l.category_normalized, del.category_normalized, d.category_normalized)
                                                                              AS category_normalized,
                COALESCE(l.new_skus, 0)              AS new_skus_launched,
                l.avg_launch_price,
                COALESCE(del.skus_delisted, 0)        AS skus_delisted,
                COALESCE(l.new_skus,0) - COALESCE(del.skus_delisted,0)
                                                       AS net_catalogue_change,
                COALESCE(d.dead_stock_products, 0)    AS dead_stock_products,
                CASE
                    WHEN COALESCE(l.new_skus,0) - COALESCE(del.skus_delisted,0) > 5
                        THEN 'expanding'
                    WHEN COALESCE(l.new_skus,0) - COALESCE(del.skus_delisted,0) < -5
                        THEN 'retreating'
                    ELSE 'stable'
                END                                    AS trajectory,
                CURRENT_DATE                            AS report_date
            FROM launches l
            FULL OUTER JOIN delistings del
                   ON del.brand = l.brand AND del.category_normalized = l.category_normalized
            FULL OUTER JOIN dead d
                   ON d.brand = COALESCE(l.brand, del.brand)
                  AND d.category_normalized = COALESCE(l.category_normalized, del.category_normalized)
            ORDER BY net_catalogue_change DESC
        """,
    },

    # =========================================================================
    # #12 — Inventory Distress Liquidation Calendar
    # "Which brand/categories are actively liquidating right now, and how
    #  urgently."
    # Grain: brand, category_normalized
    # =========================================================================
    {
        "id": "l2_12",
        "name": "Inventory Distress Liquidation Calendar",
        "table": "product_l2_12_liquidation_calendar",
        "unique_on": ["brand", "category_normalized", "report_date"],
        "requires": ["l1_17", "l1_10", "l1_03", "l1_22", "l1_24"],
        "sql": """
            WITH latest_escalation AS (SELECT max(snapshot_date) AS d FROM signal_l1_17_depth_escalation),
            escalations AS (
                SELECT brand, category_normalized,
                       count(*) AS escalating_products,
                       round(avg(last_depth_pct)::numeric, 1) AS avg_current_depth_pct
                FROM signal_l1_17_depth_escalation, latest_escalation
                WHERE snapshot_date = latest_escalation.d
                GROUP BY brand, category_normalized
            ),
            latest_dead AS (SELECT max(snapshot_date) AS d FROM signal_l1_10_dead_stock),
            dead AS (
                SELECT brand, category_normalized, count(*) AS dead_stock_products
                FROM signal_l1_10_dead_stock, latest_dead
                WHERE snapshot_date = latest_dead.d
                GROUP BY brand, category_normalized
            ),
            latest_staircase AS (SELECT max(snapshot_date) AS d FROM signal_l1_03_price_staircase),
            staircases AS (
                SELECT brand, category_normalized, count(*) AS staircase_products
                FROM signal_l1_03_price_staircase, latest_staircase
                WHERE snapshot_date = latest_staircase.d
                GROUP BY brand, category_normalized
            ),
            velocity AS (
                SELECT brand, category_normalized, sum(skus_dropped) AS total_skus_in_bursts
                FROM signal_l1_22_discount_velocity
                GROUP BY brand, category_normalized
            ),
            restocking AS (
                SELECT brand, category_normalized, sum(variants_restocked) AS variants_restocked_recently
                FROM signal_l1_24_restock_density
                GROUP BY brand, category_normalized
            )
            SELECT
                COALESCE(e.brand, d.brand, s.brand, v.brand, r.brand)               AS brand,
                COALESCE(e.category_normalized, d.category_normalized, s.category_normalized,
                         v.category_normalized, r.category_normalized)              AS category_normalized,
                COALESCE(e.escalating_products, 0)  AS escalating_products,
                e.avg_current_depth_pct,
                COALESCE(d.dead_stock_products, 0)  AS dead_stock_products,
                COALESCE(s.staircase_products, 0)   AS staircase_products,
                COALESCE(v.total_skus_in_bursts, 0) AS skus_in_discount_bursts,
                COALESCE(r.variants_restocked_recently, 0) AS variants_restocked_recently,
                -- Distress is escalating discounts + dead stock with LOW restock
                -- activity behind it — a category that's both cutting deeper AND
                -- not moving is the urgent one, not just whichever discounts most.
                CASE
                    WHEN COALESCE(e.escalating_products,0) >= 3
                     AND COALESCE(d.dead_stock_products,0) >= 3
                     AND COALESCE(r.variants_restocked_recently,0) < 5
                        THEN 'urgent — deepening discounts, dead stock, no restock activity'
                    WHEN COALESCE(e.escalating_products,0) >= 3
                     OR  COALESCE(d.dead_stock_products,0) >= 5
                        THEN 'watch'
                    ELSE 'normal'
                END                                   AS distress_level,
                CURRENT_DATE                           AS report_date
            FROM escalations e
            FULL OUTER JOIN dead d
                   ON d.brand = e.brand AND d.category_normalized = e.category_normalized
            FULL OUTER JOIN staircases s
                   ON s.brand = COALESCE(e.brand, d.brand)
                  AND s.category_normalized = COALESCE(e.category_normalized, d.category_normalized)
            FULL OUTER JOIN velocity v
                   ON v.brand = COALESCE(e.brand, d.brand, s.brand)
                  AND v.category_normalized = COALESCE(e.category_normalized, d.category_normalized, s.category_normalized)
            FULL OUTER JOIN restocking r
                   ON r.brand = COALESCE(e.brand, d.brand, s.brand, v.brand)
                  AND r.category_normalized = COALESCE(e.category_normalized, d.category_normalized, s.category_normalized, v.category_normalized)
            ORDER BY distress_level, escalating_products DESC
        """,
    },

    # =========================================================================
    # #02 — Factory Production Blueprint
    # "What size/colour mix should actually get produced, based on what sells
    #  and what sits."
    # Grain: brand, category_normalized, stocked_out_size
    # COVERAGE: same DeFacto/LCW gap as #09/#13.
    # =========================================================================
    {
        "id": "l2_02",
        "name": "Factory Production Blueprint",
        "table": "product_l2_02_production_blueprint",
        "unique_on": ["brand", "category_normalized", "stocked_out_size", "report_date"],
        "requires": ["l1_11", "l1_10", "l1_12", "l1_13"],
        "sql": """
            WITH size_demand AS (
                SELECT brand, category_normalized, stocked_out_size,
                       count(*) AS stockout_count
                FROM signal_l1_11_size_asymmetry
                GROUP BY brand, category_normalized, stocked_out_size
            ),
            latest_dead AS (SELECT max(snapshot_date) AS d FROM signal_l1_10_dead_stock),
            oversupply AS (
                SELECT brand, category_normalized, count(*) AS dead_stock_products
                FROM signal_l1_10_dead_stock, latest_dead
                WHERE snapshot_date = latest_dead.d
                GROUP BY brand, category_normalized
            ),
            launches AS (
                SELECT brand, category_normalized, count(*) AS new_skus
                FROM signal_l1_12_new_sku_launch
                GROUP BY brand, category_normalized
            ),
            delists AS (
                SELECT brand, category_normalized, count(*) AS delisted_skus
                FROM signal_l1_13_product_delisted
                GROUP BY brand, category_normalized
            )
            SELECT
                sd.brand,
                sd.category_normalized,
                sd.stocked_out_size,
                sd.stockout_count                       AS undersupply_signal,
                COALESCE(o.dead_stock_products, 0)      AS oversupply_signal_category,
                COALESCE(l.new_skus, 0)                 AS new_skus_launched_category,
                COALESCE(dl.delisted_skus, 0)            AS delisted_skus_category,
                CASE
                    WHEN sd.stockout_count >= 5 AND COALESCE(o.dead_stock_products,0) = 0
                        THEN 'increase production — undersupplied, no offsetting dead stock'
                    WHEN sd.stockout_count >= 5 AND COALESCE(o.dead_stock_products,0) > 0
                        THEN 'rebalance — this size undersupplied while category overstocked elsewhere'
                    ELSE 'monitor'
                END                                       AS production_signal,
                CURRENT_DATE                               AS report_date
            FROM size_demand sd
            LEFT JOIN oversupply o
                   ON o.brand = sd.brand AND o.category_normalized = sd.category_normalized
            LEFT JOIN launches l
                   ON l.brand = sd.brand AND l.category_normalized = sd.category_normalized
            LEFT JOIN delists dl
                   ON dl.brand = sd.brand AND dl.category_normalized = sd.category_normalized
            WHERE sd.stockout_count >= 2
            ORDER BY sd.stockout_count DESC
        """,
    },
]
