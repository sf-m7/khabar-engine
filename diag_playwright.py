"""
Real-browser probe. Answers the ONE question that decides whether the
headless-browser project is worth building at all:

  Does a REAL Chromium browser, going through the SAME DataImpulse proxy,
  reach a Cloudflare-fronted store — and if so, does it see a solvable
  challenge page, or the actual store?

Three possible outcomes:

  1. Browser can't even connect (proxy/TLS error, timeout) ->
     IP-LEVEL BLOCK. A real browser is reset the same way curl_cffi was.
     No amount of "look more human" client software fixes this — the IP
     itself is the problem. Headless-browser project would be WASTED effort.

  2. Browser connects but the page IS a Cloudflare challenge/interstitial
     ("Just a moment...", Turnstile widget, "Attention Required") ->
     There's something to solve. Plain Playwright alone likely won't pass
     it either (Cloudflare detects headless automation too) — this would
     need a stealth/undetected browser AND possibly a CAPTCHA-solving
     service. Worth scoping as a real (paid, slower) project.

  3. Browser connects and sees the ACTUAL store content ->
     Cloudflare isn't blocking real browsers over this IP at all — it's
     specifically distinguishing curl_cffi's HTTP client from a real
     browser despite fingerprint impersonation. A plain headless browser
     (Playwright, no stealth needed) would likely work for these brands.

Screenshots are saved for each target so you can SEE which case you're in,
even without reading code.
"""
import os, sys, time
from playwright.sync_api import sync_playwright

USER = os.environ["DATAIMPULSE_PROXY_USERNAME"]
PASS = os.environ["DATAIMPULSE_PROXY_PASSWORD"]
HOST = os.environ.get("DATAIMPULSE_HOST") or "gw.dataimpulse.com"
COUNTRY = (os.environ.get("SHOPIFY_PROXY_COUNTRY") or "eg").strip().lower()

def proxy_username():
    if COUNTRY in ("", "global", "any", "all", "world"):
        return USER
    return f"{USER}__cr.{COUNTRY}"

CHALLENGE_MARKERS = [
    "just a moment", "checking your browser", "cf-chl", "turnstile",
    "attention required", "cf-mitigated", "cloudflare ray id",
    "please wait while we verify",
]

TARGETS = ["dalydress.com", "arafastores.com"]

def probe(pw, domain, port):
    proxy_cfg = {
        "server": f"http://{HOST}:{port}",
        "username": proxy_username(),
        "password": PASS,
    }
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(proxy=proxy_cfg, ignore_https_errors=True)
    page = context.new_page()
    result = {"domain": domain, "status": None, "title": None,
              "challenge": False, "error": None, "screenshot": None}
    try:
        resp = page.goto(f"https://{domain}/", timeout=30000, wait_until="domcontentloaded")
        result["status"] = resp.status if resp else None
        result["title"] = page.title()
        body_lower = (page.content() or "").lower()
        result["challenge"] = any(m in body_lower for m in CHALLENGE_MARKERS)
        shot = f"screenshot_{domain}.png"
        page.screenshot(path=shot)
        result["screenshot"] = shot
    except Exception as e:
        result["error"] = str(e)[:200]
    finally:
        context.close()
        browser.close()
    return result

print(f"pool={COUNTRY or 'global'}  host={HOST}\n")
with sync_playwright() as pw:
    verdicts = []
    for i, domain in enumerate(TARGETS):
        port = 10000 + i * 1000 + int(time.time()) % 500   # fresh-ish sticky port per target
        r = probe(pw, domain, port)
        print(f"── {domain} ──")
        if r["error"]:
            print(f"   CONNECTION FAILED: {r['error']}")
            verdicts.append("blocked")
        else:
            print(f"   HTTP status : {r['status']}")
            print(f"   Page title  : {r['title']!r}")
            print(f"   Challenge?  : {'YES' if r['challenge'] else 'no'}")
            print(f"   Screenshot  : {r['screenshot']}")
            verdicts.append("challenge" if r["challenge"] else "content")
        print()

print("=" * 60)
if all(v == "blocked" for v in verdicts):
    print("VERDICT: IP-level block. A real browser is reset the same way\n"
          "as curl_cffi. Building a headless-browser scraper would NOT help\n"
          "— the fix has to be cleaner IPs, not smarter client software.")
elif any(v == "content" for v in verdicts):
    print("VERDICT: A real browser reaches actual store content over this\n"
          "proxy. Cloudflare is distinguishing curl_cffi from a real browser\n"
          "despite fingerprint impersonation. A plain headless-browser scraper\n"
          "(no stealth patches needed) is likely worth building for these brands.")
else:
    print("VERDICT: A real browser connects but hits a Cloudflare challenge\n"
          "page. This needs a stealth/undetected browser and possibly a\n"
          "CAPTCHA-solving service — a bigger, paid project. Check the\n"
          "screenshots to see exactly what's being shown.")
