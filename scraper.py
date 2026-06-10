# ═══════════════════════════════════════════════════════
# KHABAR — Scraper v14
# Built on v13. Changes:
#  v14.1  DeFacto integration via PartialIndexScrollResult API
#         No proxy required — plain GET, follows NextDataUrl chain
#         Size enrichment pass (SIZE_CAP=10, same pattern as LCW)
#  v14.2  Fixed parse_defacto_sizes: real HTML uses data-size on <button>
#         with class "is-no-stock" for OOS — not data-value on <li>
#  v14.3  Snapshot pre-load changed from single .limit(20000) query to paginated
#         .range() loop — PostgREST ignores .limit() above 1000, partial index
#         blocks upsert ON CONFLICT, so insert is safe when pre-load is exhaustive
#  v14.4  DeFacto size source changed from HTML parsing to CombinProductListByProductLongCode
#         batch API — sizes populated inline per catalog page, no SIZE_CAP crawl needed
#  v14.5  Fixed backfill query: add products!inner(brand) join for brand filter
#  v14.6  LCW size parser switched from CSS-class HTML to embedded JSON.
#         Pages contain cartOperationViewModel + optimizedDetailModel with full
#         per-size stock + price data. SIZE_CAP raised to 65 (still <1GB/month
#         since pages are 60 KB gzipped, not 172 KB decoded). Dedupe by URL stem
#         eliminates redundant fetches. Backfill completes in ~86 days.
#  v14.7  SYSTEM AUDIT FIXES — detection & alert engine repair:
#         • load_last_prices() now paginates (.range loop). It was capped at the
#           PostgREST 1000-row default, so ~95% of the catalog never received a
#           price baseline → every observation logged as a directionless event and
#           no real drop was ever detected/alerted. This was the root cause of the
#           price_events flood and the dead alert pipeline.
#         • First-sighting no longer writes a direction=NULL event; it seeds the
#           in-memory baseline silently (the snapshot already stores the price).
#         • Removed the inverted ">5-day last_updated_at" alert gate that could
#           never be true for a daily scraper (it blocked every alert). Alerts now
#           fire on a genuine down-transition below first_observed_price.
#         • Added MIN_ALERT_DISCOUNT_PCT (10%) quality floor on alerts.
#         • Restored the rolling 30-day purge of price_events (was dropped in v14).
#  v14.8  Price-change detection moved from per-variant to PER-PRODUCT. Fixing the
#         1000-row cap in v14.7 exposed a latent flip-flop: variants of a
#         multi-price product were each compared against the product's median
#         snapshot with the baseline mutating mid-loop, manufacturing phantom
#         up/down events (e.g. 219 mens_club "down" events from 86 products in one
#         run). Detection now computes one representative price per product via
#         product_repr_price() — the SAME median build_snapshot_rows stores — and
#         emits at most one event per product per run. Alerts now fan out across a
#         product's in-stock sizes. Stockout detection stays per-variant.
#  v14.9  LCW cross-page colour-split fix. LCW paginates by OptionId, so one
#         model's colours can span multiple catalog pages; per-page detection saw
#         partial colour sets and emitted symmetric phantom down/up round-trips
#         (e.g. 1099->599 then 599->1099 minutes apart in one run), which also
#         created false flash-sale flags. The scraper now accumulates every colour
#         of a model across the whole crawl and runs snapshot + price detection
#         ONCE, on the complete set, yielding a stable median. Stockout detection
#         remains per-page/per-variant. Shopify and DeFacto were unaffected (their
#         variants never split across pages).
#  v14.10 Snapshot now tracks the latest price each day, not just the first. For
#         brands scraped multiple times daily (Shopify, DeFacto), a sale starting
#         after the day's first run left the snapshot frozen at the pre-sale price,
#         so every later run re-fired the same drop against the stale baseline
#         (738 products primed to double-count on one observed run). sync_snapshot_price()
#         updates the day's snapshot to the new price after each real change, so a
#         drop is recorded once and the warehouse holds the day's latest price.
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

# Minimum true discount (vs first_observed_price) before a user is alerted.
# Keeps alerts genuine and naturally caps alert volume.
MIN_ALERT_DISCOUNT_PCT = 10

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
    {"name": "defacto",    "domain": "www.defacto.com.eg",       "engine": "defacto"},
]

BRAND_DISPLAY = {
    "town_team":  "Town Team",
    "ravin":      "Ravin",
    "mens_club":  "Men's Club",
    "tree":       "Tree",
    "dott_jeans": "Dott Jeans",
    "lc_waikiki": "LC Waikiki",
    "defacto":    "DeFacto",
}

# ── Category Taxonomy ─────────────────────────────────────────────────────────
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

# ── DeFacto category config ───────────────────────────────────────────────────
# fx_sb1=2182 is the Egypt portal identifier (constant).
# fx_sb2 identifies the gender category:
#   2427 = Men's  |  2426 = Women's
# Confirmed from browser Network tab on 9 Jun 2026.
DEFACTO_CATEGORIES = [
    {
        "name":   "Men",
        "gender": "men",
        "fx_sb1": "2182",
        "fx_sb2": "2427",
    },
    {
        "name":   "Women",
        "gender": "women",
        "fx_sb1": "2182",
        "fx_sb2": "2426",
    },
]

# ── Network ───────────────────────────────────────────────────────────────────

def get_resilient_session():
    return requests.Session(impersonate="chrome124")

def get_lcw_session():
    if not WEBSHARE_PROXY:
        return requests.Session(impersonate="chrome124")
    base_user  = WEBSHARE_USER.split("-")[0]
    eg_session = random.randint(1, 900)
    eg_user    = f"{base_user}-eg-{eg_session}"
    proxy_url  = f"http://{eg_user}:{WEBSHARE_PASS}@p.webshare.io:80"
    print(f"  [LCW] Egyptian proxy session selected: -eg-{eg_session}")
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
    # Quality floor (v14.7): only alert on a genuinely meaningful markdown.
    # Prevents trivial 1-2% wobble from spamming users and keeps "deal" honest.
    honest_discount = round(((variant_baseline - current_price) / variant_baseline) * 100)
    if honest_discount < MIN_ALERT_DISCOUNT_PCT:
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
    """
    Returns {product_id: price} for the most recent day that has snapshots
    (today first, else yesterday).

    CRITICAL (v14.7): this MUST paginate. PostgREST silently caps every
    response at 1000 rows regardless of how many snapshots exist. The old
    single-query version returned only the first 1000 products per brand, so
    every product beyond row 1000 came back with no baseline — meaning the
    scraper treated ~95% of the catalog as "new" on every run, flooding
    price_events with directionless rows and never detecting a real price
    change (and therefore never firing an alert) for those products.
    The .range() loop below loads the full set, exactly like the snapshot
    pre-load and prev_stock_state loaders already do.
    """
    today, yesterday = str(date.today()), str(date.today() - timedelta(days=1))
    for target_date in [today, yesterday]:
        prices, offset = {}, 0
        while True:
            result = safe_db_execute(
                supabase.table("price_snapshots")
                .select("product_id, price")
                .eq("brand", brand_name)
                .eq("snapshot_date", target_date)
                .range(offset, offset + 999)
            )
            rows = (result.data or []) if result else []
            for row in rows:
                if row.get("product_id"):
                    prices[row["product_id"]] = float(row["price"])
            if len(rows) < 1000:
                break
            offset += 1000
        if prices:
            return prices
    return {}

def product_repr_price(records):
    """
    The single representative current price for a product = median of its
    variant prices. Detection and snapshot writing BOTH use this exact function
    so that the price stored today equals the baseline read back tomorrow when
    nothing has changed — preventing the per-variant flip-flop that otherwise
    manufactures phantom up/down events for multi-price products. (v14.8)
    """
    prices = sorted(r["_meta_price"] for r in records if r.get("_meta_price"))
    return prices[len(prices) // 2] if prices else None

def product_repr_baseline(records):
    """Representative first_observed_price for a product = median of variant baselines."""
    bases = sorted(r["_meta_baseline"] for r in records if r.get("_meta_baseline"))
    return bases[len(bases) // 2] if bases else None

def build_snapshot_rows(brand_name, product_variant_tracking, today, existing_ids):
    rows = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for db_pid, records in product_variant_tracking.items():
        if not records or db_pid in existing_ids:
            continue
        median_price = product_repr_price(records)
        if median_price is None:
            continue
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

def sync_snapshot_price(supabase, db_pid, today, price):
    """
    Keep today's snapshot equal to the latest detected price (v14.10).
    Snapshots are written once per product per day (first observation). For
    brands scraped several times a day, a sale that starts AFTER the first run
    leaves the snapshot frozen at the pre-sale price — so every later run of the
    day re-compares the new price against the stale baseline and re-fires the
    same "down" event. Updating the snapshot to the latest price after each real
    change makes the baseline track reality, so the drop is recorded once.
    """
    safe_db_execute(
        supabase.table("price_snapshots")
        .update({"price": price})
        .eq("product_id", db_pid)
        .eq("snapshot_date", str(today))
    )

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
    prev_prices = load_last_prices(supabase, brand_name)
    existing_snapshot_ids = set()
    _snap_offset = 0
    while True:
        _snap = safe_db_execute(
            supabase.table("price_snapshots").select("product_id")
            .eq("brand", brand_name).eq("snapshot_date", str(today))
            .range(_snap_offset, _snap_offset + 999)
        )
        _rows = (_snap.data or []) if _snap else []
        for r in _rows:
            if r.get("product_id"):
                existing_snapshot_ids.add(r["product_id"])
        if len(_rows) < 1000:
            break
        _snap_offset += 1000
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

            snap_rows = build_snapshot_rows(brand_name, product_variant_tracking, today, existing_snapshot_ids)
            if snap_rows:
                safe_db_execute(supabase.table("price_snapshots").insert(snap_rows))

            for db_pid, records in product_variant_tracking.items():
                if not records: continue
                sizes_in_stock = [r["_meta_size"] for r in records if r["_meta_available"] and r["_meta_size"]]
                # Per-variant stockout detection (correct at the variant grain).
                for rec in records:
                    prev_v = prev_stock_state.get(rec["external_sku"])
                    if prev_v:
                        detect_and_write_stockout(
                            supabase, rec["variant_db_id"], db_pid, brand_name,
                            rec["_meta_size"], rec["_meta_color"],
                            prev_v["is_in_stock"], rec["_meta_available"],
                            rec["_meta_price"], rec["_meta_baseline"]
                        )
                # Product-level price detection (v14.8): one representative price per
                # product, compared once. This is what build_snapshot_rows stores, so
                # an unchanged product compares equal and produces no event — ending
                # the per-variant flip-flop that manufactured phantom up/down churn.
                curr_price = product_repr_price(records)
                v_base     = product_repr_baseline(records)
                if curr_price is None:
                    continue
                last_p = prev_prices.get(db_pid)
                if last_p is None:
                    prev_prices[db_pid] = curr_price
                elif abs(last_p - curr_price) > 0.01:
                    direction = "down" if curr_price < last_p else "up"
                    price_changes += 1
                    if direction == "down" and v_base and curr_price < v_base:
                        prod = next((p for p in products
                                     if str(p["id"]) == next((k for k, v in product_id_map.items() if v == db_pid), None)), None)
                        if prod:
                            p_cat = normalize_category(f"{prod['title']} {prod.get('product_type','')}")
                            p_url = f"https://{domain}/products/{prod['handle']}"
                            for sz in set(sizes_in_stock):
                                find_and_alert_users(
                                    supabase, session, brand_name, p_cat,
                                    sz, curr_price, prod["title"], p_url, v_base
                                )
                    honest_disc = round(((v_base - curr_price) / v_base) * 100, 2) if (v_base and curr_price < v_base) else None
                    safe_db_execute(supabase.table("price_events").insert({
                        "product_id":    db_pid, "brand": brand_name,
                        "price_before":  last_p, "price_after": curr_price,
                        "compare_at_price": records[0].get("_meta_compare"),
                        "discount_pct":  honest_disc,
                        "direction":     direction, "sizes_in_stock": sizes_in_stock,
                        "recorded_at":   datetime.now(timezone.utc).isoformat(),
                    }))
                    prev_prices[db_pid] = curr_price
                    sync_snapshot_price(supabase, db_pid, today, curr_price)

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

def parse_lcw_page_data(html):
    """
    Extract structured variant data from an LCW product page.

    Confirmed from page source inspection (9 Jun 2026):
    The page embeds two JSON blobs as JavaScript variables:
      var cartOperationViewModel = {...};    ← per-color: sizes + stock + price
      var optimizedDetailModel   = {...};    ← model-wide: all colors (Options[])

    Returns:
      {
        "current_option_id": int,         # OptionId this page belongs to
        "sizes": [                        # all sizes for THIS color
          {"size": "M", "stock": 4, "price": 1499.0, "size_id": 13542}, ...
        ],
        "option_stock": int,              # total stock for THIS color
        "color_name": str,                # English color name
        "color_code": str,                # ColorCode (e.g. "R9J")
        "category_tree": dict,            # breadcrumb levels
        "all_options": [                  # every color of the same model
          {"option_id": int, "color": str, "in_stock": bool, "url": str}, ...
        ],
      }

    Returns None if either JSON blob is missing or unparseable. The caller
    handles None by skipping the row — we never partially populate.
    """
    # Grab cartOperationViewModel — contains ProductSizes[] with per-size stock
    cart_m = re.search(r'var\s+cartOperationViewModel\s*=\s*(\{.*?\});\s*$',
                       html, re.MULTILINE | re.DOTALL)
    if not cart_m:
        # Fallback: try without trailing semicolon anchor
        cart_m = re.search(r'var\s+cartOperationViewModel\s*=\s*(\{.+?\});',
                           html, re.DOTALL)
    detail_m = re.search(r'var\s+optimizedDetailModel\s*=\s*(\{.+?\});',
                         html, re.DOTALL)
    if not cart_m or not detail_m:
        return None

    try:
        cart   = json.loads(cart_m.group(1))
        detail = json.loads(detail_m.group(1))
    except (json.JSONDecodeError, ValueError) as e:
        # JSON malformed — most likely a non-greedy regex caught too little
        return None

    sizes = []
    for ps in (cart.get("ProductSizes") or []):
        sz_obj = ps.get("Size") or {}
        price_obj = ps.get("Price") or {}
        size_val = sz_obj.get("Value")
        if not size_val:
            continue
        sizes.append({
            "size":     str(size_val).strip(),
            "stock":    int(ps.get("Stock") or 0),
            "price":    float(price_obj.get("Price") or 0),
            "size_id":  sz_obj.get("SizeId"),
        })

    # Walk Options[] inside optimizedDetailModel.ModelInfo to enumerate colors
    all_options = []
    model_info = (detail.get("ModelInfo") or {})
    for opt in (model_info.get("Options") or []):
        opt_id = opt.get("OptionId")
        if not opt_id:
            continue
        all_options.append({
            "option_id": int(opt_id),
            "color":     opt.get("MainColorName") or opt.get("Title") or "",
            "color_code": opt.get("ColorCode"),
            "in_stock":  bool(opt.get("IsStockAvailable")),
            "url":       opt.get("Url") or "",
        })

    option_obj = (detail.get("Option") or {})
    category_tree = option_obj.get("MainCategoryTree") or {}
    # Flatten the tree for compatibility with lcw_normalize_category which
    # expects {"Level1": "...", "Level2": "...", ...} string values.
    cat_flat = {}
    for k, v in category_tree.items():
        if isinstance(v, dict):
            cat_flat[k] = v.get("LevelValue") or ""
        elif isinstance(v, list) and v:
            cat_flat[k] = (v[0] or {}).get("LevelValue") or ""

    return {
        "current_option_id": int(cart.get("OptionId") or 0),
        "sizes":             sizes,
        "option_stock":      int(cart.get("OptionStock") or 0),
        "color_name":        cart.get("Color") or option_obj.get("MainColorName") or "",
        "color_code":        option_obj.get("ColorCode") or "",
        "category_tree":     cat_flat,
        "all_options":       all_options,
    }

def fetch_lcw_product_page(session, url):
    """
    GET an LCW product page and parse its embedded JSON.
    Server returns gzip-compressed HTML (~60 KB on wire / ~350 KB decoded).
    Returns the dict from parse_lcw_page_data() or None.
    """
    try:
        res = execute_with_retry(session.get, url, max_retries=1, backoff=0,
                                 timeout=10, headers={
            "accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "accept-encoding": "gzip, deflate, br",
        })
        if res.status_code == 200:
            return parse_lcw_page_data(res.text)
        print(f"  ⚠️ Size page HTTP {res.status_code}: {url}")
    except Exception as e:
        print(f"  ⚠️ Size fetch error: {e}")
    return None

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
    existing_snapshot_ids = set()
    _snap_offset = 0
    while True:
        _snap = safe_db_execute(
            supabase.table("price_snapshots").select("product_id")
            .eq("brand", brand_name).eq("snapshot_date", str(today))
            .range(_snap_offset, _snap_offset + 999)
        )
        _rows = (_snap.data or []) if _snap else []
        for r in _rows:
            if r.get("product_id"):
                existing_snapshot_ids.add(r["product_id"])
        if len(_rows) < 1000:
            break
        _snap_offset += 1000
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
        time.sleep(random.uniform(2, 4))
    except Exception as e:
        print(f"  [LCW] Priming failed (will attempt API anyway): {e}")

    # v14.9: accumulate every colour of a model ACROSS all pages/categories, then
    # snapshot + detect ONCE after the crawl. LCW paginates by OptionId (colour),
    # so a single model's colours can land on different pages; per-page detection
    # then saw partial variant sets and manufactured symmetric phantom down/up
    # round-trips (e.g. 1099->599 on one page, 599->1099 on another) that also
    # polluted the flash-sale signal. One detection pass over the full colour set
    # gives a stable median and kills the artifact.
    lcw_model_records = {}   # db_pid -> [variant _meta records across the whole run]
    lcw_model_info    = {}   # db_pid -> {desc, category, url} for alerts

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
                    "size": None,
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

                # Per-variant (per-colour) stockout detection — correct at this grain,
                # safe to run per page (each OptionId appears on exactly one page).
                for db_pid, records in product_variant_tracking.items():
                    for rec in records:
                        prev_v = prev_stock_state.get(rec["external_sku"])
                        if prev_v:
                            detect_and_write_stockout(
                                supabase, rec["variant_db_id"], db_pid, brand_name,
                                rec["_meta_size"], None,
                                prev_v["is_in_stock"], rec["_meta_available"],
                                rec["_meta_price"], rec["_meta_baseline"]
                            )
                    # Accumulate this page's colours into the run-level model record
                    # set; price detection happens once, after the full crawl.
                    lcw_model_records.setdefault(db_pid, []).extend(records)
                    if db_pid not in lcw_model_info:
                        it = next((item for item in items
                                   if str(item.get("ModelId")) == next((k for k, v in product_id_map.items() if v == db_pid), None)), None)
                        if it:
                            lcw_model_info[db_pid] = {
                                "desc":     it.get("ProductDescription") or it.get("BrandPropertyDescription") or "LCW Item",
                                "category": lcw_normalize_category(it.get("BreadCrump") or {}),
                                "url":      f"https://{domain}{it.get('ModelUrl') or ''}",
                            }

            print(f"  [{cat_name}] Page {page_idx}/{page_count} — {len(batch_products)} products processed.")

    # ── Single detection pass over the FULL colour set per model (v14.9) ──────
    # Now that every page/category has been crawled, lcw_model_records holds all
    # colours of each model. We snapshot once and detect once per model using a
    # stable median, so partial-page views can no longer manufacture phantom
    # up/down round-trips.
    snap_rows = build_snapshot_rows(brand_name, lcw_model_records, today, existing_snapshot_ids)
    if snap_rows:
        for i in range(0, len(snap_rows), 100):
            safe_db_execute(supabase.table("price_snapshots").insert(snap_rows[i:i+100]))

    for db_pid, records in lcw_model_records.items():
        if not records:
            continue
        curr_price = product_repr_price(records)
        v_base     = product_repr_baseline(records)
        if curr_price is None:
            continue
        last_p = prev_prices.get(db_pid)
        if last_p is None:
            prev_prices[db_pid] = curr_price
            continue
        if abs(last_p - curr_price) <= 0.01:
            continue
        direction = "down" if curr_price < last_p else "up"
        price_changes += 1
        sizes_in_stock = [r["_meta_size"] for r in records if r["_meta_available"] and r["_meta_size"]]
        if direction == "down" and v_base and curr_price < v_base:
            info = lcw_model_info.get(db_pid) or {}
            for sz in set(sizes_in_stock):
                find_and_alert_users(
                    supabase, session, brand_name, info.get("category", "uncategorized"),
                    sz, curr_price, info.get("desc", "LCW Item"),
                    info.get("url", f"https://{domain}"), v_base
                )
        honest_disc = round(((v_base - curr_price) / v_base) * 100, 2) if (v_base and curr_price < v_base) else None
        safe_db_execute(supabase.table("price_events").insert({
            "product_id":       db_pid, "brand": brand_name,
            "price_before":     last_p, "price_after": curr_price,
            "compare_at_price": records[0].get("_meta_compare"),
            "discount_pct":     honest_disc,
            "direction":        direction,
            "sizes_in_stock":   sizes_in_stock,
            "recorded_at":      datetime.now(timezone.utc).isoformat(),
        }))
        prev_prices[db_pid] = curr_price
        sync_snapshot_price(supabase, db_pid, today, curr_price)

    # ── LCW size enrichment pass (rewritten v14.6) ────────────────────────────
    # Bandwidth math:
    #   - Pages return ~60 KB gzipped (server gzip enabled, verified 9 Jun 2026)
    #   - Catalog scrape uses ~870 MB/month — leaves ~130 MB/month headroom
    #   - SIZE_CAP=65 pages/day × 60 KB × 30 days = ~117 MB/month — fits 1 GB
    #
    # Throughput math:
    #   - Each fetch returns ALL sizes for the requested color (5-7 sizes typical)
    #     PLUS the list of every OTHER color of the same model (Options[])
    #   - We dedupe by URL stem (strip "-o-{optionId}" suffix) → fetch ONCE per model
    #   - One fetch populates one color's sizes immediately, and tells us the
    #     OptionId of every sibling color so future fetches know to skip them
    #   - 5,574 unique models in catalog ÷ 65/day = ~86 days to fully backfill
    #
    # Bot-safety:
    #   - 0.6-1.4s random sleep between fetches (≈40-65/min sustained)
    #   - All traffic goes through Egyptian residential proxy session
    #   - Same headers a real browser sends, no robotic patterns
    SIZE_CAP     = 65
    SIZE_TIMEOUT = 600   # 10 minutes hard ceiling — far less than the 180-min workflow limit
    print(f"  [LCW] Fetching sizes for variants missing data (cap: {SIZE_CAP}/run)...")

    try:
        # Pull all null-size LCW variant rows in one paginated sweep
        all_missing, miss_offset = [], 0
        while True:
            chunk = safe_db_execute(
                supabase.table("product_variants")
                .select("id, product_id, external_sku, color, is_in_stock, first_observed_price, products!inner(url, brand)")
                .eq("products.brand", "lc_waikiki")
                .is_("size", "null")
                .range(miss_offset, miss_offset + 999)
            )
            rows = (chunk.data or []) if chunk else []
            all_missing.extend(rows)
            if len(rows) < 1000:
                break
            miss_offset += 1000

        if not all_missing:
            print("  [LCW] All variants have size data. ✅")
            return products_seen, price_changes

        print(f"  [LCW] {len(all_missing)} variants need sizes across {len(set((r.get('products') or {}).get('url') or '' for r in all_missing))} URLs.")

        # Group rows by URL — each URL = one color = one variant row in our DB.
        # Multiple variant rows may map to the same product URL (defensive).
        url_to_rows = {}
        for row in all_missing:
            url = (row.get("products") or {}).get("url")
            if not url:
                continue
            url_to_rows.setdefault(url, []).append(row)

        # Process up to SIZE_CAP unique URLs
        urls_to_fetch = list(url_to_rows.keys())[:SIZE_CAP]
        fetched = populated_rows = 0
        size_pass_start = time.time()

        for url in urls_to_fetch:
            if time.time() - size_pass_start > SIZE_TIMEOUT:
                print(f"  [LCW] Size pass time limit reached — stopping early.")
                break

            page_data = fetch_lcw_product_page(session, url)
            fetched += 1

            if not page_data or not page_data.get("sizes"):
                time.sleep(random.uniform(0.6, 1.4))
                continue

            sizes      = page_data["sizes"]
            now_iso    = datetime.now(timezone.utc).isoformat()

            # For EACH variant row sharing this URL (typically just one):
            # update first size on the existing row, insert new rows for remaining sizes.
            for row in url_to_rows[url]:
                product_id   = row.get("product_id")
                color        = row.get("color")
                fop          = row.get("first_observed_price") or prev_prices.get(product_id)
                parent_stock = row.get("is_in_stock", True)

                for i, sz in enumerate(sizes):
                    # Use the API's per-size stock when available; fall back to parent_stock
                    size_in_stock = (sz.get("stock", 0) > 0) if sz.get("stock") is not None else parent_stock
                    size_value    = sz["size"]

                    if i == 0:
                        safe_db_execute(
                            supabase.table("product_variants")
                            .update({
                                "size":            size_value,
                                "is_in_stock":     size_in_stock,
                                "last_updated_at": now_iso,
                            })
                            .eq("id", row["id"])
                        )
                    else:
                        new_sku = f"{row['external_sku']}_{size_value.replace(' ', '_')}"
                        safe_db_execute(
                            supabase.table("product_variants").upsert({
                                "product_id":           product_id,
                                "external_sku":         new_sku,
                                "color":                color,
                                "size":                 size_value,
                                "is_in_stock":          size_in_stock,
                                "first_observed_price": fop,
                                "last_updated_at":      now_iso,
                            }, on_conflict="external_sku")
                        )
                populated_rows += 1

            # Polite delay to avoid pattern detection
            time.sleep(random.uniform(0.6, 1.4))

        print(f"  [LCW] Sizes: {fetched} pages fetched, {populated_rows} variant rows populated.")
    except Exception as e:
        print(f"  [LCW] Size population error (non-fatal): {e}")

    return products_seen, price_changes

# ── DeFacto Scraper ───────────────────────────────────────────────────────────

def fetch_defacto_sizes_batch(session, domain, long_codes):
    """
    POST to CombinProductListByProductLongCode with a list of LongCodes.
    Returns a dict: {LongCode -> [{"size": str, "is_in_stock": bool}, ...]}

    Confirmed from browser Network tab (9 Jun 2026):
    - Method: POST
    - Content-Type: application/json
    - Body: ["LONGCODE1", "LONGCODE2", ...]
    - Response: {Data: [{LongCode, Sizes: [{SizeName, StockQuantity: null}, ...], Stock: N}, ...]}

    Per-size StockQuantity is always null — DeFacto does not expose it via API.
    We inherit is_in_stock from the parent variant's Stock > 0 field.
    """
    if not long_codes:
        return {}
    url = f"https://{domain}/en-eg/Catalog/CombinProductListByProductLongCode"
    try:
        res = execute_with_retry(
            session.post, url,
            json=long_codes,
            timeout=15,
            headers={
                "accept":           "*/*",
                "accept-language":  "en-US,en;q=0.9",
                "content-type":     "application/json; charset=UTF-8",
                "origin":           f"https://{domain}",
                "referer":          f"https://{domain}/en-eg/",
                "sec-fetch-dest":   "empty",
                "sec-fetch-mode":   "cors",
                "sec-fetch-site":   "same-origin",
                "x-requested-with": "XMLHttpRequest",
            }
        )
        if res.status_code != 200:
            print(f"  ⚠️ [DeFacto] CombinProduct HTTP {res.status_code}")
            return {}
        data = res.json()
        items = data.get("Data") or []
        result = {}
        for item in items:
            lc = item.get("ProductLongCode") or (item.get("DataLayer") or {}).get("LongCode")
            if not lc:
                continue
            # Use the outer Sizes array (has SizeName), stock inherited from Stock field
            raw_sizes = item.get("Sizes") or (item.get("DataLayer") or {}).get("Sizes") or []
            stock_total = int(item.get("ProductVariantMiniProductStock") or
                              (item.get("DataLayer") or {}).get("Stock") or 0)
            is_avail = stock_total > 0
            result[lc] = [
                {"size": sz["SizeName"], "is_in_stock": is_avail}
                for sz in raw_sizes if sz.get("SizeName")
            ]
        return result
    except Exception as e:
        print(f"  ⚠️ [DeFacto] CombinProduct batch error: {e}")
        return {}

def scrape_defacto(supabase, session, brand_name, domain, today, prev_stock_state):
    """
    Scrapes DeFacto Egypt via the PartialIndexScrollResult API.

    Key API facts:
    - Simple GET request, returns JSON, no auth required.
    - Pagination: response contains NextDataUrl — we follow the chain until
      NextDataUrl is absent or None. No need to count pages manually.
    - Each DataLayer item is one color variant of a product (one LongCode = one color).
    - Price fields: Price = full price, DiscountedPrice = sale price (equals Price
      when no sale is active). We treat DiscountedPrice as current price.
    - Stock: Stock field = total units across all sizes. Stock > 0 = has inventory.
    - Sizes: Sizes[] array present in catalog but StockQuantity is always null.
      Per-size stock only available from the product page HTML.
    - Product URL: https://{domain}/en-eg/{SeoName}-{ProductVariantIndex}
    - External ID: LongCode (e.g. "H8248AX26SMNM84") — unique per color variant.
    """
    print(f"  [DeFacto] Starting catalog scrape (no proxy)...")
    products_seen, price_changes = 0, 0

    prev_prices = load_last_prices(supabase, brand_name)
    # Paginated load — PostgREST enforces a 1000-row cap per response regardless
    # of .limit(). We loop with .range() until we get a partial page, same
    # pattern used for prev_stock_state loading.
    existing_snapshot_ids = set()
    _snap_offset = 0
    while True:
        _snap = safe_db_execute(
            supabase.table("price_snapshots").select("product_id")
            .eq("brand", brand_name).eq("snapshot_date", str(today))
            .range(_snap_offset, _snap_offset + 999)
        )
        _rows = (_snap.data or []) if _snap else []
        for r in _rows:
            if r.get("product_id"):
                existing_snapshot_ids.add(r["product_id"])
        if len(_rows) < 1000:
            break
        _snap_offset += 1000
    print(f"  [DeFacto] {len(existing_snapshot_ids)} snapshots already exist for today.")

    headers = {
        "accept":          "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "referer":         f"https://{domain}/en-eg/man-new-season",
        "sec-fetch-dest":  "empty",
        "sec-fetch-mode":  "cors",
        "sec-fetch-site":  "same-origin",
    }

    for cat in DEFACTO_CATEGORIES:
        cat_name   = cat["name"]
        cat_gender = cat["gender"]
        fx_sb1     = cat["fx_sb1"]
        fx_sb2     = cat["fx_sb2"]

        # Build the first page URL. We use pageSize=36 (default observed in browser).
        # SortOrder=0 is the default sort (newest first) — confirmed constant.
        first_url = (
            f"https://{domain}/en-eg/Catalog/PartialIndexScrollResult"
            f"?page=1&SortOrder=0&pageSize=36"
            f"&fx_sb1={fx_sb1}&fx_sb2={fx_sb2}&PictureType=0"
        )

        print(f"  [{cat_name}] Fetching first page...")
        next_url   = first_url
        page_index = 0

        while next_url:
            page_index += 1
            try:
                res = execute_with_retry(
                    session.get, next_url, timeout=30, headers=headers
                )
            except Exception as e:
                print(f"  ⚠️ [DeFacto][{cat_name}] Page {page_index} network error: {e}")
                break

            if res.status_code != 200:
                # Akamai may block GitHub Actions datacenter IPs.
                # If we see a non-200 on the first page, print a clear message
                # so the operator knows to consider adding proxy support.
                print(f"  ⚠️ [DeFacto][{cat_name}] HTTP {res.status_code} on page {page_index}.")
                if page_index == 1:
                    print(f"  ⚠️ [DeFacto] First page blocked. Akamai may be rejecting the GitHub Actions IP.")
                    print(f"  ⚠️ [DeFacto] Consider adding proxy support in a future iteration.")
                break

            try:
                data = res.json()
            except Exception:
                print(f"  ⚠️ [DeFacto][{cat_name}] Page {page_index} returned non-JSON body.")
                break

            # DataLayer contains the product list for this page.
            items = data.get("Data", {}).get("DataLayer") or []
            if not items:
                # Also try the top-level DataLayer (API response shape varies slightly)
                items = data.get("DataLayer") or []

            if not items:
                print(f"  [DeFacto][{cat_name}] Page {page_index} — no items. End of catalog.")
                break

            # ── Products upsert ───────────────────────────────────────────
            batch_products, seen_ext_ids = [], set()
            for item in items:
                long_code = item.get("LongCode")
                if not long_code or long_code in seen_ext_ids:
                    continue
                seen_ext_ids.add(long_code)

                name = item.get("Name") or item.get("GtmName") or f"DeFacto-{long_code}"

                # Category from CategoriesLvl3 > Lvl2 > Lvl1, fallback to product name
                cat_lvl3 = (item.get("CategoriesLvl3") or {}).get("CategoryName") or ""
                cat_lvl2 = (item.get("CategoriesLvl2") or {}).get("CategoryName") or ""
                cat_lvl1 = (item.get("CategoriesLvl1") or {}).get("CategoryName") or ""
                category_raw  = cat_lvl3 or cat_lvl2 or cat_lvl1 or ""
                category_norm = normalize_category(f"{category_raw} {name}")

                # Gender from the catalog loop (men/women) is authoritative here.
                # ProductGenderName in the API is a numeric category index, not text.
                gender = cat_gender

                # URL: /en-eg/{SeoName}-{ProductVariantIndex}
                seo_name = item.get("SeoName") or ""
                variant_index = item.get("ProductVariantIndex") or ""
                if seo_name and variant_index:
                    product_url = f"https://{domain}/en-eg/{seo_name}-{variant_index}"
                else:
                    product_url = f"https://{domain}/en-eg/"

                batch_products.append({
                    "brand":               brand_name,
                    "external_id":         long_code,
                    "name":                name,
                    "category_raw":        category_raw,
                    "category_normalized": category_norm,
                    "gender":              gender,
                    "sizes_available":     [],
                    "url":                 product_url,
                    "image_url":           item.get("PictureName") or None,
                    "last_seen_at":        datetime.now(timezone.utc).isoformat(),
                    "is_active":           True,
                })

            if not batch_products:
                break

            product_upsert_rows = []
            for i in range(0, len(batch_products), 100):
                res_p = safe_db_execute(
                    supabase.table("products").upsert(
                        batch_products[i:i+100], on_conflict="brand,external_id"
                    )
                )
                if res_p and res_p.data:
                    product_upsert_rows.extend(res_p.data)

            product_id_map  = {row["external_id"]: row["id"] for row in product_upsert_rows}
            products_seen  += len(batch_products)

            # Set first_observed_price on new products (fires once per product lifecycle)
            for item in items:
                db_pid = product_id_map.get(item.get("LongCode"))
                if not db_pid:
                    continue
                # DiscountedPrice is the real current price.
                # Price is the full/original price (equals DiscountedPrice when not on sale).
                # We use DiscountedPrice as the launch price because that's what the
                # customer actually paid on day one.
                disc_price = float(item.get("DiscountedPrice") or 0)
                full_price = float(item.get("Price") or 0)
                fop = disc_price if disc_price > 0 else full_price
                if fop > 0:
                    safe_db_execute(
                        supabase.table("products")
                        .update({"first_observed_price": fop})
                        .eq("id", db_pid)
                        .is_("first_observed_price", "null")
                    )

            # ── Variants upsert ───────────────────────────────────────────
            # One variant row per LongCode (= one color of a product).
            # Size is set to None here; populated by the size pass below.
            batch_variants, product_variant_tracking = [], {}
            for item in items:
                long_code = item.get("LongCode")
                db_pid    = product_id_map.get(long_code)
                if not db_pid:
                    continue
                product_variant_tracking.setdefault(db_pid, [])

                disc_price = float(item.get("DiscountedPrice") or 0)
                full_price = float(item.get("Price") or 0)

                # Sale detection: DiscountedPrice < Price means a real markdown.
                if disc_price > 0 and disc_price < full_price:
                    price      = disc_price
                    compare_at = full_price
                else:
                    price      = full_price if full_price > 0 else disc_price
                    compare_at = None

                if price == 0:
                    continue

                # Stock: total units across all sizes. > 0 means the product
                # has at least one size in stock (we don't know which ones yet).
                stock_qty  = item.get("Stock") or 0
                is_avail   = int(stock_qty) > 0

                # Color: ColorGtmName is the English color name confirmed in API data.
                color_name = item.get("ColorGtmName") or item.get("ColorName") or None

                sku        = f"defacto_{long_code}"
                prev       = prev_stock_state.get(sku)
                v_baseline = float(prev["first_observed_price"]) if (prev and prev.get("first_observed_price")) else price

                batch_variants.append({
                    "product_id":          db_pid,
                    "external_sku":        sku,
                    "color":               color_name,
                    "size":                None,   # filled by size pass
                    "is_in_stock":         is_avail,
                    "first_observed_price": v_baseline,
                    "last_updated_at":     datetime.now(timezone.utc).isoformat(),
                    "_meta_price":         price,
                    "_meta_compare":       compare_at,
                    "_meta_baseline":      v_baseline,
                    "_meta_size":          None,
                    "_meta_color":         color_name,
                    "_meta_available":     is_avail,
                })

            if batch_variants:
                db_payload = [{k: v for k, v in r.items() if not k.startswith("_meta_")} for r in batch_variants]
                variant_upsert_rows = []
                for i in range(0, len(db_payload), 100):
                    res_v = safe_db_execute(
                        supabase.table("product_variants").upsert(
                            db_payload[i:i+100], on_conflict="external_sku"
                        )
                    )
                    if res_v and res_v.data:
                        variant_upsert_rows.extend(res_v.data)

                sku_to_id = {row["external_sku"]: row["id"] for row in variant_upsert_rows}
                for vr in batch_variants:
                    vr["variant_db_id"] = sku_to_id.get(vr["external_sku"])
                    product_variant_tracking[vr["product_id"]].append(vr)

                # ── Inline size population via batch API ──────────────────────
                # Call CombinProductListByProductLongCode with all LongCodes
                # from this page. Returns sizes immediately — no deferred pass.
                page_long_codes = [
                    vr["external_sku"].replace("defacto_", "", 1)
                    for vr in batch_variants
                ]
                if page_long_codes:
                    size_map = fetch_defacto_sizes_batch(session, domain, page_long_codes)
                    now_iso_sz = datetime.now(timezone.utc).isoformat()
                    for vr in batch_variants:
                        lc    = vr["external_sku"].replace("defacto_", "", 1)
                        sizes = size_map.get(lc, [])
                        if not sizes or not vr.get("variant_db_id"):
                            continue
                        # Update first size on existing row
                        safe_db_execute(
                            supabase.table("product_variants")
                            .update({"size": sizes[0]["size"], "is_in_stock": sizes[0]["is_in_stock"], "last_updated_at": now_iso_sz})
                            .eq("id", vr["variant_db_id"])
                        )
                        # Insert additional sizes as new variant rows
                        for sz in sizes[1:]:
                            extra_sku = f"defacto_{lc}_{sz['size'].replace(' ', '_')}"
                            safe_db_execute(
                                supabase.table("product_variants").upsert({
                                    "product_id":           vr["product_id"],
                                    "external_sku":         extra_sku,
                                    "color":                vr.get("_meta_color"),
                                    "size":                 sz["size"],
                                    "is_in_stock":          sz["is_in_stock"],
                                    "first_observed_price": vr.get("_meta_baseline"),
                                    "last_updated_at":      now_iso_sz,
                                }, on_conflict="external_sku")
                            )

                # Snapshots
                snap_rows = build_snapshot_rows(brand_name, product_variant_tracking, today, existing_snapshot_ids)
                if snap_rows:
                    safe_db_execute(supabase.table("price_snapshots").insert(snap_rows))

                # Stockout detection + price events
                for db_pid, records in product_variant_tracking.items():
                    if not records:
                        continue
                    sizes_in_stock = [r["_meta_size"] for r in records if r["_meta_available"] and r["_meta_size"]]
                    # Per-variant stockout detection.
                    for rec in records:
                        prev_v = prev_stock_state.get(rec["external_sku"])
                        if prev_v:
                            detect_and_write_stockout(
                                supabase, rec["variant_db_id"], db_pid, brand_name,
                                rec["_meta_size"], rec["_meta_color"],
                                prev_v["is_in_stock"], rec["_meta_available"],
                                rec["_meta_price"], rec["_meta_baseline"]
                            )
                    # Product-level price detection (v14.8).
                    curr_price = product_repr_price(records)
                    v_base     = product_repr_baseline(records)
                    if curr_price is None:
                        continue
                    last_p = prev_prices.get(db_pid)
                    if last_p is None:
                        prev_prices[db_pid] = curr_price
                    elif abs(last_p - curr_price) > 0.01:
                        direction = "down" if curr_price < last_p else "up"
                        price_changes += 1

                        if direction == "down" and v_base and curr_price < v_base:
                            p_item = next((pi for pi in items
                                           if pi.get("LongCode") == next((k for k, v in product_id_map.items() if v == db_pid), None)), None)
                            if p_item:
                                p_name = p_item.get("Name") or p_item.get("GtmName") or "DeFacto Item"
                                seo    = p_item.get("SeoName") or ""
                                vidx   = p_item.get("ProductVariantIndex") or ""
                                p_url  = f"https://{domain}/en-eg/{seo}-{vidx}" if seo and vidx else f"https://{domain}"
                                cat_lvl3 = (p_item.get("CategoriesLvl3") or {}).get("CategoryName") or ""
                                p_cat  = normalize_category(f"{cat_lvl3} {p_name}")
                                for sz in set(sizes_in_stock):
                                    find_and_alert_users(
                                        supabase, session, brand_name, p_cat,
                                        sz, curr_price, p_name, p_url, v_base
                                    )

                        honest_disc = round(((v_base - curr_price) / v_base) * 100, 2) if (v_base and curr_price < v_base) else None
                        safe_db_execute(supabase.table("price_events").insert({
                            "product_id":       db_pid,
                            "brand":            brand_name,
                            "price_before":     last_p,
                            "price_after":      curr_price,
                            "compare_at_price": records[0].get("_meta_compare"),
                            "discount_pct":     honest_disc,
                            "direction":        direction,
                            "sizes_in_stock":   sizes_in_stock,
                            "recorded_at":      datetime.now(timezone.utc).isoformat(),
                        }))
                        prev_prices[db_pid] = curr_price
                        sync_snapshot_price(supabase, db_pid, today, curr_price)

            print(f"  [{cat_name}] Page {page_index} — {len(items)} items processed.")

            # Follow NextDataUrl to the next page.
            # The API gives us the exact URL — no need to increment page numbers.
            raw_next = data.get("Data", {}).get("NextDataUrl") or data.get("NextDataUrl")
            if raw_next:
                next_url = raw_next
                time.sleep(random.uniform(0.8, 1.5))
            else:
                print(f"  [{cat_name}] No NextDataUrl — end of catalog.")
                break

    # ── DeFacto backfill pass: fix any variants still missing sizes ─────────
    # Catches variants from previous runs before this batch API approach.
    # Loads all null-size variants for this brand, batches their LongCodes
    # into one POST, and writes sizes in bulk. Runs once per scrape.
    try:
        all_missing, miss_offset = [], 0
        while True:
            chunk = safe_db_execute(
                supabase.table("product_variants")
                .select("id, product_id, external_sku, color, is_in_stock, first_observed_price, products!inner(brand)")
                .eq("products.brand", brand_name)
                .is_("size", "null")
                .range(miss_offset, miss_offset + 999)
            )
            rows = (chunk.data or []) if chunk else []
            all_missing.extend(rows)
            if len(rows) < 1000:
                break
            miss_offset += 1000

        if all_missing:
            print(f"  [DeFacto] Backfilling sizes for {len(all_missing)} variants...")
            # Extract LongCode from external_sku: "defacto_E7961AX26SPWT32" -> "E7961AX26SPWT32"
            sku_to_row = {}
            long_codes = []
            for row in all_missing:
                lc = row["external_sku"].replace("defacto_", "", 1)
                sku_to_row[lc] = row
                long_codes.append(lc)

            # Batch in groups of 36 (one catalog page worth) to stay reasonable
            populated = 0
            for batch_start in range(0, len(long_codes), 36):
                batch = long_codes[batch_start:batch_start + 36]
                size_map = fetch_defacto_sizes_batch(session, domain, batch)
                now_iso  = datetime.now(timezone.utc).isoformat()
                for lc, sizes in size_map.items():
                    if not sizes:
                        continue
                    row = sku_to_row.get(lc)
                    if not row:
                        continue
                    product_id = row.get("product_id")
                    color      = row.get("color")
                    fop        = row.get("first_observed_price") or prev_prices.get(product_id)
                    for i, sz in enumerate(sizes):
                        if i == 0:
                            safe_db_execute(
                                supabase.table("product_variants")
                                .update({"size": sz["size"], "is_in_stock": sz["is_in_stock"], "last_updated_at": now_iso})
                                .eq("id", row["id"])
                            )
                        else:
                            sku = f"defacto_{lc}_{sz['size'].replace(' ', '_')}"
                            safe_db_execute(
                                supabase.table("product_variants").upsert({
                                    "product_id":           product_id,
                                    "external_sku":         sku,
                                    "color":                color,
                                    "size":                 sz["size"],
                                    "is_in_stock":          sz["is_in_stock"],
                                    "first_observed_price": fop,
                                    "last_updated_at":      now_iso,
                                }, on_conflict="external_sku")
                            )
                    populated += 1
                time.sleep(random.uniform(0.5, 1.0))
            print(f"  [DeFacto] Backfill complete: {populated} variants populated.")
        else:
            print("  [DeFacto] All variants have size data. ✅")
    except Exception as e:
        print(f"  [DeFacto] Size backfill error (non-fatal): {e}")

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
        if brand_config["engine"] not in ("lcw_proxy", "defacto"):
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
        elif brand_config["engine"] == "defacto":
            seen, changes = scrape_defacto(supabase, session, brand_name, domain, today, prev_stock_state)
        else:
            seen, changes = 0, 0

        print(f"\n  ✅ {brand_name}: {seen} products scanned, {changes} price changes recorded.")
        return changes
    except Exception as e:
        print(f"\n  🚨 CRITICAL FAILURE in {brand_name.upper()} pipeline: {e}")
        print(f"  ⚠️ Quarantining {brand_name} fault. Moving safely to next brand.")
        return 0

if __name__ == "__main__":
    # SCRAPE_TARGET controls which brands this run processes.
    #   SCRAPE_TARGET=shopify  → Shopify brands + DeFacto (no proxy, runs 4x daily)
    #   SCRAPE_TARGET=lcw      → LC Waikiki only (proxy, runs 1x daily)
    #   SCRAPE_TARGET=defacto  → DeFacto only (for isolated testing)
    #   SCRAPE_TARGET=all      → everything (manual one-off runs)
    SCRAPE_TARGET = os.environ.get("SCRAPE_TARGET", "all").lower()
    if SCRAPE_TARGET == "shopify":
        # DeFacto uses no proxy — it joins the no-cost daily group
        active_brands = [b for b in BRANDS if b["engine"] in ("shopify", "defacto")]
    elif SCRAPE_TARGET == "lcw":
        active_brands = [b for b in BRANDS if b["engine"] == "lcw_proxy"]
    elif SCRAPE_TARGET == "defacto":
        active_brands = [b for b in BRANDS if b["engine"] == "defacto"]
    else:
        active_brands = BRANDS
    print(f"🚀 Khabar Scraper starting... target={SCRAPE_TARGET} ({len(active_brands)} brands)")
    startup_jitter = random.uniform(0, 30)
    print(f"  Startup jitter: {startup_jitter:.1f}s")
    time.sleep(startup_jitter)

    if SCRAPE_TARGET in ("lcw", "all"):
        try:
            _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
            cutoff_snap = str(date.today() - timedelta(days=90))
            safe_db_execute(
                _sb.table("price_snapshots").delete().lt("snapshot_date", cutoff_snap)
            )
            # price_events is the rolling 30-day alert engine — prune older rows so
            # the table stays bounded. (Restored in v14.7; the purge had been
            # dropped during the v14 refactor, letting the table grow without limit.)
            # price_snapshots remains the permanent warehouse for long-range history.
            cutoff_events = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            safe_db_execute(
                _sb.table("price_events").delete().lt("recorded_at", cutoff_events)
            )
            now_iso     = datetime.now(timezone.utc).isoformat()
            cutoff_seen = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
            stale_products = safe_db_execute(
                _sb.table("products")
                .select("id, brand")
                .eq("is_active", True)
                .lt("last_seen_at", cutoff_seen)
                .limit(500)
            )
            if stale_products and stale_products.data:
                pids = [r["id"] for r in stale_products.data]
                safe_db_execute(
                    _sb.table("products")
                    .update({"is_active": False, "delisted_at": now_iso})
                    .in_("id", pids)
                )
                print(f"  Marked {len(pids)} stale products as delisted.")
            stale_variants = safe_db_execute(
                _sb.table("product_variants")
                .select("id, product_id, size, color, products!inner(brand, last_seen_at)")
                .is_("delisted_at", "null")
                .lt("products.last_seen_at", cutoff_seen)
                .limit(200)
            )
            if stale_variants and stale_variants.data:
                event_rows = [{
                    "variant_id":            v["id"],
                    "product_id":            v.get("product_id"),
                    "brand":                 (v.get("products") or {}).get("brand"),
                    "size":                  v.get("size"),
                    "color":                 v.get("color"),
                    "event_type":            "delisted",
                    "price_at_event":        None,
                    "discount_pct_at_event": None,
                    "was_on_discount":       False,
                    "recorded_at":           now_iso,
                } for v in stale_variants.data]
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

    try:
        _sb2 = create_client(SUPABASE_URL, SUPABASE_KEY)
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
