"""
report_bestseller.py — WEEKLY "Market Demand & Stockout".
The market-wide view a brand's own POS can't produce: how fast the whole market
is clearing, what's genuinely selling out, and where the next buy should go.
Reads signal tables (already computed from the full hot+cold lake). Kept short
on purpose — a client skims it in two minutes.
"""

import pandas as pd
import report_lib as R

UNCAT = "uncategorized"


def run():
    conn = R.connect()
    rep = R.Report(
        slug="market-demand-stockout",
        title="Khabar — Market Demand & Stockout",
        cadence="weekly",
        subtitle="Weekly · witnessed sellouts & restock behaviour, all tracked brands",
    )

    # 1) MARKET VELOCITY — latest snapshot only (avoids double-counted weeks)
    mv = R.latest(conn, "signal_l2_market_velocity")
    if not mv.empty:
        mv = mv.sort_values("week_start")
        last = mv.iloc[-1]
        wow = last.get("wow_pct")
        tier, stamp = R.confidence(days=len(mv) * 7, events=int(mv["stockouts"].sum()))
        rep.h2("Is the market speeding up or slowing down?")
        rep.p(f"**{last['velocity_read']}**"
              + (f" — {wow:+.0f}% week-over-week in sellouts." if pd.notna(wow) else "."))
        img = R.lines(rep.run_dir, "market_velocity",
                      x=[str(d)[:10] for d in mv["week_start"]],
                      series={"Sellouts": (mv["stockouts"].tolist(), R.RUST),
                              "Restocks": (mv["restocks"].tolist(), R.TEAL)},
                      title="Sellouts vs restocks, by week", ylabel="events")
        rep.img(img, "Weekly sellouts vs restocks across all brands.")
        if "accelerating" in str(last["velocity_read"]):
            rep.do("Demand is outrunning restocks — protect stock on movers and pull planned "
                   "markdowns forward before rivals clear first.")
            rep.action("Pull key-item markdowns forward — market clearing faster.", stamp, 1)
        elif "slowing" in str(last["velocity_read"]):
            rep.do("Market is cooling — hold discounts; cutting now sheds margin without "
                   "moving more units.")
            rep.action("Hold discounts — market cooling, deeper cuts won't lift volume.", stamp, 1)
        rep.note("Velocity = weekly witnessed sellouts vs restocks, all brands. Young series — "
                 "read direction, not magnitude.")
    else:
        rep.gap("Market velocity", "signal_l2_market_velocity empty")

    # 2) REORDER PRIORITY — high sellout + slow/incomplete restock
    so = R.latest(conn, "signal_l1_08_variant_stockout")
    re = R.latest(conn, "signal_l1_09_variant_restock")
    rep.h2("Where demand is outrunning supply — reorder first")
    if not so.empty:
        so = so[so["category_normalized"] != UNCAT]
        so_agg = (so.groupby("category_normalized", as_index=False)
                    .agg(sellouts=("stockout_events", "sum"),
                         brands=("brand", "nunique"), obs=("observed_days", "max")))
        if not re.empty:
            re = re[re["category_normalized"] != UNCAT]
            re_agg = (re.groupby("category_normalized", as_index=False)
                        .agg(completion=("completion_rate_pct", "mean"),
                             restock_days=("median_restock_days", "median")))
            m = so_agg.merge(re_agg, on="category_normalized", how="left")
        else:
            m = so_agg.assign(completion=pd.NA, restock_days=pd.NA)
        m["chase"] = (m["sellouts"] * (1 - m["completion"].fillna(50) / 100)
                      * (1 + m["restock_days"].fillna(3) / 7)).round(0)
        m = m.sort_values("chase", ascending=False).head(5)
        m["completion"] = m["completion"].round(0)
        m["restock_days"] = m["restock_days"].round(1)
        rep.table(m, cols=["category_normalized", "sellouts", "brands", "completion",
                           "restock_days"],
                  headers=["Category", "Sellouts", "Brands", "Restock completion %",
                           "Median restock days"])
        img = R.bar(rep.run_dir, "reorder", m["category_normalized"].tolist(),
                    m["chase"].tolist(), "Reorder priority", color=R.RUST, xlabel="chase score")
        rep.img(img, "Higher = selling out faster than it comes back.")
        top = m.iloc[0]
        tier, stamp = R.confidence(brands=int(top["brands"]),
                                   days=int(top["obs"]) if pd.notna(top["obs"]) else None,
                                   events=int(m["sellouts"].sum()))
        rep.do(f"Reorder **{', '.join(m['category_normalized'].head(3))}** first — high "
               f"sellouts with slow, incomplete restock is unmet demand, not noise.")
        rep.action(f"Reorder: {', '.join(m['category_normalized'].head(3))}.", stamp, 1)
        rep.note("Chase = sellouts × (1 − restock completion) × restock-lag. Stock-blind "
                 "brands (DeFacto, Mobaco) and LCW per-size excluded.")
    else:
        rep.gap("Reorder priority", "signal_l1_08 empty")

    # 3) SIZE SKEW + 4) CLEARANCE — compact
    sz = R.latest(conn, "signal_l1_11_size_asymmetry")
    if not sz.empty:
        sk = (sz.groupby("stocked_out_size").size()
                .sort_values(ascending=False).head(3))
        tier, stamp = R.confidence(brands=sz["brand"].nunique(), events=len(sz))
        rep.h2("Sizes that sell out first")
        rep.p("Empty while the rest of the run still sits: **"
              + ", ".join(str(s) for s in sk.index) + "**.")
        rep.action(f"Buy deeper in sizes {', '.join(str(s) for s in sk.index)}.", stamp, 2)

    dv = R.drop_excluded(R.latest(conn, "signal_l1_22_discount_velocity"))
    if not dv.empty:
        top = dv.sort_values("skus_dropped", ascending=False).iloc[0]
        tier, stamp = R.confidence(brands=dv["brand"].nunique(),
                                   events=int(dv["skus_dropped"].sum()))
        rep.h2("Clearance wave to watch")
        rep.p(f"**{top['brand']}** is dumping **{top['category_normalized']}** "
              f"({int(top['skus_dropped'])} SKUs at once).")
        rep.do("Don't discount into their wave — hold and let it pass, or match only if you "
               "share the shopper.")
        rep.action(f"Time your move around {top['brand']}'s {top['category_normalized']} "
                   f"clearance.", stamp, 2)

    rep.write()
    conn.close()


if __name__ == "__main__":
    run()
