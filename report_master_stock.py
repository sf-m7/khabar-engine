"""
report_master_stock.py — MASTER REPORT: What to Do With Your Stock
================================================================================
Synthesises the Discount + Market reports into one action plan.

v3 fixes (Aug 2026):
  - CRITICAL: same price-snapshot bug as report_master_order.py. Queries
    required snapshot_date = today, which silently broke whenever the day's
    scrape was incomplete (today's example: 16k products vs the normal ~57k).
    Fixed by pulling each product's latest snapshot within the live 8-day
    window (DISTINCT ON) instead of a fixed calendar date.
  - Market temperature (§05) was including the current, still-forming week
    in its trend — since the report runs early Monday, that week's bucket
    only has a few hours of data and always looked like "supply catching up"
    regardless of the real picture. Now shows the last 4 COMPLETE weeks only.
  - Added §01 weekly diff (brand launch deltas + price-move patterns) —
    designed but never wired into render().

Enhancements:
  1. Confidence badges (brand count × product count × sell-outs)
  2. Age × discount matrix (heatmap)
  3. Speed to sell (median days to first sell-out)
  4. Confidence intervals on sell-through rates (binomial 95% CI)
  5. "Observed association" labeling
  6. Restock-adjusted demand index
  7. Competitor activity radar with posture labels
  8. Weekly diff (market signals — see note in render_weekly_diff)
"""

import math
import html as _html
from datetime import date

import report_lib as R
import report_html as H

EXCLUDED_SELL = "('tree','dalydress','defacto')"
EXCLUDED_ALL  = "('tree','dalydress')"

LIVE_WINDOW_DAYS = 8

LATEST_PRICE_CTE = f"""
    latest_price AS (
        SELECT DISTINCT ON (product_id) product_id, brand, price, snapshot_date
        FROM price_snapshots
        WHERE snapshot_date > CURRENT_DATE - INTERVAL '{LIVE_WINDOW_DAYS} days'
        ORDER BY product_id, snapshot_date DESC
    )
"""

# Taxonomy stopgap (Aug 2026) — see report_master_order.py for full writeup.
# Scraper's CATEGORY_MAP checks "shirts" before "sweatshirts"/"hoodies", and
# since "sweatshirt" contains "shirt" as a substring, those get misclassified
# into the shirts category (~15% of it, up to 26% within "long-sleeve").
# Excluded at query time here too so the stock report stays consistent with
# the order report until the scraper/taxonomy fix + backfill lands.
SHIRT_TAXONOMY_FILTER = (
    "AND NOT (p.category_normalized='shirts' AND "
    "(LOWER(p.name) LIKE '%sweatshirt%' OR LOWER(p.name) LIKE '%hoodie%' "
    "OR LOWER(p.name) LIKE '%hoody%'))"
)


def esc(s):
    return _html.escape(str(s))


def ci95(p, n):
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

def q_sell_through_by_depth(conn):
    """Sell-through rate by discount depth per subcategory.

    FIXED: was `ps.snapshot_date = MAX(snapshot_date)`. Now per-product
    latest snapshot within the live 8-day window.
    """
    sql = f"""
    WITH {LATEST_PRICE_CTE}
    SELECT
        p.category_normalized as cat, p.subcategory as sub,
        CASE
            WHEN p.first_observed_price IS NULL OR p.first_observed_price <= 0
                 OR lp.price >= p.first_observed_price THEN 'full_price'
            WHEN 100.0*(1 - lp.price/p.first_observed_price) < 25 THEN '1_24'
            WHEN 100.0*(1 - lp.price/p.first_observed_price) < 40 THEN '25_39'
            ELSE '40p'
        END as depth,
        COUNT(DISTINCT lp.product_id) as products,
        COUNT(DISTINCT se.product_id) as with_sellout
    FROM latest_price lp
    JOIN products p ON p.id = lp.product_id
    LEFT JOIN stockout_events se ON se.product_id = lp.product_id
        AND se.event_type = 'stockout' AND se.witnessed = true
        AND se.recorded_at > NOW() - INTERVAL '21 days'
    WHERE lp.brand NOT IN {EXCLUDED_SELL}
      AND p.subcategory IS NOT NULL AND p.subcategory != ''
      AND p.category_normalized NOT IN ('uncategorized')
      {SHIRT_TAXONOMY_FILTER}
    GROUP BY p.category_normalized, p.subcategory,
             CASE
                 WHEN p.first_observed_price IS NULL OR p.first_observed_price <= 0
                      OR lp.price >= p.first_observed_price THEN 'full_price'
                 WHEN 100.0*(1 - lp.price/p.first_observed_price) < 25 THEN '1_24'
                 WHEN 100.0*(1 - lp.price/p.first_observed_price) < 40 THEN '25_39'
                 ELSE '40p'
             END
    HAVING COUNT(DISTINCT lp.product_id) > 10
    """
    return R.df_sql(conn, sql)


def q_age_discount_matrix(conn):
    """Age × discount cross-tab for the heatmap.

    FIXED: same latest-snapshot bug.
    """
    sql = f"""
    WITH {LATEST_PRICE_CTE}
    SELECT
        CASE
            WHEN EXTRACT(DAY FROM NOW() - p.first_seen_at) <= 14 THEN '0-14d'
            WHEN EXTRACT(DAY FROM NOW() - p.first_seen_at) <= 30 THEN '15-30d'
            WHEN EXTRACT(DAY FROM NOW() - p.first_seen_at) <= 60 THEN '31-60d'
            ELSE '60d+'
        END as age_band,
        CASE
            WHEN p.first_observed_price IS NULL OR p.first_observed_price <= 0
                 OR lp.price >= p.first_observed_price THEN 'full_price'
            WHEN 100.0*(1 - lp.price/p.first_observed_price) < 20 THEN '1_19'
            WHEN 100.0*(1 - lp.price/p.first_observed_price) < 30 THEN '20_29'
            WHEN 100.0*(1 - lp.price/p.first_observed_price) < 40 THEN '30_39'
            ELSE '40p'
        END as depth,
        COUNT(DISTINCT lp.product_id) as products,
        COUNT(DISTINCT se.product_id) as with_sellout
    FROM latest_price lp
    JOIN products p ON p.id = lp.product_id
    LEFT JOIN stockout_events se ON se.product_id = lp.product_id
        AND se.event_type = 'stockout' AND se.witnessed = true
        AND se.recorded_at > NOW() - INTERVAL '21 days'
    WHERE lp.brand NOT IN {EXCLUDED_SELL}
      AND p.category_normalized NOT IN ('uncategorized')
      {SHIRT_TAXONOMY_FILTER}
    GROUP BY 1, 2
    """
    return R.df_sql(conn, sql)


def q_speed_to_sell(conn):
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
        FROM stockout_events WHERE event_type='stockout' AND witnessed=true
        GROUP BY product_id
    ) se ON se.product_id = p.id
    WHERE p.subcategory IS NOT NULL AND p.subcategory != ''
      AND p.brand NOT IN {EXCLUDED_SELL}
      {SHIRT_TAXONOMY_FILTER}
    GROUP BY p.category_normalized, p.subcategory
    HAVING COUNT(DISTINCT p.id) > 15
    """
    return R.df_sql(conn, sql)


def q_confidence_inputs(conn):
    """FIXED: same latest-snapshot bug."""
    sql = f"""
    WITH {LATEST_PRICE_CTE}
    SELECT
        p.category_normalized as cat, p.subcategory as sub,
        COUNT(DISTINCT lp.brand) as brands,
        COUNT(DISTINCT lp.product_id) as products,
        COUNT(DISTINCT se.product_id) FILTER (
            WHERE se.event_type='stockout' AND se.witnessed=true
        ) as with_sellout
    FROM latest_price lp
    JOIN products p ON p.id = lp.product_id
    LEFT JOIN stockout_events se ON se.product_id = lp.product_id
        AND se.recorded_at > NOW() - INTERVAL '21 days'
    WHERE lp.brand NOT IN {EXCLUDED_SELL}
      AND p.subcategory IS NOT NULL AND p.subcategory != ''
      {SHIRT_TAXONOMY_FILTER}
    GROUP BY p.category_normalized, p.subcategory
    """
    return R.df_sql(conn, sql)


def q_brand_activity(conn):
    """Brand radar: launches, price changes, sell-outs, restocks this week."""
    sql = f"""
    SELECT
        p.brand,
        COUNT(DISTINCT CASE WHEN p.first_seen_at >= NOW()-INTERVAL '7 days' THEN p.id END) as launches,
        COUNT(DISTINCT CASE WHEN p.first_seen_at >= NOW()-INTERVAL '14 days'
              AND p.first_seen_at < NOW()-INTERVAL '7 days' THEN p.id END) as prev_launches,
        COUNT(DISTINCT CASE WHEN pe.recorded_at >= NOW()-INTERVAL '7 days' THEN pe.id END) as price_chg,
        COUNT(DISTINCT CASE WHEN pe.recorded_at >= NOW()-INTERVAL '7 days'
              AND pe.direction='up' THEN pe.id END) as price_up,
        COUNT(DISTINCT CASE WHEN pe.recorded_at >= NOW()-INTERVAL '7 days'
              AND pe.direction='down' THEN pe.id END) as price_down,
        COUNT(DISTINCT CASE WHEN se.event_type='stockout' AND se.witnessed=true
              AND se.recorded_at >= NOW()-INTERVAL '7 days' THEN se.id END) as sellouts,
        COUNT(DISTINCT CASE WHEN se.event_type='restock' AND se.witnessed=true
              AND se.recorded_at >= NOW()-INTERVAL '7 days' THEN se.id END) as restocks
    FROM products p
    LEFT JOIN price_events pe ON pe.product_id = p.id
        AND pe.recorded_at >= NOW() - INTERVAL '7 days'
    LEFT JOIN stockout_events se ON se.product_id = p.id
        AND se.recorded_at >= NOW() - INTERVAL '7 days'
    WHERE p.brand NOT IN {EXCLUDED_ALL} AND p.is_active = true
    GROUP BY p.brand
    HAVING COUNT(DISTINCT CASE WHEN p.first_seen_at >= NOW()-INTERVAL '7 days' THEN p.id END) > 0
        OR COUNT(DISTINCT CASE WHEN pe.recorded_at >= NOW()-INTERVAL '7 days' THEN pe.id END) > 0
        OR COUNT(DISTINCT CASE WHEN se.recorded_at >= NOW()-INTERVAL '7 days' THEN se.id END) > 0
    ORDER BY launches DESC
    """
    return R.df_sql(conn, sql)


def q_market_temp(conn):
    """Weekly sell-out vs restock ratio.

    FIXED: excludes the current, still-forming week. The report runs early
    Monday, so "this week" would otherwise only contain a few hours of data
    and always show a misleadingly low ratio ("supply catching up") no
    matter what the real market looks like. Shows the last 4 COMPLETE weeks.
    """
    sql = f"""
    SELECT
        DATE_TRUNC('week', recorded_at)::date as week,
        COUNT(*) FILTER (WHERE event_type='stockout' AND witnessed=true) as sellouts,
        COUNT(*) FILTER (WHERE event_type='restock' AND witnessed=true) as restocks,
        ROUND(1.0 * COUNT(*) FILTER (WHERE event_type='stockout' AND witnessed=true) /
            NULLIF(COUNT(*) FILTER (WHERE event_type='restock' AND witnessed=true), 0), 2
        ) as ratio
    FROM stockout_events
    WHERE brand NOT IN {EXCLUDED_ALL}
      AND recorded_at >= DATE_TRUNC('week', CURRENT_DATE) - INTERVAL '28 days'
      AND recorded_at <  DATE_TRUNC('week', CURRENT_DATE)
    GROUP BY DATE_TRUNC('week', recorded_at)::date
    ORDER BY week
    """
    return R.df_sql(conn, sql)


def q_bestseller_persistence(conn):
    sql = f"""
    SELECT
        p.category_normalized as cat, p.subcategory as sub,
        ROUND(AVG(sub.weeks_on), 1) as avg_weeks
    FROM bestseller_rank br
    JOIN products p ON p.id = br.product_id
    JOIN (
        SELECT product_id, COUNT(DISTINCT DATE_TRUNC('week', snapshot_date)) as weeks_on
        FROM bestseller_rank GROUP BY product_id
    ) sub ON sub.product_id = br.product_id
    WHERE p.subcategory IS NOT NULL AND br.brand NOT IN {EXCLUDED_ALL}
      {SHIRT_TAXONOMY_FILTER}
    GROUP BY p.category_normalized, p.subcategory
    """
    return R.df_sql(conn, sql)


def q_launches_by_brand(conn):
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
      {SHIRT_TAXONOMY_FILTER}
    GROUP BY pe.brand, p.category_normalized
    HAVING COUNT(*) > 50
    ORDER BY changes DESC
    """
    return R.df_sql(conn, sql)


# ---------------------------------------------------------------------------
#  Board assembly
# ---------------------------------------------------------------------------

def build_stock_board(conn):
    st = q_sell_through_by_depth(conn)
    sp = q_speed_to_sell(conn)
    ci_data = q_confidence_inputs(conn)
    pers = q_bestseller_persistence(conn)

    items = {}
    for _, r in st.iterrows():
        key = (r["cat"], r["sub"])
        if key not in items:
            items[key] = {"cat": r["cat"], "sub": r["sub"], "depths": {}}
        n = int(r["products"])
        so = int(r["with_sellout"])
        rate = round(100 * so / n, 1) if n > 0 else 0
        items[key]["depths"][r["depth"]] = {"rate": rate, "n": n, "ci": ci95(rate/100, n)}

    rows = []
    for key, item in items.items():
        cat, sub = key
        depths = item["depths"]
        if not depths:
            continue

        cir = ci_data[(ci_data["cat"] == cat) & (ci_data["sub"] == sub)]
        if not cir.empty:
            n_br, n_pr, n_se = int(cir.iloc[0]["brands"]), int(cir.iloc[0]["products"]), int(cir.iloc[0]["with_sellout"])
        else:
            n_br, n_pr, n_se = 0, 0, 0
        conf = conf_level(n_br, n_pr, n_se)

        spd = sp[(sp["cat"] == cat) & (sp["sub"] == sub)]
        med_days = int(spd.iloc[0]["median_days"]) if not spd.empty else None

        pr = pers[(pers["cat"] == cat) & (pers["sub"] == sub)]
        avg_wk = float(pr.iloc[0]["avg_weeks"]) if not pr.empty else 0

        best_depth = max(depths, key=lambda d: depths[d]["rate"])
        fp = depths.get("full_price", {}).get("rate", 0)

        rows.append({
            "cat": cat, "sub": sub, "brands": n_br, "products": n_pr, "conf": conf,
            "depths": depths, "best_depth": best_depth, "full_price_rate": fp,
            "med_days": med_days, "avg_weeks": avg_wk,
        })

    return rows


def assign_stock_verdict(row):
    depths = row["depths"]
    fp = depths.get("full_price", {}).get("rate", 0)
    light = depths.get("1_24", {}).get("rate", 0)
    mid = depths.get("25_39", {}).get("rate", 0)
    deep = depths.get("40p", {}).get("rate", 0)

    if all(d.get("rate", 0) < 12 for d in depths.values()):
        return "stop", "Stop — outlet"
    if fp > light and fp > mid and fp >= deep * 0.8 and fp >= 20:
        return "raise", "Test price increase"
    if light > fp * 1.3 and light > mid and light > deep:
        return "light", "Reduce to ~20%"
    if deep > fp and deep > light and deep > mid:
        return "deep", "Hold or go 40%+"
    if mid > fp * 1.3 and mid >= deep * 0.8:
        return "light", "Start at ~25%"
    if fp >= 15 or light >= 25:
        return "hold", "Hold price"
    return "hold", "Hold price"


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
.bar h1{font-size:16px;font-weight:700}.bar .tag{font-family:var(--mono);font-size:10px;color:#fff;background:var(--bad);border-radius:3px;padding:2px 6px;font-weight:600}
.bar .date{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-left:auto}
.verdict{border:1px solid var(--bad);background:var(--bad-bg);border-radius:8px;padding:18px 20px;margin:20px 0 8px}
.verdict .lbl{font-family:var(--mono);font-size:10px;letter-spacing:.1em;color:var(--bad);text-transform:uppercase;font-weight:700}
.verdict .v{font-size:17px;font-weight:700;margin:8px 0 6px;line-height:1.45}.verdict .sub{font-size:13.5px;color:var(--muted);line-height:1.55}
section.blk{border:1px solid var(--line);border-radius:8px;background:#fff;margin:16px 0;overflow:hidden}
.blk>.hd{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--box);flex-wrap:wrap}
.blk>.hd .n{font-family:var(--mono);font-size:10.5px;color:var(--faint)}.blk>.hd .t{font-size:13.5px;font-weight:650}
.blk>.hd .badge{margin-left:auto;font-family:var(--mono);font-size:9.5px;padding:2px 8px;border-radius:20px;color:#fff;font-weight:600}
.blk>.hd .badge.key{background:var(--bad)}.blk>.hd .badge.new{background:var(--blue)}.blk>.hd .badge.mkt{background:#555}
.blk>.bd{padding:16px 18px}
.note{font-size:13px;color:var(--muted);line-height:1.6;margin:14px 0 0;padding:10px 12px;background:var(--paper);border-radius:5px;border-left:3px solid var(--grid)}
.note b{color:var(--ink);font-weight:600}
.intro{font-size:13.5px;color:var(--muted);line-height:1.55;margin-bottom:14px}
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -18px;padding:0 18px}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:800px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--grid);vertical-align:top}
th{font-family:var(--mono);font-size:9.5px;letter-spacing:.04em;color:var(--faint);text-transform:uppercase;position:sticky;top:0;background:#fff}
td.m{font-family:var(--mono);font-size:12px}tr:last-child td{border-bottom:none}
.pill{font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:20px;color:#fff;display:inline-block;font-weight:700;white-space:nowrap}
.pill.hold{background:var(--good)}.pill.light{background:var(--act)}.pill.deep{background:var(--bad)}.pill.stop{background:#7C2D12}.pill.raise{background:var(--blue)}
.b{font-family:var(--mono);font-size:10px;display:inline-block;background:var(--box);border-radius:3px;padding:1px 6px;margin:1px 2px}
.b.first{background:var(--act-bg);border:1px solid #E8C98E;color:var(--act);font-weight:700}
.tier{font-family:var(--mono);font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;font-weight:600;padding:10px 10px 5px;color:var(--faint);border-top:1px solid var(--line);background:var(--paper)}
.proof{display:inline-flex;align-items:center;gap:3px}
.proof-dot{width:8px;height:8px;border-radius:2px;flex-shrink:0}
.proof-dot.best{background:var(--good)}.proof-dot.ok{background:var(--act)}.proof-dot.bad{background:var(--bad)}.proof-dot.neutral{background:var(--grid)}
.proof-pct{font-family:var(--mono);font-size:11px;font-weight:600}
.conf{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:3px;vertical-align:middle}
.conf.hi{background:var(--good)}.conf.md{background:var(--warn)}.conf.lo{background:var(--bad)}
.ci{font-family:var(--mono);font-size:9px;color:var(--faint);font-weight:400}
.speed{font-family:var(--mono);font-size:9.5px;color:var(--faint);display:block;margin-top:2px}
.posture{font-family:var(--mono);font-size:10px;padding:2px 8px;border-radius:20px;color:#fff;display:inline-block;font-weight:700}
.posture.attack{background:var(--good)}.posture.manage{background:var(--act)}.posture.defend{background:var(--bad)}.posture.sleep{background:var(--faint)}
.delta{font-family:var(--mono);font-size:10.5px;font-weight:600}
.delta.up{color:var(--good)}.delta.down{color:var(--bad)}.delta.flat{color:var(--faint)}
.temp-row{display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--grid)}
.temp-row:last-child{border-bottom:none}
.temp-week{font-family:var(--mono);font-size:11px;color:var(--faint);width:55px;flex-shrink:0}
.temp-bar{flex:1;display:flex;gap:2px}
.temp-seg{height:18px;border-radius:2px}.temp-seg.so{background:var(--act)}.temp-seg.rs{background:var(--good-bg);border:1px solid #A8D5AD}
.temp-ratio{font-family:var(--mono);font-size:12px;font-weight:600;width:45px;text-align:right;flex-shrink:0}
.temp-read{font-family:var(--mono);font-size:10px;color:var(--faint);width:95px;flex-shrink:0}
.hm{border-collapse:collapse;min-width:500px}
.hm th,.hm td{text-align:center;padding:8px 10px;font-family:var(--mono);font-size:12px}
.hm .hot{background:#daf1dc;font-weight:700;color:var(--good)}
.hm .warm{background:#fef9e7;color:var(--warn)}
.hm .cold{background:#fce8e6;color:var(--bad)}
.hm .neut{background:var(--paper);color:var(--faint)}
.hm .sub-ci{display:block;font-size:8.5px;color:var(--faint);font-weight:400}
.cov{font-family:var(--mono);font-size:11px;color:var(--muted);line-height:2}
.cov b{color:var(--ink);font-weight:600}.cov .x{color:var(--bad)}.cov span{display:inline-block;margin-right:18px}
.diff-box{background:var(--blue-bg);border:1px solid #C7D9F7;border-radius:6px;padding:14px 16px}
.diff-hd{font-family:var(--mono);font-size:10px;font-weight:700;color:var(--blue);text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px}
.diff-row{display:flex;gap:8px;align-items:flex-start;padding:5px 0}
.diff-row+.diff-row{border-top:1px solid #dde6f7}
.diff-arrow{font-family:var(--mono);font-size:13px;width:18px;text-align:center;flex-shrink:0}
.diff-txt{font-size:13.5px;line-height:1.5}
@media(max-width:700px){body{font-size:14px}.wrap{padding:0 14px 60px}.verdict{padding:14px 16px}.verdict .v{font-size:15.5px}.blk>.bd{padding:14px}.table-wrap{margin:0 -14px;padding:0 14px}.note{font-size:12.5px}.cov span{display:block;margin-right:0}.temp-read{display:none}}
@media(max-width:450px){.bar .date{margin-left:0;flex-basis:100%}.verdict .v{font-size:14.5px}table{font-size:12px}th,td{padding:7px 6px}}
"""


def render_depth_cell(depths, band_key, best_depth):
    d = depths.get(band_key)
    if not d or d["n"] < 5:
        return '<td><span class="proof"><span class="proof-dot neutral"></span><span class="proof-pct">—</span></span></td>'
    dot_cls = "best" if band_key == best_depth else "ok" if d["rate"] >= 20 else "bad"
    style = ' style="color:var(--good);"' if band_key == best_depth else ""
    ci_txt = f'<br><span class="ci">±{d["ci"]}</span>' if d["ci"] > 0 else ""
    return (f'<td><span class="proof"><span class="proof-dot {dot_cls}"></span>'
            f'<span class="proof-pct"{style}>{d["rate"]}%</span></span>{ci_txt}</td>')


def render_weekly_diff(conn):
    """§01 — data-driven weekly diff. Same scope note as report_master_order.py:
    tracks market signals (launches, price moves), not board-state changes."""
    launches = q_launches_by_brand(conn)
    moves = q_price_movements(conn)
    mt = q_market_temp(conn)

    rows_html = ""
    count = 0

    if len(mt) >= 2:
        last_two = mt.tail(2)
        prev_ratio = float(last_two.iloc[0]["ratio"] or 0)
        curr_ratio = float(last_two.iloc[1]["ratio"] or 0)
        if prev_ratio > 0 and abs(curr_ratio - prev_ratio) / prev_ratio > 0.25:
            direction = "up" if curr_ratio > prev_ratio else "down"
            arrow_color = "var(--bad)" if direction == "up" else "var(--good)"
            reading = "demand pulling ahead of supply" if direction == "up" else "supply catching up to demand"
            rows_html += (f'<div class="diff-row"><span class="diff-arrow" style="color:{arrow_color};">'
                          f'{"▲" if direction=="up" else "▼"}</span>'
                          f'<div class="diff-txt"><b>Market temperature shifted</b> — sell-out/restock ratio '
                          f'moved from {prev_ratio}× to {curr_ratio}×. {reading.capitalize()}.</div></div>')
            count += 1

    for _, r in launches.iterrows():
        if count >= 4: break
        tw, lw = int(r["this_week"]), int(r["last_week"])
        if lw == 0 and tw >= 30:
            rows_html += (f'<div class="diff-row"><span class="diff-arrow" style="color:var(--good);">▲</span>'
                          f'<div class="diff-txt"><b>{esc(r["brand"])} returned to launching</b> — '
                          f'{tw:,} new products after little to no activity last week.</div></div>')
            count += 1
        elif lw > 0 and tw >= lw * 2 and tw >= 30:
            rows_html += (f'<div class="diff-row"><span class="diff-arrow" style="color:var(--good);">▲</span>'
                          f'<div class="diff-txt"><b>{esc(r["brand"])} ramped up launches</b> — '
                          f'{tw:,} new products this week, up from {lw:,}.</div></div>')
            count += 1

    for _, r in moves.iterrows():
        if count >= 6: break
        inc, dec, chg = int(r["increases"]), int(r["decreases"]), int(r["changes"])
        if inc >= chg * 0.9 and inc >= 50:
            rows_html += (f'<div class="diff-row"><span class="diff-arrow" style="color:var(--good);">▲</span>'
                          f'<div class="diff-txt"><b>{esc(r["brand"])} raised {esc(r["cat"])} prices</b> — '
                          f'{inc} increases, {dec} cuts. Confidence signal.</div></div>')
            count += 1
        elif dec >= chg * 0.9 and dec >= 50:
            cut_pct = r["avg_cut_pct"]
            cut_txt = f" (avg −{cut_pct}%)" if cut_pct == cut_pct else ""
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
    <div class="diff-hd">Market moves &amp; price shifts</div>
    {rows_html}
  </div>
  <div class="note">Tracks brand launch volume, price-move patterns, and market temperature
    week over week. Doesn't yet track board-position changes (e.g. a category moving from
    HOLD to STOP) — that needs a stored snapshot of last week's board, planned as a follow-up.</div>
  </div>
</section>"""


def render(conn):
    rows = build_stock_board(conn)
    adm = q_age_discount_matrix(conn)
    ba = q_brand_activity(conn)
    mt = q_market_temp(conn)

    tiers = {"stop": [], "light": [], "deep": [], "raise": [], "hold": []}
    for r in rows:
        v, _ = assign_stock_verdict(r)
        tiers[v].append(r)

    tier_labels = {
        "stop":  "Urgent — stop discounting these",
        "light": "Fix now — you're at the wrong depth",
        "deep":  "If discounting, commit — half-measures hurt",
        "raise": "Raise price — demand supports it",
        "hold":  "No action needed — working fine",
    }
    tier_order = ["stop", "light", "deep", "raise", "hold"]

    top_actions = []
    if tiers["stop"]:
        names = ", ".join(f'{r["cat"]} · {r["sub"]}' for r in tiers["stop"][:2])
        top_actions.append(f"Stop discounting {names} — nothing moves them at any depth")
    if tiers["light"]:
        r = tiers["light"][0]
        light_rate = r["depths"].get("1_24", {}).get("rate", 0)
        light_ci = r["depths"].get("1_24", {}).get("ci", 0)
        deep_rate = r["depths"].get("40p", {}).get("rate", 0)
        top_actions.append(f"Reduce {r['cat']} · {r['sub']} discount to ~20% "
                          f"({light_rate}% ±{light_ci} vs {deep_rate}% at 40%+)")
    if tiers["raise"]:
        names = ", ".join(f'{r["sub"]}' for r in tiers["raise"][:2])
        top_actions.append(f"{names} can take a price increase — discounting destroys demand")

    head = ". ".join(top_actions[:3]) + "." if top_actions else "No urgent actions this week."

    verdict_html = f"""<div class="verdict">
  <div class="lbl">This week's action</div>
  <div class="v">{head}</div>
  <div class="sub">Every recommendation carries a confidence level based on sample size and brand coverage.
    Sell-through figures show margins of error. All findings are observed associations — not controlled experiments.</div>
</div>"""

    diff_html = render_weekly_diff(conn)

    board_rows = ""
    for tier in tier_order:
        if not tiers[tier]:
            continue
        board_rows += f'<tr><td colspan="7" class="tier">{esc(tier_labels[tier])}</td></tr>'
        for r in tiers[tier][:5]:
            depths = r["depths"]
            v, label = assign_stock_verdict(r)
            speed_txt = f'{r["med_days"]} days to sell' if r["med_days"] else "—"
            fp = render_depth_cell(depths, 'full_price', r['best_depth'])
            l_cell = render_depth_cell(depths, '1_24', r['best_depth'])
            m_cell = render_depth_cell(depths, '25_39', r['best_depth'])
            d_cell = render_depth_cell(depths, '40p', r['best_depth'])
            board_rows += f"""<tr>
  <td>{esc(r['cat'])} · <b>{esc(r['sub'])}</b><br>
    <span class="speed"><span class="conf {r['conf']}"></span> {r['brands']} brands · {r['products']:,} products</span></td>
  <td><span class="pill {v}">{esc(label)}</span></td>
  {fp}{l_cell}{m_cell}{d_cell}
  <td><span class="speed">{speed_txt}</span></td>
</tr>"""

    if not board_rows:
        board_rows = '<tr><td colspan="7" style="text-align:center;color:var(--faint);padding:20px;">No qualifying categories this week.</td></tr>'

    board_html = f"""<section class="blk">
  <div class="hd"><span class="n">02</span>
    <span class="t">The stock action board — what to do with each category</span>
    <span class="badge key">KEY SECTION</span></div>
  <div class="bd">
    <p class="intro">Confidence: <span class="conf hi"></span> high
      <span class="conf md"></span> medium <span class="conf lo"></span> low.
      Sell-through shows ±margin of error. Speed = median days to first sell-out.</p>
    <div class="table-wrap"><table><thead><tr>
      <th>What you have</th><th>What to do</th>
      <th>Sell-out<br><span style="font-weight:400;text-transform:none;">full price</span></th>
      <th>Sell-out<br><span style="font-weight:400;text-transform:none;">1-24%</span></th>
      <th>Sell-out<br><span style="font-weight:400;text-transform:none;">25-39%</span></th>
      <th>Sell-out<br><span style="font-weight:400;text-transform:none;">40%+</span></th>
      <th>Speed &amp; context</th>
    </tr></thead><tbody>{board_rows}</tbody></table></div>
    <div class="note"><b>How to read this:</b> Green dot = best sell-through at that depth.
      ± figures are approximate 95% confidence margins.
      <br><br><b>Important:</b> These are <b>observed associations</b> — products discounted to 40%
      may already be the ones that weren't selling. Use alongside the age × discount table (§03)
      for a more complete picture.</div>
  </div>
</section>"""

    age_order = ["0-14d", "15-30d", "31-60d", "60d+"]
    depth_order = ["full_price", "1_19", "20_29", "30_39", "40p"]
    depth_labels = {"full_price": "Full price", "1_19": "1-19%", "20_29": "20-29%", "30_39": "30-39%", "40p": "40%+"}

    hm_rows = ""
    for age in age_order:
        hm_rows += f'<tr><td style="text-align:left;"><b>{esc(age)}</b></td>'
        for d in depth_order:
            cell = adm[(adm["age_band"] == age) & (adm["depth"] == d)]
            if cell.empty or int(cell.iloc[0]["products"]) < 5:
                hm_rows += '<td class="neut">—</td>'
                continue
            n = int(cell.iloc[0]["products"])
            so = int(cell.iloc[0]["with_sellout"])
            rate = round(100 * so / n, 1)
            cls = "hot" if rate >= 40 else "warm" if rate >= 15 else "cold"
            warn = " ⚠" if n < 30 else ""
            hm_rows += f'<td class="{cls}">{rate}%<span class="sub-ci">n={n:,}{warn}</span></td>'
        hm_rows += "</tr>"

    heatmap_html = f"""<section class="blk">
  <div class="hd"><span class="n">03</span>
    <span class="t">Does product age change the picture? — age × discount heatmap</span></div>
  <div class="bd">
    <p class="intro">Sell-through by product age × discount depth. Shows how the two interact.</p>
    <div class="table-wrap"><table class="hm"><thead><tr>
      <th style="text-align:left;">Product age</th>
      {"".join(f'<th>{esc(depth_labels[d])}</th>' for d in depth_order)}
    </tr></thead><tbody>{hm_rows}</tbody></table></div>
    <div class="note"><b>Key insights:</b> 15-30 day products respond best to light discounts.
      31-60 day products need any discount at all. 0-14 day products should generally launch
      at full price. Cells with n &lt; 30 marked ⚠ — low confidence.
      <br><br>All figures are <b>observed associations</b> across the entire market, not controlled experiments.
      n = sample size.</div>
  </div>
</section>"""

    brand_rows = ""
    for _, b in ba.head(12).iterrows():
        launches, prev, pc = int(b["launches"]), int(b["prev_launches"]), int(b["price_chg"])
        p_up, p_down, so = int(b["price_up"]), int(b["price_down"]), int(b["sellouts"])

        if prev > 0:
            if launches > prev * 1.5: delta_html = f'<span class="delta up">▲ {prev}</span>'
            elif launches < prev * 0.5: delta_html = f'<span class="delta down">▼ {prev}</span>'
            else: delta_html = '<span class="delta flat">≈</span>'
        else:
            delta_html = '<span class="delta up">▲ 0</span>' if launches > 0 else '<span class="delta flat">=</span>'

        arrow = " ↑" if pc > 0 and p_up >= pc * 0.9 else " ↓" if pc > 0 and p_down >= pc * 0.9 else " ↕" if pc > 0 else ""

        if launches > 50 and so > launches: posture = "attack"
        elif pc > launches * 2: posture = "manage"
        elif pc > 100 and launches < 30: posture = "defend"
        elif launches < 10 and pc < 20: posture = "sleep"
        else: posture = "manage"
        posture_labels = {"attack": "Attacking", "manage": "Managing", "defend": "Defending", "sleep": "Quiet"}

        brand_rows += f"""<tr>
  <td><b>{esc(b['brand'])}</b></td>
  <td class="m">{launches:,}</td><td>{delta_html}</td>
  <td class="m">{pc:,}{arrow}</td><td class="m">{so:,}</td>
  <td><span class="posture {posture}">{posture_labels[posture]}</span></td>
</tr>"""

    radar_html = f"""<section class="blk">
  <div class="hd"><span class="n">04</span>
    <span class="t">What your competitors did this week</span>
    <span class="badge mkt">MARKET CONTEXT</span></div>
  <div class="bd">
    <div class="table-wrap"><table><thead><tr>
      <th>Brand</th><th>Launches</th><th>vs last wk</th>
      <th>Price chg</th><th>Sell-outs</th><th>Posture</th>
    </tr></thead><tbody>{brand_rows}</tbody></table></div>
    <div class="note"><b>Attacking</b> = launching + growing. <b>Managing</b> = adjusting existing.
      <b>Defending</b> = cutting prices. <b>Quiet</b> = minimal activity.
      Arrows: ↑ mostly increases, ↓ mostly cuts, ↕ mixed.</div>
  </div>
</section>"""

    temp_rows_html = ""
    if mt.empty:
        temp_rows_html = '<div style="color:var(--faint);font-size:13px;">Not enough complete weeks of data yet.</div>'
    else:
        for _, t in mt.iterrows():
            so, rs = int(t["sellouts"]), int(t["restocks"])
            total = so + rs or 1
            so_pct, rs_pct = so/total*100, rs/total*100
            ratio = float(t["ratio"]) if t["ratio"] else 0
            color = "var(--good)" if ratio < 1 else "var(--bad)" if ratio > 3 else "var(--act)"
            label = "Supply catching up" if ratio < 1 else "Peak demand" if ratio > 3 else "Demand ahead"
            week_str = str(t["week"])[5:10]
            temp_rows_html += f"""<div class="temp-row">
  <span class="temp-week">{esc(week_str)}</span>
  <div class="temp-bar"><div class="temp-seg so" style="width:{so_pct:.0f}%;"></div>
    <div class="temp-seg rs" style="width:{rs_pct:.0f}%;"></div></div>
  <span class="temp-ratio" style="color:{color};">{ratio}×</span>
  <span class="temp-read">{label}</span>
</div>"""

    temp_html = f"""<section class="blk">
  <div class="hd"><span class="n">05</span>
    <span class="t">Market temperature — is the window closing?</span></div>
  <div class="bd">{temp_rows_html}
    <div class="note"><b>What this means:</b> Above 1× = demand ahead of supply.
      Below 1× = supply catching up. Shows the last 4 <b>complete</b> weeks —
      the current in-progress week is excluded since it's always partial when this report runs.
      Categories where you're holding price still have room. Categories where everyone's
      discounting won't recover.</div>
  </div>
</section>"""

    coverage_html = """<section class="blk">
  <div class="hd"><span class="n">06</span><span class="t">Methodology & reliability</span></div>
  <div class="bd"><div class="cov">
    <span><b>Sell-through by depth:</b> witnessed sell-outs cross-referenced with current discount, 21 days</span>
    <span><b>Prices:</b> each product's latest snapshot within the last 8 days (not a single fixed date)</span>
    <span><b>Discount depth:</b> uses first-observed price (not brand's compare-at, which brands inflate)</span>
    <span><b>Confidence intervals:</b> approximate 95% binomial CI — within-brand correlation means true uncertainty is wider</span>
    <span><b>Speed to sell:</b> median days from launch to first witnessed sell-out</span>
    <span><b>Age × discount:</b> all categories combined — patterns may differ within subcategories</span>
    <span><b>Market temperature:</b> last 4 complete calendar weeks, current in-progress week excluded</span>
    <span class="x">All findings are <b>observed associations</b>, not causal estimates</span>
    <span class="x">Excludes DeFacto sell-outs (stock unreliable), Tree, Dalydress (phantom data)</span>
    <span class="x">Excludes sweatshirts/hoodies mistagged as "shirts" by a known classifier bug (~15% of the category) — fix pending a taxonomy backfill</span>
  </div></div>
</section>"""

    today = date.today().isoformat()
    body = verdict_html + diff_html + board_html + heatmap_html + radar_html + temp_html + coverage_html

    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Khabar — What to Do With Your Stock</title>
<style>{MASTER_CSS}</style>
</head><body>
<header><div class="bar">
  <h1>Khabar — What to Do With Your Stock</h1>
  <span class="tag">LIVE</span>
  <span class="date">week of {today}</span>
</div></header>
<div class="wrap">{body}</div>
</body></html>"""

    return page


def run():
    conn = R.connect()
    html = render(conn)
    run_dir, _ = R.build_run_dir("master-stock-action", "weekly")
    (run_dir / "report.html").write_text(html)
    R.copy_to_latest(run_dir, "master-stock-action")
    print(f"[OK] master-stock-action: {run_dir / 'report.html'}")
    conn.close()


if __name__ == "__main__":
    run()
