#!/usr/bin/env python3
"""
Mobaco color diagnostic — one-time, throwaway.
Fetches a few Mobaco products + their variations and prints the raw data
so we can see where the real color lives. Reads nothing, writes nothing.
"""
import json
import os
import requests

DOMAIN = "mobaco.com"
BASE = f"https://{DOMAIN}/wp-json/wc/store/v1/products"
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "referer": f"https://{DOMAIN}/",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
}


def show(label, obj, limit=1500):
    print(f"\n===== {label} =====")
    print(json.dumps(obj, indent=2, ensure_ascii=False)[:limit])


def main():
    # 1. Grab a small page of products
    r = requests.get(BASE, params={"per_page": 3}, headers=HEADERS, timeout=20)
    print(f"products list -> HTTP {r.status_code}")
    if r.status_code != 200:
        print("BODY:", r.text[:400])
        return
    products = r.json()
    if not products:
        print("No products returned.")
        return

    for p in products[:2]:
        pid = p.get("id")
        name = p.get("name")
        print("\n" + "#" * 60)
        print(f"PRODUCT {pid}: {name}")
        print("TOP-LEVEL KEYS:", list(p.keys()))

        # attributes[] usually names the axes (Color, Size, ...)
        show("attributes[]", p.get("attributes", "MISSING"))
        # variations[] summary as embedded in the parent
        show("variations[] (parent view, first 2)",
             (p.get("variations") or [])[:2])

        # 2. Now the dedicated variations endpoint for this product
        vr = requests.get(f"{BASE}/{pid}/variations",
                          headers=HEADERS, timeout=20)
        print(f"\nvariations endpoint -> HTTP {vr.status_code}")
        if vr.status_code == 200:
            vrows = vr.json()
            if isinstance(vrows, list) and vrows:
                show("variation[0] FULL", vrows[0], limit=2000)
            else:
                print("variations endpoint returned empty/odd shape")
        else:
            print("BODY:", vr.text[:300])


if __name__ == "__main__":
    main()
