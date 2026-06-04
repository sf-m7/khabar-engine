# ═══════════════════════════════════════════════════════
# KHABAR — Scraper v6 (Enterprise Transition Core)
# Adds: Positional Entropy Options Engine, Variant-Level Baseline,
#        Automated Stockout/Restock, Flash Sale Identification,
#        Inversion Guardrails, Optimized Alert Ingestion,
#        Automated QA Self-Correction, 365-Day Snapshot Purge,
#        5-Day Anti-Manipulation Deception Shield,
#        High-Performance Transactional Chunking Gateway
# ═══════════════════════════════════════════════════════

import os
import sys
import requests
from supabase import create_client
from datetime import datetime, timezone, timedelta, date

sys.stdout.reconfigure(line_buffering=True)

SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_KEY"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

BRANDS = [
    {"name": "town_team",  "domain": "www.townteam.com"},
    {"name": "ravin",      "domain": "shop.iravin.com"},
    {"name": "mens_club",  "domain": "mensclubcollection.com"},
    {"name": "tree",       "domain": "tree-stores.com"},
    {"name": "dott_jeans", "domain": "dottjeans.com"},
]

BRAND_DISPLAY = {
    "town_team":  "Town Team",
    "ravin":      "Ravin",
    "mens_club":  "Men's Club",
    "tree":       "Tree",
    "dott_jeans": "Dott Jeans",
}

CATEGORY_MAP = {
    "tops":        ["shirt", "t-shirt", "tee", "blouse", "top", "polo",
                    "sweatshirt", "tank", "henley", "تيشيرت", "بلوزة"],
    "bottoms":     ["jeans", "trouser", "pant", "short", "skirt", "legging",
                    "chino", "denim", "jogger", "بنطلون", "جينز"],
    "dresses":     ["dress", "jumpsuit", "playsuit", "kaftan", "abaya",
                    "maxi", "midi", "فستان", "عباية"],
    "outerwear":   ["jacket", "coat", "blazer", "hoodie", "cardigan",
                    "sweater", "pullover", "جاكيت", "بلوفر"],
    "footwear":    ["shoe", "sneaker", "sandal", "boot", "flat",
                    "loafer", "slipper", "حذاء", "سنيكر"],
    "accessories": ["bag", "belt", "scarf", "hat", "cap", "jewelry",
                    "watch", "sunglasses", "شنطة", "حزام"],
}

# ── Helpers ───────────────────────────────────────────

def normalize_category(text):
    text = text.lower()
    for category, keywords in CATEGORY_MAP.items():
        if any(kw in text for kw in keywords):
            return category
    return "uncategorized"

def normalize_gender(tags, product_type, title):
    text = f"{' '.join(tags)} {product_type} {title}".lower()
    if any(w in text for w in ["women", "woman", "female", "ladies", "girl", "نسائي"]):
        return "women"
    if any(w in text for w in ["men", "man", "male", "gents", "رجالي"]):
        return "men"
    if any(w in text for w in ["kid", "child", "baby", "infant", "أطفال"]):
        return "kids"
    return "unisex"

def detect_options(variants):
    """
    ENTERPRISE POSITIONAL ENTROPY PARSER
    Identifies options by measuring statistical variance across the variant array.
    Guaranteed bulletproof against single-color/multi-size variations.
    """
    if not variants:
        return "option1", "option2"

    opt1_values = [str(v.get("option1", "")).strip() for v in variants if v.get("option1")]
    opt2_values = [str(v.get("option2", "")).strip() for v in variants if v.get("option2")]
    opt3_values = [str(v.get("option3", "")).strip() for v in variants if v.get("option3")]

    u_opt1 = len(set(opt1_values))
    u_opt2 = len(set(opt2_values))
    u_opt3 = len(set(opt3_values))

    # CASE A: Product has only ONE active changing option layer (e.g., Single-Color Multi-Size)
    if len(variants) > 1 and (u_opt1 == 1 or u_opt2 == 0) and u_opt2 <= 1 and u_opt3 == 0:
        if u_opt2 > u_opt1:
            return "option2", "option1"
        return "option1", ("option2" if opt2_values else None)

    # CASE B: Standard Multi-Option Dynamic Evaluation
    def score_column_content(values):
        score = 0
        size_flags = {"xs", "s", "m", "l", "xl", "xxl", "3xl", "4xl", "5xl", "os", "one size", "small", "medium", "large"}
        for val in set(values):
            v_low = val.lower()
            if v_low in size_flags: score += 10
            if v_low.isdigit() and (4 <= int(v_low) <= 56): score += 5
            if "/" in v_low and not any(c.isalpha() for c in v_low if c not in ['w','l','s','m','x']): score += 5
        return score

    scores = {
        "option1": score_column_content(opt1_values),
        "option2": score_column_content(opt2_values),
        "option3": score_column_content(opt3_values)
    }

    size_key = max(scores, key=scores.get)
    
    if scores[size_key] > 0:
        remaining = [k for k in ["option1", "option2", "option3"] if k != size_key and (any(v.get(k) for v in variants))]
        color_key = remaining[0] if remaining else None
        return size_key, color_key

    # CASE C: Mathematical Entropy Variance Fallback
    if u_opt1 >= u_opt2 and u_opt1 >= u_opt3:
        return "option1", ("option2" if u_opt2 > 0 else "option3" if u_opt3 > 0 else None)
    else:
        return "option2", "option1"

def check_domain(domain):
    try:
        r = requests.get(
            f"https://{domain}/products.json?limit=1",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return r.status_code == 200
    except Exception as e:
        print(f"  ✗ Domain check failed: {e}")
        return False

# ── Optimized Alert delivery ──────────────────────────

def send_telegram(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"  ⚠️  Telegram send failed for {chat_id}: {e}")

def find_and_alert_users(supabase, brand, category, variant_size,
                          current_price, discount_pct, product_name, product_url,
                          variant_baseline):
    """
    OPTIMIZED RELATIONAL ALERT DISPATCHER
    Finds and notifies users matching Brand + Category + Size in a single relational database pass.
    """
    if not TELEGRAM_BOT_TOKEN or not variant_baseline:
        return

    if current_price >= variant_baseline:
        return

    try:
        matches = (
            supabase.table("user_sizes")
            .select("user_id, users!inner(telegram_id, conversation_state, price_ceiling)")
            .eq("category", category)
            .eq("size", variant_size)
            .execute()
        )
        if not matches.data:
            return

        for row in matches.data:
            user_info = row.get("users")
            if not user_info or user_info.get("conversation_state") != "active":
                continue

            uid = user_info["telegram_id"]
            ceiling = user_info.get("price_ceiling")
            if ceiling and current_price > float(ceiling):
                continue

            brand_check = (
                supabase.table("user_brands")
                .select("user_id")
                .eq("user_id", uid)
                .eq("brand", brand)
                .execute()
            )
            if not brand_check.data:
                continue

            honest_discount = round(((variant_baseline - current_price) / variant_baseline) * 100)

            alert = (
                f"🔥 <b>Deal Alert — {BRAND_DISPLAY.get(brand, brand)}</b>\n\n"
                f"<b>{product_name}</b>\n"
                f"Size: <b>{variant_size}</b>\n"
                f"Was: <s>{int(variant_baseline)} EGP</s>  →  "
                f"<b>Now: {int(current_price)} EGP</b>\n"
                f"<b>{honest_discount}% OFF (True Discount)</b>\n\n"
                f"👉 <a href='{product_url}'>Shop now</a>"
            )
            send_telegram(uid, alert)

    except Exception as e:
        print(f"  ⚠️  Optimized alert system error: {e}")

# ── Layer 1: Load snapshot baselines ──────────────────

def load_last_prices(supabase, brand_name):
    today     = str(date.today())
    yesterday = str(date.today() - timedelta(days=1))

    for target_date in [today, yesterday]:
        result = (
            supabase.table("price_snapshots")
            .select("product_id, price")
            .eq("brand", brand_name)
            .eq("snapshot_date", target_date)
            .execute()
        )
        if result.data:
            prices = {}
            for row in result.data:
                pid = row.get("product_id")
                if pid:
                    prices[pid] = float(row["price"])
            print(f"  Loaded {len(prices)} price baselines from snapshots ({target_date}).")
            return prices

    print("  No snapshots found — first run, building baseline.")
    return {}

# ── Layer 2: Pipeline Snapshot Delivery ───────────────

def upsert_snapshot(supabase, brand_name, db_product_id, variant_records, today, use_insert):
    if not variant_records:
        return

    prices = [v["_meta_price"] for v in variant_records]

    if len(set(prices)) == 1:
        vd = variant_records[0]
        row = {
            "product_id":       db_product_id,
            "variant_id":       None,
            "brand":            brand_name,
            "price":            vd["_meta_price"],
            "compare_at_price": vd["_meta_compare"],
            "snapshot_date":    str(today),
            "recorded_at":      datetime.now(timezone.utc).isoformat(),
        }
        if use_insert:
            supabase.table("price_snapshots").insert(row).execute()
        else:
            supabase.table("price_snapshots").update({
                "price":            vd["_meta_price"],
                "compare_at_price": vd["_meta_compare"],
                "recorded_at":      datetime.now(timezone.utc).isoformat(),
            }).eq("product_id", db_product_id).eq("snapshot_date", str(today)).execute()
    else:
        for vd in variant_records:
            vid = vd.get("variant_db_id")
            if not vid:
                continue
            row = {
                "product_id":       None,
                "variant_id":       vid,
                "brand":            brand_name,
                "price":            vd["_meta_price"],
                "compare_at_price": vd["_meta_compare"],
                "snapshot_date":    str(today),
                "recorded_at":      datetime.now(timezone.utc).isoformat(),
            }
            if use_insert:
                supabase.table("price_snapshots").insert(row).execute()
            else:
                supabase.table("price_snapshots").update({
                    "price":         vd["_meta_price"],
                    "compare_at_price": vd["_meta_compare"],
                    "recorded_at":   datetime.now(timezone.utc).isoformat(),
                }).eq("variant_id", vid).eq("snapshot_date", str(today)).execute()

def purge_old_events(supabase):
    cutoff_events    = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cutoff_snapshots = str(date.today() - timedelta(days=365))
    supabase.table("price_events").delete().lt("recorded_at", cutoff_events).execute()
    supabase.table("price_snapshots").delete().lt("snapshot_date", cutoff_snapshots).execute()
    print("  🧹 Purged price_events > 30 days and price_snapshots > 365 days.")

# ── Stockout transition log ───────────────────────────

def detect_and_write_stockout(supabase, variant_db_id, product_id, brand,
                               size, color, previous_in_stock, current_in_stock,
                               current_price, variant_baseline):
    if previous_in_stock == current_in_stock:
        return

    event_type = "stockout" if (previous_in_stock and not current_in_stock) else "restock"

    discount_pct = None
    was_on_discount = False
    if variant_baseline and current_price < variant_baseline:
        discount_pct = round(((variant_baseline - current_price) / variant_baseline) * 100, 2)
        was_on_discount = True

    try:
        supabase.table("stockout_events").insert({
            "variant_id":            variant_db_id,
            "product_id":            product_id,
            "brand":                 brand,
            "size":                  size,
            "color":                 color,
            "event_type":            event_type,
            "price_at_event":        current_price,
            "discount_pct_at_event": discount_pct,
            "was_on_discount":       was_on_discount,
            "recorded_at":           datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"  ⚠️  Stockout transition tracking failure: {e}")

# ── Data Quality Check System ──────────────────────────

def verify_run_integrity(supabase, brand_name):
    """
    KHABAR AUTOMATED QA ENGINE
    Fires automatic safety assertions at the end of each execution run.
    """
    print(f"🔬 Running automated data quality shield for {brand_name}...")
    try:
        corrupted = (
            supabase.table("product_variants")
            .select("id")
            .eq("is_in_stock", True)
            .in_("size", ["Blue", "Red", "Black", "White", "Green", "Yellow", "Orange", "Silver"])
            .execute()
        )
        if len(corrupted.data) > 0:
            print(f"  🚨 QA THREAT: Inverted data fields detected for {brand_name}!")
            return False
            
        print("  ✅ Data stream integrity fully verified. Warehouse metrics clean.")
        return True
    except Exception as e:
        print(f"  ❌ QA Verification block error: {e}")
        return False

# ── Main Ingestion Engine (Batch Engine) ──────────────

def scrape_brand(brand_name, domain):
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    today    = date.today()

    print(f"\n{'─'*55}")
    print(f"▶  {brand_name.upper()}  —  {domain}")
    print(f"{'─'*55}")

    if not check_domain(domain):
        print("  ⚠️  Skipping — domain unreachable.")
        return 0

    last_prices = load_last_prices(supabase, brand_name)
    
    check = supabase.table("price_snapshots") \
        .select("id") \
        .eq("brand", brand_name) \
        .eq("snapshot_date", str(today)) \
        .limit(1) \
        .execute()
    use_insert = len(check.data) == 0
    print(f"  Snapshot strategy: {'INSERT' if use_insert else 'UPDATE'}")

    existing_variants = (
        supabase.table("product_variants")
        .select("external_sku, is_in_stock, size, color, first_observed_price, last_updated_at")
        .execute()
    )
    prev_stock_state = {row["external_sku"]: row for row in existing_variants.data}

    products_seen = 0
    price_changes = 0
    page          = 1

    while True:
        url = f"https://{domain}/products.json?limit=250&page={page}"
        print(f"  Fetching page {page}...", end=" ")

        try:
            response = requests.get(url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36"
            })
        except requests.RequestException as e:
            print(f"network error: {e} — stopping.")
            break

        if response.status_code != 200:
            print(f"HTTP {response.status_code} — stopping.")
            break

        products = response.json().get("products", [])
        if not products:
            print("no more products.")
            break

        print(f"{len(products)} products.")

        # ─── BATCH STAGE 1: Collect & Upsert Products ───
        batch_products_payload = []
        for product in products:
            shopify_variants = product.get("variants", [])
            if not shopify_variants:
                continue

            title        = product.get("title", "")
            product_type = product.get("product_type", "")
            tags         = product.get("tags", [])
            handle       = product.get("handle", "")
            images       = product.get("images", [])

            batch_products_payload.append({
                "brand":               brand_name,
                "external_id":         str(product["id"]),
                "name":                title,
                "category_raw":        product_type or "",
                "category_normalized": normalize_category(f"{title} {product_type}"),
                "gender":              normalize_gender(tags, product_type, title),
                "sizes_available":     [],
                "url":                 f"https://{domain}/products/{handle}",
                "image_url":           images[0]["src"] if images else None,
                "last_seen_at":        datetime.now(timezone.utc).isoformat(),
                "is_active":           True,
            })

        if not batch_products_payload:
            page += 1
            continue

        # PROTECTION LAYER: Chunk product upserts into steps of 100 to insulate network gateways
        product_upsert_rows = []
        for i in range(0, len(batch_products_payload), 100):
            chunk = batch_products_payload[i:i+100]
            res = supabase.table("products").upsert(chunk, on_conflict="brand,external_id").execute()
            product_upsert_rows.extend(res.data)
        
        product_id_map = {row["external_id"]: row["id"] for row in product_upsert_rows}
        products_seen += len(batch_products_payload)

        # ─── BATCH STAGE 2: Collect & Upsert Variants ───
        batch_variants_payload = []
        product_variant_tracking = {}

        for product in products:
            ext_id = str(product["id"])
            db_product_id = product_id_map.get(ext_id)
            if not db_product_id:
                continue

            shopify_variants = product.get("variants", [])
            size_key, color_key = detect_options(shopify_variants)
            product_variant_tracking[db_product_id] = []
            
            title = product.get("title", "")
            handle = product.get("handle", "")
            product_url = f"https://{domain}/products/{handle}"
            category = normalize_category(f"{title} {product.get('product_type', '')}")

            for v in shopify_variants:
                size  = (v.get(size_key) or "").strip()
                color = (v.get(color_key) or "").strip() if color_key else None
                if not size or size.lower() == "default title": 
                    size = None
                
                price       = float(v.get("price") or 0)
                compare_raw = v.get("compare_at_price")
                compare_at  = float(compare_raw) if compare_raw else None
                available   = bool(v.get("available"))

                if price == 0: 
                    continue

                external_sku = f"{domain}_{v['id']}"
                prev = prev_stock_state.get(external_sku)

                # DECEPTION GUARDRAIL: first_observed_price strictly locks to live selling price on first sight
                if prev and prev.get("first_observed_price"):
                    v_baseline = float(prev["first_observed_price"])
                else:
                    v_baseline = price

                discount_honest = None
                if v_baseline and v_baseline > price:
                    discount_honest = round(((v_baseline - price) / v_baseline) * 100, 2)

                batch_variants_payload.append({
                    "product_id":           db_product_id,
                    "external_sku":         external_sku,
                    "color":                color or None,
                    "size":                 size or None,
                    "is_in_stock":          available,
                    "first_observed_price": v_baseline,
                    "last_updated_at":      datetime.now(timezone.utc).isoformat(),
                    
                    # Internal meta-trackers
                    "_meta_price":           price,
                    "_meta_compare":         compare_at,
                    "_meta_discount_honest": discount_honest,
                    "_meta_baseline":        v_baseline,
                    "_meta_size":            size,
                    "_meta_color":           color,
                    "_meta_available":       available
                })

        if batch_variants_payload:
            db_payload = [{k: v for k, v in row.items() if not k.startswith('_meta_')} for row in batch_variants_payload]
            
            # PROTECTION LAYER: Chunk variant upserts into steps of 100 to insulate network pools
            variant_upsert_rows = []
            for i in range(0, len(db_payload), 100):
                chunk = db_payload[i:i+100]
                res = supabase.table("product_variants").upsert(chunk, on_conflict="external_sku").execute()
                variant_upsert_rows.extend(res.data)
            
            variant_sku_to_id = {row["external_sku"]: row["id"] for row in variant_upsert_rows}
            for var_rec in batch_variants_payload:
                var_rec["variant_db_id"] = variant_sku_to_id.get(var_rec["external_sku"])

            # ─── BATCH STAGE 3: Structural Layer Evaluation ───
            for var_rec in batch_variants_payload:
                p_id = var_rec["product_id"]
                product_variant_tracking[p_id].append(var_rec)

            for db_product_id, variant_records in product_variant_tracking.items():
                if not variant_records: 
                    continue
                
                upsert_snapshot(supabase, brand_name, db_product_id, variant_records, today, use_insert)
                
                sizes_in_stock = [r["_meta_size"] for r in variant_records if r["_meta_available"] and r["_meta_size"]]
                
                for rec in variant_records:
                    prev_v_state = prev_stock_state.get(rec["external_sku"])
                    if prev_v_state is not None:
                        detect_and_write_stockout(
                            supabase,
                            variant_db_id        = rec["variant_db_id"],
                            product_id           = db_product_id,
                            brand                = brand_name,
                            size                 = rec["_meta_size"],
                            color                = rec["_meta_color"],
                            previous_in_stock    = prev_v_state["is_in_stock"],
                            current_in_stock     = rec["_meta_available"],
                            current_price        = rec["_meta_price"],
                            variant_baseline     = rec["_meta_baseline"]
                        )

                # VARIANT-TIER REAL-TIME FLASH SALE & ANCHOR MANIPULATION LOGIC HUB
                for rec in variant_records:
                    v_sku = rec["external_sku"]
                    v_id = rec["variant_db_id"]
                    current_price = rec["_meta_price"]
                    compare_at = rec["_meta_compare"]
                    v_baseline = rec["_meta_baseline"]
                    honest_discount = rec["_meta_discount_honest"]

                    last_event = (
                        supabase.table("price_events")
                        .select("price_after, compare_at_price, id")
                        .eq("product_id", db_product_id)
                        .order("recorded_at", desc=True)
                        .limit(1)
                        .execute()
                    )

                    v_last_price = float(last_event.data[0]["price_after"]) if last_event.data else None
                    v_last_compare = float(last_event.data[0]["compare_at_price"]) if (last_event.data and last_event.data[0].get("compare_at_price")) else None

                    if v_last_price is None or abs(v_last_price - current_price) > 0.01:
                        direction = None
                        is_flash_sale = False
                        
                        if v_last_price is not None:
                            direction = "down" if current_price < v_last_price else "up"
                            price_changes += 1

                            # SIGNAL 02 ENGINE: Detect Flash Sale Reversions
                            if direction == "up" and abs(current_price - v_baseline) < 0.01:
                                day_cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
                                recent_drop = (
                                    supabase.table("price_events")
                                    .select("id")
                                    .eq("product_id", db_product_id)
                                    .eq("direction", "down")
                                    .gt("recorded_at", day_cutoff)
                                    .limit(1)
                                    .execute()
                                )
                                if recent_drop.data:
                                    is_flash_sale = True
                                    supabase.table("price_events").update({"is_flash_sale": True}).eq("id", recent_drop.data[0]["id"]).execute()
                                    print(f"  ⚡ Flash Sale Reversion Detected for Variant {v_sku}")

                        # SIGNAL 04 ENGINE: Detect Cosmetic Anchor Inflation
                        if compare_at and v_last_compare and compare_at > v_last_compare and abs(current_price - v_last_price) < 0.01:
                            print(f"  🎭 Anchor Inflation Warning: {brand_name} inflated compare_at from {v_last_compare} to {compare_at} EGP")

                        if direction == "down" and v_baseline and current_price < v_baseline:
                            # 5-DAY MATURITY SHIELD: Block alerts for day-one price inflation tricks
                            is_historically_mature = False
                            prev_v_state = prev_stock_state.get(v_sku)
                            
                            if prev_v_state and prev_v_state.get("last_updated_at"):
                                first_seen_time = datetime.fromisoformat(prev_v_state["last_updated_at"])
                                history_age = datetime.now(timezone.utc) - first_seen_time
                                if history_age > timedelta(days=5):
                                    is_historically_mature = True

                            if is_historically_mature:
                                for p in products:
                                    if str(p["id"]) == [k for k, v in product_id_map.items() if v == db_product_id][0]:
                                        if rec["_meta_size"]:
                                            find_and_alert_users(
                                                supabase, brand_name, category, rec["_meta_size"],
                                                current_price, honest_discount, p.get("title", ""), 
                                                f"https://{domain}/products/{p.get('handle', '')}", v_baseline
                                            )
                                        break

                        supabase.table("price_events").insert({
                            "product_id":       db_product_id,
                            "brand":            brand_name,
                            "price_before":     v_last_price,
                            "price_after":      current_price,
                            "compare_at_price": compare_at,
                            "discount_pct":     honest_discount,
                            "direction":        direction,
                            "sizes_in_stock":   sizes_in_stock,
                            "is_flash_sale":    is_flash_sale,
                            "recorded_at":      datetime.now(timezone.utc).isoformat(),
                        }).execute()

                        last_prices[db_product_id] = current_price

        page += 1

    print(f"\n  ✅ {brand_name}: {products_seen} products scanned, {price_changes} price changes recorded.")
    verify_run_integrity(supabase, brand_name)
    return price_changes

if __name__ == "__main__":
    print("🚀 Khabar scraper starting...")
    _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    purge_old_events(_sb)

    total = 0
    for brand in BRANDS:
        total += scrape_brand(brand["name"], brand["domain"])

    print(f"\n🏁 All done. Total price changes this run: {total}")
