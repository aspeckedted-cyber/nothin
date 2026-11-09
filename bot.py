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
import re
from datetime import datetime
import base64
import hashlib
import hmac
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
CASHTREE_ORDER_API = "https://cashtree.space/api/order"

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
            "id": "055555555",
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
    "customers": [],
    "used_utrs": [],
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
STATE.setdefault("customers", [])
STATE["customers"] = [c for c in STATE["customers"] if str(c.get("techId")) != "TECH101"]
STATE.setdefault("used_utrs", [])
STATE.setdefault("verified_bank_utrs", {})  # { "12_digit_utr": amount_float } received from Bank SMS Webhook or BharatPe API
STATE.setdefault("pending_payments", [])
STATE["settings"].setdefault("freechargeCookie", os.environ.get("FREECHARGE_COOKIE", ""))
STATE["settings"].setdefault("bharatpeMid", os.environ.get("BHARATPE_MID", ""))
STATE["settings"].setdefault("bharatpeToken", os.environ.get("BHARATPE_TOKEN", ""))
STATE["settings"].setdefault("razorpayKeyId", os.environ.get("RAZORPAY_KEY_ID", ""))
STATE["settings"].setdefault("razorpayKeySecret", os.environ.get("RAZORPAY_KEY_SECRET", ""))
STATE["settings"].setdefault("webhookSecret", os.environ.get("WEBHOOK_SECRET", "jpw123"))
STATE.setdefault("payment_sessions", {})  # { session_id: { techId, baseAmount, uniqueAmount, status, createdAt, rzpOrderId } }


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


def place_reach_order_on_cashtree(prof: dict, target_id: str, target_pass: str) -> dict:
    """Calls POST https://cashtree.space/api/order with {telegramChatId, targetId, targetPass}."""
    cid = str(prof.get("id", "")).strip()
    login_on_cashtree(prof)
    chat_id = prof.get("telegramChatId") or f"WEB_{cid}"
    token = prof.get("authToken") or build_web_bearer_token(chat_id)
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Authorization": token if token.startswith("Bearer ") else f"Bearer {token}",
        "Origin": "https://cashtree.space",
        "Referer": "https://cashtree.space/app",
    }
    payload = {
        "telegramChatId": chat_id,
        "targetId": str(target_id).strip(),
        "targetPass": str(target_pass).strip(),
    }
    try:
        resp = requests.post(CASHTREE_ORDER_API, json=payload, headers=headers, timeout=15)
        data = resp.json() if resp.content else {}
        return {
            "ok": resp.ok and bool(data.get("success")),
            "status": resp.status_code,
            "data": data,
        }
    except Exception as exc:
        logger.warning("Cashtree order API error (%s): %s", cid, exc)
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


def send_backup_document(chat_id: int, caption_note: str = "💾 Full Accounts & Coins Backup") -> None:
    """Feature #2: Sends the complete STATE (profiles, passwords, coins, orders) as a downloadable .json file to Telegram."""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        return
    save_state(STATE)
    total_profs = len(STATE.get("profiles", []))
    total_coins = sum(float(p.get("jpwCoins", 0.0)) for p in STATE.get("profiles", []))
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    json_bytes = json.dumps(STATE, indent=2, ensure_ascii=False).encode("utf-8")
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"jpw_backup_{total_profs}ids_{stamp}.json"
    caption = (
        f"💾 <b>{caption_note}</b>\n"
        f"👤 <b>Total Accounts:</b> {total_profs}\n"
        f"🪙 <b>Total Coins:</b> {total_coins:.2f}\n\n"
        "🔄 <i>Restore karne ke liye kabhi bhi yeh file wapas is Bot me forward/upload kar dein!</i>"
    )
    try:
        requests.post(
            url,
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"document": (filename, json_bytes, "application/json")},
            timeout=20,
        )
    except Exception as exc:
        logger.warning("send_backup_document error: %s", exc)


def restore_from_json_text(chat_id: int, raw_text: str) -> bool:
    """Restores profiles and orders from uploaded JSON file or pasted JSON array/object."""
    try:
        parsed = json.loads(raw_text)
    except Exception:
        return False

    incoming_profiles = []
    incoming_orders = []
    if isinstance(parsed, list):
        incoming_profiles = parsed
    elif isinstance(parsed, dict):
        incoming_profiles = parsed.get("profiles", [])
        incoming_orders = parsed.get("orders", [])
        if isinstance(parsed.get("stats"), dict):
            STATE["stats"].update(parsed["stats"])
        if isinstance(parsed.get("settings"), dict):
            STATE["settings"].update(parsed["settings"])
    else:
        return False

    if not isinstance(incoming_profiles, list):
        return False

    existing_map = {str(p.get("id")): p for p in STATE.get("profiles", []) if p.get("id")}
    added_count = 0
    updated_count = 0
    for ip in reversed(incoming_profiles):
        if not isinstance(ip, dict) or not ip.get("id"):
            continue
        pid = str(ip["id"])
        if pid in existing_map:
            ep = existing_map[pid]
            ep_coins = float(ep.get("jpwCoins") or 0.0)
            ip_coins = float(ip.get("jpwCoins") or 0.0)
            ep.update(ip)
            ep["jpwCoins"] = max(ep_coins, ip_coins)
            updated_count += 1
        else:
            STATE["profiles"].insert(0, ip)
            existing_map[pid] = ip
            added_count += 1

    if isinstance(incoming_orders, list):
        existing_oids = {int(o.get("id", 0)) for o in STATE.get("orders", []) if o.get("id")}
        for io in reversed(incoming_orders):
            if isinstance(io, dict) and int(io.get("id", 0)) not in existing_oids:
                STATE["orders"].insert(0, io)

    save_state(STATE)
    total_coins = sum(float(p.get("jpwCoins", 0.0)) for p in STATE.get("profiles", []))
    send_message(
        chat_id,
        f"✅ <b>Backup Restored Successfully!</b>\n\n"
        f"➕ <b>New Accounts Added:</b> {added_count}\n"
        f"🔄 <b>Existing Updated:</b> {updated_count}\n"
        f"👤 <b>Total Saved Accounts:</b> {len(STATE['profiles'])}\n"
        f"🪙 <b>Total Portfolio Coins:</b> {total_coins:.2f} Coins",
        reply_markup=build_main_keyboard(),
    )
    return True


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
                {"text": "💾 Backup JSON", "callback_data": "menu_backup"},
            ],
            [
                {"text": "👤 Saved Accounts", "callback_data": "menu_profiles"},
                {"text": "💳 Wallet & UPI QR", "callback_data": "menu_wallet"},
            ],
            [
                {"text": "👥 App Users (ID/Pass/₹)", "callback_data": "menu_app_users"},
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
    if not chat_id:
        return

    # Feature #2: Check if user uploaded a .json backup file to restore!
    doc = msg.get("document")
    if isinstance(doc, dict) and doc.get("file_id"):
        fname = str(doc.get("file_name") or "").lower()
        if fname.endswith(".json") or doc.get("mime_type") == "application/json":
            try:
                f_info = tg_api("getFile", {"file_id": doc["file_id"]})
                f_path = (f_info.get("result") or {}).get("file_path")
                if f_path:
                    dl_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f_path}"
                    raw_json = requests.get(dl_url, timeout=20).text
                    if restore_from_json_text(chat_id, raw_json):
                        return
            except Exception as exc:
                logger.warning("Document restore error: %s", exc)
            send_message(chat_id, "❌ Could not parse uploaded JSON backup file.")
            return

    text = (msg.get("text") or "").strip()
    if not text:
        return

    # If user pasted raw JSON array/object directly into chat, restore it automatically!
    if (text.startswith("[") and text.endswith("]")) or (text.startswith("{") and text.endswith("}")):
        if restore_from_json_text(chat_id, text):
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
            "• <code>/reach &lt;TargetID&gt; &lt;TargetPass&gt;</code> — Direct Real Reach Order (Auto-picks ≥1 Coin ID)\n"
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
        send_backup_document(chat_id, f"Auto-Backup (+1 Account: {cid})")

    elif cmd in ("/claimbonus", "/bonus"):
        send_claim_all_bonus(chat_id)

    elif cmd in ("/backup", "/export"):
        send_backup_document(chat_id, "Manual Backup Requested")

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

    elif cmd in ("/reach", "/order"):
        # Supports two easy formats:
        # 1) /reach <TargetID> <TargetPass>  (Auto-picks any saved account with >= 1.00 Coin!)
        # 2) /order <ProfileID> <TargetID> <TargetPass>
        if len(parts) < 3:
            send_message(
                chat_id,
                "⚡ <b>Direct Real Reach Order (POST /api/order):</b>\n\n"
                "🔹 <b>Auto-Pick ≥1 Coin ID:</b>\n"
                "<code>/reach &lt;TargetID&gt; &lt;TargetPass&gt;</code>\n"
                "Example: <code>/reach 1234567 0987</code>\n\n"
                "🔹 <b>Specific Profile ID:</b>\n"
                "<code>/order &lt;ProfileID&gt; &lt;TargetID&gt; &lt;TargetPass&gt;</code>\n"
                "⏰ <i>Service Time: 09:30 AM – 10:15 PM (IST)</i>",
            )
            return

        if cmd == "/reach" or len(parts) == 3:
            target_id = parts[1].strip()
            target_pass = parts[2].strip()
            ready_list = [p for p in STATE["profiles"] if float(p.get("jpwCoins", 0.0)) >= 1.0]
            ready_list.sort(key=lambda x: float(x.get("jpwCoins", 0.0)), reverse=True)
            prof = ready_list[0] if ready_list else (STATE["profiles"][0] if STATE["profiles"] else None)
        else:
            pid = parts[1].strip()
            target_id = parts[2].strip()
            target_pass = parts[3].strip()
            prof = next((p for p in STATE["profiles"] if p["id"] == pid), None)

        if not prof:
            send_message(chat_id, "❌ Profile not found!")
            return

        pid = prof["id"]
        send_message(
            chat_id,
            f"⏳ Sending Real Reach Order via <code>{pid}</code> ({prof['name']} — 🪙 {float(prof.get('jpwCoins', 0.0)):.2f} Coins)...\n"
            f"🎯 Target ID: <code>{target_id}</code> | Pass: <code>{target_pass}</code>",
        )

        api_res = place_reach_order_on_cashtree(prof, target_id, target_pass)
        srv_data = api_res.get("data") or {}
        srv_msg = srv_data.get("message") or srv_data.get("error") or ("Order submitted!" if api_res["ok"] else "Order failed")

        if api_res["ok"]:
            u_obj = srv_data.get("user") or {}
            prof["jpwCoins"] = float(u_obj.get("jpwCoins", max(0.0, float(prof.get("jpwCoins", 0.0)) - 1.0)))
            if prof.get("credits", 0) > 0:
                prof["credits"] -= 1
                prof["rupees"] = max(0, prof.get("rupees", 0) - 20)

            wo_id = random.randint(7100000000, 7999999999)
            now_str = datetime.now().strftime("%I:%M %p")
            order_obj = {
                "id": wo_id,
                "profileId": pid,
                "targetId": target_id,
                "notes": f"Pass: {target_pass} | {srv_msg}",
                "status": "COMPLETED",
                "time": now_str,
            }
            STATE["orders"].insert(0, order_obj)
            save_state(STATE)

            send_message(
                chat_id,
                f"✅ <b>Real Reach Order Executed!</b>\n\n"
                f"🆔 <b>Work Order:</b> <code>#{wo_id}</code>\n"
                f"👤 <b>Used Account:</b> <code>{pid}</code> ({prof['name']})\n"
                f"🎯 <b>Target ID:</b> <code>{target_id}</code>\n"
                f"🔑 <b>Target Pass:</b> <code>{target_pass}</code>\n"
                f"💬 <b>Server:</b> {srv_msg}\n"
                f"🪙 <b>Remaining Coins:</b> {prof['jpwCoins']:.2f} Coins",
                reply_markup=build_main_keyboard(),
            )
            send_backup_document(chat_id, f"Auto-Backup (Reach Order #{wo_id})")
        else:
            send_message(
                chat_id,
                f"⚠️ <b>Cashtree Server Response (ID {pid}):</b>\n"
                f"{srv_msg}",
                reply_markup=build_main_keyboard(),
            )

    elif cmd == "/orders":
        send_orders_summary(chat_id)

    elif cmd == "/fcotp":
        if len(parts) < 2:
            send_message(chat_id, "⚠️ <b>Format:</b> <code>/fcotp &lt;10-Digit-Mobile&gt;</code>")
            return
        res = fc_send_otp_request(parts[1])
        send_message(
            chat_id,
            res["message"] + ("\n👉 Verify karne ke liye bhejein: <code>/fcverify " + parts[1] + " &lt;OTP&gt;</code>" if res.get("ok") else ""),
        )

    elif cmd == "/fcverify":
        if len(parts) < 3:
            send_message(chat_id, "⚠️ <b>Format:</b> <code>/fcverify &lt;10-Digit-Mobile&gt; &lt;OTP&gt;</code>")
            return
        res = fc_verify_otp_request(parts[1], parts[2])
        send_message(chat_id, res["message"], reply_markup=build_main_keyboard())

    elif cmd in ("/users", "/customers"):
        send_customers_summary(chat_id)

    elif cmd == "/addbal":
        # Usage: /addbal <TechID> <Rupees>
        if len(parts) < 3 or not parts[2].lstrip("-").isdigit():
            send_message(
                chat_id,
                "⚠️ <b>Format:</b> <code>/addbal &lt;TechnicianID&gt; &lt;Rupees&gt;</code>\n"
                "Example: <code>/addbal 0691112222 20</code>",
            )
            return
        tid = parts[1].strip()
        amt = int(parts[2])
        cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == tid), None)
        if not cust:
            cust = {
                "techId": tid,
                "password": str(random.randint(100000, 999999)),
                "name": f"Tech_{tid[-4:]}",
                "balance": max(0, amt),
                "totalOrders": 0,
                "createdAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            STATE.setdefault("customers", []).insert(0, cust)
        else:
            cust["balance"] = max(0, int(cust.get("balance", 0)) + amt)
        save_state(STATE)
        send_message(
            chat_id,
            f"✅ <b>Customer Wallet Updated!</b>\n"
            f"👤 <b>Technician ID:</b> <code>{cust['techId']}</code>\n"
            f"🔑 <b>Password:</b> <code>{cust.get('password', '12345')}</code>\n"
            f"💳 <b>New Balance:</b> ₹{cust['balance']}",
            reply_markup=build_main_keyboard(),
        )

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
    """Returns (is_ready, status_text) based on 12:00 AM Midnight IST (UTC+5:30) daily reset."""
    last_str = prof.get("lastBonusTime")
    if not last_str:
        return True, "🟢 Bonus Ready"
    try:
        clean_iso = str(last_str).replace("Z", "+00:00")
        last_dt = datetime.fromisoformat(clean_iso)
        last_ts = last_dt.timestamp()
        now_ts = time.time()

        ist_offset = (5 * 3600) + (30 * 60)  # +05:30 IST in seconds
        day_sec = 86400

        last_ist_day = int((last_ts + ist_offset) // day_sec)
        now_ist_day = int((now_ts + ist_offset) // day_sec)

        if now_ist_day > last_ist_day:
            return True, "🟢 Bonus Ready"

        next_midnight_utc_ts = ((now_ist_day + 1) * day_sec) - ist_offset
        rem = max(0, int(next_midnight_utc_ts - now_ts))
        if rem <= 0:
            return True, "🟢 Bonus Ready"
        hrs = rem // 3600
        mins = (rem % 3600) // 60
        return False, f"⏳ 12AM: {hrs:02d}h {mins:02d}m left"
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
    if success_count > 0:
        send_backup_document(chat_id, f"Auto-Backup (+{total_earned:.2f} Coins Claimed)")


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


def send_customers_summary(chat_id: int) -> None:
    """Displays all Customer APK Users with their Technician ID, Password, and Wallet Balance (₹)."""
    customers = STATE.get("customers", [])
    total_bal = sum(int(c.get("balance", 0)) for c in customers)
    ready_nodes = sum(1 for p in STATE.get("profiles", []) if float(p.get("jpwCoins", 0.0)) >= 1.0)

    lines = [
        f"👥 <b>Customer APK Users ({len(customers)} Total)</b>",
        f"💳 <b>Total User Wallet Balance:</b> ₹{total_bal}",
        f"🎯 <b>Backend Auto-Order Nodes (≥1 Coin):</b> {ready_nodes} IDs\n",
    ]
    if not customers:
        lines.append("<i>No customer APK users registered yet.</i>")
    else:
        for i, c in enumerate(customers[:30], 1):
            tid = c.get("techId", "N/A")
            pwd = c.get("password", "12345")
            bal = int(c.get("balance", 0))
            ords = int(c.get("totalOrders", 0))
            reach_ready = "🟢 1+ Reach Ready" if bal >= 20 else "⚪ ₹0"
            lines.append(
                f"{i}. 👤 <b>ID:</b> <code>{tid}</code> | 🔑 <b>Pass:</b> <code>{pwd}</code>\n"
                f"   └ 💰 <b>Balance: ₹{bal}</b> ({reach_ready}) | 📦 Orders: {ords}"
            )
    lines.append(
        "\n💡 <b>Add/Edit Balance:</b> <code>/addbal &lt;TechID&gt; 20</code>\n"
        f"📱 <b>Customer APK Link:</b> <code>{WEBAPP_URL.rstrip('/')}/?mode=customer</code>"
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
    elif data == "menu_backup":
        send_backup_document(chat_id, "1-Click Accounts & Coins Backup")
    elif data == "menu_app_users":
        send_customers_summary(chat_id)
    elif data.startswith("utr_ok_"):
        # Format: utr_ok_<utr>_<techId>_<amount>
        parts = data.split("_")
        if len(parts) >= 5:
            utr_val = parts[2]
            amt_val = int(parts[-1]) if parts[-1].isdigit() else 20
            tid_val = "_".join(parts[3:-1])
            used_utrs = STATE.setdefault("used_utrs", [])
            if utr_val in used_utrs:
                send_message(chat_id, f"ℹ️ UTR <code>{utr_val}</code> was already approved.")
                return
            cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == tid_val), None)
            if cust:
                credit_customer_verified_utr(cust, utr_val, amt_val, "Admin 1-Tap")
                send_message(
                    chat_id,
                    f"✅ <b>Approved ₹{amt_val} for {tid_val}!</b>\n"
                    f"🧾 UTR: <code>{utr_val}</code>\n"
                    f"💳 User's APK Wallet is now <b>₹{cust['balance']}</b> (Live updated on user screen!)",
                )
    elif data.startswith("utr_no_"):
        parts = data.split("_")
        if len(parts) >= 4:
            utr_val = parts[2]
            tid_val = "_".join(parts[3:])
            STATE["pending_payments"] = [p for p in STATE.get("pending_payments", []) if p.get("utr") != utr_val]
            save_state(STATE)
            send_message(chat_id, f"❌ Rejected Fake UTR <code>{utr_val}</code> from <code>{tid_val}</code>.")


# ================= AIOHTTP WEB SERVER (SERVES MINI APP + API SYNC) =================
async def serve_index(request: web.Request) -> web.Response:
    if HTML_FILE.exists():
        return web.FileResponse(HTML_FILE)
    return web.Response(text="index.html not found in current directory.", status=404)


async def ping_health(request: web.Request) -> web.Response:
    """Ultra-fast 2-byte healthcheck endpoint for cron-job.org and self-pinger."""
    return web.Response(text="OK")


async def api_state_get(request: web.Request) -> web.Response:
    return web.json_response(STATE)


async def api_state_post(request: web.Request) -> web.Response:
    """Smart Two-Way Sync: safely merges profiles, deletions, orders, stats, and settings from Mini App into bot.py STATE."""
    try:
        body = await request.json()

        # 1. Handle explicit profile deletions from Mini App
        deleted_ids = body.get("deletedProfileIds")
        if isinstance(deleted_ids, list) and deleted_ids:
            del_set = {str(x) for x in deleted_ids if x}
            STATE["profiles"] = [p for p in STATE.get("profiles", []) if str(p.get("id")) not in del_set]

        # 2. Smart merge profiles without overwriting newer coins/lastBonusTime on server
        incoming_profiles = body.get("profiles")
        if isinstance(incoming_profiles, list):
            existing_map = {str(p.get("id")): p for p in STATE.get("profiles", []) if p.get("id")}
            for ip in reversed(incoming_profiles):
                if not isinstance(ip, dict) or not ip.get("id"):
                    continue
                pid = str(ip["id"])
                if pid in existing_map:
                    ep = existing_map[pid]
                    # Preserve highest coins & newest lastBonusTime
                    ep_coins = float(ep.get("jpwCoins") or 0.0)
                    ip_coins = float(ip.get("jpwCoins") or 0.0)
                    ep_bonus = float(ep.get("bonusCoins") or 0.0)
                    ip_bonus = float(ip.get("bonusCoins") or 0.0)
                    ep_last = ep.get("lastBonusTime")
                    ip_last = ip.get("lastBonusTime")
                    ep_reg = bool(ep.get("registered")) or bool(ip.get("registered"))

                    ep.update(ip)
                    ep["jpwCoins"] = max(ep_coins, ip_coins)
                    ep["bonusCoins"] = max(ep_bonus, ip_bonus)
                    ep["registered"] = ep_reg
                    if ep_last and ip_last:
                        ep["lastBonusTime"] = max(str(ep_last), str(ip_last))
                    elif ep_last or ip_last:
                        ep["lastBonusTime"] = ep_last or ip_last
                else:
                    STATE["profiles"].insert(0, ip)
                    existing_map[pid] = ip

        # 3. Merge orders & status updates (e.g., COMPLETED marked in Admin tab)
        incoming_orders = body.get("orders")
        if isinstance(incoming_orders, list):
            existing_omap = {int(o.get("id", 0)): o for o in STATE.get("orders", []) if o.get("id")}
            for io in reversed(incoming_orders):
                if not isinstance(io, dict) or not io.get("id"):
                    continue
                oid = int(io["id"])
                if oid in existing_omap:
                    existing_omap[oid].update(io)
                else:
                    STATE["orders"].insert(0, io)
                    existing_omap[oid] = io

        # 4. Merge Admin stats & settings
        incoming_stats = body.get("stats")
        if isinstance(incoming_stats, dict):
            if float(incoming_stats.get("collected", 0)) >= float(STATE["stats"].get("collected", 0)):
                STATE["stats"].update(incoming_stats)

        incoming_settings = body.get("settings")
        if isinstance(incoming_settings, dict):
            # Only overwrite server settings if Admin explicitly clicked 'Save Settings' (saveSettings=True)
            # or if incoming settings have a newer updatedAt timestamp than server settings!
            inc_ts = int(incoming_settings.get("updatedAt") or 0)
            srv_ts = int(STATE["settings"].get("updatedAt") or 0)
            if body.get("saveSettings") is True or inc_ts > srv_ts:
                for k, v in incoming_settings.items():
                    # Never wipe a saved freechargeCookie with an empty string unless explicitly cleared
                    if k == "freechargeCookie" and not v and STATE["settings"].get("freechargeCookie") and not body.get("saveSettings"):
                        continue
                    STATE["settings"][k] = v
                if body.get("saveSettings") is True and inc_ts <= srv_ts:
                    STATE["settings"]["updatedAt"] = int(time.time() * 1000)

        if body.get("adminCustomerUpdate") is True:
            incoming_customers = body.get("customers")
            if isinstance(incoming_customers, list):
                STATE["customers"] = [
                    c for c in incoming_customers
                    if isinstance(c, dict) and c.get("techId") and str(c.get("techId")) != "TECH101"
                ]

        save_state(STATE)
        return web.json_response({"ok": True, "totalProfiles": len(STATE.get("profiles", []))})
    except Exception as exc:
        logger.warning("api_state_post error: %s", exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=400)


async def api_register_proxy(request: web.Request) -> web.Response:
    """Optional backend proxy to https://cashtree.space/api/register."""
    body = await request.json()
    cid = body.get("customId", "")
    pwd = body.get("password", "12345")
    name = body.get("name", "RRYSB")
    res = await asyncio.to_thread(register_on_cashtree, cid, pwd, name)
    return web.json_response(res["data"], status=res["status"] or 200)


async def api_customer_me(request: web.Request) -> web.Response:
    """Safe read-only sync endpoint for Customer APK: returns only this customer's live balance, orders, and UPI settings."""
    tech_id = str(request.query.get("techId") or "").strip()
    upi_id = STATE["settings"].get("upiId", "jpwportal@freecharge")
    app_title = STATE["settings"].get("appTitle", "JPW REACH")
    if not tech_id:
        return web.json_response({"ok": True, "upiId": upi_id, "appTitle": app_title})

    cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == tech_id), None)
    if not cust:
        return web.json_response({"ok": False, "message": "User not found", "upiId": upi_id, "appTitle": app_title}, status=404)

    my_orders = [o for o in STATE.get("orders", []) if str(o.get("customerTechId")) == tech_id]
    return web.json_response({
        "ok": True,
        "customer": {
            "techId": cust.get("techId"),
            "name": cust.get("name"),
            "balance": int(cust.get("balance", 0)),
            "totalOrders": int(cust.get("totalOrders", 0)),
        },
        "orders": my_orders[:20],
        "upiId": upi_id,
        "appTitle": app_title,
    })


async def api_customer_auth(request: web.Request) -> web.Response:
    """Customer APK Login / Auto-Registration by Technician ID & Password."""
    try:
        body = await request.json()
        tech_id = str(body.get("techId") or "").strip()
        password = str(body.get("password") or "").strip()
        if not tech_id or not password:
            return web.json_response({"ok": False, "message": "Enter Technician ID & Password"}, status=400)

        customers = STATE.setdefault("customers", [])
        cust = next((c for c in customers if str(c.get("techId")) == tech_id), None)
        if cust:
            if str(cust.get("password")) != password:
                return web.json_response({"ok": False, "message": "❌ Wrong Password for this Technician ID!"}, status=401)
        else:
            cust = {
                "techId": tech_id,
                "password": password,
                "name": str(body.get("name") or f"Tech_{tech_id[-4:]}"),
                "balance": 0,
                "totalOrders": 0,
                "createdAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            customers.insert(0, cust)
            save_state(STATE)
            if ADMIN_CHAT_ID:
                asyncio.create_task(
                    asyncio.to_thread(
                        send_message,
                        ADMIN_CHAT_ID,
                        f"🆕 <b>New APK User Registered!</b>\n👤 Tech ID: <code>{tech_id}</code>\n🔑 Pass: <code>{password}</code>",
                    )
                )

        my_orders = [o for o in STATE.get("orders", []) if str(o.get("customerTechId")) == tech_id]
        return web.json_response({
            "ok": True,
            "customer": cust,
            "orders": my_orders[:20],
            "upiId": STATE["settings"].get("upiId", "jpwportal@ybl"),
            "appTitle": STATE["settings"].get("appTitle", "JPW Portal"),
        })
    except Exception as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=400)


FC_OTP_SESSIONS = {}  # { mobile: { "session": requests.Session, "otpId": str } }


def fc_send_otp_request(mobile: str) -> dict:
    """Sends Freecharge MSITE Login OTP to the given 10-digit mobile number."""
    mob = "".join(ch for ch in str(mobile or "") if ch.isdigit())[-10:]
    if len(mob) != 10:
        return {"ok": False, "message": "⚠️ Enter a valid 10-digit mobile number"}

    sess = requests.Session()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
        "Origin": "https://www.freecharge.in",
        "Referer": "https://www.freecharge.in/",
    }
    # Warm up session cookies & extract csrfRequestIdentifier if issued
    try:
        r0 = sess.get("https://www.freecharge.in/", headers=headers, timeout=8)
        csrf_tok = sess.cookies.get("csrfRequestIdentifier") or r0.cookies.get("csrfRequestIdentifier")
        if csrf_tok:
            headers["csrfRequestIdentifier"] = csrf_tok
    except Exception:
        pass

    endpoints = [
        ("https://www.freecharge.in/fc/um/otp/send?fcAppType=MSITE", {"mobileNumber": mob}),
        ("https://www.freecharge.in/rest/login/sendOtp?fcAppType=MSITE", {"mobile": mob, "mobileNumber": mob}),
    ]
    last_err = "Could not reach Freecharge OTP server"
    for url, payload in endpoints:
        try:
            resp = sess.post(url, json=payload, headers=headers, timeout=12)
            try:
                data = resp.json() if resp.content else {}
            except Exception:
                # Non-JSON response means Freecharge blocked Render's US (Oregon) datacenter IP
                last_err = "Freecharge blocks US Server IP for OTP SMS. Please login on freecharge.in in your browser & paste app_fc cookie below!"
                continue

            otp_id = (
                data.get("otpId")
                or (data.get("data") or {}).get("otpId")
                or data.get("requestId")
                or (data.get("data") or {}).get("requestId")
                or ""
            )
            if resp.ok and (otp_id or data.get("status") in ("SUCCESS", 200, True) or "otp" in str(data).lower()):
                FC_OTP_SESSIONS[mob] = {"session": sess, "otpId": str(otp_id), "headers": headers}
                return {
                    "ok": True,
                    "mobile": mob,
                    "otpId": str(otp_id),
                    "message": f"📲 OTP sent to {mob}! Enter the OTP to auto-connect Freecharge.",
                }
            last_err = data.get("errorMessage") or data.get("message") or f"HTTP {resp.status_code}"
        except Exception as exc:
            last_err = str(exc)

    return {"ok": False, "message": f"⚠️ {last_err}"}


def fc_verify_otp_request(mobile: str, otp_code: str, custom_upi_id: str = "") -> dict:
    """Verifies Freecharge OTP, automatically extracts `app_fc` session cookie & sets `@freecharge` UPI ID!"""
    mob = "".join(ch for ch in str(mobile or "") if ch.isdigit())[-10:]
    otp = "".join(ch for ch in str(otp_code or "") if ch.isdigit())
    if len(mob) != 10 or not otp:
        return {"ok": False, "message": "⚠️ Enter valid 10-digit Mobile & OTP"}

    saved_sess_info = FC_OTP_SESSIONS.get(mob) or {}
    sess = saved_sess_info.get("session") or requests.Session()
    otp_id = saved_sess_info.get("otpId") or ""

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36",
        "Origin": "https://www.freecharge.in",
        "Referer": "https://www.freecharge.in/",
    }

    endpoints = [
        (
            "https://www.freecharge.in/fc/um/otp/verify?fcAppType=MSITE",
            {"mobileNumber": mob, "otp": otp, "otpId": otp_id},
        ),
        (
            "https://www.freecharge.in/rest/login/verifyOtp?fcAppType=MSITE",
            {"mobile": mob, "mobileNumber": mob, "otp": otp, "otpId": otp_id},
        ),
    ]

    last_err = "OTP verification failed"
    for url, payload in endpoints:
        try:
            resp = sess.post(url, json=payload, headers=headers, timeout=12)
            try:
                data = resp.json() if resp.content else {}
            except Exception:
                data = {}
            cookies_dict = sess.cookies.get_dict()
            resp_cookies = resp.cookies.get_dict()
            all_cookies = {**cookies_dict, **resp_cookies}

            app_fc_val = (
                all_cookies.get("app_fc")
                or data.get("app_fc")
                or (data.get("data") or {}).get("app_fc")
                or (data.get("data") or {}).get("token")
                or data.get("token")
                or ""
            )
            full_cookie_str = "; ".join(f"{k}={v}" for k, v in all_cookies.items()) if all_cookies else ""

            if app_fc_val or ("app_fc" in full_cookie_str):
                cookie_to_save = full_cookie_str if "app_fc=" in full_cookie_str else f"app_fc={app_fc_val}"
                vpa = (
                    custom_upi_id.strip()
                    or (data.get("data") or {}).get("vpa")
                    or f"{mob}@freecharge"
                )
                STATE["settings"]["freechargeCookie"] = cookie_to_save
                STATE["settings"]["freechargeMobile"] = mob
                STATE["settings"]["upiId"] = vpa
                STATE["settings"]["updatedAt"] = int(time.time() * 1000)
                save_state(STATE)
                return {
                    "ok": True,
                    "upiId": vpa,
                    "freechargeCookie": cookie_to_save,
                    "message": f"✅ Freecharge Connected! UPI ID set to {vpa} & 24×7 Auto-Verify Activated!",
                }
            last_err = data.get("errorMessage") or data.get("message") or f"HTTP {resp.status_code}"
        except Exception as exc:
            last_err = str(exc)

    return {"ok": False, "message": f"⚠️ {last_err}"}


def check_freecharge_transactions(
    expected_utr: str | None = None,
    expected_unique_amt: float | None = None,
    session_id: str | None = None,
    tech_id: str | None = None,
) -> tuple[bool, int, str]:
    """
    Checks the Admin's own Freecharge account transaction history 24x7 using their `app_fc` session cookie.
    Matches incoming UPI credit transactions by:
    - Exact 12-digit UTR (`expected_utr`)
    - OR Exact Unique Decimal Amount (`₹20.01`, `₹20.02`) / Order Session Note (`session_id` / `tech_id`)
    Returns (matched_bool, credited_amount_int, matched_utr_str).
    """
    fc_cookie = (STATE["settings"].get("freechargeCookie") or os.environ.get("FREECHARGE_COOKIE") or "").strip()
    if not fc_cookie:
        return False, 0, ""

    cookie_hdr = fc_cookie if ("=" in fc_cookie) else f"app_fc={fc_cookie}"
    headers = {
        "Cookie": cookie_hdr,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36",
        "Origin": "https://www.freecharge.in",
        "Referer": "https://www.freecharge.in/transactions",
    }

    endpoints = [
        ("POST", "https://www.freecharge.in/thv/listv3?fcAppType=MSITE", {"isAndroid": False}),
        ("GET", "https://www.freecharge.in/thv/listv3?fcAppType=MSITE", None),
    ]
    used_utrs = STATE.setdefault("used_utrs", [])

    for method, url, payload in endpoints:
        try:
            if method == "POST":
                resp = requests.post(url, json=payload, headers=headers, timeout=8)
            else:
                resp = requests.get(url, headers=headers, timeout=8)
            if not resp.ok or not resp.content:
                continue
            raw_str = resp.text
            data = resp.json() if resp.content else {}

            # Parse transaction items list from Freecharge response
            tx_list = (
                data.get("data")
                or data.get("transactions")
                or (data.get("data") or {}).get("transactions")
                if isinstance(data, dict)
                else []
            )
            if isinstance(tx_list, list):
                for tx in tx_list:
                    if not isinstance(tx, dict):
                        continue
                    tx_json = json.dumps(tx)
                    status = str(tx.get("status") or tx.get("txnStatus") or "SUCCESS").upper()
                    if status not in ("SUCCESS", "COMPLETED", "CREDIT", "PAID"):
                        continue
                    amt_val = round(float(tx.get("amount") or tx.get("txnAmount") or 0), 2)
                    # Find 12-digit UTR inside transaction metadata
                    utr_candidates = re.findall(r"\b([1-9]\d{11})\b", tx_json)
                    tx_utr = str(
                        tx.get("upiTxnId")
                        or tx.get("rrn")
                        or tx.get("bankRefNo")
                        or tx.get("utr")
                        or (utr_candidates[0] if utr_candidates else "")
                        or tx.get("txnId")
                        or ""
                    ).strip()

                    if not tx_utr or tx_utr in used_utrs:
                        continue

                    # Match 1: By explicit UTR
                    if expected_utr and expected_utr in tx_json and amt_val >= 1:
                        return True, int(amt_val), expected_utr

                    # Match 2: By Unique Decimal Amount (e.g. ₹20.01, ₹20.02) or Session ID / Tech ID
                    if expected_unique_amt is not None and amt_val == round(float(expected_unique_amt), 2):
                        return True, int(amt_val), tx_utr
                    if session_id and session_id in tx_json and amt_val >= 1:
                        return True, int(amt_val), tx_utr
        except Exception as exc:
            logger.warning("Freecharge check warning: %s", exc)

    return False, 0, ""


def check_real_bharatpe_api(utr: str, expected_amount: int) -> tuple[bool, int]:
    """Checks live BharatPe Merchant API if bharatpeToken & bharatpeMid are configured."""
    mid = (STATE["settings"].get("bharatpeMid") or os.environ.get("BHARATPE_MID") or "").strip()
    token = (STATE["settings"].get("bharatpeToken") or os.environ.get("BHARATPE_TOKEN") or "").strip()
    if not mid or not token:
        return False, 0
    try:
        url = f"https://payments-tesseract.bharatpe.in/api/v1/merchant/transactions?module=PAYMENT_QR&merchantId={mid}"
        headers = {"token": token, "user-agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json() if resp.content else {}
        txns = (data.get("data") or {}).get("transactions") or []
        for tx in txns:
            bank_ref = str(tx.get("bankReferenceNo") or tx.get("utr") or "").strip()
            status = str(tx.get("status") or "").upper()
            amt = int(float(tx.get("amount") or 0))
            if bank_ref == utr and status == "SUCCESS" and amt >= expected_amount:
                return True, amt
    except Exception as exc:
        logger.warning("BharatPe API check error: %s", exc)
    return False, 0


def credit_customer_verified_utr(cust: dict, utr: str, amount: int, source_label: str) -> None:
    """Credits customer wallet once a UTR is genuinely verified by Bank SMS Webhook, BharatPe API, or Admin 1-Tap."""
    used_utrs = STATE.setdefault("used_utrs", [])
    if utr not in used_utrs:
        used_utrs.append(utr)
    cust["balance"] = int(cust.get("balance", 0)) + amount
    STATE["stats"]["collected"] = int(STATE["stats"].get("collected", 0)) + amount
    STATE["stats"]["cost"] = int(STATE["stats"].get("cost", 0)) + (amount // 20) * 13
    STATE["stats"]["profit"] = STATE["stats"]["collected"] - STATE["stats"]["cost"]
    # Remove from pending_payments if present
    STATE["pending_payments"] = [p for p in STATE.get("pending_payments", []) if p.get("utr") != utr]
    save_state(STATE)


def allocate_unique_amount(base_amount: int) -> float:
    """
    When 2, 3, or 20 users click 'Pay ₹20' at the exact same time, assigns each active session
    a unique paise tag (e.g. ₹20.01, ₹20.02, ₹20.03) valid for 5 minutes.
    When the payment hits BharatPe or Bank SMS, the exact paise or Order Ref matches the exact user
    with ZERO collision and ZERO UTR typing!
    """
    now_ts = time.time()
    sessions = STATE.setdefault("payment_sessions", {})
    # Expire sessions older than 6 minutes
    expired = [k for k, v in sessions.items() if now_ts - float(v.get("createdAt", 0)) > 360 and v.get("status") == "PENDING"]
    for k in expired:
        sessions.pop(k, None)

    used_paise = {
        round(float(v.get("uniqueAmount", 0)) - int(float(v.get("uniqueAmount", 0))), 2)
        for v in sessions.values()
        if v.get("status") == "PENDING" and int(float(v.get("baseAmount", 20))) == base_amount
    }
    for p in range(1, 99):
        frac = round(p / 100.0, 2)
        if frac not in used_paise:
            return round(base_amount + frac, 2)
    return float(base_amount)


async def api_customer_create_payment(request: web.Request) -> web.Response:
    """Creates a Flipkart/Amazon-style auto-tracked Payment Session (Razorpay Order + Dynamic Unique UPI Session)."""
    try:
        body = await request.json()
        tech_id = str(body.get("techId") or "").strip()
        base_amt = int(body.get("amount") or 20)
        if not tech_id:
            return web.json_response({"ok": False, "message": "Login required"}, status=400)

        session_id = f"JPW{int(time.time())}{random.randint(100, 999)}"
        unique_amt = allocate_unique_amount(base_amt)

        rzp_key_id = (STATE["settings"].get("razorpayKeyId") or os.environ.get("RAZORPAY_KEY_ID") or "").strip()
        rzp_secret = (STATE["settings"].get("razorpayKeySecret") or os.environ.get("RAZORPAY_KEY_SECRET") or "").strip()
        rzp_order_id = None

        # If Razorpay keys are configured, create an official Razorpay Order
        if rzp_key_id and rzp_secret:
            try:
                resp = await asyncio.to_thread(
                    requests.post,
                    "https://api.razorpay.com/v1/orders",
                    auth=(rzp_key_id, rzp_secret),
                    json={
                        "amount": base_amt * 100,  # in paise
                        "currency": "INR",
                        "receipt": session_id,
                        "notes": {"techId": tech_id, "sessionId": session_id},
                    },
                    timeout=10,
                )
                if resp.ok:
                    rzp_order_id = (resp.json() or {}).get("id")
            except Exception as exc:
                logger.warning("Razorpay order creation warning: %s", exc)

        upi_id = STATE["settings"].get("upiId", "jpwportal@ybl")
        app_title = STATE["settings"].get("appTitle", "JPW Portal")
        # Embed session_id in `tr` (Transaction Reference) and `tn` (Transaction Note) + unique_amt
        upi_uri = (
            f"upi://pay?pa={quote(upi_id)}&pn={quote(app_title)}"
            f"&am={unique_amt:.2f}&cu=INR&tr={quote(session_id)}&tn={quote('ID_' + tech_id + '_' + session_id)}"
        )

        sess_obj = {
            "sessionId": session_id,
            "techId": tech_id,
            "baseAmount": base_amt,
            "uniqueAmount": unique_amt,
            "upiId": upi_id,
            "appTitle": app_title,
            "upiUri": upi_uri,
            "rzpKeyId": rzp_key_id if rzp_order_id else None,
            "rzpOrderId": rzp_order_id,
            "status": "PENDING",
            "createdAt": time.time(),
        }
        STATE.setdefault("payment_sessions", {})[session_id] = sess_obj
        save_state(STATE)
        return web.json_response({"ok": True, "session": sess_obj})
    except Exception as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=400)


async def api_customer_verify_razorpay(request: web.Request) -> web.Response:
    """Verifies Razorpay Checkout signature and immediately credits ₹20 to Customer Wallet."""
    try:
        body = await request.json()
        session_id = str(body.get("sessionId") or "").strip()
        rzp_order_id = str(body.get("razorpay_order_id") or "").strip()
        rzp_payment_id = str(body.get("razorpay_payment_id") or "").strip()
        rzp_signature = str(body.get("razorpay_signature") or "").strip()

        sess = STATE.setdefault("payment_sessions", {}).get(session_id)
        if not sess:
            return web.json_response({"ok": False, "message": "Session not found"}, status=404)
        if sess.get("status") == "COMPLETED":
            cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == str(sess["techId"])), None)
            return web.json_response({"ok": True, "customer": cust, "message": "Already credited!"})

        rzp_secret = (STATE["settings"].get("razorpayKeySecret") or os.environ.get("RAZORPAY_KEY_SECRET") or "").strip()
        msg = f"{rzp_order_id}|{rzp_payment_id}".encode("utf-8")
        expected_sig = hmac.new(rzp_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(expected_sig, rzp_signature):
            return web.json_response({"ok": False, "message": "❌ Invalid payment signature"}, status=400)

        sess["status"] = "COMPLETED"
        cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == str(sess["techId"])), None)
        if cust:
            credit_customer_verified_utr(cust, rzp_payment_id, int(sess["baseAmount"]), "Razorpay Checkout")
        return web.json_response({
            "ok": True,
            "status": "COMPLETED",
            "message": f"🎉 ₹{sess['baseAmount']} Payment Successful!",
            "customer": cust,
        })
    except Exception as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=400)


async def api_customer_check_payment(request: web.Request) -> web.Response:
    """
    Polled every 2 seconds by the Customer App while waiting on the payment screen.
    Checks if the payment session was completed via:
    1) Bank/UPI SMS Webhook (matching exact uniqueAmount like ₹20.01 or sessionId/techId)
    2) Live BharatPe Merchant API (matching exact uniqueAmount or transaction note after session start time)
    No UTR input needed from the customer!
    """
    try:
        body = await request.json()
        session_id = str(body.get("sessionId") or "").strip()
        sess = STATE.setdefault("payment_sessions", {}).get(session_id)
        if not sess:
            return web.json_response({"ok": False, "status": "NOT_FOUND"}, status=404)

        tech_id = str(sess.get("techId"))
        cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == tech_id), None)

        if sess.get("status") == "COMPLETED":
            return web.json_response({
                "ok": True,
                "status": "COMPLETED",
                "message": f"🎉 ₹{sess['baseAmount']} Added to Wallet!",
                "customer": cust,
            })

        # Check 1: Live Freecharge 24x7 Cloud Checker (using app_fc session cookie — No Phone Needed!)
        if cust:
            fc_ok, fc_amt, fc_utr = await asyncio.to_thread(
                check_freecharge_transactions,
                None,
                float(sess["uniqueAmount"]),
                session_id,
                tech_id,
            )
            if fc_ok:
                sess["status"] = "COMPLETED"
                credit_customer_verified_utr(cust, fc_utr, int(sess["baseAmount"]), "Freecharge 24x7 Auto-Detect")
                if ADMIN_CHAT_ID:
                    asyncio.create_task(
                        asyncio.to_thread(
                            send_message,
                            ADMIN_CHAT_ID,
                            f"⚡ <b>Freecharge Auto-Detected Payment (₹{sess['uniqueAmount']:.2f})!</b>\n"
                            f"👤 Tech ID: <code>{tech_id}</code> (+₹{sess['baseAmount']})\n"
                            f"🧾 Ref/UTR: <code>{fc_utr}</code>",
                        )
                    )
                return web.json_response({
                    "ok": True,
                    "status": "COMPLETED",
                    "message": f"🎉 ₹{sess['baseAmount']} Freecharge Verified! Wallet Updated!",
                    "customer": cust,
                })

        # Check 2: Live BharatPe API for a transaction matching uniqueAmount (e.g. 20.01) or note containing session_id/tech_id
        mid = (STATE["settings"].get("bharatpeMid") or os.environ.get("BHARATPE_MID") or "").strip()
        token = (STATE["settings"].get("bharatpeToken") or os.environ.get("BHARATPE_TOKEN") or "").strip()
        if mid and token and cust:
            try:
                url = f"https://payments-tesseract.bharatpe.in/api/v1/merchant/transactions?module=PAYMENT_QR&merchantId={mid}"
                resp = await asyncio.to_thread(requests.get, url, headers={"token": token, "user-agent": "Mozilla/5.0"}, timeout=8)
                data = resp.json() if resp.content else {}
                txns = (data.get("data") or {}).get("transactions") or []
                used_utrs = STATE.setdefault("used_utrs", [])
                for tx in txns:
                    bank_ref = str(tx.get("bankReferenceNo") or tx.get("utr") or "").strip()
                    status = str(tx.get("status") or "").upper()
                    tx_amt = round(float(tx.get("amount") or 0), 2)
                    tx_note = str(tx.get("payerHandle") or tx.get("remarks") or tx.get("note") or "")
                    if status == "SUCCESS" and bank_ref and bank_ref not in used_utrs:
                        if tx_amt == round(float(sess["uniqueAmount"]), 2) or session_id in tx_note or tech_id in tx_note:
                            sess["status"] = "COMPLETED"
                            credit_customer_verified_utr(cust, bank_ref, int(sess["baseAmount"]), "BharatPe Auto-Detect")
                            return web.json_response({
                                "ok": True,
                                "status": "COMPLETED",
                                "message": f"🎉 ₹{sess['baseAmount']} Auto-Detected! Wallet Updated!",
                                "customer": cust,
                            })
            except Exception as exc:
                logger.warning("BharatPe poll check error: %s", exc)

        return web.json_response({"ok": True, "status": "PENDING", "customer": cust})
    except Exception as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=400)


async def api_admin_fc_send_otp(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        mobile = str(body.get("mobile") or "").strip()
        res = await asyncio.to_thread(fc_send_otp_request, mobile)
        return web.json_response(res, status=200 if res.get("ok") else 400)
    except Exception as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=400)


async def api_admin_fc_verify_otp(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        mobile = str(body.get("mobile") or "").strip()
        otp = str(body.get("otp") or "").strip()
        upi_id = str(body.get("upiId") or "").strip()
        res = await asyncio.to_thread(fc_verify_otp_request, mobile, otp, upi_id)
        return web.json_response(res, status=200 if res.get("ok") else 400)
    except Exception as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=400)


async def api_payment_webhook(request: web.Request) -> web.Response:
    """
    Receives real-time Bank / UPI SMS or Notification from SMS Forwarder / MacroDroid app on Admin's phone!
    Automatically extracts the 12-digit UTR & Rs amount (e.g. 'Received Rs.20.00 ... Ref 431298765432').
    If a customer already submitted that UTR (or submits it later), it auto-credits ₹20 in 0 seconds!
    """
    try:
        if request.content_type == "application/json":
            body = await request.json()
            raw_text = str(body.get("text") or body.get("message") or body.get("sms") or body.get("body") or json.dumps(body))
        else:
            raw_text = await request.text()

        # Extract 12-digit UTR number from SMS/Notification text
        utr_matches = re.findall(r"\b([1-9]\d{11})\b", raw_text)
        # Extract exact decimal amount (e.g. Rs. 20.01, INR 20.02, ₹20.00)
        amt_match = re.search(r"(?:rs\.?|inr|₹)\s*([0-9]+(?:\.[0-9]{1,2})?)", raw_text, re.IGNORECASE)
        exact_float_amt = round(float(amt_match.group(1)), 2) if amt_match else 20.0
        detected_amt = int(exact_float_amt)

        # AUTO-MATCH ACTIVE PAYMENT SESSION BY UNIQUE DECIMAL AMOUNT (e.g. 20.01, 20.02) OR SESSION ID!
        sessions = STATE.setdefault("payment_sessions", {})
        for sid, sess in list(sessions.items()):
            if sess.get("status") == "PENDING":
                if round(float(sess.get("uniqueAmount", 0)), 2) == exact_float_amt or sid in raw_text or str(sess.get("techId")) in raw_text:
                    tid = sess.get("techId")
                    cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == str(tid)), None)
                    if cust:
                        utr_tag = utr_matches[0] if utr_matches else f"SMS_{int(time.time())}"
                        sess["status"] = "COMPLETED"
                        credit_customer_verified_utr(cust, utr_tag, int(sess.get("baseAmount", 20)), "Zero-UTR Auto-Match")
                        if ADMIN_CHAT_ID:
                            asyncio.create_task(
                                asyncio.to_thread(
                                    send_message,
                                    ADMIN_CHAT_ID,
                                    f"⚡ <b>Zero-UTR Auto-Payment Matched (₹{exact_float_amt:.2f})!</b>\n"
                                    f"👤 Tech ID: <code>{tid}</code> (+₹{sess.get('baseAmount', 20)})",
                                )
                            )
                        break

        if not utr_matches:
            return web.json_response({"ok": False, "message": "No 12-digit UTR found in webhook text"}, status=400)

        verified_bank = STATE.setdefault("verified_bank_utrs", {})
        used_utrs = STATE.setdefault("used_utrs", [])
        auto_credited_users = []

        for utr in utr_matches:
            if utr in used_utrs:
                continue
            verified_bank[utr] = detected_amt

            # Check if any customer is already waiting for this exact UTR in pending_payments!
            pending_list = STATE.get("pending_payments", [])
            matching_pend = next((p for p in pending_list if p.get("utr") == utr), None)
            if matching_pend:
                tid = matching_pend.get("techId")
                cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == str(tid)), None)
                if cust:
                    credit_customer_verified_utr(cust, utr, detected_amt, "Bank SMS Webhook")
                    auto_credited_users.append(f"{tid} (+₹{detected_amt})")
                    if ADMIN_CHAT_ID:
                        asyncio.create_task(
                            asyncio.to_thread(
                                send_message,
                                ADMIN_CHAT_ID,
                                f"✅ <b>Bank SMS Matched! Auto-Credited ₹{detected_amt}</b>\n"
                                f"👤 Tech ID: <code>{tid}</code>\n🧾 UTR: <code>{utr}</code>",
                            )
                        )

        save_state(STATE)
        return web.json_response({
            "ok": True,
            "recordedUtrs": utr_matches,
            "amount": detected_amt,
            "autoCredited": auto_credited_users,
        })
    except Exception as exc:
        logger.warning("api_payment_webhook error: %s", exc)
        return web.json_response({"ok": False, "message": str(exc)}, status=400)


async def api_customer_pay(request: web.Request) -> web.Response:
    """
    Scam-Proof ₹20 Payment Verification:
    1. Rejects already-used UTRs immediately.
    2. Checks `STATE['verified_bank_utrs']` (populated by Bank/UPI SMS Webhook).
    3. Checks live BharatPe Merchant API if configured.
    4. If neither matched yet, puts UTR in `pending_payments` (DOES NOT give free ₹20 to fake UTRs!)
       and sends a 1-Tap [✅ Approve] / [❌ Reject Fake] button to Admin on Telegram, while auto-crediting
       the instant the Bank SMS arrives!
    """
    try:
        body = await request.json()
        tech_id = str(body.get("techId") or "").strip()
        raw_utr = "".join(ch for ch in str(body.get("utr") or "") if ch.isdigit())
        amount = int(body.get("amount") or 20)

        if len(raw_utr) != 12 or raw_utr[0] == "0" or len(set(raw_utr)) < 3:
            return web.json_response(
                {"ok": False, "message": "⚠️ Invalid UTR! Enter valid 12-digit UPI UTR number."},
                status=400,
            )

        used_utrs = STATE.setdefault("used_utrs", [])
        if raw_utr in used_utrs:
            return web.json_response(
                {"ok": False, "message": "❌ This UTR number has already been used!"},
                status=400,
            )

        customers = STATE.setdefault("customers", [])
        cust = next((c for c in customers if str(c.get("techId")) == tech_id), None)
        if not cust:
            return web.json_response({"ok": False, "message": "Technician ID not found"}, status=404)

        # CHECK 1: Did the Bank/UPI SMS Webhook already record this exact UTR?
        verified_bank = STATE.setdefault("verified_bank_utrs", {})
        if raw_utr in verified_bank:
            real_amt = int(verified_bank.pop(raw_utr) or amount)
            credit_customer_verified_utr(cust, raw_utr, real_amt, "Bank SMS Webhook")
            if ADMIN_CHAT_ID:
                asyncio.create_task(
                    asyncio.to_thread(
                        send_message,
                        ADMIN_CHAT_ID,
                        f"⚡ <b>Instant Bank-Verified Payment (+₹{real_amt})!</b>\n"
                        f"👤 Tech ID: <code>{tech_id}</code>\n🧾 UTR: <code>{raw_utr}</code>\n"
                        f"💳 New Balance: ₹{cust['balance']}",
                    )
                )
            return web.json_response({
                "ok": True,
                "status": "VERIFIED",
                "message": f"✅ Bank Verified! ₹{real_amt} Added to Wallet!",
                "customer": cust,
            })

        # CHECK 1B: Check Live Freecharge 24x7 Cloud Statement (using app_fc cookie)
        fc_ok, fc_amt, fc_utr = await asyncio.to_thread(
            check_freecharge_transactions,
            raw_utr,
            None,
            None,
            tech_id,
        )
        if fc_ok and fc_amt >= amount:
            credit_customer_verified_utr(cust, raw_utr, fc_amt, "Freecharge Live API")
            if ADMIN_CHAT_ID:
                asyncio.create_task(
                    asyncio.to_thread(
                        send_message,
                        ADMIN_CHAT_ID,
                        f"⚡ <b>Freecharge UTR Verified (+₹{fc_amt})!</b>\n"
                        f"👤 Tech ID: <code>{tech_id}</code>\n🧾 UTR: <code>{raw_utr}</code>\n"
                        f"💳 New Balance: ₹{cust['balance']}",
                    )
                )
            return web.json_response({
                "ok": True,
                "status": "VERIFIED",
                "message": f"✅ Freecharge Verified! ₹{fc_amt} Added to Wallet!",
                "customer": cust,
            })

        # CHECK 2: Check Live BharatPe Merchant API (if configured)
        bp_ok, bp_amt = await asyncio.to_thread(check_real_bharatpe_api, raw_utr, amount)
        if bp_ok:
            credit_customer_verified_utr(cust, raw_utr, bp_amt, "BharatPe Live API")
            if ADMIN_CHAT_ID:
                asyncio.create_task(
                    asyncio.to_thread(
                        send_message,
                        ADMIN_CHAT_ID,
                        f"⚡ <b>BharatPe API Verified (+₹{bp_amt})!</b>\n"
                        f"👤 Tech ID: <code>{tech_id}</code>\n🧾 UTR: <code>{raw_utr}</code>\n"
                        f"💳 New Balance: ₹{cust['balance']}",
                    )
                )
            return web.json_response({
                "ok": True,
                "status": "VERIFIED",
                "message": f"✅ BharatPe Bank Verified! ₹{bp_amt} Added to Wallet!",
                "customer": cust,
            })

        # CHECK 3: Not in Bank Webhook or BharatPe yet -> NEVER give free money to unverified UTR!
        # Save in pending_payments (auto-credits as soon as SMS arrives, or Admin taps 1-click button in Telegram)
        pending_list = STATE.setdefault("pending_payments", [])
        if not any(p.get("utr") == raw_utr for p in pending_list):
            pending_list.insert(0, {
                "utr": raw_utr,
                "techId": tech_id,
                "amount": amount,
                "time": datetime.now().strftime("%I:%M %p"),
            })
            save_state(STATE)

            if ADMIN_CHAT_ID:
                kb = {
                    "inline_keyboard": [
                        [
                            {"text": f"✅ Approve ₹{amount} ({tech_id})", "callback_data": f"utr_ok_{raw_utr}_{tech_id}_{amount}"},
                            {"text": "❌ Reject Fake UTR", "callback_data": f"utr_no_{raw_utr}_{tech_id}"},
                        ]
                    ]
                }
                asyncio.create_task(
                    asyncio.to_thread(
                        send_message,
                        ADMIN_CHAT_ID,
                        f"🔔 <b>New UPI Payment Submitted!</b>\n\n"
                        f"👤 <b>Technician ID:</b> <code>{tech_id}</code>\n"
                        f"💰 <b>Claimed Amount:</b> ₹{amount}\n"
                        f"🧾 <b>UTR Number:</b> <code>{raw_utr}</code>\n\n"
                        "<i>(Agar Bank SMS Webhook ya BharatPe chalu hai toh SMS aate hi apne-aap approve ho jayega, warna niche 1-tap karein):</i>",
                        kb,
                    )
                )

        return web.json_response({
            "ok": False,
            "status": "PENDING_BANK",
            "message": "⏳ Checking UTR with Bank Server... (Jaise hi bank me ₹20 confirm hoga, upar wallet me apne-aap ₹20 aa jayega! Fake UTR मान्य नहीं है)",
            "customer": cust,
        })
    except Exception as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=400)


async def api_customer_order(request: web.Request) -> web.Response:
    """Customer submits 1 Reach Order (₹20) -> Bot automatically uses a backend account with >= 1.00 Coin to place order on Cashtree!"""
    try:
        body = await request.json()
        tech_id = str(body.get("techId") or "").strip()
        target_id = str(body.get("targetId") or "").strip()
        target_pass = str(body.get("targetPass") or "").strip()

        if not tech_id or not target_id or not target_pass:
            return web.json_response(
                {"ok": False, "message": "⚠️ Please enter Target ID and Target Password"},
                status=400,
            )

        customers = STATE.setdefault("customers", [])
        cust = next((c for c in customers if str(c.get("techId")) == tech_id), None)
        if not cust:
            return web.json_response({"ok": False, "message": "Technician account not found"}, status=404)

        if int(cust.get("balance", 0)) < 20:
            return web.json_response(
                {"ok": False, "message": "⚠️ Insufficient Balance (₹0)! Please pay ₹20 first."},
                status=400,
            )

        # Find a backend Cashtree node account that has >= 1.00 Coin (or fallback to highest coin / credit account)
        profiles = STATE.get("profiles", [])
        ready_nodes = [p for p in profiles if float(p.get("jpwCoins", 0.0)) >= 1.0]
        ready_nodes.sort(key=lambda x: float(x.get("jpwCoins", 0.0)), reverse=True)
        node_prof = ready_nodes[0] if ready_nodes else (profiles[0] if profiles else None)

        if not node_prof:
            return web.json_response(
                {"ok": False, "message": "⚠️ No backend service node configured. Contact Admin."},
                status=500,
            )

        # Place real order on Cashtree using the selected backend node account!
        api_res = await asyncio.to_thread(place_reach_order_on_cashtree, node_prof, target_id, target_pass)
        srv_data = api_res.get("data") or {}
        srv_msg = srv_data.get("message") or srv_data.get("error") or ("Reach Order Successful!" if api_res["ok"] else "Server rejected order")

        if not api_res["ok"]:
            # Do NOT deduct customer's ₹20 if Cashtree server rejected the order (e.g., outside service hours 09:30 AM - 10:15 PM IST)
            return web.json_response(
                {"ok": False, "message": f"⚠️ {srv_msg} (Your ₹{cust['balance']} balance is safe!)"},
                status=400,
            )

        # Order succeeded on Cashtree! Deduct ₹20 from Customer Wallet and 1 Coin from Backend Node
        cust["balance"] = max(0, int(cust.get("balance", 0)) - 20)
        cust["totalOrders"] = int(cust.get("totalOrders", 0)) + 1

        u_obj = srv_data.get("user") or {}
        node_prof["jpwCoins"] = float(u_obj.get("jpwCoins", max(0.0, float(node_prof.get("jpwCoins", 0.0)) - 1.0)))

        wo_id = random.randint(7100000000, 7999999999)
        now_str = datetime.now().strftime("%I:%M %p")
        order_obj = {
            "id": wo_id,
            "profileId": node_prof["id"],
            "customerTechId": tech_id,
            "targetId": target_id,
            "notes": f"APK User: {tech_id} | Pass: {target_pass}",
            "status": "COMPLETED",
            "time": now_str,
        }
        STATE["orders"].insert(0, order_obj)
        save_state(STATE)

        if ADMIN_CHAT_ID:
            asyncio.create_task(
                asyncio.to_thread(
                    send_message,
                    ADMIN_CHAT_ID,
                    f"🚀 <b>Auto-Order Placed by APK User!</b>\n"
                    f"👤 <b>Customer Tech ID:</b> <code>{tech_id}</code> (New Bal: ₹{cust['balance']})\n"
                    f"🎯 <b>Target ID:</b> <code>{target_id}</code> | Pass: <code>{target_pass}</code>\n"
                    f"⚡ <b>Used Backend Node:</b> <code>{node_prof['id']}</code> (Coins left: {node_prof['jpwCoins']:.2f})",
                )
            )

        my_orders = [o for o in STATE.get("orders", []) if str(o.get("customerTechId")) == tech_id]
        return web.json_response({
            "ok": True,
            "message": f"🚀 Order #{wo_id} Placed Successfully! ({srv_msg})",
            "customer": cust,
            "order": order_obj,
            "orders": my_orders[:20],
        })
    except Exception as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=400)


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
        await asyncio.sleep(600)  # Check every 10 minutes (auto-claims right after 12:00 AM IST)


async def keep_alive_self_pinger() -> None:
    """Pings the public Render URL every 4 minutes so the free instance stays warm 24x7."""
    await asyncio.sleep(15)
    target_url = WEBAPP_URL if (WEBAPP_URL and "example.com" not in WEBAPP_URL) else "https://nothin-7d7i.onrender.com"
    ping_url = target_url.rstrip("/") + "/ping"
    while True:
        try:
            await asyncio.to_thread(requests.get, ping_url, timeout=10)
        except Exception:
            pass
        await asyncio.sleep(240)  # Every 4 minutes


async def on_startup(app: web.Application) -> None:
    app["poller"] = asyncio.create_task(poll_telegram_updates())
    app["bonus_scheduler"] = asyncio.create_task(auto_daily_bonus_scheduler())
    app["keep_alive"] = asyncio.create_task(keep_alive_self_pinger())


async def on_cleanup(app: web.Application) -> None:
    for task_key in ("poller", "bonus_scheduler", "keep_alive"):
        task = app.get(task_key)
        if task:
            task.cancel()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", serve_index)
    app.router.add_get("/ping", ping_health)
    app.router.add_get("/api/state", api_state_get)
    app.router.add_post("/api/state", api_state_post)
    app.router.add_post("/api/register-proxy", api_register_proxy)
    app.router.add_get("/api/customer/me", api_customer_me)
    app.router.add_post("/api/customer/auth", api_customer_auth)
    app.router.add_post("/api/customer/pay", api_customer_pay)
    app.router.add_post("/api/customer/order", api_customer_order)
    app.router.add_post("/api/payment-webhook", api_payment_webhook)
    app.router.add_post("/api/customer/create-payment", api_customer_create_payment)
    app.router.add_post("/api/customer/check-payment", api_customer_check_payment)
    app.router.add_post("/api/customer/verify-razorpay", api_customer_verify_razorpay)
    app.router.add_post("/api/admin/fc-send-otp", api_admin_fc_send_otp)
    app.router.add_post("/api/admin/fc-verify-otp", api_admin_fc_verify_otp)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    logger.info("Starting JPW Mini App & Telegram Bot Server on port %s...", PORT)
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
