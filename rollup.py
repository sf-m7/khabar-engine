# ═══════════════════════════════════════════════════════
# KHABAR — Weekly Rollup (trigger)
# ═══════════════════════════════════════════════════════
# v2: Calls run_weekly_rollup_week() ONCE PER WEEK, via a DIRECT Postgres
# connection — not supabase.rpc(), which routes through PostgREST. PostgREST
# enforces an 8-second statement_timeout on the connecting role regardless of
# which API key is used, and that ceiling is not configurable on any Supabase
# plan. A weekly historical aggregation over the whole catalog is a batch job,
# not a quick API call, so it gets its own transport: a direct psycopg2
# connection (via SUPABASE_DB_URL), which is NOT subject to that 8s cap.
#
# Per-week dispatch is kept (rather than one big call) because it's the right
# shape regardless of transport: the rolling 30-day window is self-healing by
# design (a missed or failed week is simply rebuilt on the next run), so
# bounding failures to a single week — instead of all-or-nothing — means one
# slow/failed week can never block the others, and forward progress is never
# lost. scraper.py and bot.py are UNCHANGED: their calls are small and fast,
# exactly what PostgREST is for. Only this batch job needed a different transport.
# ═══════════════════════════════════════════════════════

import os
import sys
from datetime import date, timedelta

import psycopg2
from supabase import create_client

sys.stdout.reconfigure(line_buffering=True)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
DB_URL       = os.environ["SUPABASE_DB_URL"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

WINDOW_DAYS       = 30   # matches the rolling self-healing window the function uses
STATEMENT_TIMEOUT = "90s"  # generous explicit ceiling — if a week ever needs longer
                            # than this, that's a real signal worth investigating,
                            # not something to silently wait forever on.


def pending_weeks():
    """ISO week-start (Monday) dates touched by the rolling window, oldest first.
    Computed locally so no DB round-trip is needed just to find them."""
    today = date.today()
    start = today - timedelta(days=WINDOW_DAYS)
    weeks, d = [], start
    while d <= today:
        monday = d - timedelta(days=d.weekday())
        if monday not in weeks:
            weeks.append(monday)
        d += timedelta(days=1)
    return weeks


if __name__ == "__main__":
    print("🚀 Khabar weekly rollup — direct connection, one week per call...")
    weeks = pending_weeks()
    print(f"  Weeks in scope: {', '.join(str(w) for w in weeks)}")

    ok, failed = [], []
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True  # each week is its own unit of work; commit independently
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
            for wk in weeks:
                try:
                    cur.execute("SELECT run_weekly_rollup_week(%s)", (wk,))
                    ok.append(wk)
                    print(f"  ✅ {wk} rolled up.")
                except Exception as e:
                    failed.append(wk)
                    print(f"  ⚠️ {wk} failed this run (self-healing — will retry next run): {e}")
                    conn.rollback()
    finally:
        conn.close()

    try:
        s = supabase.table("weekly_product_summary").select("id", count="exact").limit(1).execute()
        v = supabase.table("weekly_variant_exception").select("id", count="exact").limit(1).execute()
        print(f"✅ Done. {len(ok)}/{len(weeks)} weeks succeeded. "
              f"Product-week rows: {s.count}. Variant-exception rows: {v.count}.")
    except Exception as e:
        print(f"⚠️ Could not fetch final counts: {e}")

    if failed:
        print(f"⚠️ {len(failed)} week(s) did not complete this run: "
              f"{', '.join(str(w) for w in failed)}. They will be retried automatically next run.")
        sys.exit(1)
