-- Khabar → PlanetScale: indexes, foreign keys, identity resets. Run AFTER data load.

-- ---------- Indexes (non-primary-key) ----------
CREATE INDEX idx_alert_queue_send_at ON public.alert_queue USING btree (send_at) WHERE (sent = false);
CREATE UNIQUE INDEX bestseller_rank_product_id_snapshot_date_key ON public.bestseller_rank USING btree (product_id, snapshot_date);
CREATE INDEX idx_bestseller_brand_date ON public.bestseller_rank USING btree (brand, snapshot_date);
CREATE INDEX idx_bestseller_date ON public.bestseller_rank USING btree (snapshot_date);
CREATE UNIQUE INDEX category_map_match_type_pattern_key ON public.category_map USING btree (match_type, pattern);
CREATE UNIQUE INDEX fx_rate_rate_date_key ON public.fx_rate USING btree (rate_date);
CREATE INDEX idx_lcw_progress_date_status ON public.lcw_scrape_progress USING btree (scrape_date, status);
CREATE UNIQUE INDEX lcw_scrape_progress_date_slot_cat_key ON public.lcw_scrape_progress USING btree (scrape_date, slot, category_id);
CREATE INDEX idx_meta_ad_brand_date ON public.meta_ad_activity USING btree (brand, snapshot_date);
CREATE UNIQUE INDEX meta_ad_activity_brand_snapshot_date_key ON public.meta_ad_activity USING btree (brand, snapshot_date);
CREATE INDEX idx_price_events_brand ON public.price_events USING btree (brand);
CREATE INDEX idx_price_events_discount ON public.price_events USING btree (discount_pct DESC) WHERE (direction = 'down'::text);
CREATE INDEX idx_price_events_product ON public.price_events USING btree (product_id);
CREATE INDEX idx_price_events_recorded ON public.price_events USING btree (recorded_at DESC);
CREATE UNIQUE INDEX idx_snapshot_product_day ON public.price_snapshots USING btree (product_id, snapshot_date) WHERE (product_id IS NOT NULL);
CREATE UNIQUE INDEX idx_snapshot_variant_day ON public.price_snapshots USING btree (variant_id, snapshot_date) WHERE (variant_id IS NOT NULL);
CREATE INDEX idx_snapshots_brand_date ON public.price_snapshots USING btree (brand, snapshot_date DESC);
CREATE INDEX idx_snapshots_date ON public.price_snapshots USING btree (snapshot_date);
CREATE INDEX idx_variants_color_lower ON public.product_variants USING btree (lower(TRIM(BOTH FROM color)));
CREATE INDEX idx_variants_delisted ON public.product_variants USING btree (delisted_at) WHERE (delisted_at IS NOT NULL);
CREATE INDEX idx_variants_product ON public.product_variants USING btree (product_id);
CREATE INDEX idx_variants_size ON public.product_variants USING btree (size);
CREATE UNIQUE INDEX product_variants_external_sku_key ON public.product_variants USING btree (external_sku);
CREATE INDEX idx_products_attributes_extracted ON public.products USING gin (attributes_extracted);
CREATE INDEX idx_products_brand ON public.products USING btree (brand);
CREATE INDEX idx_products_brand_category ON public.products USING btree (brand, category_normalized);
CREATE INDEX idx_products_delisted ON public.products USING btree (delisted_at) WHERE (delisted_at IS NOT NULL);
CREATE UNIQUE INDEX products_brand_external_id_key ON public.products USING btree (brand, external_id);
CREATE INDEX idx_health_brand_time ON public.scraper_health_log USING btree (brand, checked_at DESC);
CREATE INDEX idx_l1_01_brand ON public.signal_l1_01_genuine_price_drop USING btree (brand);
CREATE INDEX idx_l1_01_drop ON public.signal_l1_01_genuine_price_drop USING btree (drop_pct DESC);
CREATE UNIQUE INDEX signal_l1_01_genuine_price_drop_product_id_snapshot_date_key ON public.signal_l1_01_genuine_price_drop USING btree (product_id, snapshot_date);
CREATE INDEX idx_l1_03_brand ON public.signal_l1_03_price_staircase USING btree (brand);
CREATE INDEX idx_l1_03_descent ON public.signal_l1_03_price_staircase USING btree (total_descent_pct DESC);
CREATE UNIQUE INDEX signal_l1_03_price_staircase_product_id_snapshot_date_key ON public.signal_l1_03_price_staircase USING btree (product_id, snapshot_date);
CREATE INDEX idx_l107_band ON public.signal_l1_07_price_anomaly USING btree (brand, snapshot_date, is_band_move);
CREATE INDEX idx_l107_brand ON public.signal_l1_07_price_anomaly USING btree (brand, snapshot_date DESC);
CREATE INDEX idx_l107_date ON public.signal_l1_07_price_anomaly USING btree (snapshot_date DESC);
CREATE UNIQUE INDEX signal_l1_07_price_anomaly_product_id_snapshot_date_key ON public.signal_l1_07_price_anomaly USING btree (product_id, snapshot_date);
CREATE INDEX idx_l1_17_brand ON public.signal_l1_17_depth_escalation USING btree (brand);
CREATE INDEX idx_l1_17_depth ON public.signal_l1_17_depth_escalation USING btree (last_depth_pct DESC);
CREATE UNIQUE INDEX signal_l1_17_depth_escalation_product_id_snapshot_date_key ON public.signal_l1_17_depth_escalation USING btree (product_id, snapshot_date);
CREATE INDEX idx_sr_signal ON public.signal_runs USING btree (signal_id, run_at DESC);
CREATE INDEX idx_sr_status ON public.signal_runs USING btree (status, run_at DESC);
CREATE INDEX idx_se_brand ON public.stockout_events USING btree (brand);
CREATE INDEX idx_se_recorded_at ON public.stockout_events USING btree (recorded_at);
CREATE INDEX idx_se_type ON public.stockout_events USING btree (event_type);
CREATE INDEX idx_se_variant ON public.stockout_events USING btree (variant_id);
CREATE INDEX idx_se_variant_time ON public.stockout_events USING btree (variant_id, recorded_at);
CREATE INDEX idx_se_witnessed ON public.stockout_events USING btree (witnessed, brand, event_type);
CREATE INDEX idx_user_brands_brand ON public.user_brands USING btree (brand);
CREATE INDEX idx_user_sizes_lookup ON public.user_sizes USING btree (category, size);
CREATE UNIQUE INDEX users_referral_code_key ON public.users USING btree (referral_code);
CREATE INDEX idx_weekly_bestseller_brand_week ON public.weekly_bestseller_summary USING btree (brand, week_start);
CREATE INDEX idx_weekly_bestseller_product ON public.weekly_bestseller_summary USING btree (product_id, week_start);
CREATE UNIQUE INDEX weekly_bestseller_summary_product_week_key ON public.weekly_bestseller_summary USING btree (product_id, week_start);
CREATE INDEX idx_wps_brand_week ON public.weekly_product_summary USING btree (brand, week_start);
CREATE INDEX idx_wps_cat_week ON public.weekly_product_summary USING btree (category_normalized, week_start);
CREATE UNIQUE INDEX uq_wps_product_week ON public.weekly_product_summary USING btree (product_id, week_start);
CREATE INDEX idx_wve_product_week ON public.weekly_variant_exception USING btree (product_id, week_start);
CREATE UNIQUE INDEX uq_wve_variant_week ON public.weekly_variant_exception USING btree (variant_id, week_start);

-- ---------- Foreign keys (with delete rules preserved) ----------
ALTER TABLE public.alert_queue ADD CONSTRAINT alert_queue_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(telegram_id);
ALTER TABLE public.alert_queue ADD CONSTRAINT alert_queue_price_event_id_fkey FOREIGN KEY (price_event_id) REFERENCES public.price_events(id);
ALTER TABLE public.bestseller_rank ADD CONSTRAINT bestseller_rank_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;
ALTER TABLE public.price_events ADD CONSTRAINT price_events_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id);
ALTER TABLE public.price_snapshots ADD CONSTRAINT price_snapshots_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;
ALTER TABLE public.price_snapshots ADD CONSTRAINT price_snapshots_variant_id_fkey FOREIGN KEY (variant_id) REFERENCES public.product_variants(id) ON DELETE CASCADE;
ALTER TABLE public.product_variants ADD CONSTRAINT product_variants_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;
ALTER TABLE public.signal_l1_01_genuine_price_drop ADD CONSTRAINT signal_l1_01_genuine_price_drop_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;
ALTER TABLE public.signal_l1_03_price_staircase ADD CONSTRAINT signal_l1_03_price_staircase_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;
ALTER TABLE public.signal_l1_17_depth_escalation ADD CONSTRAINT signal_l1_17_depth_escalation_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;
ALTER TABLE public.stockout_events ADD CONSTRAINT stockout_events_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;
ALTER TABLE public.stockout_events ADD CONSTRAINT stockout_events_variant_id_fkey FOREIGN KEY (variant_id) REFERENCES public.product_variants(id) ON DELETE CASCADE;
ALTER TABLE public.user_brands ADD CONSTRAINT user_brands_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(telegram_id) ON DELETE CASCADE;
ALTER TABLE public.user_sizes ADD CONSTRAINT user_sizes_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(telegram_id) ON DELETE CASCADE;
ALTER TABLE public.weekly_product_summary ADD CONSTRAINT weekly_product_summary_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;
ALTER TABLE public.weekly_variant_exception ADD CONSTRAINT weekly_variant_exception_product_id_fkey FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;
ALTER TABLE public.weekly_variant_exception ADD CONSTRAINT weekly_variant_exception_variant_id_fkey FOREIGN KEY (variant_id) REFERENCES public.product_variants(id) ON DELETE CASCADE;

-- ---------- Reset identity counters so new inserts don't collide with migrated IDs ----------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'products','price_events','alert_queue','sale_periods','product_variants','price_snapshots',
    'stockout_events','weekly_product_summary','weekly_variant_exception','scraper_health_log',
    'bestseller_rank','fx_rate','meta_ad_activity','category_map','weekly_bestseller_summary',
    'signal_runs','signal_l1_07_price_anomaly','signal_l1_01_genuine_price_drop',
    'signal_l1_03_price_staircase','signal_l1_17_depth_escalation','product_runs','taxonomy_audit',
    'size_review_queue'
  ] LOOP
    EXECUTE format(
      'SELECT setval(pg_get_serial_sequence(''public.%I'',''id''), COALESCE((SELECT max(id) FROM public.%I),1), (SELECT count(*)>0 FROM public.%I))',
      t, t, t);
  END LOOP;
END $$;
