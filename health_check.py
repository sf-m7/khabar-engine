"""
Khabar — daily health check.
================================================================================
WHY THIS EXISTS

Three failures ran silently for weeks before a client-facing question exposed
them:

  * LC Waikiki recorded ZERO discounted prices across ~100,000 observations,
    because an API flag it depended on quietly disappeared.
  * The weekly witnessed-event classifier crashed on every run for weeks. The
    caller wrapped it in try/except and printed a warning, so the job kept
    reporting success.
  * Nineteen Shopify bestseller feeds started returning HTTP 429 and stopped
    collecting anything. The run still finished green.

None of these were subtle once you looked. All three shared the same shape: a
number that had been healthy for weeks went to zero, and nothing compared it
to yesterday. Exit codes stayed clean because nothing crashed — the pipeline
faithfully processed nothing at all.

So this file exists to answer one question every day: does the data look like
it did yesterday, and if not, is there a reason?

--------------------------------------------------------------------------------
DESIGN

  1. RELATIVE, NOT ABSOLUTE. Thresholds compare each brand against its own
     recent median rather than a hardcoded number. A hardcoded "expect 11,000
     LCW rows" is wrong the moment the catalog grows, and a threshold that is
     wrong gets ignored, and a check that gets ignored is worse than no check.

  2. FAILS THE BUILD. Exits non-zero on any FAIL, so GitHub marks the run red
     and emails. A warning printed into a 5,000-line log is not monitoring —
     that is precisely how all three failures survived.

  3. WARN vs FAIL. WARN is for things that are often legitimate (a quiet
     market day). FAIL is for things that are never legitimate (an entire
     brand's discount capture at zero). Crying wolf trains people to ignore
     the alarm, which recreates the original problem in a new costume.

  4. CHECKS THE OUTPUT, NOT THE CODE PATH. Every check reads what actually
     landed in the database. A check that asks "did the function run" would
     have passed on all three failures above.

Read-only. It cannot modify anything.
"""

import os
import sys
from datetime import date, timedelta

import psycopg2
from khabar_db import CA_BUNDLE
import psycopg2.extras

DB_URL = os.environ.get("KHABAR_DB_URL", "").strip() or os.environ["SUPABASE_DB_URL"]

# A brand needs some history before "compare against its own median" means
# anything. Newer brands are reported but never fail the build.
MIN_DAYS_FOR_BASELINE = 7

results = []  # (status, check_name, detail)


def record(status, name, detail):
    results.append((status, name, detail))


def q(cur, sql, args=None):
    """
    Bug fixed 2026-07-25: the original body was `cur.execute(sql, args or ())`.
    `args or ()` turns None into an EMPTY TUPLE, not None — and psycopg2 treats
    "a params value was supplied" (even an empty tuple) as a signal to parse
    every literal `%` in the SQL as a placeholder needing substitution. Any
    query with a LIKE pattern like 'signal_l1_%' then fails with "tuple index
    out of range", because psycopg2 goes looking for an argument to fill that
    `%` and the tuple has none.
    check_signal_freshness's own table-discovery query has exactly that
    pattern, so the health check was failing on itself on its first real run.
    Passing None through when there are no args (rather than ()) tells
    psycopg2 to skip parameter substitution entirely, so a literal % is just
    a character again.
    """
    cur.execute(sql, args)
    return cur.fetchall()


# ──────────────────────────────────────────────────────────────────────────
# 1. FRESHNESS — is every brand still arriving at all?
# ──────────────────────────────────────────────────────────────────────────
def check_freshness(cur):
    rows = q(cur, """
        SELECT brand,
               max(snapshot_date) AS last_day,
               CURRENT_DATE - max(snapshot_date) AS days_stale
        FROM price_snapshots
        GROUP BY brand
        ORDER BY days_stale DESC
    """)
    for r in rows:
        # LCW runs once or twice daily and can legitimately be a day behind
        # the thrice-daily Shopify brands, so 1 day is normal for anyone.
        if r["days_stale"] >= 3:
            record("FAIL", f"freshness/{r['brand']}",
                   f"no snapshots for {r['days_stale']} days (last {r['last_day']})")
        elif r["days_stale"] == 2:
            record("WARN", f"freshness/{r['brand']}",
                   f"2 days stale (last {r['last_day']})")


# ──────────────────────────────────────────────────────────────────────────
# 2. VOLUME — did a scrape half-fail?
#
# A partial scrape is more dangerous than a failed one: it writes plausible
# data for a fraction of the catalog, and every downstream percentage is then
# computed over a silently truncated population.
# ──────────────────────────────────────────────────────────────────────────
def check_volume(cur):
    rows = q(cur, """
        WITH daily AS (
            SELECT brand, snapshot_date, count(*) AS n
            FROM price_snapshots
            WHERE snapshot_date >= CURRENT_DATE - 14
            GROUP BY brand, snapshot_date
        ),
        latest AS (
            SELECT DISTINCT ON (brand) brand, snapshot_date, n
            FROM daily ORDER BY brand, snapshot_date DESC
        ),
        med AS (
            SELECT d.brand,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY d.n) AS typical,
                   count(*) AS days_seen
            FROM daily d
            JOIN latest l ON l.brand = d.brand AND d.snapshot_date < l.snapshot_date
            GROUP BY d.brand
        )
        SELECT l.brand, l.n AS today_rows, m.typical, m.days_seen,
               round((100.0 * l.n / NULLIF(m.typical, 0))::numeric, 1) AS pct_of_typical
        FROM latest l JOIN med m ON m.brand = l.brand
        WHERE m.days_seen >= %s
        ORDER BY pct_of_typical
    """, (MIN_DAYS_FOR_BASELINE,))
    for r in rows:
        pct = float(r["pct_of_typical"] or 0)
        if pct < 50:
            record("FAIL", f"volume/{r['brand']}",
                   f"{r['today_rows']:,} rows = {pct}% of typical "
                   f"({int(r['typical']):,}) — likely a partial scrape")
        elif pct < 75:
            record("WARN", f"volume/{r['brand']}",
                   f"{r['today_rows']:,} rows = {pct}% of typical")


# ──────────────────────────────────────────────────────────────────────────
# 3. DISCOUNT CAPTURE — the LCW failure, caught directly.
#
# A brand can genuinely run no promotions for a day or two. No mid-market
# fashion retailer has zero discounted products for a WEEK across a whole
# catalog; that means the price fields moved and we are reading list prices.
# ──────────────────────────────────────────────────────────────────────────
def check_discount_capture(cur):
    rows = q(cur, """
        SELECT brand,
               count(*) AS obs,
               count(*) FILTER (WHERE compare_at_price > price * 1.01) AS discounted,
               count(DISTINCT snapshot_date) AS days
        FROM price_snapshots
        WHERE snapshot_date >= CURRENT_DATE - 7
        GROUP BY brand
        HAVING count(*) > 500
        ORDER BY 3
    """)
    for r in rows:
        if r["discounted"] == 0 and r["days"] >= 5:
            record("FAIL", f"discount_capture/{r['brand']}",
                   f"0 discounted rows in {r['obs']:,} observations over "
                   f"{r['days']} days — price fields have almost certainly moved")


# ──────────────────────────────────────────────────────────────────────────
# 4. SIGNAL FRESHNESS — did compute_signals actually write?
# ──────────────────────────────────────────────────────────────────────────
def check_signal_freshness(cur):
    # v14.47 fix: signal_l1_13_product_delisted is derived from products.is_active,
    # which only changes when housekeeping's weekly stale-delist job runs (Mondays).
    # A product can sit unseen for up to 14 days before that job marks it delisted,
    # and the job itself can be up to 6 days from running — so this signal is
    # LEGITIMATELY quiet for up to ~20 days. Flagging it on the same 3-day
    # threshold as a daily signal was a false alarm in the checker, not a real
    # pipeline fault; caught the first time this ran for real.
    freshness_days = {"signal_l1_13_product_delisted": 22}
    default_days = 3

    tables = q(cur, """
        SELECT c.relname AS t
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND (c.relname LIKE 'signal_l1_%' OR c.relname LIKE 'product_l2_%')
        ORDER BY 1
    """)
    for t in tables:
        name = t["t"]
        col = q(cur, """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
              AND column_name IN ('snapshot_date','report_date')
            ORDER BY column_name LIMIT 1
        """, (name,))
        if not col:
            continue
        c = col[0]["column_name"]
        r = q(cur, f"SELECT max({c}) AS last_day, count(*) AS n FROM {name}")[0]
        if r["n"] == 0:
            record("WARN", f"signal/{name}", "table is empty")
        else:
            limit = freshness_days.get(name, default_days)
            stale = (date.today() - r["last_day"]).days
            if stale >= limit:
                record("FAIL", f"signal/{name}",
                       f"last wrote {r['last_day']} ({stale} days ago, "
                       f"limit {limit})")


# ──────────────────────────────────────────────────────────────────────────
# 5. BESTSELLER FEED — the HTTP 429 failure, caught directly.
# ──────────────────────────────────────────────────────────────────────────
def check_bestsellers(cur):
    r = q(cur, """
        SELECT count(DISTINCT brand) AS brands, max(snapshot_date) AS last_day
        FROM bestseller_rank
        WHERE snapshot_date >= CURRENT_DATE - 2
    """)[0]
    typical = q(cur, """
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY b) AS typical FROM (
            SELECT snapshot_date, count(DISTINCT brand) AS b
            FROM bestseller_rank
            WHERE snapshot_date >= CURRENT_DATE - 21
            GROUP BY snapshot_date
        ) x
    """)[0]["typical"]
    if not typical:
        return
    if (r["brands"] or 0) < float(typical) * 0.5:
        record("FAIL", "bestsellers",
               f"only {r['brands']} brands collected in the last 2 days "
               f"vs a typical {int(typical)} — feed is being blocked")


# ──────────────────────────────────────────────────────────────────────────
# 6. STOCKOUT CLASSIFICATION — the crashed-classifier failure.
# ──────────────────────────────────────────────────────────────────────────
def check_witnessed(cur):
    r = q(cur, """
        SELECT count(*) AS unclassified, min(recorded_at)::date AS oldest
        FROM stockout_events
        WHERE witnessed IS NULL AND event_type IN ('stockout','restock')
    """)[0]
    if r["unclassified"] and (date.today() - r["oldest"]).days > 9:
        record("FAIL", "witnessed_classification",
               f"{r['unclassified']:,} events unclassified, oldest from "
               f"{r['oldest']} — the weekly reclassifier is not completing")


# ──────────────────────────────────────────────────────────────────────────
# 7. INTEGRITY — cheap invariants that should never break.
# ──────────────────────────────────────────────────────────────────────────
def check_integrity(cur):
    checks = [
        ("orphan_snapshots",
         "SELECT count(*) FROM price_snapshots ps "
         "LEFT JOIN products p ON p.id=ps.product_id WHERE p.id IS NULL"),
        ("orphan_variants",
         "SELECT count(*) FROM product_variants pv "
         "LEFT JOIN products p ON p.id=pv.product_id WHERE p.id IS NULL"),
        ("zero_or_null_prices",
         "SELECT count(*) FROM price_snapshots WHERE price IS NULL OR price <= 0"),
        ("null_baselines",
         "SELECT count(*) FROM product_variants "
         "WHERE first_observed_price IS NULL AND delisted_at IS NULL"),
        ("delisted_but_in_stock",
         "SELECT count(*) FROM product_variants "
         "WHERE delisted_at IS NOT NULL AND is_in_stock = true"),
    ]
    for name, sql in checks:
        n = q(cur, sql)[0]["count"]
        if n:
            # These are structural. A non-zero count means something upstream
            # is writing rows it should not, and it will not fix itself.
            record("FAIL" if n > 100 else "WARN", f"integrity/{name}", f"{n:,} rows")


# ──────────────────────────────────────────────────────────────────────────
# 8. PRICE EVENTS — is change detection still firing at all?
# ──────────────────────────────────────────────────────────────────────────
def check_price_events(cur):
    r = q(cur, """
        SELECT count(*) AS recent FROM price_events
        WHERE recorded_at >= now() - interval '3 days'
    """)[0]
    if r["recent"] == 0:
        record("FAIL", "price_events",
               "no price events recorded in 3 days — change detection is dead")


def check_frozen_prices(cur):
    """Catch the LC Waikiki failure mode: a moved API field makes every item read
    at its list price, so prices sit frozen AND we become blind to the brand's
    discounts entirely.

    The naive "price hasn't changed in 8 days" test alone is NOT enough — it
    false-positives on the many Shopify brands that legitimately hold a product
    at a stable SALE price for weeks (price constant, compare_at_price above it).
    Their price is frozen but their discounts are fully captured via compare_at,
    so we are not blind to them and nothing is broken. Confirmed 2026-07-26:
    andora/tie_house/dalydress etc. are 60-98% on sale via compare_at while their
    price is stable — healthy, not broken.

    So this fires only when BOTH are true for a brand: (a) price is frozen across
    the window for ~all products, AND (b) almost none of its products show a
    compare_at discount either. That combination means our entire view of the
    brand's pricing is static — the real "field moved, we can't see anything"
    signature. LCW during the break had exactly this shape (price stuck at list,
    no compare_at, badges unreadable). check_discount_capture overlaps on the
    compare_at half; this adds the price-stasis half so a stale-writer bug that
    keeps serving old compare_at values is still caught.
    """
    rows = q(cur, """
        WITH per_prod AS (
            SELECT ps.brand, ps.product_id,
                   count(DISTINCT ps.price)         AS distinct_prices,
                   count(DISTINCT ps.snapshot_date) AS days
            FROM price_snapshots ps
            WHERE ps.snapshot_date >= CURRENT_DATE - 7
            GROUP BY ps.brand, ps.product_id
        ),
        frozen AS (
            SELECT brand,
                   count(*)                                    AS products,
                   count(*) FILTER (WHERE distinct_prices = 1) AS frozen,
                   max(days)                                   AS days
            FROM per_prod WHERE days >= 5 GROUP BY brand HAVING count(*) > 500
        ),
        sale AS (
            SELECT brand,
                   count(*)                                                     AS n,
                   count(*) FILTER (WHERE compare_at_price IS NOT NULL
                                      AND compare_at_price > price*1.01)        AS on_sale
            FROM (
                SELECT ps.brand, ps.product_id, ps.price, ps.compare_at_price,
                       row_number() OVER (PARTITION BY ps.product_id
                                          ORDER BY ps.snapshot_date DESC) rn
                FROM price_snapshots ps
                WHERE ps.snapshot_date >= CURRENT_DATE - 2
            ) t WHERE rn = 1 GROUP BY brand
        )
        SELECT f.brand, f.products, f.frozen, f.days,
               COALESCE(100.0*s.on_sale/NULLIF(s.n,0), 0) AS pct_on_sale
        FROM frozen f LEFT JOIN sale s ON s.brand = f.brand
    """)
    for r in rows:
        pct_frozen = 100.0 * r["frozen"] / r["products"] if r["products"] else 0
        pct_on_sale = float(r["pct_on_sale"] or 0)
        # broken only if frozen AND we can't see its discounts via compare_at
        if pct_frozen >= 95.0 and pct_on_sale < 5.0:
            record("FAIL", f"frozen_prices/{r['brand']}",
                   f"{pct_frozen:.1f}% of {r['products']:,} products held ONE "
                   f"price for {r['days']} days AND only {pct_on_sale:.1f}% show "
                   f"any compare_at discount — we appear blind to this brand's "
                   f"pricing (field moved or scraper writing stale values)")


def main():
    conn = psycopg2.connect(DB_URL, sslrootcert=CA_BUNDLE)
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    for fn in (check_freshness, check_volume, check_discount_capture,
               check_signal_freshness, check_bestsellers, check_witnessed,
               check_integrity, check_price_events, check_frozen_prices):
        try:
            fn(cur)
        except Exception as e:
            # A check that breaks must be loud. The whole reason this file
            # exists is that a silently-swallowed exception hid a real fault
            # for weeks; repeating that mistake here would be unforgivable.
            record("FAIL", f"check_error/{fn.__name__}", str(e)[:200])

    fails = [r for r in results if r[0] == "FAIL"]
    warns = [r for r in results if r[0] == "WARN"]

    print("=" * 78)
    print(f"KHABAR HEALTH CHECK — {date.today()}")
    print("=" * 78)
    if not results:
        print("\n✅ All checks passed. Nothing to report.")
    else:
        for status, name, detail in sorted(results):
            icon = "❌" if status == "FAIL" else "⚠️ "
            print(f"{icon} [{status}] {name}\n     {detail}")
    print("\n" + "-" * 78)
    print(f"{len(fails)} failure(s), {len(warns)} warning(s).")

    if fails:
        # Non-zero exit is the entire point: it turns the GitHub run red and
        # triggers the notification. Printing and exiting 0 is what the old
        # code did, and it is why nobody noticed for weeks.
        sys.exit(1)


if __name__ == "__main__":
    main()
