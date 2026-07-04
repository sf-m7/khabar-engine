#!/usr/bin/env python3
"""
Khabar Taxonomy Backfill — Phase C, Passes 2-5
================================================
Clears the color queue and fills subcategories for ALL categories,
then audits the results. Designed to be:

  - IDEMPOTENT: safe to run repeatedly; only touches rows still unresolved.
  - DICTIONARY-DRIVEN: every AI answer is written into color_map /
    category_map so the daily scraper inherits the knowledge for free.
  - CONSTRAINED: Gemini may ONLY pick from the frozen taxonomy. Any
    invented label is rejected and the row stays unclassified.
  - BUDGET-GUARDED: hard cap on API calls per run (MAX_GEMINI_CALLS).

Passes (run in this order):
  colors         Gemini reads the 1,633 unclassified color names -> color_map -> stamps variants
  subcat-rules   FREE deterministic title-keyword rules -> subcategories (no AI)
  subcat-text    Gemini reads title+category for products still NULL (cheap)
  subcat-vision  Gemini LOOKS at product images for the final holdouts (CV)
  audit          Random sample per category, vision-verified accuracy report
  all            Everything above, in order

Usage:  python taxonomy_backfill.py --pass colors
Env:    SUPABASE_DB_URL              (Postgres connection string)
        GOOGLE_SERVICE_ACCOUNT_KEY   (preferred: Vertex AI, uses $300 credits)
        GEMINI_API_KEY               (fallback: free tier, small daily quota)
        MAX_GEMINI_CALLS             (optional, default 5000)
"""

import argparse
import json
import os
import re
import sys
import time

import psycopg2
import psycopg2.extras
import requests

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
DB_URL = os.environ.get("SUPABASE_DB_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SERVICE_ACCOUNT_KEY = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
MAX_GEMINI_CALLS = int(os.environ.get("MAX_GEMINI_CALLS", "5000"))

GEMINI_MODEL = "gemini-2.5-flash"
VERTEX_REGION = "us-central1"

CALLS_MADE = 0
_LAST_CALL_AT = 0.0
_PRINTED_429_BODY = False  # print Google's full explanation once

# Backend state (set by _init_backend at startup)
_BACKEND = None          # "vertex" or "apikey"
_ACCESS_TOKEN = None
_TOKEN_EXPIRY = 0.0
_PROJECT_ID = None

# Pacing between calls. Vertex paid tier is generous; free tier is not.
MIN_CALL_INTERVAL_VERTEX = 1.0
MIN_CALL_INTERVAL_APIKEY = 5.0

# ---------------- FROZEN COLOR TAXONOMY (approved by Mohammed) --------
COLOR_FAMILIES = [
    "black", "white", "grey", "beige", "brown", "blue", "teal", "green",
    "yellow", "orange", "red", "burgundy", "pink", "purple", "metallic",
    "multi",
]

# ---------------- FROZEN SUBCATEGORY TAXONOMY (L3) ---------------------
# category_normalized -> allowed subcategories
SUBCATS = {
    "t-shirts": ["short-sleeve", "long-sleeve", "sleeveless", "oversized",
                  "graphic-printed", "basic"],
    "shirts": ["short-sleeve", "long-sleeve", "overshirt", "flannel",
                "linen", "formal"],
    "trousers": ["chino", "cargo", "formal", "wide-leg", "jogger-style"],
    "jeans": ["slim", "straight", "wide-baggy", "mom-fit", "flare"],
    "dresses": ["maxi", "midi", "mini"],
    "shorts": ["denim", "chino", "sport"],
    "pajamas": ["men", "women", "boys", "girls"],
    "sweaters": ["crewneck", "turtleneck", "v-neck", "zip"],
    "hoodies": ["pullover", "zip-up"],
    "skirts": ["maxi", "midi", "mini"],
    "jackets": ["denim", "bomber", "puffer", "leather", "windbreaker"],
}

# ---------------- FREE TEXT RULES (Pass: subcat-rules) ------------------
# (category, regex on product title, subcategory, priority)
# Lower priority number = checked first. Written into category_map so the
# scraper inherits them.
TITLE_RULES = [
    # t-shirts & shirts: sleeve length first (most decisive)
    ("t-shirts", r"long[\s\-]?sleeve", "long-sleeve", 10),
    ("t-shirts", r"short[\s\-]?sleeve|half[\s\-]?sleeve", "short-sleeve", 10),
    ("t-shirts", r"sleeve\s?less|no[\s\-]?sleeve", "sleeveless", 10),
    ("t-shirts", r"over[\s\-]?size", "oversized", 20),
    ("t-shirts", r"graphic|printed|print\b", "graphic-printed", 30),
    ("t-shirts", r"basic|plain|solid", "basic", 40),
    ("shirts", r"long[\s\-]?sleeve", "long-sleeve", 10),
    ("shirts", r"short[\s\-]?sleeve|half[\s\-]?sleeve", "short-sleeve", 10),
    ("shirts", r"over[\s\-]?shirt", "overshirt", 15),
    ("shirts", r"flannel", "flannel", 20),
    ("shirts", r"linen", "linen", 20),
    ("shirts", r"formal|classic|dress shirt", "formal", 30),
    # bottoms
    ("trousers", r"chino", "chino", 10),
    ("trousers", r"cargo", "cargo", 10),
    ("trousers", r"formal|suit|classic", "formal", 20),
    ("trousers", r"wide[\s\-]?leg", "wide-leg", 20),
    ("trousers", r"jogger", "jogger-style", 20),
    ("jeans", r"slim|skinny", "slim", 10),
    ("jeans", r"straight|regular", "straight", 10),
    ("jeans", r"wide|baggy|loose|relaxed", "wide-baggy", 10),
    ("jeans", r"\bmom\b", "mom-fit", 10),
    ("jeans", r"flare|boot[\s\-]?cut", "flare", 10),
    ("shorts", r"denim|jeans", "denim", 10),
    ("shorts", r"chino", "chino", 10),
    ("shorts", r"sport|swim|training|running", "sport", 10),
    # dresses & skirts
    ("dresses", r"\bmaxi\b", "maxi", 10),
    ("dresses", r"\bmidi\b", "midi", 10),
    ("dresses", r"\bmini\b", "mini", 10),
    ("skirts", r"\bmaxi\b", "maxi", 10),
    ("skirts", r"\bmidi\b", "midi", 10),
    ("skirts", r"\bmini\b", "mini", 10),
    # knitwear & outerwear
    ("sweaters", r"crew[\s\-]?neck|round[\s\-]?neck", "crewneck", 10),
    ("sweaters", r"turtle[\s\-]?neck|high[\s\-]?neck|mock[\s\-]?neck", "turtleneck", 10),
    ("sweaters", r"v[\s\-]?neck", "v-neck", 10),
    ("sweaters", r"\bzip", "zip", 10),
    ("hoodies", r"zip", "zip-up", 10),
    ("hoodies", r"pullover|over[\s\-]?head", "pullover", 20),
    ("jackets", r"denim|jeans", "denim", 10),
    ("jackets", r"bomber", "bomber", 10),
    ("jackets", r"puffer|padded|quilted", "puffer", 10),
    ("jackets", r"leather", "leather", 10),
    ("jackets", r"wind[\s\-]?breaker", "windbreaker", 10),
]


# ----------------------------------------------------------------------
# Plumbing
# ----------------------------------------------------------------------
def db():
    if not DB_URL:
        sys.exit("ERROR: SUPABASE_DB_URL env var is missing.")
    return psycopg2.connect(DB_URL)


def _init_backend():
    """Pick the best available lane, once, at startup.
    Vertex AI (the $300 credits) if the service account key exists;
    otherwise fall back to the free-tier API key."""
    global _BACKEND
    if SERVICE_ACCOUNT_KEY:
        _refresh_vertex_token()
        _BACKEND = "vertex"
        print(f"  backend: Vertex AI / paid credits (project: {_PROJECT_ID})")
    elif GEMINI_API_KEY:
        _BACKEND = "apikey"
        print("  backend: API key / free tier (small daily quota — "
              "expect slow progress and possible daily cutoffs)")
    else:
        sys.exit(
            "ERROR: no Google credentials found. Add either "
            "GOOGLE_SERVICE_ACCOUNT_KEY (preferred) or GEMINI_API_KEY "
            "as a repository secret."
        )


def _refresh_vertex_token():
    """Log in as the service account and get a fresh access token."""
    global _ACCESS_TOKEN, _TOKEN_EXPIRY, _PROJECT_ID
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    info = json.loads(SERVICE_ACCOUNT_KEY)
    _PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT") or info.get("project_id")
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(Request())
    _ACCESS_TOKEN = creds.token
    _TOKEN_EXPIRY = time.monotonic() + 3000  # refresh well before the 1h expiry


def _request_target():
    """Return (url, headers, params) for whichever lane we're on."""
    if _BACKEND == "vertex":
        global _ACCESS_TOKEN
        if time.monotonic() > _TOKEN_EXPIRY:
            print("  refreshing Vertex AI token ...")
            _refresh_vertex_token()
        url = (
            f"https://{VERTEX_REGION}-aiplatform.googleapis.com/v1/"
            f"projects/{_PROJECT_ID}/locations/{VERTEX_REGION}/"
            f"publishers/google/models/{GEMINI_MODEL}:generateContent"
        )
        return url, {"Authorization": f"Bearer {_ACCESS_TOKEN}",
                     "Content-Type": "application/json"}, None
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    return url, {"Content-Type": "application/json"}, {"key": GEMINI_API_KEY}


def gemini(prompt_parts, expect_json=True, retries=6):
    """One Gemini call: budget guard, pacing, backoff, verbose errors."""
    global CALLS_MADE, _LAST_CALL_AT, _PRINTED_429_BODY
    if CALLS_MADE >= MAX_GEMINI_CALLS:
        raise RuntimeError(
            f"Budget guard: reached MAX_GEMINI_CALLS={MAX_GEMINI_CALLS}. "
            "Re-run later to continue (the script resumes where it stopped)."
        )

    interval = (MIN_CALL_INTERVAL_VERTEX if _BACKEND == "vertex"
                else MIN_CALL_INTERVAL_APIKEY)
    elapsed = time.monotonic() - _LAST_CALL_AT
    if elapsed < interval:
        time.sleep(interval - elapsed)

    body = {"contents": [{"role": "user", "parts": prompt_parts}]}
    for attempt in range(retries):
        _LAST_CALL_AT = time.monotonic()
        url, headers, params = _request_target()
        try:
            resp = requests.post(url, headers=headers, params=params,
                                 json=body, timeout=120)
        except requests.exceptions.RequestException as e:
            wait = 20 * (attempt + 1)
            print(f"  network error ({e.__class__.__name__}), waiting {wait}s ...")
            time.sleep(wait)
            continue

        if resp.status_code == 403:
            # Permission problem: retrying won't help. Print Google's own
            # explanation and stop with clear guidance.
            print("\n  === GOOGLE'S FULL EXPLANATION (403) ===")
            print(f"  {resp.text[:1200]}")
            print("  =======================================\n")
            raise RuntimeError(
                "Google refused for permission reasons (403). The message "
                "above says exactly why. Most common fix: in Google Cloud "
                "Console -> IAM, confirm the khabar-backfill service "
                "account has the Editor role, save, wait 3 minutes, re-run."
            )

        if resp.status_code == 429 or resp.status_code >= 500:
            if resp.status_code == 429 and not _PRINTED_429_BODY:
                _PRINTED_429_BODY = True
                print("\n  === GOOGLE'S FULL EXPLANATION (429, printed once) ===")
                print(f"  {resp.text[:1200]}")
                print("  =====================================================\n")
            wait = min(20 * (attempt + 1), 90)
            print(f"  Gemini busy (HTTP {resp.status_code}), waiting {wait}s ...")
            time.sleep(wait)
            continue

        if resp.status_code != 200:
            print(f"  ERROR {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()

        CALLS_MADE += 1
        data = resp.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            return None
        if not expect_json:
            return text
        cleaned = re.sub(r"```json|```", "", text).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None
    raise RuntimeError(
        "Gemini stayed unavailable after 6 patient retries. If the printed "
        "explanation above mentions a DAILY limit, waiting within the run "
        "cannot help — re-run tomorrow, or switch to the Vertex lane. "
        "Nothing is lost; the script resumes where it stopped."
    )


def fetch_image_b64(url):
    """Download a product image and return (base64, mime) or None."""
    import base64
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
        if not mime.startswith("image/"):
            mime = "image/jpeg"
        return base64.b64encode(r.content).decode(), mime
    except Exception:
        return None


# ----------------------------------------------------------------------
# PASS 1: colors — clear the color_map queue with Gemini (text only)
# ----------------------------------------------------------------------
def pass_colors():
    print("== PASS colors: classifying unclassified color names ==")
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT color_raw FROM color_map WHERE status = 'unclassified' "
        "ORDER BY color_raw"
    )
    queue = [r[0] for r in cur.fetchall()]
    print(f"  queue size: {len(queue)} names")

    BATCH = 80
    resolved = 0
    for i in range(0, len(queue), BATCH):
        batch = queue[i : i + BATCH]
        prompt = f"""You classify fashion color names from Egyptian clothing
brands (names may be English, Arabic, misspelled, or transliterated).

Allowed families (you MUST pick from this list, nothing else):
{json.dumps(COLOR_FAMILIES)}

Rules:
- "shade" is a short child name (e.g. "baby blue", "olive", "mustard") or null.
- If the name lists TWO colors (e.g. "white/navy", "أبيض * أسود"): the FIRST
  listed color is the family, set is_compound=true and secondary_family to
  the second color's family.
- If it is a size, a product code, or meaningless: status="junk".
- If genuinely a color but you are not confident of the family:
  status="unclassified".
- Otherwise status="classified".

Respond ONLY with a JSON array, no markdown, one object per input name:
[{{"raw": "...", "family": "...", "shade": "... or null",
   "is_compound": false, "secondary_family": null, "status": "classified"}}]

Names to classify:
{json.dumps(batch, ensure_ascii=False)}"""

        result = gemini([{"text": prompt}])
        if not isinstance(result, list):
            print(f"  batch {i//BATCH+1}: bad response, skipping (will retry next run)")
            continue

        for item in result:
            raw = (item.get("raw") or "").strip().lower()
            fam = item.get("family")
            status = item.get("status", "unclassified")
            # HARD VALIDATION: invented family => stays unclassified
            if status == "classified" and fam not in COLOR_FAMILIES:
                status, fam = "unclassified", None
            if status not in ("classified", "junk", "unclassified"):
                status = "unclassified"
            sec = item.get("secondary_family")
            if sec not in COLOR_FAMILIES:
                sec = None
            cur.execute(
                """UPDATE color_map
                   SET color_family=%s, color_shade=%s, is_compound=%s,
                       secondary_family=%s, status=%s,
                       source='gemini_text', updated_at=now()
                   WHERE color_raw=%s AND status='unclassified'""",
                (fam if status == "classified" else None,
                 item.get("shade") if status == "classified" else None,
                 bool(item.get("is_compound")), sec, status, raw),
            )
            resolved += cur.rowcount
        conn.commit()
        print(f"  batch {i//BATCH+1}: done ({resolved} rows updated so far)")
        time.sleep(1)

    # Stamp variants from every newly classified name (direct connection:
    # no 8-second API timeout applies here).
    print("  stamping variants from the updated dictionary ...")
    cur.execute(
        """UPDATE product_variants v
           SET color_family=m.color_family, color_shade=m.color_shade
           FROM color_map m
           WHERE m.status='classified'
             AND v.color_family IS NULL
             AND LOWER(TRIM(v.color))=m.color_raw"""
    )
    print(f"  variants newly stamped: {cur.rowcount}")
    conn.commit()

    cur.execute("SELECT status, COUNT(*) FROM color_map GROUP BY status")
    print("  color_map now:", dict(cur.fetchall()))
    conn.close()


# ----------------------------------------------------------------------
# PASS 2: subcat-rules — FREE deterministic title rules (no AI)
# ----------------------------------------------------------------------
def pass_subcat_rules():
    print("== PASS subcat-rules: free keyword rules on titles ==")
    conn = db()
    cur = conn.cursor()

    # 1. Persist the rules into category_map (the scraper reads from here).
    for cat, pattern, subcat, prio in TITLE_RULES:
        cur.execute(
            """INSERT INTO category_map
               (match_type, pattern, department, category, subcategory,
                priority, source)
               VALUES ('title_keyword', %s, NULL, %s, %s, %s, 'seed')
               ON CONFLICT (match_type, pattern) DO NOTHING""",
            (f"{cat}::{pattern}", cat, subcat, prio),
        )
    conn.commit()

    # 2. Apply them, priority order, only to rows still NULL.
    total = 0
    for cat, pattern, subcat, prio in sorted(TITLE_RULES, key=lambda r: r[3]):
        cur.execute(
            """UPDATE products
               SET subcategory=%s
               WHERE subcategory IS NULL
                 AND LOWER(TRIM(category_normalized))=%s
                 AND name ~* %s""",
            (subcat, cat, pattern),
        )
        total += cur.rowcount
        conn.commit()
    print(f"  products classified by free rules: {total}")
    _subcat_progress(cur)
    conn.close()


# ----------------------------------------------------------------------
# PASS 3: subcat-text — Gemini reads title + raw category (cheap)
# ----------------------------------------------------------------------
def pass_subcat_text():
    print("== PASS subcat-text: Gemini on titles for remaining NULLs ==")
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Process ONE CATEGORY at a time. This lets us state the allowed
    # subcategories once and send only compact "id|title" pairs — cutting
    # tokens per call by ~10x compared to the old approach.
    for cat, allowed in SUBCATS.items():
        cur.execute(
            """SELECT id, name FROM products
               WHERE subcategory IS NULL AND is_active = true
                 AND LOWER(TRIM(category_normalized)) = %s
               ORDER BY id""",
            (cat,),
        )
        rows = cur.fetchall()
        if not rows:
            continue
        print(f"  {cat}: {len(rows)} products to classify")

        BATCH = 60  # just "id|title" per line = very compact
        done = 0
        for i in range(0, len(rows), BATCH):
            batch = rows[i : i + BATCH]
            # Ultra-compact format: one line per product, just id|title
            lines = "\n".join(f'{r["id"]}|{r["name"]}' for r in batch)
            prompt = (
                f'Category: {cat}. Pick ONE subcategory from: '
                f'{json.dumps(allowed)}. '
                f'For each line (id|title), return JSON: '
                f'[{{"id":N,"s":"value-or-null"}}]. '
                f'No markdown.\n{lines}'
            )

            result = gemini([{"text": prompt}])
            if not isinstance(result, list):
                print(f"    batch {i//BATCH+1}: bad response, skipping")
                continue
            ok = set(allowed)
            for item in result:
                pid = item.get("id")
                sub = item.get("s")
                if pid and sub in ok:
                    cur.execute(
                        "UPDATE products SET subcategory=%s "
                        "WHERE id=%s AND subcategory IS NULL",
                        (sub, pid),
                    )
                    done += cur.rowcount
            conn.commit()
            print(f"    batch {i//BATCH+1}/{(len(rows)-1)//BATCH+1}: "
                  f"classified {done} so far")
    _subcat_progress(cur)
    conn.close()


# ----------------------------------------------------------------------
# PASS 4: subcat-vision — Gemini LOOKS at the product image (CV)
# ----------------------------------------------------------------------
def pass_subcat_vision(limit=4000):
    print("== PASS subcat-vision: computer vision on remaining NULLs ==")
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cats = list(SUBCATS.keys())
    cur.execute(
        """SELECT id, name, image_url, LOWER(TRIM(category_normalized)) AS cat
           FROM products
           WHERE subcategory IS NULL AND is_active = true
             AND image_url IS NOT NULL
             AND LOWER(TRIM(category_normalized)) = ANY(%s)
           ORDER BY id LIMIT %s""",
        (cats, limit),
    )
    rows = cur.fetchall()
    print(f"  products going to vision this run: {len(rows)}")

    done = 0
    for r in rows:
        img = fetch_image_b64(r["image_url"])
        if not img:
            continue
        b64, mime = img
        allowed = SUBCATS[r["cat"]]
        prompt = (
            f'This is a product photo of a "{r["cat"]}" item titled '
            f'"{r["name"]}" from an Egyptian fashion store. Look at the image '
            f"and pick exactly ONE subcategory from this list: "
            f"{json.dumps(allowed)}. If the image does not clearly answer, "
            'respond null. Respond ONLY with JSON: {"subcategory": "..."} '
            'or {"subcategory": null}. No markdown.'
        )
        result = gemini([
            {"inline_data": {"mime_type": mime, "data": b64}},
            {"text": prompt},
        ])
        sub = result.get("subcategory") if isinstance(result, dict) else None
        if sub in allowed:
            cur.execute(
                "UPDATE products SET subcategory=%s "
                "WHERE id=%s AND subcategory IS NULL",
                (sub, r["id"]),
            )
            done += cur.rowcount
            conn.commit()
        if done and done % 200 == 0:
            print(f"  vision-classified so far: {done}")
        time.sleep(0.6)
    print(f"  vision-classified total: {done}")
    _subcat_progress(cur)
    conn.close()


# ----------------------------------------------------------------------
# PASS 5: audit — sample every category, vision-verify the CATEGORY label
# ----------------------------------------------------------------------
def pass_audit(sample_per_cat=40):
    print("== PASS audit: vision spot-check of category labels ==")
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(
        """CREATE TABLE IF NOT EXISTS taxonomy_audit (
             id bigserial PRIMARY KEY,
             product_id bigint,
             category_label text,
             vision_verdict text,
             agrees boolean,
             audited_at timestamptz DEFAULT now())"""
    )
    conn.commit()

    cur.execute(
        """SELECT DISTINCT LOWER(TRIM(category_normalized)) AS cat
           FROM products WHERE category_normalized IS NOT NULL
             AND LOWER(TRIM(category_normalized)) NOT IN ('uncategorized','other')"""
    )
    cats = [r["cat"] for r in cur.fetchall()]
    all_cats_json = json.dumps(cats)

    report = {}
    for cat in cats:
        cur.execute(
            """SELECT id, name, image_url FROM products
               WHERE LOWER(TRIM(category_normalized))=%s AND is_active=true
                 AND image_url IS NOT NULL
               ORDER BY random() LIMIT %s""",
            (cat, sample_per_cat),
        )
        sample = cur.fetchall()
        agree = checked = 0
        for r in sample:
            img = fetch_image_b64(r["image_url"])
            if not img:
                continue
            b64, mime = img
            prompt = (
                f"Product photo from an Egyptian fashion store, titled "
                f'"{r["name"]}". Which single category from this list best '
                f"matches what you SEE: {all_cats_json}? Respond ONLY with "
                'JSON: {"category": "..."}. No markdown.'
            )
            result = gemini([
                {"inline_data": {"mime_type": mime, "data": b64}},
                {"text": prompt},
            ])
            verdict = result.get("category") if isinstance(result, dict) else None
            if verdict is None:
                continue
            checked += 1
            ok = verdict == cat
            agree += int(ok)
            cur.execute(
                """INSERT INTO taxonomy_audit
                   (product_id, category_label, vision_verdict, agrees)
                   VALUES (%s,%s,%s,%s)""",
                (r["id"], cat, verdict, ok),
            )
            conn.commit()
            time.sleep(0.6)
        pct = round(agree / checked * 100, 1) if checked else None
        report[cat] = {"checked": checked, "agree_pct": pct}
        print(f"  {cat}: {agree}/{checked} agree ({pct}%)")

    print("\n== AUDIT SUMMARY (full detail in taxonomy_audit table) ==")
    flagged = {c: v for c, v in report.items()
               if v["agree_pct"] is not None and v["agree_pct"] < 90}
    if flagged:
        print("  CATEGORIES NEEDING ATTENTION (<90% agreement):")
        for c, v in flagged.items():
            print(f"   - {c}: {v['agree_pct']}%")
    else:
        print("  All sampled categories >= 90% agreement.")
    conn.close()


# ----------------------------------------------------------------------
def _subcat_progress(cur):
    cur.execute(
        """SELECT LOWER(TRIM(category_normalized)) AS cat,
                  COUNT(*) AS total,
                  COUNT(subcategory) AS filled
           FROM products
           WHERE LOWER(TRIM(category_normalized)) = ANY(%s) AND is_active=true
           GROUP BY 1 ORDER BY total DESC""",
        (list(SUBCATS.keys()),),
    )
    print("  subcategory progress:")
    for cat, total, filled in cur.fetchall():
        pct = round(filled / total * 100, 1) if total else 0
        print(f"   {cat:<12} {filled}/{total} ({pct}%)")


# ----------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass", dest="which", required=True,
                    choices=["colors", "subcat-rules", "subcat-text",
                             "subcat-vision", "audit", "all"])
    ap.add_argument("--vision-limit", type=int, default=4000)
    ap.add_argument("--audit-sample", type=int, default=40)
    args = ap.parse_args()

    steps = {
        "colors": pass_colors,
        "subcat-rules": pass_subcat_rules,
        "subcat-text": pass_subcat_text,
        "subcat-vision": lambda: pass_subcat_vision(args.vision_limit),
        "audit": lambda: pass_audit(args.audit_sample),
    }
    order = (["colors", "subcat-rules", "subcat-text", "subcat-vision",
              "audit"] if args.which == "all" else [args.which])

    # Pick the Google lane (skip for subcat-rules, which needs no AI)
    if any(p != "subcat-rules" for p in order):
        _init_backend()

    for name in order:
        steps[name]()
    print(f"\nDone. Gemini calls used this run: {CALLS_MADE}")
