# ═══════════════════════════════════════════════════════
# KHABAR — Scraper v13
# Built on v12. Changes:
#  v13.1  SIZE_CAP=10 (1x daily schedule, 1GB/month Webshare plan)
#  v13.2  LCW color resolved: ColorImageUrl filename (Turkish name)
#         with MainColorHexCode fallback
#  v13.3  Session priming before LCW API calls (fixes Akamai 403)
#         propagate_lcw_stock() RPC removed (caused Supabase timeout)
#  v13.4  Snapshot loading: explicit limit(20000) in both Shopify
#         and LCW scrapers — fixes duplicate key crash on 2nd+ daily run
# ═══════════════════════════════════════════════════════

import json
import os
import random
import re
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

WEBSHARE_USER  = os.environ.get("WEBSHARE_PROXY_USERNAME", "")
WEBSHARE_PASS  = os.environ.get("WEBSHARE_PROXY_PASSWORD", "")
WEBSHARE_PROXY = {
    "http":  f"http://{WEBSHARE_USER}:{WEBSHARE_PASS}@p.webshare.io:80",
    "https": f"http://{WEBSHARE_USER}:{WEBSHARE_PASS}@p.webshare.io:80",
} if WEBSHARE_USER and WEBSHARE_PASS else None

BRANDS = [
    {"name": "lc_waikiki", "domain": "www.lcwaikiki.eg",        "engine": "lcw_proxy"},
    {"name": "town_team",  "domain": "www.townteam.com",         "engine": "shopify"},
    {"name": "ravin",      "domain": "shop.iravin.com",          "engine": "shopify"},
    {"name": "mens_club",  "domain": "mensclubcollection.com",   "engine": "shopify"},
    {"name": "tree",       "domain": "tree-stores.com",          "engine": "shopify"},
    {"name": "dott_jeans", "domain": "dottjeans.com",            "engine": "shopify"},
]

BRAND_DISPLAY = {
    "town_team":  "Town Team",
    "ravin":      "Ravin",
    "mens_club":  "Men's Club",
    "tree":       "Tree",
    "dott_jeans": "Dott Jeans",
    "lc_waikiki": "LC Waikiki",
}

# ── Category Taxonomy ─────────────────────────────────────────────────────────
# Specific sub-categories stored in category_normalized.
# Broad groups (tops/bottoms/etc.) derived at query time via CATEGORY_GROUPS.
CATEGORY_MAP = {
    "t-shirts":    ["t-shirt", " tee ", " tee,", "تيشيرت", "jersey tee", "jersey t"],
    "shirts":      ["shirt", "blouse", "tunic", "تونيك", "قميص", "بلوزة"],
    "polos":       ["polo"],
    "sweatshirts": ["sweatshirt", "سويت شيرت"],
    "hoodies":     ["hoodie", "hoody", "هودي"],
    "cardigans":   ["cardigan", "كارديجان"],
    "sweaters":    ["sweater", "pullover", "knitwear", "knit", "بلوفر"],
    "bodysuits":   ["bodysuit", "body suit", "بودي"],
    "tank-tops":   ["tank", "sleeveless top", "cami", "spaghetti"],
    "jeans":       ["jean", "denim trouser", "دينيم", "جينز"],
    "trousers":    ["trouser", "pant", "chino", "بنطلون", "slacks"],
    "shorts":      ["short", "شورت"],
    "skirts":      ["skirt", "تنورة", "jupe"],
    "leggings":    ["legging", "تايتس", "tight"],
    "joggers":     ["jogger", "sweatpant", "tracksuit bottom", "jogging"],
    "sweatpants":  ["sweat pant"],
    "jackets":     ["jacket", "puffer", "parka", "windbreaker", "جاكيت"],
    "coats":       ["coat", "overcoat", "معطف"],
    "blazers":     ["blazer", "بليزر"],
    "vests":       ["vest", "gilet", "صدرية"],
    "dresses":     ["dress", "فستان", "maxi dress", "midi dress", "mini dress"],
    "jumpsuits":   ["jumpsuit", "playsuit", "overall", "romper", "جمبسوت"],
    "kaftans":     ["kaftan", "قفطان", "abaya", "عباية", "jalabiya", "جلابية"],
    "sneakers":    ["sneaker", "trainer", "athletic shoe", "سنيكر", "كوتشي"],
    "sandals":     ["sandal", "flip flop", "flip-flop", "صندل"],
    "boots":       ["boot", "بوت"],
    "loafers":     ["loafer", "moccasin", "slip-on", "flat shoe"],
    "heels":       ["heel", "pump", "wedge", "stiletto"],
    "slippers":    ["slipper", "house shoe", "شبشب"],
    "bags":        ["bag", "handbag", "backpack", "tote", "clutch", "شنطة", "حقيبة"],
    "belts":       ["belt", "حزام"],
    "scarves":     ["scarf", "شال", "stole"],
    "hats":        ["hat", "cap", "beanie", "قبعة", "طاقية"],
    "jewelry":     ["jewelry", "jewellery", "necklace", "bracelet", "ring", "earring", "مجوهرات"],
    "watches":     ["watch", "ساعة"],
    "sunglasses":  ["sunglass", "eyewear", "نظارة"],
    "socks":       ["sock", "جوارب", "stocking"],
    "underwear":   ["underwear", "bra", "brief", "boxer", "lingerie", "ملابس داخلية"],
    "swimwear":    ["swimwear", "swimsuit", "bikini", "swim trunk", "مايوه"],
    "loungewear":  ["pyjama", "pajama", "nightwear", "sleepwear", "homewear", "بيجامة"],
    "sportswear":  ["sport", "gym", "athletic", "workout", "training", "active"],
}

CATEGORY_GROUPS = {
    "tops":        ["t-shirts", "shirts", "polos", "sweatshirts", "hoodies", "bodysuits", "tank-tops", "cardigans", "sweaters"],
    "bottoms":     ["jeans", "trousers", "shorts", "skirts", "leggings", "joggers", "sweatpants"],
    "outerwear":   ["jackets", "coats", "blazers", "vests"],
    "dresses":     ["dresses", "jumpsuits", "kaftans"],
    "footwear":    ["sneakers", "sandals", "boots", "loafers", "heels", "slippers"],
    "accessories": ["bags", "belts", "scarves", "hats", "jewelry", "watches", "sunglasses", "socks", "underwear"],
    "swimwear":    ["swimwear"],
    "loungewear":  ["loungewear"],
    "sportswear":  ["sportswear"],
}

# ── Network ───────────────────────────────────────────────────────────────────

def get_resilient_session():
    return requests.Session(impersonate="chrome124")

def get_lcw_session():
    if not WEBSHARE_PROXY:
        return requests.Session(impersonate="chrome124")
    proxy_url = WEBSHARE_PROXY.get("https") or WEBSHARE_PROXY.get("http")
    return requests.Session(impersonate="chrome124", proxies={"https": proxy_url, "http": proxy_url})

def execute_with_retry(session_method, url, max_retries=3, backoff=1, **kwargs):
    delay = backoff
    for attempt in range(max_retries):
        try:
            res = session_method(url, **kwargs)
            if res.status_code in [429, 500, 502, 503, 504]:
                raise requests.RequestsError(f"HTTP {res.status_code}")
            return res
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"  ❌ Network failed on {url}: {e}")
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

# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize_category(text):
    text = text.lower()
    for category, keywords in CATEGORY_MAP.items():
        if any(kw in text for kw in keywords):
            return category
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
        size_flags = {"xs","s","m","l","xl","xxl","3xl","4xl","os","one size","small","medium","large"}
        for val in set(values):
            v_low = val.lower()
            if v_low in size_flags: score += 10
            if v_low.isdigit() and (4 <= int(v_low) <= 56): score += 5
        return score
    scores = {"option1": score_col(opt1_values), "option2": score_col(opt2_values), "option3": score_col(opt3_values)}
    size_key = max(scores, key=scores.get)
    if scores[size_key] > 0:
        remaining = [k for k in ["option1","option2","option3"] if k != size_key and any(v.get(k) for v in variants)]
        return size_key, (remaining[0] if remaining else None)
    if u_opt1 >= u_opt2 and u_opt1 >= u_opt3:
        return "option1", ("option2" if u_opt2 > 0 else "option3" if u_opt3 > 0 else None)
    return "option2", "option1"

def check_domain(session, domain):
    try:
        return execute_with_retry(session.get, f"https://{domain}", timeout=10,
                                  headers={"User-Agent": "Mozilla/5.0"}).status_code == 200
    except:
        return False

# ── Alerts ────────────────────────────────────────────────────────────────────

def send_telegram(session, chat_id, text):
    if not TELEGRAM_BOT_TOKEN: return
    try:
        execute_with_retry(session.post, f"{TELEGRAM_API}/sendMessage",
                           json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
    except:
        pass

def find_and_alert_users(supabase, session, brand, category, variant_size,
                         current_price, product_name, product_url, variant_baseline):
    if not TELEGRAM_BOT_TOKEN or not variant_baseline or current_price >= variant_baseline:
        return
    matches = safe_db_execute(
        supabase.table("user_sizes")
        .select("user_id, users!inner(telegram_id, conversation_state, price_ceiling)")
        .eq("category", category).eq("size", variant_size)
    )
    if not matches or not matches.data: return
    for row in matches.data:
        user_info = row.get("users")
        if not user_info or user_info.get("conversation_state") != "active": continue
        uid     = user_info["telegram_id"]
        ceiling = user_info.get("price_ceiling")
        if ceiling and current_price > float(ceiling): continue
        brand_check = safe_db_execute(
            supabase.table("user_brands").select("user_id").eq("user_id", uid).eq("brand", brand)
        )
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

# ── Snapshots ─────────────────────────────────────────────────────────────────

def load_last_prices(supabase, brand_name):
    """One query per brand — loads yesterday's/today's prices into memory."""
    today, yesterday = str(date.today()), str(date.today() - timedelta(days=1))
    for target_date in [today, yesterday]:
        result = safe_db_execute(
            supabase.table("price_snapshots")
            .select("product_id, price")
            .eq("brand", brand_name)
            .eq("snapshot_date", target_date)
        )
        if result and result.data:
            return {row["product_id"]: float(row["price"])
                    for row in result.data if row.get("product_id")}
    return {}

def build_snapshot_rows(brand_name, product_variant_tracking, today, existing_ids):
    """
    Build snapshot row dicts for all products not yet snapshotted today.
    Returns list ready for batch insert.
    """
    rows = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for db_pid, records in product_variant_tracking.items():
        if not records or db_pid in existing_ids:
            continue
        prices = [r["_meta_price"] for r in records if r.get("_meta_price")]
        if not prices:
            continue
        prices.sort()
        median_price = prices[len(prices) // 2]
        vd = records[0]
        rows.append({
            "product_id":       db_pid,
            "variant_id":       None,
            "brand":            brand_name,
            "price":            median_price,
            "compare_at_price": vd.get("_meta_compare"),
            "snapshot_date":    str(today),
            "recorded_at":      now_iso,
        })
        existing_ids.add(db_pid)
    return rows

def detect_and_write_stockout(supabase, variant_db_id, product_id, brand,
                               size, color, prev_stock, curr_stock, curr_price, baseline):
    if prev_stock == curr_stock: return
    event_type   = "stockout" if (prev_stock and not curr_stock) else "restock"
    discount_pct = round(((baseline - curr_price) / baseline) * 100, 2) if (baseline and curr_price < baseline) else None
    safe_db_execute(supabase.table("stockout_events").insert({
        "variant_id":           variant_db_id,
        "product_id":           product_id,
        "brand":                brand,
        "size":                 size,
        "color":                color,
        "event_type":           event_type,
        "price_at_event":       curr_price,
        "discount_pct_at_event": discount_pct,
        "was_on_discount":      bool(discount_pct),
        "recorded_at":          datetime.now(timezone.utc).isoformat(),
    }))

# ── Shopify Scraper ───────────────────────────────────────────────────────────

def scrape_shopify(supabase, session, brand_name, domain, today, prev_stock_state):
    page, products_seen, price_changes = 1, 0, 0

    # Single query for yesterday's prices — replaces per-variant DB SELECTs
    prev_prices = load_last_prices(supabase, brand_name)

    # Pre-load today's snapshotted product IDs — prevents duplicate inserts.
    # IMPORTANT: explicit limit(20000) overrides PostgREST's default 1000-row cap.
    # Without this, brands with >1000 products (Town Team: 3,084 / Ravin: 2,080)
    # fail with duplicate key errors on the 2nd+ run of the day.
    _snap = safe_db_execute(
        supabase.table("price_snapshots").select("product_id")
        .eq("brand", brand_name).eq("snapshot_date", str(today))
        .limit(20000)
    )
    existing_snapshot_ids = set(
        r["product_id"] for r in (_snap.data or []) if r.get("product_id")
    )
    print(f"  {len(existing_snapshot_ids)} snapshots already exist for today.")

    while True:
        url = f"https://{domain}/products.json?limit=250&page={page}"
        try:
            response = execute_with_retry(session.get, url, timeout=30,
                                          headers={"User-Agent": "Mozilla/5.0"})
        except Exception as e:
            print(f"  ⚠️ HTTP fault on page {page}: {e}")
            break
        if response.status_code != 200: break
        products = response.json().get("products", [])
        if not products: break

        batch_products = []
        for p in products:
            if not p.get("variants"): continue
            safe_image = p["images"][0]["src"] if p.get("images") else None
            batch_products.append({
                "brand":               brand_name,
                "external_id":         str(p["id"]),
                "name":                p["title"],
                "category_raw":        p.get("product_type", ""),
                "category_normalized": normalize_category(f"{p['title']} {p.get('product_type','')}"),
                "gender":              normalize_gender(p.get("tags", []), p.get("product_type", ""), p["title"]),
                "sizes_available":     [],
                "url":                 f"https://{domain}/products/{p['handle']}",
                "image_url":           safe_image,
                "last_seen_at":        datetime.now(timezone.utc).isoformat(),
                "is_active":           True,
            })
        if not batch_products: break

        product_upsert_rows = []
        for i in range(0, len(batch_products), 100):
            res = safe_db_execute(
                supabase.table("products").upsert(batch_products[i:i+100], on_conflict="brand,external_id")
            )
            if res and res.data: product_upsert_rows.extend(res.data)
        product_id_map = {row["external_id"]: row["id"] for row in product_upsert_rows}
        products_seen += len(batch_products)

        # Set first_observed_price on new products (only fires once per product lifecycle)
        new_pids = [pid for pid in product_id_map.values()]
        if new_pids:
            for p in products:
                db_pid = product_id_map.get(str(p["id"]))
                if not db_pid: continue
                first_variant_price = None
                for v in p.get("variants", []):
                    pr = float(v.get("price") or 0)
                    if pr > 0:
                        first_variant_price = pr
                        break
                if first_variant_price:
                    safe_db_execute(
                        supabase.table("products")
                        .update({"first_observed_price": first_variant_price})
                        .eq("id", db_pid)
                        .is_("first_observed_price", "null")
                    )

        batch_variants, product_variant_tracking = [], {}
        for p in products:
            db_pid = product_id_map.get(str(p["id"]))
            if not db_pid: continue
            size_key, color_key = detect_options(p["variants"])
            product_variant_tracking[db_pid] = []
            for v in p["variants"]:
                size  = (v.get(size_key) or "").strip() or None
                color = (v.get(color_key) or "").strip() if color_key else None
                if size and size.lower() == "default title": size = None
                price      = float(v.get("price") or 0)
                compare_at = float(v.get("compare_at_price") or 0) if v.get("compare_at_price") else None
                available  = bool(v.get("available"))
                if price == 0: continue
                sku        = f"{domain}_{v['id']}"
                prev       = prev_stock_state.get(sku)
                v_baseline = float(prev["first_observed_price"]) if (prev and prev.get("first_observed_price")) else price
                batch_variants.append({
                    "product_id": db_pid, "external_sku": sku, "color": color, "size": size,
                    "is_in_stock": available, "first_observed_price": v_baseline,
                    "last_updated_at": datetime.now(timezone.utc).isoformat(),
                    "_meta_price": price, "_meta_compare": compare_at, "_meta_baseline": v_baseline,
                    "_meta_size": size, "_meta_color": color, "_meta_available": available,
                })

        if batch_variants:
            db_payload = [{k: v for k, v in row.items() if not k.startswith("_meta_")} for row in batch_variants]
            variant_upsert_rows = []
            for i in range(0, len(db_payload), 100):
                res = safe_db_execute(
                    supabase.table("product_variants").upsert(db_payload[i:i+100], on_conflict="external_sku")
                )
                if res and res.data: variant_upsert_rows.extend(res.data)
            sku_to_id = {row["external_sku"]: row["id"] for row in variant_upsert_rows}
            for vr in batch_variants:
                vr["variant_db_id"] = sku_to_id.get(vr["external_sku"])
                product_variant_tracking[vr["product_id"]].append(vr)

            # Batch snapshot insert — 1 DB call per page instead of 1 per product
            snap_rows = build_snapshot_rows(brand_name, product_variant_tracking, today, existing_snapshot_ids)
            if snap_rows:
                safe_db_execute(supabase.table("price_snapshots").insert(snap_rows))

            for db_pid, records in product_variant_tracking.items():
                if not records: continue
                sizes_in_stock = [r["_meta_size"] for r in records if r["_meta_available"] and r["_meta_size"]]
                for rec in records:
                    prev_v = prev_stock_state.get(rec["external_sku"])
                    if prev_v:
                        detect_and_write_stockout(
                            supabase, rec["variant_db_id"], db_pid, brand_name,
                            rec["_meta_size"], rec["_meta_color"],
                            prev_v["is_in_stock"], rec["_meta_available"],
                            rec["_meta_price"], rec["_meta_baseline"]
                        )
                    curr_price, v_base = rec["_meta_price"], rec["_meta_baseline"]
                    last_p = prev_prices.get(db_pid)
                    if last_p is None or abs(last_p - curr_price) > 0.01:
                        direction = "down" if (last_p and curr_price < last_p) else "up" if last_p else None
                        if direction: price_changes += 1
                        if direction == "down" and v_base and curr_price < v_base:
                            if prev_v and prev_v.get("last_updated_at"):
                                if (datetime.now(timezone.utc) - datetime.fromisoformat(prev_v["last_updated_at"])) > timedelta(days=5):
                                    for p in products:
                                        if str(p["id"]) == next((k for k, v in product_id_map.items() if v == db_pid), None):
                                            find_and_alert_users(
                                                supabase, session, brand_name,
                                                normalize_category(f"{p['title']} {p.get('product_type','')}"),
                                                rec["_meta_size"], curr_price, p["title"],
                                                f"https://{domain}/products/{p['handle']}", v_base
                                            )
                        # Calculate honest discount vs first_observed_price
                        honest_disc = round(((v_base - curr_price) / v_base) * 100, 2) if (v_base and curr_price < v_base) else None
                        safe_db_execute(supabase.table("price_events").insert({
                            "product_id":    db_pid, "brand": brand_name,
                            "price_before":  last_p, "price_after": curr_price,
                            "compare_at_price": rec["_meta_compare"],
                            "discount_pct":  honest_disc,
                            "direction":     direction, "sizes_in_stock": sizes_in_stock,
                            "recorded_at":   datetime.now(timezone.utc).isoformat(),
                        }))
                        prev_prices[db_pid] = curr_price

        print(f"  Page {page} — {len(batch_products)} products processed.")
        time.sleep(1)
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
        if not raw: continue
        for category, keywords in CATEGORY_MAP.items():
            if any(kw in raw for kw in keywords):
                return category
    return "uncategorized"

def lcw_normalize_gender(breadcrumb, fallback_gender):
    level1 = (breadcrumb.get("Level1") or "").lower().strip()
    return LCW_BREADCRUMB_GENDER_MAP.get(level1, fallback_gender)

def parse_lcw_sizes(html):
    """
    Parse size buttons from LCW product page HTML.
    <button data-label="M" class="option-size-box">          → in stock
    <button data-label="M" class="option-size-box option-size-box__stripped"> → out of stock
    Attribute order may vary — extracted independently.
    """
    sizes = []
    for tag in re.findall(r'<button[^>]+data-label[^>]+>', html):
        label_m = re.search(r'data-label="([^"]+)"', tag)
        class_m = re.search(r'class="([^"]+)"', tag)
        if not label_m or not class_m: continue
        classes = class_m.group(1)
        if "option-size-box" in classes:
            sizes.append({
                "size":        label_m.group(1).strip(),
                "is_in_stock": "option-size-box__stripped" not in classes,
            })
    return sizes

def fetch_lcw_product_sizes(session, url):
    """GET a single product page and parse its sizes. ~172KB compressed per call."""
    try:
        res = execute_with_retry(session.get, url, max_retries=1, backoff=0,
                                 timeout=8, headers={
            "accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "accept-encoding": "gzip, deflate, br",
        })
        if res.status_code == 200:
            return parse_lcw_sizes(res.text)
        print(f"  ⚠️ Size page HTTP {res.status_code}: {url}")
    except Exception as e:
        print(f"  ⚠️ Size fetch error: {e}")
    return []

def lcw_fetch_page(session, domain, category_id, page_index, headers,
                   seen_ids=None, category_params=None):
    url = (
        f"https://{domain}/en/ajax/ProductList/ProductListPageData"
        f"?xhrKeys=CategoryTreeId,xhrKeys"
        f"&CategoryTreeId={category_id}"
        f"&PageIndex={page_index}"
        f"&Layout=three-column"
    )
    body = {
        "CategoryParameterList":  category_params or [],
        "FilterListJson":         "[]",
        "LastSeenOptionIdsJson":  json.dumps(seen_ids or []),
    }
    try:
        res = execute_with_retry(session.post, url, json=body, timeout=30, headers=headers)
        # LCW returns HTTP 404 as a normal success — confirmed from browser Network tab
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

def _parse_lcw_price(v):
    if not v: return 0.0
    if isinstance(v, (int, float)): return float(v)
    s = "".join(c for c in str(v) if c.isdigit() or c == ".")
    return float(s) if s else 0.0

def scrape_lcw(supabase, session, brand_name, domain, today, prev_stock_state):
    print("  Executing LC Waikiki Catalog Engine (API mode)...")
    print(f"  [LCW] Proxy configured: {WEBSHARE_PROXY is not None}")
    products_seen, price_changes = 0, 0

    prev_prices = load_last_prices(supabase, brand_name)
    # IMPORTANT: explicit limit(20000) overrides PostgREST's default 1000-row cap.
    # LCW has 7,197 products — without this, runs 2+ of the day hit duplicate key errors.
    _snap = safe_db_execute(
        supabase.table("price_snapshots").select("product_id")
        .eq("brand", brand_name).eq("snapshot_date", str(today))
        .limit(20000)
    )
    existing_snapshot_ids = set(
        r["product_id"] for r in (_snap.data or []) if r.get("product_id")
    )
    print(f"  [LCW] {len(existing_snapshot_ids)} snapshots already exist for today.")

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

    # ── Session priming ───────────────────────────────────────────────────────
    # Akamai Bot Manager requires a prior browser-like page visit before it
    # accepts API requests. Without this, the very first API call returns 403
    # because Akamai has no record of a valid session for this proxy IP.
    #
    # We fetch one LCW category page (HTML only — ~500-900 KB) which causes
    # Akamai to set its _abck session cookie on the session object. All
    # subsequent API calls in this same session automatically carry that cookie.
    #
    # Bandwidth: ~800 KB/run × 30 days = ~24 MB/month — acceptable within 1 GB.
    # This is far less than the old check_domain call, which loaded the full
    # Next.js app bundle. We only need the HTML response, not any assets.
    try:
        prime_url = f"https://{domain}/en/men-clothing-t-9"
        prime_headers = {
            "accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "accept-encoding": "gzip, deflate, br",
            "cache-control":   "no-cache",
            "pragma":          "no-cache",
        }
        prime_res = execute_with_retry(
            session.get, prime_url, max_retries=2, backoff=3,
            timeout=20, headers=prime_headers
        )
        print(f"  [LCW] Session primed — HTTP {prime_res.status_code} "
              f"({len(prime_res.content) / 1024:.0f} KB)")
        # Pause 2-4 seconds: behaves like a human who glanced at the page
        # before clicking into a product listing.
        time.sleep(random.uniform(2, 4))
    except Exception as e:
        print(f"  [LCW] Priming failed (will attempt API anyway): {e}")

    for cat in LCW_CATEGORIES:
        cat_id, cat_name, cat_gender = cat["id"], cat["name"], cat["gender"]
        cat_params = cat.get("params", [])
        print(f"  [{cat_name}] Fetching page 1 to get total page count...")
        seen_ids   = []
        first_data = lcw_fetch_page(session, domain, cat_id, 1, headers,
                                     seen_ids=[], category_params=cat_params)
        if not first_data:
            print(f"  ⚠️ [{cat_name}] Could not reach LCW API. Skipping.")
            continue

        catalog_meta = first_data.get("CatalogList") or {}
        total_items  = catalog_meta.get("ItemCount", 0)
        page_count   = catalog_meta.get("PageCount", 1)
        print(f"  [{cat_name}] {total_items} products across {page_count} pages.")

        for page_idx in range(1, page_count + 1):
            data = first_data if page_idx == 1 else None
            if page_idx > 1:
                time.sleep(random.uniform(1.2, 2.0))
                data = lcw_fetch_page(session, domain, cat_id, page_idx, headers,
                                       seen_ids=seen_ids, category_params=cat_params)
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

            # ── Products upsert ───────────────────────────────────────────────
            batch_products, seen_ext_ids = [], set()
            for item in items:
                model_id = item.get("ModelId")
                if not model_id or str(model_id) in seen_ext_ids: continue
                seen_ext_ids.add(str(model_id))
                name       = (item.get("ProductDescription") or item.get("BrandPropertyDescription")
                              or item.get("Name") or f"LCW-{model_id}")
                breadcrumb = item.get("BreadCrump") or {}
                category   = lcw_normalize_category(breadcrumb)
                if category == "uncategorized" and name:
                    category = lcw_normalize_category({"Level3": name})
                gender     = lcw_normalize_gender(breadcrumb, cat_gender)
                model_url  = item.get("ModelUrl") or ""
                url        = f"https://{domain}{model_url}" if model_url.startswith("/") else model_url
                batch_products.append({
                    "brand": brand_name, "external_id": str(model_id), "name": name,
                    "category_raw":        (breadcrumb.get("Level3") or breadcrumb.get("Level2") or ""),
                    "category_normalized": category, "gender": gender,
                    "sizes_available": [], "url": url,
                    "image_url":   item.get("DefaultOptionImageUrl"),
                    "last_seen_at": datetime.now(timezone.utc).isoformat(), "is_active": True,
                })

            if not batch_products: continue

            product_upsert_rows = []
            for i in range(0, len(batch_products), 100):
                res_p = safe_db_execute(
                    supabase.table("products").upsert(batch_products[i:i+100], on_conflict="brand,external_id")
                )
                if res_p and res_p.data: product_upsert_rows.extend(res_p.data)
            product_id_map  = {row["external_id"]: row["id"] for row in product_upsert_rows}
            products_seen  += len(batch_products)

            # Set first_observed_price on new LCW products
            for item in items:
                db_pid = product_id_map.get(str(item.get("ModelId")))
                if not db_pid: continue
                is_disc  = bool(item.get("Discounted") or item.get("CurrentPricesAreDiscounted"))
                disc_val = _parse_lcw_price(item.get("DiscountedPriceValue"))
                full_val = _parse_lcw_price(item.get("PriceValue") or item.get("Price"))
                fop = disc_val if (is_disc and disc_val > 0) else full_val
                if fop > 0:
                    safe_db_execute(
                        supabase.table("products")
                        .update({"first_observed_price": fop})
                        .eq("id", db_pid)
                        .is_("first_observed_price", "null")
                    )

            # ── Variants upsert ───────────────────────────────────────────────
            batch_variants, product_variant_tracking = [], {}
            for item in items:
                model_id = item.get("ModelId")
                db_pid   = product_id_map.get(str(model_id))
                if not db_pid: continue
                product_variant_tracking.setdefault(db_pid, [])
                opt_id = item.get("OptionId")

                is_discounted  = bool(item.get("Discounted") or item.get("CurrentPricesAreDiscounted"))
                discounted_val = _parse_lcw_price(item.get("DiscountedPriceValue"))
                full_val       = _parse_lcw_price(item.get("PriceValue") or item.get("Price"))
                old_val        = _parse_lcw_price(item.get("MinOldPrice"))

                if is_discounted and discounted_val > 0:
                    price      = discounted_val
                    compare_at = (old_val or full_val) if (old_val or full_val) > discounted_val else None
                else:
                    price, compare_at = full_val, None
                if price == 0: continue

                is_avail   = int(item.get("AvailableStock") or 0) > 0
                sku        = f"lcw_{opt_id}"

                # ── COLOR EXTRACTION (resolved via diagnostic run) ────────
                # The `Color` API field is always None in LCW's listing response.
                # ColorImageUrl contains the Turkish color name in its filename:
                #   e.g. ".../icon/lacivert.png" → "lacivert" (= navy)
                # MainColorHexCode is the fallback (e.g. "1A1A55").
                # Turkish color names are stored as-is — consistent across all
                # LCW products, which is what the intelligence queries need.
                color_img_url = item.get("ColorImageUrl") or ""
                color_name = None
                if color_img_url:
                    m = re.search(r'/([^/]+)\.(png|jpg|jpeg|webp)$', color_img_url, re.IGNORECASE)
                    if m:
                        color_name = m.group(1).lower()
                if not color_name:
                    color_name = item.get("MainColorHexCode") or None

                prev       = prev_stock_state.get(sku)
                v_baseline = float(prev["first_observed_price"]) if (prev and prev.get("first_observed_price")) else price

                batch_variants.append({
                    "product_id": db_pid, "external_sku": sku, "color": color_name,
                    "size": None,  # populated by size pass below
                    "is_in_stock": is_avail, "first_observed_price": v_baseline,
                    "last_updated_at": datetime.now(timezone.utc).isoformat(),
                    "_meta_price": price, "_meta_compare": compare_at,
                    "_meta_baseline": v_baseline, "_meta_size": None,
                    "_meta_color": color_name, "_meta_available": is_avail,
                })

            if batch_variants:
                db_payload = [{k: v for k, v in r.items() if not k.startswith("_meta_")} for r in batch_variants]
                variant_upsert_rows = []
                for i in range(0, len(db_payload), 100):
                    res_v = safe_db_execute(
                        supabase.table("product_variants").upsert(db_payload[i:i+100], on_conflict="external_sku")
                    )
                    if res_v and res_v.data: variant_upsert_rows.extend(res_v.data)
                sku_to_id = {row["external_sku"]: row["id"] for row in variant_upsert_rows}
                for vr in batch_variants:
                    vr["variant_db_id"] = sku_to_id.get(vr["external_sku"])
                    product_variant_tracking[vr["product_id"]].append(vr)

                # Batch snapshot insert
                snap_rows = build_snapshot_rows(brand_name, product_variant_tracking, today, existing_snapshot_ids)
                if snap_rows:
                    safe_db_execute(supabase.table("price_snapshots").insert(snap_rows))

                sizes_in_stock_map = {}
                for db_pid, records in product_variant_tracking.items():
                    if not records: continue
                    sizes_in_stock = [r["_meta_size"] for r in records if r["_meta_available"] and r["_meta_size"]]
                    sizes_in_stock_map[db_pid] = sizes_in_stock
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
                        last_p = prev_prices.get(db_pid)
                        if last_p is None or abs(last_p - curr_price) > 0.01:
                            direction = "down" if (last_p and curr_price < last_p) else "up" if last_p else None
                            if direction: price_changes += 1
                            if direction == "down" and v_base and curr_price < v_base:
                                if prev_v and prev_v.get("last_updated_at"):
                                    if (datetime.now(timezone.utc) - datetime.fromisoformat(prev_v["last_updated_at"])) > timedelta(days=5):
                                        for item in items:
                                            if str(item.get("ModelId")) == next((k for k, v in product_id_map.items() if v == db_pid), None):
                                                desc       = item.get("ProductDescription") or item.get("BrandPropertyDescription") or "LCW Item"
                                                breadcrumb = item.get("BreadCrump") or {}
                                                category   = lcw_normalize_category(breadcrumb)
                                                model_url  = item.get("ModelUrl") or ""
                                                find_and_alert_users(
                                                    supabase, session, brand_name, category,
                                                    rec["_meta_size"], curr_price, desc,
                                                    f"https://{domain}{model_url}", v_base
                                                )
                            honest_disc = round(((v_base - curr_price) / v_base) * 100, 2) if (v_base and curr_price < v_base) else None
                            safe_db_execute(supabase.table("price_events").insert({
                                "product_id":      db_pid, "brand": brand_name,
                                "price_before":    last_p, "price_after": curr_price,
                                "compare_at_price": rec.get("_meta_compare"),
                                "discount_pct":    honest_disc,
                                "direction":       direction,
                                "sizes_in_stock":  sizes_in_stock_map.get(db_pid, []),
                                "recorded_at":     datetime.now(timezone.utc).isoformat(),
                            }))
                            prev_prices[db_pid] = curr_price

            print(f"  [{cat_name}] Page {page_idx}/{page_count} — {len(batch_products)} products processed.")

    # ── Size population pass ──────────────────────────────────────────────────
    # Fetch product pages for LCW variants that still have size=null.
    # SIZE_CAP at 10 keeps per-run cost at ~28.8MB on the 1GB/month Webshare plan.
    # Null-size population at 1x daily: ~970 days to finish — intentionally slow.
    # Acceptable for now; the core intelligence (price + stock) runs fine without.
    # Upgrade path: move to 2GB Webshare plan → SIZE_CAP=25 → ~65 days to finish.
    SIZE_CAP      = 10   # product pages per run — conservative for 1GB/month plan
    SIZE_TIMEOUT  = 300  # bail out of size pass after 5 minutes regardless
    print(f"  [LCW] Fetching sizes for variants missing data (cap: {SIZE_CAP}/run)...")
    try:
        missing = safe_db_execute(
            supabase.table("product_variants")
            .select("id, product_id, external_sku, color, is_in_stock, first_observed_price, products!inner(url, brand)")
            .eq("products.brand", "lc_waikiki")
            .is_("size", "null")
            .limit(SIZE_CAP)
        )
        if missing and missing.data:
            print(f"  [LCW] {len(missing.data)} variants need sizes.")
            fetched = populated = 0
            size_pass_start = time.time()
            for row in missing.data:
                if time.time() - size_pass_start > SIZE_TIMEOUT:
                    print(f"  [LCW] Size pass time limit reached — stopping early.")
                    break
                url = (row.get("products") or {}).get("url")
                if not url: continue
                sizes = fetch_lcw_product_sizes(session, url)
                fetched += 1
                if sizes:
                    product_id = row.get("product_id")
                    color      = row.get("color")
                    fop        = row.get("first_observed_price") or prev_prices.get(product_id)
                    now_iso    = datetime.now(timezone.utc).isoformat()
                    # is_in_stock from HTML is unreliable through the proxy —
                    # LCW shows all sizes as stripped without session cookies.
                    # Use the parent variant's is_in_stock (from listing API's
                    # AvailableStock field) as the authoritative source.
                    parent_stock = row.get("is_in_stock", True)
                    for i, sz in enumerate(sizes):
                        if i == 0:
                            # Update existing variant row with first size
                            # Keep parent's is_in_stock — don't overwrite with HTML data
                            safe_db_execute(
                                supabase.table("product_variants")
                                .update({"size": sz["size"], "last_updated_at": now_iso})
                                .eq("id", row["id"])
                            )
                        else:
                            # Insert additional size rows — inherit parent's stock status
                            sku = f"{row['external_sku']}_{sz['size'].replace(' ', '_')}"
                            safe_db_execute(
                                supabase.table("product_variants").upsert({
                                    "product_id": product_id, "external_sku": sku,
                                    "color": color, "size": sz["size"],
                                    "is_in_stock": parent_stock,
                                    "first_observed_price": fop,
                                    "last_updated_at": now_iso,
                                }, on_conflict="external_sku")
                            )
                    populated += 1
                time.sleep(random.uniform(0.4, 1.0))
            print(f"  [LCW] Sizes: {fetched} pages fetched, {populated} products populated.")
        else:
            print("  [LCW] All variants have size data. ✅")
    except Exception as e:
        print(f"  [LCW] Size population error (non-fatal): {e}")

    return products_seen, price_changes

# ── Orchestrator ──────────────────────────────────────────────────────────────

def scrape_brand(brand_name, domain):
    try:
        supabase     = create_client(SUPABASE_URL, SUPABASE_KEY)
        brand_config = next(b for b in BRANDS if b["name"] == brand_name)
        session      = get_lcw_session() if brand_config["engine"] == "lcw_proxy" else get_resilient_session()
    except Exception as e:
        print(f"❌ Initialization failed for {brand_name}: {e}")
        return 0

    today = date.today()
    print(f"\n{'─'*55}\n▶  {brand_name.upper()}  —  {domain}\n{'─'*55}")
    try:
        # Skip check_domain for LCW — loading the homepage through the proxy
        # costs ~31MB (full Next.js app). API failures are handled gracefully.
        if brand_config["engine"] != "lcw_proxy":
            if not check_domain(session, domain):
                print(f"  ⚠️ Domain {domain} unreachable. Skipping.")
                return 0

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
            if len(rows) < 1000: break
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
    # ── Brand filter ──────────────────────────────────────────────────────────
    # SCRAPE_TARGET controls which brands this run processes.
    # Set via the workflow yml env block — no code change needed to switch.
    #
    #   SCRAPE_TARGET=shopify  → only Shopify brands (no proxy, zero Webshare cost)
    #   SCRAPE_TARGET=lcw      → only LC Waikiki (proxy, ~29 MB/run)
    #   SCRAPE_TARGET=all      → everything (default, for manual one-off runs)
    #
    # WHY: Shopify brands use no proxy so they can run 4x daily for free.
    # LCW uses the proxy so it runs 1x daily to fit within 1 GB/month budget.
    # Two yml files, one scraper, no duplicated logic.
    SCRAPE_TARGET = os.environ.get("SCRAPE_TARGET", "all").lower()
    if SCRAPE_TARGET == "shopify":
        active_brands = [b for b in BRANDS if b["engine"] == "shopify"]
    elif SCRAPE_TARGET == "lcw":
        active_brands = [b for b in BRANDS if b["engine"] == "lcw_proxy"]
    else:
        active_brands = BRANDS
    print(f"🚀 Khabar Scraper starting... target={SCRAPE_TARGET} ({len(active_brands)} brands)")
    startup_jitter = random.uniform(0, 30)
    print(f"  Startup jitter: {startup_jitter:.1f}s")
    time.sleep(startup_jitter)

    # ── Database housecleaning ───────────────────────────────────────────────
    # WHAT WE KEEP FOREVER (intelligence assets):
    #   - products       — even delisted ones (brand health signals)
    #   - product_variants — historical SKUs feed velocity & launch analysis
    #   - price_events   — every price change is precious history
    #   - stockout_events — demand velocity over years
    #
    # WHAT WE PRUNE:
    #   - price_snapshots > 90 days — captured by price_events when changed
    #   - Mark products NOT seen for 14 days as is_active=false
    #     (triggers L1·13 delisting signal; rows retained for intelligence)
    try:
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)

        # 1. Prune old daily snapshots (kept 90 days for Mode B IQR window)
        cutoff_snap = str(date.today() - timedelta(days=90))
        safe_db_execute(
            _sb.table("price_snapshots").delete().lt("snapshot_date", cutoff_snap)
        )

        # 2. Detect delisted products/variants — feeds L1·13 product-delisted
        #    and L1·15 variant-count-decay signals.
        now_iso     = datetime.now(timezone.utc).isoformat()
        cutoff_seen = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()

        # ── Product-level delisting ──
        stale_products = safe_db_execute(
            _sb.table("products")
            .select("id, brand")
            .eq("is_active", True)
            .lt("last_seen_at", cutoff_seen)
        )
        if stale_products and stale_products.data:
            pids = [r["id"] for r in stale_products.data]
            safe_db_execute(
                _sb.table("products")
                .update({"is_active": False, "delisted_at": now_iso})
                .in_("id", pids)
            )
            print(f"  Marked {len(pids)} stale products as delisted.")

        # ── Variant-level delisting (the per-SKU intelligence layer) ──
        stale_variants = safe_db_execute(
            _sb.table("product_variants")
            .select("id, product_id, size, color, is_in_stock, first_observed_price, products!inner(brand, last_seen_at)")
            .is_("delisted_at", "null")
            .lt("products.last_seen_at", cutoff_seen)
        )
        if stale_variants and stale_variants.data:
            event_rows = []
            for v in stale_variants.data:
                pid = v.get("product_id")
                last_snap = safe_db_execute(
                    _sb.table("price_snapshots")
                    .select("price, compare_at_price")
                    .eq("product_id", pid)
                    .order("snapshot_date", desc=True)
                    .limit(1)
                )
                final_price = float(last_snap.data[0]["price"]) if (last_snap and last_snap.data) else None
                baseline    = v.get("first_observed_price")
                discount    = None
                was_on_disc = False
                if final_price and baseline and float(baseline) > 0:
                    discount = round(((float(baseline) - final_price) / float(baseline)) * 100, 2)
                    was_on_disc = discount > 0
                event_rows.append({
                    "variant_id":            v["id"],
                    "product_id":            pid,
                    "brand":                 (v.get("products") or {}).get("brand"),
                    "size":                  v.get("size"),
                    "color":                 v.get("color"),
                    "event_type":            "delisted",
                    "price_at_event":        final_price,
                    "discount_pct_at_event": discount,
                    "was_on_discount":       was_on_disc,
                    "recorded_at":           now_iso,
                })

            if event_rows:
                for i in range(0, len(event_rows), 100):
                    safe_db_execute(_sb.table("stockout_events").insert(event_rows[i:i+100]))

            vids = [v["id"] for v in stale_variants.data]
            for i in range(0, len(vids), 200):
                safe_db_execute(
                    _sb.table("product_variants")
                    .update({"delisted_at": now_iso, "is_in_stock": False})
                    .in_("id", vids[i:i+200])
                )

            print(f"  Recorded {len(event_rows)} variant delisting events.")

    except Exception as e:
        print(f"⚠️ Pre-run housecleaning dropped: {e}")

    total = sum(scrape_brand(b["name"], b["domain"]) for b in active_brands)

    # ── Post-run intelligence detection ──────────────────────────────────────
    try:
        _sb2 = create_client(SUPABASE_URL, SUPABASE_KEY)

        # L1·02 Flash Sale Detection
        flash_cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        downs = safe_db_execute(
            _sb2.table("price_events")
            .select("id, product_id, price_after, recorded_at")
            .eq("direction", "down")
            .eq("is_flash_sale", False)
            .gt("recorded_at", flash_cutoff)
        )
        if downs and downs.data:
            flash_count = 0
            for d in downs.data:
                upper_bound = (datetime.fromisoformat(d["recorded_at"]) + timedelta(hours=24)).isoformat()
                revert = safe_db_execute(
                    _sb2.table("price_events")
                    .select("id")
                    .eq("product_id", d["product_id"])
                    .eq("direction", "up")
                    .gt("recorded_at", d["recorded_at"])
                    .lt("recorded_at", upper_bound)
                    .limit(1)
                )
                if revert and revert.data:
                    safe_db_execute(
                        _sb2.table("price_events")
                        .update({"is_flash_sale": True})
                        .eq("id", d["id"])
                    )
                    flash_count += 1
            if flash_count:
                print(f"  ⚡ Detected {flash_count} flash sale events (L1·02).")

        # L1·07 Mode B Statistical Deal Detection
        oldest_snap = safe_db_execute(
            _sb2.table("price_snapshots")
            .select("snapshot_date")
            .order("snapshot_date", desc=False)
            .limit(1)
        )
        if oldest_snap and oldest_snap.data:
            first_date = oldest_snap.data[0]["snapshot_date"]
            days_of_data = (date.today() - date.fromisoformat(str(first_date))).days
            if days_of_data >= 30:
                stat_cutoff = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
                recent_events = safe_db_execute(
                    _sb2.table("price_events")
                    .select("id, product_id, price_after")
                    .eq("is_statistical_deal", False)
                    .eq("direction", "down")
                    .gt("recorded_at", stat_cutoff)
                )
                if recent_events and recent_events.data:
                    stat_count = 0
                    for ev in recent_events.data:
                        thirty_ago = str(date.today() - timedelta(days=30))
                        history = safe_db_execute(
                            _sb2.table("price_snapshots")
                            .select("price")
                            .eq("product_id", ev["product_id"])
                            .gte("snapshot_date", thirty_ago)
                            .order("snapshot_date", desc=False)
                        )
                        if history and history.data and len(history.data) >= 10:
                            prices = sorted(float(r["price"]) for r in history.data)
                            q1 = prices[len(prices) // 4]
                            q3 = prices[3 * len(prices) // 4]
                            iqr = q3 - q1
                            threshold = q1 - 1.5 * iqr
                            if float(ev["price_after"]) < threshold:
                                safe_db_execute(
                                    _sb2.table("price_events")
                                    .update({"is_statistical_deal": True})
                                    .eq("id", ev["id"])
                                )
                                stat_count += 1
                    if stat_count:
                        print(f"  📊 Detected {stat_count} statistical deal events (L1·07).")

    except Exception as e:
        print(f"  ⚠️ Post-run intelligence detection error: {e}")

    print(f"\n🏁 All done. Total price changes this run: {total}")
