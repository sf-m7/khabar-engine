"""
report_price.py — WEEKLY "How to Price" (Phase 1, brand-blind).
================================================================================
The market's price ladder per category — where prices actually sit, so a brand
can see its headroom. Brand-blind by design (Phase 2 overlays the client's own
price to produce raise/hold/cut verdicts).

Uses per-category price BANDS (P25 / median / P75), not global brand-tiers:
global tiers invert (a premium brand's cheap t-shirts drag its "tier" median
below mid), so bands are the honest, inversion-free read.
"""

import report_lib as R
import report_html as H

MIN_PRODUCTS = 40   # a category needs enough live prices to band reliably
CATS = ("shirts", "t-shirts", "trousers", "jeans", "dresses", "shorts",
        "skirts", "sweaters")


def run():
    conn = R.connect()

    # per-category price bands from the latest snapshot; phantom brands dropped
    # in SQL via a literal set kept in sync with ref_excluded_brands (price-based
    # → scope 'all'). Aggregation is over prices so it can't be done post-hoc.
    df = R.df_sql(conn, """
        WITH latest AS (
          SELECT p.category_normalized AS cat, ps.price
          FROM price_snapshots ps
          JOIN products p ON p.id = ps.product_id
          WHERE ps.snapshot_date = (SELECT max(snapshot_date) FROM price_snapshots)
            AND ps.price > 0
            AND ps.brand NOT IN ('tree','dalydress')
            AND p.category_normalized IS NOT NULL
            AND p.category_normalized NOT IN ('uncategorized','')
        )
        SELECT cat,
               count(*) AS n,
               round(percentile_cont(0.25) WITHIN GROUP (ORDER BY price)) AS p25,
               round(percentile_cont(0.50) WITHIN GROUP (ORDER BY price)) AS med,
               round(percentile_cont(0.75) WITHIN GROUP (ORDER BY price)) AS p75
        FROM latest GROUP BY cat
    """)

    df = df[(df["n"] >= MIN_PRODUCTS) & (df["cat"].isin(CATS))].copy()
    if df.empty:
        body = H.section("01", "Price ladder", H.why("Not enough live prices to band this week."))
        return H.write("how-to-price", "Khabar — How to Price",
                       "Weekly · market price ladder (brand-blind)", body)

    df["spread"] = df["p75"] - df["p25"]
    df = df.sort_values("med", ascending=False)
    rows = df.to_dict("records")
    widest = max(rows, key=lambda r: r["spread"])

    # --- verdict -------------------------------------------------------------
    v = H.verdict(
        "The decision this report answers — Phase 1 (brand-blind)",
        "Where the market prices each category — and where there's room to move.",
        f"Median prices run from <b>{int(min(r['med'] for r in rows))} EGP</b> to "
        f"<b>{int(max(r['med'] for r in rows))} EGP</b>. Widest positioning room is in "
        f"<b>{H.esc(widest['cat'])}</b> ({int(widest['p25'])}–{int(widest['p75'])} EGP). "
        f"Your own price overlays here in Phase 2 → raise / hold / cut.")

    # --- 1 · price band ladder ----------------------------------------------
    band_items = [{"label": r["cat"], "lo": r["p25"], "med": r["med"], "hi": r["p75"]}
                  for r in rows]
    ladder = H.bands(band_items,
                     caption="each bar = middle 50% of market prices (P25–P75) · dot = median EGP")
    s1 = H.section("01", "Market price ladder, by category (EGP)",
                   ladder + H.why(
                       "A wide bar = lots of pricing latitude in that category; a tight bar = "
                       "commoditised. Prices are live market prices, honest (not discount-inflated)."))

    # --- 2 · colour & size price levers (maturing) --------------------------
    s2 = H.section("02", "Does colour or size move price here?", H.maturing(
        "Coming as the colour dimension lands.",
        "Whether beige commands more than black, or XL more than M, is a real pricing lever — "
        "but colour isn’t carried in the price layer yet (planned). Size rarely moves price in "
        "this market. This panel fills in without changing the ladder above."), badge="MATURING")

    # --- 3 · coverage + Phase-2 nudge ---------------------------------------
    s3 = H.section("03", "Coverage & confidence", H.coverage([
        ("<b>Prices:</b> live market prices (reliable)", "normal"),
        ("excludes tree, dalydress (phantom)", "excl"),
        (f"categories with &lt;{MIN_PRODUCTS} live prices hidden", "normal"),
        ("<b>Phase 2:</b> share your prices → personal raise/hold/cut", "normal"),
    ]))

    body = v + s1 + s2 + s3
    return H.write("how-to-price", "Khabar — How to Price",
                   "Weekly · market price ladder (brand-blind)", body)


if __name__ == "__main__":
    run()
