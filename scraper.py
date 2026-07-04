# ═══════════════════════════════════════════════════════
# KHABAR — Scraper v14  (current: v14.29)
# Built on v13. Changes:
#  v14.1  DeFacto integration via PartialIndexScrollResult API
#         No proxy required — plain GET, follows NextDataUrl chain
#         Size enrichment pass (SIZE_CAP=10, same pattern as LCW)
#  v14.2  Fixed parse_defacto_sizes: real HTML uses data-size on <button>
#         with class "is-no-stock" for OOS — not data-value on <li>
#  v14.3  Snapshot pre-load changed from single .limit(20000) query to paginated
#         .range() loop — PostgREST ignores .limit() above 1000, partial index
#         blocks upsert ON CONFLICT, so insert is safe when pre-load is exhaustive
#  v14.4  DeFacto size source changed from HTML parsing to CombinProductListByProductLongCode
#         batch API — sizes populated inline per catalog page, no SIZE_CAP crawl needed
#  v14.5  Fixed backfill query: add products!inner(brand) join for brand filter
#  v14.6  LCW size parser switched from CSS-class HTML to embedded JSON.
#         Pages contain cartOperationViewModel + optimizedDetailModel with full
#         per-size stock + price data. SIZE_CAP raised to 65 (still <1GB/month
#         since pages are 60 KB gzipped, not 172 KB decoded). Dedupe by URL stem
#         eliminates redundant fetches. Backfill completes in ~86 days.
#  v14.7  SYSTEM AUDIT FIXES — detection & alert engine repair:
#         • load_last_prices() now paginates (.range loop). It was capped at the
#           PostgREST 1000-row default, so ~95% of the catalog never received a
#           price baseline → every observation logged as a directionless event and
#           no real drop was ever detected/alerted. This was the root cause of the
#           price_events flood and the dead alert pipeline.
#         • First-sighting no longer writes a direction=NULL event; it seeds the
#           in-memory baseline silently (the snapshot already stores the price).
#         • Removed the inverted ">5-day last_updated_at" alert gate that could
#           never be true for a daily scraper (it blocked every alert). Alerts now
#           fire on a genuine down-transition below first_observed_price.
#         • Added MIN_ALERT_DISCOUNT_PCT (10%) quality floor on alerts.
#         • Restored the rolling 30-day purge of price_events (was dropped in v14).
#  v14.8  Price-change detection moved from per-variant to PER-PRODUCT. Fixing the
#         1000-row cap in v14.7 exposed a latent flip-flop: variants of a
#         multi-price product were each compared against the product's median
#         snapshot with the baseline mutating mid-loop, manufacturing phantom
#         up/down events (e.g. 219 mens_club "down" events from 86 products in one
#         run). Detection now computes one representative price per product via
#         product_repr_price() — the SAME median build_snapshot_rows stores — and
#         emits at most one event per product per run. Alerts now fan out across a
#         product's in-stock sizes. Stockout detection stays per-variant.
#  v14.9  LCW cross-page colour-split fix. LCW paginates by OptionId, so one
#         model's colours can span multiple catalog pages; per-page detection saw
#         partial colour sets and emitted symmetric phantom down/up round-trips
#         (e.g. 1099->599 then 599->1099 minutes apart in one run), which also
#         created false flash-sale flags. The scraper now accumulates every colour
#         of a model across the whole crawl and runs snapshot + price detection
#         ONCE, on the complete set, yielding a stable median. Stockout detection
#         remains per-page/per-variant. Shopify and DeFacto were unaffected (their
#         variants never split across pages).
#  v14.10 Snapshot now tracks the latest price each day, not just the first. For
#         brands scraped multiple times daily (Shopify, DeFacto), a sale starting
#         after the day's first run left the snapshot frozen at the pre-sale price,
#         so every later run re-fired the same drop against the stale baseline
#         (738 products primed to double-count on one observed run). sync_snapshot_price()
#         updates the day's snapshot to the new price after each real change, so a
#         drop is recorded once and the warehouse holds the day's latest price.
#  v14.11 Added two Shopify brands: Carina (carina.eg, women's basics/sleepwear)
#         and 2S Egypt (www.2segypt.com, family homewear/pajamas). Both expose the
#         standard /products.json endpoint, so they route through the existing
#         scrape_shopify engine with no new parsing code; the shared CATEGORY_MAP
#         taxonomy normalizes their catalogs automatically. No proxy needed — they
#         join the SCRAPE_TARGET=shopify group that runs alongside DeFacto.
#  v14.12 Brand expansion + a resilience fix:
#         • Added Andora (andoraeg.com) and Cizaro (cizaro.NET — the .com is an
#           unrelated POS/ERP vendor) as Shopify brands.
#         • Added Mobaco (mobaco.com) on a NEW WooCommerce engine that reads the
#           public Store API (/wp-json/wc/store/v1/products). Product-level price
#           + sale detection; per-size stock inherited from the product (coarse),
#           same pattern as DeFacto. currency_minor_unit is read from the response
#           (never hardcoded) and prices are sanity-bounded so a bad parse cannot
#           poison a baseline. New SCRAPE_TARGET=mobaco allows an isolated
#           validation run before it joins the daily no-proxy group.
#         • load_last_prices() now falls back to the most recent snapshot day in a
#           14-day window instead of only today/yesterday. A single missed daily
#           run (e.g. GitHub skipping LCW's slot) previously left the next run with
#           no baseline, silently re-seeding the whole catalog and detecting zero
#           changes for that day. It now compares against the last day with data.
#  v14.13 Three fixes from the post-v14.12 review:
#         • LCW colours now stored in English. LCW's ColorImageUrl filenames are
#           Turkish (siyah/beyaz/lacivert/...), and that's what we'd been storing.
#           Added LCW_COLOR_TR_EN translation dict + normalize_lcw_color() applied
#           at both catalog write and size-backfill so colour-by-brand queries
#           don't need per-brand normalisation downstream.
#         • LCW size backfill now rescues NULL colours. When the catalog couldn't
#           extract a colour (no ColorImageUrl + no MainColorHexCode), the size
#           pass left children with size populated but color=NULL. It now reads
#           cartOperationViewModel.Color from the product page JSON it already
#           fetches, normalises it, and writes it back to the parent row too.
#         • check_domain() pre-flight is now skipped for the woocommerce engine.
#           Mobaco's WordPress homepage rejected the bare "Mozilla/5.0" UA used
#           by the pre-flight, blocking the entire brand from running. Same
#           bypass already existed for lcw_proxy and defacto.
#  v14.14 Three efficiency + resilience fixes from the post-v14.13 review:
#         • FOP-write gating across ALL FOUR engines (Shopify, LCW, DeFacto,
#           WooCommerce). Previously we issued the idempotent
#               UPDATE products SET first_observed_price=$1
#               WHERE id=$2 AND first_observed_price IS NULL
#           on every product, every run. The WHERE clause made it safely no-op
#           on the server, but the network round-trip happened anyway —
#           ~31,000 wasted calls per run. Now each engine pre-loads the set of
#           product_ids that already have FOP set (load_products_with_fop)
#           and only issues the UPDATE for genuinely new products. FOP itself
#           is unchanged: still the honest baseline that never overwrites.
#         • DeFacto size-write gating. The inline size pass was fetching +
#           rewriting sizes for the FULL catalog every run (~23,500 redundant
#           rewrites/run), even though almost every variant already had its
#           size populated from a previous run. It now skips variants whose
#           prev_stock_state row already has a non-null size; the end-of-run
#           backfill block still catches any stragglers, so coverage doesn't
#           regress — only the redundant work goes away.
#         • Mobaco (WooCommerce) per-product resilience. The four per-product
#           loops inside scrape_woocommerce are now individually try/excepted
#           so one malformed item is skipped (with a real traceback for
#           diagnosis) instead of crashing the whole brand. Same fault-isolation
#           pattern the orchestrator already applies at the brand level, now
#           applied at the item level inside the most fragile engine.
#  v14.15 2S Egypt Arabic → English colour normalisation. 2S Egypt publishes
#         colour names in Arabic, so its variant.color values were being stored
#         in Arabic while every other brand stores English — breaking the
#         cross-brand colour queries the playbook depends on. Added
#         ARABIC_COLOR_AR_EN + normalize_arabic_color(), exactly parallel to the
#         LCW Turkish map, and a one-line ARABIC_COLOR_BRANDS opt-in set so the
#         Shopify engine only routes Arabic-catalog brands through it. Unknown
#         values pass through untouched, so an unmapped colour surfaces in the
#         data (visible, addable to the dict) rather than being silently dropped.
#  v14.16 Mobaco image-field crash fixed. The WooCommerce Store API usually
#         returns `images` as a list, but some Mobaco products return it as a
#         dict; the old `(p.get("images") or [{}])[0]` indexed that dict with
#         [0] and raised KeyError: 0, which — thanks to the v14.14 per-product
#         guard — skipped the whole product instead of crashing the brand. Two
#         products (1207002, 1204625) were being dropped each run over nothing
#         but a thumbnail. Replaced with _woo_first_image(), which handles list,
#         dict, string, empty, and missing shapes without ever raising. The two
#         lost products are recovered on the next run.
#  v14.17 EGRESS REDUCTION — eliminated the redundant per-brand FOP read.
#         load_products_with_fop() issued a SECOND full-catalog read of the
#         products table for every brand on every run, purely to learn which
#         products already had first_observed_price. That information is
#         already implied by the variant preload scrape_brand() runs at the top
#         of every brand: any product that already has a variant row has been
#         scraped before, so its first_observed_price was set on first sighting.
#         We now add product_id to that existing preload SELECT and derive
#         fop_done_ids from it IN MEMORY — removing one catalog-sized read per
#         brand per run with zero behaviour change. The DB keeps its
#         `WHERE first_observed_price IS NULL` guard, so the baseline still can
#         never be overwritten. load_products_with_fop() is removed; the four
#         engines now receive fop_done_ids as a parameter instead of loading it.
#         Paired with cutting the Shopify schedule from 4x to 3x daily, this is
#         the change that pulls monthly egress back under the 5 GB free-tier cap.
#  v14.18 Two fixes — pagination safety + the Sisyphean size-overwrite loop.
#
#         PAGINATION: added an empty-page retry guard to Shopify's ?page=N loop.
#         Shopify's storefront /products.json endpoint occasionally returns a
#         transient empty page mid-catalog, which made the old `if not products:
#         break` conclude the catalog had ended and silently under-scrape (e.g.
#         dalydress returning 1,272 of ~5,400 products on one observed run). The
#         fix retries the same page once after a 3-second delay before trusting
#         the empty result. Pagination stays on ?page=N (since_id does NOT work
#         on the public storefront endpoint — it's an Admin API parameter that
#         the storefront silently ignores, causing an infinite loop).
#
#         SIZE-OVERWRITE FIX: DeFacto and LCW variant upserts no longer include
#         `"size": None` in the payload. These engines get size data from a
#         SEPARATE API (CombinProductList for DeFacto, product-page JSON for
#         LCW), not from the catalog listing. Including size=None made the
#         upsert's ON CONFLICT SET clause overwrite sizes that a previous run's
#         inline/backfill pass had already populated — forcing the backfill to
#         redo ~5,000 variants every run (~1h40m wasted). Omitting the key means
#         the SET clause skips the column, preserving existing values. New
#         variants get NULL by default (correct — inline/backfill will populate
#         them). This also unblocks LCW's size backfill, which was fighting the
#         same losing battle at SIZE_CAP=65/day and could never make progress.
#  v14.19 Color-from-title extraction for Tree and Cizaro (see COLOR_FROM_TITLE_BRANDS
#         block below) — both publish each colourway as a separate product rather
#         than a variant option, so colour only exists in the product title.
#         Also: WooCommerce (Mobaco) rewrite to fetch real per-variation stock/price
#         from /products/{id}/variations instead of trusting the parent product's
#         blanket is_in_stock flag.
#  v14.20 Widened retry backoff (3→5 attempts, longer max delay, jitter, Retry-After
#         honoring) after a real run showed Carina/2S Egypt/Andora/Cizaro/Mobaco all
#         failing with 503/429 in the same run — confirmed shared-IP rate-limiting,
#         not independent outages. Also: end-of-run retry pass for brands that scanned
#         0 products, and inter-brand pacing (8-20s) to spread bursty traffic out.
#  v14.21 Color extraction fallback pass for Tree (extract_color_tree) and longest-
#         match scan for Cizaro (extract_color_cizaro) — see COLOR_FROM_TITLE_BRANDS
#         block below for full detail and tested accuracy figures.
#  v14.22 TWO FIXES — see full rationale inline at each change site:
#
#         FIX 1 — LCW color-level is_in_stock corruption (size backfill was
#         overwriting the wrong field). Diagnosed live via Supabase: 562 LCW
#         stockout_events, 100% with size=NULL, several SKUs showing 3-5
#         consecutive "restock" events with ZERO "stockout" events between them
#         (e.g. lcw_5048758: restock@06-08, restock@06-11, restock@06-11 again).
#         Root cause confirmed by inspecting the affected rows directly: the
#         color-level parent row (lcw_{opt_id}) was being written TWICE by two
#         different passes that disagree about what is_in_stock means for that
#         row —
#           (a) the catalog pass sets is_in_stock = (AvailableStock > 0), the
#               correct COLOR-WIDE aggregate ("is any size of this color
#               purchasable"), but
#           (b) the size-backfill pass's `i == 0` branch was ALSO writing
#               is_in_stock to that same parent row, using ONE size's stock
#               state (whichever size happened to be first in that day's API
#               response — order not guaranteed stable run to run).
#         When the first size in the array was OOS while other sizes of the
#         same color were in stock, pass (b) flipped the parent to False. The
#         NEXT catalog run then saw AvailableStock > 0 (true — other sizes are
#         still available) against a stale False baseline and logged a
#         "restock" that never really happened. This could repeat indefinitely
#         since size order isn't stable, manufacturing the exact multi-restock,
#         zero-stockout pattern found in the data. Confirmed NOT a bug in
#         detect_and_write_stockout() itself — it was faithfully recording
#         whatever the row said; the row was just being corrupted by a second
#         writer with a different idea of what the field meant.
#         FIX: the size-backfill's i==0 branch no longer writes is_in_stock to
#         the parent row at all. is_in_stock on the COLOR-level parent (the bare
#         lcw_{opt_id} row) is now written EXCLUSIVELY by the catalog pass's
#         AvailableStock read — the one source that actually represents "is
#         this color, in any size, purchasable." The size-level CHILD rows
#         (lcw_{opt_id}_{SIZE}) are unaffected by this fix and keep getting
#         their own correct per-size is_in_stock from the sizes[] array, as
#         before — those rows were never part of the bug (each size has its own
#         external_sku, so they were never the same row being double-written).
#         No backfill/repair needed: this only changes future writes. The
#         parent row's is_in_stock will self-correct on its next catalog pass
#         the same way any other stock-state field would.
#
#         FIX 2 — Carina is a women-only brand; the generic normalize_gender()
#         keyword scan was returning "unisex" for 95% of its catalog (1,287 of
#         1,358 products) because Carina's titles/tags don't reliably say
#         "women" even though the brand sells exclusively women's basics and
#         sleepwear. Added a brand-level override: FEMALE_ONLY_BRANDS = {"carina"}.
#         scrape_shopify() now checks this set BEFORE falling back to
#         normalize_gender() for any brand in it — gender is fixed to "women"
#         unconditionally, no keyword scan involved, no chance of a unisex
#         fallback. Every other brand's gender detection is completely
#         unaffected; this is scoped to exactly one brand via the same opt-in
#         set pattern already used for COLOR_FROM_TITLE_BRANDS and
#         ARABIC_COLOR_BRANDS.
#  v14.23 Mobaco and DeFacto both showed 100% in-stock across their entire
#         variant sets (Mobaco: 7,879/7,879; DeFacto: 25,976/25,976) with zero
#         stockout_events ever recorded for either brand. Diagnosed live via
#         Supabase — these are TWO DIFFERENT root causes that happen to look
#         identical on the surface:
#
#         DEFACTO — confirmed NOT a scraper bug, a permanent data ceiling.
#         DeFacto's CombinProductListByProductLongCode API (the only source of
#         per-size data DeFacto exposes) returns StockQuantity as null for
#         every size, every product — this was already known and documented
#         at that function's definition. Per-variant is_in_stock is correctly
#         inherited from the product-level Stock field because there is no
#         finer signal to read; this is the same category of structural blind
#         spot as DeFacto's already-documented catalog-pagination gap. No code
#         change made here — writing speculative logic against a field the
#         API itself always returns empty would not produce real signal.
#
#         MOBACO — a real, fixable scraper bug, now fixed. v14.19 introduced
#         fetch_mobaco_variations() to pull real per-variation price/stock
#         from /products/{id}/variations, matched against the parent
#         product's variations[] list by reconstructing a (attr_name,
#         attr_value) tuple from both sides and comparing them as text.
#         Confirmed live: across all 416 actively-tracked variable products,
#         EVERY variant of EVERY product shared one identical price with no
#         exceptions — the live lookup was matching 0% of the time, silently
#         falling back to the parent's blanket is_in_stock/price every single
#         run, every variant, the exact "blanket flag" problem v14.19 was
#         built to eliminate. Root cause: the parent product's variations[]
#         summary and the dedicated /variations endpoint are not guaranteed to
#         describe an attribute the same way (one can report a human label,
#         the other a taxonomy slug), so a text-based match across the two is
#         not reliable. FIX: match on the variation's own numeric `id` instead
#         — the one field both API responses are guaranteed to agree on,
#         since they describe the exact same WordPress object. Also found
#         (cosmetic, not actively harmful): 2,690 leftover variant rows across
#         517 products from BEFORE the v14.19 rewrite, frozen at their last
#         write the moment the new engine deployed (confirmed via
#         last_updated_at clustering at the exact cutover timestamp) — these
#         are dead rows under a different SKU-naming scheme, not actively
#         being corrupted; no cleanup performed here since cleanup risks
#         deleting history before Mohammed has had a chance to look. Added a
#         one-run diagnostic print (first variable product only) confirming
#         the id-based match rate, so the next real run leaves visible proof
#         this landed — or a clear signal if Mobaco's actual API shape holds
#         a further surprise.
#  v14.24 NEW FEATURE — sub-category attribute extraction (sleeve length for
#         tops, fit/cut for bottoms). Requested by Mohammed: can finer detail
#         (long/short/no sleeves; wide-leg/skinny/regular; chino/jeans/
#         sweatpants) be scraped reliably across brands? Investigated by
#         sampling 10 random products per brand (all 14) plus a full-table
#         regex pass against every actual top-like/bottom-like product
#         (29,733 rows) before writing any extraction code. Findings:
#
#           - Neither attribute lives in one consistent field across brands.
#             Sleeve length is the stronger signal in category_raw for
#             town_team, ravin, mobaco; in the title for everyone else who
#             states it at all (defacto 35%, ravin 43%, lc_waikiki 11%,
#             town_team 17% — all measured post-implementation against the
#             full catalog). Fit/cut is dominantly a TITLE signal everywhere
#             EXCEPT town_team (ONLY in category_raw, e.g. "Men Jeans" vs
#             "Men Pants" — 0% in title) and cizaro (strong in BOTH, but
#             category_raw needed aggressive normalisation first — see below).
#           - 2s_egypt and dott_jeans/mens_club have low coverage for one or
#             both attributes; for 2s_egypt this is partly a genuine source
#             gap (loungewear/basics catalog) and partly something worse —
#             see the guard below.
#
#         IMPLEMENTATION: new module-level TOP_LIKE_CATEGORIES /
#         BOTTOM_LIKE_CATEGORIES gate which category_normalized values each
#         attribute even applies to. Two extractors — extract_sleeve_length()
#         and extract_fit_cut() — each take a brand-specific field-priority
#         order (SLEEVE_LENGTH_CATEGORY_FIRST_BRANDS / FIT_CUT_CATEGORY_FIRST_
#         BRANDS opt-in sets, same pattern as COLOR_FROM_TITLE_BRANDS),
#         checking one field first and falling back to the other, never
#         guessing — both return None when nothing matches, exactly like the
#         existing colour extractors. Cizaro's category_raw needed an
#         aggressive normalisation pass (_normalize_for_fit_match: lowercase,
#         collapse all punctuation/spacing to single spaces) before matching,
#         since the same fit was found written 5+ different ways ("Wide Leg",
#         "wide-leg", "Wide-Leg", "Wideleg", "Wide- leg") — a direct substring
#         match on the raw field would have missed most of these. Results are
#         combined by build_attributes_extracted() — the single entry point
#         every engine calls — into the new products.attributes_extracted
#         JSONB column (migration: add_attributes_extracted_to_products, GIN
#         indexed), e.g. {"sleeve_length": "long sleeve"} or {"fit": "wide
#         leg"}, never both keys on one product since a category is either
#         top-like or bottom-like, never both.
#
#         GUARD — 2s_egypt pajama contamination. Confirmed live: 2,316 of
#         2s_egypt's 3,091 products classified into a bottoms category
#         (jeans/trousers/shorts/leggings/joggers/sweatpants) are actually
#         PAJAMAS — the pre-existing normalize_category() keyword scan
#         catches "jeans" or "sweatpant" used as a FABRIC/STYLE descriptor
#         inside a pajama title (e.g. "بيجامه رجالي جينز" = "men's denim-look
#         pyjama") and misfiles it as a real denim/sweatpants garment. Running
#         fit/cut extraction on these would manufacture a false sub-category
#         on top of an already-wrong base category. Added
#         _is_2s_egypt_pajama_contaminated() — a narrow guard checked only for
#         this one brand, scanning category_raw/name for "pajama"/"بيجام" and
#         returning None (no extraction attempted) when found. This does NOT
#         fix normalize_category() itself, which is the actual root cause and
#         a pre-existing issue affecting category_normalized accuracy more
#         broadly — that's flagged to Mohammed as a separate item, deliberately
#         not bundled into this feature. Effect of the guard: 2s_egypt's false
#         fit hits dropped from 5 to 3 (the 3 remaining are genuine, verified
#         non-pajama trousers with real fit words in the title).
#
#         VALIDATION: tested against all 29,733 real top-like/bottom-like
#         products in the live database (not synthetic examples) before this
#         was considered done. Spot-checked dozens of real hits per brand by
#         hand across every brand — zero observed false positives after the
#         2s_egypt guard. Brands with near-zero coverage for one or both
#         attributes (2s_egypt, dott_jeans, mens_club on sleeve length) were
#         individually confirmed to be genuine source gaps (the catalog text
#         itself doesn't state the attribute), not parsing misses.
#  v14.25 EXTENDED the v14.24 sub-category pattern to five more category
#         groups, after Mohammed asked whether the same concept generalises
#         beyond tops/bottoms: dress length + dress silhouette (dresses),
#         jacket type (jackets/coats), bra/lounge-top style + brief/bottom
#         style (underwear, mutually exclusive — gated by which garment word
#         the title actually contains, never guessed across that boundary),
#         swimwear silhouette (swimwear), and bag type (bags). Same
#         methodology as v14.24: real samples pulled and regex-checked
#         against the live database BEFORE writing extraction code, every
#         vocabulary validated against actual titles, every extractor
#         returns None rather than guessing. Full detail and exact coverage
#         numbers per group are in the block comment directly above the
#         extractor functions (search "ROUND 2"). Also checked and
#         deliberately EXCLUDED: footwear (sneakers/sandals/boots/loafers/
#         heels/slippers) and belts/hats/jewelry — sampled the same way,
#         found mostly Arabic titles with too little recurring English
#         vocabulary to justify a list right now.
#
#         FULL-TABLE COVERAGE (measured against the live 47,972-row products
#         table, 22 Jun 2026, this is the honest ceiling of what title/
#         category TEXT PARSING can do across 14 differently-run catalogs):
#           - 10,698 products (22.3%) get at least one sub-category attribute.
#           - 37,274 products (77.7%) stay null — split roughly into:
#               ~9,063 (19%) are in categories with no extractor at all
#               (uncategorized, loungewear, socks, belts, jewelry, skirts,
#               hats, scarves, jumpsuits, slippers, sportswear, footwear,
#               kaftans, sunglasses) — checked and found too thin to build.
#               ~28,200 (59%) ARE in a category we extract for, but the
#               brand's own title/category text simply doesn't state the
#               attribute for that specific product — an honest gap in the
#               SOURCE, not a bug in the extractor.
#           - Coverage is heavily brand-dependent: cizaro 49%, ravin 53%,
#             esla 49%, defacto 48%, dott_jeans 47% vs. 2s_egypt 0.1% (almost
#             entirely loungewear + Arabic titles, structurally out of reach
#             for this method) and andora/carina/dalydress in the 15-16% band.
#
#         ── FUTURE DIRECTION: NLP / LLM / COMPUTER VISION ──────────────────
#         Getting durably past this ~22% ceiling with MORE regex vocabulary
#         is not the right lever — every group already checked (footwear,
#         accessories) came back thin not because the word list was
#         incomplete, but because the SOURCE TEXT genuinely doesn't contain
#         the attribute in any language-pattern-matchable form (Arabic-only
#         titles with no transliterated English term; bare SKU codes; one-
#         or-two-word titles like "Dress" or "Baggage" with zero descriptive
#         content). Closing that gap needs a fundamentally different
#         technique, not a bigger dictionary. Two independent directions,
#         not mutually exclusive, both deliberately NOT implemented yet:
#
#         1. LLM-BASED TEXT INFERENCE (the more immediately useful one).
#            Send {brand, category_normalized, category_raw, name} for any
#            product where build_attributes_extracted() currently returns
#            {} to a small/cheap model (e.g. a Haiku-class model) with a
#            constrained prompt asking ONLY for the same canonical labels
#            this file already defines (the FIT_CUT_CANONICAL / DRESS_LENGTH_
#            CANONICAL / etc. dicts above are the right place to source the
#            allowed-value list from, so the LLM's output space matches what
#            downstream queries already expect) — and explicitly allow
#            "unknown" as a valid answer so the model doesn't hallucinate an
#            attribute that genuinely isn't stated, mirroring the None-not-a-
#            guess principle this whole file already follows. Arabic titles
#            are the strongest case for this: an LLM can read "بنطلون حريمي
#            رجل واسعه" and correctly infer "wide leg" the way a fluent human
#            would, where a fixed regex dictionary structurally cannot
#            without transliterating every possible Arabic phrasing by hand.
#            Should run as a SEPARATE batch/offline pass against the
#            DATABASE (read attributes_extracted = '{}', call the model,
#            write back), not inline in the scraper's per-run hot path —
#            keeps the scraper's existing reliability/speed/cost profile
#            completely untouched, and makes it trivial to re-run only the
#            still-null rows after improving the prompt, without re-scraping
#            anything. Needs: a budget cap or row-count cap per run (this
#            file's existing SIZE_CAP-style pattern is the right template),
#            and a confidence/"unknown" path so a bad LLM call degrades to
#            None, never to a wrong stored value, exactly like every
#            extractor in this file already does on a regex miss.
#
#         2. COMPUTER VISION ON image_url (the harder, higher-ceiling one).
#            Every product already has an image_url from the catalog API —
#            completely unused for inference today. A vision model (or a
#            cheaper dedicated classifier, if volume ever justifies training
#            one) could in principle determine sleeve length, neckline,
#            length, and silhouette DIRECTLY from the product photo,
#            independent of whether the brand wrote anything descriptive in
#            the title at all — which is exactly the population text-based
#            extraction can never reach (the ~28,200 in-scope-but-unstated
#            rows above). This is meaningfully more expensive per item than
#            text inference (image tokens cost more than a short text prompt,
#            and many products have 1 image but some brands' galleries have
#            several — would need a rule for which image(s) to send) and
#            current image_url values aren't guaranteed stable/non-expiring
#            for every brand, so this needs its own small feasibility check
#            (confirm image URLs are fetchable months after being scraped,
#            confirm cost per call before committing to it at full catalog
#            scale) before it's worth building. Likely the second phase, after
#            text-based LLM inference above has been tried and its own ceiling
#            measured — no need to reach for the more expensive tool first
#            when the cheaper one hasn't been tested yet.
#
#         Neither direction is implemented in this file. This comment exists
#         so the next iteration starts from "here's what was already tried
#         and why," not from zero.
#  v14.29 THREE NEW DATA STREAMS — low-cost additions that deepen the
#         intelligence products without significant egress or storage cost:
#
#         1. BRAND LAUNCH DATE (source_published_at). Shopify's /products.json
#            includes `published_at` — the real date a product went live on
#            the brand's storefront, often months or years before our scraper
#            first saw it. WooCommerce's Store API has `date_created`. LCW
#            and DeFacto's catalog APIs don't expose a launch date, so those
#            stay NULL (honest). This is captured on the existing upsert —
#            zero extra requests. The column reaches backwards in time:
#            even though our price observations start June 2026, a product
#            published in 2024 gives real lifecycle age for launch-to-
#            markdown velocity and product-age cohort analysis.
#
#         2. BEST-SELLER RANK (top 150 per brand per day). Shopify's public
#            /collections/all/products.json?sort_by=best-selling returns
#            products ranked by the brand's own sales data — a DIRECT
#            demand signal that corroborates stockout-inferred demand with
#            an independent measurement. Capped at 150 per brand to keep
#            storage bounded (~10-20 MB steady state). One request per
#            brand per day (16 Shopify brands × 1 request = 16 requests
#            total). Stored in `bestseller_rank` table. Not available for
#            LCW/DeFacto/WooCommerce (no public sort-by-bestselling API).
#            Runs as a post-run pass in __main__.
#
#         3. DAILY FX RATE (USD→EGP). A single number per day from
#            frankfurter.app (free, no API key). Stored in `fx_rate`
#            table. NOT a per-variant USD price — it's a contextual number
#            joined by date to price_events, used to distinguish FX-driven
#            repricing from genuine promotional markdowns. Critical for
#            defending the honest-discount product against "the currency
#            moved" objections. Runs once at the start of each run.
#
#         DB migration: add_published_at_bestseller_rank_fx_rate
#         (adds source_published_at column + two new tables).
# ═══════════════════════════════════════════════════════

import json
import os
import random
import re
import sys
import time
import traceback
from curl_cffi import requests
from supabase import create_client
from datetime import datetime, timezone, timedelta, date

sys.stdout.reconfigure(line_buffering=True)

SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_KEY"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Minimum true discount (vs first_observed_price) before a user is alerted.
# Keeps alerts genuine and naturally caps alert volume.
MIN_ALERT_DISCOUNT_PCT = 10

WEBSHARE_USER  = os.environ.get("WEBSHARE_PROXY_USERNAME", "")
WEBSHARE_PASS  = os.environ.get("WEBSHARE_PROXY_PASSWORD", "")
WEBSHARE_PROXY = {
    "http":  f"http://{WEBSHARE_USER}:{WEBSHARE_PASS}@p.webshare.io:80",
    "https": f"http://{WEBSHARE_USER}:{WEBSHARE_PASS}@p.webshare.io:80",
} if WEBSHARE_USER and WEBSHARE_PASS else None

BRANDS = [
    {"name": "lc_waikiki", "domain": "www.lcwaikiki.eg",       "engine": "lcw_proxy"},
    {"name": "dalydress",  "domain": "dalydress.com",            "engine": "shopify"},
    {"name": "just_sbr",   "domain": "www.justsbr.com",          "engine": "shopify"},
    {"name": "activ",      "domain": "activ.eg",                 "engine": "shopify"},
    {"name": "mlameh",     "domain": "mlameh.com",               "engine": "shopify"},
    {"name": "khotwh",     "domain": "khotwh.com",               "engine": "shopify"},
    {"name": "tomato",     "domain": "www.tomatostores.com",     "engine": "shopify"},
    {"name": "esla",       "domain": "esla-store.com",           "engine": "shopify"},
    {"name": "town_team",  "domain": "www.townteam.com",         "engine": "shopify"},
    {"name": "ravin",      "domain": "shop.iravin.com",          "engine": "shopify"},
    {"name": "mens_club",  "domain": "mensclubcollection.com",   "engine": "shopify"},
    {"name": "tree",       "domain": "tree-stores.com",          "engine": "shopify"},
    {"name": "dott_jeans", "domain": "dottjeans.com",            "engine": "shopify"},
    {"name": "carina",     "domain": "carina.eg",                "engine": "shopify"},
    {"name": "2s_egypt",   "domain": "www.2segypt.com",          "engine": "shopify"},
    {"name": "andora",     "domain": "www.andoraeg.com",         "engine": "shopify"},
    {"name": "cizaro",     "domain": "cizaro.net",               "engine": "shopify"},
    {"name": "mobaco",     "domain": "mobaco.com",               "engine": "woocommerce"},
    {"name": "defacto",    "domain": "www.defacto.com.eg",       "engine": "defacto"},
]

BRAND_DISPLAY = {
    "town_team":  "Town Team",
    "ravin":      "Ravin",
    "mlameh":     "Mlameh",
    "just_sbr":   "Just SBR",
    "activ":      "Activ",
    "khotwh":     "Khotwh",
    "tomato":     "Tomato Stores",
    "mens_club":  "Men's Club",
    "tree":       "Tree",
    "dott_jeans": "Dott Jeans",
    "carina":     "Carina",
    "2s_egypt":   "2S Egypt",
    "andora":     "Andora",
    "cizaro":     "Cizaro",
    "dalydress":  "Dalydress",
    "esla":       "Esla",
    "mobaco":     "Mobaco",
    "lc_waikiki": "LC Waikiki",
    "defacto":    "DeFacto",
}

# v14.22 FIX 2: brands whose ENTIRE catalog is a single gender regardless of
# what normalize_gender()'s keyword scan finds in tags/product_type/title.
# Carina is a women-only basics/sleepwear brand; its product titles don't
# reliably contain a gender keyword, so the generic scan was defaulting 95%
# of its catalog to "unisex" — masking the true gender on a brand where it's
# never actually ambiguous. Membership here is the ONLY thing that activates
# this override, so every other brand's gender detection (including the
# normal "unisex" fallback for genuinely mixed-gender catalogs) is completely
# unaffected. Same opt-in-set pattern as COLOR_FROM_TITLE_BRANDS and
# ARABIC_COLOR_BRANDS below.
FEMALE_ONLY_BRANDS = {"carina", "just_sbr", "mlameh"}

# ── Category Taxonomy ─────────────────────────────────────────────────────────
CATEGORY_MAP = {
    "t-shirts":    ["t-shirt", " tee ", " tee,", "تيشيرت", "jersey tee", "jersey t"],
    "shirts":      ["shirt", "blouse", "tunic", "تونيك", "قميص", "بلوزة"],
    "polos":       ["polo"],
    "sweatshirts": ["sweatshirt", "سويت شيرت"],
    "hoodies":     ["hoodie", "hoody", "هودي"],
    "cardigans":   ["cardigan", "كارديجان"],
    "sweaters":    ["sweater", "pullover", "knitwear", "knit", "بلوفر"],
    "bodysuits":   ["bodysuit", "body suit", "بودي"],
    "tank-tops":   ["tank", "sleeveless top", "cami", "spaghetti"],
    "jeans":       ["jean", "denim trouser", "دينيم", "جينز"],
    "trousers":    ["trouser", "pant", "chino", "بنطلون", "slacks"],
    "shorts":      ["short", "شورت"],
    "skirts":      ["skirt", "تنورة", "jupe"],
    "leggings":    ["legging", "تايتس", "tight"],
    "joggers":     ["jogger", "sweatpant", "tracksuit bottom", "jogging"],
    "sweatpants":  ["sweat pant"],
    "jackets":     ["jacket", "puffer", "parka", "windbreaker", "جاكيت"],
    "coats":       ["coat", "overcoat", "معطف"],
    "blazers":     ["blazer", "بليزر"],
    "vests":       ["vest", "gilet", "صدرية"],
    "dresses":     ["dress", "فستان", "maxi dress", "midi dress", "mini dress"],
    "jumpsuits":   ["jumpsuit", "playsuit", "overall", "romper", "جمبسوت"],
    "kaftans":     ["kaftan", "قفطان", "abaya", "عباية", "jalabiya", "جلابية"],
    "sneakers":    ["sneaker", "trainer", "athletic shoe", "سنيكر", "كوتشي"],
    "sandals":     ["sandal", "flip flop", "flip-flop", "صندل"],
    "boots":       ["boot", "بوت"],
    "loafers":     ["loafer", "moccasin", "slip-on", "flat shoe"],
    "heels":       ["heel", "pump", "wedge", "stiletto"],
    "slippers":    ["slipper", "house shoe", "شبشب"],
    "bags":        ["bag", "handbag", "backpack", "tote", "clutch", "شنطة", "حقيبة"],
    "belts":       ["belt", "حزام"],
    "scarves":     ["scarf", "شال", "stole"],
    "hats":        ["hat", "cap", "beanie", "قبعة", "طاقية"],
    "jewelry":     ["jewelry", "jewellery", "necklace", "bracelet", "ring", "earring", "مجوهرات"],
    "watches":     ["watch", "ساعة"],
    "sunglasses":  ["sunglass", "eyewear", "نظارة"],
    "socks":       ["sock", "جوارب", "stocking"],
    "underwear":   ["underwear", "bra", "brief", "boxer", "lingerie", "ملابس داخلية"],
    "swimwear":    ["swimwear", "swimsuit", "bikini", "swim trunk", "مايوه"],
    "loungewear":  ["pyjama", "pajama", "nightwear", "sleepwear", "homewear", "بيجامة"],
    "sportswear":  ["sport", "gym", "athletic", "workout", "training", "active"],
}

CATEGORY_GROUPS = {
    "tops":        ["t-shirts", "shirts", "polos", "sweatshirts", "hoodies", "bodysuits", "tank-tops", "cardigans", "sweaters"],
    "bottoms":     ["jeans", "trousers", "shorts", "skirts", "leggings", "joggers", "sweatpants"],
    "outerwear":   ["jackets", "coats", "blazers", "vests"],
    "dresses":     ["dresses", "jumpsuits", "kaftans"],
    "footwear":    ["sneakers", "sandals", "boots", "loafers", "heels", "slippers"],
    "accessories": ["bags", "belts", "scarves", "hats", "jewelry", "watches", "sunglasses", "socks", "underwear"],
    "swimwear":    ["swimwear"],
    "loungewear":  ["loungewear"],
    "sportswear":  ["sportswear"],
}

# ── DeFacto category config ───────────────────────────────────────────────────
# fx_sb1=2182 is the Egypt portal identifier (constant).
# fx_sb2 identifies the gender category:
#   2427 = Men's  |  2426 = Women's
# Confirmed from browser Network tab on 9 Jun 2026.
DEFACTO_CATEGORIES = [
    {
        "name":   "Men",
        "gender": "men",
        "fx_sb1": "2182",
        "fx_sb2": "2427",
    },
    {
        "name":   "Women",
        "gender": "women",
        "fx_sb1": "2182",
        "fx_sb2": "2426",
    },
]

# ── Arabic colour normalisation (2S Egypt) ────────────────────────────────────
# 2S Egypt's Shopify catalog publishes colour names in Arabic. Every other brand
# stores English, and the playbook's cross-brand colour queries ("what % of black
# bottoms got discounted across brands") only work if colours are normalised to a
# single language. Same rationale as the LCW Turkish→English map: a static dict is
# cheaper and more reliable than a translation API (no rate limits, no cost, no
# surprises), and the palette is finite. Alternate Arabic spellings (e.g. the two
# common forms of alef, أ vs ا) are included so real catalog values match.
ARABIC_COLOR_AR_EN = {
    "أسود": "black", "اسود": "black",
    "أبيض": "white", "ابيض": "white", "اوف وايت": "off-white", "أوف وايت": "off-white",
    "رمادي": "grey", "رصاصي": "grey", "جراي": "grey",
    "أحمر": "red", "احمر": "red",
    "أزرق": "blue", "ازرق": "blue",
    "لبني": "light blue", "سماوي": "sky blue", "بترولي": "petrol",
    "كحلي": "navy", "نيفي": "navy",
    "أخضر": "green", "اخضر": "green", "زيتي": "olive", "زيتوني": "olive",
    "أصفر": "yellow", "اصفر": "yellow",
    "وردي": "pink", "زهري": "pink", "روز": "rose", "بمبي": "pink",
    "بنفسجي": "purple", "موف": "mauve", "موفي": "mauve", "ليلكي": "lilac", "لافندر": "lavender",
    "برتقالي": "orange", "مشمشي": "apricot",
    "بني": "brown", "جملي": "camel", "كاميل": "camel", "عسلي": "tan", "ترابي": "taupe",
    "بيج": "beige",
    "كريمي": "cream", "كريم": "cream",
    "ذهبي": "gold", "جولد": "gold",
    "فضي": "silver", "سيلفر": "silver", "نحاسي": "copper",
    "تركواز": "turquoise", "تيفاني": "teal",
    "خمري": "burgundy", "نبيتي": "burgundy", "عنابي": "burgundy", "بوردو": "burgundy", "ماروني": "maroon",
    "كاكي": "khaki", "خاكي": "khaki",
    "فوشيا": "fuchsia",
    "مرجاني": "coral", "كورال": "coral",
    "نعناعي": "mint", "منت": "mint",
    "ملون": "multicolor", "متعدد الألوان": "multicolor", "مطبوع": "printed", "مشجر": "floral",
}

def normalize_arabic_color(raw):
    """Strip + translate Arabic → English when known. Arabic has no letter case,
    so we match the trimmed value directly; Latin-script fallbacks are lowercased
    for cross-brand consistency. Unknown values pass through untouched so we never
    silently drop a colour we haven't mapped yet — it surfaces in the data and can
    be added to the dict (same self-revealing behaviour as the LCW map)."""
    if not raw:
        return None
    key = str(raw).strip()
    if key in ARABIC_COLOR_AR_EN:
        return ARABIC_COLOR_AR_EN[key]
    return key.lower()

# Brands whose Shopify catalogs publish colour names in Arabic. Membership here
# is the ONLY thing that routes a brand's colours through normalize_arabic_color,
# so enabling another Arabic-catalog brand later is a one-line addition and no
# English-catalog brand is ever touched.
ARABIC_COLOR_BRANDS = {"2s_egypt"}

# ── Network ───────────────────────────────────────────────────────────────────

def get_resilient_session():
    return requests.Session(impersonate="chrome124")

def get_lcw_session():
    if not WEBSHARE_PROXY:
        return requests.Session(impersonate="chrome124")
    base_user  = WEBSHARE_USER.split("-")[0]
    eg_session = random.randint(1, 900)
    eg_user    = f"{base_user}-eg-{eg_session}"
    proxy_url  = f"http://{eg_user}:{WEBSHARE_PASS}@p.webshare.io:80"
    print(f"  [LCW] Egyptian proxy session selected: -eg-{eg_session}")
    return requests.Session(impersonate="chrome124", proxies={"https": proxy_url, "http": proxy_url})

def execute_with_retry(session_method, url, max_retries=5, backoff=2, max_delay=60, **kwargs):
    """
    v14.20: widened from the original 3-attempt / 1-2-4s backoff.

    WHY: a real-world run showed Carina, 2S Egypt, Andora, Cizaro (all
    consecutive in BRANDS) and Mobaco all failing with 503/429 in the SAME
    run. Different domains, different platforms (Shopify + WooCommerce) —
    the only common factor is the GitHub Actions runner's IP. This is
    rate-limiting / a transient WAF response triggered by a burst of
    automated traffic from one IP hitting several storefronts back-to-back,
    not five independent site outages. The fix is to absorb a longer
    rate-limit window with patience rather than retry-and-give-up in ~3s.

    CHANGES:
      - max_retries 3 -> 5, backoff base 1 -> 2 (2,4,8,16,32, each capped at
        max_delay=60s) — gives a transient block roughly a minute of total
        retry window to clear before this one request gives up.
      - Random jitter (±30%) added to every delay. Without jitter, a brand
        that fails at the same script-relative moment every run (because the
        run itself is on a fixed schedule) retries on the same fixed
        cadence every time — jitter spreads retries out so they don't
        re-collide with whatever caused the block in the first place.
      - 429 specifically: if the server sends a Retry-After header, honor it
        (capped at max_delay) instead of our own backoff guess — the server
        is telling us exactly how long it wants us to wait.
      - Caller can still override max_retries/backoff per-call. See
        scrape_woocommerce for the call-site pacing fix that addresses the
        ROOT cause of Mobaco's 429s (too many requests too fast) rather than
        just retrying around it.
    """
    delay = backoff
    for attempt in range(max_retries):
        retry_after = None
        try:
            res = session_method(url, **kwargs)
            if res.status_code in (429, 500, 502, 503, 504):
                if res.status_code == 429:
                    try:
                        retry_after = float(res.headers.get("Retry-After", ""))
                    except (TypeError, ValueError):
                        retry_after = None
                err = requests.RequestsError(f"HTTP {res.status_code}")
                err.retry_after = retry_after
                raise err
            return res
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"  ❌ Network failed on {url}: {e}")
                raise e
            wait = min(getattr(e, "retry_after", None) or delay, max_delay)
            jitter = wait * random.uniform(-0.3, 0.3)
            sleep_for = max(0.5, wait + jitter)
            print(f"  ⏳ Retrying {url} in {sleep_for:.1f}s (attempt {attempt+1}/{max_retries}, {e})")
            time.sleep(sleep_for)
            delay *= 2

def safe_db_execute(query, retries=3):
    delay = 2
    for attempt in range(retries):
        try:
            return query.execute()
        except Exception as e:
            if attempt == retries - 1:
                print(f"  ❌ Supabase transaction permanently failed: {e}")
                return None
            print(f"  ⚠️ Supabase connection dropped. Retrying in {delay}s... (Attempt {attempt+1}/{retries})")
            time.sleep(delay)
            delay *= 2

# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_category(text):
    text = text.lower()
    for category, keywords in CATEGORY_MAP.items():
        if any(kw in text for kw in keywords):
            return category
    return "uncategorized"

def normalize_gender(tags, product_type, title):
    text = f"{' '.join(tags)} {product_type} {title}".lower()
    if any(w in text for w in ["women", "woman", "female", "ladies", "girl", "نسائي"]): return "women"
    if any(w in text for w in ["men", "man", "male", "gents", "رجالي"]): return "men"
    if any(w in text for w in ["kid", "child", "baby", "infant", "أطفال"]): return "kids"
    return "unisex"

def detect_options(variants):
    if not variants: return "option1", "option2"
    opt1_values = [str(v.get("option1", "")).strip() for v in variants if v.get("option1")]
    opt2_values = [str(v.get("option2", "")).strip() for v in variants if v.get("option2")]
    opt3_values = [str(v.get("option3", "")).strip() for v in variants if v.get("option3")]
    u_opt1, u_opt2, u_opt3 = len(set(opt1_values)), len(set(opt2_values)), len(set(opt3_values))
    if len(variants) > 1 and (u_opt1 == 1 or u_opt2 == 0) and u_opt2 <= 1 and u_opt3 == 0:
        if u_opt2 > u_opt1: return "option2", "option1"
        return "option1", ("option2" if opt2_values else None)
    def score_col(values):
        score = 0
        size_flags = {"xs","s","m","l","xl","xxl","3xl","4xl","os","one size","small","medium","large"}
        for val in set(values):
            v_low = val.lower()
            if v_low in size_flags: score += 10
            if v_low.isdigit() and (4 <= int(v_low) <= 56): score += 5
        return score
    scores = {"option1": score_col(opt1_values), "option2": score_col(opt2_values), "option3": score_col(opt3_values)}
    size_key = max(scores, key=scores.get)
    if scores[size_key] > 0:
        remaining = [k for k in ["option1","option2","option3"] if k != size_key and any(v.get(k) for v in variants)]
        return size_key, (remaining[0] if remaining else None)
    if u_opt1 >= u_opt2 and u_opt1 >= u_opt3:
        return "option1", ("option2" if u_opt2 > 0 else "option3" if u_opt3 > 0 else None)
    return "option2", "option1"

def check_domain(session, domain):
    try:
        return execute_with_retry(session.get, f"https://{domain}", timeout=10,
                                  headers={"User-Agent": "Mozilla/5.0"}).status_code == 200
    except:
        return False

# ── Alerts ────────────────────────────────────────────────────────────────────

def send_telegram(session, chat_id, text):
    if not TELEGRAM_BOT_TOKEN: return
    try:
        execute_with_retry(session.post, f"{TELEGRAM_API}/sendMessage",
                           json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
    except:
        pass

def find_and_alert_users(supabase, session, brand, category, variant_size,
                         current_price, product_name, product_url, variant_baseline):
    if not TELEGRAM_BOT_TOKEN or not variant_baseline or current_price >= variant_baseline:
        return
    # Quality floor (v14.7): only alert on a genuinely meaningful markdown.
    # Prevents trivial 1-2% wobble from spamming users and keeps "deal" honest.
    honest_discount = round(((variant_baseline - current_price) / variant_baseline) * 100)
    if honest_discount < MIN_ALERT_DISCOUNT_PCT:
        return
    matches = safe_db_execute(
        supabase.table("user_sizes")
        .select("user_id, users!inner(telegram_id, conversation_state, price_ceiling)")
        .eq("category", category).eq("size", variant_size)
    )
    if not matches or not matches.data: return
    for row in matches.data:
        user_info = row.get("users")
        if not user_info or user_info.get("conversation_state") != "active": continue
        uid     = user_info["telegram_id"]
        ceiling = user_info.get("price_ceiling")
        if ceiling and current_price > float(ceiling): continue
        brand_check = safe_db_execute(
            supabase.table("user_brands").select("user_id").eq("user_id", uid).eq("brand", brand)
        )
        if not brand_check or not brand_check.data: continue
        alert = (
            f"🔥 <b>Deal Alert — {BRAND_DISPLAY.get(brand, brand)}</b>\n\n"
            f"<b>{product_name}</b>\n"
            f"Size: <b>{variant_size}</b>\n"
            f"Was: <s>{int(variant_baseline)} EGP</s>  →  <b>Now: {int(current_price)} EGP</b>\n"
            f"<b>{honest_discount}% OFF (True Discount)</b>\n\n"
            f"👉 <a href='{product_url}'>Shop now</a>"
        )
        send_telegram(session, uid, alert)

# ── Snapshots ─────────────────────────────────────────────────────────────────

def load_last_prices(supabase, brand_name):
    """
    Returns {product_id: price} for this brand's MOST RECENT snapshot day
    within the last 14 days (today preferred, else the latest prior day).

    CRITICAL (v14.7): this MUST paginate. PostgREST silently caps every
    response at 1000 rows regardless of how many snapshots exist, so the
    baseline must be loaded with a .range() loop, not a single query.

    RESILIENCE (v14.12): this previously looked back only ONE day (today, else
    yesterday). For a once-daily brand (LCW) that meant a single missed run —
    e.g. when GitHub's scheduler skipped the midnight slot — left "yesterday"
    empty, so the next run found no baseline, silently re-seeded the whole
    catalog as first-sighted, and detected zero price changes for that day.
    We now pick the most recent snapshot_date that actually HAS data inside a
    14-day window. One skipped run no longer blinds detection: the next run
    just compares against the last day we do have, which correctly captures
    whatever moved during the gap. Beyond 14 days with no data we return empty
    (silent re-seed) so a long outage can't fire a flood of stale "changes".
    """
    today = date.today()
    floor = str(today - timedelta(days=14))
    latest = safe_db_execute(
        supabase.table("price_snapshots")
        .select("snapshot_date")
        .eq("brand", brand_name)
        .lte("snapshot_date", str(today))
        .gte("snapshot_date", floor)
        .order("snapshot_date", desc=True)
        .limit(1)
    )
    if not latest or not latest.data:
        return {}
    target_date = latest.data[0]["snapshot_date"]
    prices, offset = {}, 0
    while True:
        result = safe_db_execute(
            supabase.table("price_snapshots")
            .select("product_id, price")
            .eq("brand", brand_name)
            .eq("snapshot_date", target_date)
            .range(offset, offset + 999)
        )
        rows = (result.data or []) if result else []
        for row in rows:
            if row.get("product_id"):
                prices[row["product_id"]] = float(row["price"])
        if len(rows) < 1000:
            break
        offset += 1000
    return prices

# NOTE (v14.17): load_products_with_fop() was REMOVED here. It used to issue a
# second full-catalog read of the products table per brand per run just to learn
# which products already had first_observed_price. scrape_brand() now derives that
# set (fop_done_ids) for free from the variant preload it already runs, and passes
# it into each engine. See the v14.17 changelog entry at the top of the file.

def product_repr_price(records):
    """
    The single representative current price for a product = median of its
    variant prices. Detection and snapshot writing BOTH use this exact function
    so that the price stored today equals the baseline read back tomorrow when
    nothing has changed — preventing the per-variant flip-flop that otherwise
    manufactures phantom up/down events for multi-price products. (v14.8)
    """
    prices = sorted(r["_meta_price"] for r in records if r.get("_meta_price"))
    return prices[len(prices) // 2] if prices else None

def product_repr_baseline(records):
    """Representative first_observed_price for a product = median of variant baselines."""
    bases = sorted(r["_meta_baseline"] for r in records if r.get("_meta_baseline"))
    return bases[len(bases) // 2] if bases else None

def build_snapshot_rows(brand_name, product_variant_tracking, today, existing_ids):
    rows = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for db_pid, records in product_variant_tracking.items():
        if not records or db_pid in existing_ids:
            continue
        median_price = product_repr_price(records)
        if median_price is None:
            continue
        vd = records[0]
        rows.append({
            "product_id":       db_pid,
            "variant_id":       None,
            "brand":            brand_name,
            "price":            median_price,
            "compare_at_price": vd.get("_meta_compare"),
            "snapshot_date":    str(today),
            "recorded_at":      now_iso,
        })
        existing_ids.add(db_pid)
    return rows

def sync_snapshot_price(supabase, db_pid, today, price):
    """
    Keep today's snapshot equal to the latest detected price (v14.10).
    Snapshots are written once per product per day (first observation). For
    brands scraped several times a day, a sale that starts AFTER the first run
    leaves the snapshot frozen at the pre-sale price — so every later run of the
    day re-compares the new price against the stale baseline and re-fires the
    same "down" event. Updating the snapshot to the latest price after each real
    change makes the baseline track reality, so the drop is recorded once.

    This is ALSO what makes "the last run of the day = the day's close": whichever
    run touches a product last writes the price the weekly aggregation rolls up.
    """
    safe_db_execute(
        supabase.table("price_snapshots")
        .update({"price": price})
        .eq("product_id", db_pid)
        .eq("snapshot_date", str(today))
    )

def detect_and_write_stockout(supabase, variant_db_id, product_id, brand,
                               size, color, prev_stock, curr_stock, curr_price, baseline):
    if prev_stock == curr_stock: return
    event_type   = "stockout" if (prev_stock and not curr_stock) else "restock"
    discount_pct = round(((baseline - curr_price) / baseline) * 100, 2) if (baseline and curr_price < baseline) else None
    safe_db_execute(supabase.table("stockout_events").insert({
        "variant_id":           variant_db_id,
        "product_id":           product_id,
        "brand":                brand,
        "size":                 size,
        "color":                color,
        "event_type":           event_type,
        "price_at_event":       curr_price,
        "discount_pct_at_event": discount_pct,
        "was_on_discount":      bool(discount_pct),
        "recorded_at":          datetime.now(timezone.utc).isoformat(),
    }))

# ── Color-from-title extraction (Tree, Cizaro) — v14.19 addition ────────────
#
# CONTEXT: Tree and Cizaro publish each colourway as a SEPARATE Shopify
# product rather than as a variant option (e.g. "Black T-Shirt" and "White
# T-Shirt" are two different products, not one product with a colour
# dropdown). detect_options() correctly finds no colour option for these
# brands because there genuinely isn't one in the Shopify variant structure —
# this was never a parsing bug, it's why color coverage measured 13.2% (Tree)
# and 0% (Cizaro) in the June 2026 data integrity audit. The colour IS
# present, but only in the product TITLE.
#
# TESTED ACCURACY (against 76-79 real titles sampled from live Supabase data,
# 20 Jun 2026, graded by hand against ground truth):
#   Tree:    88.2% exact match, 0% wrong (false colour), 7.9% honest miss
#            (typo or unmapped word -> NULL, same as today), 3.9% partial
#            (got the base colour, missed a modifier word like "dark").
#   Cizaro:  93.4% exact match, 0% wrong (false colour), 3.9% honest miss,
#            2.6% genuinely ambiguous in the source title itself (e.g. a
#            title naming both a body colour and a hardware finish colour
#            with no reliable rule for which is "the" product colour).
# The only failure category that matters — extracting an ACTIVELY WRONG
# colour — is 0% for both brands after two rounds of fixes (excluding
# "denim"/"raw denim"/"ice" as false-positive matches, and guarding against
# metal hardware finishes being mistaken for garment colour on buckle-only
# products). Every other failure mode degrades to NULL — the same honest gap
# that exists today — never to a wrong value.
#
# DESIGN: a brand only runs through this path if it's in
# COLOR_FROM_TITLE_BRANDS. No other brand's colour handling changes at all.
# This keeps the blast radius of this feature to exactly the two brands it
# was built for.

# Canonical colour vocabulary for title extraction. Deliberately reuses
# English colour words that already appear as VALUES in this file's own
# LCW_COLOR_TR_EN and ARABIC_COLOR_AR_EN translation maps, plus common
# English colour words not already covered — so this isn't a third,
# disconnected colour list, it's the same vocabulary the brand already
# trusts for LCW and 2S Egypt.
TITLE_COLOR_WORDS = {
    # multi-word phrases — checked before single words so "Navy Blue" wins
    # over a lone "Blue" match (longest-match-wins via the sort below)
    "off-white", "off white", "light blue", "sky blue", "baby blue",
    "navy blue", "mid blue", "dark blue", "ocean blue", "marine blue",
    "acid blue", "bright mid blue", "powder blue", "midnight navy",
    "light grey", "dark grey", "washed black", "washed grey",
    "vintage blue", "deep vintage blue", "oat beige", "rose gold",
    "mint green", "dark grey", "dark olive", "dark mint", "steel blue",
    "army green",
    # single words
    "black", "white", "grey", "gray", "anthracite", "red", "burgundy",
    "pink", "fuchsia", "coral", "orange", "yellow", "ecru", "beige",
    "brown", "khaki", "green", "olive", "petrol", "turquoise", "blue",
    "navy", "indigo", "purple", "plum", "lilac", "multicolor", "cream",
    "gold", "silver", "copper", "camel", "tan", "taupe", "mauve",
    "lavender", "apricot", "maroon", "mint", "chocolate", "honey",
    "lemon",
}
# NOTE: "denim", "raw denim", and "ice" are intentionally EXCLUDED from this
# vocabulary. Testing showed these are finish/material names in Cizaro's
# catalog ("Ice Denim ... Buckle"), never real garment colours, and always
# produced a wrong match when included. See extract_color_cizaro() below.
TITLE_COLOR_WORDS_SORTED = sorted(TITLE_COLOR_WORDS, key=len, reverse=True)

# Words that, if found in a title, mean an apparent "colour" match is
# actually describing belt/buckle hardware finish, not the garment — so a
# gold/silver/copper match alone (with nothing else found) should be
# discarded rather than stored as the product's colour.
METAL_FINISHES = {"gold", "silver", "copper"}


def extract_color_tree(name):
    """
    Tree's title pattern, in two passes:

    PASS 1 (original, unchanged): colour is the trailing segment after the
    LAST dash-like separator (-, –, —, |), requiring an EXACT match against
    a known colour phrase. This is what keeps the false-positive rate at 0%
    — a segment that doesn't fully resolve to a recognised colour word falls
    through rather than guessing.

    PASS 2 (v14.21 addition): if Pass 1 finds nothing, scan the WHOLE title
    for the longest matching colour phrase (same technique as
    extract_color_cizaro), since a meaningful share of Tree's catalog puts
    the colour at the start or middle of the title instead of as a clean
    trailing segment ("Men's White Casual Polo Shirt", "Men's Running Shoes
    Black", "Boys' Red Crew Neck Sweatshirt - Cotton Blend - Print").

    TESTED (20 Jun 2026, two independent random samples from live data,
    150 + 60 titles, hand-graded against ground truth): Pass 2 recovered
    color on ~25% of previously-NULL Tree variants with ZERO new false
    positives across both samples — every title that genuinely has no
    colour word still correctly returns None; every risk case from the
    original Pass-1-only testing (brand tag "TREE", "Regular fit", "Print"
    as a texture word) still correctly returns None. The only quality loss
    is occasionally dropping a modifier word ("Steel Blue" -> "blue", "Dark
    Mint" -> "mint") — never a wrong base colour.

    Returns a lowercase colour phrase, or None.
    """
    parts = re.split(r'[-–—|]', name)
    if len(parts) >= 2:
        tail = parts[-1].strip().lower()
        if tail:
            for phrase in TITLE_COLOR_WORDS_SORTED:
                if tail == phrase:
                    return phrase

    # Pass 2: whole-title scan, longest-match-wins, only reached when Pass 1
    # found nothing.
    lower = name.lower()
    for phrase in TITLE_COLOR_WORDS_SORTED:
        if re.search(r'(?<![a-z])' + re.escape(phrase) + r'(?![a-z])', lower):
            return phrase
    return None


def extract_color_cizaro(name):
    """
    Cizaro's title pattern has no reliable separator — the colour phrase can
    appear anywhere (start, middle, end), so we scan the whole lowercased
    title for the longest matching colour phrase, deliberately NOT trusting
    position. Two corrections were required after testing against real data
    to eliminate every false-positive case found:

      1. "denim"/"raw denim"/"ice" are excluded from the vocabulary entirely
         — they're finish/material names in this catalog, never colours.
      2. If the only match found is a metal finish word (gold/silver/copper)
         AND the title contains "buckle", that match is hardware finish, not
         garment colour — we look for any other real colour word instead,
         or return None if there isn't one, rather than storing the
         hardware's finish as the product's colour.

    Returns a lowercase colour phrase, or None.
    """
    lower = name.lower()
    scan_target = re.sub(r'\braw\s+denim\b', ' ', lower)
    scan_target = re.sub(r'\bdenim\b', ' ', scan_target)
    scan_target = re.sub(r'\bice\b', ' ', scan_target)

    found = None
    for phrase in TITLE_COLOR_WORDS_SORTED:
        if re.search(r'(?<![a-z])' + re.escape(phrase) + r'(?![a-z])', scan_target):
            found = phrase
            break

    if found in METAL_FINISHES and "buckle" in lower:
        for phrase in TITLE_COLOR_WORDS_SORTED:
            if phrase in METAL_FINISHES:
                continue
            if re.search(r'(?<![a-z])' + re.escape(phrase) + r'(?![a-z])', scan_target):
                return phrase
        return None  # hardware-only title — no real garment colour to report

    return found


def extract_color_activ(name):
    """
    Activ's title pattern (confirmed live, 100% of catalog): the colour is
    the trailing segment after the LAST " - " separator, and it is in
    ARABIC. Examples:
        "ACTIV MEN'S FLIP FLOP - BLACK"           -> "black"
        "سويت شيرت اكتف بناتي - بني"                -> "بني" (brown)
        "حذاء اكتف اطفالي فاشون - كحلي*ازرق"        -> "كحلي*ازرق" (navy/white two-tone)

    We do NOT try to match against a fixed vocabulary here, because Activ's
    colours are Arabic free-text that the colour dictionary (color_map)
    resolves downstream — the same machine that handles every other brand's
    raw colours. So this extractor's only job is to isolate the trailing
    segment cleanly. The dictionary decides what family it maps to.

    Returns the trailing segment (lowercased, trimmed) or None if there's no
    dash separator or the segment is implausible as a colour (too long/short).
    """
    if " - " not in name:
        return None
    tail = name.rsplit(" - ", 1)[-1].strip().lower()
    # Sanity: a real colour segment is short. Reject anything that's clearly
    # not a colour (a whole sentence, or an empty tail).
    if not tail or len(tail) > 30:
        return None
    return tail


# Brand -> extraction function. Membership here is the ONLY thing that
# routes a brand's products through title-based colour extraction, so
# enabling another brand later is a one-line addition and no brand outside
# this dict is ever touched.
COLOR_FROM_TITLE_BRANDS = {
    "tree":   extract_color_tree,
    "cizaro": extract_color_cizaro,
    "activ":  extract_color_activ,
}


def extract_color_from_title(brand_name, product_name):
    """
    Single entry point used by scrape_shopify(). Returns a lowercase colour
    string, or None if the brand isn't in COLOR_FROM_TITLE_BRANDS, or if the
    brand's extractor doesn't find a confident match. Never raises — a
    malformed title degrades to None, same as today's honest gap, rather
    than crashing the scrape.
    """
    extractor = COLOR_FROM_TITLE_BRANDS.get(brand_name)
    if not extractor:
        return None
    try:
        return extractor(product_name)
    except Exception:
        return None

# ── Sub-category attribute extraction (sleeve length, bottoms fit/cut) ──────
# v14.24 addition.
#
# CONTEXT: Mohammed asked whether finer sub-category detail (sleeve length on
# tops; fit/cut — wide leg, skinny, baggy, chino vs jeans vs sweatpants — on
# bottoms) could be scraped reliably across brands. Investigated by sampling
# 10 random products per brand (all 14) plus full-table regex checks against
# every top-like and bottom-like product. Findings, confirmed against live
# Supabase data on 22 Jun 2026:
#
#   SLEEVE LENGTH lives in TWO different fields depending on the brand, never
#   both, never neither for the brands where it's stated at all:
#     - category_raw is the strong signal for: town_team (21% of tops; title
#       has 0%), ravin (46% in category vs 21% in title), and to a lesser
#       extent mobaco.
#     - title is the strong signal for: defacto (55%), lc_waikiki (18%),
#       andora, tree, carina.
#     - 2s_egypt, dott_jeans, mens_club: low coverage either way — these
#       catalogs (loungewear, basics) mostly don't state sleeve length at all,
#       which is an honest gap in the SOURCE, not a parsing miss.
#
#   BOTTOMS FIT/CUT is dominantly a TITLE signal almost everywhere (cizaro
#   90%, esla 91%, dott_jeans 87%, defacto 61%, ravin 69%, lc_waikiki 49%),
#   with TWO exceptions where category_raw is the real signal instead:
#     - town_team: fit/cut is ONLY in category_raw (e.g. "Men Jeans" vs
#       "Men Pants" — 16% coverage), 0% in title.
#     - cizaro: ALSO has strong category_raw coverage (178/232, 77%) in
#       addition to title — but category_raw is written extremely
#       inconsistently ("Wide Leg", "wide-leg", "Wide-Leg", "Wideleg", "Wide-
#       leg", "Wide Leg , non denim , trousers" are all the same fit), so it
#       must be aggressively normalised (strip spaces/punctuation, lowercase)
#       before matching a canonical vocabulary — a direct substring match on
#       the raw string would miss most of these variants.
#     - mens_club: category_raw has some signal (e.g. "032 Chino") worth
#       checking first since it's cleaner than mens_club's titles for this.
#
#   2s_egypt is a near-total gap (2/3,091 bottoms) for a DIFFERENT reason than
#   "missing data": its catalog is dominated by pyjamas/loungewear, and several
#   of its "jeans"/"leggings" classified products are actually pyjamas whose
#   title mentions "jeans" only as a FABRIC LOOK descriptor (e.g. "بيجامه
#   رجالي جينز" = men's denim-look pyjama) — normalize_category()'s keyword
#   scan is catching the fabric word, not a real bottoms garment. This is a
#   pre-existing category_normalized accuracy issue, NOT something this
#   feature should paper over by inventing a fit value — flagged to Mohammed
#   separately, left untouched here.
#
# DESIGN: two independent extractors, each gated to the categories where the
# attribute is meaningful (sleeve length only for top-like categories; fit/cut
# only for bottom-like categories), each checking fields in a BRAND-SPECIFIC
# priority order discovered above, falling back to the other field if the
# first is empty. Both return None — never a guess — when nothing matches,
# exactly like the existing colour extractors. Result is stored in the new
# products.attributes_extracted JSONB column as e.g. {"sleeve_length": "long
# sleeve"} or {"fit": "wide leg"}, never both keys at once since a product is
# either top-like or bottom-like, not both.

TOP_LIKE_CATEGORIES    = {"t-shirts", "shirts", "polos", "sweatshirts", "hoodies",
                          "cardigans", "sweaters", "bodysuits", "tank-tops", "blazers"}
BOTTOM_LIKE_CATEGORIES = {"jeans", "trousers", "shorts", "leggings", "joggers", "sweatpants"}

# Sleeve length vocabulary. Multi-word phrases first so "short sleeve" wins
# over a lone "sleeve" — not that "sleeve" alone is in this list, but kept
# consistent with the longest-match-wins pattern used by the colour lists.
SLEEVE_LENGTH_WORDS = [
    "short sleeve", "short-sleeve", "short sleeves", "short-sleeves",
    "long sleeve", "long-sleeve", "long sleeves", "long-sleeves",
    "half sleeve", "half-sleeve", "half sleeves",
    "full sleeve", "full-sleeve", "full sleeves",
    "sleeveless", "elbow sleeve", "elbow-sleeve",
    "3/4 sleeve", "three-quarter sleeve",
]
SLEEVE_LENGTH_WORDS_SORTED = sorted(SLEEVE_LENGTH_WORDS, key=len, reverse=True)

# Canonical sleeve-length labels each raw phrase maps to, so "short-sleeve"
# and "short sleeves" both store as the same value rather than fragmenting
# the signal by punctuation/plural variants.
SLEEVE_LENGTH_CANONICAL = {
    "short sleeve": "short sleeve", "short-sleeve": "short sleeve",
    "short sleeves": "short sleeve", "short-sleeves": "short sleeve",
    "long sleeve": "long sleeve", "long-sleeve": "long sleeve",
    "long sleeves": "long sleeve", "long-sleeves": "long sleeve",
    "half sleeve": "half sleeve", "half-sleeve": "half sleeve",
    "half sleeves": "half sleeve",
    "full sleeve": "long sleeve", "full-sleeve": "long sleeve",
    "full sleeves": "long sleeve",
    "sleeveless": "sleeveless",
    "elbow sleeve": "elbow sleeve", "elbow-sleeve": "elbow sleeve",
    "3/4 sleeve": "three-quarter sleeve", "three-quarter sleeve": "three-quarter sleeve",
}

def _find_sleeve_length(text):
    """Longest-match scan for a sleeve-length phrase in already-lowercased text.
    Returns the canonical label, or None."""
    if not text:
        return None
    lower = text.lower()
    for phrase in SLEEVE_LENGTH_WORDS_SORTED:
        if phrase in lower:
            return SLEEVE_LENGTH_CANONICAL.get(phrase, phrase)
    return None

# Brands where category_raw is the stronger sleeve-length signal — checked
# FIRST for these, with title as fallback. Every other brand checks title
# first, with category_raw as fallback. Confirmed from the live coverage
# numbers in the block comment above.
SLEEVE_LENGTH_CATEGORY_FIRST_BRANDS = {"town_team", "ravin", "mobaco"}

def extract_sleeve_length(brand_name, category_normalized, category_raw, name):
    """
    Returns a canonical sleeve-length string, or None if not a top-like
    category, or if neither field yields a confident match. Never raises.
    """
    if category_normalized not in TOP_LIKE_CATEGORIES:
        return None
    try:
        if brand_name in SLEEVE_LENGTH_CATEGORY_FIRST_BRANDS:
            return _find_sleeve_length(category_raw) or _find_sleeve_length(name)
        return _find_sleeve_length(name) or _find_sleeve_length(category_raw)
    except Exception:
        return None

# Bottoms fit/cut vocabulary. Longest-match-wins ordering via the sort below.
# "mom fit"/"mom jeans" intentionally separate from "regular fit" — a "mom"
# cut is a recognisable, named silhouette in this market's listings, not
# interchangeable with a generic "regular fit" label.
FIT_CUT_WORDS = [
    "extreme wide leg", "super baggy", "loose baggy", "wide leg", "wide-leg",
    "wideleg", "wide tailored", "skinny fit", "slim fit", "slim-fit",
    "regular fit", "relaxed fit", "straight fit", "tapered fit",
    "high waist", "high-waist", "boyfriend", "mom fit", "mom-fit", "momfit",
    "mom jeans", "bootcut", "boot cut", "flare", "flared", "baggy",
    "skinny", "slim", "straight", "tapered", "relaxed", "loose",
    "jogger", "joggers", "cargo", "chino", "chinos", "sweatpants",
    "sweatpant", "curvy fit", "skater",
]
FIT_CUT_WORDS_SORTED = sorted(FIT_CUT_WORDS, key=len, reverse=True)

# Canonical fit/cut labels — collapses spelling/spacing/punctuation variants
# to one stored value. This is required for Cizaro in particular: its
# category_raw has been observed as "Wide Leg", "wide leg", "Wide-Leg",
# "Wideleg", "Wide- leg", and "wide-leg" for the exact same fit.
FIT_CUT_CANONICAL = {
    "extreme wide leg": "wide leg", "wide leg": "wide leg", "wide-leg": "wide leg",
    "wideleg": "wide leg", "wide tailored": "wide leg",
    "super baggy": "baggy", "loose baggy": "baggy", "baggy": "baggy", "loose": "baggy",
    "skinny fit": "skinny", "skinny": "skinny",
    "slim fit": "slim", "slim-fit": "slim", "slim": "slim",
    "regular fit": "regular", "straight fit": "straight", "straight": "straight",
    "relaxed fit": "relaxed", "relaxed": "relaxed",
    "tapered fit": "tapered", "tapered": "tapered",
    "high waist": "high waist", "high-waist": "high waist",
    "boyfriend": "boyfriend",
    "mom fit": "mom fit", "mom-fit": "mom fit", "momfit": "mom fit", "mom jeans": "mom fit",
    "bootcut": "bootcut", "boot cut": "bootcut",
    "flare": "flare", "flared": "flare",
    "jogger": "jogger", "joggers": "jogger",
    "cargo": "cargo",
    "chino": "chino", "chinos": "chino",
    "sweatpants": "sweatpants", "sweatpant": "sweatpants",
    "curvy fit": "curvy fit",
    "skater": "skater",
}

def _normalize_for_fit_match(text):
    """
    Aggressive normalisation before fit/cut matching: lowercase, then collapse
    every run of non-alphanumeric characters (spaces, dashes, commas, multiple
    dashes, etc.) down to a single space. This is what lets "Wide-Leg",
    "Wideleg" — wait, "Wideleg" has no separator at all, so it's listed as its
    own vocabulary entry above rather than relying on normalisation to invent
    a word boundary that isn't there. For every OTHER punctuation/spacing
    variant ("Wide Leg" / "Wide-Leg" / "Wide- leg" / "wide-leg"), collapsing
    separators to single spaces is what makes one canonical phrase match all
    of them.
    """
    if not text:
        return ""
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).strip()

def _find_fit_cut(text):
    """Longest-match scan for a fit/cut phrase in normalised text. Returns the
    canonical label, or None."""
    normalized = _normalize_for_fit_match(text)
    if not normalized:
        return None
    for phrase in FIT_CUT_WORDS_SORTED:
        phrase_normalized = _normalize_for_fit_match(phrase)
        if phrase_normalized and phrase_normalized in normalized:
            return FIT_CUT_CANONICAL.get(phrase, phrase)
    return None

# Brands where category_raw is the stronger (or, for Cizaro, an additional
# strong) fit/cut signal — checked FIRST for these, with title as fallback.
# Every other brand checks title first. Confirmed from the live coverage
# numbers in the block comment above.
FIT_CUT_CATEGORY_FIRST_BRANDS = {"town_team", "cizaro", "mens_club"}

def _is_2s_egypt_pajama_contaminated(category_raw, name):
    """
    v14.24 guard: confirmed live (22 Jun 2026) that 75% of 2s_egypt's
    products classified into a bottoms category (jeans/trousers/shorts/
    leggings/joggers/sweatpants — 2,316 of 3,091) are actually PAJAMAS. The
    pre-existing normalize_category() keyword scan catches "jeans" or
    "sweatpant" used as a FABRIC/STYLE descriptor inside a pajama title
    (e.g. "بيجامه رجالي جينز" = "men's denim-look pyjama") and misfiles it as
    a real denim/sweatpants garment. Extracting a fit/cut value for these
    would manufacture a false sub-category on top of an already-wrong base
    category. This guard does NOT fix normalize_category() itself (a
    separate, pre-existing issue flagged to Mohammed outside this feature)
    — it only stops THIS feature from compounding that error. Scoped to
    2s_egypt only; no other brand's fit/cut extraction is affected.
    """
    raw_lower = (category_raw or "").lower()
    return "pajama" in raw_lower or "بيجام" in (name or "")

def extract_fit_cut(brand_name, category_normalized, category_raw, name):
    """
    Returns a canonical fit/cut string, or None if not a bottom-like
    category, or if neither field yields a confident match. Never raises.
    """
    if category_normalized not in BOTTOM_LIKE_CATEGORIES:
        return None
    if brand_name == "2s_egypt" and _is_2s_egypt_pajama_contaminated(category_raw, name):
        return None
    try:
        if brand_name in FIT_CUT_CATEGORY_FIRST_BRANDS:
            return _find_fit_cut(category_raw) or _find_fit_cut(name)
        return _find_fit_cut(name) or _find_fit_cut(category_raw)
    except Exception:
        return None

# ── Sub-category attribute extraction, ROUND 2 — v14.25 addition ───────────
# Same generalised pattern as v14.24 (sleeve length / fit-cut), extended to
# five more category groups after Mohammed asked whether the concept scales
# beyond tops/bottoms. Investigated the same way: pulled real samples + ran
# regex coverage checks against the live database BEFORE writing any
# extraction code, for every group. Findings (22 Jun 2026, against the live
# 47,972-row products table):
#
#   DRESSES (1,186 total) — THREE separate, independently-occurring
#   attributes, not one: length (mini/midi/maxi — 252 hits, 21%), silhouette
#   (a-line/bodycon/wrap/shirt dress/fit-and-flare — 170 hits, 14%), and
#   sleeve length (136 hits, 11% — reuses the EXACT same vocabulary already
#   built for tops, since "sleeveless"/"long sleeve" mean the same thing on
#   a dress as on a shirt).
#
#   JACKETS/COATS (1,402 total) — ONE attribute, jacket type/style (puffer,
#   bomber, denim jacket, leather jacket, blazer, parka, trench, quilted,
#   windbreaker, biker — 402 hits in title, 29%; category_raw only adds 45
#   more, title is clearly dominant here, no brand-specific override needed).
#
#   UNDERWEAR (964 total) — split into TWO attributes gated by which garment
#   the product actually is (never both on one product): bra/lounge-top
#   style (push-up, padded, unpadded, wireless, wired, strapless, bralette,
#   seamless) when the title says bra/lingerie-top language, vs brief/panty
#   style (thong, bikini, hipster, brazilian, boxer) when it says
#   panty/knicker/boxer language. Combined coverage 364/964 (38%). Bundling
#   these into one generic "underwear style" field would have produced
#   nonsense — a brief's silhouette and a bra's support style are not the
#   same axis, so they're stored under different keys.
#
#   SWIMWEAR (280 total) — ONE attribute, silhouette (one-piece, bikini,
#   swimsuit, tankini, burkini, board short — 174 hits, 62%, the strongest
#   coverage of any group checked in this round).
#
#   BAGS (1,014 total) — ONE attribute, bag type (shoulder bag, crossbody,
#   tote, backpack, clutch, duffel, wallet, satchel, handbag, sling, messenger
#   — 450 hits, 44%). NOTE: "wallet" is included in this vocabulary because
#   it currently shares the bags base category (a pre-existing
#   category_normalized grouping choice, not something this feature changes)
#   — flagged to Mohammed, not fixed here.
#
#   CHECKED AND DELIBERATELY EXCLUDED — footwear (sneakers/sandals/boots/
#   loafers/heels/slippers, ~307 combined) and belts/hats/jewelry (~1,359
#   combined): sampled the same way, found mostly Arabic titles with little
#   recurring English descriptive vocabulary and/or signal too scattered to
#   justify a vocabulary list right now. Revisit if these brands' English
#   title coverage improves, or if Mohammed has brand-specific knowledge of
#   a pattern that isn't visible from text alone.
#
# DESIGN: identical pattern to v14.24 — category-gated, longest-match-wins,
# canonical-label collapsing, returns None rather than guessing. None of
# these five needed a brand-specific field-priority override the way
# town_team/cizaro/mens_club did for fit-cut — title was the dominant signal
# in every one of these five groups, so each new extractor checks title only
# (category_raw's contribution was consistently small enough not to be worth
# the added complexity of a fallback check, except where noted below).

DRESS_CATEGORIES    = {"dresses"}
JACKET_CATEGORIES   = {"jackets", "coats"}
UNDERWEAR_CATEGORIES = {"underwear"}
SWIMWEAR_CATEGORIES  = {"swimwear"}
BAG_CATEGORIES        = {"bags"}

# -- Dress length --
DRESS_LENGTH_WORDS = ["mini dress", "midi dress", "maxi dress",
                      "knee length", "knee-length", "ankle length", "ankle-length",
                      "mini", "midi", "maxi"]
DRESS_LENGTH_WORDS_SORTED = sorted(DRESS_LENGTH_WORDS, key=len, reverse=True)
DRESS_LENGTH_CANONICAL = {
    "mini dress": "mini", "midi dress": "midi", "maxi dress": "maxi",
    "knee length": "knee-length", "knee-length": "knee-length",
    "ankle length": "ankle-length", "ankle-length": "ankle-length",
    "mini": "mini", "midi": "midi", "maxi": "maxi",
}

def extract_dress_length(category_normalized, name):
    if category_normalized not in DRESS_CATEGORIES or not name:
        return None
    try:
        lower = name.lower()
        for phrase in DRESS_LENGTH_WORDS_SORTED:
            if phrase in lower:
                return DRESS_LENGTH_CANONICAL.get(phrase, phrase)
        return None
    except Exception:
        return None

# -- Dress silhouette --
DRESS_SILHOUETTE_WORDS = ["fit and flare", "fit-and-flare", "fit & flare",
                          "a-line", "a line", "bodycon", "shirt dress",
                          "wrap dress", "shift dress", "slip dress", "bandage"]
DRESS_SILHOUETTE_WORDS_SORTED = sorted(DRESS_SILHOUETTE_WORDS, key=len, reverse=True)
DRESS_SILHOUETTE_CANONICAL = {
    "fit and flare": "fit and flare", "fit-and-flare": "fit and flare",
    "fit & flare": "fit and flare",
    "a-line": "a-line", "a line": "a-line",
    "bodycon": "bodycon", "shirt dress": "shirt dress",
    "wrap dress": "wrap", "shift dress": "shift", "slip dress": "slip",
    "bandage": "bandage",
}

def extract_dress_silhouette(category_normalized, name):
    if category_normalized not in DRESS_CATEGORIES or not name:
        return None
    try:
        lower = name.lower()
        for phrase in DRESS_SILHOUETTE_WORDS_SORTED:
            if phrase in lower:
                return DRESS_SILHOUETTE_CANONICAL.get(phrase, phrase)
        return None
    except Exception:
        return None

# -- Jacket/coat type --
JACKET_TYPE_WORDS = ["denim jacket", "leather jacket", "bomber jacket",
                     "puffer jacket", "biker jacket",
                     "puffer", "bomber", "blazer", "parka", "trench",
                     "quilted", "windbreaker", "biker", "varsity"]
JACKET_TYPE_WORDS_SORTED = sorted(JACKET_TYPE_WORDS, key=len, reverse=True)
JACKET_TYPE_CANONICAL = {
    "denim jacket": "denim", "leather jacket": "leather",
    "bomber jacket": "bomber", "puffer jacket": "puffer", "biker jacket": "biker",
    "puffer": "puffer", "bomber": "bomber", "blazer": "blazer",
    "parka": "parka", "trench": "trench", "quilted": "quilted",
    "windbreaker": "windbreaker", "biker": "biker", "varsity": "varsity",
}

def extract_jacket_type(category_normalized, name):
    if category_normalized not in JACKET_CATEGORIES or not name:
        return None
    try:
        lower = name.lower()
        for phrase in JACKET_TYPE_WORDS_SORTED:
            if phrase in lower:
                return JACKET_TYPE_CANONICAL.get(phrase, phrase)
        return None
    except Exception:
        return None

# -- Underwear: bra/top style vs brief/bottom style — gated by garment word --
UNDERWEAR_TOP_HINTS    = ("bra", "bralette", "lingerie top", "corset", "bustier")
UNDERWEAR_BOTTOM_HINTS = ("panty", "panties", "brief", "knicker", "boxer", "thong")

UNDERWEAR_TOP_STYLE_WORDS = ["push-up", "push up", "padded", "unpadded",
                             "wireless", "wire-free", "wired", "non-wired",
                             "strapless", "seamless", "bralette"]
UNDERWEAR_TOP_STYLE_SORTED = sorted(UNDERWEAR_TOP_STYLE_WORDS, key=len, reverse=True)
UNDERWEAR_TOP_STYLE_CANONICAL = {
    "push-up": "push-up", "push up": "push-up",
    "padded": "padded", "unpadded": "unpadded",
    "wireless": "wireless", "wire-free": "wireless", "wired": "wired",
    "non-wired": "wireless", "strapless": "strapless",
    "seamless": "seamless", "bralette": "bralette",
}

UNDERWEAR_BOTTOM_STYLE_WORDS = ["bikini brief", "boy short", "boyshort",
                                "brazilian", "hipster", "thong", "full coverage",
                                "full brief"]
UNDERWEAR_BOTTOM_STYLE_SORTED = sorted(UNDERWEAR_BOTTOM_STYLE_WORDS, key=len, reverse=True)
UNDERWEAR_BOTTOM_STYLE_CANONICAL = {
    "bikini brief": "bikini", "boy short": "boyshort", "boyshort": "boyshort",
    "brazilian": "brazilian", "hipster": "hipster", "thong": "thong",
    "full coverage": "full brief", "full brief": "full brief",
}

def extract_underwear_style(category_normalized, name):
    """
    Returns ("top_style", value) or ("bottom_style", value) or None — gated
    by which garment-type word the title contains, so a bra's support
    style and a brief's cut are never confused with each other. If the
    title contains neither a top-garment word nor a bottom-garment word
    (e.g. a generic "Lingerie Set"), returns None rather than guessing
    which axis applies.
    """
    if category_normalized not in UNDERWEAR_CATEGORIES or not name:
        return None
    try:
        lower = name.lower()
        is_top    = any(h in lower for h in UNDERWEAR_TOP_HINTS)
        is_bottom = any(h in lower for h in UNDERWEAR_BOTTOM_HINTS)
        if is_top and not is_bottom:
            for phrase in UNDERWEAR_TOP_STYLE_SORTED:
                if phrase in lower:
                    return ("top_style", UNDERWEAR_TOP_STYLE_CANONICAL.get(phrase, phrase))
        elif is_bottom and not is_top:
            for phrase in UNDERWEAR_BOTTOM_STYLE_SORTED:
                if phrase in lower:
                    return ("bottom_style", UNDERWEAR_BOTTOM_STYLE_CANONICAL.get(phrase, phrase))
        return None
    except Exception:
        return None

# -- Swimwear silhouette --
SWIMWEAR_WORDS = ["board short", "one-piece", "one piece", "bikini",
                  "swimsuit", "tankini", "burkini"]
SWIMWEAR_WORDS_SORTED = sorted(SWIMWEAR_WORDS, key=len, reverse=True)
SWIMWEAR_CANONICAL = {
    "board short": "board short",
    "one-piece": "one-piece", "one piece": "one-piece",
    "bikini": "bikini", "swimsuit": "swimsuit",
    "tankini": "tankini", "burkini": "burkini",
}

def extract_swimwear_silhouette(category_normalized, name):
    if category_normalized not in SWIMWEAR_CATEGORIES or not name:
        return None
    try:
        lower = name.lower()
        for phrase in SWIMWEAR_WORDS_SORTED:
            if phrase in lower:
                return SWIMWEAR_CANONICAL.get(phrase, phrase)
        return None
    except Exception:
        return None

# -- Bag type --
BAG_TYPE_WORDS = ["shoulder bag", "crossbody bag", "crossbody", "tote bag",
                  "tote", "backpack", "clutch", "duffel", "wallet",
                  "satchel", "handbag", "sling bag", "sling", "messenger bag",
                  "messenger"]
BAG_TYPE_WORDS_SORTED = sorted(BAG_TYPE_WORDS, key=len, reverse=True)
BAG_TYPE_CANONICAL = {
    "shoulder bag": "shoulder bag", "crossbody bag": "crossbody", "crossbody": "crossbody",
    "tote bag": "tote", "tote": "tote", "backpack": "backpack", "clutch": "clutch",
    "duffel": "duffel", "wallet": "wallet", "satchel": "satchel",
    "handbag": "handbag", "sling bag": "sling", "sling": "sling",
    "messenger bag": "messenger", "messenger": "messenger",
}

def extract_bag_type(category_normalized, name):
    if category_normalized not in BAG_CATEGORIES or not name:
        return None
    try:
        lower = name.lower()
        for phrase in BAG_TYPE_WORDS_SORTED:
            if phrase in lower:
                return BAG_TYPE_CANONICAL.get(phrase, phrase)
        return None
    except Exception:
        return None

def build_attributes_extracted(brand_name, category_normalized, category_raw, name):
    """
    Single entry point used by every engine. Returns a dict suitable for the
    products.attributes_extracted JSONB column. Each key is independently
    optional and never raises; absence of a key means genuinely not stated
    by the source, not a parsing failure. v14.24 added sleeve_length/fit;
    v14.25 adds dress_length/dress_silhouette (dresses), jacket_type
    (jackets/coats), top_style/bottom_style (underwear, mutually exclusive
    per the garment-word gate in extract_underwear_style), swimwear_style,
    and bag_type. A product only ever gets keys relevant to its own
    category_normalized — e.g. a t-shirt never gets a bag_type key.
    """
    attrs = {}

    sleeve = extract_sleeve_length(brand_name, category_normalized, category_raw, name)
    if sleeve:
        attrs["sleeve_length"] = sleeve

    fit = extract_fit_cut(brand_name, category_normalized, category_raw, name)
    if fit:
        attrs["fit"] = fit

    dress_len = extract_dress_length(category_normalized, name)
    if dress_len:
        attrs["dress_length"] = dress_len

    dress_sil = extract_dress_silhouette(category_normalized, name)
    if dress_sil:
        attrs["dress_silhouette"] = dress_sil

    jacket_type = extract_jacket_type(category_normalized, name)
    if jacket_type:
        attrs["jacket_type"] = jacket_type

    underwear_result = extract_underwear_style(category_normalized, name)
    if underwear_result:
        key, value = underwear_result
        attrs[key] = value

    swim = extract_swimwear_silhouette(category_normalized, name)
    if swim:
        attrs["swimwear_style"] = swim

    bag = extract_bag_type(category_normalized, name)
    if bag:
        attrs["bag_type"] = bag

    return attrs

# ── Shopify Scraper ───────────────────────────────────────────────────────────

def scrape_shopify(supabase, session, brand_name, domain, today, prev_stock_state, fop_done_ids):
    products_seen, price_changes = 0, 0
    page = 1                     # v14.18: kept on ?page=N (since_id is Admin-API only)
    prev_prices = load_last_prices(supabase, brand_name)
    # v14.17: fop_done_ids is passed in from scrape_brand (derived from the
    # variant preload we already load). The separate load_products_with_fop()
    # full-catalog read per brand per run is gone. The set is still mutated
    # locally as new products get seeded so the same row can't be written twice
    # if it appears on multiple catalog pages.
    existing_snapshot_ids = set()
    _snap_offset = 0
    while True:
        _snap = safe_db_execute(
            supabase.table("price_snapshots").select("product_id")
            .eq("brand", brand_name).eq("snapshot_date", str(today))
            .range(_snap_offset, _snap_offset + 999)
        )
        _rows = (_snap.data or []) if _snap else []
        for r in _rows:
            if r.get("product_id"):
                existing_snapshot_ids.add(r["product_id"])
        if len(_rows) < 1000:
            break
        _snap_offset += 1000
    print(f"  {len(existing_snapshot_ids)} snapshots already exist for today.")

    # v14.18: empty-page retry guard. Shopify's storefront /products.json
    # endpoint occasionally returns a transient empty page mid-catalog, which
    # made the old `if not products: break` conclude the catalog had ended.
    # We now retry the same page once before trusting the empty result.
    # Pagination stays on ?page=N — since_id is an Admin API parameter that
    # the public storefront endpoint silently ignores (causes infinite loop).
    empty_confirm = False
    while True:
        url = f"https://{domain}/products.json?limit=250&page={page}"
        try:
            response = execute_with_retry(session.get, url, timeout=30,
                                          headers={"User-Agent": "Mozilla/5.0"})
        except Exception as e:
            print(f"  ⚠️ HTTP fault on page {page}: {e}")
            break
        if response.status_code != 200: break
        products = response.json().get("products", [])
        if not products:
            # v14.18: a single empty page might be transient — retry once
            # before concluding the catalog has ended.
            if not empty_confirm:
                empty_confirm = True
                time.sleep(3)
                continue   # retry same page number
            break
        empty_confirm = False

        batch_products = []
        for p in products:
            if not p.get("variants"): continue
            safe_image = p["images"][0]["src"] if p.get("images") else None
            # v14.22 FIX 2: Carina is women-only. Skip the generic keyword scan
            # entirely for brands in FEMALE_ONLY_BRANDS — gender is fixed to
            # "women" unconditionally, so a catalog with no gender keywords in
            # its titles/tags (Carina's actual situation) can never fall
            # through to "unisex". No other brand's gender detection changes.
            if brand_name in FEMALE_ONLY_BRANDS:
                resolved_gender = "women"
            else:
                resolved_gender = normalize_gender(p.get("tags", []), p.get("product_type", ""), p["title"])
            cat_norm = normalize_category(f"{p['title']} {p.get('product_type','')}")
            batch_products.append({
                "brand":               brand_name,
                "external_id":         str(p["id"]),
                "name":                p["title"],
                "category_raw":        p.get("product_type", ""),
                "category_normalized": cat_norm,
                "gender":              resolved_gender,
                "sizes_available":     [],
                "url":                 f"https://{domain}/products/{p['handle']}",
                "image_url":           safe_image,
                "last_seen_at":        datetime.now(timezone.utc).isoformat(),
                "is_active":           True,
                # v14.29: brand's real launch date — Shopify's published_at
                # is the date the product went live on the storefront, often
                # predating our scraper by months/years. Captured for free
                # (already in the /products.json response we download).
                "source_published_at": p.get("published_at"),
                # v14.24+v14.25: sub-category detail (sleeve length/fit for tops
                # and bottoms; dress length/silhouette, jacket type, underwear
                # style, swimwear style, bag type for their respective
                # categories) — see build_attributes_extracted for the full list.
                "attributes_extracted": build_attributes_extracted(
                    brand_name, cat_norm, p.get("product_type", ""), p["title"]
                ),
            })
        if not batch_products:
            break

        product_upsert_rows = []
        for i in range(0, len(batch_products), 100):
            res = safe_db_execute(
                supabase.table("products").upsert(batch_products[i:i+100], on_conflict="brand,external_id")
            )
            if res and res.data: product_upsert_rows.extend(res.data)
        product_id_map = {row["external_id"]: row["id"] for row in product_upsert_rows}
        products_seen += len(batch_products)

        for p in products:
            db_pid = product_id_map.get(str(p["id"]))
            if not db_pid: continue
            # v14.17: skip if this product already has a baseline (membership in
            # fop_done_ids, now passed in). The DB still has the IS NULL guard as
            # a belt-and-braces safety net, but cutting the call is the saving.
            if db_pid in fop_done_ids: continue
            first_variant_price = None
            for v in p.get("variants", []):
                pr = float(v.get("price") or 0)
                if pr > 0:
                    first_variant_price = pr
                    break
            if first_variant_price:
                safe_db_execute(
                    supabase.table("products")
                    .update({"first_observed_price": first_variant_price})
                    .eq("id", db_pid)
                    .is_("first_observed_price", "null")
                )
                fop_done_ids.add(db_pid)

        batch_variants, product_variant_tracking = [], {}
        for p in products:
            db_pid = product_id_map.get(str(p["id"]))
            if not db_pid: continue
            size_key, color_key = detect_options(p["variants"])
            product_variant_tracking[db_pid] = []
            for v in p["variants"]:
                size  = (v.get(size_key) or "").strip() or None
                color = (v.get(color_key) or "").strip() if color_key else None
                if size and size.lower() == "default title": size = None
                # v14.15: 2S Egypt (and any future Arabic-catalog brand) stores
                # colours in Arabic — normalise to English so cross-brand colour
                # queries work. No-op for every English-catalog brand.
                if color and brand_name in ARABIC_COLOR_BRANDS:
                    color = normalize_arabic_color(color)
                # v14.19: Tree and Cizaro publish each colourway as a SEPARATE
                # Shopify product rather than a variant option, so color_key
                # is correctly None for these brands (there's no colour option
                # to find) — the colour lives only in the product title. This
                # ONLY fires when no variant-level colour was found, and ONLY
                # for brands in COLOR_FROM_TITLE_BRANDS, so it can never
                # override a real Shopify-variant colour and never touches
                # any other brand's data.
                if not color and brand_name in COLOR_FROM_TITLE_BRANDS:
                    color = extract_color_from_title(brand_name, p.get("title", ""))
                price      = float(v.get("price") or 0)
                compare_at = float(v.get("compare_at_price") or 0) if v.get("compare_at_price") else None
                available  = bool(v.get("available"))
                if price == 0: continue
                sku        = f"{domain}_{v['id']}"
                prev       = prev_stock_state.get(sku)
                v_baseline = float(prev["first_observed_price"]) if (prev and prev.get("first_observed_price")) else price
                batch_variants.append({
                    "product_id": db_pid, "external_sku": sku, "color": color, "size": size,
                    "is_in_stock": available, "first_observed_price": v_baseline,
                    "last_updated_at": datetime.now(timezone.utc).isoformat(),
                    "_meta_price": price, "_meta_compare": compare_at, "_meta_baseline": v_baseline,
                    "_meta_size": size, "_meta_color": color, "_meta_available": available,
                })


        if batch_variants:
            db_payload = [{k: v for k, v in row.items() if not k.startswith("_meta_")} for row in batch_variants]
            variant_upsert_rows = []
            for i in range(0, len(db_payload), 100):
                res = safe_db_execute(
                    supabase.table("product_variants").upsert(db_payload[i:i+100], on_conflict="external_sku")
                )
                if res and res.data: variant_upsert_rows.extend(res.data)
            sku_to_id = {row["external_sku"]: row["id"] for row in variant_upsert_rows}
            for vr in batch_variants:
                vr["variant_db_id"] = sku_to_id.get(vr["external_sku"])
                product_variant_tracking[vr["product_id"]].append(vr)

            snap_rows = build_snapshot_rows(brand_name, product_variant_tracking, today, existing_snapshot_ids)
            if snap_rows:
                safe_db_execute(supabase.table("price_snapshots").insert(snap_rows))

            for db_pid, records in product_variant_tracking.items():
                if not records: continue
                sizes_in_stock = [r["_meta_size"] for r in records if r["_meta_available"] and r["_meta_size"]]
                # Per-variant stockout detection (correct at the variant grain).
                for rec in records:
                    prev_v = prev_stock_state.get(rec["external_sku"])
                    if prev_v:
                        detect_and_write_stockout(
                            supabase, rec["variant_db_id"], db_pid, brand_name,
                            rec["_meta_size"], rec["_meta_color"],
                            prev_v["is_in_stock"], rec["_meta_available"],
                            rec["_meta_price"], rec["_meta_baseline"]
                        )
                # Product-level price detection (v14.8): one representative price per
                # product, compared once. This is what build_snapshot_rows stores, so
                # an unchanged product compares equal and produces no event — ending
                # the per-variant flip-flop that manufactured phantom up/down churn.
                curr_price = product_repr_price(records)
                v_base     = product_repr_baseline(records)
                if curr_price is None:
                    continue
                last_p = prev_prices.get(db_pid)
                if last_p is None:
                    prev_prices[db_pid] = curr_price
                elif abs(last_p - curr_price) > 0.01:
                    direction = "down" if curr_price < last_p else "up"
                    price_changes += 1
                    if direction == "down" and v_base and curr_price < v_base:
                        prod = next((p for p in products
                                     if str(p["id"]) == next((k for k, v in product_id_map.items() if v == db_pid), None)), None)
                        if prod:
                            p_cat = normalize_category(f"{prod['title']} {prod.get('product_type','')}")
                            p_url = f"https://{domain}/products/{prod['handle']}"
                            for sz in set(sizes_in_stock):
                                find_and_alert_users(
                                    supabase, session, brand_name, p_cat,
                                    sz, curr_price, prod["title"], p_url, v_base
                                )
                    honest_disc = round(((v_base - curr_price) / v_base) * 100, 2) if (v_base and curr_price < v_base) else None
                    safe_db_execute(supabase.table("price_events").insert({
                        "product_id":    db_pid, "brand": brand_name,
                        "price_before":  last_p, "price_after": curr_price,
                        "compare_at_price": records[0].get("_meta_compare"),
                        "discount_pct":  honest_disc,
                        "direction":     direction, "sizes_in_stock": sizes_in_stock,
                        "recorded_at":   datetime.now(timezone.utc).isoformat(),
                    }))
                    prev_prices[db_pid] = curr_price
                    sync_snapshot_price(supabase, db_pid, today, curr_price)

        print(f"  Page {page} — {len(batch_products)} products processed.")
        time.sleep(1)
        page += 1
    return products_seen, price_changes

# ── LC Waikiki Scraper ────────────────────────────────────────────────────────

LCW_CATEGORIES = [
    {
        "id": 9, "name": "Men", "gender": "men",
        "params": [
            {"PropertyId": 67, "PropertyValueId": [10]},
            {"PropertyId": 63, "PropertyValueId": [57794]},
        ],
    },
    {
        "id": 1, "name": "Women", "gender": "women",
        "params": [
            {"PropertyId": 67, "PropertyValueId": [8]},
        ],
    },
]

LCW_BREADCRUMB_GENDER_MAP = {
    "men": "men", "man": "men", "رجال": "men", "رجالي": "men",
    "women": "women", "woman": "women", "نساء": "women", "نسائي": "women",
    "kids": "kids", "children": "kids", "أطفال": "kids",
}

# LCW colour-swatch image filenames are the Turkish colour name (e.g. /siyah.png),
# which is what we end up storing in product_variants.color. This translation map
# normalises them to the English names every other brand uses. The set is finite
# (LCW publishes a fixed palette) so a static dict is cheaper and more reliable
# than a translation API — no rate limits, no cost, no surprises.
LCW_COLOR_TR_EN = {
    "siyah": "black", "beyaz": "white", "gri": "grey", "antrasit": "anthracite",
    "kirmizi": "red", "bordo": "burgundy", "pembe": "pink", "fusya": "fuchsia",
    "mercan": "coral", "turuncu": "orange", "sari": "yellow", "ekru": "ecru",
    "bej": "beige", "kahve": "brown", "haki": "khaki", "yesil": "green",
    "petrol": "petrol", "turkuaz": "turquoise", "mavi": "blue", "lacivert": "navy",
    "indigo": "indigo", "mor": "purple", "murdum": "plum", "lila": "lilac",
    "cokrenkli": "multicolor",
}

def normalize_lcw_color(raw):
    """Lowercase + strip + translate Turkish → English when known. Leaves
    unknown values untouched so we never silently drop a colour we don't
    recognise — those will surface naturally and can be added to the dict."""
    if not raw:
        return None
    key = str(raw).strip().lower()
    return LCW_COLOR_TR_EN.get(key, key)

def lcw_normalize_category(breadcrumb):
    for level in ["Level3", "Level4", "Level2"]:
        raw = (breadcrumb.get(level) or "").lower().strip()
        if not raw: continue
        for category, keywords in CATEGORY_MAP.items():
            if any(kw in raw for kw in keywords):
                return category
    return "uncategorized"

def lcw_normalize_gender(breadcrumb, fallback_gender):
    level1 = (breadcrumb.get("Level1") or "").lower().strip()
    return LCW_BREADCRUMB_GENDER_MAP.get(level1, fallback_gender)

def parse_lcw_page_data(html):
    """
    Extract structured variant data from an LCW product page.

    Confirmed from page source inspection (9 Jun 2026):
    The page embeds two JSON blobs as JavaScript variables:
      var cartOperationViewModel = {...};    ← per-color: sizes + stock + price
      var optimizedDetailModel   = {...};    ← model-wide: all colors (Options[])

    Returns:
      {
        "current_option_id": int,         # OptionId this page belongs to
        "sizes": [                        # all sizes for THIS color
          {"size": "M", "stock": 4, "price": 1499.0, "size_id": 13542}, ...
        ],
        "option_stock": int,              # total stock for THIS color
        "color_name": str,                # English color name
        "color_code": str,                # ColorCode (e.g. "R9J")
        "category_tree": dict,            # breadcrumb levels
        "all_options": [                  # every color of the same model
          {"option_id": int, "color": str, "in_stock": bool, "url": str}, ...
        ],
      }

    Returns None if either JSON blob is missing or unparseable. The caller
    handles None by skipping the row — we never partially populate.
    """
    # Grab cartOperationViewModel — contains ProductSizes[] with per-size stock
    cart_m = re.search(r'var\s+cartOperationViewModel\s*=\s*(\{.*?\});\s*$',
                       html, re.MULTILINE | re.DOTALL)
    if not cart_m:
        # Fallback: try without trailing semicolon anchor
        cart_m = re.search(r'var\s+cartOperationViewModel\s*=\s*(\{.+?\});',
                           html, re.DOTALL)
    detail_m = re.search(r'var\s+optimizedDetailModel\s*=\s*(\{.+?\});',
                         html, re.DOTALL)
    if not cart_m or not detail_m:
        return None

    try:
        cart   = json.loads(cart_m.group(1))
        detail = json.loads(detail_m.group(1))
    except (json.JSONDecodeError, ValueError) as e:
        # JSON malformed — most likely a non-greedy regex caught too little
        return None

    sizes = []
    for ps in (cart.get("ProductSizes") or []):
        sz_obj = ps.get("Size") or {}
        price_obj = ps.get("Price") or {}
        size_val = sz_obj.get("Value")
        if not size_val:
            continue
        sizes.append({
            "size":     str(size_val).strip(),
            "stock":    int(ps.get("Stock") or 0),
            "price":    float(price_obj.get("Price") or 0),
            "size_id":  sz_obj.get("SizeId"),
        })

    # Walk Options[] inside optimizedDetailModel.ModelInfo to enumerate colors
    all_options = []
    model_info = (detail.get("ModelInfo") or {})
    for opt in (model_info.get("Options") or []):
        opt_id = opt.get("OptionId")
        if not opt_id:
            continue
        all_options.append({
            "option_id": int(opt_id),
            "color":     opt.get("MainColorName") or opt.get("Title") or "",
            "color_code": opt.get("ColorCode"),
            "in_stock":  bool(opt.get("IsStockAvailable")),
            "url":       opt.get("Url") or "",
        })

    option_obj = (detail.get("Option") or {})
    category_tree = option_obj.get("MainCategoryTree") or {}
    # Flatten the tree for compatibility with lcw_normalize_category which
    # expects {"Level1": "...", "Level2": "...", ...} string values.
    cat_flat = {}
    for k, v in category_tree.items():
        if isinstance(v, dict):
            cat_flat[k] = v.get("LevelValue") or ""
        elif isinstance(v, list) and v:
            cat_flat[k] = (v[0] or {}).get("LevelValue") or ""

    return {
        "current_option_id": int(cart.get("OptionId") or 0),
        "sizes":             sizes,
        "option_stock":      int(cart.get("OptionStock") or 0),
        "color_name":        cart.get("Color") or option_obj.get("MainColorName") or "",
        "color_code":        option_obj.get("ColorCode") or "",
        "category_tree":     cat_flat,
        "all_options":       all_options,
    }

def fetch_lcw_product_page(session, url):
    """
    GET an LCW product page and parse its embedded JSON.
    Server returns gzip-compressed HTML (~60 KB on wire / ~350 KB decoded).
    Returns the dict from parse_lcw_page_data() or None.
    """
    try:
        res = execute_with_retry(session.get, url, max_retries=1, backoff=0,
                                 timeout=10, headers={
            "accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "accept-encoding": "gzip, deflate, br",
        })
        if res.status_code == 200:
            return parse_lcw_page_data(res.text)
        print(f"  ⚠️ Size page HTTP {res.status_code}: {url}")
    except Exception as e:
        print(f"  ⚠️ Size fetch error: {e}")
    return None

def lcw_fetch_page(session, domain, category_id, page_index, headers,
                   seen_ids=None, category_params=None):
    url = (
        f"https://{domain}/en/ajax/ProductList/ProductListPageData"
        f"?xhrKeys=CategoryTreeId,xhrKeys"
        f"&CategoryTreeId={category_id}"
        f"&PageIndex={page_index}"
        f"&Layout=three-column"
    )
    body = {
        "CategoryParameterList":  category_params or [],
        "FilterListJson":         "[]",
        "LastSeenOptionIdsJson":  json.dumps(seen_ids or []),
    }
    try:
        res = execute_with_retry(session.post, url, json=body, timeout=30, headers=headers)
        if res.status_code not in [200, 404]:
            print(f"  ⚠️ LCW API unexpected HTTP {res.status_code} (cat={category_id}, page={page_index})")
            return None
        try:
            return res.json()
        except Exception:
            print(f"  ⚠️ LCW HTTP {res.status_code} but body is not JSON")
            return None
    except Exception as e:
        print(f"  ⚠️ LCW network fault (cat={category_id}, page={page_index}): {e}")
        return None

def _parse_lcw_price(v):
    if not v: return 0.0
    if isinstance(v, (int, float)): return float(v)
    s = "".join(c for c in str(v) if c.isdigit() or c == ".")
    return float(s) if s else 0.0

def scrape_lcw(supabase, session, brand_name, domain, today, prev_stock_state, fop_done_ids):
    print("  Executing LC Waikiki Catalog Engine (API mode)...")
    print(f"  [LCW] Proxy configured: {WEBSHARE_PROXY is not None}")
    products_seen, price_changes = 0, 0

    prev_prices = load_last_prices(supabase, brand_name)
    # v14.17: fop_done_ids is passed in from scrape_brand (derived from the
    # variant preload). LCW carried the biggest gain from gating these no-op
    # writes — ~5,000 products once daily — and now it doesn't even need the
    # separate full-products read to do it.
    existing_snapshot_ids = set()
    _snap_offset = 0
    while True:
        _snap = safe_db_execute(
            supabase.table("price_snapshots").select("product_id")
            .eq("brand", brand_name).eq("snapshot_date", str(today))
            .range(_snap_offset, _snap_offset + 999)
        )
        _rows = (_snap.data or []) if _snap else []
        for r in _rows:
            if r.get("product_id"):
                existing_snapshot_ids.add(r["product_id"])
        if len(_rows) < 1000:
            break
        _snap_offset += 1000
    print(f"  [LCW] {len(existing_snapshot_ids)} snapshots already exist for today.")

    headers = {
        "accept":          "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "origin":          f"https://{domain}",
        "referer":         f"https://{domain}/en/men-clothing-t-9",
        "sec-fetch-dest":  "empty",
        "sec-fetch-mode":  "cors",
        "sec-fetch-site":  "same-origin",
        "priority":        "u=1, i",
    }

    try:
        prime_url = f"https://{domain}/en/men-clothing-t-9"
        prime_headers = {
            "accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "accept-encoding": "gzip, deflate, br",
            "cache-control":   "no-cache",
            "pragma":          "no-cache",
        }
        prime_res = execute_with_retry(
            session.get, prime_url, max_retries=2, backoff=3,
            timeout=20, headers=prime_headers
        )
        print(f"  [LCW] Session primed — HTTP {prime_res.status_code} "
              f"({len(prime_res.content) / 1024:.0f} KB)")
        time.sleep(random.uniform(2, 4))
    except Exception as e:
        print(f"  [LCW] Priming failed (will attempt API anyway): {e}")

    # v14.9: accumulate every colour of a model ACROSS all pages/categories, then
    # snapshot + detect ONCE after the crawl. LCW paginates by OptionId (colour),
    # so a single model's colours can land on different pages; per-page detection
    # then saw partial variant sets and manufactured symmetric phantom down/up
    # round-trips (e.g. 1099->599 on one page, 599->1099 on another) that also
    # polluted the flash-sale signal. One detection pass over the full colour set
    # gives a stable median and kills the artifact.
    lcw_model_records = {}   # db_pid -> [variant _meta records across the whole run]
    lcw_model_info    = {}   # db_pid -> {desc, category, url} for alerts

    for cat in LCW_CATEGORIES:
        cat_id, cat_name, cat_gender = cat["id"], cat["name"], cat["gender"]
        cat_params = cat.get("params", [])
        print(f"  [{cat_name}] Fetching page 1 to get total page count...")
        seen_ids   = []
        first_data = lcw_fetch_page(session, domain, cat_id, 1, headers,
                                     seen_ids=[], category_params=cat_params)
        if not first_data:
            print(f"  ⚠️ [{cat_name}] Could not reach LCW API. Skipping.")
            continue

        catalog_meta = first_data.get("CatalogList") or {}
        total_items  = catalog_meta.get("ItemCount", 0)
        page_count   = catalog_meta.get("PageCount", 1)
        print(f"  [{cat_name}] {total_items} products across {page_count} pages.")

        for page_idx in range(1, page_count + 1):
            data = first_data if page_idx == 1 else None
            if page_idx > 1:
                time.sleep(random.uniform(1.2, 2.0))
                data = lcw_fetch_page(session, domain, cat_id, page_idx, headers,
                                       seen_ids=seen_ids, category_params=cat_params)
                if not data:
                    print(f"  ⚠️ [{cat_name}] Page {page_idx} failed. Skipping.")
                    continue

            items = (data.get("CatalogList") or {}).get("Items") or []
            if not items:
                print(f"  ⚠️ [{cat_name}] Page {page_idx} returned 0 items.")
                break

            for _item in items:
                _opt = _item.get("OptionId")
                if _opt and _opt not in seen_ids:
                    seen_ids.append(_opt)

            batch_products, seen_ext_ids = [], set()
            for item in items:
                model_id = item.get("ModelId")
                if not model_id or str(model_id) in seen_ext_ids: continue
                seen_ext_ids.add(str(model_id))
                name       = (item.get("ProductDescription") or item.get("BrandPropertyDescription")
                              or item.get("Name") or f"LCW-{model_id}")
                breadcrumb = item.get("BreadCrump") or {}
                category   = lcw_normalize_category(breadcrumb)
                if category == "uncategorized" and name:
                    category = lcw_normalize_category({"Level3": name})
                gender     = lcw_normalize_gender(breadcrumb, cat_gender)
                model_url  = item.get("ModelUrl") or ""
                url        = f"https://{domain}{model_url}" if model_url.startswith("/") else model_url
                cat_raw    = breadcrumb.get("Level3") or breadcrumb.get("Level2") or ""
                batch_products.append({
                    "brand": brand_name, "external_id": str(model_id), "name": name,
                    "category_raw":        cat_raw,
                    "category_normalized": category, "gender": gender,
                    "sizes_available": [], "url": url,
                    "image_url":   item.get("DefaultOptionImageUrl"),
                    "last_seen_at": datetime.now(timezone.utc).isoformat(), "is_active": True,
                    # v14.24: LCW's strongest signal for both sleeve length and
                    # fit/cut is the title — confirmed via live coverage check,
                    # so this brand uses build_attributes_extracted's default
                    # (title-first) priority order. Also picks up any v14.25
                    # dress/jacket/underwear/swimwear/bag attributes if this
                    # product's category matches one of those groups.
                    "attributes_extracted": build_attributes_extracted(
                        brand_name, category, cat_raw, name
                    ),
                })

            if not batch_products: continue

            product_upsert_rows = []
            for i in range(0, len(batch_products), 100):
                res_p = safe_db_execute(
                    supabase.table("products").upsert(batch_products[i:i+100], on_conflict="brand,external_id")
                )
                if res_p and res_p.data: product_upsert_rows.extend(res_p.data)
            product_id_map  = {row["external_id"]: row["id"] for row in product_upsert_rows}
            products_seen  += len(batch_products)

            for item in items:
                db_pid = product_id_map.get(str(item.get("ModelId")))
                if not db_pid: continue
                # v14.17: skip already-seeded products — fop_done_ids is passed
                # in; the DB still enforces the IS NULL guard, but the network
                # call no longer happens.
                if db_pid in fop_done_ids: continue
                is_disc  = bool(item.get("Discounted") or item.get("CurrentPricesAreDiscounted"))
                disc_val = _parse_lcw_price(item.get("DiscountedPriceValue"))
                full_val = _parse_lcw_price(item.get("PriceValue") or item.get("Price"))
                fop = disc_val if (is_disc and disc_val > 0) else full_val
                if fop > 0:
                    safe_db_execute(
                        supabase.table("products")
                        .update({"first_observed_price": fop})
                        .eq("id", db_pid)
                        .is_("first_observed_price", "null")
                    )
                    fop_done_ids.add(db_pid)

            batch_variants, product_variant_tracking = [], {}
            for item in items:
                model_id = item.get("ModelId")
                db_pid   = product_id_map.get(str(model_id))
                if not db_pid: continue
                product_variant_tracking.setdefault(db_pid, [])
                opt_id = item.get("OptionId")

                is_discounted  = bool(item.get("Discounted") or item.get("CurrentPricesAreDiscounted"))
                discounted_val = _parse_lcw_price(item.get("DiscountedPriceValue"))
                full_val       = _parse_lcw_price(item.get("PriceValue") or item.get("Price"))
                old_val        = _parse_lcw_price(item.get("MinOldPrice"))

                if is_discounted and discounted_val > 0:
                    price      = discounted_val
                    compare_at = (old_val or full_val) if (old_val or full_val) > discounted_val else None
                else:
                    price, compare_at = full_val, None
                if price == 0: continue

                is_avail   = int(item.get("AvailableStock") or 0) > 0
                sku        = f"lcw_{opt_id}"

                color_img_url = item.get("ColorImageUrl") or ""
                color_name = None
                if color_img_url:
                    m = re.search(r'/([^/]+)\.(png|jpg|jpeg|webp)$', color_img_url, re.IGNORECASE)
                    if m:
                        color_name = m.group(1).lower()
                if not color_name:
                    color_name = item.get("MainColorHexCode") or None
                # Translate the Turkish swatch filename into the English colour name
                # that every other brand uses, so cross-brand colour queries (e.g.
                # "what % of men's black bottoms got discounted") work without
                # per-brand special-casing downstream.
                color_name = normalize_lcw_color(color_name)

                prev       = prev_stock_state.get(sku)
                v_baseline = float(prev["first_observed_price"]) if (prev and prev.get("first_observed_price")) else price

                batch_variants.append({
                    "product_id": db_pid, "external_sku": sku, "color": color_name,
                    # v14.18: "size" intentionally EXCLUDED from the upsert payload.
                    # LCW's catalog API doesn't include per-variant sizes — those come
                    # from product-page JSON (the backfill pass). Including size=None
                    # here made the upsert overwrite sizes populated by previous runs,
                    # forcing the backfill to redo work every run. Omitting the key
                    # means ON CONFLICT SET skips the column, preserving existing data.
                    #
                    # is_in_stock HERE is the COLOR-WIDE aggregate (AvailableStock > 0
                    # across every size of this color) — this field's ONLY writer for
                    # this row going forward (v14.22 FIX 1). The size-backfill pass
                    # below populates "size" on size-suffixed CHILD rows and no longer
                    # touches is_in_stock on THIS parent row at all.
                    "is_in_stock": is_avail, "first_observed_price": v_baseline,
                    "last_updated_at": datetime.now(timezone.utc).isoformat(),
                    "_meta_price": price, "_meta_compare": compare_at,
                    "_meta_baseline": v_baseline, "_meta_size": None,
                    "_meta_color": color_name, "_meta_available": is_avail,
                })

            if batch_variants:
                db_payload = [{k: v for k, v in r.items() if not k.startswith("_meta_")} for r in batch_variants]
                variant_upsert_rows = []
                for i in range(0, len(db_payload), 100):
                    res_v = safe_db_execute(
                        supabase.table("product_variants").upsert(db_payload[i:i+100], on_conflict="external_sku")
                    )
                    if res_v and res_v.data: variant_upsert_rows.extend(res_v.data)
                sku_to_id = {row["external_sku"]: row["id"] for row in variant_upsert_rows}
                for vr in batch_variants:
                    vr["variant_db_id"] = sku_to_id.get(vr["external_sku"])
                    product_variant_tracking[vr["product_id"]].append(vr)

                # Per-variant (per-colour) stockout detection — correct at this grain,
                # safe to run per page (each OptionId appears on exactly one page).
                for db_pid, records in product_variant_tracking.items():
                    for rec in records:
                        prev_v = prev_stock_state.get(rec["external_sku"])
                        if prev_v:
                            detect_and_write_stockout(
                                supabase, rec["variant_db_id"], db_pid, brand_name,
                                rec["_meta_size"], None,
                                prev_v["is_in_stock"], rec["_meta_available"],
                                rec["_meta_price"], rec["_meta_baseline"]
                            )
                    # Accumulate this page's colours into the run-level model record
                    # set; price detection happens once, after the full crawl.
                    lcw_model_records.setdefault(db_pid, []).extend(records)
                    if db_pid not in lcw_model_info:
                        it = next((item for item in items
                                   if str(item.get("ModelId")) == next((k for k, v in product_id_map.items() if v == db_pid), None)), None)
                        if it:
                            lcw_model_info[db_pid] = {
                                "desc":     it.get("ProductDescription") or it.get("BrandPropertyDescription") or "LCW Item",
                                "category": lcw_normalize_category(it.get("BreadCrump") or {}),
                                "url":      f"https://{domain}{it.get('ModelUrl') or ''}",
                            }

            print(f"  [{cat_name}] Page {page_idx}/{page_count} — {len(batch_products)} products processed.")

    # ── Single detection pass over the FULL colour set per model (v14.9) ──────
    # Now that every page/category has been crawled, lcw_model_records holds all
    # colours of each model. We snapshot once and detect once per model using a
    # stable median, so partial-page views can no longer manufacture phantom
    # up/down round-trips.
    snap_rows = build_snapshot_rows(brand_name, lcw_model_records, today, existing_snapshot_ids)
    if snap_rows:
        for i in range(0, len(snap_rows), 100):
            safe_db_execute(supabase.table("price_snapshots").insert(snap_rows[i:i+100]))

    for db_pid, records in lcw_model_records.items():
        if not records:
            continue
        curr_price = product_repr_price(records)
        v_base     = product_repr_baseline(records)
        if curr_price is None:
            continue
        last_p = prev_prices.get(db_pid)
        if last_p is None:
            prev_prices[db_pid] = curr_price
            continue
        if abs(last_p - curr_price) <= 0.01:
            continue
        direction = "down" if curr_price < last_p else "up"
        price_changes += 1
        sizes_in_stock = [r["_meta_size"] for r in records if r["_meta_available"] and r["_meta_size"]]
        if direction == "down" and v_base and curr_price < v_base:
            info = lcw_model_info.get(db_pid) or {}
            for sz in set(sizes_in_stock):
                find_and_alert_users(
                    supabase, session, brand_name, info.get("category", "uncategorized"),
                    sz, curr_price, info.get("desc", "LCW Item"),
                    info.get("url", f"https://{domain}"), v_base
                )
        honest_disc = round(((v_base - curr_price) / v_base) * 100, 2) if (v_base and curr_price < v_base) else None
        safe_db_execute(supabase.table("price_events").insert({
            "product_id":       db_pid, "brand": brand_name,
            "price_before":     last_p, "price_after": curr_price,
            "compare_at_price": records[0].get("_meta_compare"),
            "discount_pct":     honest_disc,
            "direction":        direction,
            "sizes_in_stock":   sizes_in_stock,
            "recorded_at":      datetime.now(timezone.utc).isoformat(),
        }))
        prev_prices[db_pid] = curr_price
        sync_snapshot_price(supabase, db_pid, today, curr_price)

    # ── LCW size enrichment pass (rewritten v14.6) ────────────────────────────
    # Bandwidth math:
    #   - Pages return ~60 KB gzipped (server gzip enabled, verified 9 Jun 2026)
    #   - Catalog scrape uses ~870 MB/month — leaves ~130 MB/month headroom
    #   - SIZE_CAP=65 pages/day × 60 KB × 30 days = ~117 MB/month — fits 1 GB
    #
    # Throughput math:
    #   - Each fetch returns ALL sizes for the requested color (5-7 sizes typical)
    #     PLUS the list of every OTHER color of the same model (Options[])
    #   - We dedupe by URL stem (strip "-o-{optionId}" suffix) → fetch ONCE per model
    #   - One fetch populates one color's sizes immediately, and tells us the
    #     OptionId of every sibling color so future fetches know to skip them
    #   - 5,574 unique models in catalog ÷ 65/day = ~86 days to fully backfill
    #
    # Bot-safety:
    #   - 0.6-1.4s random sleep between fetches (≈40-65/min sustained)
    #   - All traffic goes through Egyptian residential proxy session
    #   - Same headers a real browser sends, no robotic patterns
    #
    # v14.22 FIX 1: this pass used to ALSO write is_in_stock onto the PARENT
    # (color-level, lcw_{opt_id}) row inside the `if i == 0` branch below,
    # using just the FIRST size's stock state. That collided with the catalog
    # pass above, which is the correct, color-wide source for that same field
    # (AvailableStock > 0 across every size). Whichever pass ran more recently
    # "won", and since the sizes[] array's order isn't guaranteed stable run to
    # run, the parent row could flip is_in_stock back and forth with NOTHING
    # about the product actually changing — confirmed live: several LCW SKUs
    # showed 3-5 consecutive "restock" events with ZERO "stockout" events
    # between them, which is only possible if a false transition was being
    # manufactured. The size pass no longer writes is_in_stock to the parent
    # row at all — see the full diagnosis in the v14.22 changelog at the top
    # of this file. Per-size stock is UNCHANGED and still correct: it's written
    # onto each size's own child row (the `else` branch a few lines down),
    # which was never part of this bug (each size has its own external_sku, so
    # those rows were never the parent row being double-written).
    # SIZE_CAP: how many LCW product pages to fetch for sizes per run.
    # Controlled by the LCW_SIZE_CAP repository variable so it can be raised
    # (to clear a backlog) or lowered (to conserve proxy bandwidth) WITHOUT
    # editing this file. Defaults to 65 if the variable isn't set.
    SIZE_CAP     = int(os.environ.get("LCW_SIZE_CAP", "65"))
    SIZE_TIMEOUT = 600   # 10 minutes hard ceiling — far less than the 180-min workflow limit
    print(f"  [LCW] Fetching sizes for variants missing data (cap: {SIZE_CAP}/run)...")

    try:
        # Pull all null-size LCW variant rows in one paginated sweep
        all_missing, miss_offset = [], 0
        while True:
            chunk = safe_db_execute(
                supabase.table("product_variants")
                .select("id, product_id, external_sku, color, is_in_stock, first_observed_price, products!inner(url, brand)")
                .eq("products.brand", "lc_waikiki")
                .is_("size", "null")
                .range(miss_offset, miss_offset + 999)
            )
            rows = (chunk.data or []) if chunk else []
            all_missing.extend(rows)
            if len(rows) < 1000:
                break
            miss_offset += 1000

        if not all_missing:
            print("  [LCW] All variants have size data. ✅")
            return products_seen, price_changes

        print(f"  [LCW] {len(all_missing)} variants need sizes across {len(set((r.get('products') or {}).get('url') or '' for r in all_missing))} URLs.")

        # Group rows by URL — each URL = one color = one variant row in our DB.
        # Multiple variant rows may map to the same product URL (defensive).
        url_to_rows = {}
        for row in all_missing:
            url = (row.get("products") or {}).get("url")
            if not url:
                continue
            url_to_rows.setdefault(url, []).append(row)

        # Process up to SIZE_CAP unique URLs
        urls_to_fetch = list(url_to_rows.keys())[:SIZE_CAP]
        fetched = populated_rows = 0
        size_pass_start = time.time()

        for url in urls_to_fetch:
            if time.time() - size_pass_start > SIZE_TIMEOUT:
                print(f"  [LCW] Size pass time limit reached — stopping early.")
                break

            page_data = fetch_lcw_product_page(session, url)
            fetched += 1

            if not page_data or not page_data.get("sizes"):
                time.sleep(random.uniform(0.6, 1.4))
                continue

            sizes      = page_data["sizes"]
            now_iso    = datetime.now(timezone.utc).isoformat()
            # Rescue the colour from the embedded JSON when the catalog couldn't
            # extract one (no ColorImageUrl and no MainColorHexCode → NULL parent).
            # cartOperationViewModel.Color is the per-OptionId colour name; it's
            # also Turkish, so we run it through the same translator.
            page_color = normalize_lcw_color(page_data.get("color_name"))

            # For EACH variant row sharing this URL (typically just one):
            # update first size on the existing row, insert new rows for remaining sizes.
            for row in url_to_rows[url]:
                product_id   = row.get("product_id")
                # If the parent row had no colour, use what the product page told us.
                # If both are missing, leave it NULL (better than guessing).
                color        = row.get("color") or page_color
                fop          = row.get("first_observed_price") or prev_prices.get(product_id)
                # v14.22 FIX 1: parent_stock is STILL read here (used as the
                # fallback for a size whose own stock count is missing — see
                # size_in_stock below), but it is NEVER written back onto the
                # parent row anymore. It only ever flows into CHILD rows now.
                parent_stock = row.get("is_in_stock", True)

                for i, sz in enumerate(sizes):
                    # Use the API's per-size stock when available; fall back to parent_stock
                    size_in_stock = (sz.get("stock", 0) > 0) if sz.get("stock") is not None else parent_stock
                    size_value    = sz["size"]

                    if i == 0:
                        # v14.22 FIX 1: is_in_stock REMOVED from this payload.
                        # This branch updates the PARENT row (row["id"], the
                        # original lcw_{opt_id} color-level row) to attach the
                        # first size's name — but it must NEVER write that
                        # size's stock state onto the parent's is_in_stock,
                        # because the parent's is_in_stock means "is ANY size
                        # of this color available", a color-wide aggregate
                        # that only the catalog pass's AvailableStock read is
                        # entitled to set. Writing a single size's stock here
                        # is what manufactured the phantom restock/stockout
                        # flips — see the v14.22 changelog at the top of this
                        # file for the full live-data diagnosis.
                        update_payload = {
                            "size":            size_value,
                            "last_updated_at": now_iso,
                        }
                        if not row.get("color") and color:
                            update_payload["color"] = color
                        safe_db_execute(
                            supabase.table("product_variants")
                            .update(update_payload)
                            .eq("id", row["id"])
                        )
                    else:
                        # Child rows (one per additional size) are UNAFFECTED by
                        # FIX 1 — each has its OWN external_sku, so this is_in_stock
                        # write has always belonged to this row alone, never shared
                        # with the parent. This is the correct, real per-size signal.
                        new_sku = f"{row['external_sku']}_{size_value.replace(' ', '_')}"
                        safe_db_execute(
                            supabase.table("product_variants").upsert({
                                "product_id":           product_id,
                                "external_sku":         new_sku,
                                "color":                color,
                                "size":                 size_value,
                                "is_in_stock":          size_in_stock,
                                "first_observed_price": fop,
                                "last_updated_at":      now_iso,
                            }, on_conflict="external_sku")
                        )
                populated_rows += 1

            # Polite delay to avoid pattern detection
            time.sleep(random.uniform(0.6, 1.4))

        print(f"  [LCW] Sizes: {fetched} pages fetched, {populated_rows} variant rows populated.")
    except Exception as e:
        print(f"  [LCW] Size population error (non-fatal): {e}")

    return products_seen, price_changes

# ── DeFacto Scraper ───────────────────────────────────────────────────────────

def fetch_defacto_sizes_batch(session, domain, long_codes):
    """
    POST to CombinProductListByProductLongCode with a list of LongCodes.
    Returns a dict: {LongCode -> [{"size": str, "is_in_stock": bool}, ...]}

    Confirmed from browser Network tab (9 Jun 2026):
    - Method: POST
    - Content-Type: application/json
    - Body: ["LONGCODE1", "LONGCODE2", ...]
    - Response: {Data: [{LongCode, Sizes: [{SizeName, StockQuantity: null}, ...], Stock: N}, ...]}

    Per-size StockQuantity is always null — DeFacto does not expose it via API.
    We inherit is_in_stock from the parent variant's Stock > 0 field.
    """
    if not long_codes:
        return {}
    url = f"https://{domain}/en-eg/Catalog/CombinProductListByProductLongCode"
    try:
        res = execute_with_retry(
            session.post, url,
            json=long_codes,
            timeout=15,
            headers={
                "accept":           "*/*",
                "accept-language":  "en-US,en;q=0.9",
                "content-type":     "application/json; charset=UTF-8",
                "origin":           f"https://{domain}",
                "referer":          f"https://{domain}/en-eg/",
                "sec-fetch-dest":   "empty",
                "sec-fetch-mode":   "cors",
                "sec-fetch-site":   "same-origin",
                "x-requested-with": "XMLHttpRequest",
            }
        )
        if res.status_code != 200:
            print(f"  ⚠️ [DeFacto] CombinProduct HTTP {res.status_code}")
            return {}
        data = res.json()
        items = data.get("Data") or []
        result = {}
        for item in items:
            lc = item.get("ProductLongCode") or (item.get("DataLayer") or {}).get("LongCode")
            if not lc:
                continue
            # Use the outer Sizes array (has SizeName), stock inherited from Stock field
            raw_sizes = item.get("Sizes") or (item.get("DataLayer") or {}).get("Sizes") or []
            stock_total = int(item.get("ProductVariantMiniProductStock") or
                              (item.get("DataLayer") or {}).get("Stock") or 0)
            is_avail = stock_total > 0
            result[lc] = [
                {"size": sz["SizeName"], "is_in_stock": is_avail}
                for sz in raw_sizes if sz.get("SizeName")
            ]
        return result
    except Exception as e:
        print(f"  ⚠️ [DeFacto] CombinProduct batch error: {e}")
        return {}

def scrape_defacto(supabase, session, brand_name, domain, today, prev_stock_state, fop_done_ids):
    """
    Scrapes DeFacto Egypt via the PartialIndexScrollResult API.
    """
    print(f"  [DeFacto] Starting catalog scrape (no proxy)...")
    products_seen, price_changes = 0, 0

    prev_prices = load_last_prices(supabase, brand_name)
    # v14.17: fop_done_ids passed in from scrape_brand (derived from the variant
    # preload) — the separate load_products_with_fop() read is gone.
    existing_snapshot_ids = set()
    _snap_offset = 0
    while True:
        _snap = safe_db_execute(
            supabase.table("price_snapshots").select("product_id")
            .eq("brand", brand_name).eq("snapshot_date", str(today))
            .range(_snap_offset, _snap_offset + 999)
        )
        _rows = (_snap.data or []) if _snap else []
        for r in _rows:
            if r.get("product_id"):
                existing_snapshot_ids.add(r["product_id"])
        if len(_rows) < 1000:
            break
        _snap_offset += 1000
    print(f"  [DeFacto] {len(existing_snapshot_ids)} snapshots already exist for today.")

    headers = {
        "accept":          "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "referer":         f"https://{domain}/en-eg/man-new-season",
        "sec-fetch-dest":  "empty",
        "sec-fetch-mode":  "cors",
        "sec-fetch-site":  "same-origin",
    }

    for cat in DEFACTO_CATEGORIES:
        cat_name   = cat["name"]
        cat_gender = cat["gender"]
        fx_sb1     = cat["fx_sb1"]
        fx_sb2     = cat["fx_sb2"]

        first_url = (
            f"https://{domain}/en-eg/Catalog/PartialIndexScrollResult"
            f"?page=1&SortOrder=0&pageSize=36"
            f"&fx_sb1={fx_sb1}&fx_sb2={fx_sb2}&PictureType=0"
        )

        print(f"  [{cat_name}] Fetching first page...")
        next_url   = first_url
        page_index = 0

        while next_url:
            page_index += 1
            try:
                res = execute_with_retry(
                    session.get, next_url, timeout=30, headers=headers
                )
            except Exception as e:
                print(f"  ⚠️ [DeFacto][{cat_name}] Page {page_index} network error: {e}")
                break

            if res.status_code != 200:
                print(f"  ⚠️ [DeFacto][{cat_name}] HTTP {res.status_code} on page {page_index}.")
                if page_index == 1:
                    print(f"  ⚠️ [DeFacto] First page blocked. Akamai may be rejecting the GitHub Actions IP.")
                break

            try:
                data = res.json()
            except Exception:
                print(f"  ⚠️ [DeFacto][{cat_name}] Page {page_index} returned non-JSON body.")
                break

            items = (data.get("Data") or {}).get("DataLayer") or []
            if not items:
                items = data.get("DataLayer") or []

            if not items:
                print(f"  [DeFacto][{cat_name}] Page {page_index} — no items. End of catalog.")
                break

            # ── Products upsert ───────────────────────────────────────────
            batch_products, seen_ext_ids = [], set()
            for item in items:
                long_code = item.get("LongCode")
                if not long_code or long_code in seen_ext_ids:
                    continue
                seen_ext_ids.add(long_code)

                name = item.get("Name") or item.get("GtmName") or f"DeFacto-{long_code}"

                cat_lvl3 = (item.get("CategoriesLvl3") or {}).get("CategoryName") or ""
                cat_lvl2 = (item.get("CategoriesLvl2") or {}).get("CategoryName") or ""
                cat_lvl1 = (item.get("CategoriesLvl1") or {}).get("CategoryName") or ""
                category_raw  = cat_lvl3 or cat_lvl2 or cat_lvl1 or ""
                category_norm = normalize_category(f"{category_raw} {name}")

                gender = cat_gender

                seo_name = item.get("SeoName") or ""
                variant_index = item.get("ProductVariantIndex") or ""
                if seo_name and variant_index:
                    product_url = f"https://{domain}/en-eg/{seo_name}-{variant_index}"
                else:
                    product_url = f"https://{domain}/en-eg/"

                batch_products.append({
                    "brand":               brand_name,
                    "external_id":         long_code,
                    "name":                name,
                    "category_raw":        category_raw,
                    "category_normalized": category_norm,
                    "gender":              gender,
                    "sizes_available":     [],
                    "url":                 product_url,
                    "image_url":           item.get("PictureName") or None,
                    "last_seen_at":        datetime.now(timezone.utc).isoformat(),
                    "is_active":           True,
                    # v14.24: DeFacto's titles are the strongest signal for
                    # both sleeve length and fit/cut (confirmed: 55%/61% live
                    # coverage) — default title-first priority order applies.
                    # Also picks up any v14.25 dress/jacket/underwear/swimwear/
                    # bag attributes if this product's category matches.
                    "attributes_extracted": build_attributes_extracted(
                        brand_name, category_norm, category_raw, name
                    ),
                })

            if not batch_products:
                break

            product_upsert_rows = []
            for i in range(0, len(batch_products), 100):
                res_p = safe_db_execute(
                    supabase.table("products").upsert(
                        batch_products[i:i+100], on_conflict="brand,external_id"
                    )
                )
                if res_p and res_p.data:
                    product_upsert_rows.extend(res_p.data)

            product_id_map  = {row["external_id"]: row["id"] for row in product_upsert_rows}
            products_seen  += len(batch_products)

            for item in items:
                db_pid = product_id_map.get(item.get("LongCode"))
                if not db_pid:
                    continue
                # v14.17: skip already-seeded products (fop_done_ids passed in).
                if db_pid in fop_done_ids:
                    continue
                disc_price = float(item.get("DiscountedPrice") or 0)
                full_price = float(item.get("Price") or 0)
                fop = disc_price if disc_price > 0 else full_price
                if fop > 0:
                    safe_db_execute(
                        supabase.table("products")
                        .update({"first_observed_price": fop})
                        .eq("id", db_pid)
                        .is_("first_observed_price", "null")
                    )
                    fop_done_ids.add(db_pid)

            # ── Variants upsert ───────────────────────────────────────────
            batch_variants, product_variant_tracking = [], {}
            for item in items:
                long_code = item.get("LongCode")
                db_pid    = product_id_map.get(long_code)
                if not db_pid:
                    continue
                product_variant_tracking.setdefault(db_pid, [])

                disc_price = float(item.get("DiscountedPrice") or 0)
                full_price = float(item.get("Price") or 0)

                if disc_price > 0 and disc_price < full_price:
                    price      = disc_price
                    compare_at = full_price
                else:
                    price      = full_price if full_price > 0 else disc_price
                    compare_at = None

                if price == 0:
                    continue

                stock_qty  = item.get("Stock") or 0
                is_avail   = int(stock_qty) > 0

                color_name = item.get("ColorGtmName") or item.get("ColorName") or None

                sku        = f"defacto_{long_code}"
                prev       = prev_stock_state.get(sku)
                v_baseline = float(prev["first_observed_price"]) if (prev and prev.get("first_observed_price")) else price

                batch_variants.append({
                    "product_id":          db_pid,
                    "external_sku":        sku,
                    "color":               color_name,
                    # v14.18: "size" intentionally EXCLUDED. DeFacto's catalog API
                    # doesn't include per-variant sizes — those come from
                    # CombinProductList. Including size=None here made the upsert
                    # overwrite sizes populated by previous runs' inline/backfill
                    # passes, forcing the backfill to redo ~5,000 variants every
                    # run (~1h40m wasted). Omitting the key means ON CONFLICT SET
                    # skips the column, preserving existing values.
                    "is_in_stock":         is_avail,
                    "first_observed_price": v_baseline,
                    "last_updated_at":     datetime.now(timezone.utc).isoformat(),
                    "_meta_price":         price,
                    "_meta_compare":       compare_at,
                    "_meta_baseline":      v_baseline,
                    "_meta_size":          None,
                    "_meta_color":         color_name,
                    "_meta_available":     is_avail,
                })

            if batch_variants:
                db_payload = [{k: v for k, v in r.items() if not k.startswith("_meta_")} for r in batch_variants]
                variant_upsert_rows = []
                for i in range(0, len(db_payload), 100):
                    res_v = safe_db_execute(
                        supabase.table("product_variants").upsert(
                            db_payload[i:i+100], on_conflict="external_sku"
                        )
                    )
                    if res_v and res_v.data:
                        variant_upsert_rows.extend(res_v.data)

                sku_to_id = {row["external_sku"]: row["id"] for row in variant_upsert_rows}
                for vr in batch_variants:
                    vr["variant_db_id"] = sku_to_id.get(vr["external_sku"])
                    product_variant_tracking[vr["product_id"]].append(vr)

                # ── Inline size population via batch API ──────────────────────
                # v14.14: only fetch sizes for variants that don't already have
                # one stored. The end-of-run backfill block (below) is the
                # safety net that catches anything still missing, so dropping
                # the redundant per-page rewrites loses no coverage — it just
                # stops ~23,500 no-op UPDATE round-trips per run on a fully
                # populated catalog.
                variants_needing_sizes = []
                for vr in batch_variants:
                    prev = prev_stock_state.get(vr["external_sku"])
                    if not prev or not prev.get("size"):
                        variants_needing_sizes.append(vr)

                page_long_codes = [
                    vr["external_sku"].replace("defacto_", "", 1)
                    for vr in variants_needing_sizes
                ]
                if page_long_codes:
                    size_map = fetch_defacto_sizes_batch(session, domain, page_long_codes)
                    now_iso_sz = datetime.now(timezone.utc).isoformat()
                    for vr in variants_needing_sizes:
                        lc    = vr["external_sku"].replace("defacto_", "", 1)
                        sizes = size_map.get(lc, [])
                        if not sizes or not vr.get("variant_db_id"):
                            continue
                        safe_db_execute(
                            supabase.table("product_variants")
                            .update({"size": sizes[0]["size"], "is_in_stock": sizes[0]["is_in_stock"], "last_updated_at": now_iso_sz})
                            .eq("id", vr["variant_db_id"])
                        )
                        for sz in sizes[1:]:
                            extra_sku = f"defacto_{lc}_{sz['size'].replace(' ', '_')}"
                            safe_db_execute(
                                supabase.table("product_variants").upsert({
                                    "product_id":           vr["product_id"],
                                    "external_sku":         extra_sku,
                                    "color":                vr.get("_meta_color"),
                                    "size":                 sz["size"],
                                    "is_in_stock":          sz["is_in_stock"],
                                    "first_observed_price": vr.get("_meta_baseline"),
                                    "last_updated_at":      now_iso_sz,
                                }, on_conflict="external_sku")
                            )

                snap_rows = build_snapshot_rows(brand_name, product_variant_tracking, today, existing_snapshot_ids)
                if snap_rows:
                    safe_db_execute(supabase.table("price_snapshots").insert(snap_rows))

                for db_pid, records in product_variant_tracking.items():
                    if not records:
                        continue
                    sizes_in_stock = [r["_meta_size"] for r in records if r["_meta_available"] and r["_meta_size"]]
                    for rec in records:
                        prev_v = prev_stock_state.get(rec["external_sku"])
                        if prev_v:
                            detect_and_write_stockout(
                                supabase, rec["variant_db_id"], db_pid, brand_name,
                                rec["_meta_size"], rec["_meta_color"],
                                prev_v["is_in_stock"], rec["_meta_available"],
                                rec["_meta_price"], rec["_meta_baseline"]
                            )
                    curr_price = product_repr_price(records)
                    v_base     = product_repr_baseline(records)
                    if curr_price is None:
                        continue
                    last_p = prev_prices.get(db_pid)
                    if last_p is None:
                        prev_prices[db_pid] = curr_price
                    elif abs(last_p - curr_price) > 0.01:
                        direction = "down" if curr_price < last_p else "up"
                        price_changes += 1

                        if direction == "down" and v_base and curr_price < v_base:
                            p_item = next((pi for pi in items
                                           if pi.get("LongCode") == next((k for k, v in product_id_map.items() if v == db_pid), None)), None)
                            if p_item:
                                p_name = p_item.get("Name") or p_item.get("GtmName") or "DeFacto Item"
                                seo    = p_item.get("SeoName") or ""
                                vidx   = p_item.get("ProductVariantIndex") or ""
                                p_url  = f"https://{domain}/en-eg/{seo}-{vidx}" if seo and vidx else f"https://{domain}"
                                cat_lvl3 = (p_item.get("CategoriesLvl3") or {}).get("CategoryName") or ""
                                p_cat  = normalize_category(f"{cat_lvl3} {p_name}")
                                for sz in set(sizes_in_stock):
                                    find_and_alert_users(
                                        supabase, session, brand_name, p_cat,
                                        sz, curr_price, p_name, p_url, v_base
                                    )

                        honest_disc = round(((v_base - curr_price) / v_base) * 100, 2) if (v_base and curr_price < v_base) else None
                        safe_db_execute(supabase.table("price_events").insert({
                            "product_id":       db_pid,
                            "brand":            brand_name,
                            "price_before":     last_p,
                            "price_after":      curr_price,
                            "compare_at_price": records[0].get("_meta_compare"),
                            "discount_pct":     honest_disc,
                            "direction":        direction,
                            "sizes_in_stock":   sizes_in_stock,
                            "recorded_at":      datetime.now(timezone.utc).isoformat(),
                        }))
                        prev_prices[db_pid] = curr_price
                        sync_snapshot_price(supabase, db_pid, today, curr_price)

            print(f"  [{cat_name}] Page {page_index} — {len(items)} items processed.")

            raw_next = (data.get("Data") or {}).get("NextDataUrl") or data.get("NextDataUrl")
            if raw_next:
                next_url = raw_next
                time.sleep(random.uniform(0.8, 1.5))
            else:
                print(f"  [{cat_name}] No NextDataUrl — end of catalog.")
                break

    # ── DeFacto backfill pass: fix any variants still missing sizes ─────────
    try:
        all_missing, miss_offset = [], 0
        while True:
            chunk = safe_db_execute(
                supabase.table("product_variants")
                .select("id, product_id, external_sku, color, is_in_stock, first_observed_price, products!inner(brand)")
                .eq("products.brand", brand_name)
                .is_("size", "null")
                .range(miss_offset, miss_offset + 999)
            )
            rows = (chunk.data or []) if chunk else []
            all_missing.extend(rows)
            if len(rows) < 1000:
                break
            miss_offset += 1000

        if all_missing:
            print(f"  [DeFacto] Backfilling sizes for {len(all_missing)} variants...")
            sku_to_row = {}
            long_codes = []
            for row in all_missing:
                lc = row["external_sku"].replace("defacto_", "", 1)
                sku_to_row[lc] = row
                long_codes.append(lc)

            populated = 0
            for batch_start in range(0, len(long_codes), 36):
                batch = long_codes[batch_start:batch_start + 36]
                size_map = fetch_defacto_sizes_batch(session, domain, batch)
                now_iso  = datetime.now(timezone.utc).isoformat()
                for lc, sizes in size_map.items():
                    if not sizes:
                        continue
                    row = sku_to_row.get(lc)
                    if not row:
                        continue
                    product_id = row.get("product_id")
                    color      = row.get("color")
                    fop        = row.get("first_observed_price") or prev_prices.get(product_id)
                    for i, sz in enumerate(sizes):
                        if i == 0:
                            safe_db_execute(
                                supabase.table("product_variants")
                                .update({"size": sz["size"], "is_in_stock": sz["is_in_stock"], "last_updated_at": now_iso})
                                .eq("id", row["id"])
                            )
                        else:
                            sku = f"defacto_{lc}_{sz['size'].replace(' ', '_')}"
                            safe_db_execute(
                                supabase.table("product_variants").upsert({
                                    "product_id":           product_id,
                                    "external_sku":         sku,
                                    "color":                color,
                                    "size":                 sz["size"],
                                    "is_in_stock":          sz["is_in_stock"],
                                    "first_observed_price": fop,
                                    "last_updated_at":      now_iso,
                                }, on_conflict="external_sku")
                            )
                    populated += 1
                time.sleep(random.uniform(0.5, 1.0))
            print(f"  [DeFacto] Backfill complete: {populated} variants populated.")
        else:
            print("  [DeFacto] All variants have size data. ✅")
    except Exception as e:
        print(f"  [DeFacto] Size backfill error (non-fatal): {e}")

    return products_seen, price_changes

# ── WooCommerce Scraper (Mobaco) — v14.19 rewrite ────────────────────────────
# WooCommerce ships a public, key-less "Store API" that powers its own cart/blocks
# and is enabled by default in WooCommerce core regardless of theme:
#   GET /wp-json/wc/store/v1/products?per_page=100&page=N   → JSON array
# Each product carries a `prices` object with amounts expressed in MINOR units as
# strings, plus the divisor to apply:
#   prices.price            "12000"   (current)
#   prices.regular_price    "15000"   (list price)
#   prices.sale_price       "12000"
#   prices.currency_minor_unit  2     → real price = int(amount) / 10**minor_unit
# We read currency_minor_unit from the response (never hardcode it) so a store with
# a 3-decimal currency can't silently make every price 10x wrong.
#
# v14.19 FIXES (confirmed against a live Mobaco product sample, 20 Jun 2026):
#
#   PROBLEM 1 — stock always true. The old code read the PRODUCT-level
#   `is_in_stock`, which WooCommerce's Store API sets to true as long as ANY
#   variation is purchasable. Every variant row inherited that one blanket
#   flag, so a single-variant stockout could never be detected — confirmed
#   via live query: 2,704 Mobaco variants, 100% in_stock, 0 stockout_events
#   in 9 days. FIX: each "variable" product's real variations now come from a
#   SEPARATE call — GET /products/{id}/variations — which the documented
#   WooCommerce Store API v1 schema returns with a PER-VARIATION is_in_stock
#   + price. This mirrors the same "second API call for the real per-unit
#   data" pattern already used for DeFacto (CombinProductList) and LCW
#   (product-page JSON).
#
#   PROBLEM 2 — colour collapsed to NULL for any multi-colour product. The
#   old `_woo_extract_sizes_colors()` returned a single colour only when
#   exactly one colour term existed product-wide, then cross-multiplied that
#   one colour against every size — which is simply wrong for any product
#   with 2+ colours (most of Mobaco's catalog; confirmed in the live sample:
#   2 of 3 sampled products had multiple colour terms and were the ones with
#   color=NULL in the DB). FIX: read each variation's actual attribute
#   pairing directly from `variations[].attributes[]`, so colour and size are
#   matched per real purchasable unit instead of guessed by cross-multiplying
#   a flat size list against one fallback colour.
#
#   KNOWN CEILING — NOT FIXABLE BY PARSING. Mobaco's `pa_colour` attribute
#   terms are internal SKU-style codes (e.g. "MS110148L143"), not human
#   colour words. Confirmed directly in the live sample: no human-readable
#   colour value exists anywhere in this API response, parent or variation
#   level. We do NOT invent a fake colour mapping — that would silently
#   manufacture false data, which is worse than an honest gap. The stored
#   `color` field for Mobaco will keep showing these codes; cross-brand
#   colour signals should EXCLUDE Mobaco until/unless a human-labelled source
#   is found. Size values ("N002", "J006"...) are the same kind of internal
#   code and have the same ceiling — but unlike colour, size strings are at
#   least usable for demand-by-size-CODE analysis within Mobaco alone
#   (consistent per garment line), so we keep storing them as-is.
#
# DESIGN DECISION: fetch /variations for EVERY variable product, EVERY run
# (~524 products total) rather than gating on missing data. Mobaco's catalog
# is small enough that this is cheap (one extra small-JSON call per product,
# no proxy needed), and "fetch all every run" avoids the kind of gating-state
# bugs that took several v14.x iterations to shake out on DeFacto and LCW —
# not worth importing that complexity for a brand this size.

WOO_SIZE_ATTR_HINTS  = ("size", "sizes", "مقاس", "المقاس")
WOO_COLOR_ATTR_HINTS = ("color", "colour", "colours", "colors", "لون", "اللون")

# v14.23: one-time-per-run diagnostic flag for the variation id-matching fix.
# Reset to {"done": False} at the top of scrape_woocommerce() so it prints
# once per scheduled run, not once per script lifetime.
_MOBACO_DIAG = {"done": False}

def _woo_price(amount, minor_unit):
    """Convert a WooCommerce minor-unit price string to a real number, safely."""
    try:
        if amount is None or amount == "":
            return 0.0
        return int(str(amount)) / (10 ** int(minor_unit))
    except (ValueError, TypeError):
        try:
            return float(amount)
        except (ValueError, TypeError):
            return 0.0

def _woo_first_image(product):
    """
    Robustly pull the first image URL from a Store API product. (v14.16)

    The Store API USUALLY returns `images` as a list of {"src": ...} dicts, but
    some Mobaco products return it as a DICT instead. Indexing a non-empty dict
    with [0] raises `KeyError: 0`, and because the image sits in the products
    dict literal, that error was discarding the ENTIRE product over a purely
    cosmetic field (confirmed: products 1207002 and 1204625 went missing this
    way). This handles list, dict, bare string, empty, and missing without ever
    raising — it returns None when there is no usable URL, so a weird image
    shape costs us only the thumbnail, never the product.
    """
    imgs = product.get("images")
    if not imgs:
        return None
    first = None
    if isinstance(imgs, list):
        first = imgs[0] if imgs else None
    elif isinstance(imgs, dict):
        first = next(iter(imgs.values()), None)
    if isinstance(first, dict):
        return first.get("src")
    if isinstance(first, str):
        return first
    return None

def _woo_build_term_lookup(product):
    """
    attr_name (lowercased) -> {slug (lowercased) -> human-facing term name}.
    For Mobaco today the "human-facing term name" is itself an internal SKU
    code (see ceiling note above) — this function is honest about resolving
    whatever name WooCommerce actually stores, it does not improve on it.
    """
    lookup = {}
    for attr in product.get("attributes") or []:
        name = (attr.get("name") or "").strip().lower()
        terms = attr.get("terms") or []
        lookup[name] = {
            str(t.get("slug", "")).strip().lower(): t.get("name")
            for t in terms if t.get("slug") is not None
        }
    return lookup

def _woo_resolve_variation_attrs(variation, term_lookup):
    """
    Given one entry from product.variations[] (raw {name, value} slug pairs)
    and the product's term_lookup, resolve which value is the size label and
    which is the colour label using the WOO_*_ATTR_HINTS, same hint strings
    the rest of the codebase uses. Returns (size_or_None, color_or_None).

    v14.23: no longer returns a lookup_key. Matching against the live
    /variations endpoint now happens on the variation's own numeric `id`
    (see fetch_mobaco_variations' v14.23 changelog) — attribute name/value
    text is only used here for what it's actually reliable for: resolving
    human-facing size/colour labels to store in the database.
    """
    size_val, color_val = None, None
    for a in (variation.get("attributes") or []):
        aname = (a.get("name") or "").strip().lower()
        aval  = (a.get("value") or "").strip().lower()
        if not aname:
            continue
        resolved = term_lookup.get(aname, {}).get(aval, aval)
        if any(h in aname for h in WOO_SIZE_ATTR_HINTS):
            size_val = resolved
        elif any(h in aname for h in WOO_COLOR_ATTR_HINTS):
            color_val = resolved
    return size_val, color_val

def fetch_mobaco_variations(session, domain, product_id):
    """
    GET the real per-variation price + stock for one product via the
    documented WooCommerce Store API v1 endpoint:
        GET /wp-json/wc/store/v1/products/{id}/variations
    Returns {variation_id (int): {"price", "compare_at", "is_in_stock", "sku"}}.
    Returns {} on ANY failure (network, bad JSON, unexpected shape) — caller
    falls back to product-level price/stock for that one variation rather
    than raising, matching the fault-isolation pattern used everywhere else
    in this engine.

    v14.23: keyed by the variation's own numeric `id` instead of a
    reconstructed (attr_name, attr_value) tuple. See the v14.23 changelog
    entry at the top of this file for the full diagnosis — in short, the
    parent product's embedded variations[] list and this dedicated endpoint
    are NOT guaranteed to describe attributes the same way (one can use a
    human label like "Size" / "N001", the other a taxonomy slug like
    "pa_size" / "n001"), so building a matching key from name/value text
    matched 0% of the time across the entire brand. The variation `id` field
    is the one place both responses are guaranteed to agree, since they're
    two different views of the exact same WordPress post object.
    """
    url = f"https://{domain}/wp-json/wc/store/v1/products/{product_id}/variations"
    try:
        res = execute_with_retry(
            session.get, url, max_retries=2, backoff=2, timeout=15,
            headers={
                "accept":          "application/json, text/plain, */*",
                "accept-language": "en-US,en;q=0.9",
                "referer":         f"https://{domain}/",
            }
        )
        if res.status_code != 200:
            return {}
        rows = res.json()
        if not isinstance(rows, list):
            return {}
    except Exception:
        return {}

    result = {}
    for row in rows:
        try:
            var_id = row.get("id")
            if not var_id:
                continue
            prices = row.get("prices") or {}
            minor  = prices.get("currency_minor_unit", 2)
            cur    = _woo_price(prices.get("price"), minor)
            reg    = _woo_price(prices.get("regular_price"), minor)
            if cur <= 0:
                continue
            result[int(var_id)] = {
                "price":       cur,
                "compare_at":  reg if reg > cur else None,
                "is_in_stock": bool(row.get("is_in_stock", False)),
                "sku":         row.get("sku") or "",
            }
        except Exception:
            continue
    return result

def scrape_woocommerce(supabase, session, brand_name, domain, today, prev_stock_state, fop_done_ids):
    """
    Scrapes a WooCommerce store via the public Store API.

    v14.19: now fetches each variable product's REAL per-variation price +
    stock via a second call to /products/{id}/variations (see header comment
    above for the full rationale). Simple products (type != "variable", no
    variations[]) are unchanged — one variant row from the product-level
    price/stock, exactly as before, since there is no finer grain available.
    """
    print(f"  [WooCommerce] Starting Store API scrape (no proxy)...")
    products_seen, price_changes = 0, 0
    PER_PAGE = 100
    _MOBACO_DIAG["done"] = False  # v14.23: reset so the diagnostic prints once per run

    prev_prices = load_last_prices(supabase, brand_name)
    # v14.17: fop_done_ids passed in from scrape_brand (derived from the variant
    # preload) — the separate load_products_with_fop() read is gone.
    existing_snapshot_ids = set()
    _snap_offset = 0
    while True:
        _snap = safe_db_execute(
            supabase.table("price_snapshots").select("product_id")
            .eq("brand", brand_name).eq("snapshot_date", str(today))
            .range(_snap_offset, _snap_offset + 999)
        )
        _rows = (_snap.data or []) if _snap else []
        for r in _rows:
            if r.get("product_id"):
                existing_snapshot_ids.add(r["product_id"])
        if len(_rows) < 1000:
            break
        _snap_offset += 1000
    print(f"  [WooCommerce] {len(existing_snapshot_ids)} snapshots already exist for today.")

    headers = {
        "accept":          "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "referer":         f"https://{domain}/",
    }

    page = 1
    while True:
        url = f"https://{domain}/wp-json/wc/store/v1/products?per_page={PER_PAGE}&page={page}"
        try:
            res = execute_with_retry(session.get, url, timeout=30, headers=headers)
        except Exception as e:
            print(f"  ⚠️ [WooCommerce] Page {page} network error: {e}")
            break
        if res.status_code != 200:
            if page == 1:
                print(f"  ⚠️ [WooCommerce] HTTP {res.status_code} on first page — Store API "
                      f"may be disabled or path differs. Body: {res.text[:160]}")
            break
        try:
            products = res.json()
        except Exception:
            print(f"  ⚠️ [WooCommerce] Page {page} returned non-JSON body.")
            break
        if not isinstance(products, list) or not products:
            break

        # ── Products upsert ───────────────────────────────────────────────
        # v14.14: each per-product iteration is individually try/excepted so a
        # single malformed item is skipped (with a traceback for diagnosis)
        # rather than crashing the entire brand. Mobaco specifically has been
        # the failure source — until we name the offending field we want every
        # other product on the page to still get scraped.
        batch_products, seen_ext_ids = [], set()
        for p in products:
            try:
                pid = p.get("id")
                if not pid or str(pid) in seen_ext_ids:
                    continue
                seen_ext_ids.add(str(pid))
                name = p.get("name") or f"{brand_name}-{pid}"
                cats = [c.get("name", "") for c in (p.get("categories") or [])]
                category_raw  = cats[0] if cats else ""
                category_norm = normalize_category(f"{' '.join(cats)} {name}")
                gender        = normalize_gender(cats, "", name)
                batch_products.append({
                    "brand":               brand_name,
                    "external_id":         str(pid),
                    "name":                name,
                    "category_raw":        category_raw,
                    "category_normalized": category_norm,
                    "gender":              gender,
                    "sizes_available":     [],
                    "url":                 p.get("permalink") or f"https://{domain}/",
                    "image_url":           _woo_first_image(p),
                    "last_seen_at":        datetime.now(timezone.utc).isoformat(),
                    "is_active":           True,
                    # v14.29: WooCommerce Store API exposes date_created.
                    "source_published_at": p.get("date_created"),
                    # v14.24: Mobaco is in SLEEVE_LENGTH_CATEGORY_FIRST_BRANDS
                    # (some sleeve-length signal in category_raw); for fit/cut
                    # it uses the default title-first order — Mobaco wasn't a
                    # category-dominant brand for that attribute. Also picks up
                    # any v14.25 dress/jacket/underwear/swimwear/bag attributes
                    # if this product's category matches.
                    "attributes_extracted": build_attributes_extracted(
                        brand_name, category_norm, category_raw, name
                    ),
                })
            except Exception as e:
                _pid = (p or {}).get("id", "?")
                print(f"  ⚠️ [WooCommerce] Skipping product id={_pid} during products build: {e}")
                traceback.print_exc()
                continue
        if not batch_products:
            break

        product_upsert_rows = []
        for i in range(0, len(batch_products), 100):
            res_p = safe_db_execute(
                supabase.table("products").upsert(batch_products[i:i+100], on_conflict="brand,external_id")
            )
            if res_p and res_p.data:
                product_upsert_rows.extend(res_p.data)
        product_id_map  = {row["external_id"]: row["id"] for row in product_upsert_rows}
        products_seen  += len(batch_products)

        for p in products:
            try:
                db_pid = product_id_map.get(str(p.get("id")))
                if not db_pid:
                    continue
                if db_pid in fop_done_ids:
                    continue
                prices = p.get("prices") or {}
                minor  = prices.get("currency_minor_unit", 2)
                cur    = _woo_price(prices.get("price"), minor)
                if cur > 0:
                    safe_db_execute(
                        supabase.table("products")
                        .update({"first_observed_price": cur})
                        .eq("id", db_pid)
                        .is_("first_observed_price", "null")
                    )
                    fop_done_ids.add(db_pid)
            except Exception as e:
                _pid = (p or {}).get("id", "?")
                print(f"  ⚠️ [WooCommerce] Skipping product id={_pid} during FOP seed: {e}")
                traceback.print_exc()
                continue

        # ── Variants upsert — v14.19: real per-variation price/stock ──────
        batch_variants, product_variant_tracking = [], {}
        for p in products:
            try:
                db_pid = product_id_map.get(str(p.get("id")))
                if not db_pid:
                    continue
                product_variant_tracking.setdefault(db_pid, [])

                parent_prices = p.get("prices") or {}
                parent_minor  = parent_prices.get("currency_minor_unit", 2)
                parent_cur    = _woo_price(parent_prices.get("price"), parent_minor)
                parent_reg    = _woo_price(parent_prices.get("regular_price"), parent_minor)
                parent_on_sale = bool(p.get("on_sale"))
                parent_avail   = bool(p.get("is_in_stock", True))

                variations = p.get("variations") or []

                if not variations:
                    # Simple product, unchanged behaviour: one row from the
                    # product-level price/stock (there is no finer grain).
                    if parent_cur <= 0 or parent_cur > 1_000_000:
                        continue
                    compare_at = parent_reg if (parent_on_sale and parent_reg > parent_cur) else None
                    sku        = f"{brand_name}_{p['id']}"
                    prev       = prev_stock_state.get(sku)
                    v_baseline = float(prev["first_observed_price"]) if (prev and prev.get("first_observed_price")) else parent_cur
                    batch_variants.append({
                        "product_id": db_pid, "external_sku": sku, "color": None, "size": None,
                        "is_in_stock": parent_avail, "first_observed_price": v_baseline,
                        "last_updated_at": datetime.now(timezone.utc).isoformat(),
                        "_meta_price": parent_cur, "_meta_compare": compare_at, "_meta_baseline": v_baseline,
                        "_meta_size": None, "_meta_color": None, "_meta_available": parent_avail,
                    })
                    continue

                # Variable product: fetch REAL per-variation price + stock.
                # One extra call per product, every run, by design (524
                # products total — cheap, simplest to reason about, no
                # gating state to get wrong).
                var_data = fetch_mobaco_variations(session, domain, p["id"])
                term_lookup = _woo_build_term_lookup(p)

                # v14.23: one-time diagnostic on the FIRST variable product of
                # the run only — confirms whether the id-based match is
                # actually landing now. Printed once (not per-product) so it's
                # easy to spot in the run log without flooding it. If
                # var_data is non-empty but still produces zero matches against
                # this product's variation ids, that's a second-order surprise
                # worth seeing immediately rather than silently falling back
                # again exactly like before.
                if not _MOBACO_DIAG["done"]:
                    _MOBACO_DIAG["done"] = True
                    sample_ids = [v.get("id") for v in variations[:5]]
                    matched = sum(1 for vid in sample_ids if vid and int(vid) in var_data)
                    print(f"  [WooCommerce][diag] product={p.get('id')} "
                          f"variations_endpoint_rows={len(var_data)} "
                          f"sample_variation_ids={sample_ids} "
                          f"matched={matched}/{len(sample_ids)}")

                for var in variations:
                    try:
                        var_id = var.get("id")
                        size_val, color_val = _woo_resolve_variation_attrs(var, term_lookup)
                        # v14.23: match on the variation's own numeric id — the
                        # one field guaranteed to mean the same thing in both
                        # the parent product's variations[] summary and the
                        # dedicated /variations endpoint response. See
                        # fetch_mobaco_variations' changelog for why the old
                        # attribute-tuple key never matched.
                        live = var_data.get(int(var_id)) if var_id else None

                        if live:
                            cur, compare_at, is_avail = live["price"], live["compare_at"], live["is_in_stock"]
                        else:
                            # The /variations call failed, or this specific
                            # variation id wasn't in its response — fall back
                            # to the parent's values rather than dropping the
                            # variant entirely. This degrades gracefully to
                            # the OLD behaviour for just this one row instead
                            # of losing data.
                            cur = parent_cur
                            compare_at = parent_reg if (parent_on_sale and parent_reg > parent_cur) else None
                            is_avail = parent_avail

                        if cur <= 0 or cur > 1_000_000:
                            continue

                        sku_suffix = f"_{var_id}" if var_id else f"_{size_val or ''}{color_val or ''}"
                        sku        = f"{brand_name}_{p['id']}{sku_suffix}"
                        prev       = prev_stock_state.get(sku)
                        v_baseline = float(prev["first_observed_price"]) if (prev and prev.get("first_observed_price")) else cur

                        batch_variants.append({
                            "product_id": db_pid, "external_sku": sku,
                            "color": color_val, "size": size_val,
                            "is_in_stock": is_avail, "first_observed_price": v_baseline,
                            "last_updated_at": datetime.now(timezone.utc).isoformat(),
                            "_meta_price": cur, "_meta_compare": compare_at, "_meta_baseline": v_baseline,
                            "_meta_size": size_val, "_meta_color": color_val, "_meta_available": is_avail,
                        })
                    except Exception as e:
                        print(f"  ⚠️ [WooCommerce] Skipping variation on product {p.get('id')}: {e}")
                        continue

                # Be polite to Mobaco's server — one extra call per product.
                # v14.20: widened from 0.2-0.4s to 1.0-1.8s. The tighter pace
                # was ~2.5-5 requests/second sustained against one small
                # WooCommerce host for ~524 products, which is fast enough to
                # trip a basic hosting-provider rate limiter — confirmed by a
                # real run logging repeated 429s on this exact endpoint. This
                # full pass now takes roughly 524 x ~1.4s ≈ 12 minutes instead
                # of ~3, which is the actual fix; execute_with_retry's longer
                # backoff (see its v14.20 changelog) is the safety net for
                # whatever rate-limiting still slips through, not the primary
                # fix.
                time.sleep(random.uniform(1.0, 1.8))

            except Exception as e:
                _pid = (p or {}).get("id", "?")
                print(f"  ⚠️ [WooCommerce] Skipping product id={_pid} during variants build: {e}")
                traceback.print_exc()
                continue

        if batch_variants:
            db_payload = [{k: v for k, v in r.items() if not k.startswith("_meta_")} for r in batch_variants]
            variant_upsert_rows = []
            for i in range(0, len(db_payload), 100):
                res_v = safe_db_execute(
                    supabase.table("product_variants").upsert(db_payload[i:i+100], on_conflict="external_sku")
                )
                if res_v and res_v.data:
                    variant_upsert_rows.extend(res_v.data)
            sku_to_id = {row["external_sku"]: row["id"] for row in variant_upsert_rows}
            for vr in batch_variants:
                vr["variant_db_id"] = sku_to_id.get(vr["external_sku"])
                product_variant_tracking[vr["product_id"]].append(vr)

            snap_rows = build_snapshot_rows(brand_name, product_variant_tracking, today, existing_snapshot_ids)
            if snap_rows:
                safe_db_execute(supabase.table("price_snapshots").insert(snap_rows))

            for db_pid, records in product_variant_tracking.items():
                try:
                    if not records:
                        continue
                    sizes_in_stock = [r["_meta_size"] for r in records if r["_meta_available"] and r["_meta_size"]]
                    for rec in records:
                        prev_v = prev_stock_state.get(rec["external_sku"])
                        if prev_v:
                            detect_and_write_stockout(
                                supabase, rec["variant_db_id"], db_pid, brand_name,
                                rec["_meta_size"], rec["_meta_color"],
                                prev_v["is_in_stock"], rec["_meta_available"],
                                rec["_meta_price"], rec["_meta_baseline"]
                            )
                    curr_price = product_repr_price(records)
                    v_base     = product_repr_baseline(records)
                    if curr_price is None:
                        continue
                    last_p = prev_prices.get(db_pid)
                    if last_p is None:
                        prev_prices[db_pid] = curr_price
                    elif abs(last_p - curr_price) > 0.01:
                        direction = "down" if curr_price < last_p else "up"
                        price_changes += 1
                        if direction == "down" and v_base and curr_price < v_base:
                            prod = next((p for p in products if str(p.get("id")) ==
                                         next((k for k, v in product_id_map.items() if v == db_pid), None)), None)
                            if prod:
                                cats  = [c.get("name", "") for c in (prod.get("categories") or [])]
                                p_cat = normalize_category(f"{' '.join(cats)} {prod.get('name','')}")
                                p_url = prod.get("permalink") or f"https://{domain}/"
                                for sz in set(sizes_in_stock):
                                    find_and_alert_users(
                                        supabase, session, brand_name, p_cat,
                                        sz, curr_price, prod.get("name", "Item"), p_url, v_base
                                    )
                        honest_disc = round(((v_base - curr_price) / v_base) * 100, 2) if (v_base and curr_price < v_base) else None
                        safe_db_execute(supabase.table("price_events").insert({
                            "product_id":       db_pid, "brand": brand_name,
                            "price_before":     last_p, "price_after": curr_price,
                            "compare_at_price": records[0].get("_meta_compare"),
                            "discount_pct":     honest_disc,
                            "direction":        direction, "sizes_in_stock": sizes_in_stock,
                            "recorded_at":      datetime.now(timezone.utc).isoformat(),
                        }))
                        prev_prices[db_pid] = curr_price
                        sync_snapshot_price(supabase, db_pid, today, curr_price)
                except Exception as e:
                    print(f"  ⚠️ [WooCommerce] Skipping product db_pid={db_pid} during detection: {e}")
                    traceback.print_exc()
                    continue

        print(f"  [WooCommerce] Page {page} — {len(batch_products)} products processed.")
        if len(products) < PER_PAGE:
            break
        page += 1
        time.sleep(random.uniform(0.8, 1.5))

    return products_seen, price_changes

# ── FX Rate (v14.29) ─────────────────────────────────────────────────────────
#
# One row per day recording the daily USD→EGP exchange rate. Used
# analytically to distinguish FX-driven repricing from genuine promotional
# markdowns. Fetched from frankfurter.app (free, open-source, no API key).
# Idempotent: if today's rate already exists, this is a no-op.

def fetch_and_store_fx_rate(supabase, session):
    """
    Fetch today's USD→EGP rate and store it. Runs once per scraper invocation
    (the __main__ block calls it before the brand loop). If today's row
    already exists (e.g. from an earlier run), this is a silent no-op.

    Uses frankfurter.app — a free, open-source ECB-based API that covers
    EGP. No API key needed. Falls back gracefully on any failure so the
    scraper run continues regardless.
    """
    today_str = str(date.today())
    try:
        existing = safe_db_execute(
            supabase.table("fx_rate")
            .select("id")
            .eq("rate_date", today_str)
            .limit(1)
        )
        if existing and existing.data:
            return  # already recorded today

        res = execute_with_retry(
            session.get,
            f"https://api.frankfurter.app/latest?from=USD&to=EGP",
            max_retries=2, backoff=3, timeout=10,
            headers={"User-Agent": "Khabar-Scraper/1.0"}
        )
        if res.status_code != 200:
            print(f"  ⚠️ FX rate API returned HTTP {res.status_code}. Skipping.")
            return
        data = res.json()
        rate = data.get("rates", {}).get("EGP")
        if not rate:
            print(f"  ⚠️ FX rate API did not return EGP rate. Response: {data}")
            return
        safe_db_execute(
            supabase.table("fx_rate").insert({
                "rate_date": today_str,
                "usd_egp":  float(rate),
                "source":   "frankfurter",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            })
        )
        print(f"  💱 FX rate recorded: 1 USD = {rate} EGP ({today_str})")
    except Exception as e:
        print(f"  ⚠️ FX rate fetch failed (non-fatal): {e}")


# ── Best-Seller Rank (v14.29) ───────────────────────────────────────────────
#
# Shopify's public /collections/all/products.json endpoint supports
# ?sort_by=best-selling, which returns products ranked by the brand's own
# sales data. This is a DIRECT demand signal — the brand is literally
# telling us what sells most, for free, via a sort parameter.
#
# Capped at top 150 per brand. One request per brand (limit=250 covers it).
# Runs as a post-run pass, not inline with per-brand scraping, so it
# doesn't slow down the main pipeline or pollute retry logic.
#
# Only available for Shopify brands — LCW/DeFacto/WooCommerce don't have
# a public best-selling sort on their catalog APIs.

BESTSELLER_CAP = 150  # max rank to record per brand per day

def collect_bestseller_ranks(supabase, session):
    """
    For each Shopify brand, fetch the best-selling sort and record
    rank 1–BESTSELLER_CAP. Idempotent: if today's ranks already exist
    for a brand, that brand is skipped.
    """
    today_str = str(date.today())
    shopify_brands = [b for b in BRANDS if b["engine"] == "shopify"]
    total_recorded = 0

    for brand in shopify_brands:
        brand_name = brand["name"]
        domain     = brand["domain"]
        try:
            # Check if we already have today's ranks for this brand
            existing = safe_db_execute(
                supabase.table("bestseller_rank")
                .select("id")
                .eq("brand", brand_name)
                .eq("snapshot_date", today_str)
                .limit(1)
            )
            if existing and existing.data:
                continue  # already collected today

            url = f"https://{domain}/collections/all/products.json?sort_by=best-selling&limit=250"
            try:
                res = execute_with_retry(
                    session.get, url, max_retries=2, backoff=3, timeout=30,
                    headers={"User-Agent": "Mozilla/5.0"}
                )
            except Exception as e:
                print(f"  ⚠️ [Bestseller] {brand_name}: request failed ({e}). Skipping.")
                continue

            if res.status_code != 200:
                print(f"  ⚠️ [Bestseller] {brand_name}: HTTP {res.status_code}. Skipping.")
                continue

            products = res.json().get("products", [])
            if not products:
                print(f"  ⚠️ [Bestseller] {brand_name}: empty response. Skipping.")
                continue

            # Map external_ids to product_ids in our DB. We need to look up
            # each product's DB id from its Shopify external_id.
            external_ids = [str(p["id"]) for p in products[:BESTSELLER_CAP]]

            # Fetch in batches of 100 (PostgREST .in_() has practical limits)
            db_id_map = {}
            for i in range(0, len(external_ids), 100):
                chunk_ids = external_ids[i:i+100]
                result = safe_db_execute(
                    supabase.table("products")
                    .select("id, external_id")
                    .eq("brand", brand_name)
                    .in_("external_id", chunk_ids)
                )
                if result and result.data:
                    for row in result.data:
                        db_id_map[row["external_id"]] = row["id"]

            # Build rank rows
            rank_rows = []
            now_iso = datetime.now(timezone.utc).isoformat()
            for rank_pos, p in enumerate(products[:BESTSELLER_CAP], start=1):
                ext_id = str(p["id"])
                db_pid = db_id_map.get(ext_id)
                if not db_pid:
                    continue  # product not in our DB (shouldn't happen, but safe)
                rank_rows.append({
                    "product_id":    db_pid,
                    "brand":         brand_name,
                    "rank":          rank_pos,
                    "snapshot_date": today_str,
                    "recorded_at":   now_iso,
                })

            # Insert in batches
            if rank_rows:
                for i in range(0, len(rank_rows), 100):
                    safe_db_execute(
                        supabase.table("bestseller_rank")
                        .insert(rank_rows[i:i+100])
                    )
                total_recorded += len(rank_rows)

            # Brief pause between brands (same pattern as main scraper)
            time.sleep(random.uniform(1, 3))

        except Exception as e:
            print(f"  ⚠️ [Bestseller] {brand_name}: unexpected error ({e}). Skipping.")
            continue

    if total_recorded:
        print(f"  🏆 Best-seller ranks recorded: {total_recorded} products across "
              f"{len(shopify_brands)} Shopify brands.")


# ── Orchestrator ──────────────────────────────────────────────────────────────

def scrape_brand(brand_name, domain):
    """
    v14.20: now returns (seen, changes) instead of just changes.

    WHY: the old single-value return made "0 price changes because nothing
    moved" indistinguishable from "0 price changes because the whole scrape
    failed at page 1" (e.g. Carina returning 0/0 after a 503 on the very
    first request). The caller needs `seen` to tell a healthy quiet day
    apart from a brand that needs a retry pass. `changes` is unchanged in
    meaning.
    """
    try:
        supabase     = create_client(SUPABASE_URL, SUPABASE_KEY)
        brand_config = next(b for b in BRANDS if b["name"] == brand_name)
        session      = get_lcw_session() if brand_config["engine"] == "lcw_proxy" else get_resilient_session()
    except Exception as e:
        print(f"❌ Initialization failed for {brand_name}: {e}")
        return 0, 0

    today = date.today()
    print(f"\n{'─'*55}\n▶  {brand_name.upper()}  —  {domain}\n{'─'*55}")
    try:
        # Skip the homepage pre-flight for API-based engines. Their root URLs
        # are often the gateway where bot protection lives (CDN challenges, etc.),
        # while their JSON APIs sit on a different code path that we exercise
        # directly. The engine itself will surface a clear error if the API is
        # genuinely unreachable. Mobaco's WordPress homepage was rejecting the
        # bare "Mozilla/5.0" UA we used here, blocking the whole brand from running.
        if brand_config["engine"] not in ("lcw_proxy", "defacto", "woocommerce"):
            if not check_domain(session, domain):
                print(f"  ⚠️ Domain {domain} unreachable. Skipping.")
                return 0, 0

        # This single paginated read is the biggest egress line item, so as of
        # v14.17 it does DOUBLE duty:
        #   prev_stock_state — per-SKU stock/baseline for change & stockout detection
        #   fop_done_ids     — the set of product_ids already present in the DB
        #
        # fop_done_ids replaces the old load_products_with_fop() call, which was a
        # SEPARATE full-catalog read of the products table per brand per run. A
        # product that already has a variant row here has been scraped before, so
        # its products.first_observed_price was set on first sighting — making
        # membership in this set a safe "already seeded" signal. The DB keeps its
        # `WHERE first_observed_price IS NULL` guard, so the honest baseline can
        # never be overwritten even in an edge case. product_id was added to the
        # SELECT purely to make this derivation possible (one extra small column
        # vs. an entire second catalog read — a clear net egress win).
        all_variant_rows, offset = [], 0
        while True:
            chunk = safe_db_execute(
                supabase.table("product_variants")
                .select("product_id, external_sku, is_in_stock, size, color, first_observed_price, last_updated_at, products!inner(brand)")
                .eq("products.brand", brand_name)
                .range(offset, offset + 999)
            )
            rows = chunk.data if (chunk and chunk.data) else []
            all_variant_rows.extend(rows)
            if len(rows) < 1000: break
            offset += 1000
        prev_stock_state = {row["external_sku"]: row for row in all_variant_rows}
        fop_done_ids     = {row["product_id"] for row in all_variant_rows if row.get("product_id")}

        if brand_config["engine"] == "shopify":
            seen, changes = scrape_shopify(supabase, session, brand_name, domain, today, prev_stock_state, fop_done_ids)
        elif brand_config["engine"] == "lcw_proxy":
            if not WEBSHARE_PROXY:
                print("  ⚠️ WEBSHARE credentials not set. Skipping LCW.")
                seen, changes = 0, 0
            else:
                seen, changes = scrape_lcw(supabase, session, brand_name, domain, today, prev_stock_state, fop_done_ids)
        elif brand_config["engine"] == "defacto":
            seen, changes = scrape_defacto(supabase, session, brand_name, domain, today, prev_stock_state, fop_done_ids)
        elif brand_config["engine"] == "woocommerce":
            seen, changes = scrape_woocommerce(supabase, session, brand_name, domain, today, prev_stock_state, fop_done_ids)
        else:
            seen, changes = 0, 0

        print(f"\n  ✅ {brand_name}: {seen} products scanned, {changes} price changes recorded.")
        return seen, changes
    except Exception as e:
        print(f"\n  🚨 CRITICAL FAILURE in {brand_name.upper()} pipeline: {e}")
        print(f"  ⚠️ Quarantining {brand_name} fault. Moving safely to next brand.")
        return 0, 0


if __name__ == "__main__":
    # SCRAPE_TARGET controls which brands this run processes.
    SCRAPE_TARGET = os.environ.get("SCRAPE_TARGET", "all").lower()
    if SCRAPE_TARGET == "shopify":
        active_brands = [b for b in BRANDS if b["engine"] in ("shopify", "defacto", "woocommerce")]
    elif SCRAPE_TARGET == "lcw":
        active_brands = [b for b in BRANDS if b["engine"] == "lcw_proxy"]
    elif SCRAPE_TARGET == "defacto":
        active_brands = [b for b in BRANDS if b["engine"] == "defacto"]
    elif SCRAPE_TARGET == "mobaco":
        active_brands = [b for b in BRANDS if b["engine"] == "woocommerce"]
    else:
        active_brands = BRANDS
    print(f"🚀 Khabar Scraper starting... target={SCRAPE_TARGET} ({len(active_brands)} brands)")
    startup_jitter = random.uniform(0, 30)
    print(f"  Startup jitter: {startup_jitter:.1f}s")
    time.sleep(startup_jitter)

    # v14.29: record today's USD→EGP rate (once per run, idempotent).
    # Runs on every target — FX context is useful regardless of which
    # brands this particular run processes.
    try:
        _fx_sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        _fx_session = get_resilient_session()
        fetch_and_store_fx_rate(_fx_sb, _fx_session)
    except Exception as e:
        print(f"  ⚠️ FX rate step failed (non-fatal): {e}")

    # NOTE: the price_events purge and stale-product/variant delisting that
    # used to run here (gated on SCRAPE_TARGET == "lcw") were moved to
    # housekeeping.py, its own weekly workflow. Two reasons: (1) this block
    # deleted price_events with no archive step first — the same mistake
    # already caught and fixed for price_snapshots; (2) tying maintenance
    # to the LCW run meant it silently didn't happen whenever LCW failed,
    # which is often. housekeeping.py archives-then-deletes on its own
    # schedule regardless of any scraper's daily luck.

    # v14.20: run each brand, track (seen, changes) per brand, and add two
    # defenses against the rate-limiting pattern seen in a real run where
    # Carina, 2S Egypt, Andora, Cizaro, and Mobaco all failed with 503/429
    # in the SAME run — different domains/platforms, so the common factor
    # was the runner's IP getting rate-limited by a burst of back-to-back
    # automated requests, not five independent site outages.
    #
    #   1. A small randomized pause BETWEEN brands (not just within a brand's
    #      own pagination loop) — spreads out the burst across the whole run
    #      instead of hammering one storefront, finishing instantly, then
    #      immediately hammering the next.
    #   2. An END-OF-RUN RETRY PASS: any brand that scanned 0 products (the
    #      real failure signal — see scrape_brand's v14.20 changelog for why
    #      this is now returned separately from `changes`) gets one more
    #      attempt AFTER every other brand has run. By then, several minutes
    #      have passed and whatever rate limit triggered the failure has
    #      almost certainly cleared — "all brands have had a chance to cool
    #      down" is the whole point, rather than retrying immediately while
    #      the same block is probably still active.
    brand_results = {}   # brand_name -> (seen, changes)
    failed_brands = []   # brands that scanned 0 products on the first pass

    for i, b in enumerate(active_brands):
        seen, changes = scrape_brand(b["name"], b["domain"])
        brand_results[b["name"]] = (seen, changes)
        if seen == 0:
            failed_brands.append(b)
        if i < len(active_brands) - 1:
            pause = random.uniform(8, 20)
            print(f"  Pausing {pause:.1f}s before next brand...")
            time.sleep(pause)

    if failed_brands:
        print(f"\n{'═'*55}")
        print(f"  🔁 RETRY PASS — {len(failed_brands)} brand(s) scanned 0 products on "
              f"the first attempt: {', '.join(b['name'] for b in failed_brands)}")
        print(f"  Waiting 90s for any rate-limit window to clear before retrying...")
        print(f"{'═'*55}")
        time.sleep(90)
        for i, b in enumerate(failed_brands):
            seen, changes = scrape_brand(b["name"], b["domain"])
            if seen > 0:
                print(f"  ✅ Retry succeeded for {b['name']}: {seen} products scanned.")
            else:
                print(f"  ⚠️ Retry still failed for {b['name']} — leaving for the next "
                      f"scheduled run rather than retrying indefinitely.")
            brand_results[b["name"]] = (seen, changes)
            if i < len(failed_brands) - 1:
                pause = random.uniform(8, 20)
                time.sleep(pause)

    total = sum(changes for seen, changes in brand_results.values())

    try:
        _sb2 = create_client(SUPABASE_URL, SUPABASE_KEY)
        flash_cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        downs = safe_db_execute(
            _sb2.table("price_events")
            .select("id, product_id, price_after, recorded_at")
            .eq("direction", "down")
            .eq("is_flash_sale", False)
            .gt("recorded_at", flash_cutoff)
        )
        if downs and downs.data:
            flash_count = 0
            for d in downs.data:
                upper_bound = (datetime.fromisoformat(d["recorded_at"]) + timedelta(hours=24)).isoformat()
                revert = safe_db_execute(
                    _sb2.table("price_events")
                    .select("id")
                    .eq("product_id", d["product_id"])
                    .eq("direction", "up")
                    .gt("recorded_at", d["recorded_at"])
                    .lt("recorded_at", upper_bound)
                    .limit(1)
                )
                if revert and revert.data:
                    safe_db_execute(
                        _sb2.table("price_events")
                        .update({"is_flash_sale": True})
                        .eq("id", d["id"])
                    )
                    flash_count += 1
            if flash_count:
                print(f"  ⚡ Detected {flash_count} flash sale events (L1·02).")

        oldest_snap = safe_db_execute(
            _sb2.table("price_snapshots")
            .select("snapshot_date")
            .order("snapshot_date", desc=False)
            .limit(1)
        )
        if oldest_snap and oldest_snap.data:
            first_date = oldest_snap.data[0]["snapshot_date"]
            days_of_data = (date.today() - date.fromisoformat(str(first_date))).days
            if days_of_data >= 30:
                stat_cutoff = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
                recent_events = safe_db_execute(
                    _sb2.table("price_events")
                    .select("id, product_id, price_after")
                    .eq("is_statistical_deal", False)
                    .eq("direction", "down")
                    .gt("recorded_at", stat_cutoff)
                )
                if recent_events and recent_events.data:
                    stat_count = 0
                    for ev in recent_events.data:
                        thirty_ago = str(date.today() - timedelta(days=30))
                        history = safe_db_execute(
                            _sb2.table("price_snapshots")
                            .select("price")
                            .eq("product_id", ev["product_id"])
                            .gte("snapshot_date", thirty_ago)
                            .order("snapshot_date", desc=False)
                        )
                        if history and history.data and len(history.data) >= 10:
                            prices = sorted(float(r["price"]) for r in history.data)
                            q1 = prices[len(prices) // 4]
                            q3 = prices[3 * len(prices) // 4]
                            iqr = q3 - q1
                            threshold = q1 - 1.5 * iqr
                            if float(ev["price_after"]) < threshold:
                                safe_db_execute(
                                    _sb2.table("price_events")
                                    .update({"is_statistical_deal": True})
                                    .eq("id", ev["id"])
                                )
                                stat_count += 1
                    if stat_count:
                        print(f"  📊 Detected {stat_count} statistical deal events (L1·07).")
    except Exception as e:
        print(f"  ⚠️ Post-run intelligence detection error: {e}")

    # v14.29: collect best-seller ranks for Shopify brands (post-run pass).
    # Only runs on targets that include Shopify brands, and only once daily
    # (idempotent — skips brands that already have today's ranks).
    if SCRAPE_TARGET in ("shopify", "all"):
        try:
            print(f"\n{'═'*55}")
            print(f"  🏆 BEST-SELLER RANK COLLECTION")
            print(f"{'═'*55}")
            _bs_sb = create_client(SUPABASE_URL, SUPABASE_KEY)
            _bs_session = get_resilient_session()
            collect_bestseller_ranks(_bs_sb, _bs_session)
        except Exception as e:
            print(f"  ⚠️ Best-seller rank collection failed (non-fatal): {e}")

    print(f"\n🏁 All done. Total price changes this run: {total}")
