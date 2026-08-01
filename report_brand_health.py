"""
report_brand_health.py — MONTHLY "Supply Chain Stress / Brand Health".
A per-brand health read from replenishment speed, availability, markdown distress,
dead stock and discount-training risk. Shows the brands that need attention, not
a wall of every brand. Every number computed here from hot+cold-derived signals.
"""

import pandas as pd
import report_lib as R


def run():
    conn = R.connect()
    rep = R.Report(
        slug="brand-health",
        title="Khabar — Supply Chain Stress & Brand Health",
        cadence="monthly",
        subtitle="Monthly · replenishment, availability, markdown distress & dead stock",
    )

    rb = R.drop_excluded(R.latest(conn, "signal_l2_replenishment_benchmark"))
    rs = R.drop_excluded(R.latest(conn, "signal_l1_09_variant_restock"))
    de = R.drop_excluded(R.latest(conn, "signal_l1_17_depth_escalation"))
    ds = R.drop_excluded(R.latest(conn, "signal_l1_10_dead_stock"))
    tc = R.drop_excluded(R.latest(conn, "signal_l2_trained_customer"))

    brands = set()
    for d in (rb, rs, de, ds, tc):
        if not d.empty and "brand" in d.columns:
            brands |= set(d["brand"].unique())
    if not brands:
        rep.gap("Brand health scorecard", "no brand-health signals populated")
        rep.write(); conn.close(); return

    rows = []
    for b in sorted(brands):
        row = {"brand": b}
        if not rb.empty:
            r = rb[rb["brand"] == b]
            row["replen"] = float(r["vs_market_ratio"].iloc[0]) if len(r) else None
        if not rs.empty:
            r = rs[rs["brand"] == b]
            row["avail"] = round(r["completion_rate_pct"].mean()) if len(r) else None
        row["distress"] = int((de["brand"] == b).sum()) if not de.empty else 0
        row["dead"] = int((ds["brand"] == b).sum()) if not ds.empty else 0
        if not tc.empty:
            r = tc[tc["brand"] == b]
            row["risk"] = str(r["risk_read"].iloc[0]).split(" —")[0] if len(r) else None
        rows.append(row)
    sc = pd.DataFrame(rows)

    def flags(r):
        f = 0
        if pd.notna(r.get("replen")) and r["replen"] >= 1.5: f += 1
        if pd.notna(r.get("avail")) and r["avail"] < 40: f += 1
        if r.get("distress", 0) >= 20: f += 1
        if r.get("dead", 0) >= 15: f += 1
        if isinstance(r.get("risk"), str) and r["risk"].startswith("high"): f += 1
        return f
    sc["flags"] = sc.apply(flags, axis=1)
    sc["health"] = sc["flags"].map(lambda f: "🔴 stress" if f >= 3 else "🟡 watch" if f == 2 else "🟢 ok")

    # show only brands that need attention; summarise the healthy rest in one line
    attention = sc[sc["flags"] >= 2].sort_values("flags", ascending=False)
    healthy_n = int((sc["flags"] < 2).sum())

    rep.h2("Brands that need attention")
    if not attention.empty:
        show = attention.copy()
        show["replen"] = show["replen"].round(2)
        rep.table(show, cols=["brand", "health", "replen", "avail", "distress", "dead", "risk"],
                  headers=["Brand", "Health", "Replen vs mkt (×)", "Availability %",
                           "Distress markdowns", "Dead stock", "Training risk"])
        for _, r in attention[attention["flags"] >= 3].iterrows():
            why = []
            if pd.notna(r.get("replen")) and r["replen"] >= 1.5:
                why.append(f"restocks {r['replen']:.1f}× slower than market")
            if pd.notna(r.get("avail")) and r["avail"] < 40:
                why.append(f"only {r['avail']:.0f}% of sellouts return")
            if r.get("distress", 0) >= 20:
                why.append(f"{r['distress']} escalating markdowns")
            if r.get("dead", 0) >= 15:
                why.append(f"{r['dead']} dead-stock lines")
            rep.action(f"**{r['brand']}** under stress — {'; '.join(why)}.", priority=1)
    else:
        rep.p("No brand crossed two stress flags this month.")
    rep.p(f"\n_{healthy_n} other brands read healthy and are omitted for brevity._")

    if not rb.empty:
        rbx = rb.sort_values("vs_market_ratio", ascending=False).head(8)
        img = R.bar(rep.run_dir, "replen_vs_market", rbx["brand"].tolist(),
                    rbx["vs_market_ratio"].tolist(),
                    "Replenishment vs market (×, >1 = slower)", color=R.RUST, xlabel="× market")
        rep.img(img, "Brands restocking slower than the market median.")
        tier, stamp = R.confidence(brands=rb["brand"].nunique(), cycles=int(rb["cycles"].sum()))
        rep.do("Brands well above 1× restock materially slower than rivals — a supply "
               "constraint a client can be benchmarked against.")
        rep.action("Benchmark the client's restock lag against the slow brands here.", stamp, 2)

    rep.note("Health = count of stress flags (slow replen, low availability, distress "
             "markdowns, dead stock, high training risk): 3+ = stress, 2 = watch.")
    rep.write()
    conn.close()


if __name__ == "__main__":
    run()
