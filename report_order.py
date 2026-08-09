"""
report_order.py — WEEKLY "What to Order".
================================================================================
The cross-brand under-supply read no single brand's POS can produce: where the
market's demand is outrunning its supply. Ranked buy candidates by category x
size, plus the specific styles (subcategory) and colours clearing fastest.

Data via report_lib: market_undersupply() (category x size + blueprint verdict)
and demand_grid() (any grain — here subcategory and normalised colour). All
exclusions + per-cell confidence come from report_lib (single source).
"""

import report_lib as R
import report_html as H

MIN_STOCKOUTS = 5
SUB_FLOOR = 15
COL_FLOOR = 15


def run():
    conn = R.connect()
    m = R.market_undersupply(conn)

    if m is None or m.empty:
        body = H.section("01", "Under-supply", H.why("No under-supply cells this week."))
        return H.write("what-to-order", "Khabar — What to Order",
                       "Weekly · cross-brand under-supply", body)

    m = m[m["market_stockouts"] >= MIN_STOCKOUTS].sort_values(
        "market_stockouts", ascending=False)
    rows = m.head(15).to_dict("records")
    top = rows[0]

    sub = R.demand_grid(conn, ["category_normalized", "subcategory"], SUB_FLOOR)
    col = R.demand_grid(conn, ["category_normalized", "color"], COL_FLOOR)

    # --- verdict -------------------------------------------------------------
    sub_bit = ""
    if sub is not None and not sub.empty:
        s0 = sub.iloc[0]
        sub_bit = (f" The fastest-clearing style is "
                   f"<b>{H.esc(s0['category_normalized'])} \u00b7 {H.esc(s0['subcategory'])}</b>.")
    v = H.verdict(
        "The decision this report answers",
        f"The market is most under-served in "
        f"<b>{H.esc(top['category_normalized'])} \u00b7 size {H.esc(top['stocked_out_size'])}</b>.",
        f"{int(top['brands'])} brands are selling out of it across "
        f"{int(top['products'])} products.{sub_bit} These are your strongest buy "
        f"candidates \u2014 demand outrunning supply market-wide.")

    # --- 1 · category x size buy candidates ---------------------------------
    tier_cls = {"confirmed": "hi", "directional": "md", "maturing": "lo"}
    trows = [[
        H.esc(r["category_normalized"]),
        f'<span class="m">{H.esc(r["stocked_out_size"])}</span>',
        f'<span class="m">{int(r["brands"])}</span>',
        f'<span class="m">{int(r["market_stockouts"]):,}</span>',
        f'<span class="m">{int(r["products"])}</span>',
        f'<span class="m">{int(r["brands_increase"])}</span>',
        H.pill(tier_cls.get(r["confidence"], "md"), r["confidence"]),
    ] for r in rows]
    tbl = H.table(
        [("Category", False), ("Size", True), ("Brands", True), ("Sell-outs", True),
         ("Products", True), ("Blueprint: make more", True), ("Conf.", False)], trows)
    s1 = H.section("01", "Where demand is outrunning supply \u2014 by category \u00d7 size",
                   tbl + H.why(
                       "Ranked by market-wide sell-out pressure. Sell-outs is brand-weighted \u2014 "
                       "read beside brand count. Inventory history is young; treat as directional."))

    # --- 2 · which styles (subcategory) -------------------------------------
    if sub is not None and not sub.empty:
        srows = [[
            H.esc(r["category_normalized"]),
            H.esc(r["subcategory"]),
            f'<span class="m">{int(r["products"])}</span>',
            f'<span class="m">{int(r["stockouts"]):,}</span>',
            H.pill(tier_cls.get(r["confidence"], "md"), r["confidence"]),
        ] for r in sub.head(14).to_dict("records")]
        stbl = H.table([("Category", False), ("Subcategory", False),
                        ("Products", True), ("Sell-outs", True), ("Conf.", False)], srows)
        s2 = H.section("02", "Which styles are clearing \u2014 by subcategory",
                       stbl + H.why(
                           "The specific styles the market can't keep in stock \u2014 the trend read "
                           "inside each category (oversized vs basic tees, wide-leg vs chino). "
                           "Subcategory is recorded for ~half of products; the rest aren't shown here."))
    else:
        s2 = H.section("02", "Which styles are clearing \u2014 by subcategory",
                       H.why("Not enough subcategory-tagged sell-outs to rank this week."))

    # --- 3 · which colours (headline category) ------------------------------
    if col is not None and not col.empty:
        top_cat = col.groupby("category_normalized")["stockouts"].sum().idxmax()
        cc = col[col["category_normalized"] == top_cat].head(12)
        items = [{"color": r["color"], "stockouts": int(r["stockouts"])}
                 for r in cc.to_dict("records")]
        bars = H.hbars(items, "stockouts", "color", unit="",
                       caption=f"sell-outs by colour within {top_cat} (normalised; 'other' = unmapped)")
        s3 = H.section("03", f"Which colours are clearing \u2014 {H.esc(top_cat)}",
                       bars + H.why(
                           "Colour demand inside the market's hottest category. Colours are "
                           "normalised to ~18 canonical names; 'other' is the unmapped tail, shown "
                           "honestly. Available for any category on request."))
    else:
        s3 = H.section("03", "Which colours are clearing",
                       H.why("Not enough colour-tagged sell-outs to rank this week."))

    # --- 4 · coverage --------------------------------------------------------
    s4 = H.section("04", "Coverage & confidence", H.coverage([
        ("<b>Demand signals:</b> 15 brands (stock-visible)", "normal"),
        ("excludes lc_waikiki, defacto, mobaco (stock not real), tree, dalydress (phantom)", "excl"),
        ("subcategory: ~49% of products tagged", "normal"),
        ("colour: normalised, 'other' = unmapped tail", "normal"),
        ("inventory history young \u2014 directional", "normal"),
    ]))

    body = v + s1 + s2 + s3 + s4
    return H.write("what-to-order", "Khabar \u2014 What to Order",
                   "Weekly \u00b7 cross-brand under-supply", body)


if __name__ == "__main__":
    run()
