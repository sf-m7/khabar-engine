"""
report_gap_map.py — MONTHLY "Brand Gap Map": price position (hot+cold), whitespace,
timing, honesty. "Where is money being left on the table, and who is beatable."
Underpricing uses the FULL lake (R2 + Supabase) for a stable price level, not a
single hot day. Kept short — one decision per section.
"""

import pandas as pd
import report_lib as R

UNCAT = "uncategorized"


def run():
    conn = R.connect()
    rep = R.Report(
        slug="gap-map",
        title="Khabar — Brand Gap Map",
        cadence="monthly",
        subtitle="Monthly · price position, category whitespace, discount timing & honesty",
    )

    # 1) UNDERPRICING — sustained brand price vs market, over the whole lake ---
    rep.h2("Who is leaving money on the table (price position)")
    prices, src = R.lake_price_history(days=45)
    prices = R.drop_excluded(prices)
    if not prices.empty:
        prices = prices[prices["category_normalized"].notna()
                        & (prices["category_normalized"] != UNCAT)]
    if not prices.empty:
        prices = prices.rename(columns={"category_normalized": "cat"})
        # sustained price per product = median over the window (ignores flash days)
        pp = prices.groupby(["product_id", "brand", "cat"], as_index=False)["price"].median()
        bc = (pp.groupby(["brand", "cat"])
                .agg(n=("price", "size"), bmed=("price", "median")).reset_index())
        bc = bc[bc["n"] >= 15]
        mkt = (pp.groupby("cat")
                 .agg(brands=("brand", "nunique"), mmed=("price", "median")).reset_index())
        mkt = mkt[mkt["brands"] >= 4]
        m = bc.merge(mkt, on="cat")
        m["vs_market_pct"] = (100 * (m["bmed"] - m["mmed"]) / m["mmed"]).round(1)
        m["bmed"] = m["bmed"].round(); m["mmed"] = m["mmed"].round()

        # actionable band: meaningfully cheaper but still a like-for-like read.
        real = m[(m["vs_market_pct"] <= -15) & (m["vs_market_pct"] >= -45)] \
                 .sort_values("vs_market_pct").head(5)
        verify = m[m["vs_market_pct"] < -45].sort_values("vs_market_pct").head(3)

        if not real.empty:
            rep.table(real, cols=["brand", "cat", "bmed", "mmed", "vs_market_pct"],
                      headers=["Brand", "Category", "Brand median", "Market median", "vs market %"])
            img = R.bar(rep.run_dir, "underpricing",
                        (real["brand"] + " · " + real["cat"]).tolist(),
                        real["vs_market_pct"].tolist(),
                        "Furthest below market (actionable band)", color=R.AMBER, xlabel="% vs market")
            rep.img(img, "Priced below peers with room to raise.")
            tier, stamp = R.confidence(brands=int(m["brands"].max()), events=int(m["n"].sum()))
            t = real.iloc[0]
            rep.do(f"**{t['brand']} · {t['cat']}** sits {abs(t['vs_market_pct']):.0f}% under the "
                   f"market median — the clearest margin test on the board.")
            rep.action(f"Test a price rise: {t['brand']} / {t['cat']} "
                       f"({t['vs_market_pct']:.0f}% below peers).", stamp, 1)
        if not verify.empty:
            rep.p("\n_Flagged to verify (>45% gap — likely a category-mix mismatch, not true "
                  "underpricing): "
                  + ", ".join(f"{r.brand}·{r.cat}" for r in verify.itertuples()) + "._")
        rep.note(f"Price source: {src}. Sustained price = median per product over the window; "
                 f"brand median vs market median per category (≥15 products/brand, ≥4 brands). "
                 f"Absolute price, not like-for-like product matching.")
    else:
        rep.gap("Price position", "insufficient overlapping price/category data")

    # 2) WHITESPACE & DOMINANCE ---------------------------------------------
    sl = R.drop_excluded(R.latest(conn, "signal_l2_share_of_launch"))
    if not sl.empty:
        dom = sl.sort_values("share_pct", ascending=False).head(5)
        tier, stamp = R.confidence(brands=sl["brand"].nunique(),
                                   events=int(sl["brand_launches"].sum()))
        rep.h2("Category whitespace & who owns the launch flow")
        rep.table(dom, cols=["category_normalized", "brand", "share_pct", "launch_read"],
                  headers=["Category", "Brand", "Share of launches %", "Read"])
        rep.do("Single-brand-dominated categories are saturated — enter only with a "
               "differentiated product. Categories with no dominant launcher are whitespace.")
        rep.action("Target unclaimed categories; avoid head-on entry into dominated ones.",
                   stamp, 2)
        rep.note("Share of launch = brand's new SKUs / all brands' new SKUs in the category, "
                 "last 30 days.")

    # 3) TIMING -------------------------------------------------------------
    fm = R.drop_excluded(R.latest(conn, "signal_l2_first_mover"))
    if not fm.empty:
        movers = fm.sort_values("avg_days_after_category_first").head(5)
        tier, stamp = R.confidence(brands=fm["brand"].nunique(), events=len(fm))
        rep.h2("Who moves first on discounts")
        rep.table(movers, cols=["category_normalized", "brand",
                                "avg_days_after_category_first", "mover_read"],
                  headers=["Category", "Brand", "Days after category first", "Read"])
        rep.do("Leading discounts (day 0) trains your own shopper to wait — follow instead. "
               "Always following late cedes first-mover sell-through — move earlier.")
        rep.action("Re-time discounts against the category's first mover.", stamp, 2)

    # 4) HONESTY ------------------------------------------------------------
    dh = R.drop_excluded(R.latest(conn, "signal_l2_discount_honesty"))
    if not dh.empty and "manufactured_gap_pct" in dh.columns:
        inflated = (dh[dh["manufactured_gap_pct"].fillna(0) > 5]
                    .sort_values("manufactured_gap_pct", ascending=False).head(5))
        if not inflated.empty:
            tier, stamp = R.confidence(brands=dh["brand"].nunique(),
                                       events=int(dh["scored_events"].sum()))
            rep.h2("Fake-discount flags")
            rep.table(inflated, cols=["brand", "category_normalized",
                                      "avg_claimed_depth_pct", "avg_genuine_depth_pct",
                                      "manufactured_gap_pct"],
                      headers=["Brand", "Category", "Claimed %", "Genuine %", "Gap %"])
            rep.do("These advertise deeper discounts than the real drop from baseline. "
                   "Position honest pricing against them; never benchmark RRP to their anchor.")
            rep.action("Use inflated-anchor brands as a positioning contrast.", stamp, 3)
            rep.note("Honesty = advertised depth vs genuine drop from first-observed price. "
                     "Only brands publishing an RRP are scored.")

    rep.write()
    conn.close()


if __name__ == "__main__":
    run()
