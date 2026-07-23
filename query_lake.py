"""
Khabar — ad-hoc lake query runner.
================================================================================
WHAT THIS IS FOR

compute_signals.py answers the questions we decided in advance were worth
answering, on a schedule. This answers the ones we think of afterwards.

It imports khabar_lake.py unchanged — the same module the real pipeline uses —
so an ad-hoc answer here and a scheduled signal there are computed off exactly
the same data, with the same hot/cold de-duplication and the same honest
baseline. If they ever disagree, that's a real finding, not a tooling artefact.

Run it from the Actions tab, paste SQL into the box, read the answer in the log.

--------------------------------------------------------------------------------
READ-ONLY BY CONSTRUCTION

Two independent guards, because this runs with production credentials:

  1. khabar_lake.connect() attaches Postgres READ_ONLY. Postgres itself will
     reject a write regardless of what this script does.
  2. The guard below rejects anything that isn't a plain SELECT/WITH before
     DuckDB ever sees it.

Neither alone is sufficient — (1) doesn't stop a DuckDB-local DROP of a
materialised table, (2) is just string matching. Together they're adequate for
a tool only Mohammed can trigger.
"""

import os
import re
import sys

import khabar_lake

# Deliberately generous. This runs on a GitHub Actions runner (~7GB RAM, not
# the 512MB of a free web host), so the whole materialised lake fits in memory
# and queries don't need to spill to disk.
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "90"))
MAX_ROWS = int(os.environ.get("MAX_ROWS", "500"))

FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|DETACH|COPY|CREATE|"
    r"EXPORT|IMPORT|CALL|GRANT|REVOKE|VACUUM|TRUNCATE)\b",
    re.IGNORECASE,
)


def guard(sql):
    if not sql.strip():
        sys.exit("No SQL provided.")
    if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.IGNORECASE):
        sys.exit("Rejected: only SELECT / WITH queries are allowed.")
    if FORBIDDEN.search(sql):
        sys.exit("Rejected: query contains a write/DDL keyword.")


def render(cols, rows):
    """
    Fixed-width text table. Deliberately plain text rather than CSV or JSON:
    the output's only consumer is a human reading the Actions log (or pasting
    it into a chat), and alignment makes a 20-row result readable at a glance.
    """
    if not rows:
        return "(no rows)"

    data = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [
        max(len(cols[i]), max((len(r[i]) for r in data), default=0))
        for i in range(len(cols))
    ]

    sep = "-+-".join("-" * w for w in widths)
    head = " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    body = [
        " | ".join(r[i].ljust(widths[i]) for i in range(len(cols)))
        for r in data
    ]
    return "\n".join([head, sep, *body])


def main():
    sql = os.environ.get("QUERY_SQL", "")
    guard(sql)

    print("🔌 Connecting to the lake (R2 cold archive + Supabase hot tier)...")
    con = khabar_lake.connect()

    # One pull of each source table into local DuckDB memory. Same call the
    # scheduled runner makes, so the tables available to the SQL below are
    # identical to the ones every L1/L2 signal is computed from.
    khabar_lake.prefetch(con)

    n_rows, n_files, start_day, end_day = khabar_lake.snapshots(
        con, days=WINDOW_DAYS
    )
    khabar_lake.stockout_events(con)

    print(
        f"\n📊 Lake ready — {n_rows:,} snapshot rows spanning {start_day} → "
        f"{end_day} ({n_files} cold day-files + hot tier).\n"
    )

    print("🔎 Query:")
    print(sql.strip())
    print()

    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchmany(MAX_ROWS + 1)

    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]

    print("=" * 78)
    print(render(cols, rows))
    print("=" * 78)
    print(f"\n{len(rows)} row(s) returned.")
    if truncated:
        print(
            f"⚠️  Output capped at {MAX_ROWS} rows. Add a LIMIT or aggregate "
            f"further to see the rest."
        )


if __name__ == "__main__":
    main()
