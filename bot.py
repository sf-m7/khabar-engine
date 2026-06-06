# ═══════════════════════════════════════════════════════
# KHABAR — Telegram Bot (long-poll mode)
# ═══════════════════════════════════════════════════════

import os
import sys
import time
import json
from curl_cffi import requests
from supabase import create_client
from datetime import datetime, timezone

sys.stdout.reconfigure(line_buffering=True)

BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
API          = f"https://api.telegram.org/bot{BOT_TOKEN}"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

FREE_BRAND_LIMIT = 2

# Brand emoji act as visual identifiers since buttons can't hold images
BRANDS = {
    "lc_waikiki": "🛍️ LC Waikiki",
    "town_team":  "👕 Town Team",
    "ravin":      "⚡ Ravin",
    "mens_club":  "👔 Men's Club",
    "tree":       "🌿 Tree",
    "dott_jeans": "🔵 Dott Jeans",
}

BRAND_DISPLAY = {
    "town_team":  "Town Team",
    "ravin":      "Ravin",
    "mens_club":  "Men's Club",
    "tree":       "Tree",
    "dott_jeans": "Dott Jeans",
    "lc_waikiki": "LC Waikiki"
}

CATEGORIES = {
    "tops":        "Tops 👕",
    "bottoms":     "Bottoms 👖",
    "dresses":     "Dresses 👗",
    "outerwear":   "Outerwear 🧥",
    "footwear":    "Footwear 👟",
    "accessories": "Accessories 👜",
}

# Size options shown per category as buttons
CATEGORY_SIZES = {
    "tops":        ["XS", "S", "M", "L", "XL", "XXL", "XXXL"],
    "bottoms":     ["26", "28", "30", "32", "34", "36", "38", "40"],
    "dresses":     ["XS", "S", "M", "L", "XL", "XXL"],
    "outerwear":   ["XS", "S", "M", "L", "XL", "XXL", "XXXL"],
    "footwear":    ["36", "37", "38", "39", "40", "41", "42", "43", "44"],
    "accessories": ["One Size", "S/M", "L/XL"],
}

# ── Telegram Network Session Engine ───────────────────

def get_hardened_session():
    """Initializes browser footprint session to safeguard outbound web actions."""
    return requests.Session(impersonate="chrome124")

def execute_tg_call(method, data=None):
    """Executes resilient outbound interactions using hardened browser wrappers."""
    try:
        with get_hardened_session() as session:
            r = session.post(f"{API}/{method}", json=data or {}, timeout=35)
            return r.json()
    except Exception as e:
        print(f"  Telegram API error ({method}): {e}")
        return {}

def send(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return execute_tg_call("sendMessage", payload)

def edit(chat_id, message_id, text, keyboard=None):
    payload = {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       text,
        "parse_mode": "HTML",
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    else:
        payload["reply_markup"] = {}
    return execute_tg_call("editMessageText", payload)

def answer_cb(callback_id, text=""):
    execute_tg_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

# ── Supabase helpers ──────────────────────────────────

def get_offset():
    try:
        r = supabase.table("bot_state").select("value").eq("key", "last_update_id").execute()
        return int(r.data[0]["value"]) if r.data else 0
    except:
        return 0

def save_offset(update_id):
    supabase.table("bot_state").upsert(
        {"key": "last_update_id", "value": str(update_id)}
    ).execute()

def get_user(telegram_id):
    r = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    return r.data[0] if r.data else None

def create_user(telegram_id, username):
    data = {
        "telegram_id":         telegram_id,
        "username":            username or "",
        "tier":                "free",
        "conversation_state":  "new",
        "temp_data":           {},
        "brands_monitored":    [],
        "categories_selected": [],
        "sizes":               {},
        "joined_at":           datetime.now(timezone.utc).isoformat(),
        "last_active_at":      datetime.now(timezone.utc).isoformat(),
    }
    r = supabase.table("users").insert(data).execute()
    return r.data[0]

def update_user(telegram_id, data):
    data["last_active_at"] = datetime.now(timezone.utc).isoformat()
    supabase.table("users").update(data).eq("telegram_id", telegram_id).execute()

# ── Live deals for welcome screen ─────────────────────

def get_live_deals(limit=3):
    try:
        r = (
            supabase.table("price_events")
            .select("*, products(name, brand)")
            .eq("direction", "down")
            .order("recorded_at", desc=True)
            .limit(limit * 4)
            .execute()
        )
        seen, deals = set(), []
        for e in r.data:
            pid = e.get("product_id")
            if pid not in seen:
                seen.add(pid)
                deals.append(e)
            if len(deals) >= limit:
                break
        return deals
    except Exception as e:
        print(f"  ⚠️ Error fetching welcome snapshots: {e}")
        return []

def format_deals_text(deals):
    if not deals:
        return "  🔍 Scanning now — you'll be among the first to know."
    lines = []
    for d in deals:
        p     = d.get("products") or {}
        name  = (p.get("name") or "Product")[:38]
        brand_raw = d.get("brand", "")
        brand = BRAND_DISPLAY.get(brand_raw, brand_raw).upper()
        
        price_before = float(d.get("price_before") or 0)
        price_after  = float(d.get("price_after") or 0)
        disc = round(((price_before - price_after) / price_before) * 100) if price_before > 0 else 0
        
        lines.append(f"  🔥 {name} — <b>{disc}% off</b> @ {int(price_after)} EGP [{brand}]")
    return "\n".join(lines)

# ── Keyboards ─────────────────────────────────────────

def brands_keyboard(selected, tier="free"):
    rows  = []
    limit = FREE_BRAND_LIMIT if tier == "free" else len(BRANDS)
    for key, label in BRANDS.items():
        tick = "✅ " if key in selected else ""
        rows.append([{"text": f"{tick}{label}", "callback_data": f"brand|{key}"}])
    count = len(selected)
    done  = f"Done → ({count}/{limit} selected)" if count else "← Pick at least one brand"
    rows.append([{"text": done, "callback_data": "brands_done"}])
    return rows

def categories_keyboard(selected):
    rows, row = [], []
    for key, label in CATEGORIES.items():
        tick = "✅ " if key in selected else ""
        row.append({"text": f"{tick}{label}", "callback_data": f"cat|{key}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    label = f"Done → ({len(selected)} selected)" if selected else "Done → (all categories)"
    rows.append([{"text": label, "callback_data": "cats_done"}])
    return rows

def size_keyboard(category):
    sizes = CATEGORY_SIZES.get(category, ["S", "M", "L", "XL"])
    rows, row = [], []
    for size in sizes:
        safe_size = size.replace("/", "-")
        row.append({"text": size, "callback_data": f"sz|{category}|{safe_size}"})
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "Skip (any size) →", "callback_data": f"sz|{category}|skip"}])
    return rows

# ── Conversation steps ─────────────────────────────────

def show_welcome(chat_id, user):
    deals = get_live_deals(3)
    text = (
        "👋 <b>Welcome to Khabar!</b>\n\n"
        "Khabar monitors Egyptian fashion stores every 30 minutes. "
        "When a product matching your taste drops in price, "
        "you get an instant alert — ahead of everyone else.\n\n"
        f"<b>🔥 Deals detected right now:</b>\n{format_deals_text(deals)}\n\n"
        "Takes 30 seconds to set up. Let's go 👇"
    )
    keyboard = [[{"text": "Set up my alerts →", "callback_data": "start_setup"}]]
    send(chat_id, text, keyboard)
    if user:
        update_user(chat_id, {"conversation_state": "new", "temp_data": {}})

def show_brand_selection(chat_id, user, message_id):
    temp     = user.get("temp_data") or {}
    selected = temp.get("selected_brands", [])
    tier     = user.get("tier", "free")
    limit    = FREE_BRAND_LIMIT if tier == "free" else len(BRANDS)
    text = (
        "<b>Step 1 of 3 — Choose brands</b>\n\n"
        f"Free plan: pick up to <b>{limit} brands</b>.\n"
        "Upgrade anytime to monitor all brands."
    )
    edit(chat_id, message_id, text, brands_keyboard(selected, tier))
    update_user(chat_id, {"conversation_state": "setup_brands"})

def show_category_selection(chat_id, user, message_id):
    temp       = user.get("temp_data") or {}
    sel_brands = temp.get("selected_brands", [])
    selected   = temp.get("selected_categories", [])
    names      = ", ".join(BRAND_DISPLAY.get(b, b) for b in sel_brands)
    text = (
        "<b>Step 2 of 3 — Choose categories</b>\n\n"
        f"Monitoring: <b>{names}</b>\n\n"
        "Select the categories you want alerts for.\n"
        "Tap Done to get alerts for all categories."
    )
    edit(chat_id, message_id, text, categories_keyboard(selected))
    update_user(chat_id, {"conversation_state": "setup_categories"})

def show_size_for_category(chat_id, user, message_id):
    temp        = user.get("temp_data") or {}
    pending     = temp.get("sizes_pending", [])
    all_cats    = temp.get("selected_categories", [])

    if not pending:
        complete_setup(chat_id, user, message_id)
        return

    current     = pending[0]
    total       = len(all_cats) if all_cats else len(pending)
    done_so_far = total - len(pending)
    cat_label   = CATEGORIES.get(current, current)

    text = (
        f"<b>Step 3 of 3 — Sizes ({done_so_far + 1}/{total})</b>\n\n"
        f"What's your size for <b>{cat_label}</b>?\n\n"
        "Tap your size. Khabar will only alert you when that size is in stock."
    )
    edit(chat_id, message_id, text, size_keyboard(current))
    update_user(chat_id, {"conversation_state": "setup_sizes"})

def complete_setup(chat_id, user, message_id=None):
    temp       = user.get("temp_data") or {}
    brands     = temp.get("selected_brands", [])
    categories = temp.get("selected_categories", [])
    sizes_dict = temp.get("sizes_collected", {})

    update_user(chat_id, {
        "conversation_state":  "active",
        "brands_monitored":    brands,
        "categories_selected": categories,
        "sizes":               sizes_dict,
        "temp_data":           {},
    })

    try:
        for brand in brands:
            supabase.table("user_brands").upsert(
                {"user_id": chat_id, "brand": brand},
                on_conflict="user_id,brand"
            ).execute()

        for category, size in sizes_dict.items():
            clean_size = size.replace("-", "/")
            supabase.table("user_sizes").upsert(
                {"user_id": chat_id, "category": category, "size": clean_size},
                on_conflict="user_id,category"
            ).execute()

    except Exception as e:
        print(f"  ❌ Relational normalization database write failed: {e}")

    brand_names = [BRAND_DISPLAY.get(b, b) for b in brands]
    size_lines = "\n".join(f"  • {CATEGORIES.get(c, c)}: {s.replace('-', '/')}" for c, s in sizes_dict.items()) if sizes_dict else "  • All sizes"

    text = (
        "✅ <b>You're all set!</b>\n\n"
        f"<b>Brands:</b> {', '.join(brand_names)}\n"
        f"<b>Sizes Mapped:</b>\n{size_lines}\n\n"
        "Khabar is now monitoring the landscape. The moment a genuine markdown matches "
        "your profile settings, you will receive an instant push notification alert."
    )
    send(chat_id, text)

# ── Update router ──────────────────────────────────────

def process_update(update):
    if "message" in update and "text" in update["message"]:
        msg      = update["message"]
        chat_id  = msg["chat"]["id"]
        text     = msg["text"]
        username = msg["from"].get("username", "")

        user = get_user(chat_id) or create_user(chat_id, username)

        if text == "/start":
            show_welcome(chat_id, user)
        return

    if "callback_query" in update:
        cq         = update["callback_query"]
        chat_id    = cq["from"]["id"]
        username   = cq["from"].get("username", "")
        message_id = cq["message"]["message_id"]
        data       = cq["data"]
        answer_cb(cq["id"])

        user = get_user(chat_id) or create_user(chat_id, username)

        if data == "start_setup":
            show_brand_selection(chat_id, user, message_id)

        elif data.startswith("brand|"):
            brand = data.split("|")[1]
            temp  = user.get("temp_data") or {}
            sel   = list(temp.get("selected_brands", []))
            tier  = user.get("tier", "free")
            limit = FREE_BRAND_LIMIT if tier == "free" else len(BRANDS)
            if brand in sel:
                sel.remove(brand)
            elif len(sel) < limit:
                sel.append(brand)
            temp["selected_brands"] = sel
            update_user(chat_id, {"temp_data": temp})
            user    = get_user(chat_id)
            sel_now = (user.get("temp_data") or {}).get("selected_brands", [])
            text = (
                "<b>Step 1 of 3 — Choose brands</b>\n\n"
                f"Free plan: pick up to <b>{limit} brands</b>.\n"
                "Upgrade anytime to monitor all brands."
            )
            edit(chat_id, message_id, text, brands_keyboard(sel_now, tier))

        elif data == "brands_done":
            user = get_user(chat_id)
            sel  = (user.get("temp_data") or {}).get("selected_brands", [])
            if not sel:
                answer_cb(cq["id"], "Please select at least one brand first.")
            else:
                show_category_selection(chat_id, user, message_id)

        elif data.startswith("cat|"):
            cat  = data.split("|")[1]
            temp = user.get("temp_data") or {}
            sel  = list(temp.get("selected_categories", []))
            if cat in sel:
                sel.remove(cat)
            else:
                sel.append(cat)
            temp["selected_categories"] = sel
            update_user(chat_id, {"temp_data": temp})
            user       = get_user(chat_id)
            sel_now    = (user.get("temp_data") or {}).get("selected_categories", [])
            sel_brands = (user.get("temp_data") or {}).get("selected_brands", [])
            names      = ", ".join(BRAND_DISPLAY.get(b, b) for b in sel_brands)
            text = (
                "<b>Step 2 of 3 — Choose categories</b>\n\n"
                f"Monitoring: <b>{names}</b>\n\n"
                "Select categories, or tap Done for everything."
            )
            edit(chat_id, message_id, text, categories_keyboard(sel_now))

        elif data == "cats_done":
            user = get_user(chat_id)
            temp = user.get("temp_data") or {}
            sel  = temp.get("selected_categories", [])
            
            if not sel:
                sel = list(CATEGORIES.keys())
                temp["selected_categories"] = sel

            temp["sizes_pending"]   = list(sel)
            temp["sizes_collected"] = {}
            update_user(chat_id, {"temp_data": temp})
            user = get_user(chat_id)
            show_size_for_category(chat_id, user, message_id)

        elif data.startswith("sz|"):
            _, cat, size = data.split("|")
            temp      = user.get("temp_data") or {}
            pending   = list(temp.get("sizes_pending", []))
            collected = temp.get("sizes_collected", {})

            if cat in pending:
                pending.remove(cat)
            
            if size != "skip":
                collected[cat] = size

            temp["sizes_pending"]   = pending
            temp["sizes_collected"] = collected
            update_user(chat_id, {"temp_data": temp})
            user = get_user(chat_id)
            show_size_for_category(chat_id, user, message_id)

# ── Long-poll main loop worker thread ─────────────────

if __name__ == "__main__":
    print("🚀 Khabar Telegram Bot router operational...")
    offset = get_offset()
    while True:
        try:
            with get_hardened_session() as poll_session:
                updates = poll_session.post(f"{API}/getUpdates", json={"offset": offset + 1, "timeout": 30}, timeout=45).json()
                if "result" in updates:
                    for up in updates["result"]:
                        offset = up["update_id"]
                        save_offset(offset)
                        process_update(up)
        except Exception as e:
            print(f"Loop cooling failure: {e}")
            time.sleep(3)
