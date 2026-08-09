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

## Change Log- **2026-08-08 · #1** — Read source of truth (`compute_products.py`, `products.py`, `signals.py`, `compute_signals.py`, `housekeeping.py`, `archive.py`, `report_lib.py`). Built Rule Inventory (§1). Killed the original `rl_*`-views P0 as duplicative; wrote revised minimal P0 (§3). Confirmed L2 history is not purged today; Phase-2 archival already designed in `archive.py` (§1). Logged open decision A/B (§4). **No DB changes made.**
- **2026-08-08 · #2** — Founder chose **Option B**. Confirmed exact brand strings from live DB. Found MCP role cannot run DDL (`permission denied for schema public`) → DDL routed to the GitHub Actions psql path. Produced P0 artifacts `p0_ref_excluded_brands.sql` and `p0_report_lib_changes.py` (§6). Verified `report_lib.confidence()` stamps signal maturity but does **not** suppress per-cell %, so `pct_reliable()`/`cell_tier()` added as the genuine gap.
- **2026-08-08 · #3** — Founder committed the fully-edited `report_lib.py` (all 3 edits, compiles clean). Attempted `ref_excluded_brands` via the **PlanetScale web console** → also `permission denied for schema public` (console uses a restricted role; only the admin/Actions DDL path can CREATE in `public`). **Decision: DEFER the table.** `refresh_exclusions()` catches the missing-table exception and uses the hardcoded fallback, which equals the intended exclusions exactly (`all`: tree, dalydress · `stock`: lc_waikiki, defacto, mobaco). So P0 exclusions + freshness guard + pct floor are **functionally live** via the committed code. **Deferred task (no urgency, no functional impact today):** create `ref_excluded_brands` once via the privileged DDL path (whoever created the existing tables), OR a future one-click Actions workflow. When it lands, `refresh_exclusions()` starts reading it automatically — no code change. Only matters once a SQL consumer (the chatbot) needs to share the list.

- **2026-08-08 · #4** — **P1 build-first (verified live).** Cross-brand market under-supply rollup works: top cell shirts·XL = 9 brands, 369 stockouts, 130 products, 5 flagged "increase production", `confirmed`. **Finding A:** `pct_size_specific_demand` degenerate (`while_other_sizes_available` == `stockout_count` everywhere → 100%); dropped, upstream fix needed in l1_08/l1_11 derivation. **Finding B:** `market_stockouts` is brand-weighted (mens_club ≈ ½ of shirts-XL); mitigation = always show `brands`; brand-balanced measure later. Rollup is category × size only; gender + colour = P3.
- **2026-08-08 · #5** — **P1 merged into `report_lib.py`.** `market_undersupply(conn)` folded in after `cell_tier` (the loose `p1_report_lib_rollup.py` snippet is now superseded — ignore it). Final `report_lib.py` = P0 (3 edits) + P1 (1 function), 453 lines, compiles clean. This single file replaces the P0 version currently committed. Build log to be added to the repo as `docs/KHABAR_BUILD_LOG.md`.

## Backlog (non-blocking): 1) create `ref_excluded_brands` via admin DDL · 2) fix `while_other_sizes_available` upstream · 3) brand-balanced demand measure.
## Next: P2 — wire Report 4 (Discount) to live data through the read seam.
- **2026-08-08 · #6** — **P2 = Path B (build the real curve).** Read current `compute_signals.py` + `signals.py` (founder-uploaded, authoritative). Prototyped clear-rate-by-depth live on the hot window: sane and counterintuitive — trousers clear 48.8% at 0–10% depth vs ~4% past 30%; shirts/t-shirts peak at 20–30% then fall. Honest reading: deep-discounted items are the unsellable tail, not rescued by depth (association, not cause). Wrote new registry signal **`l2_clear_rate_by_depth`** into `signals.py` (mirrors `l2_size_demand_curve`; reads `snapshots` for honest depth vs `first_observed_price`, `stock_events` for witnessed clearance; buckets by max depth per category × gender; per-bucket floor `products >= 10`). Brand is aggregated away, so all 5 excluded brands (tree, dalydress, lc_waikiki, defacto, mobaco) are filtered INLINE — another instance to migrate when `ref_excluded_brands` lands (backlog #1). Self-provisions `signal_l2_clear_rate_by_depth` on first engine run. `signals.py` compiles clean (1920 lines).

- **2026-08-08 · #7** — **P2 Path A refinement (signal v2).** Verified the v1 `l2_clear_rate_by_depth` over the full 60-day lake and found: (a) my hot-window claim "deep discounts don't clear" was a short-window artefact — over 60 days the shape is category × gender specific (women's dresses fall with depth; men's shirts clear ~63% even at 40%+); (b) a time-exposure + episode-mismatch confound (`cleared` was all-time while depth was windowed). **Rewrote the signal** to episode-aligned + censored: bucket by max honest depth and the DATE reached; "cleared" = witnessed on-discount stockout within **21 days** of that date; **censoring** excludes products that reached depth <21 days before window-end (no full chance to sell). Output columns unchanged → reuses existing `signal_l2_clear_rate_by_depth` table, no DDL. Prototyped logic live (sane). `signals.py` compiles clean (1945 lines). Needs one engine re-run to repopulate with the v2 metric; reports read latest snapshot_date, so any lingering v1 day is ignored.

- **2026-08-08 · #8** — **P2 verified v2 + Report 4 rendered (Path A).** v2 signal landed clean (49 rows = 49 keys, no dupes; censoring cut 96→49). But full-window v2 came out starved: 34/49 cells at 0%, overall clear-rate 5.2%, max 21.7% → **the depth-curve is data-gated**: v1 biased high (exposure), v2 biased low (witnessed clearance history is recent-heavy + only ~60 days, so censoring + 21-day horizon can't both be satisfied yet). Signal is CORRECT and self-matures as history accrues. **Rendered `report_4_discount.html` on LIVE data** — solid content = discount depth + distress per category from l2_01/l2_12 (t-shirts/shirts urgent, 2.3k/1.95k products discounting; sweaters clear 64%, loungewear 2%); the clear-rate curve shipped as an honest **MATURING** panel (no fake numbers). Duplication side-question resolved: DATA tables replace cleanly per day (no dupes); `signal_runs` appends per execution BY DESIGN (audit trail — 2 records today from the rerun is correct, not corruption); minor: `product_runs` logged 0 today despite data updating (timezone/logging quirk to check later).

- **2026-08-08 · #9** — **Full report suite built as automated generators (commit as one batch).** Moved from hand-rendered HTML to weekly generators, all routing through `report_lib` (data/exclusions/confidence) + new `report_html.py` (shared presentation). Files: `report_html.py`, `report_order.py` (R1), `report_price.py` (R2), `report_marketing.py` (R3), `report_discount.py` (R4), and workflow `report_suite.yml` (Mon 12:00 UTC, after signals+products refresh; commits the HTML back). Verified each on live data; shipped solid parts, marked the rest maturing (no fake numbers):
  - **R1 Order** — category × size cross-brand under-supply via `market_undersupply()`; menswear tops lead (shirts/t-shirts XL/L/M, confirmed). Gender+colour = maturing.
  - **R2 Price** — per-category price BANDS (P25/median/P75 EGP). Found global brand-tiers INVERT (t-shirts "Upper-Mid" median 360 < "Mid" 499 — a brand's tier is global while its per-category prices vary), so used inversion-free bands instead. Colour/size levers = maturing; Phase-2 (client price overlay) nudge in footer.
  - **R3 Marketing** — market-wide velocity trend (5 wks, accelerating→steady) + first-mover discount timing (phantom brands dropped). Granular category×colour warming = maturing (needs a per-category velocity signal; today's is market-wide only).
  - **R4 Discount** — discount depth + distress per category; clear-rate curve = maturing (data-gated, per #8).
  - All compile clean; `report_suite.yml` valid YAML.

## Commit batch: report_html.py, report_order.py, report_price.py, report_marketing.py, report_discount.py → repo root; report_suite.yml → .github/workflows/; this log → docs/KHABAR_BUILD_LOG.md.
## Backlog (non-blocking): 1) create ref_excluded_brands via admin DDL · 2) fix while_other_sizes_available upstream · 3) brand-balanced demand measure · 4) per-category velocity signal (unlocks R3 granular) · 5) colour dimension in signals (unlocks R1/R2 colour) · 6) clear-rate curve matures with history (R4). · 7) fix 5 brands with no stock signal (andora, cizaro, eagle, just_sbr, premoda → lifts demand coverage 15→20) · 8) best-seller / bread-and-butter + low-hanging-fruit section in R1 (cross under-supply × best-seller records)
- **2026-08-08 · #10** — **First workflow run: env fix.** `report_order.py` failed at `import pandas` — `requirements.txt` was missing `pandas` AND `matplotlib` (both imported at module level by `report_lib`; matplotlib for the legacy markdown reports' PNG charts). Added `pandas>=2.0.0` and `matplotlib>=3.7.0` to `requirements.txt`. This also means the pre-existing markdown report generators never ran cleanly in CI under this requirements file. Re-run the suite workflow after committing the updated requirements.txt. Code unchanged — pure dependency fix.
- **2026-08-08 · #11** — **P3 started (colour + subcategory depth).** Checked new dimensions before building: `products.subcategory` exists (34 clean values, but only ~49% of products populated — must disclose), `product_variants.color`/`size` exist. Colour is NOT usable raw (~3,800 distinct free-text values incl. Arabic + SKU codes) → needs normalization. Built in `report_lib`: `canonical_color()` (keyword→~18 canonical colours, first-match-wins, Arabic + long-tail synonyms, unmatched→'other' shown not hidden) and `demand_grid(conn, group_cols, min_stockouts)` (cross-brand witnessed demand at any grain — category × colour/subcategory/gender/size — normalises colour, aggregates, floors, confidence; reads 60-day stockout_events; excludes the 5 stock brands in SQL). Verified colour prototype live: clean palette per category (shirts: white/black/blue/beige/navy/green/olive…) with real counts. Caveat: 'other' bucket is material (unmapped tail) — reports must show it. `report_lib.py` compiles clean (529 lines). NEXT: wire `demand_grid` into report_order.py (colour + subcategory breakdown sections) + verify rendered output; then consider R2 colour price-lever.

- **2026-08-08 · #12** — **P3 wired into Report 1.** `report_order.py` now renders, from `demand_grid()`: §2 "which styles" (category × subcategory — short-sleeve, oversized/graphic tees, wide-leg trousers, wide-baggy jeans etc., verified live) and §3 "which colours" (normalised colour demand within the hottest category). Replaced the old gender/colour maturing placeholder. Coverage footer discloses: 15 stock-visible brands, subcategory ~49% tagged, colour 'other' bucket. `report_lib.py` + `report_order.py` compile clean. P3 batch so far = these 2 files. NEXT (optional, same P3 batch): colour price-lever in Report 2 (does colour move price); Report 3 colour-warming stays maturing (needs per-category-colour time series).
- **2026-08-08 · #13** — **P3 Report 1 VERIFIED LIVE.** Founder deployed + ran the workflow; pulled `reports/what-to-order/latest/report.html` from repo — all 4 sections rendered from real data: category×size, subcategory (short-sleeve/oversized/graphic/wide-leg/chino/wide-baggy), colour (black/white/beige/navy/blue/green/olive/grey/brown/red/pink/other for shirts), coverage. Colour normalizer + demand_grid confirmed working end-to-end. **P3 for Report 1 = DONE.**

- **2026-08-08 · #14** — **P3 finished for R2 + R3.** Added to `report_lib`: `color_price()` (median product price by category × canonical colour — dedup per product×colour) and `demand_trend()` (weekly witnessed-stockout trend per cell → warming/cooling/steady). Verified live: colour IS a modest price lever (shirts brown/olive 799 vs blue 600 ≈33%; t-shirts brown 499 vs red 349 ≈43%); colour warming computable (4 clean weeks, all rising with the heating market — thin, directional, accumulates). Wired R2 §2 "Does colour move price?" (real lever + verdict) and R3 §3 "Warming & cooling by category × colour" (DIRECTIONAL badge, sharpens over time) — replacing both maturing placeholders. Per founder's call: build now so it accumulates rather than wait for data. All 6 report files compile clean.

## Status: P0–P2 DONE · Suite live weekly · P3 DONE (colour + subcategory across R1/R2/R3; R3 colour-warming intentionally young/directional, self-sharpening). P3 batch to commit: report_lib.py, report_order.py, report_price.py, report_marketing.py.
