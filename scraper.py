# ═══════════════════════════════════════════════════════
# KHABAR — Scraper v4
# Two-layer data architecture:
#   Layer 1: price_events  → real-time alerts (30-day rolling, then purged)
#   Layer 2: price_snapshots → B2B intelligence warehouse (permanent, 1 row/product/day)
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
    """Returns True if this variant option looks like a clothing/shoe size."""
    if not value:
        return False
    v = value.strip().lower()
    if v in SIZE_KEYWORDS:
        return True
    if v.isdigit() and 20 <= int(v) <= 50:   # waist sizes (26–40) and shoe sizes
        return True
    if "/" in v:                               # "28/30" style
        return True
    return False

def detect_options(variants):
    """
    Determines which Shopify option holds sizes and which holds colors.
    Returns (size_key, color_key) — color_key may be None.
    """
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

# ── Layer 1: Load existing prices for change detection ──

def load_last_prices(supabase, brand_name):
    """
    Loads the most recent recorded price per product in one query.
    Used to detect whether a price has changed since the last scrape.
    """
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
    return prices

# ── Layer 2: Daily snapshot UPSERT ────────────────────

def upsert_snapshot(supabase, brand_name, db_product_id, variant_records, today):
    """
    Writes today's price snapshot to the intelligence warehouse.

    If all variants share the same price (most common for Egyptian fashion brands):
    → one product-level row (variant_id = NULL)

    If variants have different prices (e.g. size-specific liquidation):
    → one variant-level row per variant (product_id = NULL)

    Running this 48 times per day on the same product still produces
    exactly ONE row per day, because of the UPSERT + partial unique index.
    """
    if not variant_records:
        return

    prices = [v["price"] for v in variant_records]

    if len(set(prices)) == 1:
        # ── Uniform pricing → product-level snapshot ──
        vd = variant_records[0]
        supabase.table("price_snapshots").upsert(
            {
                "product_id":       db_product_id,
                "variant_id":       None,
                "brand":            brand_name,
                "price":            vd["price"],
                "compare_at_price": vd["compare_at"],
                "discount_pct":     vd["discount_pct"],
                "snapshot_date":    str(today),
                "recorded_at":      datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="product_id,snapshot_date",
        ).execute()
    else:
        # ── Heterogeneous pricing → variant-level snapshots ──
        for vd in variant_records:
            vid = vd.get("variant_db_id")
            if not vid:
                continue
            supabase.table("price_snapshots").upsert(
                {
                    "product_id":       None,
                    "variant_id":       vid,
                    "brand":            brand_name,
                    "price":            vd["price"],
                    "compare_at_price": vd["compare_at"],
                    "discount_pct":     vd["discount_pct"],
                    "snapshot_date":    str(today),
                    "recorded_at":      datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="variant_id,snapshot_date",
            ).execute()

# ── Purge old price_events ─────────────────────────────

def purge_old_events(supabase):
    """
    Deletes price_events older than 30 days.
    These rows have already triggered their alerts and are no longer needed.
    The intelligence warehouse (price_snapshots) holds the permanent record.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    supabase.table("price_events").delete().lt("recorded_at", cutoff).execute()
    print("  🧹 Purged price_events older than 30 days.")

# ── Main scrape function ───────────────────────────────

def scrape_brand(brand_name, domain):
    # Fresh Supabase connection per brand (avoids HTTP/2 session limit)
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    today    = date.today()

    print(f"\n{'─'*55}")
    print(f"▶  {brand_name.upper()}  —  {domain}")
    print(f"{'─'*55}")

    if not check_domain(domain):
        print("  ⚠️  Skipping — domain unreachable.")
        return 0

    last_prices   = load_last_prices(supabase, brand_name)
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

        for product in products:
            shopify_variants = product.get("variants", [])
            if not shopify_variants:
                continue

            title        = product.get("title", "")
            product_type = product.get("product_type", "")
            tags         = product.get("tags", [])
            handle       = product.get("handle", "")
            images       = product.get("images", [])

            # Detect which Shopify option holds sizes vs colors
            size_key, color_key = detect_options(shopify_variants)

            # ── Upsert product catalog record ──
            upsert_result = (
                supabase.table("products")
                .upsert(
                    {
                        "brand":               brand_name,
                        "external_id":         str(product["id"]),
                        "name":                title,
                        "category_raw":        product_type or "",
                        "category_normalized": normalize_category(f"{title} {product_type}"),
                        "gender":              normalize_gender(tags, product_type, title),
                        "sizes_available":     [],  # deprecated; variants table is now the source
                        "url":                 f"https://{domain}/products/{handle}",
                        "image_url":           images[0]["src"] if images else None,
                        "last_seen_at":        datetime.now(timezone.utc).isoformat(),
                        "is_active":           True,
                    },
                    on_conflict="brand,external_id",
                )
                .execute()
            )
            db_product_id = upsert_result.data[0]["id"]
            products_seen += 1

            # ── Upsert variants + build variant_records for snapshot ──
            variant_records = []

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

                # Upsert this variant into the catalog
                external_sku = f"{domain}_{v['id']}"
                vr = supabase.table("product_variants").upsert(
                    {
                        "product_id":      db_product_id,
                        "external_sku":    external_sku,
                        "color":           color or None,
                        "size":            size or None,
                        "is_in_stock":     available,
                        "last_updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    on_conflict="external_sku",
                ).execute()

                variant_db_id = vr.data[0]["id"] if vr.data else None

                variant_records.append({
                    "variant_db_id": variant_db_id,
                    "price":         price,
                    "compare_at":    compare_at,
                    "discount_pct":  discount_pct,
                })

            if not variant_records:
                continue

            # Representative price = first variant (uniform in 95%+ of cases)
            main_price       = variant_records[0]["price"]
            main_compare     = variant_records[0]["compare_at"]
            main_discount    = variant_records[0]["discount_pct"]

            # ── Layer 2: UPSERT today's snapshot ──
            upsert_snapshot(supabase, brand_name, db_product_id, variant_records, today)

            # ── Layer 1: Record price change event if price changed ──
            last_price = last_prices.get(db_product_id)

            if last_price is None or abs(last_price - main_price) > 0.01:
                direction = None
                if last_price is not None:
                    direction = "down" if main_price < last_price else "up"
                    price_changes += 1
                    print(
                        f"  💰 {title[:40]}: {last_price} → {main_price} EGP "
                        f"[{direction}]"
                        + (f" ({main_discount}% off)" if main_discount else "")
                    )

                # In-stock sizes for this change event
                sizes_in_stock = [
                    v.get(size_key, "") for v in shopify_variants
                    if v.get("available") and v.get(size_key)
                ]

                supabase.table("price_events").insert(
                    {
                        "product_id":       db_product_id,
                        "brand":            brand_name,
                        "price_before":     last_price,
                        "price_after":      main_price,
                        "compare_at_price": main_compare,
                        "discount_pct":     main_discount,
                        "direction":        direction,
                        "sizes_in_stock":   sizes_in_stock,
                        "recorded_at":      datetime.now(timezone.utc).isoformat(),
                    }
                ).execute()

                last_prices[db_product_id] = main_price

        page += 1

    print(
        f"\n  ✅ {brand_name}: {products_seen} products scanned, "
        f"{price_changes} price changes recorded."
    )
    return price_changes


# ── Entry point ────────────────────────────────────────

if __name__ == "__main__":
    print("🚀 Khabar scraper starting...")

    # Housekeeping: purge stale alert-layer data at the start of each run
    _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    purge_old_events(_sb)

    total = 0
    for brand in BRANDS:
        total += scrape_brand(brand["name"], brand["domain"])

    print(f"\n🏁 All done. Total price changes this run: {total}")
