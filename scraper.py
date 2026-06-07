# ═══════════════════════════════════════════════════════
# KHABAR — Scraper v11
# Shopify brands: full size/color/stockout/price pipeline
# LCW: price + stock tracking via Webshare proxy (no sizes yet)
# Key fixes: price_events never deleted, per-variant SELECT
#            eliminated (in-memory lookup), upsert_snapshot
#            uses ON CONFLICT, priming calls removed.
# ═══════════════════════════════════════════════════════

import json
import os
import sys
import time
from curl_cffi import requests
from supabase import create_client
from datetime import datetime, timezone, timedelta, date

sys.stdout.reconfigure(line_buffering=True)

SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_KEY"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API       = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ── Webshare residential proxy ────────────────────────────────────────────────
# LCW's API is protected by Akamai Bot Manager, which blocks datacenter IPs.
# Webshare rotating residential proxy gives GitHub Actions a real home-user IP.
# Credentials stored as GitHub Secrets: WEBSHARE_PROXY_USERNAME / PASSWORD.
# Endpoint p.webshare.io:80 is Webshare's backbone — IP rotates each request.
WEBSHARE_USER  = os.environ.get("WEBSHARE_PROXY_USERNAME", "")
WEBSHARE_PASS  = os.environ.get("WEBSHARE_PROXY_PASSWORD", "")
WEBSHARE_PROXY = {
    "http":  f"http://{WEBSHARE_USER}:{WEBSHARE_PASS}@p.webshare.io:80",
    "https": f"http://{WEBSHARE_USER}:{WEBSHARE_PASS}@p.webshare.io:80",
} if WEBSHARE_USER and WEBSHARE_PASS else None

BRANDS = [
    # LCW first — measure proxy bandwidth immediately, then Shopify brands follow
    {"name": "lc_waikiki", "domain": "www.lcwaikiki.eg", "engine": "lcw_proxy"},
    {"name": "town_team",  "domain": "www.townteam.com", "engine": "shopify"},
    {"name": "ravin",      "domain": "shop.iravin.com", "engine": "shopify"},
    {"name": "mens_club",  "domain": "mensclubcollection.com", "engine": "shopify"},
    {"name": "tree",       "domain": "tree-stores.com", "engine": "shopify"},
    {"name": "dott_jeans", "domain": "dottjeans.com", "engine": "shopify"},
]

BRAND_DISPLAY = {
    "town_team":  "Town Team",
    "ravin":      "Ravin",
    "mens_club":  "Men's Club",
    "tree":       "Tree",
    "dott_jeans": "Dott Jeans",
    "lc_waikiki": "LC Waikiki"
}

# ── Category Taxonomy ────────────────────────────────────────────────────────
# Two-level system:
#   category_normalized = specific sub-category (e.g. "t-shirts", "jeans")
#   Broad groupings (tops/bottoms/etc.) derived at query time for reports.
#   Order matters — more specific patterns checked first.
#   Applies to ALL brands: Shopify and LCW normalized to same taxonomy.
CATEGORY_MAP = {
    # ── Tops ──────────────────────────────────────────────────────────────────
    "t-shirts":    ["t-shirt", " tee ", " tee,", "تيشيرت", "jersey tee", "jersey t"],
    "shirts":      ["shirt", "blouse", "tunic", "تونيك", "قميص", "بلوزة"],
    "polos":       ["polo"],
    "sweatshirts": ["sweatshirt", "سويت شيرت"],
    "hoodies":     ["hoodie", "hoody", "هودي"],
    "cardigans":   ["cardigan", "كارديجان"],
    "sweaters":    ["sweater", "pullover", "knitwear", "knit", "بلوفر"],
    "bodysuits":   ["bodysuit", "body suit", "بودي"],
    "tank-tops":   ["tank", "sleeveless top", "cami", "spaghetti"],
    # ── Bottoms ───────────────────────────────────────────────────────────────
    "jeans":       ["jean", "denim trouser", "دينيم", "جينز"],
    "trousers":    ["trouser", "pant", "chino", "بنطلون", "slacks"],
    "shorts":      ["short", "شورت"],
    "skirts":      ["skirt", "تنورة", "jupe"],
    "leggings":    ["legging", "تايتس", "tight"],
    "joggers":     ["jogger", "sweatpant", "tracksuit bottom", "jogging"],
    "sweatpants":  ["sweat pant"],
    # ── Outerwear ─────────────────────────────────────────────────────────────
    "jackets":     ["jacket", "puffer", "parka", "windbreaker", "جاكيت"],
    "coats":       ["coat", "overcoat", "معطف"],
    "blazers":     ["blazer", "بليزر"],
    "vests":       ["vest", "gilet", "صدرية"],
    # ── Dresses & Jumpsuits ───────────────────────────────────────────────────
    "dresses":     ["dress", "فستان", "maxi dress", "midi dress", "mini dress"],
    "jumpsuits":   ["jumpsuit", "playsuit", "overall", "romper", "جمبسوت"],
    "kaftans":     ["kaftan", "قفطان", "abaya", "عباية", "jalabiya", "جلابية"],
    # ── Footwear ──────────────────────────────────────────────────────────────
    "sneakers":    ["sneaker", "trainer", "athletic shoe", "سنيكر", "كوتشي"],
    "sandals":     ["sandal", "flip flop", "flip-flop", "صندل"],
    "boots":       ["boot", "بوت"],
    "loafers":     ["loafer", "moccasin", "slip-on", "flat shoe"],
    "heels":       ["heel", "pump", "wedge", "stiletto"],
    "slippers":    ["slipper", "house shoe", "شبشب"],
    # ── Accessories ───────────────────────────────────────────────────────────
    "bags":        ["bag", "handbag", "backpack", "tote", "clutch", "شنطة", "حقيبة"],
    "belts":       ["belt", "حزام"],
    "scarves":     ["scarf", "شال", "stole"],
    "hats":        ["hat", "cap", "beanie", "قبعة", "طاقية"],
    "jewelry":     ["jewelry", "jewellery", "necklace", "bracelet", "ring", "earring", "مجوهرات"],
    "watches":     ["watch", "ساعة"],
    "sunglasses":  ["sunglass", "eyewear", "نظارة"],
    "socks":       ["sock", "جوارب", "stocking"],
    "underwear":   ["underwear", "bra", "brief", "boxer", "lingerie", "ملابس داخلية"],
    # ── Swimwear ──────────────────────────────────────────────────────────────
    "swimwear":    ["swimwear", "swimsuit", "bikini", "swim trunk", "مايوه"],
    # ── Loungewear ────────────────────────────────────────────────────────────
    "loungewear":  ["pyjama", "pajama", "nightwear", "sleepwear", "homewear", "بيجامة"],
    # ── Sportswear ────────────────────────────────────────────────────────────
    "sportswear":  ["sport", "gym", "athletic", "workout", "training", "active"],
}

# Broad groups for report-level aggregation (category_normalized → group)
CATEGORY_GROUPS = {
    "tops":        ["t-shirts", "shirts", "polos", "sweatshirts", "hoodies",
                    "bodysuits", "tank-tops", "cardigans", "sweaters"],
    "bottoms":     ["jeans", "trousers", "shorts", "skirts", "leggings",
                    "joggers", "sweatpants"],
    "outerwear":   ["jackets", "coats", "blazers", "vests"],
    "dresses":     ["dresses", "jumpsuits", "kaftans"],
    "footwear":    ["sneakers", "sandals", "boots", "loafers", "heels", "slippers"],
    "accessories": ["bags", "belts", "scarves", "hats", "jewelry",
                    "watches", "sunglasses", "socks", "underwear"],
    "swimwear":    ["swimwear"],
    "loungewear":  ["loungewear"],
    "sportswear":  ["sportswear"],
}

# ── Resilience & Network Handlers ──────────────────────

def get_resilient_session():
    # Unchanged from before LCW work — Shopify brands use this exclusively.
    # curl_cffi with Chrome impersonation for TLS fingerprinting.
    return requests.Session(impersonate="chrome124")

def get_lcw_session():
    """
    Proxied session exclusively for LC Waikiki.
    Webshare residential proxy is passed at session init using curl_cffi's
    correct syntax — not via session.proxies.update() which is unreliable.
    The proxy makes GitHub Actions appear as a real residential user,
    bypassing Akamai's "Host not in allowlist" datacenter IP block.
    """
    if not WEBSHARE_PROXY:
        return requests.Session(impersonate="chrome124")
    # curl_cffi accepts proxy at session level via the proxies kwarg
    # Use the https entry since LCW is HTTPS
    proxy_url = WEBSHARE_PROXY.get("https") or WEBSHARE_PROXY.get("http")
    s = requests.Session(impersonate="chrome124", proxies={"https": proxy_url, "http": proxy_url})
    return s

def execute_with_retry(session_method, url, max_retries=3, backoff=1, **kwargs):
    """
    Helper to provide retry resilience natively for curl_cffi requests,
    mimicking the old urllib3 Retry behavior.
    """
    delay = backoff
    for attempt in range(max_retries):
        try:
            res = session_method(url, **kwargs)
            if res.status_code in [429, 500, 502, 503, 504]:
                raise requests.RequestsError(f"HTTP Status {res.status_code}")
            return res
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"  ❌ Network request permanently failed on {url}: {e}")
                raise e
            time.sleep(delay)
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
    try: 
        return execute_with_retry(session.get, f"https://{domain}", timeout=10, headers={"User-Agent": "Mozilla/5.0"}).status_code == 200
    except: 
        return False

# ── Alerts & Snapshots ────────────────────────────────

def send_telegram(session, chat_id, text):
    if not TELEGRAM_BOT_TOKEN: return
    try: 
        execute_with_retry(session.post, f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
    except: 
        pass

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

def upsert_snapshot(supabase, brand_name, db_product_id, variant_records, today, use_insert=True):
    """
    Writes one price snapshot per product per day using ON CONFLICT DO UPDATE.
    Safe to call multiple times per day — second call updates price in place.
    The use_insert param is kept for signature compatibility but ignored.
    """
    if not variant_records:
        return
    prices = [v["_meta_price"] for v in variant_records if v.get("_meta_price")]
    if not prices:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    if len(set(prices)) == 1:
        vd = variant_records[0]
        safe_db_execute(supabase.table("price_snapshots").upsert(
            {"product_id": db_product_id, "variant_id": None, "brand": brand_name,
             "price": vd["_meta_price"], "compare_at_price": vd["_meta_compare"],
             "snapshot_date": str(today), "recorded_at": now_iso},
            on_conflict="product_id,snapshot_date"
        ))
    else:
        for vd in variant_records:
            vid = vd.get("variant_db_id")
            if not vid:
                continue
            safe_db_execute(supabase.table("price_snapshots").upsert(
                {"product_id": None, "variant_id": vid, "brand": brand_name,
                 "price": vd["_meta_price"], "compare_at_price": vd["_meta_compare"],
                 "snapshot_date": str(today), "recorded_at": now_iso},
                on_conflict="variant_id,snapshot_date"
            ))

def detect_and_write_stockout(supabase, variant_db_id, product_id, brand, size, color, prev_stock, curr_stock, curr_price, baseline):
    if prev_stock == curr_stock: return
    event_type = "stockout" if (prev_stock and not curr_stock) else "restock"
    discount_pct = round(((baseline - curr_price) / baseline) * 100, 2) if (baseline and curr_price < baseline) else None
    safe_db_execute(supabase.table("stockout_events").insert({"variant_id": variant_db_id, "product_id": product_id, "brand": brand, "size": size, "color": color, "event_type": event_type, "price_at_event": curr_price, "discount_pct_at_event": discount_pct, "was_on_discount": bool(discount_pct), "recorded_at": datetime.now(timezone.utc).isoformat()}))

# ── Ingestion Routers ─────────────────────────────────

def scrape_shopify(supabase, session, brand_name, domain, today, prev_stock_state):
    page, products_seen, price_changes = 1, 0, 0
    # Load yesterday's prices in ONE query instead of one SELECT per variant.
    # For Town Team (~30k variants) this eliminates ~30,000 DB round trips,
    # cutting runtime from 2+ hours to under 30 minutes.
    prev_prices = load_last_prices(supabase, brand_name)

    while True:
        url = f"https://{domain}/products.json?limit=250&page={page}"
        try: 
            response = execute_with_retry(session.get, url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
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
                upsert_snapshot(supabase, brand_name, db_pid, records, today)
                sizes_in_stock = [r["_meta_size"] for r in records if r["_meta_available"] and r["_meta_size"]]

                for rec in records:
                    prev_v = prev_stock_state.get(rec["external_sku"])
                    if prev_v: detect_and_write_stockout(supabase, rec["variant_db_id"], db_pid, brand_name, rec["_meta_size"], rec["_meta_color"], prev_v["is_in_stock"], rec["_meta_available"], rec["_meta_price"], rec["_meta_baseline"])
                    
                    curr_price, v_base = rec["_meta_price"], rec["_meta_baseline"]
                    last_p = prev_prices.get(db_pid)  # in-memory — zero DB queries per variant

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
                        prev_prices[db_pid] = curr_price  # keep in-memory dict current
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

def lcw_normalize_category(breadcrumb):
    for level in ["Level3", "Level4", "Level2"]:
        raw = (breadcrumb.get(level) or "").lower().strip()
        if not raw:
            continue
        for category, keywords in CATEGORY_MAP.items():
            if any(kw in raw for kw in keywords):
                return category
    return "uncategorized"

def lcw_normalize_gender(breadcrumb, fallback_gender):
    level1 = (breadcrumb.get("Level1") or "").lower().strip()
    return LCW_BREADCRUMB_GENDER_MAP.get(level1, fallback_gender)

def lcw_fetch_sizes(session, domain, opt_id, headers):
    try:
        url = f"https://{domain}/en/ajax/product/OptionDetailAjax?optionId={opt_id}"
        res = execute_with_retry(session.get, url, timeout=10, headers=headers)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list):
                return data
            return data.get("Sizes") or data.get("sizes") or [{"Size": "One Size", "IsAvailable": True}]
    except Exception:
        pass
    return [{"Size": "One Size", "IsAvailable": True}]

def lcw_fetch_page(session, domain, category_id, page_index, headers, seen_ids=None, category_params=None):
    url = (
        f"https://{domain}/en/ajax/ProductList/ProductListPageData"
        f"?xhrKeys=CategoryTreeId,xhrKeys"
        f"&CategoryTreeId={category_id}"
        f"&PageIndex={page_index}"
        f"&Layout=three-column"
    )
    body = {
        # CategoryParameterList MUST match the browser payload — empty [] returns 0 results.
        "CategoryParameterList": category_params or [],
        "FilterListJson": "[]",
        "LastSeenOptionIdsJson": json.dumps(seen_ids or []),
    }
    try:
        # curl_cffi sets Content-Type: application/json automatically when json= is used.
        # Pass headers directly — no Content-Type override needed.
        res = execute_with_retry(session.post, url, json=body, timeout=30, headers=headers)
        # LCW returns HTTP 404 as its normal success code for this endpoint.
        # Only treat non-200/404 codes as real failures.
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

def scrape_lcw(supabase, session, brand_name, domain, today, prev_stock_state):
    print("  Executing LC Waikiki Catalog Engine (API mode)...")
    products_seen, price_changes = 0, 0

    # upsert_snapshot uses ON CONFLICT DO UPDATE — no insert/update toggle needed
    use_insert = True
    prev_prices = load_last_prices(supabase, brand_name)

    # Headers copied from browser cURL capture — matched exactly to what LCW accepts.
    # curl_cffi impersonation injects sec-ch-ua / sec-fetch-* automatically.
    # X-Requested-With removed — real Chrome never sends this header.
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

    # Priming removed — full HTML page loads were main bandwidth culprit.
    # Akamai session established automatically on first POST API call.

    for cat in LCW_CATEGORIES:
        cat_id, cat_name, cat_gender = cat["id"], cat["name"], cat["gender"]
        print(f"  [{cat_name}] Fetching page 1 to get total page count...")

        seen_ids = []
        first_data = lcw_fetch_page(session, domain, cat_id, 1, headers, seen_ids=[], category_params=cat.get("params", []))
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
                time.sleep(1.5)
                data = lcw_fetch_page(session, domain, cat_id, page_idx, headers, seen_ids=seen_ids, category_params=cat.get("params", []))
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
                breadcrumb = item.get("BreadCrump") or {}
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

            batch_variants, product_variant_tracking = [], {}

            for item in items:
                model_id = item.get("ModelId")
                db_pid   = product_id_map.get(str(model_id))
                if not db_pid:
                    continue

                product_variant_tracking[db_pid] = []
                opt_id = item.get("OptionId")

                price      = float(item.get("PriceValue") or 0)
                if price == 0:
                    continue

                old_price_str = item.get("OldPrice") or ""
                compare_at = (
                    float("".join(c for c in old_price_str if c.isdigit() or c == "."))
                    if any(c.isdigit() for c in old_price_str)
                    else None
                )

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

                    upsert_snapshot(supabase, brand_name, db_pid, records, today)
                    sizes_in_stock = [r["_meta_size"] for r in records if r["_meta_available"] and r["_meta_size"]]

                    for rec in records:
                        prev_v = prev_stock_state.get(rec["external_sku"])

                        if prev_v:
                            detect_and_write_stockout(
                                supabase, rec["variant_db_id"], db_pid, brand_name,
                                rec["_meta_size"], None,
                                prev_v["is_in_stock"], rec["_meta_available"],
                                rec["_meta_price"], rec["_meta_baseline"]
                            )

                        curr_price, v_base = rec["_meta_price"], rec["_meta_baseline"]
                        last_p = prev_prices.get(db_pid)  # in-memory — zero DB queries

                        if last_p is None or abs(last_p - curr_price) > 0.01:
                            direction = "down" if (last_p and curr_price < last_p) else "up" if last_p else None
                            if direction:
                                price_changes += 1

                            if direction == "down" and v_base and curr_price < v_base:
                                if prev_v and prev_v.get("last_updated_at"):
                                    if (datetime.now(timezone.utc) - datetime.fromisoformat(prev_v["last_updated_at"])) > timedelta(days=5):
                                        target_ext_id = next((k for k, v in product_id_map.items() if v == db_pid), None)
                                        for item in items:
                                            if target_ext_id and str(item.get("ModelId")) == target_ext_id:
                                                desc = item.get("ProductDescription") or item.get("BrandPropertyDescription") or "LCW Item"
                                                breadcrumb = item.get("BreadCrump") or {}
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
                            prev_prices[db_pid] = curr_price

            print(f"  [{cat_name}] Page {page_idx}/{page_count} — {len(batch_products)} products processed.")

    return products_seen, price_changes


def scrape_brand(brand_name, domain):
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        brand_config = next(b for b in BRANDS if b["name"] == brand_name)
        # LCW gets a proxied session; all other brands get a clean session.
        # Keeping them separate prevents the proxy from interfering with
        # Shopify brand TLS connections (caused the Town Team page 11 error).
        session = get_lcw_session() if brand_config["engine"] == "lcw_proxy" else get_resilient_session()
    except Exception as e:
        print(f"❌ Initialization failed for {brand_name}: {e}")
        return 0

    today = date.today()
    print(f"\n{'─'*55}\n▶  {brand_name.upper()}  —  {domain}\n{'─'*55}")
    
    try:
        if not check_domain(session, domain): 
            print(f"  ⚠️ Domain {domain} unreachable. Skipping.")
            return 0

        # Fixed: Inner join on products table to correctly filter variants by brand
        all_variant_rows, offset = [], 0
        while True:
            chunk = safe_db_execute(
                supabase.table("product_variants")
                .select("external_sku, is_in_stock, size, color, first_observed_price, last_updated_at, products!inner(brand)")
                .eq("products.brand", brand_name)
                .range(offset, offset + 999)
            )
            rows = chunk.data if (chunk and chunk.data) else []
            all_variant_rows.extend(rows)
            if len(rows) < 1000:
                break
            offset += 1000
            
        prev_stock_state = {row["external_sku"]: row for row in all_variant_rows}

        if brand_config["engine"] == "shopify":
            seen, changes = scrape_shopify(supabase, session, brand_name, domain, today, prev_stock_state)
        elif brand_config["engine"] == "lcw_proxy":
            if not WEBSHARE_PROXY:
                print("  ⚠️ WEBSHARE credentials not set. Skipping LCW.")
                seen, changes = 0, 0
            else:
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
        # price_events: NEVER deleted — permanent intelligence asset.
        # Every historical price change is needed for seasonal patterns,
        # year-on-year comparison, and all L1/L2 intelligence products.
        #
        # price_snapshots: keep 90 days — covers one full season for
        # Mode B statistical detection (30-day IQR window) while staying
        # within Supabase 500MB free tier until first paying B2B client.
        # Upgrade Supabase to $25/mo when subscriber revenue justifies it.
        cutoff_snap = str(date.today() - timedelta(days=90))
        safe_db_execute(_sb.table("price_snapshots").delete().lt("snapshot_date", cutoff_snap))
    except Exception as e:
        print(f"⚠️ Pre-run housecleaning dropped: {e}")
    
    total = sum(scrape_brand(b["name"], b["domain"]) for b in BRANDS)
    print(f"\n🏁 All done. Total price changes this run: {total}")
