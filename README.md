# khabar-engine

Data pipeline behind **Khabar** — cross-brand fashion retail intelligence for the Egyptian market. Scrapes 22+ brands, turns raw price/stock history into signals and client-facing intelligence products.

> **Finding a file fast:** press <kbd>t</kbd> on this page to fuzzy-search any filename. Type `sig`, `arch`, `report`, etc.

This README is a map, not machinery — it changes nothing about how the code runs. All files live flat in the repo root by design (GitHub Actions and Render reference them by name).

---

## The pipeline in one line

**Scrape** → store hot (Supabase) + cold (R2 archive) → **compute L1 signals** → **compute L2 products** → **generate client reports** → **deliver** (Telegram).

---

## Files by role

### Scrapers — data in
| File | What it does |
|---|---|
| [`scraper.py`](scraper.py) | Main multi-engine scraper (Shopify API, LCW proxy engine, DeFacto GraphQL, WooCommerce). The front door for all data. |

### Signal & product engines — data → intelligence
| File | What it does |
|---|---|
| [`khabar_lake.py`](khabar_lake.py) | Query layer stitching hot (Supabase, ~8 days) + cold (R2 Parquet) into one queryable lake via DuckDB. Imported by almost everything below. |
| [`signals.py`](signals.py) | **L1 registry** — declarative definitions of every L1 signal (the SQL + what it needs). Data, not logic. |
| [`compute_signals.py`](compute_signals.py) | **L1 runner** — walks the registry, computes each signal against the lake, writes results to Supabase aggregate tables. |
| [`products.py`](products.py) | **L2 registry** — declarative definitions of the L2 intelligence products (reads the small L1 tables). |
| [`compute_products.py`](compute_products.py) | **L2 runner** — same shape as the L1 runner, one step up the stack. |

### Client reports — intelligence → deliverables
| File | What it does |
|---|---|
| [`report_lib.py`](report_lib.py) | Shared plumbing for all reports (DB connection, folder/naming conventions, formatting). Every report imports this. |
| [`report_bestseller.py`](report_bestseller.py) | **Weekly** — "Market Demand & Stockout": how fast the market is clearing, what's selling out, where to buy next. |
| [`report_brand_health.py`](report_brand_health.py) | **Monthly** — "Supply Chain Stress / Brand Health": per-brand read on replenishment, availability, markdown distress, dead stock. |
| [`report_gap_map.py`](report_gap_map.py) | **Monthly** — "Brand Gap Map": price position, whitespace, timing, honesty — where money's left on the table. |
| [`report_integrity.py`](report_integrity.py) | **Weekly (internal)** — files `integrity_audit.py`'s output into the standard reports folder shape. |

### Storage & archive — the moat
| File | What it does |
|---|---|
| [`archive.py`](archive.py) | Copies `price_snapshots` from Supabase to the R2 cold archive (Parquet). Runs as its own job. |
| [`housekeeping.py`](housekeeping.py) | Weekly maintenance — archives + prunes the append-only event/rollup tables. |
| [`rollup.py`](rollup.py) | Weekly rollup of raw data into summary tables. |
| [`compaction.py`](compaction.py) | Monthly `VACUUM FULL` to reclaim Supabase space (was manual, now automated). |
| [`query_archive.py`](query_archive.py) | "Supabase SQL box, but for R2" — run DuckDB queries directly against the cold archive. |
| [`query_lake.py`](query_lake.py) | Ad-hoc query runner over the full hot+cold lake, for questions not decided in advance. |

### Delivery
| File | What it does |
|---|---|
| [`bot.py`](bot.py) | Telegram bot (long-poll). Primary client delivery + live-deals screen. Reads `price_events` directly. |

### Monitoring & audits
| File | What it does |
|---|---|
| [`health_check.py`](health_check.py) | Daily health check — catches silent pipeline failures. |
| [`integrity_audit.py`](integrity_audit.py) | Row counts + integrity checks for every deployed signal/product table. |
| [`verify_fop_immutability.py`](verify_fop_immutability.py) | One-off — verifies first-observed-price never got rewritten (the core honest-discount invariant). |

### Brand-specific tools & backfills
| File | What it does |
|---|---|
| [`taxonomy_backfill.py`](taxonomy_backfill.py) | Fills colors/subcategories across all categories, then audits results. |
| [`lcw_price_probe.py`](lcw_price_probe.py) | LCW-specific — resolves LCW's discounted-price quirk (sale price hides in `CampaignBadges`). |
| [`lcw_size_backfill.py`](lcw_size_backfill.py) | **Temporary** — clears the LCW size-data backlog via repeated manual runs. |
| [`mobaco_diagnostic.py`](mobaco_diagnostic.py) | Mobaco-specific — hunts for real color names (Mobaco's color field holds SKU codes). |

### One-time migrations — safe to ignore day-to-day
| File | What it does |
|---|---|
| [`r2_daily_backfill.py`](r2_daily_backfill.py) | One-time R2 re-partition: per-run files → one file per calendar day (`price_snapshots`). |
| [`r2_housekeeping_repartition.py`](r2_housekeeping_repartition.py) | Same, for the five event tables housekeeping archives. |
| [`recompress_archive.py`](recompress_archive.py) | One-time R2 re-compression (zstd → snappy) for Supabase S3-FDW readability. |

### Config
| File | What it does |
|---|---|
| [`requirements.txt`](requirements.txt) | Python dependencies. |
| [`.github/workflows/`](.github/workflows) | All GitHub Actions schedules (see clock below). |

---

## The clock (all times UTC)

**Daily**
- Shopify scrape — 04:00, 12:00, 20:00
- LCW scrape — two windows (~05:23–06:03 and 17:23–18:03)
- L1 signals — 05:00
- L2 products — 09:03
- Health check — 20:00

**Weekly (Monday), in strict order**
- 03:00 rollup → 03:20 archive → 03:40 housekeeping → 04:00 compaction
- 05:00 integrity audit
- 06:00 weekly reports (bestseller + integrity)

**Monthly**
- 1st @ 06:30 — monthly reports (brand health + gap map)

**Continuous**
- Telegram bot — every 6 hours

---

## Non-negotiable data invariants

- **Witnessed-discount rule:** discounts are measured against Khabar's own `first_observed_price`, never the brand-supplied `compare_at_price`. `discount_pct` is always derived at query time, never read from storage.
- **FOP immutability:** first-observed prices are never rewritten. That baseline is what every honest-discount number depends on.
- **Archive-before-delete:** nothing leaves Supabase until a verified copy exists in R2.
- **Storage window:** archive threshold must stay **strictly less than** the Supabase retention window. Equal or inverted = permanent data loss. Change one, change both in the same commit.
