# ═══════════════════════════════════════════════════════
# KHABAR — Scraper v10 (Enterprise Resilience Core)
# Adds: Exponential Backoff, Brand Fault Isolation,
#       Regional Cookie Priming, and Raw AJAX Routing.
# ═══════════════════════════════════════════════════════

import os
import sys
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from supabase import create_client
from datetime import datetime, timezone, timedelta, date

sys.stdout.reconfigure(line_buffering=True)

SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_KEY"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

BRANDS = [
    {"name": "lc_waikiki", "domain": "www.lcwaikiki.eg", "engine": "lcw_ajax"},
    {"name": "town_team",  "domain": "www.townteam.com", "engine": "shopify"},
    {"name": "ravin",      "domain": "shop.iravin.com", "engine": "shopify"},
    {"name": "mens_club",  "domain": "mensclubcollection.com", "engine": "shopify"},
    {"name": "tree",       "domain": "tree-stores.com", "engine": "shopify"},
    {"name": "dott_jeans", "domain": "dottjeans.com", "engine": "shopify"}
]

BRAND_DISPLAY = {
    "town_team":  "Town Team",
    "ravin":      "Ravin",
    "mens_club":  "Men's Club",
    "tree":       "Tree",
    "dott_jeans": "Dott Jeans",
    "lc_waikiki": "LC Waikiki"
}

CATEGORY_MAP = {
    "tops":        ["shirt", "t-shirt", "tee", "blouse", "top", "polo", "sweatshirt", "tank", "henley", "تيشيرت", "بلوزة"],
    "bottoms":     ["jeans", "trouser", "pant", "short", "skirt", "legging", "chino", "denim", "jogger", "بنطلون", "جينز"],
    "dresses":     ["dress", "jumpsuit", "playsuit", "kaftan", "abaya", "maxi", "midi", "فستان", "عباية"],
    "outerwear":   ["jacket", "coat", "blazer", "hoodie", "cardigan", "sweater", "pullover", "جاكيت", "بلوفر"],
    "footwear":    ["shoe", "sneaker", "sandal", "boot", "flat", "loafer", "slipper", "حذاء", "سنيكر"],
    "accessories": ["bag", "belt", "scarf", "hat", "cap", "jewelry", "watch", "sunglasses", "شنطة", "حزام"],
}

# ── Resilience & Network Handlers ──────────────────────

def get_resilient_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

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

# ── Helpers ───────────────────────────────────────────

def normalize_category(text):
    text = text.lower()
    for category, keywords in CATEGORY_MAP.items():
        if any(kw in text for kw in keywords): return category
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
        size_flags = {"xs", "s", "m", "l", "xl", "xxl", "3xl", "4xl", "os", "one size", "small", "medium", "large"}
        for val in set(values):
            v_low = val.lower()
            if v_low in size_flags: score += 10
            if v_low.isdigit() and (4 <= int(v_low) <= 56): score += 5
        return score

    scores = {"option1": score_col(opt1_values), "option2": score_col(opt2_values), "option3": score_col(opt3_values)}
    size_key = max(scores, key=scores.get)
    if scores[size_key] > 0:
        remaining = [k for k in ["option1", "option2", "option3"] if k != size_key and (any(v.get(k) for v in variants))]
        return size_key, (remaining[0] if remaining else None)

    if u_opt1 >= u_opt2 and u_opt1 >= u_opt3:
        return "option1", ("option2" if u_opt2 > 0 else "option3" if u_opt3 > 0 else None)
    return "option2", "option1"

def check_domain(session, domain):
    try: return session.get(f"https://{domain}", timeout=10, headers={"User-Agent": "Mozilla/5.0"}).status_code == 200
    except: return False

# ── Alerts & Snapshots ────────────────────────────────

def send_telegram(session, chat_id, text):
    if not TELEGRAM_BOT_TOKEN: return
    try: session.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
    except: pass

def find_and_alert_users(supabase, session, brand, category, variant_size, current_price, product_name, product_url, variant_baseline):
    if not TELEGRAM_BOT_TOKEN or not variant_baseline or current_price >= variant_baseline: return
    matches = safe_db_execute(supabase.table("user_sizes").select("user_id, users!inner(telegram_id, conversation_state, price_ceiling)").eq("category", category).eq("size", variant_size))
    if not matches or not matches.data: return
    
    for row in matches.data:
        user_info = row.get("users")
        if not user_info or user_info.get("conversation_state") != "active": continue
        uid = user_info["telegram_id"]
        ceiling = user_info.get("price_ceiling")
        if ceiling and current_price > float(ceiling): continue
        brand_check = safe_db_execute(supabase.table("user_brands").select("user_id").eq("user_id", uid).eq("brand", brand))
        if not brand_check or not brand_check.data: continue
        
        honest_discount = round(((variant_baseline - current_price) / variant_baseline) * 100)
        alert = (
            f"🔥 <b>Deal Alert — {BRAND_DISPLAY.get(brand, brand)}</b>\n\n"
            f"<b>{product_name}</b>\n"
            f"Size: <b>{variant_size}</b>\n"
            f"Was: <s>{int(variant_baseline)} EGP</s>  →  <b>Now: {int(current_price)} EGP</b>\n"
            f"<b>{honest_discount}% OFF (True Discount)</b>\n\n"
            f"👉 <a href='{product_url}'>Shop now</a>"
        )
        send_telegram(session, uid, alert)

def load_last_prices(supabase, brand_name):
    today, yesterday = str(date.today()), str(date.today() - timedelta(days=1))
    for target_date in [today, yesterday]:
        result = safe_db_execute(supabase.table("price_snapshots").select("product_id, price").eq("brand", brand_name).eq("snapshot_date", target_date))
        if result and result.data: return {row.get("product_id"): float(row["price"]) for row in result.data if row.get("product_id")}
    return {}

def upsert_snapshot(supabase, brand_name, db_product_id, variant_records, today, use_insert):
    if not variant_records: return
    prices = [v["_meta_price"] for v in variant_records]
    if len(set(prices)) == 1:
        vd = variant_records[0]
        row = {"product_id": db_product_id, "variant_id": None, "brand": brand_name, "price": vd["_meta_price"], "compare_at_price": vd["_meta_compare"], "snapshot_date": str(today), "recorded_at": datetime.now(timezone.utc).isoformat()}
        if use_insert: safe_db_execute(supabase.table("price_snapshots").insert(row))
        else: safe_db_execute(supabase.table("price_snapshots").update({"price": vd["_meta_price"], "compare_at_price": vd["_meta_compare"], "recorded_at": datetime.now(timezone.utc).isoformat()}).eq("product_id", db_product_id).eq("snapshot_date", str(today)))
    else:
        for vd in variant_records:
            vid = vd.get("variant_db_id")
            if not vid: continue
            row = {"product_id": None, "variant_id": vid, "brand": brand_name, "price": vd["_meta_price"], "compare_at_price": vd["_meta_compare"], "snapshot_date": str(today), "recorded_at": datetime.now(timezone.utc).isoformat()}
            if use_insert: safe_db_execute(supabase.table("price_snapshots").insert(row))
            else: safe_db_execute(supabase.table("price_snapshots").update({"price": vd["_meta_price"], "compare_at_price": vd["_meta_compare"], "recorded_at": datetime.now(timezone.utc).isoformat()}).eq("variant_id", vid).eq("snapshot_date", str(today)))

def detect_and_write_stockout(supabase, variant_db_id, product_id, brand, size, color, prev_stock, curr_stock, curr_price, baseline):
    if prev_stock == curr_stock: return
    event_type = "stockout" if (prev_stock and not curr_stock) else "restock"
    discount_pct = round(((baseline - curr_price) / baseline) * 100, 2) if (baseline and curr_price < baseline) else None
    safe_db_execute(supabase.table("stockout_events").insert({"variant_id": variant_db_id, "product_id": product_id, "brand": brand, "size": size, "color": color, "event_type": event_type, "price_at_event": curr_price, "discount_pct_at_event": discount_pct, "was_on_discount": bool(discount_pct), "recorded_at": datetime.now(timezone.utc).isoformat()}))

# ── Ingestion Routers ─────────────────────────────────

def scrape_shopify(supabase, session, brand_name, domain, today, prev_stock_state):
    page, products_seen, price_changes = 1, 0, 0
    check_insert = safe_db_execute(supabase.table("price_snapshots").select("id").eq("brand", brand_name).eq("snapshot_date", str(today)).limit(1))
    use_insert = len(check_insert.data) == 0 if (check_insert and check_insert.data is not None) else True

    while True:
        url = f"https://{domain}/products.json?limit=250&page={page}"
        try: response = session.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        except Exception as e:
            print(f"  ⚠️ HTTP fault on page {page}: {e}")
            break
            
        if response.status_code != 200: break
        products = response.json().get("products", [])
        if not products: break

        batch_products = []
        for p in products:
            if not p.get("variants"): continue
            safe_image = p.get("images")[0].get("src") if p.get("images") and len(p.get("images")) > 0 else None
            batch_products.append({"brand": brand_name, "external_id": str(p["id"]), "name": p["title"], "category_raw": p.get("product_type", ""), "category_normalized": normalize_category(f"{p['title']} {p.get('product_type','')}" ), "gender": normalize_gender(p.get("tags",[]), p.get("product_type",""), p["title"]), "sizes_available": [], "url": f"https://{domain}/products/{p['handle']}", "image_url": safe_image, "last_seen_at": datetime.now(timezone.utc).isoformat(), "is_active": True})

        if not batch_products: break
        product_upsert_rows = []
        for i in range(0, len(batch_products), 100):
            res = safe_db_execute(supabase.table("products").upsert(batch_products[i:i+100], on_conflict="brand,external_id"))
            if res and res.data: product_upsert_rows.extend(res.data)
        
        product_id_map = {row["external_id"]: row["id"] for row in product_upsert_rows}
        products_seen += len(batch_products)

        batch_variants, product_variant_tracking = [], {}
        for p in products:
            db_pid = product_id_map.get(str(p["id"]))
            if not db_pid: continue
            size_key, color_key = detect_options(p["variants"])
            product_variant_tracking[db_pid] = []

            for v in p["variants"]:
                size, color = (v.get(size_key) or "").strip(), (v.get(color_key) or "").strip() if color_key else None
                if not size or size.lower() == "default title": size = None
                price, compare_at, available = float(v.get("price") or 0), float(v.get("compare_at_price") or 0) if v.get("compare_at_price") else None, bool(v.get("available"))
                if price == 0: continue

                sku = f"{domain}_{v['id']}"
                prev = prev_stock_state.get(sku)
                v_baseline = float(prev["first_observed_price"]) if (prev and prev.get("first_observed_price")) else price

                batch_variants.append({"product_id": db_pid, "external_sku": sku, "color": color, "size": size, "is_in_stock": available, "first_observed_price": v_baseline, "last_updated_at": datetime.now(timezone.utc).isoformat(), "_meta_price": price, "_meta_compare": compare_at, "_meta_baseline": v_baseline, "_meta_size": size, "_meta_color": color, "_meta_available": available})

        if batch_variants:
            db_payload = [{k: v for k, v in row.items() if not k.startswith('_meta_')} for row in batch_variants]
            variant_upsert_rows = []
            for i in range(0, len(db_payload), 100):
                res = safe_db_execute(supabase.table("product_variants").upsert(db_payload[i:i+100], on_conflict="external_sku"))
                if res and res.data: variant_upsert_rows.extend(res.data)
            
            sku_to_id = {row["external_sku"]: row["id"] for row in variant_upsert_rows}
            for vr in batch_variants: vr["variant_db_id"] = sku_to_id.get(vr["external_sku"])
            for vr in batch_variants: product_variant_tracking[vr["product_id"]].append(vr)

            for db_pid, records in product_variant_tracking.items():
                if not records: continue
                upsert_snapshot(supabase, brand_name, db_pid, records, today, use_insert)
                sizes_in_stock = [r["_meta_size"] for r in records if r["_meta_available"] and r["_meta_size"]]

                for rec in records:
                    prev_v = prev_stock_state.get(rec["external_sku"])
                    if prev_v: detect_and_write_stockout(supabase, rec["variant_db_id"], db_pid, brand_name, rec["_meta_size"], rec["_meta_color"], prev_v["is_in_stock"], rec["_meta_available"], rec["_meta_price"], rec["_meta_baseline"])
                    
                    curr_price, v_base = rec["_meta_price"], rec["_meta_baseline"]
                    last_ev = safe_db_execute(supabase.table("price_events").select("price_after").eq("product_id", db_pid).order("recorded_at", desc=True).limit(1))
                    last_p = float(last_ev.data[0]["price_after"]) if (last_ev and last_ev.data) else None

                    if last_p is None or abs(last_p - curr_price) > 0.01:
                        direction = "down" if (last_p and curr_price < last_p) else "up" if last_p else None
                        if direction: price_changes += 1
                        
                        if direction == "down" and v_base and curr_price < v_base:
                            if prev_v and prev_v.get("last_updated_at"):
                                if (datetime.now(timezone.utc) - datetime.fromisoformat(prev_v["last_updated_at"])) > timedelta(days=5):
                                    target_ext_id = next((k for k, v in product_id_map.items() if v == db_pid), None)
                                    for p in products:
                                        if target_ext_id and str(p["id"]) == target_ext_id:
                                            find_and_alert_users(supabase, session, brand_name, rec["_meta_size"], curr_price, p["title"], f"https://{domain}/products/{p['handle']}", v_base)

                        safe_db_execute(supabase.table("price_events").insert({"product_id": db_pid, "brand": brand_name, "price_before": last_p, "price_after": curr_price, "direction": direction, "sizes_in_stock": sizes_in_stock, "recorded_at": datetime.now(timezone.utc).isoformat()}))
        page += 1
    return products_seen, price_changes


# ── LC Waikiki Scraper ────────────────────────────────────────────────────────
#
# HOW THIS WORKS (plain English):
#   LCW's website calls its own internal API to load product listings.
#   We discovered this API via the browser's Network tab — it's a clean GET
#   request that returns JSON, no Playwright or headless browser needed.
#
# API ENDPOINT:
#   GET https://www.lcwaikiki.eg/en/ajax/ProductList/ProductListPageData
#   Key params: CategoryTreeId (which section), PageIndex (which page)
#
# CATEGORY IDs (discovered via Network tab inspection):
#   1  = Women  (~6,048 products)
#   9  = Men    (~3,570 products)
#   We scrape top-level gender categories only to avoid double-counting
#   products that also appear in sub-categories (e.g. Trousers = 260).
#
# SIZES:
#   Each item in the listing has an OptionId. We make a second lightweight
#   call to /OptionDetailAjax to get the actual sizes + availability for
#   that specific product option (colour). This is the same approach the
#   original scrape_lcw used — we kept it because it works.
#
# PRICE FIELDS (confirmed from browser Response tab):
#   PriceValue  = current selling price (always present)
#   OldPrice    = compare-at price as a formatted string e.g. "1,299.00 EGP"
#                 (only present when item is on sale — we strip non-numeric chars)
#
# CATEGORY / GENDER:
#   BreadCrumb in the response gives the full hierarchy:
#   Level1=Men, Level2=Clothing, Level3=Shorts-Men, Level4=Denim Shorts
#   We normalise Level3 (most specific useful level) against CATEGORY_MAP.
#   Gender comes from Level1 (Men / Women / Kids).
#
# PAGE LIMIT NOTE:
#   The old version had `if page > 5: break` — that capped at ~515 products
#   out of 9,600+. We now read PageCount from the first response and loop
#   all pages. A 1.5s polite delay between pages avoids rate-limiting.

LCW_CATEGORIES = [
    # Add Kids category ID here once discovered via Network tab (same method)
    {"id": 9, "name": "Men",   "gender": "men"},
    {"id": 1, "name": "Women", "gender": "women"},
]

LCW_BREADCRUMB_GENDER_MAP = {
    "men": "men", "man": "men", "رجال": "men", "رجالي": "men",
    "women": "women", "woman": "women", "نساء": "women", "نسائي": "women",
    "kids": "kids", "children": "kids", "أطفال": "kids",
}

def lcw_normalize_category(breadcrumb):
    """
    Extract the most useful category level from LCW's BreadCrumb dict
    and map it to Khabar's universal CATEGORY_MAP taxonomy.
    Tries Level3 first (e.g. 'Shorts - Men'), then Level4, then Level2.
    """
    for level in ["Level3", "Level4", "Level2"]:
        raw = (breadcrumb.get(level) or "").lower().strip()
        if not raw:
            continue
        # Try every keyword in every category
        for category, keywords in CATEGORY_MAP.items():
            if any(kw in raw for kw in keywords):
                return category
    return "uncategorized"

def lcw_normalize_gender(breadcrumb, fallback_gender):
    """
    Read gender from BreadCrumb Level1 ('Men' / 'Women' / 'Kids').
    Falls back to the category-level gender we passed in (from LCW_CATEGORIES).
    """
    level1 = (breadcrumb.get("Level1") or "").lower().strip()
    return LCW_BREADCRUMB_GENDER_MAP.get(level1, fallback_gender)

def lcw_fetch_sizes(session, domain, opt_id, headers):
    """
    Fetches size + availability data for one LCW product option (colour).
    Returns a list of dicts: [{"Size": "M", "IsAvailable": True}, ...]
    Falls back to a single "One Size" entry if the call fails.
    """
    try:
        url = f"https://{domain}/en/ajax/product/OptionDetailAjax?optionId={opt_id}"
        res = session.get(url, timeout=10, headers=headers)
        if res.status_code == 200:
            data = res.json()
            # The response is either a list directly or wrapped in a key
            if isinstance(data, list):
                return data
            return data.get("Sizes") or data.get("sizes") or [{"Size": "One Size", "IsAvailable": True}]
    except Exception:
        pass
    return [{"Size": "One Size", "IsAvailable": True}]

def lcw_fetch_page(session, domain, category_id, page_index, headers):
    """
    Fetches one page of products for a given LCW category.
    Returns the parsed JSON dict or None on failure.

    IMPORTANT — must be POST, not GET.
    Confirmed via browser Network tab: Request Method = POST.
    LCW returns HTTP 404 for GET requests even with correct query params.
    The Content-Type: application/json header is also required.
    Session cookies (set during priming) are carried automatically.
    """
    url = (
        f"https://{domain}/en/ajax/ProductList/ProductListPageData"
        f"?xhrKeys=CategoryTreeId,xhrKeys"
        f"&CategoryTreeId={category_id}"
        f"&PageIndex={page_index}"
        f"&Layout=three-column"
    )
    # Merge in Content-Type — required for POST to be accepted
    post_headers = {**headers, "Content-Type": "application/json"}
    try:
        # POST with empty JSON body — LCW only reads query params for category listing
        res = session.post(url, json={}, timeout=30, headers=post_headers)
        if res.status_code != 200:
            print(f"  ⚠️ LCW API returned HTTP {res.status_code} (cat={category_id}, page={page_index})")
            return None
        return res.json()
    except Exception as e:
        print(f"  ⚠️ LCW network fault (cat={category_id}, page={page_index}): {e}")
        return None

def scrape_lcw(supabase, session, brand_name, domain, today, prev_stock_state):
    """
    Scrapes all LC Waikiki Egypt products across Women and Men categories
    using the internal ProductListPageData API.
    Produces the same output as scrape_shopify: products, variants, price events,
    snapshots, stockout events, and Telegram alerts — all via the same shared helpers.
    """
    print("  Executing LC Waikiki Catalog Engine (API mode)...")
    products_seen, price_changes = 0, 0

    check_insert = safe_db_execute(
        supabase.table("price_snapshots").select("id")
        .eq("brand", brand_name).eq("snapshot_date", str(today)).limit(1)
    )
    use_insert = (
        len(check_insert.data) == 0
        if (check_insert and check_insert.data is not None)
        else True
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://{domain}/en/women-t-1",
    }

    # Prime session cookies — LCW needs a warm session to return product data.
    # We hit the homepage (not a category page) because the homepage issues ALL
    # the required cookies in one response: visitorId, GeoSettings, ASP.NET_SessionId,
    # guestSessionId. The session object stores them automatically and sends them
    # on every subsequent request, including the POST API calls.
    try:
        print("  [LCW] Priming session cookies via homepage...")
        prime_headers = {**headers}
        prime_headers.pop("Content-Type", None)  # homepage is a GET
        session.get(f"https://{domain}", headers=prime_headers, timeout=20)
        session.get(f"https://{domain}/en/women-t-1", headers=prime_headers, timeout=15)
        print("  [LCW] Session primed.")
    except Exception as e:
        print(f"  [LCW] Cookie priming failed (non-fatal): {e}")

    for cat in LCW_CATEGORIES:
        cat_id, cat_name, cat_gender = cat["id"], cat["name"], cat["gender"]
        print(f"  [{cat_name}] Fetching page 1 to get total page count...")

        first_data = lcw_fetch_page(session, domain, cat_id, 1, headers)
        if not first_data:
            print(f"  ⚠️ [{cat_name}] Could not reach LCW API. Skipping category.")
            continue

        catalog_meta = first_data.get("CatalogList") or {}
        total_items  = catalog_meta.get("ItemCount", 0)
        page_count   = catalog_meta.get("PageCount", 1)
        print(f"  [{cat_name}] {total_items} products across {page_count} pages.")

        for page_idx in range(1, page_count + 1):
            if page_idx == 1:
                data = first_data
            else:
                time.sleep(1.5)  # Polite delay — avoids triggering LCW rate limits
                data = lcw_fetch_page(session, domain, cat_id, page_idx, headers)
                if not data:
                    print(f"  ⚠️ [{cat_name}] Page {page_idx} failed. Skipping.")
                    continue

            items = (data.get("CatalogList") or {}).get("Items") or []
            if not items:
                print(f"  ⚠️ [{cat_name}] Page {page_idx} returned 0 items.")
                break

            # ── Build product batch ──────────────────────────────────────────
            batch_products = []
            for item in items:
                model_id = item.get("ModelId")
                if not model_id:
                    continue
                name = (
                    item.get("ProductDescription")
                    or item.get("BrandPropertyDescription")
                    or item.get("Name")
                    or f"LCW-{model_id}"
                )
                breadcrumb = item.get("BreadCrumb") or {}
                category   = lcw_normalize_category(breadcrumb)
                gender     = lcw_normalize_gender(breadcrumb, cat_gender)
                model_url  = item.get("ModelUrl") or ""
                url        = f"https://{domain}{model_url}" if model_url.startswith("/") else model_url

                batch_products.append({
                    "brand":             brand_name,
                    "external_id":       str(model_id),
                    "name":              name,
                    "category_raw":      (breadcrumb.get("Level3") or ""),
                    "category_normalized": category,
                    "gender":            gender,
                    "sizes_available":   [],
                    "url":               url,
                    "image_url":         item.get("DefaultOptionImageUrl"),
                    "last_seen_at":      datetime.now(timezone.utc).isoformat(),
                    "is_active":         True,
                })

            if not batch_products:
                continue

            # Upsert products in batches of 100 (Supabase recommended max)
            product_upsert_rows = []
            for i in range(0, len(batch_products), 100):
                res_p = safe_db_execute(
                    supabase.table("products")
                    .upsert(batch_products[i:i+100], on_conflict="brand,external_id")
                )
                if res_p and res_p.data:
                    product_upsert_rows.extend(res_p.data)

            product_id_map = {row["external_id"]: row["id"] for row in product_upsert_rows}
            products_seen += len(batch_products)

            # ── Build variant batch (with per-product size API call) ──────────
            batch_variants, product_variant_tracking = [], {}

            for item in items:
                model_id = item.get("ModelId")
                db_pid   = product_id_map.get(str(model_id))
                if not db_pid:
                    continue

                product_variant_tracking[db_pid] = []
                opt_id = item.get("OptionId")

                # Current price: PriceValue is always numeric (confirmed from browser)
                price      = float(item.get("PriceValue") or 0)
                if price == 0:
                    continue

                # Compare-at price: OldPrice is a formatted string e.g. "1,299.00 EGP"
                old_price_str = item.get("OldPrice") or ""
                compare_at = (
                    float("".join(c for c in old_price_str if c.isdigit() or c == "."))
                    if any(c.isdigit() for c in old_price_str)
                    else None
                )

                # Fetch sizes for this product option
                sizes_data = lcw_fetch_sizes(session, domain, opt_id, headers)

                for s_entry in sizes_data:
                    size_label = (s_entry.get("Size") or "One Size").strip()
                    is_avail   = bool(s_entry.get("IsAvailable", True))
                    sku        = f"lcw_{opt_id}_{size_label.replace(' ', '_')}"

                    prev       = prev_stock_state.get(sku)
                    v_baseline = float(prev["first_observed_price"]) if (prev and prev.get("first_observed_price")) else price

                    batch_variants.append({
                        "product_id":          db_pid,
                        "external_sku":        sku,
                        "color":               None,
                        "size":                size_label,
                        "is_in_stock":         is_avail,
                        "first_observed_price": v_baseline,
                        "last_updated_at":     datetime.now(timezone.utc).isoformat(),
                        # _meta_ fields are used for logic below, stripped before DB write
                        "_meta_price":         price,
                        "_meta_compare":       compare_at,
                        "_meta_baseline":      v_baseline,
                        "_meta_size":          size_label,
                        "_meta_color":         None,
                        "_meta_available":     is_avail,
                    })

            if batch_variants:
                db_payload = [{k: v for k, v in row.items() if not k.startswith("_meta_")} for row in batch_variants]
                variant_upsert_rows = []
                for i in range(0, len(db_payload), 100):
                    res_v = safe_db_execute(
                        supabase.table("product_variants")
                        .upsert(db_payload[i:i+100], on_conflict="external_sku")
                    )
                    if res_v and res_v.data:
                        variant_upsert_rows.extend(res_v.data)

                sku_to_id = {row["external_sku"]: row["id"] for row in variant_upsert_rows}
                for vr in batch_variants:
                    vr["variant_db_id"] = sku_to_id.get(vr["external_sku"])
                for vr in batch_variants:
                    product_variant_tracking[vr["product_id"]].append(vr)

                for db_pid, records in product_variant_tracking.items():
                    if not records:
                        continue

                    upsert_snapshot(supabase, brand_name, db_pid, records, today, use_insert)
                    sizes_in_stock = [r["_meta_size"] for r in records if r["_meta_available"] and r["_meta_size"]]

                    for rec in records:
                        prev_v = prev_stock_state.get(rec["external_sku"])

                        # Stockout / restock detection
                        if prev_v:
                            detect_and_write_stockout(
                                supabase, rec["variant_db_id"], db_pid, brand_name,
                                rec["_meta_size"], None,
                                prev_v["is_in_stock"], rec["_meta_available"],
                                rec["_meta_price"], rec["_meta_baseline"]
                            )

                        # Price change detection and alerting
                        curr_price, v_base = rec["_meta_price"], rec["_meta_baseline"]
                        last_ev = safe_db_execute(
                            supabase.table("price_events").select("price_after")
                            .eq("product_id", db_pid).order("recorded_at", desc=True).limit(1)
                        )
                        last_p = float(last_ev.data[0]["price_after"]) if (last_ev and last_ev.data) else None

                        if last_p is None or abs(last_p - curr_price) > 0.01:
                            direction = "down" if (last_p and curr_price < last_p) else "up" if last_p else None
                            if direction:
                                price_changes += 1

                            # Alert subscribers if price dropped below their baseline
                            if direction == "down" and v_base and curr_price < v_base:
                                if prev_v and prev_v.get("last_updated_at"):
                                    if (datetime.now(timezone.utc) - datetime.fromisoformat(prev_v["last_updated_at"])) > timedelta(days=5):
                                        target_ext_id = next((k for k, v in product_id_map.items() if v == db_pid), None)
                                        for item in items:
                                            if target_ext_id and str(item.get("ModelId")) == target_ext_id:
                                                desc = item.get("ProductDescription") or item.get("BrandPropertyDescription") or "LCW Item"
                                                breadcrumb = item.get("BreadCrumb") or {}
                                                category   = lcw_normalize_category(breadcrumb)
                                                model_url  = item.get("ModelUrl") or ""
                                                product_url = f"https://{domain}{model_url}"
                                                find_and_alert_users(
                                                    supabase, session, brand_name, category,
                                                    rec["_meta_size"], curr_price,
                                                    desc, product_url, v_base
                                                )

                            safe_db_execute(
                                supabase.table("price_events").insert({
                                    "product_id":    db_pid,
                                    "brand":         brand_name,
                                    "price_before":  last_p,
                                    "price_after":   curr_price,
                                    "direction":     direction,
                                    "sizes_in_stock": sizes_in_stock,
                                    "recorded_at":   datetime.now(timezone.utc).isoformat(),
                                })
                            )

            print(f"  [{cat_name}] Page {page_idx}/{page_count} — {len(batch_products)} products processed.")

    return products_seen, price_changes


def scrape_brand(brand_name, domain):
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        session = get_resilient_session()
    except Exception as e:
        print(f"❌ Initialization failed for {brand_name}: {e}")
        return 0

    today = date.today()
    print(f"\n{'─'*55}\n▶  {brand_name.upper()}  —  {domain}\n{'─'*55}")
    
    try:
        if not check_domain(session, domain): 
            print(f"  ⚠️ Domain {domain} unreachable. Skipping.")
            return 0

        existing_variants = safe_db_execute(supabase.table("product_variants").select("external_sku, is_in_stock, size, color, first_observed_price, last_updated_at"))
        prev_stock_state = {row["external_sku"]: row for row in existing_variants.data} if (existing_variants and existing_variants.data) else {}

        brand_config = next(b for b in BRANDS if b["name"] == brand_name)
        
        if brand_config["engine"] == "shopify":
            seen, changes = scrape_shopify(supabase, session, brand_name, domain, today, prev_stock_state)
        elif brand_config["engine"] == "lcw_ajax":
            seen, changes = scrape_lcw(supabase, session, brand_name, domain, today, prev_stock_state)
        else:
            seen, changes = 0, 0

        print(f"\n  ✅ {brand_name}: {seen} products scanned, {changes} price changes recorded.")
        return changes

    except Exception as e:
        print(f"\n  🚨 CRITICAL FAILURE in {brand_name.upper()} pipeline: {e}")
        print(f"  ⚠️ Quarantining {brand_name} fault. Moving safely to next brand.")
        return 0

if __name__ == "__main__":
    print("🚀 Khabar Network-Hardened Scraper starting...")
    try:
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        cutoff_ev, cutoff_snap = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(), str(date.today() - timedelta(days=365))
        safe_db_execute(_sb.table("price_events").delete().lt("recorded_at", cutoff_ev))
        safe_db_execute(_sb.table("price_snapshots").delete().lt("snapshot_date", cutoff_snap))
    except Exception as e:
        print(f"⚠️ Pre-run housecleaning dropped: {e}")
    
    total = sum(scrape_brand(b["name"], b["domain"]) for b in BRANDS)
    print(f"\n🏁 All done. Total price changes this run: {total}")
