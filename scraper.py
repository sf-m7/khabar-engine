# ═══════════════════════════════════════════════════════
# KHABAR — Scraper v6 (Enterprise Transition Core)
# Adds: Variant-Level Baseline, Automated Stockout/Restock,
#        Inversion Guardrails, Optimized Alert Ingestion,
#        365-Day Snapshot Purge Cycles
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

SIZE_KEYWORDS = {
    "xs", "s", "m", "l", "xl", "xxl", "xxxl", "2xl", "3xl", "4xl",
    "small", "medium", "large", "x-large", "xx-large",
    "os", "one size", "free size", "onesize", "فري", "فري سايز",
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

def looks_like_size(value):
    if not value:
        return False
    v = value.strip().lower()
    if v in SIZE_KEYWORDS:
        return True
    if v.isdigit() and 20 <= int(v) <= 50:
        return True
    if "/" in v:
        return True
    return False

def detect_options(variants):
    sample = variants[:8]
    opt1 = [v.get("option1", "") for v in sample]
    opt2 = [v.get("option2", "") for v in sample]
    opt3 = [v.get("option3", "") for v in sample]

    if any(looks_like_size(v) for v in opt1):
        return "option1", ("option2" if any(opt2) else None)
    elif any(looks_like_size(v) for v in opt2):
        return "option2", ("option1" if any(opt1) else None)
    elif any(looks_like_size(v) for v in opt3):
        return "option3", ("option1" if any(opt1) else None)
    else:
        return "option1", ("option2" if any(opt2) else None)

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
            "discount_pct":     vd["_meta_discount_honest"],
            "snapshot_date":    str(today),
            "recorded_at":      datetime.now(timezone.utc).isoformat(),
        }
        if use_insert:
            supabase.table("price_snapshots").insert(row).execute()
        else:
            supabase.table("price_snapshots").update({
                "price":            vd["_meta_price"],
                "compare_at_price": vd["_meta_compare"],
                "discount_pct":     vd["_meta_discount_honest"],
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
                "discount_pct":     vd["_meta_discount_honest"],
                "snapshot_date":    str(today),
                "recorded_at":      datetime.now(timezone.utc).isoformat(),
            }
            if use_insert:
                supabase.table("price_snapshots").insert(row).execute()
            else:
                supabase.table("price_snapshots").update({
                    "price":         vd["_meta_price"],
                    "discount_pct":  vd["_meta_discount_honest"],
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
        .select("external_sku, is_in_stock, size, color, first_observed_price")
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

        products_upsert_result = (
            supabase.table("products")
            .upsert(batch_products_payload, on_conflict="brand,external_id")
            .execute()
        )
        
        product_id_map = {row["external_id"]: row["id"] for row in products_upsert_result.data}
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

                # GUARDRAIL AGAINST PRE-DISCOUNTED ITEMS: Lock original anchor floor on first sight
                if prev and prev.get("first_observed_price"):
                    v_baseline = float(prev["first_observed_price"])
                else:
                    v_baseline = compare_at if (compare_at and compare_at > price) else price

                discount_honest = None
                if v_baseline and v_baseline > price:
                    discount_honest = round(((v_baseline - price) / v_baseline) * 100, 2)

                batch_variants_payload.append({
                    "product_id":           db_product_id,
                    "external_sku":         external_sku,
                    "color":                color or None,
                    "size":                 size or None,
                    "is_in_stock":          available,
                    "first_observed_price": v_baseline, # Fixed variant attribute tier
                    "last_updated_at":      datetime.now(timezone.utc).isoformat(),
                    
                    # Internal configuration meta trackers
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
            variants_upsert_result = supabase.table("product_variants").upsert(db_payload, on_conflict="external_sku").execute()
            
            variant_sku_to_id = {row["external_sku"]: row["id"] for row in variants_upsert_result.data}
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
                
                # Sizing matrix completion tracking parameters
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

                main_price           = variant_records[0]["_meta_price"]
                main_compare         = variant_records[0]["_meta_compare"]
                main_discount_honest = variant_records[0]["_meta_discount_honest"]
                v_baseline           = variant_records[0]["_meta_baseline"]
                
                last_price = last_prices.get(db_product_id)
                
                if last_price is None or abs(last_price - main_price) > 0.01:
                    direction = None
                    if last_price is not None:
                        direction = "down" if main_price < last_price else "up"
                        price_changes += 1
                        
                        # Process instantaneous downstream user dispatch alerts
                        if direction == "down" and v_baseline and main_price < v_baseline:
                            for p in products:
                                if str(p["id"]) == [k for k, v in product_id_map.items() if v == db_product_id][0]:
                                    product_title = p.get("title", "")
                                    for rec in variant_records:
                                        if rec["_meta_size"]:
                                            find_and_alert_users(
                                                supabase, brand_name, category, rec["_meta_size"],
                                                main_price, main_discount_honest, product_title, product_url,
                                                rec["_meta_baseline"]
                                            )
                                    break

                    supabase.table("price_events").insert({
                        "product_id":       db_product_id,
                        "brand":            brand_name,
                        "price_before":     last_price,
                        "price_after":      main_price,
                        "compare_at_price": main_compare,
                        "discount_pct":     main_discount_honest,
                        "direction":        direction,
                        "sizes_in_stock":   sizes_in_stock,
                        "recorded_at":      datetime.now(timezone.utc).isoformat(),
                    }).execute()

                    last_prices[db_product_id] = main_price

        page += 1

    print(f"\n  ✅ {brand_name}: {products_seen} products scanned, {price_changes} price changes recorded.")
    return price_changes

if __name__ == "__main__":
    print("🚀 Khabar scraper starting...")
    _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    purge_old_events(_sb)

    total = 0
    for brand in BRANDS:
        total += scrape_brand(brand["name"], brand["domain"])

    print(f"\n🏁 All done. Total price changes this run: {total}")
