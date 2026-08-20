"""
TRUE load test — runs in GitHub Actions, read-only (no DB writes).

Reproduces the real burst that actually triggers Cloudflare: every brand's FULL
catalog, all pages, back-to-back, DIRECT (no proxy), from the one datacenter IP.
This is the worst case (in production most brands are proxied and don't hit this
IP), which is exactly the "what if we moved everyone direct?" question.

Per brand it records: pages crawled, products seen, decompressed KB downloaded,
and the FIRST blocking status (403/429) with the page it happened on. No retries
per page — we want to see the raw block, not mask it.

Pacing between brands mirrors the real scraper's inter-brand pause so the burst
cadence is realistic. Tune with PACE_MIN / PACE_MAX env vars.

Reading the result:
  - Brand ends CLEAN (no block, reached end of catalog) -> real candidate to
    move off the proxy.
  - Brand BLOCKED at any page -> must stay on the proxy.
  - Total KB across CLEAN brands ~= the catalog bandwidth you'd move off the
    proxy meter (actual proxy billing is the gzipped wire size, roughly 1/4 of
    the decompressed KB shown here).
"""
import os, time, random
from curl_cffi import requests

PACE_MIN = float(os.environ.get("PACE_MIN", "6"))
PACE_MAX = float(os.environ.get("PACE_MAX", "12"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "80"))

# Same order as the real BRANDS list — order affects burst timing.
SHOPIFY = [
    ("dalydress", "dalydress.com"), ("arafa", "arafastores.com"), ("eagle", "eagle.com.eg"),
    ("tie_house", "tie-house.com"), ("premoda", "www.premoda.net"), ("just_sbr", "www.justsbr.com"),
    ("activ", "activ.eg"), ("mlameh", "mlameh.com"), ("khotwh", "khotwh.com"),
    ("tomato", "www.tomatostores.com"), ("esla", "esla-store.com"), ("town_team", "www.townteam.com"),
    ("ravin", "shop.iravin.com"), ("mens_club", "mensclubcollection.com"), ("tree", "tree-stores.com"),
    ("dott_jeans", "dottjeans.com"), ("carina", "carina.eg"), ("andora", "www.andoraeg.com"),
    ("cizaro", "cizaro.net"),
]
WOO = [("mobaco", "mobaco.com")]

HDRS = {"User-Agent": "Mozilla/5.0"}

def get(url):
    return requests.get(url, impersonate="chrome124", timeout=20, headers=HDRS)

def crawl_shopify(domain):
    d = domain.rstrip("/")
    pages = products = kbytes = 0
    blocked = None
    for page in range(1, MAX_PAGES + 1):
        url = f"https://{d}/products.json?limit=250&page={page}"
        try:
            r = get(url)
        except Exception as e:
            blocked = f"ERR:{type(e).__name__}@p{page}"
            break
        if r.status_code in (403, 429):
            blocked = f"{r.status_code}@p{page}"
            break
        if r.status_code != 200:
            blocked = f"{r.status_code}@p{page}"
            break
        kbytes += len(r.content) / 1024
        items = r.json().get("products", [])
        if not items:
            break
        pages += 1
        products += len(items)
    # one best-selling call too, mirroring the real run's extra fetch
    if blocked is None:
        try:
            bs = get(f"https://{d}/collections/all/products.json?sort_by=best-selling&limit=150")
            if bs.status_code in (403, 429):
                blocked = f"bestseller {bs.status_code}"
            else:
                kbytes += len(bs.content) / 1024
        except Exception as e:
            blocked = f"bestseller ERR:{type(e).__name__}"
    return pages, products, kbytes, blocked

def crawl_woo(domain):
    d = domain.rstrip("/")
    pages = products = kbytes = 0
    blocked = None
    for page in range(1, MAX_PAGES + 1):
        url = f"https://{d}/wp-json/wc/store/v1/products?per_page=100&page={page}"
        try:
            r = get(url)
        except Exception as e:
            blocked = f"ERR:{type(e).__name__}@p{page}"
            break
        if r.status_code in (403, 429) or r.status_code != 200:
            blocked = f"{r.status_code}@p{page}"
            break
        kbytes += len(r.content) / 1024
        items = r.json()
        if not items:
            break
        pages += 1
        products += len(items)
    return pages, products, kbytes, blocked

print(f"{'brand':<12} {'pages':>5} {'prods':>6} {'KB(decomp)':>11}  result")
print("-" * 60)
total_kb = clean_kb = 0.0
clean = []
blocked_list = []

work = [(n, d, "shopify") for n, d in SHOPIFY] + [(n, d, "woo") for n, d in WOO]
for i, (name, dom, kind) in enumerate(work):
    fn = crawl_shopify if kind == "shopify" else crawl_woo
    pages, prods, kb, blocked = fn(dom)
    total_kb += kb
    if blocked is None:
        result = "CLEAN -> can go direct"
        clean.append(name); clean_kb += kb
    else:
        result = f"BLOCKED {blocked} -> stay on proxy"
        blocked_list.append(name)
    print(f"{name:<12} {pages:>5} {prods:>6} {kb:>11.0f}  {result}")
    if i < len(work) - 1:
        time.sleep(random.uniform(PACE_MIN, PACE_MAX))

print("-" * 60)
print(f"TOTAL decompressed: {total_kb/1024:.1f} MB  (proxy-billed ~= {total_kb/1024/4:.1f} MB gzipped)")
print(f"CLEAN brands ({len(clean)}): {', '.join(clean) or '-'}")
print(f"  -> movable off proxy ~= {clean_kb/1024/4:.1f} MB gzipped per run")
print(f"BLOCKED brands ({len(blocked_list)}): {', '.join(blocked_list) or '-'}")
