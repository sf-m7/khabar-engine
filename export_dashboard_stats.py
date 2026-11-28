# ═══════════════════════════════════════════════════════
# KHABAR — Dashboard Stats Exporter
# Writes stats.json for the public research dashboard.
# Only ever exports aggregate counts — no brand names, no
# prices, no per-brand figures. That's deliberate: this file
# is read by a public GitHub Pages site with no access control.
# ═══════════════════════════════════════════════════════

import os
import json
import datetime
import psycopg2
import certifi
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

DB_URL = os.environ["PLANETSCALE_DB_URL"]


def connection_url_with_trusted_ca(url):
    """
    This DSN requires channel_binding, which forbids anything weaker than
    sslmode=verify-full — so the fix isn't to lower the SSL mode. The
    actual problem is that psycopg2-binary's bundled OpenSSL doesn't
    reliably find a trusted CA store on the GitHub Actions runner. This
    keeps verify-full and just points it at certifi's CA bundle instead.
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["sslmode"] = ["verify-full"]
    query["sslrootcert"] = [certifi.where()]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def main():
    conn = psycopg2.connect(connection_url_with_trusted_ca(DB_URL))
    cur = conn.cursor()

    cur.execute("""
        SELECT
          (SELECT COUNT(DISTINCT brand) FROM products WHERE is_active = true),
          (SELECT COUNT(*) FROM products WHERE is_active = true),
          (SELECT COUNT(*) FROM product_variants),
          (SELECT COUNT(*) FROM product_variants WHERE is_in_stock = true),
          (CURRENT_DATE - (SELECT MIN(first_seen_at)::date FROM products)),
          (SELECT MIN(first_seen_at)::date FROM products)
    """)
    (brands_total, products_total, variants_total,
     variants_in_stock, days_tracked, first_date) = cur.fetchone()

    cur.execute("""
        SELECT snapshot_date::date AS d, COUNT(DISTINCT product_id) AS n
        FROM price_snapshots
        GROUP BY d
        ORDER BY d ASC
    """)
    last_14d = [row[1] for row in cur.fetchall()]

    stats = {
        "last_updated": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days_tracked": int(days_tracked),
        "first_tracked_date": str(first_date),
        "brands": {"total": int(brands_total)},
        "products": {"total": int(products_total), "last_14d": last_14d},
        "variants": {"total": int(variants_total), "in_stock": int(variants_in_stock)},
    }

    with open("stats.json", "w") as f:
        json.dump(stats, f, indent=2)

    print(f"✅ stats.json written: {stats}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
