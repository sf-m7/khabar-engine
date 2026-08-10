#!/usr/bin/env python3
"""
Khabar Taxonomy Backfill — Phase C, Passes 2-5
================================================
Clears the color queue and fills subcategories for ALL categories,
then audits the results. Designed to be:

  - IDEMPOTENT: safe to run repeatedly; only touches rows still unresolved.
  - DICTIONARY-DRIVEN: every AI answer is written into color_map /
    category_map so the daily scraper inherits the knowledge for free.
    invented label is rejected and the row stays unclassified.

Passes (run in this order):
  colors         deterministic keyword rules -> color_map -> stamps variants
  subcat-rules   FREE deterministic title-keyword rules -> subcategories (no AI)
  subcat-text    deterministic title-keyword rules for remaining NULLs
  audit          Random sample per category, vision-verified accuracy report
  all            Everything above, in order

Usage:  python taxonomy_backfill.py --pass colors
Env:    KHABAR_DB_URL                (Postgres connection string)
        GOOGLE_SERVICE_ACCOUNT_KEY   (preferred: Vertex AI, uses $300 credits)
"""

import argparse
import json
import os
import re
import sys
import time

import psycopg2
from khabar_db import CA_BUNDLE
import psycopg2.extras
import requests

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
DB_URL = os.environ.get("KHABAR_DB_URL", "").strip() or os.environ.get("SUPABASE_DB_URL")
SERVICE_ACCOUNT_KEY = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")

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
# PASS: sizes — deterministic size normalization (no AI; same spirit as
# subcat-rules). Added 2026-07.
# ----------------------------------------------------------------------
#
# WHY THIS IS ITS OWN PASS, NOT PART OF THE COLOR/SUBCATEGORY WORK:
# Sizes are not one vocabulary — a product's raw "size" value can come from
# up to 6 different real-world systems, and the SAME raw text can mean
# different things depending on category (e.g. "42" is a waist inch on
# jeans but an EU dress size on a dress). So classification is gated by
# category_normalized FIRST, then by pattern within that domain.
#
# WHAT THIS PASS DELIBERATELY DOES NOT DO:
# It does not force every raw value into a family. Ambiguous notations
# (Turkish dual Beden/Boy sizing, waist-inseam combos, dual-size ranges
# like "S-M") are written to size_review_queue instead of guessed — a wrong
# guess here becomes a false market claim later. Mohammed reviews that
# queue; this pass only auto-applies patterns with no real ambiguity.
#
# DOMAIN GATE: which categories get which size system.
SIZE_DOMAIN_BOTTOMS = {"trousers", "jeans", "shorts", "joggers", "sweatpants", "leggings"}
SIZE_DOMAIN_TOPS_EU = {"t-shirts", "shirts", "dresses", "skirts", "sweaters", "cardigans",
                        "jackets", "coats", "blazers", "vests", "hoodies", "sweatshirts",
                        "polos", "tank-tops", "jumpsuits", "kaftans", "bodysuits", "pajamas"}
# Categories intentionally NOT size-normalized here: bags/belts/hats/jewelry/
# watches/sunglasses/scarves (not a garment-size axis — arafa's "18 INCH"
# bug lives here), footwear (EU shoe numbers overlap visually with EU dress
# numbers but are a different scale — needs its own pass, not this one),
# underwear (bra-band sizing like "80/B" is its own system).

LETTER_LADDER = {
    "xxs": ("XXS", 1), "xs": ("XS", 2), "s": ("S", 3), "m": ("M", 4), "l": ("L", 5),
    "xl": ("XL", 6), "xxl": ("2XL", 7), "2xl": ("2XL", 7), "xxxl": ("3XL", 8),
    "3xl": ("3XL", 8), "4xl": ("4XL", 9), "5xl": ("5XL", 10), "6xl": ("6XL", 11),
    "7xl": ("7XL", 12), "small": ("S", 3), "medium": ("M", 4), "large": ("L", 5),
}
ONE_SIZE_TOKENS = {"one size", "onesize", "os", "free", "free size", "freesize"}

# Standard EU numeric -> letter-equivalent conversion (general apparel, not
# menswear collar sizing which uses the same numbers for a different thing —
# collar sizing only appears on formal/dress shirts and isn't in this data
# in volume; flagged for later if it shows up).
EU_TO_LETTER = {
    "34": ("XXS", 1), "36": ("XS", 2), "38": ("S", 3), "40": ("M", 4),
    "42": ("L", 5), "44": ("L", 5), "46": ("XL", 6), "48": ("2XL", 7),
    "50": ("3XL", 8), "52": ("3XL", 8), "54": ("4XL", 9), "56": ("4XL", 9),
    "58": ("5XL", 10), "60": ("5XL", 10),
}

COLOR_WORD_RE = re.compile(
    r"^(black|white|off[\s_-]?white|grey|gray|beige|brown|blue|navy|nave[_ ]?blue|"
    r"teal|petrol|petroL[_ ]?blue|green|olive|khaki|yellow|orange|red|burgundy|"
    r"maroon|pink|rose|purple|lilac|lavender|mauve|gold|silver|copper|turquoise|"
    r"fuchsia|coral|mint|cream|camel|tan|taupe|multi|stone|vison|ecru|russet|"
    r"anthrcite|anthracite|d[_ ]?blue|light[_ ]?blue|dark[_ ]?grey|dark[_ ]?blue|"
    r"sky[_ ]?blue|royal[_ ]?blue|bottel[_ ]?green|olive[_ ]?green)$", re.I)
DIMENSION_RE = re.compile(r"\d+\s*(inch|cm|mm)\b|\d+\s*[*x]\s*\d+", re.I)
VENDOR_CODE_RE = re.compile(r"^[A-Z]\d{3,4}$")           # N002, J014, L006, U038
HEIGHT_CM_RE   = re.compile(r"^S1[3-8]\d$", re.I)         # S130..S189, kids/teen height
KIDS_AGE_RE    = re.compile(r"^\d{1,2}\s*Y$", re.I)
KIDS_MONTH_RE  = re.compile(r"^\d{1,2}M[\s_-]\d{1,2}M$", re.I)
KIDS_TODDLER_RE = re.compile(r"^\d[T]$", re.I)
KIDS_RANGE_RE  = re.compile(r"^\d{1,2}[\s_-]\d{1,2}$")    # bare "8-9", "6-7" — only
                                                            # trusted as kids in kids-flagged
                                                            # brands/categories; else -> review
GARBAGE_EXACT  = {"def"}

# Patterns that are genuinely ambiguous — never auto-classified, always
# queued for review with a reason so Mohammed can see WHY it's unresolved.
REVIEW_PATTERNS = [
    (re.compile(r"^beden\s*\d+\s*-\s*boy\s*\d+$", re.I), "turkish_dual_size_height"),
    (re.compile(r"^\d{2,3}[\s_-]\d{2,3}$"), "possible_waist_inseam_combo"),
    (re.compile(r"^(xs|s|m|l|xl|2xl|3xl)[\s/-](xs|s|m|l|xl|2xl|3xl)$", re.I), "dual_letter_range"),
    (re.compile(r"^(xs|s|m|l|xl)[\s/-]\d{2,3}$", re.I), "letter_plus_eu_combo"),
    (re.compile(r"^\d{2,3}\s*/\s*[ab]$", re.I), "possible_bra_band_size"),
    (re.compile(r"^x/xx$", re.I), "ambiguous_dual_notation"),
]


def classify_size(raw, category_normalized):
    """Returns (size_family, size_system, status, review_reason).
    status is one of: classified / excluded / review."""
    if not raw:
        return None, None, "excluded", None
    v = str(raw).strip()
    v_clean = v.lower()

    # Garbage / known-bad placeholder values first.
    if v_clean in GARBAGE_EXACT:
        return None, None, "excluded", None
    # Color words leaking into the size field (the arafa/tomato-class bug —
    # not brand-specific; seen across several brands). The root cause gets
    # a scraper fix separately; here we just don't let it pollute the map.
    if COLOR_WORD_RE.match(v_clean.replace(" ", "_")) or COLOR_WORD_RE.match(v_clean):
        return None, None, "excluded", None
    # Physical dimensions (bag/towel "size" — not a garment size at all).
    if DIMENSION_RE.search(v_clean):
        return None, None, "excluded", None
    # Height-based kids/teen sizing (S150 etc) — real signal, but its own
    # system; not auto-merged into anything else.
    if HEIGHT_CM_RE.match(v):
        return v.upper(), "kids_height_cm", "classified", None
    # Internal vendor/SKU codes (N002, J014, L006, U038 ...) — not sizes.
    if VENDOR_CODE_RE.match(v):
        return None, None, "excluded", None
    # One-size.
    if v_clean in ONE_SIZE_TOKENS:
        return "one-size", "one_size", "classified", None
    # Kids age notations.
    if KIDS_AGE_RE.match(v) or KIDS_MONTH_RE.match(v) or KIDS_TODDLER_RE.match(v):
        return v.upper().replace(" ", ""), "kids_age", "classified", None

    # Ambiguous notations — queue, never guess.
    for pattern, reason in REVIEW_PATTERNS:
        if pattern.match(v):
            return None, None, "review", reason

    # Bare numeric range like "8-9" is only trusted as kids sizing when the
    # category itself is a kids-flagged one; caller passes that context via
    # category_normalized being in a kids set if/when that taxonomy exists.
    # For now: treat as review rather than assume.
    if KIDS_RANGE_RE.match(v):
        return None, None, "review", "bare_numeric_range_unclear_domain"

    # Letter ladder (case/space insensitive).
    if v_clean in LETTER_LADDER:
        fam, order = LETTER_LADDER[v_clean]
        return fam, "letter", "classified", None

    # Numeric — meaning depends on domain gate.
    if v.isdigit():
        if category_normalized in SIZE_DOMAIN_BOTTOMS:
            n = int(v)
            if 20 <= n <= 60:
                return v, "waist_numeric", "classified", None
        if category_normalized in SIZE_DOMAIN_TOPS_EU:
            if v in EU_TO_LETTER:
                fam, order = EU_TO_LETTER[v]
                return fam, "eu_numeric", "classified", None
            # numeric outside the known EU ladder in a tops/dresses category
            # is very likely a kids numeric size (tree/arafa pattern) —
            # queue rather than guess which kids ladder it belongs to.
            return None, None, "review", "numeric_outside_eu_ladder_possible_kids"

    return None, None, "review", "unrecognized_pattern"


def pass_sizes():
    print("== PASS sizes: deterministic size normalization (no AI) ==")
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(
        """SELECT DISTINCT pv.size, p.category_normalized, p.brand
           FROM product_variants pv JOIN products p ON p.id = pv.product_id
           WHERE pv.delisted_at IS NULL AND p.is_active = true
             AND pv.size IS NOT NULL AND pv.size <> ''
             AND pv.size_status IS NULL"""
    )
    rows = cur.fetchall()
    print(f"  distinct (size, category, brand) combinations to classify: {len(rows)}")

    classified = excluded = queued = 0
    review_rows = []
    for r in rows:
        fam, system, status, reason = classify_size(r["size"], r["category_normalized"])
        cur.execute(
            """UPDATE product_variants pv SET size_family=%s, size_system=%s, size_status=%s
               FROM products p
               WHERE pv.product_id = p.id AND pv.delisted_at IS NULL
                 AND pv.size = %s AND p.category_normalized IS NOT DISTINCT FROM %s
                 AND p.brand = %s""",
            (fam, system, status, r["size"], r["category_normalized"], r["brand"]),
        )
        if status == "classified":
            classified += cur.rowcount
        elif status == "excluded":
            excluded += cur.rowcount
        else:
            queued += cur.rowcount
            review_rows.append((r["size"], r["category_normalized"], r["brand"], cur.rowcount, reason))
        conn.commit()

    # Dedup review rows into the queue (one row per size+category+brand).
    for size_raw, cat, brand, n, reason in review_rows:
        cur.execute(
            """INSERT INTO size_review_queue (size_raw, category_normalized, brand, sample_count, reason)
               VALUES (%s,%s,%s,%s,%s)""",
            (size_raw, cat, brand, n, reason),
        )
    conn.commit()

    print(f"  classified: {classified} variants")
    print(f"  excluded (garbage/color-leak/dimension/vendor-code): {excluded} variants")
    print(f"  queued for review: {queued} variants across {len(review_rows)} distinct patterns")
    print("  Review queue is in size_review_queue — nothing there was guessed.")
    conn.close()


# ----------------------------------------------------------------------
# Plumbing
# ----------------------------------------------------------------------
def db():
    if not DB_URL:
        sys.exit("ERROR: SUPABASE_DB_URL env var is missing.")
    return psycopg2.connect(DB_URL, sslrootcert=CA_BUNDLE)


def _request_target():
    """Return (url, headers, params) for whichever lane we're on."""
    # Seed any new raw colours from variants into color_map
    cur.execute(
        """INSERT INTO color_map (color_raw, status, source, created_at, updated_at)
           SELECT DISTINCT LOWER(TRIM(pv.color)), 'unclassified', 'auto_seed', now(), now()
           FROM product_variants pv
           WHERE pv.color IS NOT NULL AND TRIM(pv.color) <> ''
           ON CONFLICT (color_raw) DO NOTHING"""
    )
    print(f"  newly seeded into color_map: {cur.rowcount}")
    conn.commit()

    cur.execute(
        "SELECT color_raw FROM color_map WHERE status = 'unclassified' ORDER BY color_raw"
    )
    queue = [r[0] for r in cur.fetchall()]
    print(f"  queue size: {len(queue)} names")

    # Keyword rules aligned to COLOR_FAMILIES. Order matters: specific before
    # generic (e.g. "off white" before "white", "navy" before general blue).
    RULES = [
        # Arabic
        ("\u0627\u0633\u0648\u062f", "black"), ("\u0627\u0628\u064a\u0636", "white"),
        # blacks
        ("black", "black"),
        # whites (off-white, ecru, cream, ivory before "white")
        ("off white", "white"), ("offwhite", "white"), ("ecru", "white"),
        ("cream", "white"), ("ivory", "white"), ("white", "white"),
        # greys
        ("anthracite", "grey"), ("charcoal", "grey"), ("silver", "grey"),
        ("grey", "grey"), ("gray", "grey"),
        # beiges
        ("vison", "beige"), ("nude", "beige"), ("sand", "beige"), ("tan", "beige"),
        ("stone", "beige"), ("biscuit", "beige"), ("caramel", "beige"), ("beige", "beige"),
        # browns
        ("coffee", "brown"), ("camel", "brown"), ("chocolate", "brown"),
        ("mink", "brown"), ("taupe", "brown"), ("mocha", "brown"), ("brown", "brown"),
        # blues (teal separate in COLOR_FAMILIES)
        ("teal", "teal"), ("turquoise", "teal"),
        ("navy", "blue"), ("denim", "blue"), ("indigo", "blue"),
        ("cobalt", "blue"), ("petrol", "blue"), ("sky", "blue"), ("blue", "blue"),
        # greens
        ("khaki", "green"), ("olive", "green"), ("mint", "green"),
        ("emerald", "green"), ("sage", "green"), ("green", "green"),
        # yellows
        ("mustard", "yellow"), ("gold", "yellow"), ("lemon", "yellow"), ("yellow", "yellow"),
        # oranges
        ("peach", "orange"), ("coral", "orange"), ("apricot", "orange"),
        ("rust", "orange"), ("terracotta", "orange"), ("orange", "orange"),
        # reds (burgundy separate in COLOR_FAMILIES)
        ("burgundy", "burgundy"), ("maroon", "burgundy"), ("wine", "burgundy"),
        ("bordeaux", "burgundy"),
        ("crimson", "red"), ("red", "red"),
        # pinks
        ("rose", "pink"), ("fuchsia", "pink"), ("fuschia", "pink"),
        ("magenta", "pink"), ("salmon", "pink"), ("blush", "pink"), ("pink", "pink"),
        # purples
        ("lilac", "purple"), ("lavender", "purple"), ("mauve", "purple"),
        ("violet", "purple"), ("plum", "purple"), ("purple", "purple"),
        # metallics
        ("metallic", "metallic"), ("chrome", "metallic"), ("copper", "metallic"),
        ("bronze", "metallic"),
        # multi
        ("multi", "multi"), ("colored", "multi"), ("colour", "multi"),
        ("print", "multi"), ("floral", "multi"), ("striped", "multi"),
        ("pattern", "multi"), ("tie dye", "multi"), ("camo", "multi"),
    ]

    classified = 0
    junk_markers = ["size", "cm", "ml", "kg", "pcs", "/", "x ", "0x"]
    for raw in queue:
        c = raw.strip().lower()
        if not c or len(c) <= 1:
            cur.execute(
                "UPDATE color_map SET status='junk', source='keyword', updated_at=now() "
                "WHERE color_raw=%s AND status='unclassified'", (raw,))
            continue
        # Junk detection (size codes, SKUs)
        if any(m in c for m in junk_markers) or c.replace(".", "").replace("-", "").isdigit():
            cur.execute(
                "UPDATE color_map SET status='junk', source='keyword', updated_at=now() "
                "WHERE color_raw=%s AND status='unclassified'", (raw,))
            continue
        # Keyword match
        family = None
        for kw, fam in RULES:
            if kw in c:
                family = fam
                break
        if family and family in COLOR_FAMILIES:
            # Shade = the raw name itself (compact)
            cur.execute(
                """UPDATE color_map
                   SET color_family=%s, color_shade=%s, is_compound=false,
                       secondary_family=NULL, status='classified',
                       source='keyword', updated_at=now()
                   WHERE color_raw=%s AND status='unclassified'""",
                (family, raw, raw))
            classified += cur.rowcount
        # else: stays unclassified — will be visible in audit

    conn.commit()
    print(f"  keyword-classified: {classified}")

    # Stamp variants from the updated dictionary
    print("  stamping variants from the updated dictionary ...")
    cur.execute(
        """UPDATE product_variants v
           SET color_family=m.color_family, color_shade=m.color_shade
           FROM color_map m
           WHERE m.status='classified'
             AND LOWER(TRIM(v.color)) = m.color_raw
             AND (v.color_family IS NULL OR v.color_family <> m.color_family)"""
    )
    print(f"  variants updated: {cur.rowcount}")
    conn.commit()
    conn.close()


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
# PASS 3: subcat-text — deterministic title rules (no AI)
# ----------------------------------------------------------------------
def pass_subcat_text():
    """Deterministic title-keyword subcategory classifier — replaces Gemini.
    Applies TITLE_RULES to products with no subcategory, per category. Same
    approach as pass_subcat_rules but runs AFTER it to catch any new products."""
    print("== PASS subcat-text: deterministic title rules (no AI) ==")
    conn = db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    import re as _re

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
        # Build category-specific rules from TITLE_RULES
        cat_rules = [(regex, subcat, prio) for (c, regex, subcat, prio)
                     in TITLE_RULES if c == cat]
        cat_rules.sort(key=lambda x: x[2])
        done = 0
        for r in rows:
            title = (r["name"] or "").lower()
            for regex, subcat, _ in cat_rules:
                if _re.search(regex, title):
                    cur.execute(
                        "UPDATE products SET subcategory=%s WHERE id=%s AND subcategory IS NULL",
                        (subcat, r["id"]))
                    done += cur.rowcount
                    break
        conn.commit()
        if done:
            print(f"  {cat}: classified {done} of {len(rows)}")
    _subcat_progress(cur)
    conn.close()




if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass", dest="which", required=True,
                    choices=["colors", "sizes", "subcat-rules", "subcat-text", "all"])
    args = ap.parse_args()

    steps = {
        "colors": pass_colors,
        "sizes": pass_sizes,
        "subcat-rules": pass_subcat_rules,
        "subcat-text": pass_subcat_text,
    }
    order = (["colors", "sizes", "subcat-rules", "subcat-text"]
             if args.which == "all" else [args.which])

    for name in order:
        steps[name]()
    print("\nDone (zero AI calls — fully deterministic).")
