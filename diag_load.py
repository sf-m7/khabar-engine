"""
Heavy load probe. Answers ONE question: is the failure load-provoked (fixable
by running gentler / less often) or constant right now (only fixable with better
IPs)?

It fires 30 sequential GETs to ONE store's products.json through the proxy,
rotating a fresh sticky peer each time (same as the scraper), and prints pass/
fail per request.

Read the pass/fail column:
  - Mostly PASS, no clear trend      -> not load-provoked; if the real run fails
                                        at the same time, it's constant IP-rep
                                        -> only better IPs (DataImpulse ISP pool)
                                        fixes it.
  - Starts PASS then flips to FAIL   -> LOAD-provoked flagging. Running fewer/
                                        gentler requests (1 run/day, slower
                                        pacing) will materially help.
  - Mostly FAIL from the start       -> the pool is flagged right now, period.
"""
import os, random, time
from curl_cffi import requests

USER = os.environ["DATAIMPULSE_PROXY_USERNAME"]
PASS = os.environ["DATAIMPULSE_PROXY_PASSWORD"]
HOST = os.environ.get("DATAIMPULSE_HOST") or "gw.dataimpulse.com"
COUNTRY = (os.environ.get("SHOPIFY_PROXY_COUNTRY") or "eg").strip().lower()
N = int(os.environ.get("PROBE_N") or "30")
STORE = os.environ.get("PROBE_STORE") or "dalydress.com"

def puser():
    return USER if COUNTRY in ("", "global", "any", "all", "world") else f"{USER}__cr.{COUNTRY}"

def prox():
    port = random.randint(10000, 20000)
    u = f"http://{puser()}:{PASS}@{HOST}:{port}"
    return {"https": u, "http": u}

url = f"https://{STORE}/products.json?limit=1"
print(f"pool={COUNTRY or 'global'}  store={STORE}  requests={N}\n")
ok = 0
first_fail = None
line = []
for i in range(1, N + 1):
    try:
        r = requests.get(url, impersonate="chrome124", proxies=prox(), timeout=25)
        good = (r.status_code == 200)
        line.append("." if good else "x")
        if good:
            ok += 1
        elif first_fail is None:
            first_fail = i
    except Exception:
        line.append("x")
        if first_fail is None:
            first_fail = i
    if i % 10 == 0:
        print(f"  {i:>2}: {''.join(line)}")
        line = []
    time.sleep(0.7)

print(f"\nPASS {ok}/{N}   first failure at request #{first_fail if first_fail else '-'}")
if ok >= N * 0.9:
    print("=> Stays healthy under load. If the real run fails at this same time,"
          " it's constant IP-reputation -> better IPs (ISP pool) is the fix.")
elif first_fail and first_fail > 5 and ok < N * 0.7:
    print("=> Degrades under load -> LOAD-PROVOKED. Fewer/gentler runs will help.")
else:
    print("=> Pool is flagged right now regardless of load -> better IPs needed.")
