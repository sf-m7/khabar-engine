"""
report_discount.py — WEEKLY "What to Discount".
================================================================================
The market's discount posture a brand's own POS can't see: how deep each
category is discounting, whether that discounting is clearing stock or just
sliding into distress, and — as it matures — the discount depth that actually
clears.

Data flows through report_lib (exclusions, confidence). Rendering via
report_html. No numbers are computed here that aren't already in the trusted
tables; this file only shapes and frames them.
"""

import pandas as pd
import report_lib as R
import report_html as H

MIN_EVENTS = 20   # a category needs real sell-out volume to be worth showing


def _distress_rank(s):
    s = str(s)
    if s.startswith("urgent"):
        return 3
    if s == "watch":
        return 2
    return 1


def run():
    conn = R.connect()

    # --- pull the two trusted product tables (latest day), per brand ---------
    el = R.df_sql(conn, """
        SELECT brand, category_normalized, products_with_drops, stockout_events,
               avg_drop_pct, pct_stockouts_while_discounted
        FROM product_l2_01_price_elasticity
        WHERE report_date = (SELECT max(report_date) FROM product_l2_01_price_elasticity)
    """)
    di = R.df_sql(conn, """
        SELECT brand, category_normalized, escalating_products, dead_stock_products,
               staircase_products, distress_level
        FROM product_l2_12_liquidation_calendar
        WHERE report_date = (SELECT max(report_date) FROM product_l2_12_liquidation_calendar)
    """)

    # --- P0 single-source exclusion (price-based reports drop scope 'all') ----
    el = R.drop_excluded(el)
    di = R.drop_excluded(di)

    # --- aggregate to market/category level ----------------------------------
    el["_num"] = el["pct_stockouts_while_discounted"].fillna(0) / 100.0 * el["stockout_events"].fillna(0)
    g = el.groupby("category_normalized").agg(
        brands=("brand", "nunique"),
        prods=("products_with_drops", "sum"),
        events=("stockout_events", "sum"),
        avg_depth=("avg_drop_pct", "mean"),
        _num=("_num", "sum"),
    ).reset_index()
    g["clear"] = (100.0 * g["_num"] / g["events"].replace(0, pd.NA)).round(1)
    g["avg_depth"] = g["avg_depth"].round(1)

    di["_rank"] = di["distress_level"].map(_distress_rank)
    d = di.groupby("category_normalized").agg(
        esc=("escalating_products", "sum"),
        dead=("dead_stock_products", "sum"),
        stair=("staircase_products", "sum"),
        rank=("_rank", "max"),
    ).reset_index()

    m = g.merge(d, on="category_normalized", how="left").fillna(
        {"esc": 0, "dead": 0, "stair": 0, "rank": 1})
    m = m[m["events"] >= MIN_EVENTS].sort_values("prods", ascending=False)
    m["distress"] = m["rank"].map({3: "urgent", 2: "watch", 1: "normal"})

    if m.empty:
        body = H.section("01", "Discount depth & distress", H.why(
            "No category cleared the minimum sell-out volume this week."))
        return H.write("what-to-discount", "Khabar — What to Discount",
                       "Weekly · market discount posture", body)

    rows = m.to_dict("records")
    top = rows[0]
    urgent = [r for r in rows if r["distress"] == "urgent"]
    best = max(rows, key=lambda r: (r["clear"] if pd.notna(r["clear"]) else -1))
    worst = min(rows, key=lambda r: (r["clear"] if pd.notna(r["clear"]) else 999))

    # --- 0 · verdict ---------------------------------------------------------
    urg_txt = (", ".join(f"<b>{H.esc(r['category_normalized'])}</b>" for r in urgent[:3])
               or "no category")
    v = H.verdict(
        "The decision this report answers",
        f"{H.esc(top['category_normalized']).capitalize()} is the market's deepest "
        f"discount battleground.",
        f"{urg_txt} read <b>urgent</b> — deep, escalating markdowns with dead stock. "
        f"Discounting clears best in <b>{H.esc(best['category_normalized'])}</b> "
        f"({best['clear']}%) and barely moves "
        f"<b>{H.esc(worst['category_normalized'])}</b> ({worst['clear']}%).")

    # --- 1 · depth + distress table ------------------------------------------
    max_depth = max(r["avg_depth"] for r in rows) or 1
    trows = []
    for r in rows:
        bar = (f'<div class="depthbar"><i style="width:'
               f'{r["avg_depth"]/max_depth*100:.0f}%"></i>'
               f'<span>{r["avg_depth"]}%</span></div>')
        trows.append([
            H.esc(r["category_normalized"]), f'<span class="m">{r["brands"]}</span>',
            f'<span class="m">{int(r["prods"]):,}</span>', bar,
            f'<span class="m">{int(r["esc"])}</span>',
            f'<span class="m">{int(r["dead"])}</span>',
            f'<span class="dist {r["distress"]}">{r["distress"].upper()}</span>',
            H.conf_pill(int(r["brands"])),
        ])
    tbl = H.table(
        [("Category", False), ("Brands", True), ("On discount", True),
         ("Avg depth", False), ("Escalating", True), ("Dead stock", True),
         ("Distress", False), ("Conf.", False)], trows)
    s1 = H.section("01", "Discount depth & distress, by category",
                   tbl + H.why(
                       "“Escalating” = discount getting deeper without selling. "
                       "“Distress” fuses escalating + dead stock + no restock. Depth is "
                       "honest (vs first-observed price, never the brand’s inflated “original”)."))

    # --- 2 · does discounting clear it ---------------------------------------
    def clr_col(v):
        return "#3F7A4B" if v >= 20 else "#B4820A" if v >= 10 else "#B0413A"
    bars = H.hbars([{"cat": r["category_normalized"], "clear": r["clear"]}
                    for r in rows if pd.notna(r["clear"])],
                   "clear", "cat", unit="%", color_fn=clr_col,
                   caption="% of sell-outs that happened while on discount — higher = discounting moves it")
    s2 = H.section("02", "Does discounting actually clear the category?",
                   bars + H.why(
                       "Read with care: a LOW value can mean discounting isn’t working "
                       "<em>or</em> the category sells at full price without needing one. "
                       "Cross-check distress — low-clear + high-distress is the true "
                       "“discounting isn’t rescuing this” signal."))

    # --- 3 · clear-rate curve (maturing) -------------------------------------
    curve = R.latest(conn, "signal_l2_clear_rate_by_depth")
    if not curve.empty:
        cells = len(curve)
        zero = int((curve["clear_rate_pct"] == 0).sum())
        overall = round(100.0 * curve["cleared"].sum() / max(1, curve["products"].sum()), 1)
        mat = H.maturing(
            "Not yet reliable — filling in as history accumulates.",
            f"The signal that answers “what discount depth actually clears?” is "
            f"<b>live and computing daily</b>, but needs more witnessed clearance history. "
            f"Right now {zero} of {cells} category×depth cells have too few clearances to "
            f"measure (overall only {overall}% of eligible items cleared within the window), "
            f"so publishing a curve would mislead. It sharpens on its own — no action needed.")
    else:
        mat = H.maturing("Not yet computing.",
                         "signal_l2_clear_rate_by_depth is empty — it will populate on the next runs.")
    s3 = H.section("03", "Clear-rate by discount depth", mat + H.why(
        "Deliberate restraint: an earlier version looked confident but was biased by how "
        "long items had been on sale. The honest signal says “not enough evidence yet.”"),
        badge="MATURING")

    # --- 4 · coverage --------------------------------------------------------
    s4 = H.section("04", "Coverage & confidence", H.coverage([
        ("<b>Discount + distress:</b> deep price history (reliable)", "normal"),
        ("excludes tree, dalydress (phantom)", "excl"),
        ("thin categories (&lt;5 brands) flagged low-confidence", "normal"),
        ("<b>Clear-rate curve:</b> maturing — see §3", "normal"),
    ]))

    body = v + s1 + s2 + s3 + s4
    return H.write("what-to-discount", "Khabar — What to Discount",
                   "Weekly · market discount posture", body)


if __name__ == "__main__":
    run()
