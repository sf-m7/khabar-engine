"""
integrity_audit.py — Khabar signal & product integrity audit
================================================================================
WHAT THIS ANSWERS
For every deployed output table (signal_l1_*, signal_l2_*, product_l2_*) it reports:
  • size            — exact row count
  • freshness       — most recent date present, and days stale vs today
  • coverage        — distinct brands present vs active brands in the catalogue
  • history depth   — distinct dates present
  • null integrity  — null rate in the meaningful output columns
  • run health      — latest compute-run status, rows_written, rows_suppressed,
                      skip_reason, and whether a same-day RE-RUN wrote 0 rows
                      (the no-op-wipe hazard)
  • composite score — 0-100, transparent sub-scores, with CRITICAL/WARN flags

It also profiles the SUBSTRATE the signals are computed from, across BOTH tiers:
  • HOT  (Supabase price_snapshots / price_events / product_variants)
  • COLD (Cloudflare R2 parquet day-files — earliest/latest day, file count)
plus the known data-integrity invariants (discount_pct must be ~100% NULL;
delisted-but-in-stock variant count; FOP coverage).

OUTPUT
  • Human-readable report to stdout
  • integrity_report.md   (commit / upload as an Actions artifact)
  • integrity_report.json (machine-readable, for diffing week over week)

EXIT CODE
  0  = all clear
  1  = at least one CRITICAL issue (empty deployed table, stale > STALE_CRIT
       days, or a compute run in error state)
So a scheduled GitHub Actions job goes red when the pipeline degrades.

REQUIREMENTS
  psycopg2-binary   (Supabase queries)
  duckdb            (already used by khabar_lake; only needed for the R2 section)
ENV (same vars the pipeline already uses in Actions)
  SUPABASE_DB_URL                                   (required)
  R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY /
  R2_ACCOUNT_ID / R2_BUCKET_NAME                    (optional — R2 section is
                                                     skipped with a warning if
                                                     any are missing)
Nothing is written to the database. This is read-only.
================================================================================
"""

import os
import re
import sys
import json
from datetime import date, datetime, timezone

import psycopg2
import psycopg2.extras

# ------------------------------------------------------------------ thresholds
STALE_WARN = 2      # days since last data before we warn
STALE_CRIT = 4      # days since last data before we fail the build

# Signals whose newest date lags by design and must NOT be judged on the normal
# freshness rule. product_delisted dates each delist by the product's last-seen
# date; with a 14-day stale window + weekly delist cadence, its freshest event
# is always ~14-21 days old. Given a wider window so real breakage still trips.
LAGGED_STALE = {"signal_l1_13_product_delisted": 25}
NULL_WARN  = 0.05   # >5%  null in a meaningful output column -> warn
NULL_CRIT  = 0.25   # >25% null -> critical
TODAY      = date.today()

# Columns that carry the actual verdict/number of a signal or product. Null here
# means the row is present but useless, which a plain row count would hide.
MEANINGFUL_COL_HINTS = (
    "read", "pct", "price", "score", "index", "depth", "gap",
    "share", "change", "drop", "trajectory", "verdict",
)
# Columns that are legitimately allowed to be null (documented data facts) and
# must NOT count against a signal's null-integrity score.
NULL_ALLOWED = {
    "avg_claimed_depth_pct",   # NULL where the brand publishes no RRP (correct)
    "manufactured_gap_pct",
    "compare_at_price",
}


def connect():
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        sys.exit("FATAL: SUPABASE_DB_URL is not set.")
    return psycopg2.connect(dsn)


def q(cur, sql, args=None):
    cur.execute(sql, args or ())
    return cur.fetchall()


def q1(cur, sql, args=None):
    cur.execute(sql, args or ())
    row = cur.fetchone()
    return row[0] if row else None


# --------------------------------------------------------------- introspection
def discover_output_tables(cur):
    """Every deployed output table, auto-discovered so new signals appear here
    the moment they exist without editing this script."""
    rows = q(cur, """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE'
          AND (table_name LIKE 'signal_l1_%'
            OR table_name LIKE 'signal_l2_%'
            OR table_name LIKE 'product_l2_%')
        ORDER BY table_name
    """)
    return [r[0] for r in rows]


def columns_of(cur, table):
    return [r[0] for r in q(cur, """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
    """, (table,))]


def numeric_columns_of(cur, table):
    return [r[0] for r in q(cur, """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
          AND data_type IN ('integer','bigint','numeric','double precision','real')
        ORDER BY ordinal_position
    """, (table,))]


def pick_date_col(cols):
    for c in ("snapshot_date", "report_date", "computed_at", "run_at"):
        if c in cols:
            return c
    return None


def pick_brand_col(cols):
    return "brand" if "brand" in cols else None


def table_to_signal_id(table, run_ids):
    """Map an output table to its signal_runs.signal_id.
    signal_l1_01_genuine_price_drop -> l1_01
    signal_l2_discount_honesty      -> l2_discount_honesty
    """
    if not table.startswith("signal_"):
        return None
    stem = table[len("signal_"):]                # e.g. l1_01_genuine_price_drop
    for sid in run_ids:
        if stem == sid or stem.startswith(sid + "_"):
            return sid
    # l1_NN fallback: first two tokens
    m = re.match(r"(l[12]_[0-9]+)", stem)
    return m.group(1) if m else None


# --------------------------------------------------------------------- runs
def latest_runs(cur):
    """Latest run per signal + the count of runs today and today's max
    rows_written, so we can detect the no-op re-run wipe hazard."""
    out = {}
    for r in q(cur, """
        SELECT DISTINCT ON (signal_id)
               signal_id, status, skip_reason, rows_written, rows_suppressed,
               days_available, error_message, run_at
        FROM signal_runs
        ORDER BY signal_id, run_at DESC
    """):
        out[r[0]] = dict(
            status=r[1], skip_reason=r[2], rows_written=r[3],
            rows_suppressed=r[4], days_available=r[5],
            error_message=r[6], run_at=r[7],
        )
    today = {}
    for r in q(cur, """
        SELECT signal_id, count(*) runs_today, max(rows_written) max_rows_today,
               min(rows_written) min_rows_today
        FROM signal_runs
        WHERE run_at::date = %s
        GROUP BY signal_id
    """, (TODAY,)):
        today[r[0]] = dict(runs_today=r[1], max_rows_today=r[2], min_rows_today=r[3])
    return out, today


# ----------------------------------------------------------------- per table
def audit_table(cur, table, active_brands, runs, runs_today, run_ids):
    cols = columns_of(cur, table)
    date_col = pick_date_col(cols)
    brand_col = pick_brand_col(cols)

    n = q1(cur, f'SELECT count(*) FROM "{table}"')
    rec = dict(table=table, rows=n, level=_level_of(table),
               flags=[], subscores={})

    # ---- empty deployed table is always critical
    if n == 0:
        rec["flags"].append(("CRITICAL", "empty — deployed but produces no rows"))
        rec["score"] = 0
        rec["max_date"] = None
        rec["distinct_dates"] = 0
        rec["brands"] = 0
        _attach_run(rec, table, runs, runs_today, run_ids)
        return rec

    # ---- freshness
    max_date = distinct_dates = None
    stale_days = None
    fresh_score = 0.5
    if date_col:
        max_date = q1(cur, f'SELECT max({date_col})::date FROM "{table}"')
        distinct_dates = q1(cur, f'SELECT count(distinct {date_col}::date) FROM "{table}"')
        if max_date:
            stale_days = (TODAY - max_date).days
            crit_at = LAGGED_STALE.get(table, STALE_CRIT)
            warn_at = max(STALE_WARN, crit_at - 2)
            if stale_days >= crit_at:
                rec["flags"].append(("CRITICAL", f"stale {stale_days}d (max {max_date})"))
                fresh_score = 0.0
            elif stale_days >= warn_at:
                rec["flags"].append(("WARN", f"stale {stale_days}d (max {max_date})"))
                fresh_score = 0.5
            else:
                fresh_score = 1.0
    rec["max_date"] = str(max_date) if max_date else None
    rec["distinct_dates"] = distinct_dates or 0

    # ---- coverage
    brands = 0
    cov_score = 0.5
    if brand_col:
        brands = q1(cur, f'SELECT count(distinct {brand_col}) FROM "{table}"')
        if active_brands:
            ratio = brands / active_brands
            cov_score = min(ratio, 1.0)
            # note, not penalty: stock-based signals structurally exclude
            # DeFacto/Mobaco/LCW per-size, so <100% here is often correct.
            if ratio < 0.4:
                rec["flags"].append(("WARN", f"low coverage {brands}/{active_brands} brands"))
    rec["brands"] = brands

    # ---- history depth (diminishing returns; 30+ distinct dates = full)
    hist_score = min((distinct_dates or 0) / 30.0, 1.0)

    # ---- null integrity on meaningful output columns
    numeric = numeric_columns_of(cur, table)
    meaningful = [c for c in numeric
                  if c not in NULL_ALLOWED
                  and any(h in c for h in MEANINGFUL_COL_HINTS)]
    worst_null = 0.0
    worst_col = None
    for c in meaningful:
        nulls = q1(cur, f'SELECT count(*) FROM "{table}" WHERE "{c}" IS NULL')
        rate = nulls / n if n else 0
        if rate > worst_null:
            worst_null, worst_col = rate, c
    if worst_col:
        if worst_null >= NULL_CRIT:
            rec["flags"].append(("CRITICAL", f"{worst_col} {worst_null:.0%} null"))
        elif worst_null >= NULL_WARN:
            rec["flags"].append(("WARN", f"{worst_col} {worst_null:.0%} null"))
    null_score = 1.0 - min(worst_null / NULL_CRIT, 1.0)

    # ---- run health
    run_score = _attach_run(rec, table, runs, runs_today, run_ids)

    # ---- composite (weights sum to 100)
    sub = dict(freshness=fresh_score, run_health=run_score,
               null_integrity=null_score, coverage=cov_score,
               history=hist_score)
    rec["subscores"] = {k: round(v, 2) for k, v in sub.items()}
    rec["score"] = round(
        100 * (0.30*fresh_score + 0.20*run_score + 0.20*null_score
               + 0.15*cov_score + 0.15*hist_score)
    )
    return rec


def _level_of(table):
    if table.startswith("signal_l1_"):
        return "L1 signal"
    if table.startswith("signal_l2_"):
        return "L2 signal"
    return "L2 product"


def _attach_run(rec, table, runs, runs_today, run_ids):
    """Attach latest-run info and detect the same-day no-op re-run wipe hazard.
    Returns a run-health subscore in [0,1]."""
    sid = table_to_signal_id(table, run_ids)
    rec["signal_id"] = sid
    if not sid or sid not in runs:
        rec["run"] = None
        return 0.7  # products have no signal_runs row; treat as neutral-good
    r = runs[sid]
    rec["run"] = dict(status=r["status"], rows_written=r["rows_written"],
                      rows_suppressed=r["rows_suppressed"],
                      days_available=r["days_available"],
                      skip_reason=r["skip_reason"])
    score = 1.0
    if r["status"] == "error":
        rec["flags"].append(("CRITICAL", f"last run errored: {r['error_message']}"))
        score = 0.0
    elif r["status"] == "skipped":
        rec["flags"].append(("WARN", f"skipped: {(r['skip_reason'] or '')[:80]}"))
        score = 0.4
    # no-op re-run wipe hazard: >1 run today AND one of them wrote 0 rows
    td = runs_today.get(sid)
    if td and td["runs_today"] > 1 and (td["min_rows_today"] or 0) == 0 \
       and (td["max_rows_today"] or 0) > 0:
        rec["flags"].append(("WARN",
            f"{td['runs_today']} runs today; a re-run wrote 0 rows "
            "(delete-before-reinsert can wipe a good partition)"))
        score = min(score, 0.7)
    return score


# ------------------------------------------------------------------ substrate
def audit_substrate(cur):
    s = {}
    s["active_brands"] = q1(cur, "SELECT count(distinct brand) FROM products WHERE is_active")
    s["total_products"] = q1(cur, "SELECT count(*) FROM products")
    s["total_variants"] = q1(cur, "SELECT count(*) FROM product_variants")

    # hot price history
    s["snapshots_rows"] = q1(cur, "SELECT count(*) FROM price_snapshots")
    s["snapshots_min"]  = str(q1(cur, "SELECT min(snapshot_date)::date FROM price_snapshots"))
    s["snapshots_max"]  = str(q1(cur, "SELECT max(snapshot_date)::date FROM price_snapshots"))
    s["snapshots_brands"] = q1(cur, "SELECT count(distinct brand) FROM price_snapshots")

    # price_events
    s["price_events_rows"] = q1(cur, "SELECT count(*) FROM price_events")

    # ---- invariant checks -----------------------------------------------
    inv = {}
    # discount_pct must be ~100% NULL (never read raw). Non-null = someone wrote it.
    ps_dp_nonnull = q1(cur, "SELECT count(*) FROM price_snapshots WHERE discount_pct IS NOT NULL")
    inv["price_snapshots.discount_pct non-null (expect 0)"] = ps_dp_nonnull
    # delisted-but-in-stock: the known delisted_at-not-cleared bug
    delisted_in_stock = q1(cur, """
        SELECT count(*) FROM product_variants
        WHERE delisted_at IS NOT NULL AND is_in_stock = TRUE
    """)
    inv["product_variants delisted_at set but in_stock (known bug)"] = delisted_in_stock
    # FOP coverage
    fop_missing = q1(cur, "SELECT count(*) FROM product_variants WHERE first_observed_price IS NULL")
    inv["product_variants missing first_observed_price"] = fop_missing
    inv["FOP coverage %"] = round(100 * (1 - (fop_missing / s["total_variants"])), 2) if s["total_variants"] else None
    # core integrity
    inv["price_snapshots price <= 0"] = q1(cur, "SELECT count(*) FROM price_snapshots WHERE price <= 0")
    s["invariants"] = inv
    return s


def audit_r2():
    """Cold-tier depth from Cloudflare R2 parquet day-files. Optional."""
    need = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ACCOUNT_ID", "R2_BUCKET_NAME")
    if not all(os.environ.get(k) for k in need):
        return {"skipped": "R2 env not set"}
    try:
        import duckdb
    except ImportError:
        return {"skipped": "duckdb not installed"}
    try:
        con = duckdb.connect()
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute(f"""
            CREATE SECRET r2 (
                TYPE S3, KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
                SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
                ENDPOINT '{os.environ["R2_ACCOUNT_ID"]}.r2.cloudflarestorage.com',
                REGION 'auto', URL_STYLE 'path'
            );
        """)
        bucket = os.environ["R2_BUCKET_NAME"]
        out = {}
        for prefix in ("price_snapshots", "price_events", "stockout_events"):
            files = con.execute(
                f"SELECT file FROM glob('s3://{bucket}/{prefix}/*.parquet')"
            ).fetchall()
            days = sorted(f[0].split("/")[-1].replace(".parquet", "") for f in files)
            out[prefix] = dict(day_files=len(days),
                               earliest=days[0] if days else None,
                               latest=days[-1] if days else None)
        return out
    except Exception as e:  # R2 is best-effort; never fail the whole audit on it
        return {"error": str(e)}


# --------------------------------------------------------------------- render
def render(records, substrate, r2):
    crit = [r for r in records if any(f[0] == "CRITICAL" for f in r["flags"])]
    warn = [r for r in records if any(f[0] == "WARN" for f in r["flags"])
            and r not in crit]

    lines = []
    P = lines.append
    P(f"# Khabar Integrity Audit — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}\n")

    # verdict
    P("## Verdict")
    P(f"- Output tables audited: **{len(records)}**")
    P(f"- CRITICAL: **{len(crit)}**   WARN: **{len(warn)}**   "
      f"Clean: **{len(records)-len(crit)-len(warn)}**")
    ab = substrate.get("active_brands")
    P(f"- Active brands in catalogue: **{ab}**")
    P("")

    if crit:
        P("## CRITICAL")
        for r in crit:
            for lvl, msg in r["flags"]:
                if lvl == "CRITICAL":
                    P(f"- `{r['table']}` — {msg}")
        P("")
    if warn:
        P("## WARN")
        for r in warn:
            for lvl, msg in r["flags"]:
                if lvl == "WARN":
                    P(f"- `{r['table']}` — {msg}")
        P("")

    # per-table table
    P("## Per-table")
    P("| Table | Level | Rows | Dates | Brands | Max date | Score |")
    P("|---|---|---:|---:|---:|---|---:|")
    for r in sorted(records, key=lambda x: (-x["score"], x["table"])):
        P(f"| {r['table']} | {r['level']} | {r['rows']:,} | "
          f"{r['distinct_dates']} | {r['brands']} | {r['max_date'] or '—'} | "
          f"{r['score']} |")
    P("")

    # substrate
    P("## Substrate (what signals are computed from)")
    P(f"- products: {substrate['total_products']:,}  |  "
      f"variants: {substrate['total_variants']:,}")
    P(f"- price_snapshots (HOT): {substrate['snapshots_rows']:,} rows, "
      f"{substrate['snapshots_min']} → {substrate['snapshots_max']}, "
      f"{substrate['snapshots_brands']} brands")
    P(f"- price_events: {substrate['price_events_rows']:,} rows")
    P("")
    P("### Invariants")
    for k, v in substrate["invariants"].items():
        P(f"- {k}: **{v}**")
    P("")

    # R2
    P("## Cold tier (Cloudflare R2)")
    if "skipped" in r2:
        P(f"- skipped: {r2['skipped']}")
    elif "error" in r2:
        P(f"- error: {r2['error']}")
    else:
        for prefix, d in r2.items():
            P(f"- {prefix}: {d['day_files']} day-files, "
              f"{d['earliest']} → {d['latest']}")
    P("")

    return "\n".join(lines)


def main():
    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()

    run_map, runs_today = latest_runs(cur)
    run_ids = list(run_map.keys())
    substrate = audit_substrate(cur)
    active_brands = substrate.get("active_brands") or 0

    tables = discover_output_tables(cur)
    records = [audit_table(cur, t, active_brands, run_map, runs_today, run_ids)
               for t in tables]

    r2 = audit_r2()

    report_md = render(records, substrate, r2)
    print(report_md)

    with open("integrity_report.md", "w") as f:
        f.write(report_md)
    with open("integrity_report.json", "w") as f:
        json.dump(dict(generated_at=datetime.now(timezone.utc).isoformat(),
                       substrate=substrate, r2=r2, tables=records),
                  f, indent=2, default=str)

    cur.close()
    conn.close()

    has_critical = any(f[0] == "CRITICAL" for r in records for f in r["flags"])
    sys.exit(1 if has_critical else 0)


if __name__ == "__main__":
    main()
