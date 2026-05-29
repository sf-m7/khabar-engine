# ═══════════════════════════════════════════════════════
# KHABAR — Shopify Price Scraper (v3 — correct size detection)
# ═══════════════════════════════════════════════════════

import os
import sys
import requests
from supabase import create_client
from datetime import datetime, timezone

sys.stdout.reconfigure(line_buffering=True)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

BRANDS = [
    {"name": "town_team", "domain": "www.townteam.com"},
    {"name": "ravin",     "domain": "shop.iravin.com"},
    {"name": "mens_club", "domain": "mensclubcollection.com"},
    {"name": "tree",      "domain": "tree-stores.com"},
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

# These are values that look like sizes — NOT colors
SIZE_KEYWORDS = {
    "xs", "s", "m", "l", "xl", "xxl", "xxxl", "2xl", "3xl", "4xl",
    "small", "medium", "large", "x-large", "xx-large",
    "os", "one size", "free size", "onesize", "فري", "فري سايز",
}

def looks_like_size(value):
    """Returns True if a variant option value looks like a size, not a color."""
    if not value:
        return False
    v = value.strip().lower()
    if v in SIZE_KEYWORDS:
        return True
    # Pure numbers = waist/shoe sizes (28, 30, 38, 39, 40...)
    if v.isdigit():
        return True
    # Formats like "28/30", "32W", "30L", "28W/30L"
    if "/" in v or v.endswith("w") or v.endswith("l"):
        return True
    return False

def extract_sizes(variants):
    """
    Finds which Shopify option (option1, option2, option3) contains sizes.
    Returns (all_sizes, sizes_in_stock) — deduplicated lists.
    """
    # Sample up to 8 variants to decide which option holds sizes
    sample = variants[:8]
    opt1_samples = [v.get("option1", "") for v in sample]
    opt2_samples = [v.get("option2", "") for v in sample]
    opt3_samples = [v.get("option3", "") for v in sample]

    if any(looks_like_size(v) for v in opt1_samples):
        size_key = "option1"
    elif any(looks_like_size(v) for v in opt2_samples):
        size_key = "option2"
    elif any(looks_like_size(v) for v in opt3_samples):
        size_key = "option3"
    else:
        # Fallback: use option1 even if we can't confirm it's a size
        size_key = "option1"

    seen = set()
    all_sizes = []
    sizes_in_stock = []

    for v in variants:
        size = (v.get(size_key) or v.get("title") or "").strip()
        if not size or size.lower() == "default title":
            continue
        if size not in seen:
            seen.add(size)
            all_sizes.append(size)
        if v.get("available") and size not in sizes_in_stock:
            sizes_in_stock.append(size)

    return all_sizes, sizes_in_stock

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

def check_domain(domain):
    try:
        r = requests.get(
            f"https://{domain}/products.json?limit=1",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        return r.status_code == 200
    except Exception as e:
        print(f"  ✗ Domain check failed: {e}")
        return False

def load_last_prices(brand_name):
    print(f"  Loading existing price history for {brand_name}...")
    result = (
        supabase.table("price_events")
        .select("product_id, price_after, recorded_at")
        .eq("brand", brand_name)
        .order("recorded_at", desc=True)
        .limit(5000)
        .execute()
    )
    prices = {}
    for row in result.data:
        pid = row["product_id"]
        if pid not in prices:
            prices[pid] = float(row["price_after"])
    print(f"  Found {len(prices)} products with existing price history.")
    return prices

def scrape_brand(brand_name, domain):
    global supabase
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)  # fresh connection per brand
    print(f"\n{'─'*55}")
    print(f"▶  {brand_name.upper()}  —  {domain}")
    print(f"{'─'*55}")

    if not check_domain(domain):
        print(f"  ⚠️  Skipping — domain unreachable.")
        return 0

    last_prices = load_last_prices(brand_name)
    products_seen = 0
    price_changes = 0
    page = 1

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

        for product in products:
            variants = product.get("variants", [])
            if not variants:
                continue

            main             = variants[0]
            price            = float(main.get("price") or 0)
            if price == 0:
                continue

            compare_raw      = main.get("compare_at_price")
            compare_at_price = float(compare_raw) if compare_raw else None
            discount_pct     = None
            if compare_at_price and compare_at_price > price:
                discount_pct = round((compare_at_price - price) / compare_at_price * 100, 2)

            # ── Correct size extraction ──
            all_sizes, sizes_in_stock = extract_sizes(variants)

            title        = product.get("title", "")
            product_type = product.get("product_type", "")
            tags         = product.get("tags", [])
            handle       = product.get("handle", "")
            images       = product.get("images", [])

            upsert_result = (
                supabase.table("products")
                .upsert({
                    "brand":               brand_name,
                    "external_id":         str(product["id"]),
                    "name":                title,
                    "category_raw":        product_type or "",
                    "category_normalized": normalize_category(f"{title} {product_type}"),
                    "gender":              normalize_gender(tags, product_type, title),
                    "sizes_available":     all_sizes,
                    "url":                 f"https://{domain}/products/{handle}",
                    "image_url":           images[0]["src"] if images else None,
                    "last_seen_at":        datetime.now(timezone.utc).isoformat(),
                    "is_active":           True,
                }, on_conflict="brand,external_id")
                .execute()
            )

            db_id      = upsert_result.data[0]["id"]
            last_price = last_prices.get(db_id)
            products_seen += 1

            if last_price is None or abs(last_price - price) > 0.01:
                direction = None
                if last_price is not None:
                    direction = "down" if price < last_price else "up"
                    price_changes += 1
                    print(f"  💰 {title[:45]}: {last_price} → {price} EGP "
                          f"[{direction}]" + (f" ({discount_pct}% off)" if discount_pct else ""))

                supabase.table("price_events").insert({
                    "product_id":       db_id,
                    "brand":            brand_name,
                    "price_before":     last_price,
                    "price_after":      price,
                    "compare_at_price": compare_at_price,
                    "discount_pct":     discount_pct,
                    "direction":        direction,
                    "sizes_in_stock":   sizes_in_stock,
                    "recorded_at":      datetime.now(timezone.utc).isoformat(),
                }).execute()

                last_prices[db_id] = price

        page += 1

    print(f"\n  ✅ {brand_name}: {products_seen} products scanned, "
          f"{price_changes} price changes recorded.")
    return price_changes


if __name__ == "__main__":
    print("🚀 Khabar scraper starting...")
    total = 0
    for brand in BRANDS:
        total += scrape_brand(brand["name"], brand["domain"])
    print(f"\n🏁 All done. Total price changes this run: {total}")
