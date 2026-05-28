# ═══════════════════════════════════════════════════════
# KHABAR — Shopify Price Scraper
# Runs every 30 minutes via GitHub Actions.
# Checks Town Team, Ravin, Men's Club for price changes.
# Writes new products and price events to Supabase.
# ═══════════════════════════════════════════════════════

import os
import requests
from supabase import create_client
from datetime import datetime, timezone

# ── Supabase connection (credentials come from GitHub Secrets) ──
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Brands to scrape (all Shopify) ─────────────────────
BRANDS = [
    {"name": "town_team", "domain": "www.townteam.com"},
    {"name": "ravin",     "domain": "www.ravin.com"},
    {"name": "mens_club", "domain": "www.mensclubeg.com"},
]

# ── Category keyword map ────────────────────────────────
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

def normalize_category(text):
    """Maps any product name/type to our universal category."""
    text = text.lower()
    for category, keywords in CATEGORY_MAP.items():
        if any(kw in text for kw in keywords):
            return category
    return "uncategorized"

def normalize_gender(tags, product_type, title):
    """Detects gender from tags, product type, or title."""
    text = f"{' '.join(tags)} {product_type} {title}".lower()
    if any(w in text for w in ["women", "woman", "female", "ladies", "girl", "نسائي"]):
        return "women"
    if any(w in text for w in ["men", "man", "male", "gents", "رجالي"]):
        return "men"
    if any(w in text for w in ["kid", "child", "baby", "infant", "أطفال"]):
        return "kids"
    return "unisex"

def get_last_recorded_price(product_db_id):
    """Looks up the most recent price we recorded for this product."""
    result = (
        supabase.table("price_events")
        .select("price_after")
        .eq("product_id", product_db_id)
        .order("recorded_at", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        return float(result.data[0]["price_after"])
    return None  # means we've never seen this product before

def scrape_brand(brand_name, domain):
    """Scrapes all products from one Shopify brand and records any price changes."""
    print(f"\n{'─'*50}")
    print(f"▶ {brand_name.upper()} — {domain}")
    print(f"{'─'*50}")

    products_seen = 0
    price_changes = 0
    page = 1

    while True:
        url = f"https://{domain}/products.json?limit=250&page={page}"
        try:
            response = requests.get(url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36"
            })
        except requests.RequestException as e:
            print(f"  ⚠️  Network error on page {page}: {e}")
            break

        if response.status_code != 200:
            print(f"  ⚠️  Got HTTP {response.status_code} — stopping")
            break

        products = response.json().get("products", [])
        if not products:
            break  # no more pages

        print(f"  Page {page}: {len(products)} products found")

        for product in products:
            variants = product.get("variants", [])
            if not variants:
                continue

            # ── Prices (from first variant) ──
            main = variants[0]
            price = float(main.get("price") or 0)
            if price == 0:
                continue

            compare_raw = main.get("compare_at_price")
            compare_at_price = float(compare_raw) if compare_raw else None

            discount_pct = None
            if compare_at_price and compare_at_price > price:
                discount_pct = round((compare_at_price - price) / compare_at_price * 100, 2)

            # ── Sizes ──
            all_sizes, sizes_in_stock = [], []
            for v in variants:
                size = v.get("option1") or v.get("title", "")
                if size and size.lower() != "default title":
                    all_sizes.append(size)
                    if v.get("available"):
                        sizes_in_stock.append(size)

            # ── Metadata ──
            title        = product.get("title", "")
            product_type = product.get("product_type", "")
            tags         = product.get("tags", [])
            handle       = product.get("handle", "")
            images       = product.get("images", [])

            cat_text     = f"{title} {product_type}"
            category_raw = product_type or ""

            # ── Upsert into products table ──
            # (upsert = insert if new, update if already exists)
            upsert_result = (
                supabase.table("products")
                .upsert({
                    "brand":               brand_name,
                    "external_id":         str(product["id"]),
                    "name":                title,
                    "category_raw":        category_raw,
                    "category_normalized": normalize_category(cat_text),
                    "gender":              normalize_gender(tags, product_type, title),
                    "sizes_available":     all_sizes,
                    "url":                 f"https://{domain}/products/{handle}",
                    "image_url":           images[0]["src"] if images else None,
                    "last_seen_at":        datetime.now(timezone.utc).isoformat(),
                    "is_active":           True,
                }, on_conflict="brand,external_id")
                .execute()
            )

            db_id = upsert_result.data[0]["id"]
            products_seen += 1

            # ── Check if price changed ──
            last_price = get_last_recorded_price(db_id)

            # Record if: first time we see it, OR price is different from last time
            if last_price is None or abs(last_price - price) > 0.01:
                direction = None
                if last_price is not None:
                    direction = "down" if price < last_price else "up"
                    price_changes += 1
                    print(f"  💰 {title[:45]}: {last_price} → {price} EGP [{direction}]"
                          + (f" ({discount_pct}% off)" if discount_pct else ""))

                supabase.table("price_events").insert({
                    "product_id":        db_id,
                    "brand":             brand_name,
                    "price_before":      last_price,
                    "price_after":       price,
                    "compare_at_price":  compare_at_price,
                    "discount_pct":      discount_pct,
                    "direction":         direction,
                    "sizes_in_stock":    sizes_in_stock,
                    "recorded_at":       datetime.now(timezone.utc).isoformat(),
                }).execute()

        page += 1

    print(f"  ✅ Done: {products_seen} products scanned, {price_changes} price changes recorded")
    return price_changes


# ── Entry point ─────────────────────────────────────────
if __name__ == "__main__":
    print("🚀 Khabar scraper starting...")
    total = 0
    for brand in BRANDS:
        total += scrape_brand(brand["name"], brand["domain"])
    print(f"\n🏁 All done. Total price changes this run: {total}")
