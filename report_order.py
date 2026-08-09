"""
report_order.py — WEEKLY "What to Order".
================================================================================
The cross-brand under-supply read no single brand's POS can produce: where the
market's demand is outrunning its supply, by category x size. Ranked buy
candidates, with the production blueprint's own "increase" verdict alongside.

Data via report_lib.market_undersupply() (which applies P0 exclusions + the
per-cell confidence tier). Gender + colour are a later dimension (maturing).
"""

import pandas as pd
import report_lib as R
import report_html as H

MIN_STOCKOUTS = 5


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

    # --- verdict -------------------------------------------------------------
    inc_txt = (f", and the production blueprint already flags "
               f"{int(top['brands_increase'])} of them to make more."
               if top.get("brands_increase") else ".")
    v = H.verdict(
        "The decision this report answers",
        f"The market is most under-served in "
        f"<b>{H.esc(top['category_normalized'])} · size {H.esc(top['stocked_out_size'])}</b>.",
        f"{int(top['brands'])} brands are selling out of it across "
        f"{int(top['products'])} products{inc_txt} These are your strongest buy "
        f"candidates — demand outrunning supply market-wide.")

    # --- 1 · ranked buy candidates ------------------------------------------
    tier_cls = {"confirmed": "hi", "directional": "md", "maturing": "lo"}
    trows = []
    for r in rows:
        trows.append([
            H.esc(r["category_normalized"]),
            f'<span class="m">{H.esc(r["stocked_out_size"])}</span>',
            f'<span class="m">{int(r["brands"])}</span>',
            f'<span class="m">{int(r["market_stockouts"]):,}</span>',
            f'<span class="m">{int(r["products"])}</span>',
            f'<span class="m">{int(r["brands_increase"])}</span>',
            H.pill(tier_cls.get(r["confidence"], "md"), r["confidence"]),
        ])
    tbl = H.table(
        [("Category", False), ("Size", True), ("Brands", True), ("Sell-outs", True),
         ("Products", True), ("Blueprint: make more", True), ("Conf.", False)], trows)
    s1 = H.section(
        "01", "Where demand is outrunning supply — strongest buy candidates",
        tbl + H.why(
            "Ranked by market-wide sell-out pressure. Sell-outs is brand-weighted — "
            "read it beside brand count. “Make more” = how many brands the production "
            "blueprint independently flags to increase. Inventory history is young; "
            "treat as directional."))

    # --- 2 · gender + colour (maturing) -------------------------------------
    s2 = H.section("02", "Gender & colour breakdown", H.maturing(
        "Coming as the data deepens.",
        "The under-supply read is category × size today. Splitting it by gender and "
        "colour — so you can see it’s <em>women’s beige</em> knitwear specifically — needs "
        "those dimensions carried through the signal layer (planned). It fills in "
        "without changing this report’s shape."), badge="MATURING")

    # --- 3 · coverage --------------------------------------------------------
    s3 = H.section("03", "Coverage & confidence", H.coverage([
        ("<b>Size / stock signals:</b> 21 brands", "normal"),
        ("excludes lc_waikiki, defacto (stock not real), tree, dalydress (phantom)", "excl"),
        ("<b>Inventory history:</b> young — directional", "normal"),
        ("gender & colour: maturing — see §2", "normal"),
    ]))

    body = v + s1 + s2 + s3
    return H.write("what-to-order", "Khabar — What to Order",
                   "Weekly · cross-brand under-supply", body)


if __name__ == "__main__":
    run()
