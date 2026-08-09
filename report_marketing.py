"""
report_marketing.py — WEEKLY "Marketing Timing" (market layer only).
================================================================================
The market-timing overlay for a brand's own promo decision — never an ad budget
(Khabar can't see the client's stock or margins). Ships what's solid today:
the market's overall temperature (velocity trend) and who leads discounts in
each category. Granular per-category × colour warming is maturing — it needs a
per-category velocity signal that isn't built yet.
"""

import pandas as pd
import report_lib as R
import report_html as H


def run():
    conn = R.connect()

    mv = R.latest(conn, "signal_l2_market_velocity")
    fm = R.latest(conn, "signal_l2_first_mover")
    fm = R.drop_excluded(fm) if not fm.empty else fm   # phantom brands out

    # --- verdict -------------------------------------------------------------
    if not mv.empty:
        mv = mv.sort_values("week_start")
        last = str(mv.iloc[-1]["velocity_read"])
        head = ("The market is accelerating — demand outrunning restocks."
                if "accelerating" in last else
                "The market is cooling — demand easing." if "slowing" in last or "cooling" in last
                else "The market is steady.")
    else:
        head = "Market temperature unavailable this week."
    v = H.verdict(
        "The decision this report answers — market timing only (never a budget)",
        head,
        "Use this to time your own promotion. Warming + you hold stock = push; a rival "
        "clearance wave = hold. Khabar can’t see your stock or margins, so it never says "
        "how much to spend.")

    # --- 1 · market temperature ---------------------------------------------
    if not mv.empty:
        wk = [str(d)[5:10] for d in mv["week_start"]]
        chart = H.lines(
            [("Sell-outs", mv["stockouts"].tolist(), "#B4532A"),
             ("Restocks", mv["restocks"].tolist(), "#2f6f6a")],
            wk, title="Weekly sell-outs vs restocks (all brands)",
            caption="sell-outs rising faster than restocks = market heating")
        tier, stamp = R.confidence(days=len(mv) * 7, events=int(mv["stockouts"].sum()))
        s1 = H.section("01", "Market temperature", chart + H.why(
            f"Read: <b>{H.esc(last)}</b>. Direction, not magnitude — the series is young "
            f"({len(mv)} weeks). {H.esc(stamp)}"))
    else:
        s1 = H.section("01", "Market temperature", H.why("signal_l2_market_velocity empty."))

    # --- 2 · discount timing (who leads) ------------------------------------
    if not fm.empty and "mover_read" in fm.columns:
        firsts = fm[fm["mover_read"].str.contains("first mover", case=False, na=False)]
        by_cat = (firsts.groupby("category_normalized")["brand"].nunique()
                  .reset_index(name="leaders").sort_values("leaders", ascending=False).head(10))
        trows = [[H.esc(r["category_normalized"]),
                  f'<span class="m">{int(r["leaders"])}</span>']
                 for r in by_cat.to_dict("records")]
        tbl = H.table([("Category", False), ("Discount leaders", True)], trows)
        s2 = H.section("02", "Discount timing — don't lead the market", tbl + H.why(
            "“Leaders” = brands that consistently discount first in a category. Discounting "
            "before them trains your own shoppers to wait; follow, don’t lead."))
    else:
        s2 = H.section("02", "Discount timing", H.why("first-mover signal empty."))

    # --- 3 · granular warming (maturing) ------------------------------------
    s3 = H.section("03", "Warming & cooling by category × colour", H.maturing(
        "Coming as per-category velocity lands.",
        "The rich version — which category and colour is heating or cooling, confirmed by "
        "price direction — needs a per-category velocity signal (today’s is market-wide only) "
        "and the colour dimension. Both are planned; this fills in without changing the "
        "temperature read above."), badge="MATURING")

    # --- 4 · coverage --------------------------------------------------------
    s4 = H.section("04", "Coverage & confidence", H.coverage([
        ("<b>Market temperature:</b> ~5 weeks — directional", "normal"),
        ("excludes tree, dalydress (phantom)", "excl"),
        ("<b>Not an ad budget</b> — market conditions only", "normal"),
        ("category × colour warming: maturing — see §3", "normal"),
    ]))

    body = v + s1 + s2 + s3 + s4
    return H.write("marketing-timing", "Khabar — Marketing Timing",
                   "Weekly · market-timing overlay", body)


if __name__ == "__main__":
    run()
