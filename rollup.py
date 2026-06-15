# ═══════════════════════════════════════════════════════
# KHABAR — Weekly Rollup Fill Job (rollup.py)
# ═══════════════════════════════════════════════════════
# Builds the durable weekly intelligence layer from the daily data.
#
# Source of truth: price_snapshots (dense, one row per product per day) for the
# price story, and stockout_events for the stock story. It deliberately does NOT
# read price_events (sparse + purged) so a skipped job or an event purge can
# never lose a week.
#
# Event-driven: a product's weekly row is written ONLY when its summary differs
# from the most recent earlier week already stored (the product's first week is
# always written as the baseline anchor). Quiet weeks store nothing; the
# weekly_continuous view fills them by carrying the last week forward.
#
# Idempotent + self-healing: every run rebuilds the summary for every week still
# present in the raw snapshot window and upserts by (product_id, week_start). It
# never touches summaries for weeks older than the raw window, so once a week
# ages out it is frozen. Safe to run twice; safe to miss a run.
#
# DESTRUCTIVE ACTIONS: none. This job never deletes raw data. Trimming the daily
# window is a separate change applied only AFTER these outputs are validated.
# ═══════════════════════════════════════════════════════

import os
import sys
import statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta, date

from supabase import create_client

sys.stdout.reconfigure(line_buffering=True)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# A day counts as "on discount" when the price is at least this far below the
# product's honest baseline (first_observed_price). Matches the alert floor.
DISCOUNT_THRESHOLD_PCT = 10.0

# Fields that define whether a week is "different" from the prior kept week.
# recorded_at / sample_days are intentionally excluded — they wobble even when
# nothing real changed, and we don't want them to force a redundant row.
MEANINGFUL_FIELDS = (
    "price_median", "price_min", "price_max",
    "days_on_discount", "max_discount_pct",
    "stockout_count", "restock_count", "is_active", "delisted",
)


def safe_db(query, retries=3):
    delay = 2
    for attempt in range(retries):
        try:
            return query.execute()
        except Exception as e:
            if attempt == retries - 1:
                print(f"  ❌ Supabase failed permanently: {e}")
                return None
            import time
            time.sleep(delay)
            delay *= 2


def week_start_of(d: date) -> date:
    """Monday of the ISO week containing d."""
    return d - timedelta(days=d.isoweekday() - 1)


def paginate(table_select_builder):
    """Run a Supabase select in 1000-row pages and return all rows."""
    rows, offset = [], 0
    while True:
        res = safe_db(table_select_builder.range(offset, offset + 999))
        chunk = (res.data or []) if res else []
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return rows


# ── Load the set of weeks currently held in the raw snapshot window ──────────

def weeks_in_window():
    lo = safe_db(supabase.table("price_snapshots").select("snapshot_date")
                 .order("snapshot_date", desc=False).limit(1))
    hi = safe_db(supabase.table("price_snapshots").select("snapshot_date")
                 .order("snapshot_date", desc=True).limit(1))
    if not (lo and lo.data and hi and hi.data):
        return []
    first = week_start_of(date.fromisoformat(str(lo.data[0]["snapshot_date"])))
    last  = week_start_of(date.fromisoformat(str(hi.data[0]["snapshot_date"])))
    weeks, w = [], first
    while w <= last:
        weeks.append(w)
        w += timedelta(days=7)
    return weeks


# ── Per-brand metadata, loaded once ──────────────────────────────────────────

def load_products(brand):
    rows = paginate(
        supabase.table("products")
        .select("id, category_normalized, gender, first_observed_price, is_active, delisted_at")
        .eq("brand", brand)
    )
    return {r["id"]: r for r in rows}


def load_existing_summaries(brand):
    """All stored summary rows for this brand, grouped by product, sorted by week."""
    rows = paginate(
        supabase.table("weekly_product_summary")
        .select("product_id, week_start, " + ", ".join(MEANINGFUL_FIELDS))
        .eq("brand", brand)
    )
    by_product = defaultdict(list)
    for r in rows:
        by_product[r["product_id"]].append(r)
    for pid in by_product:
        by_product[pid].sort(key=lambda x: x["week_start"])
    return by_product


# ── Compute one week's summaries for one brand ───────────────────────────────

def summarize_week(brand, wk_start, products_meta):
    wk_end = wk_start + timedelta(days=6)

    snaps = paginate(
        supabase.table("price_snapshots")
        .select("product_id, price, snapshot_date")
        .eq("brand", brand)
        .gte("snapshot_date", str(wk_start))
        .lte("snapshot_date", str(wk_end))
    )
    if not snaps:
        return {}

    # day -> price per product, to derive open/close/median/min/max + discount days
    by_product = defaultdict(dict)  # pid -> {date: price}
    for s in snaps:
        pid = s.get("product_id")
        if pid is None or s.get("price") is None:
            continue
        by_product[pid][str(s["snapshot_date"])] = float(s["price"])

    # stock events this week, per product
    events = paginate(
        supabase.table("stockout_events")
        .select("product_id, event_type")
        .eq("brand", brand)
        .gte("recorded_at", str(wk_start))
        .lte("recorded_at", str(wk_end + timedelta(days=1)))
    )
    so_count = defaultdict(int)
    rs_count = defaultdict(int)
    for e in events:
        pid = e.get("product_id")
        if pid is None:
            continue
        if e.get("event_type") == "stockout":
            so_count[pid] += 1
        elif e.get("event_type") == "restock":
            rs_count[pid] += 1

    iso = wk_start.isocalendar()
    out = {}
    for pid, day_prices in by_product.items():
        meta = products_meta.get(pid, {})
        days = sorted(day_prices.keys())
        prices = [day_prices[d] for d in days]
        if not prices:
            continue

        fop = meta.get("first_observed_price")
        fop = float(fop) if fop is not None else None

        days_on_disc, depths, max_depth = 0, [], 0.0
        if fop and fop > 0:
            for p in prices:
                depth = (fop - p) / fop * 100
                if depth >= DISCOUNT_THRESHOLD_PCT:
                    days_on_disc += 1
                    depths.append(depth)
                    max_depth = max(max_depth, depth)

        # discount events within the week = number of day-over-day price drops
        drops = sum(1 for i in range(1, len(prices)) if prices[i] < prices[i - 1] - 0.01)

        delisted_at = meta.get("delisted_at")
        out[pid] = {
            "product_id":             pid,
            "brand":                  brand,
            "category_normalized":    meta.get("category_normalized"),
            "gender":                 meta.get("gender"),
            "week_start":             str(wk_start),
            "iso_week":               iso.week,
            "iso_year":               iso.year,
            "price_median":           round(statistics.median(prices), 2),
            "price_min":              round(min(prices), 2),
            "price_max":              round(max(prices), 2),
            "price_open":             round(day_prices[days[0]], 2),
            "price_close":            round(day_prices[days[-1]], 2),
            "first_observed_price":   round(fop, 2) if fop else None,
            "discount_event_count":   drops,
            "max_discount_pct":       round(max_depth, 2) if max_depth else None,
            "avg_discount_depth_pct": round(sum(depths) / len(depths), 2) if depths else None,
            "days_on_discount":       days_on_disc,
            "sizes_total":            None,   # best-effort, see note below
            "sizes_in_stock_avg":     None,   # best-effort, see note below
            "stockout_count":         so_count.get(pid, 0),
            "restock_count":          rs_count.get(pid, 0),
            "is_active":              bool(meta.get("is_active", True)),
            "delisted":               delisted_at is not None,
            "sample_days":            len(days),
        }
    return out


def differs(candidate, prior):
    """True if the candidate week is meaningfully different from the prior kept week."""
    if prior is None:
        return True   # first week → always write the baseline anchor
    for f in MEANINGFUL_FIELDS:
        a, b = candidate.get(f), prior.get(f)
        if isinstance(a, float) or isinstance(b, float):
            if a is None or b is None:
                if a is not b:
                    return True
            elif abs(float(a) - float(b)) > 0.01:
                return True
        elif a != b:
            return True
    return False


# ── Variant exceptions: sizes/colours whose STOCK STATE changed this week ────
# Reliable from stockout_events (carries variant_id, size, colour). A variant
# earns a row when it had a stock change that week; with the view's carry-forward
# this reconstructs the full "which sizes were available each week" series.
# (Variant-level PRICE deviations are added later, once variant-level price
# snapshots are confirmed present for a brand — flagged in the plan.)

def variant_exceptions_for_week(brand, wk_start):
    wk_end = wk_start + timedelta(days=6)
    iso = wk_start.isocalendar()
    events = paginate(
        supabase.table("stockout_events")
        .select("variant_id, product_id, size, color, event_type")
        .eq("brand", brand)
        .gte("recorded_at", str(wk_start))
        .lte("recorded_at", str(wk_end + timedelta(days=1)))
    )
    agg = {}
    for e in events:
        vid = e.get("variant_id")
        if vid is None:
            continue
        a = agg.setdefault(vid, {
            "variant_id": vid, "product_id": e.get("product_id"),
            "week_start": str(wk_start), "iso_week": iso.week, "iso_year": iso.year,
            "size": e.get("size"), "color": e.get("color"),
            "price": None, "is_in_stock": None, "days_in_stock": None,
            "stockout_count": 0, "restock_count": 0,
        })
        if e.get("event_type") == "stockout":
            a["stockout_count"] += 1
            a["is_in_stock"] = False
        elif e.get("event_type") == "restock":
            a["restock_count"] += 1
            a["is_in_stock"] = True
    return list(agg.values())


# ── Main ─────────────────────────────────────────────────────────────────────

def run():
    print("🚀 Khabar weekly rollup starting...")
    weeks = weeks_in_window()
    if not weeks:
        print("  No snapshot data found. Nothing to roll up.")
        return
    print(f"  Weeks in raw window: {weeks[0]} → {weeks[-1]} ({len(weeks)} weeks)")

    brands = [r["name"] for r in [
        {"name": "lc_waikiki"}, {"name": "town_team"}, {"name": "ravin"},
        {"name": "mens_club"}, {"name": "tree"}, {"name": "dott_jeans"},
        {"name": "carina"}, {"name": "2s_egypt"}, {"name": "andora"},
        {"name": "cizaro"}, {"name": "mobaco"}, {"name": "defacto"},
    ]]

    total_written, total_skipped, total_var = 0, 0, 0

    for brand in brands:
        products_meta = load_products(brand)
        if not products_meta:
            continue
        existing = load_existing_summaries(brand)
        print(f"\n  ▶ {brand}: {len(products_meta)} products, "
              f"{sum(len(v) for v in existing.values())} summaries on record")

        for wk in weeks:
            week_rows = summarize_week(brand, wk, products_meta)
            to_upsert, to_clear = [], []

            for pid, cand in week_rows.items():
                prior_list = [r for r in existing.get(pid, []) if r["week_start"] < str(wk)]
                prior = prior_list[-1] if prior_list else None
                if differs(cand, prior):
                    to_upsert.append(cand)
                    # keep in-memory view current so later weeks compare correctly
                    existing.setdefault(pid, [])
                    existing[pid] = [r for r in existing[pid] if r["week_start"] != str(wk)]
                    existing[pid].append({**{k: cand.get(k) for k in MEANINGFUL_FIELDS},
                                          "product_id": pid, "week_start": str(wk)})
                    existing[pid].sort(key=lambda x: x["week_start"])
                else:
                    # identical to prior → ensure no stale row lingers for this week
                    to_clear.append((pid, str(wk)))
                    total_skipped += 1

            for i in range(0, len(to_upsert), 200):
                safe_db(supabase.table("weekly_product_summary")
                        .upsert(to_upsert[i:i + 200], on_conflict="product_id,week_start"))
            total_written += len(to_upsert)

            for pid, wks in to_clear:
                safe_db(supabase.table("weekly_product_summary")
                        .delete().eq("product_id", pid).eq("week_start", wks))

            ve = variant_exceptions_for_week(brand, wk)
            for i in range(0, len(ve), 200):
                safe_db(supabase.table("weekly_variant_exception")
                        .upsert(ve[i:i + 200], on_conflict="variant_id,week_start"))
            total_var += len(ve)

    print(f"\n🏁 Rollup done. {total_written} summary rows written, "
          f"{total_skipped} quiet weeks skipped, {total_var} variant exceptions.")


if __name__ == "__main__":
    run()
