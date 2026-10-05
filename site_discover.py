#!/usr/bin/env python3
"""Find the data API behind a headless storefront (e.g. Mobaco's new Nuxt site).
Usage: python site_discover.py https://mobaco.com/en/women"""
import re, sys, json
from curl_cffi import requests

start = sys.argv[1]
s = requests.Session(impersonate="chrome124")
H = {"accept-language": "en-US,en;q=0.9"}
KEY = re.compile(r'(graphql|hypernode|/rest/V1|/api/[\w/\-]+|baseURL|apiUrl|API_URL|magento|algolia|elastic|typesense|meilisearch)', re.I)

def show_hits(label, text, limit=25):
    hits = sorted({m.group(0) if len(m.group(0)) > 6 else m.group(0) for m in KEY.finditer(text)})
    urls = sorted(set(re.findall(r'https?://[\w\.\-]+(?:/[\w\.\-/%?=&]*)?', text)))
    urls = [u for u in urls if re.search(r'hypernode|graphql|api|rest|search|algolia|elastic', u, re.I)]
    print(f"[{label}] keyword hits: {hits[:limit]}")
    print(f"[{label}] candidate urls: {urls[:limit]}")

r = s.get(start, headers=H, timeout=45)
print("PAGE", r.status_code, len(r.text))
html = r.text
show_hits("html", html)
m = re.search(r'window\.__NUXT__\s*=\s*(.{0,600})', html, re.S)
print("NUXT state head:", (m.group(1)[:600] if m else None))
m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.{0,800})', html, re.S)
print("NUXT_DATA head:", (m.group(1)[:800] if m else None))
scripts = re.findall(r'src="([^"]+\.js)"', html)
print("scripts:", scripts[:12])
for sc in scripts[:8]:
    u = sc if sc.startswith("http") else "https://" + start.split("/")[2] + sc
    try:
        t = s.get(u, headers=H, timeout=45).text
        show_hits(u.split("/")[-1], t, 15)
    except Exception as e:
        print("script fail", u, e)

host = start.split("/")[2]
back = [f"https://{host.split('.')[0]}.hypernode.io", f"https://{host}"]
for b in back:
    for path, body in [("/graphql", {"query": "{ storeConfig { store_code base_currency_code } }"}),
                       ("/graphql", {"query": '{ products(search:"", pageSize:2){ total_count items{ sku name price_range{minimum_price{final_price{value} regular_price{value}}} } } }'})]:
        try:
            x = s.post(b + path, json=body, headers={"content-type": "application/json", **H}, timeout=30)
            print(f"POST {b}{path} -> {x.status_code} {x.text[:300]!r}")
        except Exception as e:
            print(f"POST {b}{path} -> ERR {str(e)[:100]}")
    for path in ["/rest/V1/store/storeConfigs", "/sitemap.xml", "/api/products", "/wp-json"]:
        try:
            x = s.get(b + path, headers=H, timeout=30)
            print(f"GET {b}{path} -> {x.status_code} {x.headers.get('content-type')} {x.text[:150]!r}")
        except Exception as e:
            print(f"GET {b}{path} -> ERR {str(e)[:100]}")
