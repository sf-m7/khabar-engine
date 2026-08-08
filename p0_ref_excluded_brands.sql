-- ============================================================================
-- P0 · ref_excluded_brands — SINGLE SOURCE of consumption-layer brand exclusions
-- Apply via the GitHub Actions psql workflow (MCP role is DDL-denied by design).
-- Idempotent: safe to re-run. Replaces the hardcoded EXCLUDE_BRANDS set in
-- report_lib.py (which becomes a lookup) and is readable by future SQL consumers
-- (e.g. the chatbot's vetted SQL) so Python and SQL share ONE exclusion source.
--
-- scope semantics:
--   'all'   → drop from EVERY report (phantom / collection-artifact brands)
--   'stock' → drop only from STOCK/size-based reports (price data is fine)
-- ============================================================================

CREATE TABLE IF NOT EXISTS ref_excluded_brands (
    brand     text PRIMARY KEY,
    scope     text NOT NULL CHECK (scope IN ('all','stock')),
    reason    text,
    added_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO ref_excluded_brands (brand, scope, reason) VALUES
    ('tree',       'all',   'phantom — shares Dalydress 06-18 mass-delist date; retained upstream pending review, dropped from client output'),
    ('dalydress',  'all',   'phantom — 06-18 collection bug (68% catalogue false-delist); still leaks into product_l2_* on normal days'),
    ('lc_waikiki', 'stock', 'per-size stock fabricated (0.1% mixed-stock vs ~78% for real per-size brands)'),
    ('defacto',    'stock', 'API returns null per-size stock; zero witnessed inventory events'),
    ('mobaco',     'stock', 'stock reads unreliable (Cloudflare JS challenge on variations)')
ON CONFLICT (brand) DO UPDATE
    SET scope = EXCLUDED.scope, reason = EXCLUDED.reason;

-- Verify:
--   SELECT brand, scope FROM ref_excluded_brands ORDER BY scope, brand;
