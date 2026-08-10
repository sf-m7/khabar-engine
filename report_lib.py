"""
report_lib.py — shared plumbing for Khabar's periodical reports.
================================================================================
One place for the things every report needs, so each generator is just "pull
these signals, decide the verdict, say what to do." Everything that isn't
report-specific lives here: the database connection, the folder/naming
convention, CSV output, chart rendering, the confidence-tier stamp, and a small
Markdown builder that keeps every report visually consistent.

DESIGN STANCE (why the reports look the way they do)
A report a client reads is NOT market research. It opens with what to DO, ranks
those actions, and attaches to each a confidence tier backed by the actual
sample it came from. Method footnotes go at the BOTTOM. Numbers are always
computed here in code — never asserted — so a verdict is defensible line by line.

FOLDER CONVENTION (see build_run_dir)
  reports/<report-slug>/<period>/report.md
                                 data.csv
                                 charts/*.png
  reports/<report-slug>/latest/  <- overwritten copy of the newest run, so the
                                    bot / a client link always has a stable path.
Weekly reports use the period YYYY-MM-DD (the run's Monday); monthly use YYYY-MM.
Charts are pre-rendered PNGs referenced by RELATIVE path in the .md, so GitHub
renders them inline with zero runtime dependency.
"""

import os
import shutil
from pathlib import Path
from datetime import date

import psycopg2
from khabar_db import CA_BUNDLE
import pandas as pd

import warnings
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")
import matplotlib
matplotlib.use("Agg")            # headless: no display in CI
import matplotlib.pyplot as plt

REPORTS_ROOT = Path(os.environ.get("REPORTS_ROOT", "reports"))

# Brands kept OUT of client-facing reports. SINGLE SOURCE OF TRUTH is the
# ref_excluded_brands table (scope 'all' = phantom; 'stock' = fabricated stock).
# The hardcoded sets below are ONLY a crash-proof fallback if that table can't be
# read (older branch / not yet migrated), so a report never dies and never
# silently shows junk.
_FALLBACK_ALL   = {"tree", "dalydress"}
_FALLBACK_STOCK = {"lc_waikiki", "defacto", "mobaco"}

# Populated by refresh_exclusions(); start at fallback so imports never crash.
EXCLUDE_BRANDS       = set(_FALLBACK_ALL)                         # scope 'all'
EXCLUDE_STOCK_BRANDS = set(_FALLBACK_ALL) | set(_FALLBACK_STOCK)  # 'all' + 'stock'


def refresh_exclusions(conn):
    """Load exclusion sets from ref_excluded_brands (single source). Falls back
    to the hardcoded defaults if the table is unavailable. Called once by
    connect() so every report run uses the current table with no extra wiring."""
    global EXCLUDE_BRANDS, EXCLUDE_STOCK_BRANDS
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT brand, scope FROM ref_excluded_brands")
            rows = cur.fetchall()
        drop_all   = {b for b, s in rows if s == "all"}
        drop_stock = {b for b, s in rows if s == "stock"}
        EXCLUDE_BRANDS       = drop_all
        EXCLUDE_STOCK_BRANDS = drop_all | drop_stock
    except Exception:
        conn.rollback()
        EXCLUDE_BRANDS       = set(_FALLBACK_ALL)
        EXCLUDE_STOCK_BRANDS = set(_FALLBACK_ALL) | set(_FALLBACK_STOCK)
    return EXCLUDE_BRANDS, EXCLUDE_STOCK_BRANDS


def drop_excluded(df, col="brand"):
    """Drop phantom brands (scope 'all'). For PRICE-based reports."""
    if df is None or df.empty or col not in df.columns:
        return df
    return df[~df[col].isin(EXCLUDE_BRANDS)]


def drop_excluded_stock(df, col="brand"):
    """Drop phantom + fabricated-stock brands. For STOCK/size-based reports."""
    if df is None or df.empty or col not in df.columns:
        return df
    return df[~df[col].isin(EXCLUDE_STOCK_BRANDS)]

# Palette (from the Khabar playbook) — kept muted and consistent across charts.
INK   = "#1a2b2b"
TEAL  = "#2f6f6a"
AMBER = "#c98a2b"
RUST  = "#b4532a"
MUTE  = "#9aa7a7"
plt.rcParams.update({
    "figure.dpi": 150, "savefig.bbox": "tight",
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.edgecolor": MUTE, "axes.labelcolor": INK, "text.color": INK,
    "xtick.color": INK, "ytick.color": INK, "font.size": 10,
})


# --------------------------------------------------------------------- data
def connect():
    dsn = os.environ.get("KHABAR_DB_URL", "").strip() or os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit("FATAL: SUPABASE_DB_URL not set.")
    conn = psycopg2.connect(dsn, sslrootcert=CA_BUNDLE)
    refresh_exclusions(conn)          # load ref_excluded_brands (fallback-safe)
    return conn


def df_sql(conn, sql):
    return pd.read_sql_query(sql, conn)


def latest(conn, table, date_col="snapshot_date"):
    """Rows from the most recent snapshot of a signal table. Empty frame if the
    table is missing or empty — a report degrades gracefully rather than crash.
    Signal tables are already computed from the full hot+cold lake (their windows
    reach back months, far past Supabase's 8-day retention), so reading them is
    reading hot+cold. Only raw-price sections need the lake directly (below)."""
    try:
        return df_sql(conn, f"""
            SELECT * FROM {table}
            WHERE {date_col} = (SELECT max({date_col}) FROM {table})
        """)
    except Exception:
        return pd.DataFrame()


def lake_price_history(days=45):
    """Prices + category over `days`, stitched hot (Supabase) + cold (R2), for
    price-position work that must span our ENTIRE data — NOT just the 8-day hot
    window.

    Deliberately prices-only: it does NOT pull product_variants (452K) or
    variant_baselines (462K) the way khabar_lake.snapshots() does, because the
    underpricing calc needs no discount baseline. That trims the monthly Supabase
    read by ~75%. Cold parquet already carries brand/category/price, so the cold
    half is free (R2). The hot half joins only price_snapshots -> products for
    category. Dedup on snapshot_id (hot wins on overlap days) and the LCW price
    quarantine are BOTH preserved, so the result is identical to snapshots()
    for price purposes.

    Returns (dataframe[product_id, brand, category_normalized, price], source).
    Falls back to hot-only if R2/DuckDB is unavailable, with a visible label."""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    quarantine = ("NOT (brand = 'lc_waikiki' AND "
                  "CAST(snapshot_date AS DATE) <= DATE '2026-07-25')")
    try:
        import khabar_lake
        # keep the quarantine identical to the lake's, even if the date moves
        quarantine = khabar_lake._snapshot_quarantine_sql("snapshot_date", "brand")
        con = khabar_lake.connect()           # sets up R2 secret + pg attach
        bucket = os.environ["R2_BUCKET_NAME"]

        files = [f[0] for f in con.execute(
            f"SELECT file FROM glob('s3://{bucket}/price_snapshots/*.parquet')"
        ).fetchall()
                 if f[0].split("/")[-1].replace(".parquet", "") >= cutoff]
        if files:
            flist = ", ".join(f"'{x}'" for x in files)
            cold = (f"SELECT snapshot_id, product_id, brand, category_normalized, "
                    f"price, CAST(snapshot_date AS DATE) snapshot_date "
                    f"FROM read_parquet([{flist}])")
        else:
            cold = ("SELECT NULL::BIGINT snapshot_id, NULL::BIGINT product_id, "
                    "NULL::VARCHAR brand, NULL::VARCHAR category_normalized, "
                    "NULL::DOUBLE price, NULL::DATE snapshot_date WHERE FALSE")

        # hot half: price_snapshots + products (for category). NO variants.
        hot = (f"SELECT ps.id snapshot_id, ps.product_id, ps.brand, "
               f"pr.category_normalized, CAST(ps.price AS DOUBLE) price, "
               f"CAST(ps.snapshot_date AS DATE) snapshot_date "
               f"FROM pg.public.price_snapshots ps "
               f"JOIN pg.public.products pr ON pr.id = ps.product_id "
               f"WHERE CAST(ps.snapshot_date AS DATE) >= DATE '{cutoff}'")

        df = con.execute(f"""
            WITH u AS (SELECT *, 1 t FROM ({hot})
                       UNION ALL SELECT *, 2 t FROM ({cold})),
                 d AS (SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY snapshot_id ORDER BY t) rn FROM u)
            SELECT product_id, brand, category_normalized, price
            FROM d
            WHERE rn = 1 AND price > 0 AND {quarantine}
        """).df()
        return df, f"hot + cold (R2 lake, prices only, {days}d)"
    except Exception as e:
        conn = connect()
        df = df_sql(conn, f"""
            SELECT ps.product_id, ps.brand, pr.category_normalized, ps.price
            FROM price_snapshots ps JOIN products pr ON pr.id = ps.product_id
            WHERE ps.price > 0 AND {quarantine}
        """)
        conn.close()
        return df, f"hot only — lake unavailable ({str(e)[:50]})"


# ---------------------------------------------------------------- confidence
def confidence(brands=None, days=None, events=None, cycles=None):
    """Return (tier, stamp). The stamp is printed next to every verdict so the
    reader always sees the sample behind the claim. Tiers are deliberately
    conservative — a thin signal is 'maturing', never headlined as fact."""
    n = events or cycles or 0
    b = brands or 0
    d = days or 0
    if (b >= 10 and d >= 21) or n >= 500:
        tier = "confirmed"
    elif (b >= 5 and d >= 7) or n >= 100:
        tier = "directional"
    else:
        tier = "maturing"
    bits = []
    if brands is not None:  bits.append(f"{brands} brands")
    if days   is not None:  bits.append(f"{days}d")
    if events is not None:  bits.append(f"{events:,} events")
    if cycles is not None:  bits.append(f"{cycles:,} cycles")
    return tier, f"[{tier} · {', '.join(bits)}]" if bits else f"[{tier}]"


# ------------------------------------------------------------ freshness / floor
def stale_brands(conn, signal_table, date_col="snapshot_date", max_lag_days=2):
    """Brands whose newest row in `signal_table` lags the table's own max date by
    more than `max_lag_days`. Catches silent per-brand stalls — e.g. LC Waikiki
    froze in signal_l1_01_genuine_price_drop on 2026-07-31 while its inputs
    stayed fresh. Nothing else detects this. `signal_table` must be a trusted
    literal from report code (not user input). Returns {brand: days_stale};
    empty means all current."""
    q = f"""
        WITH per_brand AS (
            SELECT brand, max({date_col}) AS last_date
            FROM {signal_table} GROUP BY brand
        ), mx AS (SELECT max(last_date) AS m FROM per_brand)
        SELECT brand, (mx.m - last_date) AS days_stale
        FROM per_brand, mx
        WHERE (mx.m - last_date) > %s
        ORDER BY days_stale DESC
    """
    with conn.cursor() as cur:
        cur.execute(q, (max_lag_days,))
        return {b: int(d) for b, d in cur.fetchall()}


def pct_reliable(events, floor=5):
    """A percentage is shown only when it rests on >= `floor` observations.
    Below the floor a '100%' from 2 events is noise, not signal."""
    return (events or 0) >= floor


def cell_tier(events):
    """Per-CELL maturity, mirroring confidence()'s vocabulary but for one cell's
    own sample size. Keeps one shared vocabulary: confirmed / directional /
    maturing."""
    n = events or 0
    if n >= 10:
        return "confirmed"
    if n >= 5:
        return "directional"
    return "maturing"


# ------------------------------------------------------------- market rollup
def market_undersupply(conn):
    """Cross-brand under-supply by category x size, from raw witnessed
    stockout_events (covers all ~20 stock-usable brands, not the 15-brand
    revealed_demand product table). Joins the blueprint 'increase production'
    flag where available (partial coverage).

    Columns: category_normalized, stocked_out_size, brands, market_stockouts
    (brand-weighted — read beside brands), products, brands_increase, confidence.
    Exclusions come from the demand_grid SQL (single source)."""
    g = demand_grid(conn, ["category_normalized", "size"], min_stockouts=5)
    if g is None or g.empty:
        return g
    g = g[g["size"].notna() & ~g["size"].isin(["one_size", "kids_age", ""])].copy()
    g = g.rename(columns={"size": "stocked_out_size", "stockouts": "market_stockouts"})

    bp = df_sql(conn, """
        SELECT category_normalized, stocked_out_size,
               count(*) FILTER (WHERE production_signal LIKE 'increase production%') AS brands_increase
        FROM product_l2_02_production_blueprint
        WHERE report_date = (SELECT max(report_date) FROM product_l2_02_production_blueprint)
          AND brand NOT IN ('tree','dalydress')
        GROUP BY 1, 2
    """)
    if bp is not None and not bp.empty:
        g = g.merge(bp, on=["category_normalized", "stocked_out_size"], how="left")
    else:
        g["brands_increase"] = 0
    g["brands_increase"] = g["brands_increase"].fillna(0).astype(int)
    return g.sort_values("market_stockouts", ascending=False).reset_index(drop=True)


# ------------------------------------------------------------- P3: colour + grid
# canonical_color(): raw colour strings are free-text (~3,800 distinct, incl.
# Arabic and SKU codes). Collapses them to ~18 canonical colours by keyword,
# FIRST match wins (specific before base). One place; any report/chatbot calls
# it. Unmatched -> 'other' (shown as its own bucket, never hidden).
_COLOR_RULES = [
    ("اسود", "black"), ("black", "black"),
    ("off white", "white"), ("offwhite", "white"), ("ecru", "white"),
    ("cream", "white"), ("ivory", "white"), ("white", "white"),
    ("navy", "navy"),
    ("denim", "blue"), ("indigo", "blue"), ("turquoise", "blue"),
    ("petrol", "blue"), ("blue", "blue"),
    ("vison", "beige"), ("nude", "beige"), ("sand", "beige"), ("tan", "beige"),
    ("stone", "beige"), ("biscuit", "beige"), ("beige", "beige"),
    ("anthracite", "grey"), ("charcoal", "grey"), ("grey", "grey"),
    ("gray", "grey"), ("silver", "grey"),
    ("coffee", "brown"), ("camel", "brown"), ("chocolate", "brown"),
    ("mink", "brown"), ("taupe", "brown"), ("brown", "brown"),
    ("khaki", "olive"), ("olive", "olive"),
    ("mint", "green"), ("teal", "green"), ("emerald", "green"), ("green", "green"),
    ("rose", "pink"), ("fuchsia", "pink"), ("fuschia", "pink"),
    ("magenta", "pink"), ("salmon", "pink"), ("pink", "pink"),
    ("burgundy", "red"), ("maroon", "red"), ("wine", "red"),
    ("crimson", "red"), ("bordeaux", "red"), ("red", "red"),
    ("mustard", "yellow"), ("gold", "yellow"), ("yellow", "yellow"),
    ("peach", "orange"), ("coral", "orange"), ("apricot", "orange"),
    ("orange", "orange"),
    ("lilac", "purple"), ("lavender", "purple"), ("mauve", "purple"),
    ("violet", "purple"), ("plum", "purple"), ("purple", "purple"),
    ("multi", "multi"), ("colored", "multi"), ("colour", "multi"),
    ("print", "multi"), ("floral", "multi"), ("striped", "multi"),
]


def canonical_color(raw):
    if not raw:
        return "other"
    c = str(raw).strip().lower()
    if not c:
        return "other"
    for kw, canon in _COLOR_RULES:
        if kw in c:
            return canon
    return "other"


def demand_grid(conn, group_cols, min_stockouts=10):
    """Cross-brand witnessed demand at a chosen grain. group_cols is any subset of
    ['category_normalized','subcategory','gender','color','size']. Returns that
    grain with products (distinct), stockouts (events), confidence (per cell).

    Colour normalised via canonical_color(); brand aggregated away so the five
    stock-excluded brands are filtered in SQL. subcategory is ~49% populated
    upstream, so a report using it must disclose coverage. Reads the 60-day
    retained stockout_events."""
    df = df_sql(conn, """
        SELECT p.category_normalized, p.subcategory, p.gender,
               se.size, lower(trim(se.color)) AS color_raw,
               pv.color_family, se.product_id, se.brand
        FROM stockout_events se
        LEFT JOIN product_variants pv ON pv.id = se.variant_id
        JOIN products p ON p.id = se.product_id
        WHERE se.witnessed AND se.event_type = 'stockout'
          AND se.brand NOT IN ('tree','dalydress','lc_waikiki','defacto','mobaco')
    """)
    if df is None or df.empty:
        return df
    if "color" in group_cols:
        df["color"] = df["color_family"].fillna(df["color_raw"].map(canonical_color))
        df["color"] = df["color"].fillna("other")
    g = (df.groupby(group_cols)
           .agg(brands=("brand", "nunique"),
                products=("product_id", "nunique"),
                stockouts=("product_id", "size"))
           .reset_index())
    g = g[g["stockouts"] >= min_stockouts].copy()
    g["confidence"] = g["stockouts"].apply(cell_tier)
    return g.sort_values("stockouts", ascending=False).reset_index(drop=True)


def color_price(conn, min_n=25):
    """Median product price by category x canonical colour — the colour price
    lever. Dedupes to one price per (product, colour). Returns category, color,
    n, med. Price-based, so only phantom brands (scope 'all') are dropped."""
    df = df_sql(conn, """
        SELECT p.category_normalized, ps.product_id, ps.price,
               pv.color_family, lower(trim(pv.color)) AS color_raw
        FROM price_snapshots ps
        JOIN products p  ON p.id = ps.product_id
        JOIN product_variants pv ON pv.product_id = ps.product_id
        WHERE ps.snapshot_date = (SELECT max(snapshot_date) FROM price_snapshots)
          AND ps.price > 0 AND pv.color IS NOT NULL AND pv.color <> ''
          AND ps.brand NOT IN ('tree','dalydress')
    """)
    if df is None or df.empty:
        return df
    df["color"] = df["color_family"].fillna(df["color_raw"].map(canonical_color))
    df = df[df["color"].notna() & (df["color"] != "other")].drop_duplicates(["product_id", "color"])
    g = (df.groupby(["category_normalized", "color"])
           .agg(n=("price", "size"), med=("price", "median")).reset_index())
    return g[g["n"] >= min_n].reset_index(drop=True)


def demand_trend(conn, group_cols, weeks=8, min_total=20):
    """Warming/cooling per cell from weekly witnessed stockouts. group_cols is any
    subset of the grid dims (colour normalised). Returns each cell with total,
    first-half vs last-half, and a trend label. Young now (~weeks of history) —
    directional; it sharpens as history accumulates. Reads 60-day stockout_events."""
    df = df_sql(conn, f"""
        SELECT date_trunc('week', se.recorded_at)::date AS wk,
               p.category_normalized, p.subcategory, p.gender,
               lower(trim(se.color)) AS color_raw, se.product_id
        FROM stockout_events se
        JOIN products p ON p.id = se.product_id
        WHERE se.witnessed AND se.event_type = 'stockout'
          AND se.brand NOT IN ('tree','dalydress','lc_waikiki','defacto','mobaco')
          AND se.recorded_at >= CURRENT_DATE - {int(weeks) * 7}
    """)
    if df is None or df.empty:
        return df
    if "color" in group_cols:
        df["color"] = df["color_raw"].map(canonical_color)
    wc = df.groupby(group_cols + ["wk"]).size().reset_index(name="stockouts")

    out = []
    for key, sub in wc.groupby(group_cols):
        s = sub.sort_values("wk")["stockouts"].tolist()
        total = sum(s)
        if total < min_total or len(s) < 3:
            continue
        h = len(s) // 2
        first, last = sum(s[:h]) or 1, sum(s[h:])
        ratio = last / first
        trend = "warming" if ratio >= 1.15 else "cooling" if ratio <= 0.85 else "steady"
        row = dict(zip(group_cols if isinstance(key, tuple) else [group_cols[0]],
                       key if isinstance(key, tuple) else [key]))
        row.update(total=total, trend=trend, weeks=len(s))
        out.append(row)
    import pandas as _pd
    res = _pd.DataFrame(out)
    return res.sort_values("total", ascending=False).reset_index(drop=True) if not res.empty else res


def bestsellers(conn, group_cols, rank_max=30, min_count=5):
    """Market best-sellers = products on brands' own best-seller lists (latest
    week, best rank <= rank_max), aggregated to a chosen grain. Proven demand,
    complements under-supply; the overlap of the two is 'low-hanging fruit'.
    group_cols is any subset of ['category_normalized','subcategory','gender'].
    Phantom brands + uncategorized dropped."""
    df = df_sql(conn, f"""
        SELECT wbs.product_id, p.category_normalized, p.subcategory, p.gender
        FROM weekly_bestseller_summary wbs
        JOIN products p ON p.id = wbs.product_id
        WHERE wbs.week_start = (SELECT max(week_start) FROM weekly_bestseller_summary)
          AND wbs.rank_best <= {int(rank_max)}
          AND wbs.brand NOT IN ('tree','dalydress')
          AND p.category_normalized IS NOT NULL
          AND p.category_normalized NOT IN ('uncategorized','')
    """)
    if df is None or df.empty:
        return df
    g = (df.groupby(group_cols)["product_id"].nunique()
           .reset_index(name="bestsellers"))
    return g[g["bestsellers"] >= min_count].sort_values(
        "bestsellers", ascending=False).reset_index(drop=True)


def order_verdicts(conn, min_stockouts=30, min_coverage=0.5):
    """P4 fusion for the buy decision. Combines, at category x subcategory:
    under-supply (demand_grid), proven demand (bestsellers), and timing
    (demand_trend). Produces a ranked board with a VERDICT, the reasons (why),
    and the timing (when). Catches the trap a naive under-supply read misses:
    under-supplied BUT cooling = the market retreating, not an opportunity.

    Distress from liquidation is category-level only, too coarse to penalise a
    hot subcategory, so timing (subcategory-level) is the anti-signal instead.
    Returns columns incl. verdict, why (list), trend, score, plus the inputs."""
    d = demand_grid(conn, ["category_normalized", "subcategory"], min_stockouts)
    if d is None or d.empty:
        return d
    d = d[d["subcategory"].notna() & (d["subcategory"] != "")].copy()

    # Coverage guard: subcategory tagging is ~80% for core apparel but 0% for
    # accessories/footwear. Only trust the subcategory board where a category's
    # demand is mostly tagged; low-coverage categories stay in the size view.
    cov = df_sql(conn, """
        SELECT p.category_normalized,
               avg((p.subcategory IS NOT NULL AND p.subcategory <> '')::int)::float AS coverage
        FROM stockout_events se JOIN products p ON p.id = se.product_id
        WHERE se.witnessed AND se.event_type = 'stockout'
          AND se.brand NOT IN ('tree','dalydress','lc_waikiki','defacto','mobaco')
          AND p.category_normalized NOT IN ('uncategorized','')
        GROUP BY 1
    """)
    if cov is not None and not cov.empty:
        keep = set(cov[cov["coverage"] >= min_coverage]["category_normalized"])
        d = d[d["category_normalized"].isin(keep)].copy()
        d = d.merge(cov, on="category_normalized", how="left")
    if d.empty:
        return d

    key = ["category_normalized", "subcategory"]
    b = bestsellers(conn, key, min_count=1)
    if b is not None and not b.empty:
        d = d.merge(b, on=key, how="left")
    if "bestsellers" not in d.columns:
        d["bestsellers"] = 0
    d["bestsellers"] = d["bestsellers"].fillna(0).astype(int)

    t = demand_trend(conn, key, weeks=8, min_total=20)
    if t is not None and not t.empty:
        d = d.merge(t[key + ["trend"]], on=key, how="left")
    if "trend" not in d.columns:
        d["trend"] = "steady"
    d["trend"] = d["trend"].fillna("steady")

    d["proven"] = d["bestsellers"] >= 3

    def _verdict(r):
        proven, tr = r["proven"], r["trend"]
        if proven and tr != "cooling":
            return "STRONG BUY"
        if (proven and tr == "cooling") or (not proven and tr == "warming"):
            return "BUY"
        return "WATCH"

    def _why(r):
        tags = ["under-supplied"]
        if r["proven"]:
            tags.append(f"proven seller ({int(r['bestsellers'])})")
        tags.append({"warming": "warming", "cooling": "cooling",
                     "steady": "steady demand"}[r["trend"]])
        return tags

    _boost = {"warming": 1.25, "steady": 1.0, "cooling": 0.7}
    d["verdict"] = d.apply(_verdict, axis=1)
    d["why"] = d.apply(_why, axis=1)
    d["score"] = (d["stockouts"]
                  * d["proven"].map({True: 1.6, False: 1.0})
                  * d["trend"].map(_boost).fillna(1.0))
    return d.sort_values("score", ascending=False).reset_index(drop=True)


def discount_verdicts(conn, min_events=20):
    """P4 fusion for the discount decision, per category. Fuses discount depth +
    clearance effectiveness (l2_01) with distress (l2_12) into a market read:
      ACTIVE CLEARANCE  - discounting hard AND it's moving (expect competitor cuts)
      STICKY DISTRESS   - deep discounts NOT clearing, dead stock rising (don't chase)
      HEALTHY MARKDOWN  - discounts clear efficiently, market calm
      FIRM              - little clearing via discount; sells near full price
    Category-level, so subcategory coverage is not a factor. Phantom brands out."""
    el = df_sql(conn, """
        SELECT brand, category_normalized, products_with_drops, stockout_events,
               avg_drop_pct, pct_stockouts_while_discounted
        FROM product_l2_01_price_elasticity
        WHERE report_date = (SELECT max(report_date) FROM product_l2_01_price_elasticity)
    """)
    di = df_sql(conn, """
        SELECT brand, category_normalized, escalating_products, dead_stock_products,
               distress_level
        FROM product_l2_12_liquidation_calendar
        WHERE report_date = (SELECT max(report_date) FROM product_l2_12_liquidation_calendar)
    """)
    el = drop_excluded(el)
    di = drop_excluded(di)
    if el is None or el.empty:
        return el

    el["_num"] = el["pct_stockouts_while_discounted"].fillna(0) / 100.0 * el["stockout_events"].fillna(0)
    g = el.groupby("category_normalized").agg(
        events=("stockout_events", "sum"),
        avg_depth=("avg_drop_pct", "mean"),
        _num=("_num", "sum")).reset_index()
    g["clear"] = (100.0 * g["_num"] / g["events"].replace(0, 1)).round(1)
    g["avg_depth"] = g["avg_depth"].round(1)

    def _drank(s):
        s = str(s)
        return 3 if s.startswith("urgent") else 2 if s == "watch" else 1
    di["_r"] = di["distress_level"].map(_drank)
    d = di.groupby("category_normalized").agg(
        esc=("escalating_products", "sum"),
        dead=("dead_stock_products", "sum"),
        rank=("_r", "max")).reset_index()

    m = g.merge(d, on="category_normalized", how="left").fillna({"esc": 0, "dead": 0, "rank": 1})
    m = m[m["events"] >= min_events].copy()
    if m.empty:
        return m

    def _verdict(r):
        clears = r["clear"] >= 15
        urgent = r["rank"] == 3
        if urgent and clears:
            return "ACTIVE CLEARANCE"
        if urgent and not clears:
            return "STICKY DISTRESS"
        if clears:
            return "HEALTHY MARKDOWN"
        return "FIRM"

    def _why(r):
        t = [f"depth {r['avg_depth']:.0f}%", f"clears {r['clear']:.0f}%"]
        if r["esc"]:
            t.append(f"{int(r['esc'])} escalating")
        if r["dead"]:
            t.append(f"{int(r['dead'])} dead stock")
        return t

    m["verdict"] = m.apply(_verdict, axis=1)
    m["why"] = m.apply(_why, axis=1)
    m["distress"] = m["rank"].map({3: "urgent", 2: "watch", 1: "normal"})
    return m.sort_values("events", ascending=False).reset_index(drop=True)


# ------------------------------------------------------------------ folders
def build_run_dir(slug, cadence):
    """cadence: 'weekly' -> YYYY-MM-DD, 'monthly' -> YYYY-MM."""
    today = date.today()
    period = today.strftime("%Y-%m-%d") if cadence == "weekly" else today.strftime("%Y-%m")
    run = REPORTS_ROOT / slug / period
    (run / "charts").mkdir(parents=True, exist_ok=True)
    return run, period


def copy_to_latest(run_dir, slug):
    latest_dir = REPORTS_ROOT / slug / "latest"
    if latest_dir.exists():
        shutil.rmtree(latest_dir)
    shutil.copytree(run_dir, latest_dir)


# -------------------------------------------------------------------- charts
def _finish(ax, title, path):
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold", color=INK)
    plt.tight_layout()
    plt.savefig(path)
    plt.close()
    return f"charts/{Path(path).name}"


def bar(run_dir, name, labels, values, title, color=TEAL, xlabel=""):
    fig, ax = plt.subplots(figsize=(7, max(2.2, 0.42 * len(labels))))
    ax.barh(range(len(labels)), values, color=color)
    ax.set_yticks(range(len(labels))); ax.set_yticklabels(labels)
    ax.invert_yaxis()
    if xlabel: ax.set_xlabel(xlabel)
    return _finish(ax, title, run_dir / "charts" / f"{name}.png")


def lines(run_dir, name, x, series, title, ylabel=""):
    """series: dict label -> (values, color)."""
    fig, ax = plt.subplots(figsize=(7, 3.4))
    for label, (vals, color) in series.items():
        ax.plot(x, vals, marker="o", label=label, color=color, linewidth=2)
    if ylabel: ax.set_ylabel(ylabel)
    ax.legend(frameon=False)
    plt.xticks(rotation=45, ha="right")
    return _finish(ax, title, run_dir / "charts" / f"{name}.png")


def scatter(run_dir, name, x, y, labels, title, xlabel, ylabel):
    fig, ax = plt.subplots(figsize=(6.5, 4.5))
    ax.scatter(x, y, color=TEAL, s=50)
    for xi, yi, li in zip(x, y, labels):
        ax.annotate(li, (xi, yi), fontsize=8, xytext=(4, 4),
                    textcoords="offset points", color=INK)
    ax.set_xlabel(xlabel); ax.set_ylabel(ylabel)
    return _finish(ax, title, run_dir / "charts" / f"{name}.png")


# ------------------------------------------------------------------ markdown
class Report:
    """Accumulates a report and writes report.md + data.csv, then mirrors the
    whole run into /latest. Keeps a running list of decision-brief actions so
    the top of the report can always lead with 'what to do'."""

    def __init__(self, slug, title, cadence, subtitle=""):
        self.slug = slug
        self.cadence = cadence
        self.run_dir, self.period = build_run_dir(slug, cadence)
        self.title = title
        self.subtitle = subtitle
        self.actions = []      # (priority, text, stamp)
        self.body = []
        self.method = []

    # decision brief ----------------------------------------------------
    def action(self, text, stamp="", priority=2):
        """priority: 1 = do first. Collected and printed at the very top."""
        self.actions.append((priority, text, stamp))

    # body --------------------------------------------------------------
    def h2(self, t):     self.body.append(f"\n## {t}\n")
    def p(self, t):      self.body.append(t + "\n")
    def do(self, t):     self.body.append(f"\n**→ Do:** {t}\n")
    def note(self, t):   self.method.append(f"- {t}")
    def img(self, rel, caption=""):
        self.body.append(f"\n![{caption}]({rel})")
        if caption:
            self.body.append(f"*{caption}*\n")
        else:
            self.body.append("")

    def table(self, df, cols=None, headers=None):
        if df.empty:
            self.body.append("_No qualifying rows this period._\n"); return
        d = df[cols] if cols else df
        head = headers or list(d.columns)
        self.body.append("| " + " | ".join(map(str, head)) + " |")
        self.body.append("|" + "|".join(["---"] * len(head)) + "|")
        for _, r in d.iterrows():
            self.body.append("| " + " | ".join("" if pd.isna(v) else str(v)
                                                for v in r.tolist()) + " |")
        self.body.append("")

    def gap(self, section, why):
        """A section that couldn't run — named honestly, never silently dropped."""
        self.body.append(f"\n## {section}\n")
        self.body.append(f"_Not available this run — {why}._\n")

    # data --------------------------------------------------------------
    def save_csv(self, df, name="data.csv"):
        df.to_csv(self.run_dir / name, index=False)

    # write -------------------------------------------------------------
    def write(self):
        lines_out = [f"# {self.title}"]
        if self.subtitle:
            lines_out.append(f"*{self.subtitle}*")
        lines_out.append("")

        # decision brief first — ranked, each with its confidence stamp
        lines_out.append("## Decision brief — what to act on")
        if self.actions:
            for _, text, stamp in sorted(self.actions, key=lambda a: a[0]):
                lines_out.append(f"- {text} {stamp}".rstrip())
        else:
            lines_out.append("- Nothing crossed the action threshold this period.")
        lines_out.append("")

        lines_out.extend(self.body)

        if self.method:
            lines_out.append("\n---\n### Method & confidence")
            lines_out.extend(self.method)
        lines_out.append(f"\n*Auto-generated {date.today()}. "
                         f"Confidence tiers: confirmed / directional / maturing — "
                         f"based on brands covered, days observed, and event volume.*")

        (self.run_dir / "report.md").write_text("\n".join(lines_out))
        copy_to_latest(self.run_dir, self.slug)
        print(f"✅ {self.slug}: {self.run_dir/'report.md'}")
        return self.run_dir
