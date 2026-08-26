"""
Root-vs-JSON probe. RUN THIS WHILE A REAL RUN IS FAILING.

It hits the same Cloudflare store two ways, in the same instant, through the
proxy — so we compare against ONE pool state instead of across days:

  A) GET  /products.json?limit=1   (cache-served, bypasses CF challenge)
  B) HEAD /                        (bare root — the old preflight; CF-challenged)
  C) GET  /                        (bare root with GET, for completeness)

Interpretation:
  A passes, B/C fail  -> the check_domain fix is correct: probe products.json,
                         never the root. The root preflight was the problem.
  A ALSO fails        -> the DataImpulse IPs are flagged by Cloudflare right now
                         (reputation), and NO scraper change helps this run —
                         the fix is cleaner IPs (DataImpulse premium/ISP pool or
                         another provider). Stop editing the scraper.
Run it a few times; if results flip between runs, that IS the reputation
flapping we suspect.
"""
import os, random, time
from curl_cffi import requests

USER = os.environ["DATAIMPULSE_PROXY_USERNAME"]
PASS = os.environ["DATAIMPULSE_PROXY_PASSWORD"]
HOST = os.environ.get("DATAIMPULSE_HOST") or "gw.dataimpulse.com"
COUNTRY = (os.environ.get("SHOPIFY_PROXY_COUNTRY") or "eg").strip().lower()

def puser():
    return USER if COUNTRY in ("", "global", "any", "all", "world") else f"{USER}__cr.{COUNTRY}"

def prox():
    port = random.randint(10000, 20000)
    u = f"http://{puser()}:{PASS}@{HOST}:{port}"
    return {"https": u, "http": u}

STORES = ["dalydress.com", "arafastores.com", "mlameh.com"]

def call(method, url):
    try:
        fn = requests.get if method == "GET" else requests.head
        r = fn(url, impersonate="chrome124", proxies=prox(), timeout=30)
        return f"{r.status_code}"
    except Exception as e:
        return "ERR " + str(e).split(". See")[0][:42]

print(f"pool={COUNTRY or 'global'}\n")
print(f"{'store':<18} {'A GET json':<14} {'B HEAD root':<16} {'C GET root'}")
print("-" * 66)
for s in STORES:
    a = call("GET",  f"https://{s}/products.json?limit=1")
    b = call("HEAD", f"https://{s}/")
    c = call("GET",  f"https://{s}/")
    print(f"{s:<18} {a:<14} {b:<16} {c}")
    time.sleep(1)
