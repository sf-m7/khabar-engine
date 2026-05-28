import os
import requests
from flask import Flask
from supabase import create_client, Client

app = Flask(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

BRANDS = {
    "town_team": "https://www.townteam.com",
    "ravin": "https://ravin.com",
    "mens_club": "https://mensclub.com",
    "2s": "https://2s-egypt.com"
}

def process_brand(brand_id, base_url):
    endpoint = f"{base_url}/products.json?limit=250"
    try:
        response = requests.get(endpoint, timeout=15)
        if response.status_code != 200: return
            
        products = response.json().get("products", [])
        for item in products:
            ext_id = str(item.get("id"))
            title = item.get("title", "")
            url = f"{base_url}/products/{item.get('handle')}"
            
            variants = item.get("variants", [])
            if not variants: continue
                
            primary = variants[0]
            current_price = float(primary.get("price") or 0)
            compare_price_raw = primary.get("compare_at_price")
            compare_price = float(compare_price_raw) if compare_price_raw else None
            
            sizes_avail = list(set([v.get("option1") for v in variants if v.get("option1")]))
            existing = supabase.table("products").select("id").eq("brand", brand_id).eq("external_id", ext_id).execute()
            
            if not existing.data:
                res = supabase.table("products").insert({
                    "brand": brand_id, "external_id": ext_id, "name": title, 
                    "url": url, "sizes_available": sizes_avail
                }).execute()
                prod_id = res.data[0]["id"]
                supabase.table("price_events").insert({
                    "product_id": prod_id, "brand": brand_id, "price_before": current_price,
                    "price_after": current_price, "compare_at_price": compare_price, "direction": "initial"
                }).execute()
            else:
                prod_id = existing.data[0]["id"]
                last_event = supabase.table("price_events").select("price_after").eq("product_id", prod_id).order("recorded_at", desc=True).limit(1).execute()
                if last_event.data:
                    old_price = float(last_event.data[0]["price_after"])
                    if old_price != current_price:
                        direction = "down" if current_price < old_price else "up"
                        discount = round(((compare_price - current_price) / compare_price) * 100, 2) if compare_price and current_price < compare_price else None
                        supabase.table("price_events").insert({
                            "product_id": prod_id, "brand": brand_id, "price_before": old_price,
                            "price_after": current_price, "compare_at_price": compare_price, 
                            "discount_pct": discount, "direction": direction
                        }).execute()
                supabase.table("products").update({"last_seen_at": "now()", "is_active": True}).eq("id", prod_id).execute()
    except Exception:
        pass

@app.route('/')
def home():
    return "Engine Active", 200

@app.route('/run-scraper')
def run_scraper():
    for brand_id, url in BRANDS.items():
        process_brand(brand_id, url)
    return "Sync Complete", 200
