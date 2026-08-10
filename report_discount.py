"""
report_discount.py — WEEKLY "What to Discount".
================================================================================
Leads with a FUSED discount board (report_lib.discount_verdicts): per category,
a market verdict fusing discount depth + clearance effectiveness + distress:
ACTIVE CLEARANCE / STICKY DISTRESS / HEALTHY MARKDOWN / FIRM. Category-level, so
subcategory coverage doesn't apply. Detail (depth + distress) and the maturing
clear-rate curve follow.
"""

import pandas as pd
import report_lib as R
import report_html as H

MIN_EVENTS = 20


def _drank(s):
    s = str(s)
    return 3 if s.startswith("urgent") else 2 if s == "watch" else 1


def run():
    conn = R.connect()
    board = R.discount_verdicts(conn, min_events=MIN_EVENTS)

    if board is None or board.empty:
        body = H.section("01", "Discount board", H.why("Not enough discount volume this week."))
        return H.write("what-to-discount", "Khabar \u2014 What to Discount",
                       "Weekly \u00b7 market discount posture", body)

    vmap = {"ACTIVE CLEARANCE": "#B45309", "STICKY DISTRESS": "#B0413A",
            "HEALTHY MARKDOWN": "#3F7A4B", "FIRM": "#3F7A4B"}
    rows = board.to_dict("records")
    sticky = [r for r in rows if r["verdict"] == "STICKY DISTRESS"]
    active = [r for r in rows if r["verdict"] == "ACTIVE CLEARANCE"]

    # --- verdict -------------------------------------------------------------
    parts = []
    if active:
        parts.append(f"<b>{', '.join(H.esc(r['category_normalized']) for r in active[:3])}</b> "
                     f"are in active clearance \u2014 expect deep competitor markdowns")
    if sticky:
        parts.append(f"<b>{', '.join(H.esc(r['category_normalized']) for r in sticky[:3])}</b> "
                     f"are stuck (deep discounts not clearing) \u2014 don't get dragged in")
    subline = "; ".join(parts) + "." if parts else "See the board for each category's discount state."
    v = H.verdict(
        "The decision this report answers",
        "Where the market is discounting hard, where it's stuck, and where it holds.",
        subline)

    # --- 1 · the discount board (fused) -------------------------------------
    brows = []
    for r in rows[:14]:
        vc = vmap.get(r["verdict"], "#6C6A64")
        why_str = " \u00b7 ".join(r["why"])
        brows.append([
            H.esc(r["category_normalized"]),
            f'<span class="m" style="color:{vc};font-weight:700">{r["verdict"]}</span>',
            f'<span style="font-size:12px">{H.esc(why_str)}</span>',
            f'<span class="dist {r["distress"]}">{r["distress"].upper()}</span>',
        ])
    btbl = H.table([("Category", False), ("Verdict", False), ("Why", False),
                    ("Distress", False)], brows)
    s1 = H.section("01", "The discount board \u2014 market verdict by category", btbl + H.why(
        "ACTIVE CLEARANCE = discounting hard and it's moving stock (competitor markdowns likely). "
        "STICKY DISTRESS = deep discounts that AREN'T clearing, dead stock rising \u2014 a discount war "
        "that isn't working. HEALTHY MARKDOWN = discounts clear efficiently. FIRM = little discounting; "
        "sells near full price. Depth is honest (vs first-observed price)."))

    # --- 2 · depth + distress detail ----------------------------------------
    max_depth = max(r["avg_depth"] for r in rows) or 1
    drows = []
    for r in sorted(rows, key=lambda x: x["avg_depth"], reverse=True)[:12]:
        bar = (f'<div class="depthbar"><i style="width:{r["avg_depth"]/max_depth*100:.0f}%"></i>'
               f'<span>{r["avg_depth"]:.0f}%</span></div>')
        drows.append([
            H.esc(r["category_normalized"]), bar,
            f'<span class="m">{r["clear"]:.0f}%</span>',
            f'<span class="m">{int(r["esc"])}</span>',
            f'<span class="m">{int(r["dead"])}</span>',
        ])
    dtbl = H.table([("Category", False), ("Avg depth", False), ("Clears", True),
                    ("Escalating", True), ("Dead stock", True)], drows)
    s2 = H.section("02", "Depth & distress detail", dtbl + H.why(
        "“Clears” = share of sell-outs that happened while on discount. Low clears + high distress "
        "is the STICKY signal; low clears + calm = sells at full price."))

    # --- 3 · clear-rate curve (maturing) ------------------------------------
    curve = R.latest(conn, "signal_l2_clear_rate_by_depth")
    if not curve.empty:
        cells = len(curve)
        zero = int((curve["clear_rate_pct"] == 0).sum())
        overall = round(100.0 * curve["cleared"].sum() / max(1, curve["products"].sum()), 1)
        mat = H.maturing("Not yet reliable — filling in as history accumulates.",
                         f"The 'what depth actually clears' curve needs more witnessed history: "
                         f"{zero} of {cells} cells still too thin (overall {overall}% cleared in-window). "
                         f"It sharpens on its own — no action needed.")
    else:
        mat = H.maturing("Not yet computing.", "signal_l2_clear_rate_by_depth will populate on the next runs.")
    s3 = H.section("03", "Clear-rate by discount depth", mat, badge="MATURING")

    # --- 4 · coverage --------------------------------------------------------
    s4 = H.section("04", "Coverage & confidence", H.coverage([
        ("<b>Discount + distress:</b> deep price history (reliable)", "normal"),
        ("excludes tree, dalydress (phantom)", "excl"),
        ("category-level \u2014 subcategory coverage not a factor", "normal"),
        ("<b>Clear-rate curve:</b> maturing \u2014 see \u00a73", "normal"),
    ]))

    body = v + s1 + s2 + s3 + s4
    return H.write("what-to-discount", "Khabar \u2014 What to Discount",
                   "Weekly \u00b7 market discount posture", body)


if __name__ == "__main__":
    run()
