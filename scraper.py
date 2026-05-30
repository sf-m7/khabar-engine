# ═══════════════════════════════════════════════════════
# KHABAR — Scraper v5 (Enterprise Batch Edition)
# Decoupled Dual-Layer Pipeline:
#   Layer 1: price_events   → Volatile real-time alerts (Purged >30 days)
#   Layer 2: price_snapshots → B2B Market Intelligence (Permanent, 1 row/day/target)
# ═══════════════════════════════════════════════════════

import os
import sys
import requests
from supabase import create_client
from datetime import datetime, timezone, timedelta, date

sys.stdout.reconfigure(line_buffering=True)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

BRANDS = [
    {"name": "town_team",  "domain": "www.townteam.com"},
    {"name": "ravin",      "domain": "shop.iravin.com"},
    {"name": "mens_club",  "domain": "mensclubcollection.com"},
    {"name": "tree",       "domain": "tree-stores.com"},
    {"name": "dott_jeans", "domain": "dottjeans.com"},
]

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

# ── Layer 1: Load baseline configurations ─────────────

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

    prices = [v["price"] for v in variant_records]

    if len(set(prices)) == 1:
        vd = variant_records[0]
        if use_insert:
            supabase.table("price_snapshots").insert({
                "product_id":       db_product_id,
                "variant_id":       None,
                "brand":            brand_name,
                "price":            vd["price"],
                "compare_at_price": vd["compare_at"],
                "discount_pct":     vd["discount_pct"],
                "snapshot_date":    str(today),
                "recorded_at":      datetime.now(timezone.utc).isoformat(),
            }).execute()
        else:
            supabase.table("price_snapshots").update({
                "price":            vd["price"],
                "compare_at_price": vd["compare_at"],
                "discount_pct":     vd["discount_pct"],
                "recorded_at":      datetime.now(timezone.utc).isoformat(),
            }).eq("product_id", db_product_id).eq("snapshot_date", str(today)).execute()
    else:
        for vd in variant_records:
            vid = vd.get("variant_db_id")
            if not vid:
                continue
            if use_insert:
                supabase.table("price_snapshots").insert({
                    "product_id":       None,
                    "variant_id":       vid,
                    "brand":            brand_name,
                    "price":            vd["price"],
                    "compare_at_price": vd["compare_at"],
                    "discount_pct":     vd["discount_pct"],
                    "snapshot_date":    str(today),
                    "recorded_at":      datetime.now(timezone.utc).isoformat(),
                }).execute()
            else:
                supabase.table("price_snapshots").update({
                    "price":            vd["price"],
                    "compare_at_price": vd["compare_at"],
                    "discount_pct":     vd["discount_pct"],
                    "recorded_at":      datetime.now(timezone.utc).isoformat(),
                }).eq("variant_id", vid).eq("snapshot_date", str(today)).execute()

def purge_old_events(supabase):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    supabase.table("price_events").delete().lt("recorded_at", cutoff).execute()
    print("  🧹 Purged price_events older than 30 days.")

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
    print(f"  Snapshot strategy: {'INSERT — first run today' if use_insert else 'UPDATE — already ran today'}")

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

                discount_pct = None
                if compare_at and compare_at > price:
                    discount_pct = round((compare_at - price) / compare_at * 100, 2)

                external_sku = f"{domain}_{v['id']}"
                
                batch_variants_payload.append({
                    "product_id":      db_product_id,
                    "external_sku":    external_sku,
                    "color":           color or None,
                    "size":            size or None,
                    "is_in_stock":     available,
                    "last_updated_at": datetime.now(timezone.utc).isoformat(),
                    # Internal metadata hooks
                    "_meta_price":        price,
                    "_meta_compare":      compare_at,
                    "_meta_discount":     discount_pct,
                    "_meta_shopify_v":    v
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
                
                main_price    = variant_records[0]["_meta_price"]
                main_compare  = variant_records[0]["_meta_compare"]
                main_discount = variant_records[0]["_meta_discount"]
                last_price    = last_prices.get(db_product_id)
                
                if last_price is None or abs(last_price - main_price) > 0.01:
                    direction = None
                    if last_price is not None:
                        direction = "down" if main_price < last_price else "up"
                        price_changes += 1
                        for p in products:
                            if str(p["id"]) == [k for k, v in product_id_map.items() if v == db_product_id][0]:
                                log_title = p.get("title", "")
                                print(f"  💰 {log_title[:35]}: {last_price} → {main_price} EGP [{direction}]")
                                break

                    sizes_in_stock = [
                        v.get(size_key, "") for v in [r["_meta_shopify_v"] for r in variant_records]
                        if v.get("available") and v.get(size_key)
                    ]

                    supabase.table("price_events").insert({
                        "product_id":       db_product_id,
                        "brand":            brand_name,
                        "price_before":     last_price,
                        "price_after":      main_price,
                        "compare_at_price": main_compare,
                        "discount_pct":     main_discount,
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
