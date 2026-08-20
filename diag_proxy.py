"""
One-shot proxy diagnostic. Runs INSIDE GitHub Actions so it hits the same
network + pool state as the real scraper. Mirrors the scraper's client exactly
(from curl_cffi import requests) and toggles ONE variable at a time so we can
see which knob actually causes the curl 28 timeout.
"""
import os, time
from curl_cffi import requests

USER = os.environ["DATAIMPULSE_PROXY_USERNAME"]
PASS = os.environ["DATAIMPULSE_PROXY_PASSWORD"]
HOST = os.environ.get("DATAIMPULSE_HOST") or "gw.dataimpulse.com"

try:
    from curl_cffi.const import CurlHttpVersion
    H11 = CurlHttpVersion.V1_1
except Exception as e:
    H11 = None
    print(f"[warn] CurlHttpVersion unavailable: {e}")

TARGETS = ["https://dalydress.com", "https://carina.eg"]

def proxy(country, port):
    return f"http://{USER}__cr.{country}:{PASS}@{HOST}:{port}"

# label, impersonate, http_version, country, port
CASES = [
    ("1 chrome124 h2   eg:823  ", "chrome124", None, "eg", 823),
    ("2 chrome124 h1.1 eg:823  ", "chrome124", H11,  "eg", 823),
    ("3 chrome124 h1.1 eg:12345", "chrome124", H11,  "eg", 12345),
    ("4 none      h1.1 eg:823  ", None,        H11,  "eg", 823),
    ("5 none      h2   eg:823  ", None,        None, "eg", 823),
    ("6 chrome124 h1.1 tr:15000", "chrome124", H11,  "tr", 15000),
    ("7 chrome124 h2   tr:15000", "chrome124", None, "tr", 15000),
]

def run(url, label, impersonate, http_version, country, port):
    kw = dict(proxies={"https": proxy(country, port), "http": proxy(country, port)},
              timeout=15)
    if impersonate:
        kw["impersonate"] = impersonate
    if http_version is not None:
        kw["http_version"] = http_version
    t0 = time.time()
    try:
        r = requests.get(url, **kw)
        return f"{r.status_code} ({time.time()-t0:4.1f}s)"
    except Exception as e:
        msg = str(e).split(". See")[0][:70]
        return f"ERR  ({time.time()-t0:4.1f}s) {type(e).__name__}: {msg}"

for url in TARGETS:
    print(f"\n===== {url} =====")
    for label, imp, hv, country, port in CASES:
        print(f"  {label} -> {run(url, label, imp, hv, country, port)}")
