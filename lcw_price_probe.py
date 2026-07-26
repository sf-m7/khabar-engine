#!/usr/bin/env python3
"""
lcw_price_probe.py — ONE-PAGE diagnostic. NOT the scraper.

Purpose: LCW's live site sells items discounted (e.g. o-5208891 shows 169 EGP,
down from 349) but our catalog crawl records the full price, because the sale
price is missing from the fields scraper.py reads. We need to see where the
sale price actually lives in the catalog API response for a KNOWN discounted
item — but the full scraper can't finish a crawl right now, and the /ajax/
endpoint is robots-blocked from a plain fetch.

This script fetches a SINGLE category page through the DataImpulse proxy (same
path the scraper uses), then for the first few items — and specifically any
item matching a known-discounted model — prints the COMPLETE raw JSON plus a
recursive scan of every nested numeric value that sits below the item's list
price. That scan reveals the exact field path carrying the sale price (169), or
proves it is not in the catalog API at all (in which case the price only lives
on the product page and the fix is different).

Runtime: seconds. One request (a few retries/rotations if the proxy peer is
bad). No full crawl, so the circuit-breaker / rotation-budget problems that
stall the daily run do not apply here.

Run locally or as a tiny manual GitHub Actions workflow with the same secrets
as the scraper (DATAIMPULSE_PROXY_USERNAME / DATAIMPULSE_PROXY_PASSWORD).
Reads nothing from and writes nothing to the database.
"""
import os
import json
import random

from curl_cffi import requests

DOMAIN = "www.lcwaikiki.eg"
# Men's T-Shirts (t-345) — full of discounted items like the 169 EGP tee we
# verified on the live site. Override with LCW_PROBE_CATEGORY if you want another.
# Men's CLOTHING top-level tree node (t-9). This is what the scraper actually
# crawls — subcategories like t-345 are "virtual" and return filters but no
# products via this endpoint. Category 9 is full of discounted items.
CATEGORY_ID = os.environ.get("LCW_PROBE_CATEGORY", "9")
# Known discounted model to hunt for specifically (o-5208891 = 169 EGP tee).
KNOWN_DISCOUNTED = os.environ.get("LCW_PROBE_MODEL", "5208891")

DATAIMPULSE_USER = os.environ.get("DATAIMPULSE_PROXY_USERNAME", "")
DATAIMPULSE_PASS = os.environ.get("DATAIMPULSE_PROXY_PASSWORD", "")
DATAIMPULSE_HOST = os.environ.get("DATAIMPULSE_HOST", "gw.dataimpulse.com")
COUNTRY = os.environ.get("LCW_PROXY_COUNTRY", "tr").split(",")[0].strip() or "tr"


def _http1_kwargs():
    try:
        from curl_cffi.const import CurlHttpVersion
        return {"http_version": CurlHttpVersion.V1_1}
    except Exception:
        return {}


def make_session():
    port = random.randint(10000, 20000)
    user = f"{DATAIMPULSE_USER}__cr.{COUNTRY}"
    proxy = f"http://{user}:{DATAIMPULSE_PASS}@{DATAIMPULSE_HOST}:{port}"
    print(f"[probe] proxy session country={COUNTRY} port.{port}")
    return requests.Session(impersonate="chrome124",
                            proxies={"https": proxy, "http": proxy})


def to_float(v):
    try:
        return float(str(v).replace("EGP", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def hunt_below(node, path, ceiling, out):
    """Record every nested numeric value strictly below `ceiling` (the list
    price) but not implausibly small — those are the candidate sale-price
    fields."""
    if isinstance(node, dict):
        for k, v in node.items():
            hunt_below(v, f"{path}.{k}", ceiling, out)
    elif isinstance(node, list):
        for i, v in enumerate(node[:8]):
            hunt_below(v, f"{path}[{i}]", ceiling, out)
    else:
        val = to_float(node)
        if val is not None and ceiling and 0 < val < ceiling and val >= ceiling * 0.15:
            out.append((path, val))


# The category page used to PRIME the session (pick up the WAF/cookie token).
# Without this, the ajax endpoint returns metadata but an empty product list —
# which is exactly what an un-primed probe sees. t-345 = men's t-shirts.
PRIME_PATH = os.environ.get("LCW_PROBE_PRIME", "men-clothing-t-9")


def fetch_page(page_index):
    ajax_url = (f"https://{DOMAIN}/en/ajax/ProductList/ProductListPageData"
                f"?xhrKeys=CategoryTreeId,xhrKeys&CategoryTreeId={CATEGORY_ID}"
                f"&PageIndex={page_index}&Layout=three-column")
    prime_url = f"https://{DOMAIN}/en/{PRIME_PATH}"
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "origin": f"https://{DOMAIN}",
        "referer": prime_url,
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin", "priority": "u=1, i",
    }
    prime_headers = {
        "accept": "text/html,application/xhtml+xml",
        "accept-language": "en-US,en;q=0.9",
        "referer": f"https://{DOMAIN}/en",
    }
    body = {"CategoryParameterList": [], "FilterListJson": "[]",
            "LastSeenOptionIdsJson": "[]"}
    # up to 6 attempts, each a fresh primed session, 15s timeout — enough to get
    # through even if a few proxy peers are bad, without any full-crawl machinery.
    for attempt in range(6):
        sess = make_session()
        try:
            # 1) PRIME: load the category HTML page first so LCW hands us the
            #    cookie/WAF token the ajax endpoint requires to return products.
            pr = sess.get(prime_url, headers=prime_headers, timeout=15,
                          **_http1_kwargs())
            print(f"[probe] attempt {attempt+1}: primed HTTP {pr.status_code} "
                  f"({len(pr.content)//1024} KB)")
            # 2) now the real ajax product-list call on the SAME session
            res = sess.post(ajax_url, json=body, headers=headers, timeout=15,
                            **_http1_kwargs())
            if res.status_code == 200:
                return res.json()
            print(f"[probe] attempt {attempt+1}: ajax HTTP {res.status_code}")
        except Exception as e:
            print(f"[probe] attempt {attempt+1} failed: {e}")
    return None


def extract_items(data):
    """Primary path is the scraper's own: data['CatalogList']['Items'].
    Fall back to a tree walk only if that's empty (in case the shape changed)."""
    items = (data.get("CatalogList") or {}).get("Items") or []
    if items:
        return items
    found = []

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            if node and isinstance(node[0], dict) and any(
                    k in node[0] for k in ("PriceValue", "ModelId", "OptionId")):
                found.append(node)
            for v in node:
                walk(v)

    walk(data)
    return max(found, key=len) if found else []


def dump_item(tag, item):
    list_price = to_float(item.get("PriceValue") or item.get("Price"))
    print(f"\n========== {tag} ==========")
    print(f"ModelId={item.get('ModelId')} OptionId={item.get('OptionId')} "
          f"ModelUrl={item.get('ModelUrl')}")
    print(f"list price (PriceValue)={list_price}")
    hits = []
    hunt_below(item, "item", list_price, hits)
    if hits:
        print("candidate sub-list-price fields (where a sale price would be):")
        for pth, val in sorted(hits, key=lambda x: x[1]):
            print(f"    {pth} = {val}")
    else:
        print("NO nested value below the list price — sale price is NOT in "
              "this catalog item at all.")
    blob = json.dumps(item, ensure_ascii=False, default=str)
    print("full raw item JSON (first 8000 chars):")
    print(blob[:8000])


def main():
    if not (DATAIMPULSE_USER and DATAIMPULSE_PASS):
        print("[probe] FATAL: DataImpulse proxy creds not set in env.")
        return

    # Scan the first few pages until we find a genuinely discounted item.
    all_items = []
    discounted = None
    for page in range(1, 5):
        data = fetch_page(page)
        if not data:
            print(f"[probe] page {page}: could not fetch after retries.")
            continue
        items = extract_items(data)
        print(f"[probe] page {page} of category {CATEGORY_ID}: {len(items)} items.")
        if not items:
            if page == 1:
                print("[probe] page 1 empty — raw top-level shape:")
                print(json.dumps(data, ensure_ascii=False, default=str)[:2000])
            continue
        all_items.extend(items)
        # a discounted item = has some nested value below its list price
        for it in items:
            lp = to_float(it.get("PriceValue") or it.get("Price"))
            hits = []
            hunt_below(it, "item", lp, hits)
            if hits:
                discounted = it
                break
        if discounted:
            break  # got what we came for

    if not all_items:
        print("[probe] no items on any page — cannot analyse.")
        return

    # 1) the specific known-discounted model, if we happened to crawl it
    match = next((it for it in all_items
                  if KNOWN_DISCOUNTED in str(it.get("ModelUrl", ""))
                  or KNOWN_DISCOUNTED in str(it.get("ModelId", ""))), None)
    if match:
        dump_item(f"KNOWN DISCOUNTED MODEL {KNOWN_DISCOUNTED}", match)

    # 2) the first genuinely-discounted item found (the key evidence)
    if discounted:
        dump_item("FIRST DISCOUNTED ITEM (has a value below list price)", discounted)
    else:
        print("\n[probe] NOTE: scanned several pages and NOT ONE item had any "
              "nested value below its list price. Strong evidence the sale "
              "price is NOT in the catalog API at all and lives only on the "
              "product page — which points to a product-page-based fix.")

    # 3) first item regardless, for baseline structure
    dump_item("FIRST ITEM ON PAGE 1 (baseline)", all_items[0])


if __name__ == "__main__":
    main()
