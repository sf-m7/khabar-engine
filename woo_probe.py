#!/usr/bin/env python3
"""Probe WooCommerce Store API capabilities for given domains.
Usage: python woo_probe.py mobaco.com rojada-egy.com <coup-domain>
Reports: reachability, latency, bulk type=variation support, currency, rate-limit behaviour."""
import sys, time, json
from curl_cffi import requests

H = {"accept": "application/json, text/plain, */*", "accept-language": "en-US,en;q=0.9"}

def get(s, url):
    t = time.time()
    try:
        r = s.get(url, headers=H, timeout=45)
        return r, round(time.time() - t, 2)
    except Exception as e:
        return None, f"ERR {str(e)[:80]}"

for d in sys.argv[1:]:
    print(f"\n===== {d} =====")
    s = requests.Session(impersonate="chrome124")
    base = f"https://{d}/wp-json/wc/store/v1"
    r, t = get(s, f"{base}/products?per_page=100&page=1")
    if r is None: print("products: unreachable", t); continue
    print(f"products p1: HTTP {r.status_code} in {t}s")
    if r.status_code != 200: print(r.text[:200]); continue
    rows = r.json()
    types = {}
    for x in rows: types[x.get("type")] = types.get(x.get("type"), 0) + 1
    print(f"  rows={len(rows)} types={types} total_pages={r.headers.get('x-wp-totalpages')} total={r.headers.get('x-wp-total')}")
    if rows:
        p = rows[0].get("prices") or {}
        print(f"  currency={p.get('currency_code')} minor={p.get('currency_minor_unit')} sample={rows[0].get('name')}")
    r, t = get(s, f"{base}/products?type=variation&per_page=100&page=1")
    if r is None: print("bulk variation: unreachable", t)
    else:
        ok = False
        if r.status_code == 200:
            v = r.json()
            ok = bool(v) and all((x.get("type") == "variation") or (x.get("parent") or 0) > 0 for x in v[:10])
            print(f"bulk variation: HTTP 200 in {t}s rows={len(v)} SUPPORTED={ok} total={r.headers.get('x-wp-total')}")
            if v: print("  sample:", json.dumps({k: v[0].get(k) for k in ("id", "parent", "type", "is_in_stock", "attributes")}, ensure_ascii=False)[:300])
        else:
            print(f"bulk variation: HTTP {r.status_code} in {t}s SUPPORTED=False")
    codes = []
    for _ in range(12):
        r, _t = get(s, f"{base}/products?per_page=1")
        codes.append(r.status_code if r is not None else "ERR")
    print(f"12 rapid requests -> {codes}")
