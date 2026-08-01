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
import pandas as pd

import matplotlib
matplotlib.use("Agg")            # headless: no display in CI
import matplotlib.pyplot as plt

REPORTS_ROOT = Path(os.environ.get("REPORTS_ROOT", "reports"))

# Brands to keep OUT of client-facing reports: phantom / contaminated data still
# pending cleanup. Featuring them would put junk in front of a buyer.
EXCLUDE_BRANDS = {"tree", "dalydress"}


def drop_excluded(df, col="brand"):
    if df is None or df.empty or col not in df.columns:
        return df
    return df[~df[col].isin(EXCLUDE_BRANDS)]

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
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit("FATAL: SUPABASE_DB_URL not set.")
    return psycopg2.connect(dsn)


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
