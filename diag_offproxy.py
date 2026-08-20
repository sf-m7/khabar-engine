"""
Off-proxy test. Runs INSIDE GitHub Actions (same datacenter IP the real scraper
uses) and tries each currently-proxied brand on a DIRECT connection — no proxy.

For a brand to safely leave DATAIMPULSE_PROXY_BRANDS it must return 200 DIRECT
on BOTH the catalog endpoint AND the best-selling endpoint, ideally on repeat
attempts (Cloudflare is intermittent). Uses limit=1 so the test itself is cheap.
Mirrors the scraper's client (curl_cffi chrome124), no proxy attached.
"""
import time
from curl_cffi import requests

# name -> domain (Shopify + the one WooCommerce brand). Excludes lc_waikiki,
# defacto (own engines, already direct) and rojada (disabled).
SHOPIFY = {
    "dalydress": "dalydress.com", "arafa": "arafastores.com", "eagle": "eagle.com.eg",
    "tie_house": "tie-house.com", "premoda": "www.premoda.net", "just_sbr": "www.justsbr.com",
    "activ": "activ.eg", "mlameh": "mlameh.com", "khotwh": "khotwh.com",
    "tomato": "www.tomatostores.com", "esla": "esla-store.com", "town_team": "www.townteam.com",
    "ravin": "shop.iravin.com", "mens_club": "mensclubcollection.com", "tree": "tree-stores.com",
    "dott_jeans": "dottjeans.com", "carina": "carina.eg", "andora": "www.andoraeg.com",
    "cizaro": "cizaro.net",
}
WOO = {"mobaco": "mobaco.com"}

def probe(url):
    try:
        r = requests.get(url, impersonate="chrome124", timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"})
        return str(r.status_code)
    except Exception as e:
        return "ERR:" + type(e).__name__

def cell(url, tries=2):
    out = []
    for _ in range(tries):
        out.append(probe(url))
        time.sleep(1)
    return "/".join(out)

print(f"{'brand':<12} {'catalog(direct)':<18} {'bestseller(direct)':<20} verdict")
print("-" * 68)
for name, dom in {**SHOPIFY, **WOO}.items():
    d = dom.rstrip("/")
    if name in WOO:
        cat = cell(f"https://{d}/wp-json/wc/store/v1/products?per_page=1")
        bs = "n/a"
        ok = cat.count("200") == 2
    else:
        cat = cell(f"https://{d}/products.json?limit=1")
        bs = cell(f"https://{d}/collections/all/products.json?sort_by=best-selling&limit=1")
        ok = cat.count("200") == 2 and bs.count("200") == 2
    verdict = "LEAVE proxy" if ok else "STAY on proxy"
    print(f"{name:<12} {cat:<18} {bs:<20} {verdict}")
