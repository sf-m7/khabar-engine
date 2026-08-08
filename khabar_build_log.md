# Khabar — Build Log (Commercialization / Report Suite)

> **Purpose.** One append-only document so that re-entering this work is *reading*, not
> rediscovery. Every step, decision, and the reason behind it goes here. Newest entries
> at the bottom of the Change Log. Nothing here is deployed unless the Change Log says so.

> **Two standing constraints (from the founder):**
> 1. Understand every rule that produced the current system/data *before* changing it.
> 2. Minimise moving parts. Where a part is unavoidable, it must not be spaghetti —
>    each rule lives in exactly **one** place, and consumers read from it.

---

## 1. Rule Inventory — where every governance rule lives TODAY
*Verified from source (`sf-m7/khabar-engine`, main) on 2026-08-08, not from memory.*

| Rule | Lives in | Form | Notes |
|---|---|---|---|
| Phantom exclusion (`tree`, `dalydress`) | `report_lib.py` | `EXCLUDE_BRANDS = {"tree","dalydress"}` + `drop_excluded(df)` | Report layer only. **Not** applied upstream, so phantom brands remain in `product_l2_*` tables and must be dropped by any consumer. |
| Stock-brand exclusion (`lc_waikiki`, `defacto`, `mobaco`) | `signals.py` | inline `WHERE brand NOT IN (...)` | Repeated in ≥3 signal SQL blocks (≈ lines 1596, 1657, 1790). Duplicated literals — the one existing smell. |
| Tree/Dalydress upstream handling | `signals.py` (~L800–825) | general "mass-delist anomaly day" rule | Catches Dalydress 06-18 collection bug (68% catalogue delist in 24h). Deliberately retains Tree (19.7%) / Mobaco (10.8%) below the 20% line. So phantom brands are **not** globally excluded upstream. |
| LCW price quarantine | `report_lib._price_history` + `khabar_lake._snapshot_quarantine_sql` | SQL predicate | Single place. |
| Sample floors | `products.py` verdict SQL | inline `stockout_count >= 2 / 3 / 5` etc. | Per-product. Gate the **verdict**, but thin rows are still emitted with raw counts (no uniform % suppression). |
| Confidence stamp | `report_lib.confidence(brands, days, events, cycles)` | Python fn → string | Already printed in report footers ("based on brands covered, days observed, and event volume"). |
| Deep hot+cold history reader | `report_lib._price_history` | returns hot + R2 cold, quarantine applied | This is the continuity/moat reader — **already exists**. |
| Product build pattern | `compute_products.py` + `products.py` | replace-by-`report_date`, append across days | Idempotent per day; history accumulates. One psycopg2 connection, plain Postgres, no DuckDB at L2. |
| Retention / purge | `housekeeping.py`, `archive.py` | verify-before-delete → R2 | Purges only `stockout_events` (>60d), `price_events` (>30d), two weekly summaries (>12wk). **Does not touch `product_l2_*` / `signal_l2_*`.** `archive.py` has a *designed, dry-run-gated* Phase-2 to archive L2 tables to R2 later. |

**Implication:** the "read / trust / continuity layer" is **already `report_lib.py`.** It owns exclusion,
confidence, quarantine, and deep-history reads. New reports must reuse it, not reimplement it.

---

## 2. Verified data facts (live DB, 2026-08-07/08)

- L2 product tables accumulate daily: 18 snapshots, 2026-07-21 → 08-07. Signal aggregates too.
- Hot `price_snapshots` = rolling ~13-day window; deep history (→ May) is **R2 cold only**, read via the lake.
- 7 of 14 catalogued L2 products are **live and populated** (01,02,08,09,10,12,13). Notion "0 built" was stale.
- 15 L1 signals materialise & run ok; `l1_02`, `l1_16` are coded but **skipped**.
- Phantom leak: Dalydress in all 4 priority product tables, Tree in 2. ~262 rows each in blueprint/revealed.
- Thin cells: 272 of 494 kept `revealed_demand` rows (55%) sit below a 5-event floor.
- Coverage differs per table: blueprint/revealed 14 brands, elasticity 18, liquidation 22.
- **Anomaly:** `signal_l1_01_genuine_price_drop` for `lc_waikiki` frozen at 2026-07-31 while its inputs
  (`price_events`, `l1_17`) are fresh to 08-08. A signal-specific stall, **not** a scraper-order or feed problem. Cause TBD (needs `l1_01` SQL review).
- Misleading column: `product_l2_02.undersupply_signal` (integer) ≈ 0 across the board while the text
  `production_signal` carries the real verdict (1,230 "increase production" rows). **Read the text, not the integer.**
- Naming overclaim: `product_l2_01_price_elasticity` is discount-depth + %stockouts-while-discounted, **not** elasticity.

---

## 3. Phase 0 — Trust/Read Layer — REVISED after reading source

**Original (WRONG) plan:** create `ref_excluded_brands` + six `rl_*` views doing exclusion + confidence + floors + relabel.
**Why killed:** confidence, exclusion, and floors already exist (`report_lib`, `products.py`). The views would have been a *second copy* of existing rules — the exact spaghetti the constraints forbid.

**Revised P0 (minimal):**
1. `report_lib.py` is the single read/trust seam. New interactive-HTML reports import it; they never read raw `product_l2_*`.
2. Add the **one genuinely missing rule** — a per-brand, per-signal **freshness guard** — as a single function in `report_lib.py` (e.g. `stale_brands(signal_table, max_lag_days=2)`), returning brands whose latest `snapshot_date` lags the table max. This is what catches the LCW `l1_01` freeze; nothing does it today.
3. Report-authoring rules (in templates, not infra): use `production_signal` text (never `undersupply_signal`); relabel "elasticity" → "discount-to-clearance".
4. Verify whether `confidence()` already suppresses sub-floor percentages; add a tiny helper only if it doesn't. Do **not** re-implement floors.

**Retention:** no new mechanism. Documented home = `archive.py` Phase-2 (verify-before-delete to R2), currently inactive/dry-run-gated, so L2 history is safe now. Standing rule: Phase-2 must archive to R2 before any delete.

---

## 4. Open decision — exclusions single-source (blocks nothing else)

Exclusions live in Python (`report_lib`), but the planned chatbot runs **SQL** and will need the same rules.

- **Option A** — keep exclusions in `report_lib` only. Smallest now; a second (SQL) copy appears when the chatbot arrives → two sources.
- **Option B (recommended)** — one `ref_excluded_brands(brand, scope, reason)` table, read by both `report_lib` (replacing the hardcoded set) and future SQL consumers. One extra object, but the single source Python + SQL share. `scope ∈ {all, stock}`: `all` = tree, dalydress; `stock` = lc_waikiki, defacto, mobaco.

*Note:* the inline `brand NOT IN (...)` literals in `signals.py` are upstream and out of P0 scope. If B is chosen, a later refactor can point them at the table too; until then, the table must be kept in sync with those literals (documented, not silently divergent).

---

## 5. Anti-spaghetti principles adopted for this work

1. **One rule, one home.** No rule is implemented in two layers. If it exists upstream, downstream inherits it.
2. **The read layer filters, stamps, relabels — it never recomputes.** Any math already done in `products.py`/`signals.py` is read, not redone.
3. **New reports depend only on `report_lib`,** never on raw tables — so trust rules can change in one file.
4. **Additive over invasive.** Prefer new functions/objects over editing hot pipeline code; when editing is required (e.g. `EXCLUDE_BRANDS` → table lookup), it's a replacement, not a parallel copy.
5. **Every change recorded here** with what/why/where + dependency, before or at deploy time.

---

## 6. Status & next action

**Decision:** Option **B** chosen — one `ref_excluded_brands` table, read by both `report_lib` and future SQL consumers.

**P0 artifacts produced (repo-ready, NOT yet applied):**
- `p0_ref_excluded_brands.sql` — CREATE + seed (idempotent). Apply via the GitHub Actions psql workflow. *(MCP role is DDL-denied — `permission denied for schema public` — consistent with the established "DDL goes through Actions/psql" rule.)*
- `p0_report_lib_changes.py` — three edits to `report_lib.py`: (1) `EXCLUDE_BRANDS` → table lookup via `refresh_exclusions()` + new `drop_excluded_stock()`, with hardcoded fallback; (2) call `refresh_exclusions(conn)` inside `connect()`; (3) add `stale_brands()` freshness guard, `pct_reliable()`, `cell_tier()`. No existing logic duplicated.

**To apply P0 (owner/dev step):**
1. Run `p0_ref_excluded_brands.sql` through the GitHub Actions psql workflow.
2. Apply the three edits in `p0_report_lib_changes.py` to `report_lib.py`; commit.
3. Confirm a report run: `refresh_exclusions` loads 2 `all` + 3 `stock` brands; `stale_brands('signal_l1_01_genuine_price_drop')` returns `{lc_waikiki: ~8}`.

**Next after P0 applied:** P1 — market rollup (cross-brand aggregation) built on top of `report_lib`, reading through `drop_excluded*` + `pct_reliable` + `stale_brands`. Then P2 — wire Report 4 (Discount) to live data via the read seam.

**Verified brand strings (seed):** `tree`, `dalydress` (all); `lc_waikiki`, `defacto`, `mobaco` (stock).

---

## Change Log
- **2026-08-08 · #1** — Read source of truth (`compute_products.py`, `products.py`, `signals.py`, `compute_signals.py`, `housekeeping.py`, `archive.py`, `report_lib.py`). Built Rule Inventory (§1). Killed the original `rl_*`-views P0 as duplicative; wrote revised minimal P0 (§3). Confirmed L2 history is not purged today; Phase-2 archival already designed in `archive.py` (§1). Logged open decision A/B (§4). **No DB changes made.**
- **2026-08-08 · #2** — Founder chose **Option B**. Confirmed exact brand strings from live DB. Found MCP role cannot run DDL (`permission denied for schema public`) → DDL routed to the GitHub Actions psql path. Produced P0 artifacts `p0_ref_excluded_brands.sql` and `p0_report_lib_changes.py` (§6). Verified `report_lib.confidence()` stamps signal maturity but does **not** suppress per-cell %, so `pct_reliable()`/`cell_tier()` added as the genuine gap.
- **2026-08-08 · #3** — Founder committed the fully-edited `report_lib.py` (all 3 edits, compiles clean). Attempted `ref_excluded_brands` via the **PlanetScale web console** → also `permission denied for schema public` (console uses a restricted role; only the admin/Actions DDL path can CREATE in `public`). **Decision: DEFER the table.** `refresh_exclusions()` catches the missing-table exception and uses the hardcoded fallback, which equals the intended exclusions exactly (`all`: tree, dalydress · `stock`: lc_waikiki, defacto, mobaco). So P0 exclusions + freshness guard + pct floor are **functionally live** via the committed code. **Deferred task (no urgency, no functional impact today):** create `ref_excluded_brands` once via the privileged DDL path (whoever created the existing tables), OR a future one-click Actions workflow. When it lands, `refresh_exclusions()` starts reading it automatically — no code change. Only matters once a SQL consumer (the chatbot) needs to share the list.

- **2026-08-08 · #4** — **P1 build-first (verified live).** Cross-brand market under-supply rollup works: top cell shirts·XL = 9 brands, 369 stockouts, 130 products, 5 flagged "increase production", `confirmed`. **Finding A:** `pct_size_specific_demand` degenerate (`while_other_sizes_available` == `stockout_count` everywhere → 100%); dropped, upstream fix needed in l1_08/l1_11 derivation. **Finding B:** `market_stockouts` is brand-weighted (mens_club ≈ ½ of shirts-XL); mitigation = always show `brands`; brand-balanced measure later. Rollup is category × size only; gender + colour = P3.
- **2026-08-08 · #5** — **P1 merged into `report_lib.py`.** `market_undersupply(conn)` folded in after `cell_tier` (the loose `p1_report_lib_rollup.py` snippet is now superseded — ignore it). Final `report_lib.py` = P0 (3 edits) + P1 (1 function), 453 lines, compiles clean. This single file replaces the P0 version currently committed. Build log to be added to the repo as `docs/KHABAR_BUILD_LOG.md`.

## Status: P0 DONE (functional). P1 DONE (proven live + merged into report_lib.py). Table deferred.
## Backlog (non-blocking): 1) create `ref_excluded_brands` via admin DDL · 2) fix `while_other_sizes_available` upstream · 3) brand-balanced demand measure.
## Next: P2 — wire Report 4 (Discount) to live data through the read seam.
