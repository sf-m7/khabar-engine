"""
Khabar — THE SIGNAL RUNNER.
================================================================================
Walks the registry in signals.py, computes each runnable signal against the
DuckDB lake (R2 cold archive + Supabase hot buffer, stitched by khabar_lake.py),
and writes the RESULTS back into small Supabase aggregate tables.

WHY RESULTS GO BACK INTO POSTGRES
DuckDB is in-process: it exists only for the life of this job, then vanishes.
Nothing can "connect to" it afterwards. So the heavy computation happens here,
against the lake where storage is cheap — and only the ANSWERS (hundreds to a
few thousand rows, kilobytes) land in Supabase, where the Telegram bot can read
them in milliseconds, client reports can pull them, and a human can eyeball them
in the table editor. The lake grows forever; the signal tables stay tiny.

THIS SCRIPT KNOWS NOTHING ABOUT ANY PARTICULAR SIGNAL.
It only knows how to: check a signal's preconditions, run its SQL, replace its
table's rows for the computed window, and log what happened. That is what makes
signal #25 a registry entry rather than a new script.

FAILURE PHILOSOPHY
One signal failing must never block the other 37. Each is isolated. A failure is
logged to signal_runs and the run continues. The job only exits non-zero if
EVERY signal failed — because that means the lake itself is broken, not a query.
"""

import os
import sys
import time
from datetime import date

import psycopg2
import psycopg2.extras

import khabar_lake
from signals import SIGNALS, BLOCKERS, blocked_by, runnable

SUPABASE_DB_URL = os.environ["SUPABASE_DB_URL"]

# A dry run computes everything and reports what it WOULD write, but touches
# no Supabase table. Use it to inspect a new signal's output before trusting it.
DRY_RUN = os.environ.get("SIGNALS_DRY_RUN", "false").lower() == "true"


def log_run(pg, signal, status, **kw):
    """
    Record every run — including the ones that produced nothing.

    This is not bookkeeping for its own sake. A signal that silently STOPS
    firing looks exactly like a signal with nothing to report, unless something
    records the difference. It is also what makes a client-facing number
    defensible: we can state precisely which days and how many observations
    produced it.
    """
    with pg.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_runs (
                signal_id, signal_name, level, status, skip_reason,
                rows_written, rows_suppressed, window_start, window_end,
                days_available, duration_seconds, error_message
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            signal["id"], signal["name"], signal["level"], status,
            kw.get("skip_reason"),
            kw.get("rows_written", 0), kw.get("rows_suppressed", 0),
            kw.get("window_start"), kw.get("window_end"),
            kw.get("days_available"), kw.get("duration_seconds"),
            kw.get("error_message"),
        ))
    pg.commit()


def replace_rows(pg, table, unique_on, rows, columns):
    """
    Write a signal's output.

    Upsert on the signal's declared unique key rather than truncate-and-insert:
    a signal recomputed for today must not erase yesterday's verdicts, which the
    bot and reports may already be serving. Re-running the same day is therefore
    idempotent — it overwrites that day's rows and leaves history alone.
    """
    if not rows:
        return 0

    cols     = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    conflict = ", ".join(unique_on)
    updates  = ", ".join(f"{c} = EXCLUDED.{c}"
                         for c in columns if c not in unique_on)

    sql = f"""
        INSERT INTO {table} ({cols}) VALUES ({placeholders})
        ON CONFLICT ({conflict}) DO UPDATE SET {updates}
    """

    with pg.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
    pg.commit()
    return len(rows)


def run_signal(con, pg, signal):
    """Compute one signal. Never raises — a failure is data, not a crash."""
    started = time.time()
    sid     = signal["id"]

    print(f"\n  ── {sid} · {signal['name']} ──")

    # --- Preconditions, in order of how cheaply they can be checked ---
    if not signal.get("enabled"):
        print("     ⏭  disabled in registry.")
        log_run(pg, signal, "skipped", skip_reason="disabled in registry")
        return "skipped"

    blockers = blocked_by(signal)
    if blockers:
        why = "; ".join(BLOCKERS[b]["why"] for b in blockers)
        print(f"     ⏸  BLOCKED by: {', '.join(blockers)}")
        print(f"        {why}")
        print("        Will start computing automatically once resolved.")
        log_run(pg, signal, "skipped",
                skip_reason=f"blocked by {', '.join(blockers)}: {why}")
        return "skipped"

    if not signal.get("sql"):
        print("     ⏭  no SQL defined yet (placeholder entry).")
        log_run(pg, signal, "skipped", skip_reason="no SQL defined yet")
        return "skipped"

    try:
        window = signal["window_days"]

        # Build the unified two-tier view for this signal's window. Different
        # signals legitimately need different lookbacks (30 days for an IQR,
        # 60+ for cross-brand co-movement), so the view is rebuilt per signal
        # rather than shared.
        n_rows, n_files, start_day, end_day = khabar_lake.snapshots(
            con, days=window
        )
        days_available = con.execute(
            "SELECT count(DISTINCT snapshot_date) FROM snapshots"
        ).fetchone()[0]

        print(f"     window: {start_day} → {end_day} "
              f"({days_available} days present of {window} requested)")

        # THE HONESTY CHECK. If the lake as a whole cannot support the window,
        # do not quietly compute over whatever exists — say so.
        if days_available < signal["min_days"]:
            msg = (f"only {days_available} days of data available, "
                   f"needs {signal['min_days']}")
            print(f"     ⏸  INSUFFICIENT HISTORY — {msg}")
            print("        Will start computing automatically as data accumulates.")
            log_run(pg, signal, "skipped", skip_reason=msg,
                    window_start=start_day, window_end=end_day,
                    days_available=days_available,
                    duration_seconds=round(time.time() - started, 2))
            return "skipped"

        sql = signal["sql"].format(
            window_days=window, min_days=signal["min_days"]
        )
        result  = con.execute(sql)
        columns = [d[0] for d in result.description]
        rows    = result.fetchall()

        # How many rows did the min_days guard withhold? Reporting this is the
        # difference between "no anomalies today" and "we couldn't tell".
        suppressed = 0
        if signal.get("suppressed_sql"):
            suppressed = con.execute(
                signal["suppressed_sql"].format(
                    window_days=window, min_days=signal["min_days"]
                )
            ).fetchone()[0]

        print(f"     computed: {len(rows)} row(s)")
        if suppressed:
            print(f"     suppressed: {suppressed} row(s) — product history "
                  f"shorter than {signal['min_days']} days")

        if DRY_RUN:
            print(f"     🧪 DRY RUN — would write {len(rows)} row(s) to "
                  f"{signal['table']}. Nothing written.")
            for r in rows[:5]:
                print(f"        {dict(zip(columns, r))}")
            log_run(pg, signal, "ok", rows_written=0, rows_suppressed=suppressed,
                    window_start=start_day, window_end=end_day,
                    days_available=days_available,
                    duration_seconds=round(time.time() - started, 2))
            return "ok"

        written = replace_rows(
            pg, signal["table"], signal["unique_on"], rows, columns
        )
        print(f"     ✅ wrote {written} row(s) → {signal['table']}")

        log_run(pg, signal, "ok", rows_written=written,
                rows_suppressed=suppressed,
                window_start=start_day, window_end=end_day,
                days_available=days_available,
                duration_seconds=round(time.time() - started, 2))
        return "ok"

    except Exception as e:
        # A broken signal must not take down the other 37.
        print(f"     ❌ FAILED: {e}")
        log_run(pg, signal, "failed", error_message=str(e)[:2000],
                duration_seconds=round(time.time() - started, 2))
        return "failed"


if __name__ == "__main__":
    mode = "DRY RUN (nothing written)" if DRY_RUN else "PRODUCTION"
    print(f"🚀 Khabar signal engine — mode={mode}")
    print(f"   Registry: {len(SIGNALS)} signal(s) declared, "
          f"{len(runnable())} runnable today.\n")

    # Show what's held back and why, every run. A blocker nobody is reminded of
    # is a blocker that never gets fixed.
    unresolved = [k for k, v in BLOCKERS.items() if not v["resolved"]]
    if unresolved:
        print("   ⏸  Unresolved blockers:")
        for b in unresolved:
            waiting = [s["id"] for s in SIGNALS if b in s.get("requires", [])]
            print(f"      • {b} — holding back: {', '.join(waiting) or 'nothing yet'}")
        print()

    con = khabar_lake.connect()
    pg  = psycopg2.connect(SUPABASE_DB_URL)

    tally = {"ok": 0, "skipped": 0, "failed": 0}
    for signal in SIGNALS:
        tally[run_signal(con, pg, signal)] += 1

    print(f"\n🏁 Signal run complete. "
          f"{tally['ok']} computed, {tally['skipped']} skipped, "
          f"{tally['failed']} failed.")

    pg.close()

    # Only a total wipeout is a job failure. One bad signal is a logged event,
    # not an outage — the other signals' output is still valid and served.
    attempted = tally["ok"] + tally["failed"]
    if attempted and tally["ok"] == 0:
        print("   🛑 Every attempted signal failed — the lake or the DB is "
              "likely broken, not the queries. Exiting non-zero.")
        sys.exit(1)

    sys.stdout.flush()
    os._exit(0)
