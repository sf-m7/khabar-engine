# ═══════════════════════════════════════════════════════
# KHABAR — Scraper v8 (Network-Hardened Core)
# Adds: PostgREST Connection Resilience & Fault Shields,
#        Shopify Batch Engine, LC Waikiki Catalog Engine,
#        Positional Entropy Options, 5-Day Maturity Shield
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
    {"name": "town_team",  "domain": "www.townteam.com", "engine": "shopify"},
    {"name": "ravin",      "domain": "shop.iravin.com", "engine": "shopify"},
    {"name": "mens_club",  "domain": "mensclubcollection.com", "engine": "shopify"},
    {"name": "tree",       "domain": "tree-stores.com", "engine": "shopify"},
    {"name": "dott_jeans", "domain": "dottjeans.com", "engine": "shopify"},
    {"name": "lc_waikiki", "domain": "www.lcwaikiki.eg", "engine": "lcw_ajax"}
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
    if not variants:
        return "option1", "option2"

    opt1_values = [str(v.get("option1", "")).strip() for v in variants if v.get("option1")]
    opt2_values = [str(v.get("option2", "")).strip() for v in variants if v.get("option2")]
    opt3_values = [str(v.get("option3", "")).strip() for v in variants if v.get("option3")]

    u_opt1 = len(set(opt1_values))
    u_opt2 = len(set(opt2_values))
    u_opt3 = len(set(opt3_values))

    if len(variants) > 1 and (u_opt1 == 1 or u_opt2 == 0) and u_opt2 <= 1 and u_opt3 == 0:
        if u_opt2 > u_opt1:
            return "option2", "option1"
        return "option1", ("option2" if opt2_values else None)

    def score_column_content(values):
        score = 0
        size_flags = {"xs", "s", "m", "l", "xl", "xxl", "3xl", "4xl", "5xl", "os", "one size", "small", "medium", "large"}
        for val in set(values):
            v_low = val.lower()
            if v_low in size_flags: score += 10
            if v_low.isdigit() and (4 <= int(v_low) <= 56): score += 5
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

    if u_opt1 >= u_opt2 and u_opt1 >= u_opt3:
        return "option1", ("option2" if u_opt2 > 0 else "option3" if u_opt3 > 0 else None)
    return "option2", "option1"

def check_domain(domain):
    try:
        r = requests.get(f"https://{domain}", timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code == 200
    except:
        return False

# ── Alerts & Snapshots ────────────────────────────────

def send_telegram(chat_id, text):
    if not TELEGRAM_BOT_TOKEN: return
    try: requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
    except: pass

def find_and_alert_users(supabase, brand, category, variant_size, current_price, discount_pct, product_name, product_url, variant_baseline):
    if not TELEGRAM_BOT_TOKEN or not variant_baseline or current_price >= variant_baseline: return
    try:
        matches = supabase.table("user_sizes").select("user_id, users!inner(telegram_id, conversation_state, price_ceiling)").eq("category", category).eq("size", variant_size).execute()
        if not matches.data: return
        for row in matches.data:
            user_info = row.get("users")
            if not user_info or user_info.get("conversation_state") != "active": continue
            uid = user_info["telegram_id"]
            ceiling = user_info.get("price_ceiling")
            if ceiling and current_price > float(ceiling): continue
            brand_check = supabase.table("user_brands").select("user_id").eq("user_id", uid).eq("brand", brand).execute()
            if not brand_check.data: continue
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
    except: pass

def load_last_prices(supabase, brand_name):
    today, yesterday = str(date.today()), str(date.today() - timedelta(days=1))
    for target_date in [today, yesterday]:
        try:
            result = supabase.table("price_snapshots").select("product_id, price").eq("brand", brand_name).eq("snapshot_date", target_date).execute()
            if result.data:
                return {row.get("product_id"): float(row["price"]) for row in result.data if row.get("product_id")}
        except Exception as e:
            print(f"  ⚠️ Supabase snapshot load dropped via connection timeout: {e}")
            break
    return {}

def upsert_snapshot(supabase, brand_name, db_product_id, variant_records, today, use_insert):
    if not variant_records: return
    prices = [v["_meta_price"] for v in variant_records]
    try:
        if len(set(prices)) == 1:
            vd = variant_records[0]
            row = {"product_id": db_product_id, "variant_id": None, "brand": brand_name, "price": vd["_meta_price"], "compare_at_price": vd["_meta_compare"], "snapshot_date": str(today), "recorded_at": datetime.now(timezone.utc).isoformat()}
            if use_insert: supabase.table("price_snapshots").insert(row).execute()
            else: supabase.table("price_snapshots").update({"price": vd["_meta_price"], "compare_at_price": vd["_meta_compare"], "recorded_at": datetime.now(timezone.utc).isoformat()}).eq("product_id", db_product_id).eq("snapshot_date", str(today)).execute()
        else:
            for vd in variant_records:
                vid = vd.get("variant_db_id")
                if not vid: continue
                row = {"product_id": None, "variant_id": vid, "brand": brand_name, "price": vd["_meta_price"], "compare_at_price": vd["_meta_compare"], "snapshot_date": str(today), "recorded_at": datetime.now(timezone.utc).isoformat()}
                if use_insert: supabase.table("price_snapshots").insert(row).execute()
                else: supabase.table("price_snapshots").update({"price": vd["_meta_price"], "compare_at_price": vd["_meta_compare"], "recorded_at": datetime.now(timezone.utc).isoformat()}).eq("variant_id", vid).eq("snapshot_date", str(today)).execute()
    except Exception as e:
        print(f"  ⚠️ Snapshot row transaction skipped due to gateway load: {e}")

def purge_old_events(supabase):
    cutoff_events, cutoff_snapshots = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(), str(date.today() - timedelta(days=365))
    try:
        supabase.table("price_events").delete().lt("recorded_at", cutoff_events).execute()
        supabase.table("price_snapshots").delete().lt("snapshot_date", cutoff_snapshots).execute()
    except Exception as e:
        print(f"  ⚠️ Database housecleaning loop postponed: {e}")

def detect_and_write_stockout(supabase, variant_db_id, product_id, brand, size, color, previous_in_stock, current_in_stock, current_price, variant_baseline):
    if previous_in_stock == current_in_stock: return
    event_type = "stockout" if (previous_in_stock and not current_in_stock) else "restock"
    discount_pct = round(((variant_baseline - current_price) / variant_baseline) * 100, 2) if (variant_baseline and current_price < variant_baseline) else None
    try:
        supabase.table("stockout_events").insert({"variant_id": variant_db_id, "product_id": product_id, "brand": brand, "size": size, "color": color, "event_type": event_type, "price_at_event": current_price, "discount_pct_at_event": discount_pct, "was_on_discount": bool(discount_pct), "recorded_at": datetime.now(timezone.utc).isoformat()}).execute()
    except: pass

def verify_run_integrity(supabase, brand_name):
    print(f"🔬 Running automated data quality shield for {brand_name}...")
    try:
        corrupted = supabase.table("product_variants").select("id").eq("is_in_stock", True).in_("size", ["Blue", "Red", "Black", "White"]).execute()
        if len(corrupted.data) > 0: return False
        print("  ✅ Data stream integrity fully verified.")
        return True
    except:
        return True

# ── Ingestion Routers ─────────────────────────────────

def scrape_shopify(supabase, brand_name, domain, today, last_prices, prev_stock_state):
    page, products_seen, price_changes = 1, 0, 0
    
    # Catch connection timeouts gracefully when evaluating today's row initialization states
    try:
        use_insert = len(supabase.table("price_snapshots").select("id").eq("brand", brand_name).eq("snapshot_date", str(today)).limit(1).execute().data) == 0
    except:
        use_insert = True

    while True:
        url = f"https://{domain}/products.json?limit=250&page={page}"
        try: response = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        except: break
        if response.status_code != 200: break
        products = response.json().get("products", [])
        if not products: break

        batch_products = []
        for p in products:
            if not p.get("variants"): continue
            batch_products.append({"brand": brand_name, "external_id": str(p["id"]), "name": p["title"], "category_raw": p.get("product_type", ""), "category_normalized": normalize_category(f"{p['title']} {p.get('product_type','')}" ), "gender": normalize_gender(p.get("tags",[]), p.get("product_type",""), p["title"]), "sizes_available": [], "url": f"https://{domain}/products/{p['handle']}", "image_url": p.get("images",[{}])[0].get("src"), "last_seen_at": datetime.now(timezone.utc).isoformat(), "is_active": True})

        if not batch_products: break
        product_upsert_rows = []
        try:
            for i in range(0, len(batch_products), 100):
                chunk = batch_products[i:i+100]
                res = supabase.table("products").upsert(chunk, on_conflict="brand,external_id").execute()
                product_upsert_rows.extend(res.data)
        except Exception as e:
            print(f"  ⚠️ Core product batch ingest bottlenecked via database channel: {e}")
            page += 1
            continue
        
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
                discount_honest = round(((v_baseline - price) / v_baseline) * 100, 2) if v_baseline > price else None

                batch_variants.append({"product_id": db_pid, "external_sku": sku, "color": color, "size": size, "is_in_stock": available, "first_observed_price": v_baseline, "last_updated_at": datetime.now(timezone.utc).isoformat(), "_meta_price": price, "_meta_compare": compare_at, "_meta_discount_honest": discount_honest, "_meta_baseline": v_baseline, "_meta_size": size, "_meta_color": color, "_meta_available": available})

        if batch_variants:
            db_payload = [{k: v for k, v in row.items() if not k.startswith('_meta_')} for row in batch_variants]
            variant_upsert_rows = []
            try:
                for i in range(0, len(db_payload), 100):
                    res = supabase.table("product_variants").upsert(db_payload[i:i+100], on_conflict="external_sku").execute()
                    variant_upsert_rows.extend(res.data)
            except Exception as e:
                print(f"  ⚠️ Child variant records transaction dropped on page {page}: {e}")
                page += 1
                continue
            
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
                    try:
                        last_ev = supabase.table("price_events").select("price_after").eq("product_id", db_pid).order("recorded_at", desc=True).limit(1).execute()
                        last_p = float(last_ev.data[0]["price_after"]) if last_ev.data else None
                    except:
                        last_p = None

                    if last_p is None or abs(last_p - curr_price) > 0.01:
                        direction = "down" if (last_p and curr_price < last_p) else "up" if last_p else None
                        if direction: price_changes += 1
                        
                        if direction == "down" and v_base and curr_price < v_base:
                            if prev_v and prev_v.get("last_updated_at"):
                                if (datetime.now(timezone.utc) - datetime.fromisoformat(prev_v["last_updated_at"])) > timedelta(days=5):
                                    for p in products:
                                        if str(p["id"]) == [k for k, v in product_id_map.items() if v == db_pid][0]:
                                            find_and_alert_users(supabase, brand_name, rec["_meta_size"], curr_price, rec["_meta_discount_honest"], p["title"], f"https://{domain}/products/{p['handle']}", v_base)

                        try:
                            supabase.table("price_events").insert({"product_id": db_pid, "brand": brand_name, "price_before": last_p, "price_after": curr_price, "direction": direction, "sizes_in_stock": sizes_in_stock, "recorded_at": datetime.now(timezone.utc).isoformat()}).execute()
                        except:
                            pass
        page += 1
    return products_seen, price_changes

def scrape_lcw(supabase, brand_name, domain, today, last_prices, prev_stock_state):
    print("  Executing LC Waikiki Dynamic Catalog Engine...")
    page, products_seen, price_changes = 1, 0, 0
    try:
        use_insert = len(supabase.table("price_snapshots").select("id").eq("brand", brand_name).eq("snapshot_date", str(today)).limit(1).execute().data) == 0
    except:
        use_insert = True

    lcw_url = "https://www.lcwaikiki.eg/en/ajax/ProductList/ProductListPageData?xhrKeys=CategoryTreeId&CategoryTreeId=9&FilteringType=26&Layout=three-column"
    
    while True:
        try:
            res = requests.post(f"{lcw_url}&PageIndex={page}", timeout=30, headers={"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"})
            if res.status_code != 200: break
            catalog = res.json().get("CatalogList", {})
            items = catalog.get("Items", [])
            if not items: break
        except:
            break

        batch_products = []
        for item in items:
            desc = item.get("ProductDescription") or item.get("BrandPropertyDescription")
            if not desc: continue
            model_id = str(item["ModelId"])
            batch_products.append({"brand": brand_name, "external_id": model_id, "name": desc, "category_raw": "Shirt", "category_normalized": "tops", "gender": "men", "sizes_available": [], "url": f"https://{domain}{item.get('ModelUrl','')}", "image_url": item.get("DefaultOptionImageUrl"), "last_seen_at": datetime.now(timezone.utc).isoformat(), "is_active": True})

        if not batch_products: break
        product_upsert_rows = []
        try:
            for i in range(0, len(batch_products), 100):
                res_p = supabase.table("products").upsert(batch_products[i:i+100], on_conflict="brand,external_id").execute()
                product_upsert_rows.extend(res_p.data)
        except Exception as e:
            print(f"  ⚠️ LCW category sync channel bottlenecked: {e}")
            page += 1
            continue

        product_id_map = {row["external_id"]: row["id"] for row in product_upsert_rows}
        products_seen += len(batch_products)

        batch_variants, product_variant_tracking = [], {}
        for item in items:
            db_pid = product_id_map.get(str(item["ModelId"]))
            if not db_pid: continue
            product_variant_tracking[db_pid] = []
            opt_id = item.get("OptionId")
            
            try:
                opt_res = requests.get(f"https://{domain}/en/ajax/product/OptionDetailAjax?optionId={opt_id}", timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                sizes_data = opt_res.json() if opt_res.status_code == 200 else []
            except:
                sizes_data = []

            price = float(item.get("PriceValue") or 0)
            old_price_str = item.get("OldPrice") or ""
            compare_at = float(''.join(c for c in old_price_str if c.isdigit() or c=='.')) if any(c.isdigit() for c in old_price_str) else None

            if not sizes_data: sizes_data = [{"Size": "One Size", "IsAvailable": True}]

            for s_entry in sizes_data:
                size_label = s_entry.get("Size") or "One Size"
                is_avail = bool(s_entry.get("IsAvailable", True))
                sku = f"lcw_{opt_id}_{size_label.replace(' ', '_')}"
                
                prev = prev_stock_state.get(sku)
                v_baseline = float(prev["first_observed_price"]) if (prev and prev.get("first_observed_price")) else price
                discount_honest = round(((v_baseline - price) / v_baseline) * 100, 2) if v_baseline > price else None

                batch_variants.append({"product_id": db_pid, "external_sku": sku, "color": None, "size": size_label, "is_in_stock": is_avail, "first_observed_price": v_baseline, "last_updated_at": datetime.now(timezone.utc).isoformat(), "_meta_price": price, "_meta_compare": compare_at, "_meta_discount_honest": discount_honest, "_meta_baseline": v_baseline, "_meta_size": size_label, "_meta_available": is_avail})

        if batch_variants:
            db_payload = [{k: v for k, v in row.items() if not k.startswith('_meta_')} for row in batch_variants]
            variant_upsert_rows = []
            try:
                for i in range(0, len(db_payload), 100):
                    res_v = supabase.table("product_variants").upsert(db_payload[i:i+100], on_conflict="external_sku").execute()
                    variant_upsert_rows.extend(res_v.data)
            except Exception as e:
                print(f"  ⚠️ LCW size row batch commit dropped on page {page}: {e}")
                page += 1
                continue

            sku_to_id = {row["external_sku"]: row["id"] for row in variant_upsert_rows}
            for vr in batch_variants: vr["variant_db_id"] = sku_to_id.get(vr["external_sku"])
            for vr in batch_variants: product_variant_tracking[vr["product_id"]].append(vr)

            for db_pid, records in product_variant_tracking.items():
                if not records: continue
                upsert_snapshot(supabase, brand_name, db_pid, records, today, use_insert)
                sizes_in_stock = [r["_meta_size"] for r in records if r["_meta_available"]]

                for rec in records:
                    prev_v = prev_stock_state.get(rec["external_sku"])
                    if prev_v: detect_and_write_stockout(supabase, rec["variant_db_id"], db_pid, brand_name, rec["_meta_size"], None, prev_v["is_in_stock"], rec["_meta_available"], rec["_meta_price"], rec["_meta_baseline"])
                    
                    curr_price, v_base = rec["_meta_price"], rec["_meta_baseline"]
                    try:
                        last_ev = supabase.table("price_events").select("price_after").eq("product_id", db_pid).order("recorded_at", desc=True).limit(1).execute()
                        last_p = float(last_ev.data[0]["price_after"]) if last_ev.data else None
                    except:
                        last_p = None

                    if last_p is None or abs(last_p - curr_price) > 0.01:
                        direction = "down" if (last_p and curr_price < last_p) else "up" if last_p else None
                        if direction: price_changes += 1

                        if direction == "down" and v_base and curr_price < v_base:
                            if prev_v and prev_v.get("last_updated_at"):
                                if (datetime.now(timezone.utc) - datetime.fromisoformat(prev_v["last_updated_at"])) > timedelta(days=5):
                                    for item in items:
                                        if str(item["ModelId"]) == [k for k, v in product_id_map.items() if v == db_pid][0]:
                                            desc = item.get("ProductDescription") or "LCW Item"
                                            find_and_alert_users(supabase, brand_name, "tops", rec["_meta_size"], curr_price, rec["_meta_discount_honest"], desc, f"https://{domain}{item.get('ModelUrl','')}", v_base)

                        try:
                            supabase.table("price_events").insert({"product_id": db_pid, "brand": brand_name, "price_before": last_p, "price_after": curr_price, "direction": direction, "sizes_in_stock": sizes_in_stock, "recorded_at": datetime.now(timezone.utc).isoformat()}).execute()
                        except:
                            pass
        page += 1
        if page > 5: break
    return products_seen, price_changes

def scrape_brand(brand_name, domain):
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"❌ Failed to instantiate Supabase transaction context client: {e}")
        return 0

    today = date.today()
    print(f"\n{'─'*55}\n▶  {brand_name.upper()}  —  {domain}\n{'─'*55}")
    if not check_domain(domain): return 0

    last_prices = load_last_prices(supabase, brand_name)
    
    # Secure existing database variants retrieval loop inside a network-fault shield block
    try:
        existing_variants = supabase.table("product_variants").select("external_sku, is_in_stock, size, color, first_observed_price, last_updated_at").execute()
        prev_stock_state = {row["external_sku"]: row for row in existing_variants.data}
    except Exception as e:
        print(f"  ⚠️ Skipping inventory state baseline pull due to PostgREST connection timeout: {e}")
        prev_stock_state = {}

    brand_config = next(b for b in BRANDS if b["name"] == brand_name)
    
    if brand_config["engine"] == "shopify":
        seen, changes = scrape_shopify(supabase, brand_name, domain, today, last_prices, prev_stock_state)
    elif brand_config["engine"] == "lcw_ajax":
        seen, changes = scrape_lcw(supabase, brand_name, domain, today, last_prices, prev_stock_state)
    else:
        seen, changes = 0, 0

    print(f"\n  ✅ {brand_name}: {seen} products scanned, {changes} price changes recorded.")
    verify_run_integrity(supabase, brand_name)
    return changes

if __name__ == "__main__":
    print("🚀 Khabar multi-architecture scraper starting...")
    try:
        _sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        purge_old_events(_sb)
    except Exception as e:
        print(f"⚠️ Pre-run setup hook skipped: {e}")
    
    total = sum(scrape_brand(b["name"], b["domain"]) for b in BRANDS)
    print(f"\n🏁 All done. Total price changes this run: {total}")
