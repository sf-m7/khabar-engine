"""
report_order.py — WEEKLY "What to Order".
================================================================================
Cross-brand under-supply (where demand outruns supply) fused with best-seller
records (proven demand). Their overlap = low-hanging fruit: proven sellers the
market is also short on. By category x size, plus styles (subcategory) and
colours clearing fastest.

Data via report_lib: market_undersupply() (now sourced from raw stockout_events
-> ~20 brands), demand_grid() (any grain, colour normalised), bestsellers().
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
        return H.write("what-to-order", "Khabar \u2014 What to Order",
                       "Weekly \u00b7 cross-brand under-supply", body)

    m = m[m["market_stockouts"] >= MIN_STOCKOUTS].sort_values(
        "market_stockouts", ascending=False)
    rows = m.head(15).to_dict("records")
    top = rows[0]

    sub = R.demand_grid(conn, ["category_normalized", "subcategory"], SUB_FLOOR)
    col = R.demand_grid(conn, ["category_normalized", "color"], COL_FLOOR)
    bs = R.bestsellers(conn, ["category_normalized", "subcategory"])

    tier_cls = {"confirmed": "hi", "directional": "md", "maturing": "lo"}

    # --- verdict -------------------------------------------------------------
    lhf_names = ""
    if bs is not None and not bs.empty and sub is not None and not sub.empty:
        key = ["category_normalized", "subcategory"]
        merged = bs.merge(sub[key + ["stockouts"]], on=key, how="inner")
        if not merged.empty:
            r0 = merged.sort_values("bestsellers", ascending=False).iloc[0]
            lhf_names = (f" The clearest low-hanging fruit is "
                         f"<b>{H.esc(r0['category_normalized'])} \u00b7 {H.esc(r0['subcategory'])}</b> "
                         f"\u2014 a proven best-seller the market is short on.")
    v = H.verdict(
        "The decision this report answers",
        f"The market is most under-served in "
        f"<b>{H.esc(top['category_normalized'])} \u00b7 size {H.esc(top['stocked_out_size'])}</b>.",
        f"{int(top['brands'])} brands are selling out of it across "
        f"{int(top['products'])} products.{lhf_names} These are your strongest buy candidates.")

    # --- 1 · category x size buy candidates ---------------------------------
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
                       "Ranked by market-wide sell-out pressure across ~20 stock-visible brands. "
                       "Sell-outs is brand-weighted \u2014 read beside brand count. Inventory history "
                       "is young; treat as directional."))

    # --- 2 · which styles (subcategory) -------------------------------------
    if sub is not None and not sub.empty:
        srows = [[
            H.esc(r["category_normalized"]), H.esc(r["subcategory"]),
            f'<span class="m">{int(r["products"])}</span>',
            f'<span class="m">{int(r["stockouts"]):,}</span>',
            H.pill(tier_cls.get(r["confidence"], "md"), r["confidence"]),
        ] for r in sub.head(14).to_dict("records")]
        stbl = H.table([("Category", False), ("Subcategory", False),
                        ("Products", True), ("Sell-outs", True), ("Conf.", False)], srows)
        s2 = H.section("02", "Which styles are clearing \u2014 by subcategory",
                       stbl + H.why(
                           "The specific styles the market can't keep in stock (oversized vs basic "
                           "tees, wide-leg vs chino). Subcategory is recorded for ~half of products."))
    else:
        s2 = H.section("02", "Which styles are clearing \u2014 by subcategory",
                       H.why("Not enough subcategory-tagged sell-outs to rank this week."))

    # --- 3 · proven sellers & low-hanging fruit -----------------------------
    if bs is not None and not bs.empty:
        lhf_html = ""
        if sub is not None and not sub.empty:
            key = ["category_normalized", "subcategory"]
            merged = bs.merge(sub[key + ["stockouts"]], on=key, how="inner")
            if not merged.empty:
                lhf = merged.sort_values("bestsellers", ascending=False).head(6)
                items = ", ".join(
                    f"<b>{H.esc(r['category_normalized'])} \u00b7 {H.esc(r['subcategory'])}</b>"
                    for r in lhf.to_dict("records"))
                lhf_html = (f'<p style="font-size:13.5px;margin:0 0 10px;color:var(--act)">'
                            f'<b>Low-hanging fruit</b> \u2014 proven sellers the market is also short on: '
                            f'{items}.</p>')
        brows = [[H.esc(r["category_normalized"]), H.esc(r["subcategory"]),
                  f'<span class="m">{int(r["bestsellers"])}</span>']
                 for r in bs.head(12).to_dict("records")]
        btbl = H.table([("Category", False), ("Subcategory", False), ("Best-sellers", True)], brows)
        s3 = H.section("03", "Proven sellers & low-hanging fruit", lhf_html + btbl + H.why(
            "Best-sellers = products on brands' own best-seller lists this week (proven demand). "
            "Where a proven seller is also under-supplied (\u00a72), that's the safest bet \u2014 real "
            "demand, few brands serving it."))
    else:
        s3 = H.section("03", "Proven sellers & low-hanging fruit",
                       H.why("No best-seller data this week."))

    # --- 4 · which colours (headline category) ------------------------------
    if col is not None and not col.empty:
        top_cat = col.groupby("category_normalized")["stockouts"].sum().idxmax()
        cc = col[col["category_normalized"] == top_cat].head(12)
        items = [{"color": r["color"], "stockouts": int(r["stockouts"])}
                 for r in cc.to_dict("records")]
        bars = H.hbars(items, "stockouts", "color", unit="",
                       caption=f"sell-outs by colour within {top_cat} (normalised; 'other' = unmapped)")
        s4 = H.section("04", f"Which colours are clearing \u2014 {H.esc(top_cat)}",
                       bars + H.why(
                           "Colour demand inside the market's hottest category. Normalised to ~18 "
                           "canonical names; 'other' is the unmapped tail, shown honestly."))
    else:
        s4 = H.section("04", "Which colours are clearing",
                       H.why("Not enough colour-tagged sell-outs to rank this week."))

    # --- 5 · coverage --------------------------------------------------------
    s5 = H.section("05", "Coverage & confidence", H.coverage([
        ("<b>Demand signals:</b> ~20 brands (from raw stock events)", "normal"),
        ("excludes lc_waikiki, defacto, mobaco (stock not real), tree, dalydress (phantom)", "excl"),
        ("subcategory: ~49% of products tagged", "normal"),
        ("colour: normalised, 'other' = unmapped tail", "normal"),
        ("inventory history young \u2014 directional", "normal"),
    ]))

    body = v + s1 + s2 + s3 + s4 + s5
    return H.write("what-to-order", "Khabar \u2014 What to Order",
                   "Weekly \u00b7 cross-brand under-supply", body)


if __name__ == "__main__":
    run()
