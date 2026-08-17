"""
report_master_order.py — MASTER REPORT: What to Make & At What Price
================================================================================
Synthesises the Order + Price reports into one action plan. Each row on the
board is a single production decision: what to make, at what price, when.

v3 fixes (Aug 2026):
  - CRITICAL: price queries no longer require snapshot_date = today. They pull
    each product's latest snapshot within the live 8-day window (DISTINCT ON).
    The old filter broke silently whenever the day's scrape was incomplete —
    it read as "no products at any price band" and made every board row look
    like it had zero evidence, which the verdict logic reads as AVOID. That's
    why the board and gap-timing table were empty in the first live run.
  - Added §01 weekly diff (brand launch deltas + price-move patterns).
  - Added §05 production specs (size/colour tabs) — was designed but never
    wired into render().
  - Added §06 price spectrum map — same, designed but not wired in.

Enhancements over the base reports:
  1. Confidence badges per row (brand count × product count × sell-outs)
  2. Bestseller persistence score (avg weeks on lists out of 8)
  3. Demand index (sell-outs + still-out-of-stock = estimated unmet demand)
  4. Speed to sell (median days from launch to first sell-out)
  5. Confidence intervals on sell-through rates (binomial 95% CI)
  6. Newness vs genuine demand flag (persistence distinguishes)
  7. "Observed association" labeling throughout
  8. Restock-adjusted gap ratio per opportunity

All findings are explicitly labeled as observed associations — not causal.
"""

import math
import html as _html
from datetime import date

import report_lib as R
import report_html as H

# ---------------------------------------------------------------------------
#  Constants
# ---------------------------------------------------------------------------

LIVE_WINDOW_DAYS = 8   # price_snapshots retention window

# Per-product latest price within the live window. DISTINCT ON is a Postgres
# idiom: for each product_id, keep only the most recent snapshot_date row.
# This replaces the old `snapshot_date = MAX(snapshot_date)` filter, which
# only captured products scraped on the single most-recent calendar day —
# silently near-empty whenever that day's scrape was still in progress.
LATEST_PRICE_CTE = f"""
    latest_price AS (
        SELECT DISTINCT ON (product_id) product_id, brand, price, snapshot_date
        FROM price_snapshots
        WHERE snapshot_date > CURRENT_DATE - INTERVAL '{LIVE_WINDOW_DAYS} days'
        ORDER BY product_id, snapshot_date DESC
    )
"""

BOARD_SUBCATS = [
    ("t-shirts", "short-sleeve"), ("t-shirts", "graphic-printed"),
    ("t-shirts", "oversized"), ("t-shirts", "basic"),
    ("shirts", "short-sleeve"), ("shirts", "linen"),
    ("shirts", "overshirt"), ("shirts", "long-sleeve"), ("shirts", "formal"),
    ("jeans", "wide-baggy"), ("jeans", "slim"),
    ("trousers", "jogger-style"), ("trousers", "wide-leg"), ("trousers", "cargo"),
    ("trousers", "formal"), ("shorts", "sport"), ("shorts", "denim"),
    ("sweaters", "crewneck"), ("dresses", "mini"),
]

AVOID_CATS = [
    ("trousers", "jogger-style"), ("sweaters", "crewneck"),
    ("dresses", "mini"), ("shorts", "denim"),
]

PRICE_BANDS = [
    ("under_300", "under 300", 0, 300),
    ("300_499",   "300-499",  300, 500),
    ("500_699",   "500-699",  500, 700),
    ("700_999",   "700-999",  700, 1000),
    ("1000p",     "1,000+",   1000, 999999),
]

EXCLUDED_SELL = "('tree','dalydress','defacto')"
EXCLUDED_ALL  = "('tree','dalydress')"


def esc(s):
    return _html.escape(str(s))


def ci95(p, n):
    """Approximate 95% CI half-width for a proportion."""
    if n < 2 or p <= 0 or p >= 1:
        return 0
    return round(1.96 * math.sqrt(p * (1 - p) / n) * 100, 1)


def conf_level(brands, products, sellouts):
    if brands >= 12 and products >= 500 and sellouts >= 200:
        return "hi"
    if brands >= 8 and products >= 200 and sellouts >= 50:
        return "md"
    return "lo"


# ---------------------------------------------------------------------------
#  Data queries
# ---------------------------------------------------------------------------

def q_action_board(conn):
    """Buy board data: stockouts + restocks + brands per subcategory."""
    sql = f"""
    SELECT
        p.category_normalized as cat,
        p.subcategory as sub,
        COUNT(DISTINCT CASE WHEN se.event_type='stockout' THEN se.product_id END) as sellout_products,
        COUNT(CASE WHEN se.event_type='stockout' THEN 1 END) as sellouts,
        COUNT(CASE WHEN se.event_type='restock' THEN 1 END) as restocks,
        COUNT(DISTINCT se.brand) FILTER (WHERE se.event_type='stockout') as brands
    FROM stockout_events se
    JOIN products p ON p.id = se.product_id
    WHERE se.witnessed = true
      AND se.recorded_at > NOW() - INTERVAL '21 days'
      AND se.brand NOT IN {EXCLUDED_SELL}
      AND p.subcategory IS NOT NULL AND p.subcategory != ''
      AND p.category_normalized NOT IN ('uncategorized')
    GROUP BY p.category_normalized, p.subcategory
    HAVING COUNT(CASE WHEN se.event_type='stockout' THEN 1 END) > 20
    ORDER BY sellouts DESC
    """
    return R.df_sql(conn, sql)


def q_bestseller_persistence(conn):
    """Product-level persistence: avg weeks on bestseller lists per subcategory."""
    sql = f"""
    SELECT
        p.category_normalized || ' · ' || p.subcategory as item,
        p.category_normalized as cat, p.subcategory as sub,
        COUNT(DISTINCT br.product_id) as total_products,
        ROUND(AVG(sub.weeks_on), 1) as avg_weeks,
        COUNT(DISTINCT br.product_id) FILTER (WHERE sub.weeks_on >= 6) as strong,
        COUNT(DISTINCT br.product_id) FILTER (WHERE sub.weeks_on >= 3 AND sub.weeks_on < 6) as medium,
        COUNT(DISTINCT br.product_id) FILTER (WHERE sub.weeks_on < 3) as weak
    FROM bestseller_rank br
    JOIN products p ON p.id = br.product_id
    JOIN (
        SELECT product_id, COUNT(DISTINCT DATE_TRUNC('week', snapshot_date)) as weeks_on
        FROM bestseller_rank GROUP BY product_id
    ) sub ON sub.product_id = br.product_id
    WHERE p.subcategory IS NOT NULL AND p.subcategory != ''
      AND br.brand NOT IN {EXCLUDED_ALL}
    GROUP BY p.category_normalized || ' · ' || p.subcategory,
             p.category_normalized, p.subcategory
    HAVING COUNT(DISTINCT br.product_id) > 5
    """
    return R.df_sql(conn, sql)


def q_bestseller_trend(conn):
    """Weekly bestseller counts for sparklines."""
    sql = f"""
    SELECT
        DATE_TRUNC('week', br.snapshot_date)::date as week,
        p.category_normalized as cat, p.subcategory as sub,
        COUNT(DISTINCT br.product_id) as bs_count
    FROM bestseller_rank br
    JOIN products p ON p.id = br.product_id
    WHERE p.subcategory IS NOT NULL AND p.subcategory != ''
      AND br.brand NOT IN {EXCLUDED_ALL}
    GROUP BY DATE_TRUNC('week', br.snapshot_date)::date,
             p.category_normalized, p.subcategory
    ORDER BY cat, sub, week
    """
    return R.df_sql(conn, sql)


def q_price_band_sellthrough(conn):
    """Sell-through rate by price band per subcategory.

    FIXED: was `ps.snapshot_date = MAX(snapshot_date)`, which only matched
    products scraped on the single most-recent day. Now uses each product's
    latest snapshot within the live 8-day window.
    """
    bands_sql = " ".join([
        f"WHEN lp.price >= {lo} AND lp.price < {hi} THEN '{key}'"
        for key, _, lo, hi in PRICE_BANDS
    ])
    sql = f"""
    WITH {LATEST_PRICE_CTE}
    SELECT
        p.category_normalized as cat, p.subcategory as sub,
        CASE {bands_sql} END as price_band,
        COUNT(DISTINCT lp.product_id) as products,
        COUNT(DISTINCT se.product_id) as with_sellout
    FROM latest_price lp
    JOIN products p ON p.id = lp.product_id
    LEFT JOIN stockout_events se ON se.product_id = lp.product_id
        AND se.event_type = 'stockout' AND se.witnessed = true
        AND se.recorded_at > NOW() - INTERVAL '21 days'
    WHERE p.subcategory IS NOT NULL AND p.subcategory != ''
      AND p.category_normalized NOT IN ('uncategorized')
      AND lp.brand NOT IN {EXCLUDED_SELL}
    GROUP BY p.category_normalized, p.subcategory,
             CASE {bands_sql} END
    HAVING COUNT(DISTINCT lp.product_id) > 15
    """
    return R.df_sql(conn, sql)


def q_speed_to_sell(conn):
    """Median days from launch to first sell-out per subcategory."""
    sql = f"""
    SELECT
        p.category_normalized as cat, p.subcategory as sub,
        COUNT(DISTINCT p.id) as products,
        PERCENTILE_CONT(0.5) WITHIN GROUP (
            ORDER BY EXTRACT(DAY FROM se.min_so - p.first_seen_at)
        ) as median_days
    FROM products p
    JOIN (
        SELECT product_id, MIN(recorded_at) as min_so
        FROM stockout_events
        WHERE event_type = 'stockout' AND witnessed = true
        GROUP BY product_id
    ) se ON se.product_id = p.id
    WHERE p.subcategory IS NOT NULL AND p.subcategory != ''
      AND p.brand NOT IN {EXCLUDED_SELL}
      AND p.category_normalized NOT IN ('uncategorized')
    GROUP BY p.category_normalized, p.subcategory
    HAVING COUNT(DISTINCT p.id) > 15
    """
    return R.df_sql(conn, sql)


def q_demand_index(conn):
    """Sell-outs + products still out of stock = unmet demand proxy."""
    sql = f"""
    SELECT
        p.category_normalized as cat, p.subcategory as sub,
        COUNT(DISTINCT se_out.product_id) as sold_out,
        COUNT(DISTINCT se_out.product_id) FILTER (WHERE se_in.product_id IS NULL) as still_out
    FROM (
        SELECT product_id FROM stockout_events
        WHERE event_type = 'stockout' AND witnessed = true
          AND recorded_at > NOW() - INTERVAL '21 days'
          AND brand NOT IN {EXCLUDED_SELL}
        GROUP BY product_id
    ) se_out
    JOIN products p ON p.id = se_out.product_id
    LEFT JOIN (
        SELECT DISTINCT product_id FROM stockout_events
        WHERE event_type = 'restock' AND witnessed = true
          AND recorded_at > NOW() - INTERVAL '21 days'
    ) se_in ON se_in.product_id = se_out.product_id
    WHERE p.subcategory IS NOT NULL AND p.subcategory != ''
      AND p.category_normalized NOT IN ('uncategorized')
    GROUP BY p.category_normalized, p.subcategory
    """
    return R.df_sql(conn, sql)


def q_brand_stockouts(conn):
    """Top brands running out per subcategory."""
    sql = f"""
    SELECT
        p.category_normalized as cat, p.subcategory as sub,
        se.brand, COUNT(*) as stockouts
    FROM stockout_events se
    JOIN products p ON p.id = se.product_id
    WHERE se.event_type = 'stockout' AND se.witnessed = true
      AND se.recorded_at > NOW() - INTERVAL '21 days'
      AND se.brand NOT IN {EXCLUDED_SELL}
      AND p.subcategory IS NOT NULL AND p.subcategory != ''
    GROUP BY p.category_normalized, p.subcategory, se.brand
    ORDER BY cat, sub, stockouts DESC
    """
    return R.df_sql(conn, sql)


def q_confidence_inputs(conn):
    """Brand count × product count per subcategory for confidence scoring.

    FIXED: same latest-snapshot bug as q_price_band_sellthrough.
    """
    sql = f"""
    WITH {LATEST_PRICE_CTE}
    SELECT
        p.category_normalized as cat, p.subcategory as sub,
        COUNT(DISTINCT lp.brand) as brands,
        COUNT(DISTINCT lp.product_id) as products,
        COUNT(DISTINCT se.product_id) FILTER (
            WHERE se.event_type = 'stockout' AND se.witnessed = true
        ) as with_sellout
    FROM latest_price lp
    JOIN products p ON p.id = lp.product_id
    LEFT JOIN stockout_events se ON se.product_id = lp.product_id
        AND se.recorded_at > NOW() - INTERVAL '21 days'
    WHERE lp.brand NOT IN {EXCLUDED_SELL}
      AND p.subcategory IS NOT NULL AND p.subcategory != ''
      AND p.category_normalized NOT IN ('uncategorized')
    GROUP BY p.category_normalized, p.subcategory
    """
    return R.df_sql(conn, sql)


def q_brand_prices(conn):
    """Median price per brand × subcategory for spectrum strips.

    FIXED: same latest-snapshot bug.
    """
    sql = f"""
    WITH {LATEST_PRICE_CTE}
    SELECT
        p.category_normalized as cat, p.subcategory as sub,
        lp.brand,
        COUNT(DISTINCT lp.product_id) as products,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY lp.price) as median_price
    FROM latest_price lp
    JOIN products p ON p.id = lp.product_id
    WHERE p.subcategory IS NOT NULL AND p.subcategory != ''
      AND lp.brand NOT IN {EXCLUDED_ALL}
    GROUP BY p.category_normalized, p.subcategory, lp.brand
    HAVING COUNT(DISTINCT lp.product_id) > 5
    """
    return R.df_sql(conn, sql)


def q_launches_by_brand(conn):
    """New launches this week vs last week, by brand — for the weekly diff."""
    sql = f"""
    SELECT
        brand,
        COUNT(CASE WHEN first_seen_at >= NOW() - INTERVAL '7 days' THEN 1 END) as this_week,
        COUNT(CASE WHEN first_seen_at >= NOW() - INTERVAL '14 days'
                    AND first_seen_at < NOW() - INTERVAL '7 days' THEN 1 END) as last_week
    FROM products
    WHERE brand NOT IN {EXCLUDED_ALL}
      AND first_seen_at >= NOW() - INTERVAL '14 days'
    GROUP BY brand
    HAVING COUNT(CASE WHEN first_seen_at >= NOW() - INTERVAL '7 days' THEN 1 END) > 10
        OR COUNT(CASE WHEN first_seen_at >= NOW() - INTERVAL '14 days'
                       AND first_seen_at < NOW() - INTERVAL '7 days' THEN 1 END) > 10
    ORDER BY this_week DESC
    """
    return R.df_sql(conn, sql)


def q_price_movements(conn):
    """Price change summary by brand × category this week — for the weekly diff."""
    sql = f"""
    SELECT
        pe.brand,
        p.category_normalized as cat,
        COUNT(*) as changes,
        COUNT(*) FILTER (WHERE pe.direction = 'up') as increases,
        COUNT(*) FILTER (WHERE pe.direction = 'down') as decreases,
        ROUND(AVG(CASE WHEN pe.direction='down'
              THEN 100.0*(1 - pe.price_after/pe.price_before) END), 1) as avg_cut_pct
    FROM price_events pe
    JOIN products p ON p.id = pe.product_id
    WHERE pe.recorded_at > NOW() - INTERVAL '7 days'
      AND pe.brand NOT IN {EXCLUDED_ALL}
      AND p.category_normalized NOT IN ('uncategorized')
    GROUP BY pe.brand, p.category_normalized
    HAVING COUNT(*) > 50
    ORDER BY changes DESC
    """
    return R.df_sql(conn, sql)


def q_size_ratios(conn, cat, sub):
    """Size sell-out ratios for a specific subcategory."""
    sql = f"""
    SELECT se.size, COUNT(*) as stockouts
    FROM stockout_events se
    JOIN products p ON p.id = se.product_id
    WHERE se.event_type = 'stockout' AND se.witnessed = true
      AND se.recorded_at > NOW() - INTERVAL '21 days'
      AND p.category_normalized = '{cat}' AND p.subcategory = '{sub}'
      AND se.brand NOT IN {EXCLUDED_SELL}
      AND se.size IS NOT NULL
    GROUP BY se.size
    ORDER BY stockouts DESC
    LIMIT 6
    """
    return R.df_sql(conn, sql)


def q_color_ratios(conn, cat, sub):
    """Colour sell-out ratios for a specific subcategory."""
    sql = f"""
    SELECT COALESCE(pv.color_family, pv.color) as color, COUNT(*) as stockouts
    FROM stockout_events se
    JOIN products p ON p.id = se.product_id
    JOIN product_variants pv ON pv.id = se.variant_id
    WHERE se.event_type = 'stockout' AND se.witnessed = true
      AND se.recorded_at > NOW() - INTERVAL '21 days'
      AND p.category_normalized = '{cat}' AND p.subcategory = '{sub}'
      AND se.brand NOT IN {EXCLUDED_SELL}
    GROUP BY COALESCE(pv.color_family, pv.color)
    ORDER BY stockouts DESC
    LIMIT 5
    """
    return R.df_sql(conn, sql)


def q_distress(conn):
    """Discount escalation data for AVOID section."""
    sql = f"""
    SELECT
        p.category_normalized as cat, p.subcategory as sub,
        de.brand,
        COUNT(*) as escalating,
        ROUND(AVG(de.last_depth_pct), 1) as avg_depth
    FROM signal_l1_17_depth_escalation de
    JOIN products p ON p.id = de.product_id
    WHERE de.snapshot_date >= CURRENT_DATE - 21
      AND p.subcategory IS NOT NULL AND p.subcategory != ''
      AND de.brand NOT IN {EXCLUDED_ALL}
    GROUP BY p.category_normalized, p.subcategory, de.brand
    HAVING COUNT(*) > 5
    """
    return R.df_sql(conn, sql)


# ---------------------------------------------------------------------------
#  Board assembly
# ---------------------------------------------------------------------------

def build_board(conn):
    """Pull all data and assemble the action board rows."""
    ab = q_action_board(conn)
    bp = q_bestseller_persistence(conn)
    bt = q_bestseller_trend(conn)
    pb = q_price_band_sellthrough(conn)
    sp = q_speed_to_sell(conn)
    di = q_demand_index(conn)
    bs = q_brand_stockouts(conn)
    ci = q_confidence_inputs(conn)

    rows = []
    for _, r in ab.iterrows():
        cat, sub = r["cat"], r["sub"]
        key = f"{cat} · {sub}"

        pers = bp[bp["item"] == key]
        avg_wk = float(pers.iloc[0]["avg_weeks"]) if not pers.empty else 0
        strong = int(pers.iloc[0]["strong"]) if not pers.empty else 0
        medium = int(pers.iloc[0]["medium"]) if not pers.empty else 0
        weak   = int(pers.iloc[0]["weak"])   if not pers.empty else 0

        trend = bt[(bt["cat"] == cat) & (bt["sub"] == sub)].sort_values("week")
        spark_vals = list(trend["bs_count"]) if not trend.empty else []
        if spark_vals and spark_vals[0] > 0:
            trend_pct = round(100 * (spark_vals[-1] - spark_vals[0]) / spark_vals[0])
        else:
            trend_pct = 0

        pbd = pb[(pb["cat"] == cat) & (pb["sub"] == sub)]
        best_band, best_rate, best_ci, best_n = None, 0, 0, 0
        for _, pr in pbd.iterrows():
            if pr["products"] > 0:
                rate = round(100 * pr["with_sellout"] / pr["products"], 1)
                if rate > best_rate:
                    best_rate = rate
                    best_band = pr["price_band"]
                    best_n = int(pr["products"])
                    best_ci = ci95(rate/100, best_n)

        spd = sp[(sp["cat"] == cat) & (sp["sub"] == sub)]
        med_days = int(spd.iloc[0]["median_days"]) if not spd.empty else None

        did = di[(di["cat"] == cat) & (di["sub"] == sub)]
        sold_out = int(did.iloc[0]["sold_out"]) if not did.empty else 0
        still_out = int(did.iloc[0]["still_out"]) if not did.empty else 0

        sellouts = int(r["sellouts"])
        restocks = int(r["restocks"])
        gap_ratio = round(sellouts / max(restocks, 1), 1)

        brds = bs[(bs["cat"] == cat) & (bs["sub"] == sub)].head(6)
        brand_list = list(brds["brand"])

        cid = ci[(ci["cat"] == cat) & (ci["sub"] == sub)]
        if not cid.empty:
            n_brands = int(cid.iloc[0]["brands"])
            n_products = int(cid.iloc[0]["products"])
            n_sellout = int(cid.iloc[0]["with_sellout"])
        else:
            n_brands, n_products, n_sellout = int(r["brands"]), 0, 0
        conf = conf_level(n_brands, n_products, n_sellout)

        band_labels = {k: l for k, l, _, _ in PRICE_BANDS}
        price_label = band_labels.get(best_band, "—")

        rows.append({
            "cat": cat, "sub": sub, "key": key,
            "sellouts": sellouts, "restocks": restocks, "gap_ratio": gap_ratio,
            "brands": n_brands, "products": n_products, "conf": conf,
            "avg_weeks": avg_wk, "strong": strong, "medium": medium, "weak": weak,
            "spark_vals": spark_vals, "trend_pct": trend_pct,
            "price_label": price_label, "best_band": best_band,
            "best_rate": best_rate, "best_ci": best_ci, "best_n": best_n,
            "med_days": med_days, "sold_out": sold_out, "still_out": still_out,
            "brand_list": brand_list,
        })

    return rows


def assign_verdict(row):
    """Verdict logic — FIXED (Aug 2026).

    Bug found in production: the old "go" fallback path only checked
    bestseller persistence + sell-out/restock gap ratio, with no re-check
    of how strong the actual sell-through evidence was (just a near-useless
    best_rate < 10 floor at the top) and no confidence check at all. That
    let thin, off-season, niche subcategories reach "Make now" purely
    because a small bestseller pool inflated persistence and near-zero
    restocking inflated the gap ratio — even when every price band showed
    6-14% sell-through with confidence intervals nearly touching zero.
    Verified live: jackets·puffer sells 6-14% at EVERY price band (n=48 at
    its "best" band) yet reached "go" under the old logic. The stock report
    correctly flagged the same category STOP, because its verdict logic
    requires all available depth-rates to clear a real floor.

    Fix: persistence and gap ratio are now supporting evidence, not a
    substitute for actual sell-through evidence. "Go" requires a materially
    higher rate (>=20%) AND confidence that isn't "lo". "Buy" requires
    >=15% AND confidence that isn't "lo". The initial avoid floor is raised
    from 10% to 15% to match the level where genuinely weak categories
    cluster (6-14%) vs where legitimate ones sit (18%+).
    """
    cat, sub = row["cat"], row["sub"]
    if (cat, sub) in AVOID_CATS:
        return "avoid", "Don't make"
    if row["best_rate"] < 15:
        return "avoid", "Don't make"

    strong_evidence = row["best_rate"] >= 20 and row["conf"] != "lo"
    ok_evidence = row["best_rate"] >= 15 and row["conf"] != "lo"

    if (strong_evidence and row["avg_weeks"] >= 6.5 and row["trend_pct"] >= 15
            and row["gap_ratio"] >= 2.0 and row["conf"] == "hi"):
        return "go", "Make now"
    if strong_evidence and row["avg_weeks"] >= 6.5 and row["gap_ratio"] >= 2.0:
        return "go", "Make now"
    if ok_evidence and row["avg_weeks"] >= 5.0 and row["trend_pct"] >= -10 and row["gap_ratio"] >= 1.5:
        return "buy", "Make — mid vol"
    if ok_evidence and row["avg_weeks"] >= 4.0 and row["gap_ratio"] >= 1.2:
        return "buy", "Make — mid vol"
    if row["avg_weeks"] >= 3.0:
        return "watch", "Watch"
    return "watch", "Watch"


# ---------------------------------------------------------------------------
#  HTML rendering
# ---------------------------------------------------------------------------

MASTER_CSS = """
:root{--paper:#FAFAF8;--ink:#1B1B19;--muted:#6C6A64;--faint:#9A978F;
--line:#DAD7CF;--box:#EFEDE7;--grid:#E6E3DB;
--act:#B45309;--act-bg:#FBF1E3;--good:#3F7A4B;--good-bg:#EDF5EE;
--warn:#B4820A;--warn-bg:#FDF6E3;--bad:#B0413A;--bad-bg:#FBEEED;
--blue:#2563EB;--blue-bg:#EFF4FF;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
*{box-sizing:border-box;margin:0}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);line-height:1.6;font-size:15px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1020px;margin:0 auto;padding:0 20px 80px}
header{border-bottom:1px solid var(--line);background:var(--paper);position:sticky;top:0;z-index:10}
.bar{max-width:1020px;margin:0 auto;padding:12px 20px;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.bar h1{font-size:16px;font-weight:700}.bar .tag{font-family:var(--mono);font-size:10px;color:#fff;background:var(--good);border-radius:3px;padding:2px 6px;font-weight:600}
.bar .date{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-left:auto}
.verdict{border:1px solid var(--good);background:var(--good-bg);border-radius:8px;padding:18px 20px;margin:20px 0 8px}
.verdict .lbl{font-family:var(--mono);font-size:10px;letter-spacing:.1em;color:var(--good);text-transform:uppercase;font-weight:700}
.verdict .v{font-size:17px;font-weight:700;margin:8px 0 6px;line-height:1.45}.verdict .sub{font-size:13.5px;color:var(--muted);line-height:1.55}
section.blk{border:1px solid var(--line);border-radius:8px;background:#fff;margin:16px 0;overflow:hidden}
.blk>.hd{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--box);flex-wrap:wrap}
.blk>.hd .n{font-family:var(--mono);font-size:10.5px;color:var(--faint);flex-shrink:0}
.blk>.hd .t{font-size:13.5px;font-weight:650}
.blk>.hd .badge{margin-left:auto;font-family:var(--mono);font-size:9.5px;padding:2px 8px;border-radius:20px;color:#fff;font-weight:600;flex-shrink:0}
.blk>.hd .badge.key{background:var(--good)}.blk>.hd .badge.new{background:var(--blue)}
.blk>.bd{padding:16px 18px}
.note{font-size:13px;color:var(--muted);line-height:1.6;margin:14px 0 0;padding:10px 12px;background:var(--paper);border-radius:5px;border-left:3px solid var(--grid)}
.note b{color:var(--ink);font-weight:600}
.intro{font-size:13.5px;color:var(--muted);line-height:1.55;margin-bottom:14px}
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -18px;padding:0 18px}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:860px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--grid);vertical-align:top}
th{font-family:var(--mono);font-size:9.5px;letter-spacing:.04em;color:var(--faint);text-transform:uppercase;position:sticky;top:0;background:#fff}
td.m{font-family:var(--mono);font-size:12px}tr:last-child td{border-bottom:none}
.pill{font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:20px;color:#fff;display:inline-block;font-weight:700;white-space:nowrap}
.pill.go{background:var(--good)}.pill.buy{background:var(--act)}.pill.watch{background:var(--faint)}.pill.avoid{background:var(--bad)}
.b{font-family:var(--mono);font-size:10px;display:inline-block;background:var(--box);border-radius:3px;padding:1px 6px;margin:1px 2px;color:var(--ink);white-space:nowrap}
.b.top{background:var(--act-bg);border:1px solid #E8C98E;color:var(--act);font-weight:700}
.tier{font-family:var(--mono);font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;font-weight:600;padding:10px 10px 5px;color:var(--faint);border-top:1px solid var(--line);background:var(--paper)}
.timing{font-family:var(--mono);font-size:10.5px;font-weight:700;white-space:nowrap}
.timing.go{color:var(--good)}.timing.soon{color:var(--act)}.timing.slow{color:var(--bad)}
.spark{display:inline-flex;align-items:end;gap:1px;height:16px;vertical-align:middle;margin:0 4px}
.spark i{width:4px;border-radius:1px;background:var(--act);opacity:.7}
.price-pill{font-family:var(--mono);font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:4px;display:inline-block;white-space:nowrap;background:var(--good-bg);color:var(--good);border:1px solid #A8D5AD}
.conf{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:3px;vertical-align:middle}
.conf.hi{background:var(--good)}.conf.md{background:var(--warn)}.conf.lo{background:var(--bad)}
.ci{font-family:var(--mono);font-size:9px;color:var(--faint);font-weight:400}
.speed{font-family:var(--mono);font-size:9.5px;color:var(--faint);display:block;margin-top:2px}
.avoid-item{display:flex;gap:10px;padding:12px 0;border-bottom:1px solid var(--grid);align-items:flex-start}
.avoid-item:last-child{border-bottom:none}
.avoid-x{font-family:var(--mono);font-size:14px;color:var(--bad);font-weight:800;flex-shrink:0;width:20px;text-align:center;margin-top:1px}
.avoid-body{font-size:13.5px;line-height:1.55}.avoid-body strong{font-weight:650}.avoid-body .reason{color:var(--muted)}
.cov{font-family:var(--mono);font-size:11px;color:var(--muted);line-height:2}
.cov b{color:var(--ink);font-weight:600}.cov .x{color:var(--bad)}.cov span{display:inline-block;margin-right:18px}
.diff-box{background:var(--blue-bg);border:1px solid #C7D9F7;border-radius:6px;padding:14px 16px}
.diff-hd{font-family:var(--mono);font-size:10px;font-weight:700;color:var(--blue);text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px}
.diff-row{display:flex;gap:8px;align-items:flex-start;padding:5px 0}
.diff-row+.diff-row{border-top:1px solid #dde6f7}
.diff-arrow{font-family:var(--mono);font-size:13px;width:18px;text-align:center;flex-shrink:0}
.diff-txt{font-size:13.5px;line-height:1.5}
.tabs{background:var(--paper);border:1px solid var(--grid);border-radius:6px;padding:14px 16px}
.tab-nav{display:flex;gap:6px;border-bottom:1px solid var(--grid);padding-bottom:10px;margin-bottom:14px;flex-wrap:wrap}
.tab-btn{font-family:var(--mono);font-size:10.5px;font-weight:600;padding:4px 10px;border-radius:4px;border:1px solid var(--grid);background:#fff;color:var(--muted);cursor:pointer}
.tab-btn.active{background:var(--ink);color:#fff;border-color:var(--ink)}
.tab-panel{display:none}.tab-panel.active{display:block}
.spec-grid{display:flex;gap:24px;flex-wrap:wrap}
.spec-col{flex:1;min-width:240px}
.spec-title{font-family:var(--mono);font-size:10px;font-weight:600;color:var(--faint);text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px}
.bar-row{display:flex;align-items:center;gap:8px;margin:5px 0}
.bar-lbl{font-family:var(--mono);font-size:10.5px;color:var(--muted);width:44px;text-align:right;flex-shrink:0}
.bar-track{flex:1;height:16px;background:var(--grid);border-radius:3px;position:relative;overflow:hidden}
.bar-fill{position:absolute;left:0;top:0;bottom:0;border-radius:3px}
.bar-val{position:absolute;right:5px;top:0;font-family:var(--mono);font-size:9.5px;line-height:16px;color:var(--ink);font-weight:600}
.sp-block{margin:0 0 22px}.sp-block:last-child{margin-bottom:0}
.sp-name{font-size:13px;font-weight:650;margin-bottom:4px}
.sp-name span{font-weight:400;color:var(--faint);font-size:11px;margin-left:6px}
.sp-axis{position:relative;height:40px;background:var(--paper);border:1px solid var(--grid);border-radius:4px;margin:4px 0 2px}
.sp-zone{position:absolute;top:0;bottom:0;border-radius:3px}
.sp-zone.peak{background:rgba(63,122,75,.12);border:1px dashed rgba(63,122,75,.35)}
.sp-dot{position:absolute;top:11px;height:18px;font-family:var(--mono);font-size:9px;background:var(--box);border:1px solid var(--line);border-radius:3px;padding:0 4px;line-height:16px;white-space:nowrap;transform:translateX(-50%);z-index:2}
.sp-zone-lbl{position:absolute;top:1px;font-family:var(--mono);font-size:7.5px;letter-spacing:.03em;text-transform:uppercase;font-weight:600;padding:0 4px}
.sp-zone-lbl.pk{color:var(--good);right:3px}
.sp-ticks{display:flex;justify-content:space-between;font-family:var(--mono);font-size:8.5px;color:var(--faint);padding:0 1px}
@media(max-width:700px){body{font-size:14px}.wrap{padding:0 14px 60px}.verdict{padding:14px 16px}.verdict .v{font-size:15.5px}.blk>.bd{padding:14px}.table-wrap{margin:0 -14px;padding:0 14px}.note{font-size:12.5px}.cov span{display:block;margin-right:0}.spec-grid{flex-direction:column;gap:14px}}
@media(max-width:450px){.bar .date{margin-left:0;flex-basis:100%}.verdict .v{font-size:14.5px}table{font-size:12px}th,td{padding:7px 6px}.b{font-size:9px;padding:1px 4px}}
"""


def render_sparkline(vals):
    if not vals:
        return ""
    mx = max(vals) or 1
    bars = "".join(f'<i style="height:{int(v/mx*100)}%"></i>' for v in vals)
    return f'<span class="spark">{bars}</span>'


def render_board_row(row):
    verdict, label = assign_verdict(row)
    if verdict == "avoid":
        return ""

    if row["gap_ratio"] >= 2.5 and row["trend_pct"] >= 0:
        timing_cls, timing_txt = "go", "Now"
    elif row["gap_ratio"] >= 1.5:
        timing_cls, timing_txt = "soon", "Soon"
    else:
        timing_cls, timing_txt = "slow", "Slowing"

    top_brands = row["brand_list"][:3]
    rest = row["brand_list"][3:]
    brands_html = "".join(f'<span class="b top">{esc(b)}</span>' for b in top_brands)
    if rest:
        brands_html += f'<span class="b">+{len(rest)}</span>'

    tp = row["trend_pct"]
    color = "var(--good)" if tp >= 15 else "var(--bad)" if tp <= -15 else "var(--faint)"
    sign = "+" if tp >= 0 else ""
    trend_html = f'<span style="font-family:var(--mono);font-size:10px;color:{color};">{sign}{tp}%</span>'

    speed_txt = f"{row['med_days']} days to sell" if row["med_days"] else "—"

    return f"""<tr>
  <td>{esc(row['cat'])} · <b>{esc(row['sub'])}</b><br>
    <span class="speed"><span class="conf {row['conf']}"></span> {row['brands']} brands · {row['products']:,} products</span></td>
  <td><span class="pill {verdict}">{esc(label)}</span></td>
  <td><span class="price-pill">{esc(row['price_label'])}</span><br>
    <span class="ci">{row['best_rate']}% ±{row['best_ci']} sell-through</span></td>
  <td>{render_sparkline(row['spark_vals'])}{trend_html}<br>
    <span class="speed">Persistence: {row['avg_weeks']}/8 · {row['strong']} strong, {row['medium']} med, {row['weak']} weak</span>
    <span class="speed">Demand: {row['sold_out']} sold out · {row['still_out']} still out</span>
    <span class="speed">Speed: {speed_txt}</span></td>
  <td><span class="timing {timing_cls}">{timing_txt}</span><br>
    <span class="speed">Gap {row['gap_ratio']}×</span></td>
  <td>{brands_html}</td>
</tr>"""


def render_weekly_diff(conn):
    """§01 — data-driven weekly diff from brand launches + price movements.

    Note: this tracks market signals (who launched, who moved prices), not
    board-state changes (e.g. "formal shirts moved from WATCH to BUY").
    Board-state diffing needs a stored snapshot of last week's board, which
    doesn't exist yet — that's a separate follow-up, not part of this fix.
    """
    launches = q_launches_by_brand(conn)
    moves = q_price_movements(conn)

    rows_html = ""
    count = 0

    # Biggest launch swings (up)
    for _, r in launches.iterrows():
        if count >= 3:
            break
        tw, lw = int(r["this_week"]), int(r["last_week"])
        if lw > 0 and tw >= lw * 2 and tw >= 30:
            rows_html += (f'<div class="diff-row"><span class="diff-arrow" style="color:var(--good);">▲</span>'
                          f'<div class="diff-txt"><b>{esc(r["brand"])} ramped up launches</b> — '
                          f'{tw:,} new products this week, up from {lw:,}.</div></div>')
            count += 1
        elif lw == 0 and tw >= 30:
            rows_html += (f'<div class="diff-row"><span class="diff-arrow" style="color:var(--good);">▲</span>'
                          f'<div class="diff-txt"><b>{esc(r["brand"])} returned to launching</b> — '
                          f'{tw:,} new products after little to no activity last week.</div></div>')
            count += 1

    # Biggest launch drops
    for _, r in launches.iterrows():
        if count >= 5:
            break
        tw, lw = int(r["this_week"]), int(r["last_week"])
        if lw >= 30 and tw <= lw * 0.4:
            rows_html += (f'<div class="diff-row"><span class="diff-arrow" style="color:var(--bad);">▼</span>'
                          f'<div class="diff-txt"><b>{esc(r["brand"])} slowed sharply</b> — '
                          f'{tw:,} launches this week, down from {lw:,}.</div></div>')
            count += 1

    # Price movement patterns — all-increase or all-decrease signals
    for _, r in moves.iterrows():
        if count >= 7:
            break
        inc, dec, chg = int(r["increases"]), int(r["decreases"]), int(r["changes"])
        if inc >= chg * 0.9 and inc >= 50:
            rows_html += (f'<div class="diff-row"><span class="diff-arrow" style="color:var(--good);">▲</span>'
                          f'<div class="diff-txt"><b>{esc(r["brand"])} raised {esc(r["cat"])} prices</b> — '
                          f'{inc} increases, {dec} cuts. Confidence signal.</div></div>')
            count += 1
        elif dec >= chg * 0.9 and dec >= 50:
            cut_pct = r["avg_cut_pct"]
            cut_txt = f" (avg −{cut_pct}%)" if cut_pct == cut_pct else ""  # NaN check
            rows_html += (f'<div class="diff-row"><span class="diff-arrow" style="color:var(--bad);">▼</span>'
                          f'<div class="diff-txt"><b>{esc(r["brand"])} cut {esc(r["cat"])} prices broadly</b> — '
                          f'{dec} cuts{cut_txt}, {inc} increases.</div></div>')
            count += 1

    if not rows_html:
        rows_html = '<div class="diff-row"><div class="diff-txt">No major market moves detected this week.</div></div>'

    return f"""<section class="blk">
  <div class="hd"><span class="n">01</span><span class="t">What changed since last week</span>
    <span class="badge new">WEEKLY DIFF</span></div>
  <div class="bd"><div class="diff-box">
    <div class="diff-hd">Board changes &amp; price moves</div>
    {rows_html}
  </div>
  <div class="note">Tracks brand launch volume and price-move patterns week over week.
    Doesn't yet track board-position changes (e.g. a subcategory moving from WATCH to BUY) —
    that needs a stored snapshot of last week's board, which is a planned follow-up.</div>
  </div>
</section>"""


def render_specs(conn, top_rows):
    """§05 — production specs: size/colour tabs for top board opportunities."""
    tabs_nav = ""
    tabs_body = ""
    for i, row in enumerate(top_rows[:6]):
        cat, sub = row["cat"], row["sub"]
        tab_id = f"spec{i}"
        active = " active" if i == 0 else ""
        tabs_nav += f'<button class="tab-btn{active}" onclick="switchTab(this,\'{tab_id}\')">{esc(cat)} · {esc(sub)}</button>'

        sizes = q_size_ratios(conn, cat, sub)
        colors = q_color_ratios(conn, cat, sub)

        size_total = int(sizes["stockouts"].sum()) if not sizes.empty else 0
        color_total = int(colors["stockouts"].sum()) if not colors.empty else 0

        size_rows = ""
        if size_total > 0:
            mx = int(sizes["stockouts"].max())
            for _, s in sizes.iterrows():
                pct = round(100 * int(s["stockouts"]) / size_total)
                width = round(100 * int(s["stockouts"]) / mx)
                size_rows += (f'<div class="bar-row"><span class="bar-lbl">{esc(s["size"])}</span>'
                             f'<div class="bar-track"><div class="bar-fill" style="width:{width}%;background:var(--act);"></div>'
                             f'<span class="bar-val">{pct}%</span></div></div>')
        else:
            size_rows = '<div style="font-size:12px;color:var(--faint);">No size data available.</div>'

        color_rows = ""
        if color_total > 0:
            mx = int(colors["stockouts"].max())
            for _, c in colors.iterrows():
                pct = round(100 * int(c["stockouts"]) / color_total)
                width = round(100 * int(c["stockouts"]) / mx)
                color_rows += (f'<div class="bar-row"><span class="bar-lbl">{esc(c["color"])}</span>'
                               f'<div class="bar-track"><div class="bar-fill" style="width:{width}%;background:var(--box);border:1px solid var(--grid);"></div>'
                               f'<span class="bar-val">{pct}%</span></div></div>')
        else:
            color_rows = '<div style="font-size:12px;color:var(--faint);">No colour data available.</div>'

        panel_active = " active" if i == 0 else ""
        tabs_body += f"""<div id="{tab_id}" class="tab-panel{panel_active}">
  <div class="spec-grid">
    <div class="spec-col"><div class="spec-title">Size ratio</div>{size_rows}</div>
    <div class="spec-col"><div class="spec-title">Colour ratio</div>{color_rows}</div>
  </div>
</div>"""

    if not tabs_nav:
        return ""

    return f"""<section class="blk">
  <div class="hd"><span class="n">05</span><span class="t">Production specs — size and colour ratios</span></div>
  <div class="bd">
    <p class="intro">Pick a tab. Ratios come from what customers are buying out — not what brands are stocking.</p>
    <div class="tabs">
      <div class="tab-nav">{tabs_nav}</div>
      {tabs_body}
    </div>
  </div>
</section>
<script>
function switchTab(btn, id) {{
  const c = btn.closest('.tabs');
  c.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  c.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  c.querySelector('#' + id).classList.add('active');
}}
</script>"""


def render_price_map(conn, top_rows):
    """§06 — price spectrum strips for top board opportunities."""
    bp = q_brand_prices(conn)
    pb = q_price_band_sellthrough(conn)
    ci = q_confidence_inputs(conn)

    AXIS_MAX = 1600
    blocks = ""
    for row in top_rows[:5]:
        cat, sub = row["cat"], row["sub"]
        prices = bp[(bp["cat"] == cat) & (bp["sub"] == sub)].sort_values("median_price")
        if prices.empty:
            continue

        cid = ci[(ci["cat"] == cat) & (ci["sub"] == sub)]
        n_products = int(cid.iloc[0]["products"]) if not cid.empty else 0
        # rough coverage proxy: products with a price vs total seen in action board
        coverage_txt = f"{row['products']} products tracked"

        # Demand peak zone from best price band
        band_ranges = {k: (lo, hi) for k, _, lo, hi in PRICE_BANDS}
        peak_left, peak_width = None, None
        if row["best_band"] and row["best_band"] in band_ranges:
            lo, hi = band_ranges[row["best_band"]]
            hi = min(hi, AXIS_MAX)
            peak_left = round(100 * lo / AXIS_MAX, 1)
            peak_width = round(100 * (hi - lo) / AXIS_MAX, 1)

        dots = ""
        for _, p in prices.iterrows():
            price = float(p["median_price"])
            left = min(round(100 * price / AXIS_MAX, 1), 98)
            brand_short = p["brand"].replace("_", " ").title()[:10]
            dots += f'<div class="sp-dot" style="left:{left}%;">{esc(brand_short)} {int(price)}</div>'

        peak_html = ""
        if peak_left is not None:
            peak_html = (f'<div class="sp-zone peak" style="left:{peak_left}%;width:{peak_width}%;">'
                        f'<span class="sp-zone-lbl pk">★ demand peak</span></div>')

        blocks += f"""<div class="sp-block">
  <div class="sp-name">{esc(cat)} · {esc(sub)} <span>{esc(coverage_txt)}</span></div>
  <div class="sp-axis">{peak_html}{dots}</div>
  <div class="sp-ticks"><span>0</span><span>300</span><span>500</span><span>700</span><span>1,000</span><span>1,300</span><span>1,600</span></div>
</div>"""

    if not blocks:
        return ""

    return f"""<section class="blk">
  <div class="hd"><span class="n">06</span><span class="t">Where to position your price — gaps and commodity zones</span></div>
  <div class="bd">
    <p class="intro">Green = where demand peaks (from §02). Each dot is a brand's median price. Find the gap.</p>
    {blocks}
    <div class="note"><b>How to read this:</b> If a green zone has no dots in it, that's an opening —
      nobody is priced where demand peaks. Prices are observed medians, not asking prices for new stock.</div>
  </div>
</section>"""


def render(conn):
    rows = build_board(conn)

    tier_go, tier_buy, tier_watch = [], [], []
    for r in rows:
        v, _ = assign_verdict(r)
        if v == "go": tier_go.append(r)
        elif v == "buy": tier_buy.append(r)
        elif v == "watch": tier_watch.append(r)

    top_rows_for_specs = tier_go + tier_buy  # feed §05/§06 with the actionable rows

    if tier_go:
        top = tier_go[0]
        head = (f'Make {esc(top["sub"])} {esc(top["cat"])} now — selling out across '
                f'{top["brands"]} brands, demand {"up" if top["trend_pct"]>0 else "stable"} '
                f'{abs(top["trend_pct"])}% over 8 weeks, bestseller persistence '
                f'{top["avg_weeks"]}/8. Price at {esc(top["price_label"])} EGP '
                f'({top["best_rate"]}% ±{top["best_ci"]} sell-through).')
    elif tier_buy:
        top = tier_buy[0]
        head = (f'No slam-dunk buy signals this week, but {esc(top["sub"])} {esc(top["cat"])} '
                f'is worth a mid-volume bet — {top["best_rate"]}% sell-through at {esc(top["price_label"])} EGP.')
    else:
        head = "No strong buy signals this week — market in transition."

    verdict_html = f"""<div class="verdict">
  <div class="lbl">This week's action</div>
  <div class="v">{head}</div>
  <div class="sub">Every row carries a confidence level, margin of error, and demand index.
    Bestseller persistence distinguishes real demand from one-week spikes.
    All price-band findings are observed associations — not controlled experiments.</div>
</div>"""

    diff_html = render_weekly_diff(conn)

    board_rows = ""
    if tier_go:
        board_rows += '<tr><td colspan="6" class="tier">Make these now — demand is proven</td></tr>'
        for r in tier_go: board_rows += render_board_row(r)
    if tier_buy:
        board_rows += '<tr><td colspan="6" class="tier">Make these — mid volume</td></tr>'
        for r in tier_buy: board_rows += render_board_row(r)
    if tier_watch:
        board_rows += '<tr><td colspan="6" class="tier">Watch — don\'t commit yet</td></tr>'
        for r in tier_watch: board_rows += render_board_row(r)

    if not board_rows:
        board_rows = '<tr><td colspan="6" style="text-align:center;color:var(--faint);padding:20px;">No qualifying opportunities this week.</td></tr>'

    board_html = f"""<section class="blk">
  <div class="hd"><span class="n">02</span>
    <span class="t">The action board — what to make, at what price, and when</span>
    <span class="badge key">KEY SECTION</span></div>
  <div class="bd">
    <p class="intro">Confidence: <span class="conf hi"></span> high
      <span class="conf md"></span> medium <span class="conf lo"></span> low.
      Bestseller persistence = avg weeks products stayed on lists (out of 8).
      Demand index = sell-outs + products still out of stock.</p>
    <div class="table-wrap">
    <table><thead><tr>
      <th>What to make</th><th>Action</th><th>Price at</th>
      <th>Demand proof</th><th>When</th><th>Who's running out</th>
    </tr></thead><tbody>{board_rows}</tbody></table></div>
    <div class="note"><b>How to read this:</b> Each row is a production decision.
      The demand proof column shows three layers: bestseller trend, persistence score,
      and demand index. Price sell-through is an <b>observed association</b> — premium brands
      may sell more because of brand strength, not price alone.
      <br><br><b>Newness caveat:</b> Products under 14 days on bestseller lists may reflect
      small initial stock, not strong demand. Persistence score helps distinguish:
      1-2 weeks = could be scarcity, 6+ weeks = proven demand.</div>
  </div>
</section>"""

    distress = q_distress(conn)
    ci_data = q_confidence_inputs(conn)
    avoid_items = ""
    for cat, sub in AVOID_CATS:
        dd = distress[(distress["cat"] == cat) & (distress["sub"] == sub)]
        if dd.empty:
            continue
        total_esc = int(dd["escalating"].sum())
        avg_dep = round(dd["avg_depth"].mean(), 1)
        top_brands = list(dd.sort_values("escalating", ascending=False).head(3)["brand"])
        brands_txt = ", ".join(top_brands)

        ci_row = ci_data[(ci_data["cat"] == cat) & (ci_data["sub"] == sub)]
        if not ci_row.empty:
            cl = conf_level(int(ci_row.iloc[0]["brands"]), int(ci_row.iloc[0]["products"]), int(ci_row.iloc[0]["with_sellout"]))
        else:
            cl = "md"
        conf_txt = {"hi": "high", "md": "medium", "lo": "low"}[cl]

        avoid_items += f"""<div class="avoid-item">
  <span class="avoid-x">✕</span>
  <div class="avoid-body"><strong>{esc(cat)} · {esc(sub)}</strong>
    <span class="ci"><span class="conf {cl}"></span> {conf_txt} confidence</span> —
    <span class="reason">{total_esc} products in discount escalation (avg {avg_dep}% depth).
    Led by {esc(brands_txt)}. Sell-outs in this category are clearance-driven, not real demand.</span>
  </div>
</div>"""

    avoid_html = f"""<section class="blk">
  <div class="hd"><span class="n">03</span>
    <span class="t">Don't make these — they look like gaps but they're traps</span></div>
  <div class="bd">{avoid_items or '<div style="color:var(--faint);font-size:13px;">No high-confidence traps detected this week.</div>'}</div>
</section>"""

    gap_rows = ""
    for r in sorted(rows, key=lambda x: x["gap_ratio"], reverse=True):
        v, _ = assign_verdict(r)
        if v == "avoid" or r["gap_ratio"] < 1.2:
            continue
        gap_cls = "good" if r["gap_ratio"] >= 2.0 else "warn"
        gap_txt = "OPEN" if r["gap_ratio"] >= 2.0 else "NARROWING"
        gap_rows += f"""<tr>
  <td>{esc(r['cat'])} · <b>{esc(r['sub'])}</b></td>
  <td class="m">{r['sellouts']:,}</td><td class="m">{r['restocks']:,}</td>
  <td style="font-family:var(--mono);font-size:11px;font-weight:700;color:var(--{gap_cls});">{gap_txt} {r['gap_ratio']}×</td>
  <td class="m">{r['sold_out']} + {r['still_out']} = {r['sold_out']+r['still_out']}</td>
  <td>{"".join(f'<span class="b">{esc(b)}</span>' for b in r['brand_list'][:4])}</td>
</tr>"""

    if not gap_rows:
        gap_rows = '<tr><td colspan="6" style="text-align:center;color:var(--faint);padding:20px;">No qualifying gaps this week.</td></tr>'

    gap_html = f"""<section class="blk">
  <div class="hd"><span class="n">04</span>
    <span class="t">Are you early or late? — sell-outs vs restocks</span></div>
  <div class="bd">
    <p class="intro">Ratio above 2× = gap wide open. 1-2× = narrowing.</p>
    <div class="table-wrap"><table><thead><tr>
      <th>Opportunity</th><th>Sell-outs</th><th>Restocks</th><th>Gap</th>
      <th>Demand index</th><th>Top restockers</th>
    </tr></thead><tbody>{gap_rows}</tbody></table></div>
    <div class="note"><b>Demand index:</b> "sold out" + "still out of stock" = estimated total demand
      including what couldn't be sold because it was already gone.</div>
  </div>
</section>"""

    specs_html = render_specs(conn, top_rows_for_specs)
    price_map_html = render_price_map(conn, top_rows_for_specs)

    coverage_html = """<section class="blk">
  <div class="hd"><span class="n">07</span><span class="t">Methodology & reliability</span></div>
  <div class="bd"><div class="cov">
    <span><b>Confidence levels:</b> based on brand count (12+ = high), product count (500+ = high), sell-out sample (200+ = high)</span>
    <span><b>Bestseller persistence:</b> avg weeks each product appeared on bestseller lists out of 8 weeks of daily snapshots from 19 Shopify brands</span>
    <span><b>Demand index:</b> sell-outs + products still out of stock — partial proxy for unmet demand, not a unit count</span>
    <span><b>Confidence intervals:</b> approximate 95% binomial CI — within-brand correlation means true uncertainty is wider</span>
    <span><b>Speed to sell:</b> median days from product launch to first witnessed sell-out — truncated for products under 21 days old</span>
    <span><b>Prices:</b> each product's latest snapshot within the last 8 days (not a single fixed date)</span>
    <span class="x">All price-band findings are <b>observed associations</b> — premium brands may sell more because of brand strength, not price</span>
    <span class="x">Excludes DeFacto (stock unreliable), Tree, Dalydress. Under-tagged: Carina 17%, Mlameh 33%, Activ 40%</span>
  </div></div>
</section>"""

    today = date.today().isoformat()
    body = verdict_html + diff_html + board_html + avoid_html + gap_html + specs_html + price_map_html + coverage_html

    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Khabar — What to Make & At What Price</title>
<style>{MASTER_CSS}</style>
</head><body>
<header><div class="bar">
  <h1>Khabar — What to Make &amp; At What Price</h1>
  <span class="tag">LIVE</span>
  <span class="date">week of {today}</span>
</div></header>
<div class="wrap">{body}</div>
</body></html>"""

    return page


def run():
    conn = R.connect()
    html = render(conn)
    run_dir, _ = R.build_run_dir("master-order-price", "weekly")
    (run_dir / "report.html").write_text(html)
    R.copy_to_latest(run_dir, "master-order-price")
    print(f"[OK] master-order-price: {run_dir / 'report.html'}")
    conn.close()


if __name__ == "__main__":
    run()
