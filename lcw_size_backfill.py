# ═══════════════════════════════════════════════════════
# KHABAR — TEMPORARY LCW Size Backfill
# ═══════════════════════════════════════════════════════
# PURPOSE: clear the LCW size-data backlog fast, via repeated/manual runs,
# WITHOUT touching the daily production scraper's SIZE_TIMEOUT (which stays
# at 600s so the regular daily run keeps its normal, safe runtime).
#
# This is meant to be deleted — both this file and its .yml workflow —
# once the backlog check at the end reports 0 (or near-0) missing sizes.
# It is intentionally NOT part of the daily schedule.
#
# DESIGN: imports the real, already-tested functions straight from
# scraper.py (get_lcw_session, fetch_lcw_product_page, normalize_lcw_color,
# safe_db_execute, env_int/env_str, the DataImpulse config) rather than
# copy-pasting them. This means:
#   - No duplicated logic to accidentally let drift out of sync with the
#     production scraper.
#   - Any future fix to how LCW pages are fetched/parsed in scraper.py
#     automatically applies here too, with zero extra work.
#   - This file is genuinely just "run the existing size-pass loop, with a
#     much bigger time budget, on its own."
#
# The core loop below is the SAME logic that lives inside scrape_lcw()'s
# size-enrichment section — extracted to run standalone, with its own much
# larger SIZE_TIMEOUT and its own SIZE_CAP, independent of the ones the
# daily scraper uses.
# ═══════════════════════════════════════════════════════

import os
import sys
import random
import time
from datetime import datetime, timezone

sys.stdout.reconfigure(line_buffering=True)

# Import real, tested logic directly from scraper.py — see DESIGN note above.
from supabase import create_client
from scraper import (
    SUPABASE_URL,
    SUPABASE_KEY,
    env_int,
    get_lcw_session,
    fetch_lcw_product_page,
    normalize_lcw_color,
    safe_db_execute,
    DATAIMPULSE_CONFIGURED,
    LCW_PROXY_COUNTRIES,
    LCW_FORCE_HTTP1,
)

# scraper.py doesn't expose a module-level `supabase` client (every function
# there creates its own via create_client(...) when needed) — matching that
# same pattern here rather than assuming a global that doesn't exist.
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Backfill-specific budget — independent of the daily scraper's caps ────
# BACKFILL_SIZE_CAP:   how many product URLs to fetch THIS run. Generous
#                       default since the whole point is to go fast; still
#                       overridable via env var for a manual workflow_dispatch.
# BACKFILL_TIME_LIMIT: hard wall-clock ceiling for THIS run, in seconds.
#                       Defaults to 45 minutes — comfortably inside a
#                       public-repo GitHub Actions run, generous compared to
#                       the daily scraper's 600s (10 min) size-pass slice.
BACKFILL_SIZE_CAP   = env_int("BACKFILL_SIZE_CAP", 3000)
BACKFILL_TIME_LIMIT = env_int("BACKFILL_TIME_LIMIT_SECONDS", 2700)  # 45 min


def fetch_missing_size_variants():
    """Pull ALL LCW variant rows still missing size data, paginated."""
    all_missing, offset = [], 0
    while True:
        chunk = safe_db_execute(
            supabase.table("product_variants")
            .select("id, product_id, external_sku, color, is_in_stock, "
                    "first_observed_price, products!inner(url, brand)")
            .eq("products.brand", "lc_waikiki")
            .is_("size", "null")
            .range(offset, offset + 999)
        )
        rows = (chunk.data or []) if chunk else []
        all_missing.extend(rows)
        if len(rows) < 1000:
            break
        offset += 1000
    return all_missing


def run_backfill():
    if not DATAIMPULSE_CONFIGURED:
        print("  ⚠️ DATAIMPULSE credentials not set — cannot safely reach "
              "LCW's Akamai-protected pages. Aborting.")
        return

    # v14.41: get_lcw_session() returns (session, country) as of v14.40.
    # Unpacking is REQUIRED — assigning the tuple to `session` silently
    # breaks every fetch_lcw_product_page() call, which looks identical to
    # a total proxy failure and would waste a whole 45-minute run.
    session, lcw_country = get_lcw_session()
    print(f"  [Backfill] Proxy pool: {','.join(LCW_PROXY_COUNTRIES)} "
          f"(this session: {lcw_country}), "
          f"HTTP/1.1 forced: {LCW_FORCE_HTTP1}")

    print("  [Backfill] Pulling all LCW variants still missing size data...")
    all_missing = fetch_missing_size_variants()

    if not all_missing:
        print("  [Backfill] ✅ All LCW variants already have size data. "
              "Nothing to do — this script and its workflow can be deleted.")
        return

    url_to_rows = {}
    for row in all_missing:
        url = (row.get("products") or {}).get("url")
        if not url:
            continue
        url_to_rows.setdefault(url, []).append(row)

    total_urls = len(url_to_rows)
    print(f"  [Backfill] {len(all_missing)} variant rows still missing sizes, "
          f"across {total_urls} unique product URLs.")
    print(f"  [Backfill] This run will attempt up to {BACKFILL_SIZE_CAP} URLs, "
          f"capped at {BACKFILL_TIME_LIMIT/60:.0f} minutes wall-clock.")

    urls_to_fetch = list(url_to_rows.keys())[:BACKFILL_SIZE_CAP]
    fetched = populated_rows = consecutive_fails = 0
    start = time.time()

    # v2 fix: this run (2026-07-12) produced ZERO writes across a full
    # 45-minute window — every single URL failed with a 10s timeout, 0
    # bytes received (same DataImpulse bad-pool signature already seen and
    # fixed for scraper.py's main crawl). This script had no retry/rotation
    # at all, so a bad pool day meant burning the ENTIRE time budget
    # failing sequentially with nothing to show for it. Two fixes:
    #   1. One retry per URL on a FRESH session (mirrors the main
    #      scraper's rotation logic, just simpler — no run-wide budget
    #      needed here since this is already a bounded, manual job).
    #   2. Early exit if the pool is clearly having a bad day (20
    #      consecutive total failures — i.e. failed even after their
    #      retry) — stops burning bandwidth/time once it's clear
    #      continuing won't help; re-running later or tomorrow is cheaper.
    EARLY_EXIT_THRESHOLD = 20

    for url in urls_to_fetch:
        if time.time() - start > BACKFILL_TIME_LIMIT:
            print("  [Backfill] Time limit reached — stopping this run "
                  "(re-run again to continue where this left off).")
            break

        page_data = fetch_lcw_product_page(session, url)
        if not page_data:
            # One retry on a fresh session — cheap insurance against a
            # single bad residential peer, same idea as the main scraper's
            # rotation logic, without needing a shared run-wide budget here.
            # Rotate AWAY from the country that just failed, matching the
            # main scraper's v14.40 behaviour: if a whole pool is having a
            # bad hour, retrying inside it is the least useful move.
            session, lcw_country = get_lcw_session(avoid_country=lcw_country)
            time.sleep(random.uniform(1, 2))
            page_data = fetch_lcw_product_page(session, url)

        fetched += 1

        if not page_data or not page_data.get("sizes"):
            consecutive_fails += 1
            if consecutive_fails >= EARLY_EXIT_THRESHOLD:
                print(f"  ⚠️ [Backfill] {EARLY_EXIT_THRESHOLD} consecutive "
                      f"failures even after per-URL retries — this looks "
                      f"like a bad DataImpulse pool day, not a code issue. "
                      f"Stopping early rather than burning the rest of the "
                      f"time budget. Try again later today or tomorrow.")
                break
            time.sleep(random.uniform(0.6, 1.4))
            continue

        consecutive_fails = 0  # reset on any success

        sizes   = page_data["sizes"]
        now_iso = datetime.now(timezone.utc).isoformat()
        page_color = normalize_lcw_color(page_data.get("color_name"))

        for row in url_to_rows[url]:
            product_id   = row.get("product_id")
            color        = row.get("color") or page_color
            fop          = row.get("first_observed_price")
            parent_stock = row.get("is_in_stock", True)

            for i, sz in enumerate(sizes):
                size_in_stock = (sz.get("stock", 0) > 0) if sz.get("stock") is not None else parent_stock
                size_value    = sz["size"]

                if i == 0:
                    # Same v14.22 rule as production: never write a single
                    # size's stock state onto the parent (color-level) row.
                    update_payload = {
                        "size":            size_value,
                        "last_updated_at": now_iso,
                    }
                    if not row.get("color") and color:
                        update_payload["color"] = color
                    safe_db_execute(
                        supabase.table("product_variants")
                        .update(update_payload)
                        .eq("id", row["id"])
                    )
                else:
                    new_sku = f"{row['external_sku']}_{size_value.replace(' ', '_')}"
                    safe_db_execute(
                        supabase.table("product_variants").upsert({
                            "product_id":           product_id,
                            "external_sku":         new_sku,
                            "color":                color,
                            "size":                 size_value,
                            "is_in_stock":          size_in_stock,
                            "first_observed_price": fop,
                            "last_updated_at":      now_iso,
                        }, on_conflict="external_sku")
                    )
            populated_rows += 1

        if fetched % 50 == 0:
            elapsed_min = (time.time() - start) / 60
            print(f"  [Backfill] Progress: {fetched}/{len(urls_to_fetch)} URLs "
                  f"fetched, {populated_rows} variant rows populated "
                  f"({elapsed_min:.1f} min elapsed)...")

        time.sleep(random.uniform(0.6, 1.4))

    remaining_after = total_urls - fetched
    print(f"\n  ✅ [Backfill] Done this run: {fetched} URLs fetched, "
          f"{populated_rows} variant rows populated.")
    print(f"  📊 [Backfill] Approx. {max(remaining_after, 0)} URLs still "
          f"remaining — re-run this workflow again to continue.")


if __name__ == "__main__":
    print("🚀 Khabar — TEMPORARY LCW size backfill starting...")
    run_backfill()
    print("🏁 Backfill run complete.")
