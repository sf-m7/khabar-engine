"""
Khabar — ONE-OFF: verify FOP immutability against the R2 cold archive.

WHY THIS EXISTS
Every honest-discount number in the system (l1_10, l1_11, l1_01, l1_17, and
everything in L2 built on them) now depends on first_observed_price being a
true, never-rewritten baseline. That was asserted as a core invariant from
the start of this project but never actually checked against history --
the hot tier only reaches back ~8 days, so there was nothing to check it
against until now. R2 holds the real history.

WHAT IT CHECKS
For a sample of products, compares product_variants.first_observed_price
(the CURRENT value) against the price in the OLDEST snapshot that exists for
that product in the full lake (hot + R2 cold, via khabar_lake). If FOP is
truly immutable, these should match for every product that existed when its
oldest snapshot was taken. Mismatches mean FOP was rewritten after the fact
-- which would undermine every honest-discount signal built today.

HOW TO RUN
Same environment as compute_signals.py -- same secrets, same GitHub Actions
runner. Does not touch Supabase writes; read-only.

    python -u verify_fop_immutability.py

Paste the full output back. No interpretation needed on your end -- just the
raw numbers.
"""

import khabar_lake

con = khabar_lake.connect()

# Full lake history, not just the hot tier -- this is the whole point.
n_hot, n_files, start_day, end_day = khabar_lake.snapshots(con, days=365)
print(f"Lake window checked: {start_day} -> {end_day} ({n_hot:,} snapshot rows, "
      f"{n_files} archive files)")

khabar_lake.materialise_variants(con)

result = con.execute("""
    WITH earliest_snapshot AS (
        SELECT product_id, price, snapshot_date,
               ROW_NUMBER() OVER (
                   PARTITION BY product_id ORDER BY snapshot_date ASC, recorded_at ASC
               ) AS rn
        FROM snapshots
        WHERE price > 0
    ),
    current_fop AS (
        SELECT product_id, MIN(first_observed_price) AS fop
        FROM variants_raw
        WHERE first_observed_price > 0
        GROUP BY product_id
    )
    SELECT
        count(*)                                                        AS products_compared,
        count(*) FILTER (WHERE abs(f.fop - e.price) < 0.01)              AS exact_match,
        count(*) FILTER (WHERE f.fop > e.price + 0.01)                   AS fop_higher_than_earliest_seen,
        count(*) FILTER (WHERE f.fop < e.price - 0.01)                   AS fop_lower_than_earliest_seen,
        round(100.0 * count(*) FILTER (WHERE abs(f.fop - e.price) < 0.01)
              / NULLIF(count(*), 0), 2)                                  AS pct_match
    FROM earliest_snapshot e
    JOIN current_fop f ON f.product_id = e.product_id
    WHERE e.rn = 1
""").fetchone()

print("\n=== FOP IMMUTABILITY CHECK ===")
print(f"Products compared:              {result[0]:,}")
print(f"FOP matches earliest seen price: {result[1]:,}  ({result[4]}%)")
print(f"FOP HIGHER than earliest seen:   {result[2]:,}  <- concerning if large")
print(f"FOP LOWER than earliest seen:    {result[3]:,}  <- concerning if large")

# A few concrete examples of mismatches, if any, to make this inspectable
# rather than just a percentage.
print("\n=== SAMPLE MISMATCHES (up to 10) ===")
rows = con.execute("""
    WITH earliest_snapshot AS (
        SELECT product_id, price, snapshot_date, brand,
               ROW_NUMBER() OVER (
                   PARTITION BY product_id ORDER BY snapshot_date ASC, recorded_at ASC
               ) AS rn
        FROM snapshots
        WHERE price > 0
    ),
    current_fop AS (
        SELECT product_id, MIN(first_observed_price) AS fop
        FROM variants_raw
        WHERE first_observed_price > 0
        GROUP BY product_id
    )
    SELECT e.product_id, e.brand, e.snapshot_date, e.price AS earliest_seen_price, f.fop
    FROM earliest_snapshot e
    JOIN current_fop f ON f.product_id = e.product_id
    WHERE e.rn = 1 AND abs(f.fop - e.price) >= 0.01
    ORDER BY abs(f.fop - e.price) DESC
    LIMIT 10
""").fetchall()

for r in rows:
    print(f"  product {r[0]} ({r[1]}): earliest seen {r[2]} @ {r[3]}, "
          f"current FOP = {r[4]}")

if not rows:
    print("  (none)")

print("\nDone. Paste this entire output back.")
