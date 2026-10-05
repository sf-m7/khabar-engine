#!/usr/bin/env python3
"""Probe WooCommerce Store API per domain, DIRECT and via DataImpulse (Egypt exit).
Never crashes on non-JSON: prints status, content-type, server headers, body start.
Usage: python woo_probe.py mobaco.com rojada-egy.com <coup-domain>"""
import os, sys, time, json
from curl_cffi import requests

H = {"accept": "application/json, text/plain, */*", "accept-language": "en-US,en;q=0.9"}
U, P = os.environ.get("DATAIMPULSE_PROXY_USERNAME", ""), os.environ.get("DATAIMPULSE_PROXY_PASSWORD", "")

def session(mode):
    if mode == "direct":
        return requests.Session(impersonate="chrome124")
    if not (U and P):
        return None
    px = f"http://{U}__cr.eg:{P}@gw.dataimpulse.com:823"
    return requests.Session(impersonate="chrome124", proxies={"http": px, "https": px})

def get(s, url):
    t = time.time()
    try:
        return s.get(url, headers=H, timeout=60), round(time.time() - t, 2)
    except Exception as e:
        return None, f"ERR {str(e)[:100]}"

def as_json(r):
    try:
        return r.json()
    except Exception:
        return None

def describe(r):
    h = r.headers
    return (f"HTTP {r.status_code} ctype={h.get('content-type')} server={h.get('server')} "
            f"cf-mitigated={h.get('cf-mitigated')} x-sucuri={h.get('x-sucuri-id')} "
            f"body[:150]={r.text[:150]!r}")

for d in sys.argv[1:]:
    for mode in ("direct", "proxy-eg"):
        print(f"\n===== {d} [{mode}] =====")
        s = session(mode)
        if s is None:
            print("  (skipped: no DATAIMPULSE secrets in this workflow)")
            continue
        base = f"https://{d}/wp-json/wc/store/v1"
        r, t = get(s, f"{base}/products?per_page=100&page=1")
        if r is None:
            print("  products: unreachable", t); continue
        rows = as_json(r)
        if not isinstance(rows, list):
            print(f"  products p1 in {t}s: NOT JSON -> {describe(r)}"); continue
        types = {}
        for x in rows: types[x.get("type")] = types.get(x.get("type"), 0) + 1
        print(f"  products p1: HTTP {r.status_code} in {t}s rows={len(rows)} types={types} "
              f"pages={r.headers.get('x-wp-totalpages')} total={r.headers.get('x-wp-total')}")
        if rows:
            p = rows[0].get("prices") or {}
            print(f"  currency={p.get('currency_code')} minor={p.get('currency_minor_unit')} sample={rows[0].get('name')!r}")
        r, t = get(s, f"{base}/products?type=variation&per_page=100&page=1")
        if r is None:
            print("  bulk variation: unreachable", t)
        else:
            v = as_json(r)
            if isinstance(v, list):
                ok = bool(v) and all((x.get("type") == "variation") or (x.get("parent") or 0) > 0 for x in v[:10])
                print(f"  bulk variation: HTTP {r.status_code} in {t}s rows={len(v)} SUPPORTED={ok} total={r.headers.get('x-wp-total')}")
                if v: print("   sample:", json.dumps({k: v[0].get(k) for k in ("id", "parent", "type", "is_in_stock", "attributes")}, ensure_ascii=False)[:300])
            else:
                print(f"  bulk variation: NOT JSON -> {describe(r)}")
        codes = []
        for _ in range(12):
            r, _t = get(s, f"{base}/products?per_page=1")
            codes.append(r.status_code if r is not None else "ERR")
        print(f"  12 rapid requests -> {codes}")
