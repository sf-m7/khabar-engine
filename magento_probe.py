#!/usr/bin/env python3
"""Validates the exact GraphQL query the scraper uses against Mobaco. No DB access."""
import json, re
from curl_cffi import requests

src = open("scraper.py", encoding="utf-8").read()
price = re.search(r'_MAGE_PRICE = "(.*?)"\n', src).group(1)
q_parts = re.search(r'MAGENTO_QUERY = \((.*?)\n\)\n', src, re.S).group(1)
query = eval("(" + q_parts.replace("_MAGE_PRICE", repr(price)) + ")")

s = requests.Session(impersonate="chrome124")
for ep in ["https://mobaco.hypernode.io/graphql", "https://mobaco.com/graphql"]:
    r = s.post(ep, json={"query": query, "variables": {"page": 1, "size": 40}},
               headers={"content-type": "application/json"}, timeout=60)
    print(f"\n{ep} -> HTTP {r.status_code}")
    try:
        j = r.json()
    except Exception:
        print("not json:", r.text[:200]); continue
    if j.get("errors"): print("ERRORS:", str(j["errors"])[:600])
    p = (j.get("data") or {}).get("products") or {}
    items = p.get("items") or []
    print("total_count:", p.get("total_count"), "pages:", p.get("page_info"), "items on page:", len(items))
    types = {}
    for it in items: types[it.get("__typename")] = types.get(it.get("__typename"), 0) + 1
    print("types:", types)
    for it in items[:3]:
        print(json.dumps({k: it.get(k) for k in ("sku", "name", "url_key", "canonical_url", "stock_status")}, ensure_ascii=False))
        for v in (it.get("variants") or [])[:3]:
            print("   variant:", json.dumps(v, ensure_ascii=False)[:260])
    if items: break
