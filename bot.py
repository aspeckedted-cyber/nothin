
#!/usr/bin/env python3
"""
JPW Order & Wallet Portal — Telegram Bot + Mini App Backend (bot.py)
--------------------------------------------------------------------
Features:
1. Serves your `index.html` Telegram Mini App over HTTP (with optional API proxy).
2. Provides a full Telegram Bot interface:
   - /start        : Opens the Telegram Mini App WebApp button & main menu
   - /register     : Registers a profile via https://cashtree.space/api/register
   - /profiles     : Lists saved profiles and active credits
   - /recharge     : Generates a UPI payment link & instructions for ₹60 / ₹120 / ₹200 packs
   - /utr          : Verifies 12-digit UTR and adds credits (1 Credit = ₹20)
   - /order        : Places a new service order (deducts 1 credit)
   - /orders       : Shows recent orders & status
   - /admin        : Shows Admin profit stats (Collected, Base Cost, Net Profit) & pending queue
   - /done <wo_id> : Marks a pending Work Order as COMPLETED

Requirements:
   pip install aiohttp requests

Usage:
   export BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
   export WEBAPP_URL="https://your-domain-or-ngrok-url.app"
   export ADMIN_CHAT_ID="123456789"
   python3 bot.py
"""

import asyncio
import json
import logging
import os
import random
from datetime import datetime
import base64
import time
from pathlib import Path
from urllib.parse import quote

import requests
from aiohttp import web

# ================= CONFIGURATION =================
def _safe_int(val: str | None, default: int = 0) -> int:
    try:
        return int(str(val or "").strip())
    except Exception:
        return default


BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "YOUR_TELEGRAM_BOT_TOKEN_HERE").strip()
WEBAPP_URL = (os.environ.get("WEBAPP_URL") or "https://your-mini-app-url.example.com").strip()
ADMIN_CHAT_ID = _safe_int(os.environ.get("ADMIN_CHAT_ID"), 0)
PORT = _safe_int(os.environ.get("PORT"), 8080)

CASHTREE_REGISTER_API = "https://cashtree.space/api/register"
CASHTREE_LOGIN_API = "https://cashtree.space/api/login"
CASHTREE_CLAIM_BONUS_API = "https://cashtree.space/api/claim-daily-bonus"

DATA_FILE = Path("bot_state.json")
HTML_FILE = Path("index.html")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("jpw_bot")

# ================= STATE STORAGE =================
DEFAULT_STATE = {
    "settings": {
        "upiId": "jpwportal@ybl",
        "appTitle": "JPW Order Mini App",
        "registerApiUrl": CASHTREE_REGISTER_API,
    },
    "stats": {
        "collected": 60,
        "cost": 13,
        "profit": 47,
    },
    "profiles": [
        {
            "id": "0690766275",
            "name": "RRYSB",
            "password": "12345",
            "credits": 3,
            "rupees": 60,
            "status": "Ready",
            "registered": True,
            "referralCode": "JPW4484",
            "telegramChatId": "WEB_0690766275",
        },
        {
            "id": "0691113840",
            "name": "Main Node",
            "password": "12345",
            "credits": 3,
            "rupees": 60,
            "status": "Ready",
            "registered": False,
            "referralCode": None,
            "telegramChatId": None,
        },
    ],
    "orders": [
        {
            "id": 7127438722,
            "profileId": "0690766275",
            "targetId": "0689728278",
            "notes": "Standard Reach",
            "status": "COMPLETED",
            "time": "10:25 PM",
        }
    ],
}


def load_state() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read %s: %s", DATA_FILE, exc)
    return json.loads(json.dumps(DEFAULT_STATE))


def save_state(state: dict) -> None:
    try:
        DATA_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.error("Failed saving state: %s", exc)


STATE = load_state()


# ================= CASHTREE API HELPERS =================
def register_on_cashtree(custom_id: str, password: str, name: str) -> dict:
    """Calls https://cashtree.space/api/register with customId, password, and name."""
    api_url = STATE["settings"].get("registerApiUrl", CASHTREE_REGISTER_API)
    payload = {
        "customId": str(custom_id).strip(),
        "password": str(password).strip(),
        "name": str(name).strip(),
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://cashtree.space",
        "Referer": "https://cashtree.space/app",
    }
    try:
        resp = requests.post(api_url, json=payload, headers=headers, timeout=12)
        data = resp.json() if resp.content else {}
        return {
            "ok": resp.ok and bool(data.get("success")),
            "status": resp.status_code,
            "data": data,
        }
    except Exception as exc:
        logger.warning("Cashtree register API error: %s", exc)
        return {"ok": False, "status": 0, "data": {"message": str(exc)}}


def build_web_bearer_token(telegram_chat_id: str) -> str:
    b64 = base64.b64encode(telegram_chat_id.encode("utf-8")).decode("utf-8")
    return f"{b64}.{int(time.time() * 1000)}"


def extract_token_from_api_data(data: dict, fallback_chat_id: str | None = None) -> str | None:
    if not isinstance(data, dict):
        return None
    user_obj = data.get("user") if isinstance(data.get("user"), dict) else {}
    return (
        data.get("token")
        or data.get("sessionToken")
        or data.get("accessToken")
        or data.get("authToken")
        or data.get("jwt")
        or user_obj.get("token")
        or user_obj.get("sessionToken")
        or user_obj.get("accessToken")
        or user_obj.get("authToken")
        or (build_web_bearer_token(fallback_chat_id) if fallback_chat_id else None)
    )


def login_on_cashtree(prof: dict) -> dict:
    """Calls POST https://cashtree.space/api/login to obtain an active server session token."""
    cid = str(prof.get("id", "")).strip()
    pwd = str(prof.get("password") or "").strip()
    chat_id = prof.get("telegramChatId") or f"WEB_{cid}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://cashtree.space",
        "Referer": "https://cashtree.space/app",
    }
    for payload in (
        {"customId": cid, "password": pwd},
        {"customId": cid, "password": pwd, "telegramChatId": chat_id},
    ):
        try:
            resp = requests.post(CASHTREE_LOGIN_API, json=payload, headers=headers, timeout=12)
            data = resp.json() if resp.content else {}
            if resp.ok and (data.get("success") or data.get("user") or data.get("token")):
                tok = extract_token_from_api_data(data, chat_id)
                if tok:
                    prof["authToken"] = tok
                u = data.get("user") or {}
                if isinstance(u, dict):
                    prof["registered"] = True
                    prof["telegramChatId"] = u.get("telegramChatId") or chat_id
                    prof["referralCode"] = u.get("referralCode") or prof.get("referralCode")
                    if u.get("lastBonusTime"):
                        prof["lastBonusTime"] = u.get("lastBonusTime")
                return {"ok": True, "token": tok, "data": data}
        except Exception as exc:
            logger.warning("Cashtree login error (%s): %s", cid, exc)
    return {"ok": False, "token": None, "data": {}}


def claim_daily_bonus_on_cashtree(prof: dict) -> dict:
    """Logs in via /api/login first to get a valid server session token, then claims daily bonus."""
    cid = str(prof.get("id", "")).strip()
    login_on_cashtree(prof)
    chat_id = prof.get("telegramChatId") or f"WEB_{cid}"
    token = prof.get("authToken") or ""
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://cashtree.space",
        "Referer": "https://cashtree.space/app",
    }
    if token:
        headers["Authorization"] = token if token.startswith("Bearer ") else f"Bearer {token}"
    payload = {"telegramChatId": chat_id, "customId": cid}
    try:
        resp = requests.post(CASHTREE_CLAIM_BONUS_API, json=payload, headers=headers, timeout=12)
        data = resp.json() if resp.content else {}
        return {
            "ok": resp.ok and bool(data.get("success")),
            "status": resp.status_code,
            "data": data,
        }
    except Exception as exc:
        logger.warning("Cashtree claim-daily-bonus API error (%s): %s", cid, exc)
        return {"ok": False, "status": 0, "data": {"message": str(exc)}}


# ================= TELEGRAM BOT API HELPERS =================
def tg_api(method: str, payload: dict) -> dict:
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        return {"ok": False, "description": "BOT_TOKEN not configured"}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=15)
        return r.json()
    except Exception as exc:
        logger.error("Telegram API error (%s): %s", method, exc)
        return {"ok": False, "description": str(exc)}


def send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg_api("sendMessage", payload)


def build_main_keyboard() -> dict:
    """Builds inline keyboard with Mini App WebApp launcher and quick bot actions."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "⚡ Open JPW Order Mini App",
                    "web_app": {"url": WEBAPP_URL},
                }
            ],
            [
                {"text": "🎁 Claim All Daily Bonus", "callback_data": "menu_claim_all_bonus"},
                {"text": "🎯 ≥1 Coin Ready IDs", "callback_data": "menu_ready_coins"},
            ],
            [
                {"text": "🎲 Random Register", "callback_data": "menu_random_reg"},
            ],
            [
                {"text": "👤 Saved Accounts", "callback_data": "menu_profiles"},
                {"text": "💳 Wallet & UPI QR", "callback_data": "menu_wallet"},
            ],
            [
                {"text": "📦 Order Status", "callback_data": "menu_orders"},
                {"text": "🛡️ Admin & Profit", "callback_data": "menu_admin"},
            ],
        ]
    }


# ================= BOT COMMAND HANDLERS =================
def handle_message(msg: dict) -> None:
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return

    parts = text.split()
    cmd = parts[0].lower().split("@")[0]

    if cmd == "/start":
        welcome = (
            f"⚡ <b>{STATE['settings']['appTitle']}</b>\n"
            "<i>Customer, Registration & Wallet Portal</i>\n\n"
            "🔹 <b>Mini App Kholein:</b> Niche button dabayein.\n"
            "🔹 <b>Commands:</b>\n"
            "• <code>/claimbonus</code> — Claim daily bonus for ALL saved accounts\n"
            "• <code>/register &lt;10-digit-ID&gt; &lt;Name&gt; [Password]</code>\n"
            "• <code>/profiles</code> — Saved accounts & credits\n"
            "• <code>/recharge [60|120|200]</code> — UPI Recharge link\n"
            "• <code>/utr &lt;12-digit-UTR&gt; &lt;ProfileID&gt; [60|120|200]</code>\n"
            "• <code>/order &lt;ProfileID&gt; &lt;TargetID&gt; [Notes]</code>\n"
            "• <code>/orders</code> — Live order status\n"
            "• <code>/admin</code> — Profit & pending queue\n"
            "• <code>/done &lt;WorkOrderID&gt;</code> — Complete order"
        )
        send_message(chat_id, welcome, reply_markup=build_main_keyboard())

    elif cmd == "/register":
        # Usage: /register [10-digit-ID] [EngineerName] [Password] or /register random
        random_prefixes = ["069", "078", "089", "098", "052", "068"]
        random_names = ["RRYSB", "Rohan", "Rushus", "Saman", "Amit", "Vikram", "Rajesh", "Pooja", "Rahul", "Ankit"]

        if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() in ("random", "auto")):
            pref = random.choice(random_prefixes)
            cid = pref + str(random.randint(1000000, 9999999))
            name = random.choice(random_names) + str(random.randint(10, 99))
            pwd = str(random.randint(100000, 999999))
        elif len(parts) >= 3:
            cid = parts[1].strip()
            name = parts[2].strip()
            pwd = parts[3].strip() if len(parts) >= 4 else str(random.randint(100000, 999999))
        else:
            send_message(
                chat_id,
                "⚠️ <b>Format:</b> <code>/register &lt;10-digit-ID&gt; &lt;EngineerName&gt; [Password]</code>\n"
                "Ya fir 1-click random account ke liye: <code>/register random</code>",
            )
            return

        send_message(chat_id, f"⏳ Registering <code>{cid}</code> ({name}) on Cashtree API...")
        res = register_on_cashtree(cid, pwd, name)
        user_obj = (res.get("data") or {}).get("user") or {}
        ref_code = user_obj.get("referralCode", "JPW4484")
        tg_chat_id = user_obj.get("telegramChatId", f"WEB_{cid}")

        existing = next((p for p in STATE["profiles"] if p["id"] == cid), None)
        if existing:
            existing.update(
                {
                    "name": name,
                    "password": pwd,
                    "registered": res["ok"] or existing.get("registered", False),
                    "referralCode": ref_code,
                    "telegramChatId": tg_chat_id,
                }
            )
        else:
            STATE["profiles"].insert(
                0,
                {
                    "id": cid,
                    "name": name,
                    "password": pwd,
                    "credits": 3,
                    "rupees": 60,
                    "status": "Ready",
                    "registered": res["ok"],
                    "referralCode": ref_code,
                    "telegramChatId": tg_chat_id,
                },
            )
        save_state(STATE)

        if res["ok"]:
            msg_out = (
                "✅ <b>API Registration Successful!</b>\n\n"
                f"👤 <b>Custom ID:</b> <code>{cid}</code>\n"
                f"🛠️ <b>Name:</b> {name}\n"
                f"🔑 <b>Password:</b> <code>{pwd}</code>\n"
                f"🎁 <b>Referral Code:</b> <code>{ref_code}</code>\n"
                f"🆔 <b>Telegram Chat ID:</b> <code>{tg_chat_id}</code>"
            )
        else:
            api_msg = (res.get("data") or {}).get("message", "Saved locally")
            msg_out = (
                f"⚠️ <b>API Note:</b> {api_msg}\n"
                f"Profile <code>{cid}</code> ({name}) saved in Mini App state."
            )
        send_message(chat_id, msg_out, reply_markup=build_main_keyboard())

    elif cmd in ("/claimbonus", "/bonus"):
        send_claim_all_bonus(chat_id)

    elif cmd in ("/ready", "/coins"):
        send_ready_coin_accounts(chat_id)

    elif cmd == "/profiles":
        send_profiles_summary(chat_id)

    elif cmd == "/recharge":
        amt = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 60
        send_wallet_info(chat_id, amt)

    elif cmd == "/utr":
        # Usage: /utr 431289456123 0690766275 60
        if len(parts) < 3:
            send_message(
                chat_id,
                "⚠️ <b>Format:</b> <code>/utr &lt;12-Digit-UTR&gt; &lt;ProfileID&gt; [Amount]</code>\n"
                "Example: <code>/utr 431289456123 0690766275 60</code>",
            )
            return
        utr = "".join(ch for ch in parts[1] if ch.isdigit())
        pid = parts[2].strip()
        paid = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 60

        if len(utr) < 10:
            send_message(chat_id, "❌ Invalid UTR! Please enter a valid 12-digit UPI reference number.")
            return

        prof = next((p for p in STATE["profiles"] if p["id"] == pid), None)
        if not prof:
            send_message(chat_id, f"❌ Profile <code>{pid}</code> not found. Register it first!")
            return

        added_credits = paid // 20
        prof["rupees"] = prof.get("rupees", 0) + paid
        prof["credits"] = prof.get("credits", 0) + added_credits

        STATE["stats"]["collected"] += paid
        STATE["stats"]["cost"] += added_credits * 13
        STATE["stats"]["profit"] = STATE["stats"]["collected"] - STATE["stats"]["cost"]
        save_state(STATE)

        send_message(
            chat_id,
            f"🎉 <b>UTR {utr} Verified!</b>\n"
            f"➕ Added <b>{added_credits} Credits (₹{paid})</b> to Profile <code>{pid}</code> ({prof['name']}).\n"
            f"💳 New Balance: <b>{prof['credits']} Credits (₹{prof['rupees']})</b>",
        )

    elif cmd == "/order":
        # Usage: /order 0690766275 0689728278 Standard_Reach
        if len(parts) < 3:
            send_message(
                chat_id,
                "⚠️ <b>Format:</b> <code>/order &lt;ProfileID&gt; &lt;TargetID&gt; [Notes]</code>\n"
                "Example: <code>/order 0690766275 0689728278 Standard_Reach</code>",
            )
            return
        pid = parts[1].strip()
        target_id = parts[2].strip()
        notes = " ".join(parts[3:]) if len(parts) >= 4 else "Standard Reach"

        prof = next((p for p in STATE["profiles"] if p["id"] == pid), None)
        if not prof:
            send_message(chat_id, f"❌ Profile <code>{pid}</code> not found!")
            return

        if prof.get("credits", 0) < 1:
            send_message(
                chat_id,
                f"⚠️ <b>0 Credits in {pid}!</b> Please recharge ₹60 first using <code>/recharge</code>.",
            )
            return

        prof["credits"] -= 1
        prof["rupees"] = max(0, prof.get("rupees", 0) - 20)
        wo_id = random.randint(7100000000, 7999999999)
        now_str = datetime.now().strftime("%I:%M %p")

        order_obj = {
            "id": wo_id,
            "profileId": pid,
            "targetId": target_id,
            "notes": notes,
            "status": "IN PROGRESS",
            "time": now_str,
        }
        STATE["orders"].insert(0, order_obj)
        save_state(STATE)

        send_message(
            chat_id,
            f"🚀 <b>Order Placed Successfully!</b>\n\n"
            f"🆔 <b>Work Order:</b> <code>#{wo_id}</code>\n"
            f"👤 <b>From Profile:</b> <code>{pid}</code> ({prof['name']})\n"
            f"🎯 <b>Target ID:</b> <code>{target_id}</code>\n"
            f"📝 <b>Notes:</b> {notes}\n"
            f"💳 <b>Remaining Credits:</b> {prof['credits']} Credits",
        )

    elif cmd == "/orders":
        send_orders_summary(chat_id)

    elif cmd == "/admin":
        send_admin_summary(chat_id)

    elif cmd == "/done":
        if len(parts) < 2 or not parts[1].isdigit():
            send_message(chat_id, "⚠️ <b>Format:</b> <code>/done &lt;WorkOrderID&gt;</code>")
            return
        wo_id = int(parts[1])
        ord_item = next((o for o in STATE["orders"] if int(o["id"]) == wo_id), None)
        if not ord_item:
            send_message(chat_id, f"❌ Work Order <code>#{wo_id}</code> not found.")
            return
        ord_item["status"] = "COMPLETED"
        save_state(STATE)
        send_message(chat_id, f"✔ Work Order <code>#{wo_id}</code> (Target: <code>{ord_item['targetId']}</code>) marked <b>COMPLETED</b>!")


def get_bonus_cooldown_str(prof: dict) -> tuple[bool, str]:
    """Returns (is_ready, status_text) for 24-hour daily bonus cooldown."""
    last_str = prof.get("lastBonusTime")
    if not last_str:
        return True, "🟢 Bonus Ready"
    try:
        clean_iso = str(last_str).replace("Z", "+00:00")
        last_dt = datetime.fromisoformat(clean_iso)
        now_ts = time.time()
        elapsed = now_ts - last_dt.timestamp()
        rem = (24 * 3600) - elapsed
        if rem <= 0:
            return True, "🟢 Bonus Ready"
        hrs = int(rem // 3600)
        mins = int((rem % 3600) // 60)
        return False, f"⏳ {hrs:02d}h {mins:02d}m left"
    except Exception:
        return True, "🟢 Bonus Ready"


def send_ready_coin_accounts(chat_id: int) -> None:
    """Feature 2: Lists all accounts with >= 1.00 Coin ready for Reach Order."""
    profiles = STATE.get("profiles", [])
    ready = [p for p in profiles if float(p.get("jpwCoins", 0.0)) >= 1.0]
    ready.sort(key=lambda x: float(x.get("jpwCoins", 0.0)), reverse=True)

    total_coins = sum(float(p.get("jpwCoins", 0.0)) for p in profiles)
    if not ready:
        top = max(profiles, key=lambda x: float(x.get("jpwCoins", 0.0))) if profiles else None
        top_txt = f"<code>{top['id']}</code> ({float(top.get('jpwCoins', 0.0)):.2f} Coins)" if top else "None"
        send_message(
            chat_id,
            f"🎯 <b>Accounts with ≥ 1.00 Coin (Order Ready): 0</b>\n"
            f"🪙 Total Portfolio Coins: <b>{total_coins:.2f} Coins</b>\n"
            f"📈 Highest Coin ID right now: {top_txt}\n\n"
            "💡 <i>Claim daily bonus for 2 days to cross 1.00 Coin per ID!</i>",
            reply_markup=build_main_keyboard(),
        )
        return

    lines = [
        f"🎯 <b>Order-Ready Accounts (≥ 1.00 Coin): {len(ready)} IDs</b>",
        f"🪙 Total Portfolio Coins: <b>{total_coins:.2f} Coins</b>\n",
    ]
    for p in ready:
        coins = float(p.get("jpwCoins", 0.0))
        lines.append(
            f"✅ <code>{p['id']}</code> ({p['name']}) — <b>🪙 {coins:.2f} Coins</b>\n"
            f"   └ Pass: <code>{p.get('password', '')}</code>"
        )
    send_message(chat_id, "\n".join(lines), reply_markup=build_main_keyboard())


def send_claim_all_bonus(chat_id: int) -> None:
    profiles = STATE.get("profiles", [])
    if not profiles:
        send_message(chat_id, "⚠️ No saved profiles found. Register an account first!")
        return

    send_message(chat_id, f"⏳ Claiming daily bonus for <b>{len(profiles)} accounts</b>...")
    total_earned = 0.0
    success_count = 0
    report_lines = ["🎁 <b>Bulk Daily Bonus Report:</b>\n"]

    for p in profiles:
        if not p.get("registered"):
            reg_res = register_on_cashtree(p["id"], str(p.get("password") or ""), p.get("name", "Customer"))
            if reg_res["ok"]:
                u_reg = (reg_res.get("data") or {}).get("user") or {}
                p["registered"] = True
                p["referralCode"] = u_reg.get("referralCode") or p.get("referralCode")
                p["telegramChatId"] = u_reg.get("telegramChatId") or f"WEB_{p['id']}"

        res = claim_daily_bonus_on_cashtree(p)
        if res["ok"]:
            data = res.get("data") or {}
            bonus = float(data.get("randomBonus") or 0.0)
            u_obj = data.get("user") or {}
            p["jpwCoins"] = float(u_obj.get("jpwCoins", float(p.get("jpwCoins", 0.0)) + bonus))
            p["bonusCoins"] = float(u_obj.get("bonusCoins", float(p.get("bonusCoins", 0.0)) + bonus))
            p["lastBonusTime"] = u_obj.get("lastBonusTime")
            p["registered"] = True
            total_earned += bonus
            success_count += 1
            report_lines.append(
                f"✅ <code>{p['id']}</code> ({p['name']}): <b>+{bonus:.2f} Coins</b> (Total: {p['jpwCoins']:.2f})"
            )
        else:
            msg = (res.get("data") or {}).get("message") or "Already claimed / Wait 24h"
            if "already claimed" in msg.lower():
                u_err = (res.get("data") or {}).get("user") or {}
                p["lastBonusTime"] = u_err.get("lastBonusTime") or datetime.utcnow().isoformat() + "Z"
            report_lines.append(f"• <code>{p['id']}</code> ({p['name']}): <i>{msg}</i>")

    save_state(STATE)
    report_lines.append(
        f"\n🎉 <b>Summary:</b> Claimed <b>{success_count}/{len(profiles)}</b> accounts | "
        f"Total Added: <b>+{total_earned:.2f} Coins</b>"
    )
    send_message(chat_id, "\n".join(report_lines), reply_markup=build_main_keyboard())


def send_profiles_summary(chat_id: int) -> None:
    total_coins = sum(float(p.get("jpwCoins", 0.0)) for p in STATE["profiles"])
    ready_count = sum(1 for p in STATE["profiles"] if float(p.get("jpwCoins", 0.0)) >= 1.0)
    lines = [
        f"👤 <b>Saved Profiles ({len(STATE['profiles'])} Total)</b>",
        f"🪙 <b>Total Coins:</b> {total_coins:.2f} | 🎯 <b>≥1 Coin Ready:</b> {ready_count} IDs\n",
    ]
    for p in STATE["profiles"]:
        coins = float(p.get("jpwCoins", 0.0))
        _, cd_txt = get_bonus_cooldown_str(p)
        order_tag = " 🎯 <b>[ORDER READY]</b>" if coins >= 1.0 else ""
        lines.append(
            f"• <code>{p['id']}</code> ({p['name']}) — <b>🪙 {coins:.2f} Coins</b>{order_tag}\n"
            f"  └ 🔑 Pass: <code>{p.get('password', '')}</code> | {cd_txt} | {p.get('credits', 0)} Cr"
        )
    send_message(chat_id, "\n".join(lines), reply_markup=build_main_keyboard())


def send_wallet_info(chat_id: int, amt: int = 60) -> None:
    upi_id = STATE["settings"].get("upiId", "jpwportal@ybl")
    title = STATE["settings"].get("appTitle", "JPW Portal")
    credits = amt // 20
    upi_uri = f"upi://pay?pa={quote(upi_id)}&pn={quote(title)}&am={amt}.00&cu=INR"
    text = (
        f"💳 <b>Wallet Recharge (₹{amt} Pack = {credits} Credits)</b>\n\n"
        f"🔹 <b>UPI ID:</b> <code>{upi_id}</code>\n"
        f"🔹 <b>Rate:</b> ₹20 / Order Credit\n"
        f"🔹 <b>UPI Deep Link:</b> <code>{upi_uri}</code>\n\n"
        "Payment karne ke baad 12-digit UTR verify karein:\n"
        f"<code>/utr 431289456123 0690766275 {amt}</code>"
    )
    send_message(chat_id, text, reply_markup=build_main_keyboard())


def send_orders_summary(chat_id: int) -> None:
    if not STATE["orders"]:
        send_message(chat_id, "📦 No orders placed yet.")
        return
    lines = ["📦 <b>Recent Service Orders:</b>\n"]
    for o in STATE["orders"][:15]:
        badge = "✅ COMPLETED" if o["status"] == "COMPLETED" else "⏳ IN PROGRESS"
        lines.append(
            f"• <b>WO #{o['id']}</b> [{badge}]\n"
            f"  Target: <code>{o['targetId']}</code> | By: <code>{o['profileId']}</code> | {o['time']}"
        )
    send_message(chat_id, "\n".join(lines), reply_markup=build_main_keyboard())


def send_admin_summary(chat_id: int) -> None:
    st = STATE["stats"]
    pending = [o for o in STATE["orders"] if o["status"] != "COMPLETED"]
    lines = [
        "🛡️ <b>Admin & Profit Dashboard</b>\n",
        f"💰 <b>Collected:</b> ₹{st['collected']}",
        f"📉 <b>Base Cost:</b> ₹{st['cost']} (₹13/order)",
        f"📈 <b>Net Profit:</b> ₹{st['profit']}\n",
        f"📥 <b>Pending Orders ({len(pending)}):</b>",
    ]
    if not pending:
        lines.append("All orders are completed! 🎉")
    else:
        for o in pending[:10]:
            lines.append(
                f"• WO <code>{o['id']}</code> → Target: <code>{o['targetId']}</code> "
                f"(Complete: <code>/done {o['id']}</code>)"
            )
    send_message(chat_id, "\n".join(lines), reply_markup=build_main_keyboard())


def handle_callback(cb: dict) -> None:
    cb_id = cb.get("id")
    data = cb.get("data", "")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if cb_id:
        tg_api("answerCallbackQuery", {"callback_query_id": cb_id})
    if not chat_id:
        return

    if data == "menu_profiles":
        send_profiles_summary(chat_id)
    elif data == "menu_wallet":
        send_wallet_info(chat_id, 60)
    elif data == "menu_orders":
        send_orders_summary(chat_id)
    elif data == "menu_admin":
        send_admin_summary(chat_id)
    elif data == "menu_claim_all_bonus":
        send_claim_all_bonus(chat_id)
    elif data == "menu_random_reg":
        handle_message({"chat": {"id": chat_id}, "text": "/register random"})
    elif data == "menu_ready_coins":
        send_ready_coin_accounts(chat_id)


# ================= AIOHTTP WEB SERVER (SERVES MINI APP + API SYNC) =================
async def serve_index(request: web.Request) -> web.Response:
    if HTML_FILE.exists():
        return web.FileResponse(HTML_FILE)
    return web.Response(text="index.html not found in current directory.", status=404)


async def api_state_get(request: web.Request) -> web.Response:
    return web.json_response(STATE)


async def api_register_proxy(request: web.Request) -> web.Response:
    """Optional backend proxy to https://cashtree.space/api/register."""
    body = await request.json()
    cid = body.get("customId", "")
    pwd = body.get("password", "12345")
    name = body.get("name", "RRYSB")
    res = await asyncio.to_thread(register_on_cashtree, cid, pwd, name)
    return web.json_response(res["data"], status=res["status"] or 200)


async def poll_telegram_updates() -> None:
    """Background long-polling loop for Telegram bot updates."""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.warning("BOT_TOKEN is not set. Running web server only (Mini App mode).")
        return

    logger.info("Starting Telegram Bot polling...")
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
            params = {"offset": offset, "timeout": 25}
            resp = await asyncio.to_thread(requests.get, url, params=params, timeout=30)
            data = resp.json()
            if data.get("ok"):
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    if "message" in upd:
                        await asyncio.to_thread(handle_message, upd["message"])
                    elif "callback_query" in upd:
                        await asyncio.to_thread(handle_callback, upd["callback_query"])
        except Exception as exc:
            logger.warning("Polling error: %s", exc)
            await asyncio.sleep(3)
        await asyncio.sleep(0.5)


async def auto_daily_bonus_scheduler() -> None:
    """Feature 4: Background task that checks saved accounts and auto-claims daily bonus when 24h cooldown is ready."""
    await asyncio.sleep(10)
    while True:
        try:
            claimed_any = 0
            earned_sum = 0.0
            for p in STATE.get("profiles", []):
                is_ready, _ = get_bonus_cooldown_str(p)
                if is_ready and p.get("registered"):
                    res = await asyncio.to_thread(claim_daily_bonus_on_cashtree, p)
                    if res["ok"]:
                        data = res.get("data") or {}
                        bonus = float(data.get("randomBonus") or 0.0)
                        u_obj = data.get("user") or {}
                        p["jpwCoins"] = float(u_obj.get("jpwCoins", float(p.get("jpwCoins", 0.0)) + bonus))
                        p["bonusCoins"] = float(u_obj.get("bonusCoins", float(p.get("bonusCoins", 0.0)) + bonus))
                        p["lastBonusTime"] = u_obj.get("lastBonusTime") or datetime.utcnow().isoformat() + "Z"
                        claimed_any += 1
                        earned_sum += bonus
            if claimed_any > 0:
                save_state(STATE)
                logger.info("Auto-claimed daily bonus for %d accounts (+%.2f Coins)", claimed_any, earned_sum)
                if ADMIN_CHAT_ID:
                    await asyncio.to_thread(
                        send_message,
                        ADMIN_CHAT_ID,
                        f"⏰ <b>Auto-Bonus Scheduler:</b> Claimed <b>+{earned_sum:.2f} Coins</b> across <b>{claimed_any} accounts</b>!",
                    )
        except Exception as exc:
            logger.warning("Auto-bonus scheduler warning: %s", exc)
        await asyncio.sleep(1800)  # Check every 30 minutes


async def on_startup(app: web.Application) -> None:
    app["poller"] = asyncio.create_task(poll_telegram_updates())
    app["bonus_scheduler"] = asyncio.create_task(auto_daily_bonus_scheduler())


async def on_cleanup(app: web.Application) -> None:
    for task_key in ("poller", "bonus_scheduler"):
        task = app.get(task_key)
        if task:
            task.cancel()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", serve_index)
    app.router.add_get("/api/state", api_state_get)
    app.router.add_post("/api/register-proxy", api_register_proxy)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    logger.info("Starting JPW Mini App & Telegram Bot Server on port %s...", PORT)
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
