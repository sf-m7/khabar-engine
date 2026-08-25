"""
Fingerprint sweep — runs in GitHub Actions, same network + proxy as the real
scraper. Question it answers: can ANY TLS impersonation fingerprint get through
Cloudflare from a DataImpulse residential IP, or is Cloudflare rejecting the IPs
themselves (in which case no fingerprint helps and it's a provider problem)?

For each fingerprint it opens a fresh sticky proxy session and hits two
Cloudflare-fronted Shopify stores' /products.json. Reports the status or the
exact curl error. A 200/301/403-with-body means the handshake PASSED; curl 35
means Cloudflare killed the TLS handshake for that fingerprint+IP.
"""
import os, random, time
from curl_cffi import requests

USER = os.environ["DATAIMPULSE_PROXY_USERNAME"]
PASS = os.environ["DATAIMPULSE_PROXY_PASSWORD"]
HOST = os.environ.get("DATAIMPULSE_HOST") or "gw.dataimpulse.com"
COUNTRY = (os.environ.get("SHOPIFY_PROXY_COUNTRY") or "eg").strip().lower()

def proxy_user():
    if COUNTRY in ("", "global", "any", "all", "world"):
        return USER
    return f"{USER}__cr.{COUNTRY}"

def proxy():
    port = random.randint(10000, 20000)   # fresh sticky peer each call
    u = f"http://{proxy_user()}:{PASS}@{HOST}:{port}"
    return {"https": u, "http": u}

# A spread of fingerprints — different Chrome versions, Firefox, Safari.
# If Cloudflare is fingerprint-flagging, one of these may slip through; if it's
# IP reputation, they'll all fail identically.
FINGERPRINTS = ["chrome124", "chrome131", "chrome136", "chrome142",
                "chrome150", "firefox144", "safari180"]

# Cloudflare-fronted Shopify stores from your fleet.
TARGETS = ["https://dalydress.com/products.json?limit=1",
           "https://arafastores.com/products.json?limit=1"]

def try_one(fp, url):
    for attempt in range(2):                # 2 fresh peers per fingerprint
        try:
            r = requests.get(url, impersonate=fp, proxies=proxy(), timeout=25)
            return f"{r.status_code} (len {len(r.content)})"
        except Exception as e:
            last = str(e).split(". See")[0][:60]
        time.sleep(1)
    return f"FAIL {last}"

print(f"proxy pool: {COUNTRY or 'global'}   host: {HOST}\n")
print(f"{'fingerprint':<12} | " + " | ".join(t.split('/')[2] for t in TARGETS))
print("-" * 70)
any_pass = False
for fp in FINGERPRINTS:
    cells = []
    for url in TARGETS:
        res = try_one(fp, url)
        if res[:3] in ("200", "301", "302", "403", "429"):
            any_pass = True
        cells.append(res)
    print(f"{fp:<12} | " + " | ".join(cells))

print("\n" + ("=> At least one fingerprint PASSED the handshake — set "
              "SHOPIFY_IMPERSONATE to it." if any_pass else
              "=> EVERY fingerprint failed at the handshake. This is IP "
              "reputation, not fingerprint — no scraper change fixes it; the "
              "proxy IPs are the problem."))
