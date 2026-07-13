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
#
# v3: Added bestseller_rank weekly compaction, same shape as the price
# rollup above it. Rationale: bestseller_rank was on track to grow
# unbounded (~2,400 rows/day, no purge) for a signal where only the
# WEEKLY TRAJECTORY (best/worst/avg/close rank, week-over-week change) is
# analytically useful — the daily tick itself isn't. So this adds:
#   1. run_weekly_bestseller_rollup_week() — same self-healing, only-write-
#      on-change SQL function pattern as run_weekly_rollup_week(), writing
#      into weekly_bestseller_summary (permanent, tiny).
#   2. A 35-day purge of raw bestseller_rank rows — but ONLY for weeks that
#      this run (or a past run) actually finished rolling up successfully.
#      Same principle as archive.py's verify-before-delete gate: never
#      delete a day's data before its week is confirmed captured elsewhere.
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

WINDOW_DAYS       = 21   # matches the rolling self-healing window the function uses
STATEMENT_TIMEOUT = "90s"  # generous explicit ceiling — if a week ever needs longer
                            # than this, that's a real signal worth investigating,
                            # not something to silently wait forever on.

BESTSELLER_HOT_DAYS = 35  # daily bestseller_rank rows older than this are purged,
                           # matching the price_snapshots hot window in archive.py.
                           # Unlike price_snapshots, there's no R2 export here —
                           # the weekly summary IS the permanent record; the daily
                           # granularity is genuinely disposable once rolled up.  


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


def purge_rolled_up_bestseller_days(conn):
    """
    Deletes raw bestseller_rank rows that are BOTH older than the hot window
    AND belong to a week that has ALREADY rolled up successfully — checked
    directly against weekly_bestseller_summary (not just this run's ok
    list), so a week rolled up in an earlier run still gets its old daily
    rows cleared even if this run only touched later weeks.

    A week's rows are never purged unless weekly_bestseller_summary
    actually has a row for that week — mirrors archive.py's "verify it
    landed before deleting the source" gate, just checked against a table
    instead of a re-read file.
    """
    cutoff = date.today() - timedelta(days=BESTSELLER_HOT_DAYS)
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
        cur.execute(
            """
            DELETE FROM bestseller_rank br
            WHERE br.snapshot_date < %s
              AND EXISTS (
                SELECT 1 FROM weekly_bestseller_summary w
                WHERE w.week_start = (br.snapshot_date - (EXTRACT(DOW FROM br.snapshot_date)::int + 6) %% 7)
              )
            """,
            (cutoff,),
        )
        return cur.rowcount


if __name__ == "__main__":
    print("🚀 Khabar weekly rollup — direct connection, one week per call...")
    weeks = pending_weeks()
    print(f"  Weeks in scope: {', '.join(str(w) for w in weeks)}")

    ok, failed = [], []
    bs_ok, bs_failed = [], []
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True  # each week is its own unit of work; commit independently
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
            for wk in weeks:
                try:
                    cur.execute("SELECT run_weekly_rollup_week(%s)", (wk,))
                    ok.append(wk)
                    print(f"  ✅ {wk} price rollup done.")
                except Exception as e:
                    failed.append(wk)
                    print(f"  ⚠️ {wk} price rollup failed this run (self-healing — will retry next run): {e}")
                    conn.rollback()

                try:
                    cur.execute("SELECT run_weekly_bestseller_rollup_week(%s)", (wk,))
                    bs_ok.append(wk)
                    print(f"  ✅ {wk} bestseller rollup done.")
                except Exception as e:
                    bs_failed.append(wk)
                    print(f"  ⚠️ {wk} bestseller rollup failed this run (self-healing — will retry next run): {e}")
                    conn.rollback()

        print("  Purging raw bestseller_rank rows for weeks already rolled up "
              f"and older than the {BESTSELLER_HOT_DAYS}-day hot window...")
        try:
            purged = purge_rolled_up_bestseller_days(conn)
            print(f"  🗑️  Purged {purged} raw bestseller_rank rows "
                  f"(their week-level summary is preserved permanently).")
        except Exception as e:
            print(f"  ⚠️ Bestseller purge failed this run (non-fatal — raw rows "
                  f"are just kept a bit longer, nothing lost): {e}")
    finally:
        conn.close()

    try:
        s  = supabase.table("weekly_product_summary").select("id", count="exact").limit(1).execute()
        v  = supabase.table("weekly_variant_exception").select("id", count="exact").limit(1).execute()
        bs = supabase.table("weekly_bestseller_summary").select("id", count="exact").limit(1).execute()
        print(f"✅ Done. {len(ok)}/{len(weeks)} price weeks succeeded, "
              f"{len(bs_ok)}/{len(weeks)} bestseller weeks succeeded. "
              f"Product-week rows: {s.count}. Variant-exception rows: {v.count}. "
              f"Bestseller-week rows: {bs.count}.")
    except Exception as e:
        print(f"⚠️ Could not fetch final counts: {e}")

    if failed:
        print(f"⚠️ {len(failed)} price week(s) did not complete this run: "
              f"{', '.join(str(w) for w in failed)}. They will be retried automatically next run.")
    if bs_failed:
        print(f"⚠️ {len(bs_failed)} bestseller week(s) did not complete this run: "
              f"{', '.join(str(w) for w in bs_failed)}. They will be retried automatically next run.")

    if failed or bs_failed:
        sys.exit(1)
