# ═══════════════════════════════════════════════════════
# KHABAR — Telegram Bot Poller
# Runs every 5 minutes via GitHub Actions.
# Handles /start, brand/category/size setup, and stores
# user preferences in Supabase.
# ═══════════════════════════════════════════════════════

import os
import sys
import json
import requests
from supabase import create_client
from datetime import datetime, timezone

sys.stdout.reconfigure(line_buffering=True)

BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
API          = f"https://api.telegram.org/bot{BOT_TOKEN}"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

FREE_BRAND_LIMIT = 2

BRANDS = {
    "town_team":  "Town Team",
    "ravin":      "Ravin",
    "mens_club":  "Men's Club",
    "tree":       "Tree",
    "dott_jeans": "Dott Jeans",
}

CATEGORIES = {
    "tops":        "Tops 👕",
    "bottoms":     "Bottoms 👖",
    "dresses":     "Dresses 👗",
    "outerwear":   "Outerwear 🧥",
    "footwear":    "Footwear 👟",
    "accessories": "Accessories 👜",
}

# ── Telegram helpers ─────────────────────────────────

def tg(method, data=None):
    try:
        r = requests.post(f"{API}/{method}", json=data or {}, timeout=10)
        return r.json()
    except Exception as e:
        print(f"  Telegram API error ({method}): {e}")
        return {}

def send(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return tg("sendMessage", payload)

def edit(chat_id, message_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    else:
        payload["reply_markup"] = {}
    return tg("editMessageText", payload)

def answer_cb(callback_id, text=""):
    tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

# ── Supabase helpers ──────────────────────────────────

def get_offset():
    try:
        r = supabase.table("bot_state").select("value").eq("key", "last_update_id").execute()
        return int(r.data[0]["value"]) if r.data else 0
    except:
        return 0

def save_offset(update_id):
    supabase.table("bot_state").upsert({"key": "last_update_id", "value": str(update_id)}).execute()

def get_user(telegram_id):
    r = supabase.table("users").select("*").eq("telegram_id", telegram_id).execute()
    return r.data[0] if r.data else None

def create_user(telegram_id, username):
    data = {
        "telegram_id":        telegram_id,
        "username":           username or "",
        "tier":               "free",
        "conversation_state": "new",
        "temp_data":          {},
        "brands_monitored":   [],
        "categories_selected":[],
        "sizes":              {},
        "joined_at":          datetime.now(timezone.utc).isoformat(),
        "last_active_at":     datetime.now(timezone.utc).isoformat(),
    }
    r = supabase.table("users").insert(data).execute()
    return r.data[0]

def update_user(telegram_id, data):
    data["last_active_at"] = datetime.now(timezone.utc).isoformat()
    supabase.table("users").update(data).eq("telegram_id", telegram_id).execute()

# ── Live deals for welcome screen ────────────────────

def get_live_deals(limit=3):
    try:
        r = (supabase.table("price_events")
             .select("*, products(name, brand)")
             .eq("direction", "down")
             .gte("discount_pct", 25)
             .order("recorded_at", desc=True)
             .limit(limit * 4)
             .execute())
        seen, deals = set(), []
        for e in r.data:
            pid = e.get("product_id")
            if pid not in seen:
                seen.add(pid)
                deals.append(e)
            if len(deals) >= limit:
                break
        return deals
    except:
        return []

def format_deals_text(deals):
    if not deals:
        return "  🔍 Scanning now — you'll be among the first to know."
    lines = []
    for d in deals:
        p = d.get("products") or {}
        name  = (p.get("name") or "Product")[:38]
        brand = BRANDS.get(d.get("brand", ""), d.get("brand", "")).upper()
        disc  = int(d.get("discount_pct") or 0)
        price = int(d.get("price_after") or 0)
        lines.append(f"  🔥 {name} — <b>{disc}% off</b> @ {price} EGP [{brand}]")
    return "\n".join(lines)

# ── Keyboards ─────────────────────────────────────────

def brands_keyboard(selected, tier="free"):
    rows = []
    for key, label in BRANDS.items():
        tick = "✅ " if key in selected else ""
        rows.append([{"text": f"{tick}{label}", "callback_data": f"brand_{key}"}])
    count = len(selected)
    limit = FREE_BRAND_LIMIT if tier == "free" else len(BRANDS)
    done  = f"Done → ({count}/{limit} selected)" if count else "← Select at least one brand"
    rows.append([{"text": done, "callback_data": "brands_done"}])
    return rows

def categories_keyboard(selected):
    rows, row = [], []
    for key, label in CATEGORIES.items():
        tick = "✅ " if key in selected else ""
        row.append({"text": f"{tick}{label}", "callback_data": f"cat_{key}"})
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    label = f"Done → ({len(selected)} selected)" if selected else "Done → (all categories)"
    rows.append([{"text": label, "callback_data": "cats_done"}])
    return rows

# ── Conversation steps ────────────────────────────────

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
        "Upgrade to monitor all 5 brands."
    )
    edit(chat_id, message_id, text, brands_keyboard(selected, tier))
    update_user(chat_id, {"conversation_state": "setup_brands"})

def show_category_selection(chat_id, user, message_id):
    temp      = user.get("temp_data") or {}
    sel_brands = temp.get("selected_brands", [])
    selected  = temp.get("selected_categories", [])
    names     = ", ".join(BRANDS.get(b, b) for b in sel_brands)
    text = (
        "<b>Step 2 of 3 — Choose categories</b>\n\n"
        f"Monitoring: <b>{names}</b>\n\n"
        "Select specific categories, or tap Done to get alerts for everything."
    )
    edit(chat_id, message_id, text, categories_keyboard(selected))
    update_user(chat_id, {"conversation_state": "setup_categories"})

def show_size_input(chat_id, user, message_id):
    text = (
        "<b>Step 3 of 3 — Your size</b>\n\n"
        "Type your size and I'll only alert you when that size is in stock.\n\n"
        "Examples: <code>M</code>  <code>L</code>  <code>XL</code>  "
        "<code>32</code>  <code>28/30</code>\n\n"
        "Or type <code>skip</code> to get alerts for all sizes."
    )
    edit(chat_id, message_id, text)
    update_user(chat_id, {"conversation_state": "setup_size"})

def complete_setup(chat_id, user, size_text):
    temp       = user.get("temp_data") or {}
    brands     = temp.get("selected_brands", [])
    categories = temp.get("selected_categories", [])

    size = None if size_text.lower() == "skip" else size_text.strip()
    sizes_dict = {}
    if size:
        cats = categories if categories else list(CATEGORIES.keys())
        for c in cats:
            sizes_dict[c] = size

    update_user(chat_id, {
        "conversation_state": "active",
        "brands_monitored":   brands,
        "categories_selected": categories,
        "sizes":              sizes_dict,
        "temp_data":          {},
    })

    brand_names = [BRANDS.get(b, b) for b in brands]
    cat_names   = [CATEGORIES.get(c, c) for c in categories] if categories else ["All categories"]
    size_label  = size or "All sizes"

    text = (
        "✅ <b>You're all set!</b>\n\n"
        f"<b>Brands:</b> {', '.join(brand_names)}\n"
        f"<b>Categories:</b> {', '.join(cat_names)}\n"
        f"<b>Size:</b> {size_label}\n\n"
        "Khabar checks for deals every 30 minutes. The moment a qualifying "
        "discount appears, you'll get an alert instantly.\n\n"
        "Use /settings to update your preferences anytime."
    )
    send(chat_id, text)

# ── Update router ─────────────────────────────────────

def process_update(update):

    # ── Button tap ──
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

        elif data.startswith("brand_"):
            brand   = data[6:]
            temp    = user.get("temp_data") or {}
            sel     = list(temp.get("selected_brands", []))
            tier    = user.get("tier", "free")
            limit   = FREE_BRAND_LIMIT if tier == "free" else len(BRANDS)

            if brand in sel:
                sel.remove(brand)
            elif len(sel) < limit:
                sel.append(brand)

            temp["selected_brands"] = sel
            update_user(chat_id, {"temp_data": temp})
            user = get_user(chat_id)

            text = (
                "<b>Step 1 of 3 — Choose brands</b>\n\n"
                f"Free plan: pick up to <b>{limit} brands</b>.\n"
                "Upgrade to monitor all 5 brands."
            )
            sel_now = (user.get("temp_data") or {}).get("selected_brands", [])
            edit(chat_id, message_id, text, brands_keyboard(sel_now, tier))

        elif data == "brands_done":
            user = get_user(chat_id)
            sel  = (user.get("temp_data") or {}).get("selected_brands", [])
            if not sel:
                answer_cb(cq["id"], "Please select at least one brand first.")
            else:
                show_category_selection(chat_id, user, message_id)

        elif data.startswith("cat_"):
            cat  = data[4:]
            temp = user.get("temp_data") or {}
            sel  = list(temp.get("selected_categories", []))
            if cat in sel:
                sel.remove(cat)
            else:
                sel.append(cat)
            temp["selected_categories"] = sel
            update_user(chat_id, {"temp_data": temp})
            user = get_user(chat_id)
            sel_now = (user.get("temp_data") or {}).get("selected_categories", [])
            sel_brands = (user.get("temp_data") or {}).get("selected_brands", [])
            names = ", ".join(BRANDS.get(b, b) for b in sel_brands)
            text = (
                "<b>Step 2 of 3 — Choose categories</b>\n\n"
                f"Monitoring: <b>{names}</b>\n\n"
                "Select categories, or tap Done for everything."
            )
            edit(chat_id, message_id, text, categories_keyboard(sel_now))

        elif data == "cats_done":
            user = get_user(chat_id)
            show_size_input(chat_id, user, message_id)

        return

    # ── Text message ──
    if "message" not in update:
        return

    msg      = update["message"]
    chat_id  = msg["chat"]["id"]
    username = msg.get("from", {}).get("username", "")
    text     = msg.get("text", "").strip()
    if not text:
        return

    user = get_user(chat_id)

    if text.startswith("/start"):
        user = user or create_user(chat_id, username)
        show_welcome(chat_id, user)

    elif text.startswith("/settings"):
        user = user or create_user(chat_id, username)
        update_user(chat_id, {"conversation_state": "new", "temp_data": {}})
        show_welcome(chat_id, get_user(chat_id))

    elif user and user.get("conversation_state") == "setup_size":
        complete_setup(chat_id, user, text)

    elif not user or user.get("conversation_state") in ("new", None):
        send(chat_id, "Send /start to set up your deal alerts 👋")

    else:
        send(chat_id,
             "✅ Your alerts are active!\n\n"
             "Use /settings to update your preferences.\n"
             "Deals arrive automatically when Khabar finds them.")


# ── Entry point ───────────────────────────────────────

def main():
    print("🤖 Khabar bot poller starting...")
    supabase_fresh = create_client(SUPABASE_URL, SUPABASE_KEY)

    offset = get_offset()
    next_offset = offset + 1 if offset else 0
    print(f"  Fetching updates from offset {next_offset}...")

    resp = tg("getUpdates", {"offset": next_offset, "limit": 100, "timeout": 0})
    if not resp.get("ok"):
        print(f"  ⚠️  getUpdates failed: {resp}")
        return

    updates = resp.get("result", [])
    print(f"  {len(updates)} update(s) to process.")

    for upd in updates:
        try:
            process_update(upd)
        except Exception as e:
            print(f"  ❌ Error on update {upd.get('update_id')}: {e}")
        save_offset(upd["update_id"])

    print("  ✅ Done.")

if __name__ == "__main__":
    main()
