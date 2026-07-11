# ═══════════════════════════════════════════════════════
# KHABAR — Monthly Compaction (VACUUM FULL)
# ═══════════════════════════════════════════════════════
# Automates the routine previously done manually (see
# Khabar_Monthly_Compaction.md). Runs VACUUM FULL on the tables already
# confirmed safe to compact — the ones with real dead-row buildup and no
# other purge mechanism of their own.
#
# Uses a DIRECT psycopg2 connection (SUPABASE_DB_URL), same reason as
# rollup.py: VACUUM cannot run inside a transaction block, and PostgREST
# (which supabase.table(...).execute() routes through) wraps everything in
# one — so this can't be done via the normal Supabase client at all,
# scheduled or not. This is also why pg_cron isn't a fit here: pg_cron
# wraps scheduled jobs in a transaction too, so VACUUM (FULL or not) can't
# run through it either. A direct connection, run from outside Postgres
# entirely (here, via GitHub Actions), is the correct way to schedule this.
#
# SAFETY: VACUUM FULL never deletes or alters row data — it only reclaims
# disk space already marked dead by prior UPDATE/DELETE activity. The only
# real operational cost is a brief exclusive lock per table while it runs
# (each table is compacted one at a time, not concurrently, so only one
# table is ever briefly locked at once) and requiring roughly as much free
# disk space as that table's current size to rebuild it — not a concern at
# Khabar's current scale (largest table ~150MB).
# ═══════════════════════════════════════════════════════

import os
import sys
from datetime import datetime, timezone

import psycopg2

sys.stdout.reconfigure(line_buffering=True)

DB_URL = os.environ["SUPABASE_DB_URL"]

# Tables confirmed to accumulate real dead-row bloat with no purge
# mechanism of their own (price_snapshots and bestseller_rank ARE purged
# on their own schedule by rollup.py/archive.py, so they're excluded here
# — vacuuming them monthly on top of that is unnecessary, not harmful, but
# adds runtime for no real benefit).
TABLES = ["products", "product_variants", "price_snapshots", "stockout_events"]


def get_db_size(cur):
    cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
    return cur.fetchone()[0]


def check_no_active_queries(cur):
    """
    Mirrors the manual safety check done before running this by hand: warn
    (don't block) if something else is actively querying, since VACUUM FULL
    on a table with concurrent activity just waits for its lock rather than
    failing — but it's useful to know if that's likely to happen.
    """
    cur.execute("""
        SELECT count(*) FROM pg_stat_activity
        WHERE datname = current_database() AND state != 'idle' AND pid != pg_backend_pid()
    """)
    return cur.fetchone()[0]


if __name__ == "__main__":
    print(f"🚀 Khabar monthly compaction — {datetime.now(timezone.utc).isoformat()}")

    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True  # REQUIRED — VACUUM cannot run inside a transaction block
    try:
        with conn.cursor() as cur:
            size_before = get_db_size(cur)
            print(f"  📦 Database size before: {size_before}")

            active = check_no_active_queries(cur)
            if active:
                print(f"  ℹ️  {active} other active connection(s) detected — "
                      f"VACUUM FULL will simply wait for its lock on each "
                      f"table as needed, not fail.")

            for table in TABLES:
                try:
                    print(f"  🧹 VACUUM FULL {table}...")
                    cur.execute(f"VACUUM FULL {table}")
                    print(f"     ✅ {table} done.")
                except Exception as e:
                    # One table failing (e.g. a genuine lock timeout) should
                    # never block the others — same self-healing philosophy
                    # as rollup.py's per-week isolation.
                    print(f"     ⚠️ {table} failed this run (non-fatal, will "
                          f"just be picked up again next month): {e}")

            size_after = get_db_size(cur)
            print(f"  📦 Database size after:  {size_after}")
            print(f"✅ Compaction complete. {size_before} → {size_after}")
    finally:
        conn.close()
