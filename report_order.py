"""
report_order.py — WEEKLY "What to Order".
================================================================================
Leads with a FUSED buy board (report_lib.order_verdicts): each opportunity gets
a verdict (STRONG BUY / BUY / WATCH), the reasons (why), and the timing (when) —
fusing under-supply + proven best-seller demand + warming/cooling trend. Catches
the trap of chasing an under-supplied style that is actually cooling. Supporting
detail follows: category x size, colours, coverage.
"""

import report_lib as R
import report_html as H

MIN_STOCKOUTS = 5


def run():
    conn = R.connect()
    fused = R.order_verdicts(conn, min_stockouts=30)
    m = R.market_undersupply(conn)
    col = R.demand_grid(conn, ["category_normalized", "color"], 15)

    if (fused is None or fused.empty) and (m is None or m.empty):
        body = H.section("01", "Under-supply", H.why("No under-supply cells this week."))
        return H.write("what-to-order", "Khabar \u2014 What to Order",
                       "Weekly \u00b7 cross-brand under-supply", body)

    vmap = {"STRONG BUY": "#3F7A4B", "BUY": "#B45309", "WATCH": "#9A978F"}
    when = {"warming": "act now \u2014 demand rising", "cooling": "window closing \u2014 cooling",
            "steady demand": "stable", "steady": "stable"}

    # --- verdict -------------------------------------------------------------
    if fused is not None and not fused.empty:
        t0 = fused.iloc[0]
        strong = [r for r in fused.to_dict("records") if r["verdict"] == "STRONG BUY"]
        head = (f"Top buy: <b>{H.esc(t0['category_normalized'])} \u00b7 {H.esc(t0['subcategory'])}</b> "
                f"\u2014 {t0['verdict'].lower()}.")
        sub = (f"{len(strong)} styles rate STRONG BUY (proven sellers, under-supplied, not cooling). "
               f"The board ranks each opportunity by demand, proof, and timing \u2014 and flags the "
               f"under-supplied styles that are actually cooling, so you don't chase a fading trend.")
    else:
        head, sub = "Cross-brand under-supply.", "Ranked buy candidates by category and size."
    v = H.verdict("The decision this report answers", head, sub)

    body = v

    # --- 1 · THE BUY BOARD (fused) ------------------------------------------
    if fused is not None and not fused.empty:
        brows = []
        for r in fused.head(12).to_dict("records"):
            vc = vmap.get(r["verdict"], "#6C6A64")
            why = " \u00b7 ".join(r["why"])
            brows.append([
                f'{H.esc(r["category_normalized"])} \u00b7 <b>{H.esc(r["subcategory"])}</b>',
                f'<span class="m" style="color:{vc};font-weight:700">{r["verdict"]}</span>',
                f'<span style="font-size:12px">{H.esc(why)}</span>',
                f'<span class="m" style="font-size:11px">{H.esc(when.get(r["trend"], "stable"))}</span>',
                f'<span class="m">{int(r["stockouts"]):,}</span>',
                f'<span class="m">{int(r["brands"])}</span>',
            ])
        btbl = H.table([("Opportunity", False), ("Verdict", False), ("Why", False),
                        ("When", False), ("Sell-outs", True), ("Brands", True)], brows)
        s1 = H.section("01", "The buy board \u2014 ranked, with why & when", btbl + H.why(
            "Fuses three signals per style: under-supply (sell-out pressure), proven demand "
            "(on brands' best-seller lists), and trend (warming/cooling). STRONG BUY = proven + "
            "under-supplied + not cooling. WATCH = demand without proof, or cooling \u2014 the trap "
            "a plain gap report would tell you to chase."))
        body += s1

    # --- 2 · by category × size ---------------------------------------------
    if m is not None and not m.empty:
        mm = m[m["market_stockouts"] >= MIN_STOCKOUTS].head(12).to_dict("records")
        tier_cls = {"confirmed": "hi", "directional": "md", "maturing": "lo"}
        trows = [[
            H.esc(r["category_normalized"]),
            f'<span class="m">{H.esc(r["stocked_out_size"])}</span>',
            f'<span class="m">{int(r["brands"])}</span>',
            f'<span class="m">{int(r["market_stockouts"]):,}</span>',
            f'<span class="m">{int(r["brands_increase"])}</span>',
            H.pill(tier_cls.get(r["confidence"], "md"), r["confidence"]),
        ] for r in mm]
        tbl = H.table([("Category", False), ("Size", True), ("Brands", True),
                       ("Sell-outs", True), ("Blueprint: make more", True), ("Conf.", False)], trows)
        s2 = H.section("02", "The size dimension \u2014 by category \u00d7 size", tbl + H.why(
            "Which sizes to weight, across ~20 stock-visible brands. Sell-outs is brand-weighted; "
            "read beside brand count."))
        body += s2

    # --- 3 · colours ---------------------------------------------------------
    if col is not None and not col.empty:
        top_cat = col.groupby("category_normalized")["stockouts"].sum().idxmax()
        cc = col[col["category_normalized"] == top_cat].head(12)
        items = [{"color": r["color"], "stockouts": int(r["stockouts"])} for r in cc.to_dict("records")]
        bars = H.hbars(items, "stockouts", "color", unit="",
                       caption=f"sell-outs by colour within {top_cat} (normalised; 'other' = unmapped)")
        s3 = H.section("03", f"Which colours are clearing \u2014 {H.esc(top_cat)}", bars + H.why(
            "Colour demand inside the hottest category. Normalised to ~18 canonical names; "
            "'other' is the unmapped tail, shown honestly."))
        body += s3

    # --- 4 · coverage --------------------------------------------------------
    s4 = H.section("04", "Coverage & confidence", H.coverage([
        ("<b>Demand:</b> ~20 brands (raw stock events)", "normal"),
        ("<b>Proven:</b> brands' own best-seller lists", "normal"),
        ("<b>Trend:</b> ~weeks of history \u2014 directional", "normal"),
        ("excludes lc_waikiki, defacto, mobaco (stock), tree, dalydress (phantom)", "excl"),
        ("subcategory ~49% tagged; colour normalised", "normal"),
    ]))
    body += s4

    return H.write("what-to-order", "Khabar \u2014 What to Order",
                   "Weekly \u00b7 cross-brand under-supply", body)


if __name__ == "__main__":
    run()
