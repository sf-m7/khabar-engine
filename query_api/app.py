"""
Khabar — ad-hoc HTTP query layer for R2 + Supabase, for Claude/Mohammed use.
================================================================================
This is separate from compute_signals.py. That script runs khabar_lake.py on
a GitHub Actions schedule to compute L1/L2 signals. This file exposes the
SAME khabar_lake.py — unmodified, imported directly — over HTTP, so it can
be queried ad-hoc from a chat instead of only from a scheduled job.

No query logic is duplicated here. Everything correctness-critical (the
hot/cold dedup, the honest_discount_pct baseline, the witnessed-events
filter) lives in khabar_lake.py and is inherited as-is. This file only adds:
  - a bearer-token gate
  - a SELECT-only guard on raw SQL
  - an in-process cache so repeated questions don't each re-pull Supabase
"""

import os
import re
import threading
import time
from datetime import datetime

from flask import Flask, request, jsonify

import khabar_lake

app = Flask(__name__)


@app.errorhandler(Exception)
def _handle_exception(e):
    """
    Without this, any unhandled error (a bad DB password, an R2 auth
    failure, a DuckDB extension that failed to download) produces Flask's
    generic blank 500 page — the real reason only shows up in Render's
    server logs, not in the response. This surfaces it directly instead.
    """
    return jsonify({"error": str(e), "type": type(e).__name__}), 500


API_TOKEN = os.environ["QUERY_API_TOKEN"]
REFRESH_SECONDS = int(os.environ.get("QUERY_API_REFRESH_SECONDS", "1800"))  # 30 min
MAX_ROWS = int(os.environ.get("QUERY_API_MAX_ROWS", "2000"))
DEFAULT_WINDOW_DAYS = int(os.environ.get("QUERY_API_WINDOW_DAYS", "90"))

_lock = threading.Lock()
_con = None
_last_refresh = None

# Read-only surface. Blocks anything that isn't a plain SELECT, even though
# the Postgres side is already ATTACHed READ_ONLY in khabar_lake.connect() —
# this is a second, independent gate at the API boundary.
FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|DETACH|COPY|PRAGMA|CREATE|"
    r"EXPORT|IMPORT|CALL|GRANT|REVOKE|VACUUM)\b",
    re.IGNORECASE,
)


def _ensure_lake(force=False):
    """
    Materialise the lake once per process, then reuse across requests.

    khabar_lake's own docs measured 300-500MB/day of Supabase egress from ONE
    scheduled job doing a full prefetch. An API that re-pulled on every chat
    question would be worse. So: pull once, cache in process memory, refresh
    only every REFRESH_SECONDS (default 30 min) or on explicit /refresh.

    Note: if this is hosted on a free tier that sleeps when idle, the cache
    is lost on every cold start, and the first request after a sleep pays a
    full prefetch again. That's a real cost tradeoff of the free tier, not a
    bug — flagged here so it isn't a surprise later.
    """
    global _con, _last_refresh
    with _lock:
        stale = (
            _con is None
            or force
            or _last_refresh is None
            or (time.time() - _last_refresh) > REFRESH_SECONDS
        )
        if stale:
            con = khabar_lake.connect()
            khabar_lake.prefetch(con)
            khabar_lake.snapshots(con, days=DEFAULT_WINDOW_DAYS)
            khabar_lake.stockout_events(con)
            _con = con
            _last_refresh = time.time()
    return _con


def _check_auth():
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {API_TOKEN}"


@app.before_request
def _auth_guard():
    if request.path == "/health":
        return
    if not _check_auth():
        return jsonify({"error": "unauthorized"}), 401


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/refresh", methods=["POST"])
def refresh():
    """Force a fresh pull from R2 + Supabase instead of waiting for the TTL."""
    _ensure_lake(force=True)
    return jsonify({"status": "refreshed", "at": datetime.utcnow().isoformat()})


@app.route("/schema")
def schema():
    """
    Lists every table/view currently queryable and its columns, so a caller
    (Claude, in a future chat) can introspect the real shape instead of
    guessing column names.
    """
    con = _ensure_lake()
    tables = con.execute("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'main'
        ORDER BY table_name, ordinal_position
    """).fetchall()
    out = {}
    for t, c, d in tables:
        out.setdefault(t, []).append(f"{c} ({d})")
    return jsonify(out)


@app.route("/query", methods=["POST"])
def query():
    """
    Body: {"sql": "SELECT brand, count(*) FROM snapshots GROUP BY brand"}

    Runs against the already-materialised local tables/views from
    khabar_lake.py: snapshots, stockouts_raw, price_events_raw, products_dim,
    variant_baselines, stock_events, hot_raw, variants_raw. Call /schema
    first if unsure what's available.
    """
    body = request.get_json(force=True, silent=True) or {}
    sql = (body.get("sql") or "").strip()

    if not sql:
        return jsonify({"error": "missing 'sql'"}), 400
    if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.IGNORECASE):
        return jsonify({"error": "only SELECT / WITH queries are allowed"}), 400
    if FORBIDDEN.search(sql):
        return jsonify({"error": "query contains a forbidden keyword"}), 400

    con = _ensure_lake()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]

    return jsonify({
        "columns": cols,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
        "lake_refreshed_at": datetime.utcfromtimestamp(_last_refresh).isoformat(),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
