#!/usr/bin/env python3
"""
Mobaco color diagnostic v2 — hunt for real color names.
Checks three places the actual color name might live:
  1. The pa_colour taxonomy terms endpoint (WooCommerce sometimes exposes it)
  2. Product image filenames (often named by color)
  3. The variation's own image / description
"""
import json
import requests

DOMAIN = "mobaco.com"
BASE = f"https://{DOMAIN}/wp-json/wc/store/v1"
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "referer": f"https://{DOMAIN}/",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
}


def show(label, obj, limit=2000):
    print(f"\n===== {label} =====")
    print(json.dumps(obj, indent=2, ensure_ascii=False)[:limit])


def main():
    # ATTEMPT 1: the color taxonomy terms endpoint.
    # If this returns readable names, we win — all mappings at once.
    print("### ATTEMPT 1: pa_colour taxonomy terms ###")
    for path in ["/products/attributes", "/products/attributes/1/terms"]:
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS,
                             params={"per_page": 20}, timeout=20)
            print(f"\nGET {path} -> HTTP {r.status_code}")
            if r.status_code == 200:
                show(path, r.json(), limit=2500)
        except Exception as e:
            print(f"  {path} error: {e}")

    # ATTEMPT 2: look at a product's images — filenames often carry color.
    print("\n\n### ATTEMPT 2: product image filenames ###")
    r = requests.get(f"{BASE}/products", params={"per_page": 3},
                     headers=HEADERS, timeout=20)
    if r.status_code == 200:
        for p in r.json()[:3]:
            print(f"\nPRODUCT {p.get('id')}: {p.get('name')}")
            for img in (p.get("images") or [])[:4]:
                src = img.get("src", "")
                # just the filename, not the full URL
                print("  IMG:", src.rsplit("/", 1)[-1])
            # also dump tags — sometimes color is a tag
            tags = [t.get("name") for t in (p.get("tags") or [])]
            print("  TAGS:", tags)


if __name__ == "__main__":
    main()
