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
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
import hashlib
import hmac
import imaplib
import threading
import time

PAYMENT_LOCK = threading.Lock()
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
CASHTREE_CHECK_STATUS_API = "https://cashtree.space/api/app/check-status"

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
        "upiId": "jpwportal@freecharge",
        "appTitle": "JPW REACH",
        "registerApiUrl": CASHTREE_REGISTER_API,
        "gmailUser": os.environ.get("GMAIL_USER", ""),
        "gmailAppPassword": os.environ.get("GMAIL_APP_PASSWORD", ""),
        "reachPrice": 20,
        "pkgPrices": {"1": 20, "3": 60, "5": 100, "10": 200, "20": 400},
        "supportWhatsapp": os.environ.get("SUPPORT_WHATSAPP", "https://wa.me/919876543210"),
        "supportTelegram": os.environ.get("SUPPORT_TELEGRAM", "https://t.me/jpw_support"),
        "adminPassword": os.environ.get("ADMIN_PASSWORD", "786786"),
        "broadcast": {
            "id": "",
            "title": "",
            "message": "",
            "active": False,
            "style": "promo",
            "sentAt": "",
            "sentAtTs": 0,
        },
        "referral": {
            "enabled": True,
            "minRecharge": 20,
            "regBonusReach": 1.00,
            "r1BonusReach": 0.50,
            "r3BonusReach": 1.00,
            "r6BonusReach": 3.00,
            "r10BonusReach": 5.00,
            "apkShareUrl": "https://nothin-7d7i.onrender.com/?mode=customer",
        },
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
    "customers": [],
    "used_utrs": [],
    "apk_devices": {},
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
STATE.setdefault("apk_devices", {})
STATE.setdefault("verified_bank_utrs", {})  # { "12_digit_utr": amount_float } received from Bank SMS Webhook or BharatPe API
STATE.setdefault("pending_payments", [])
STATE["settings"].setdefault("adminPassword", os.environ.get("ADMIN_PASSWORD", "786786"))
STATE["settings"].setdefault("broadcast", {"id": "", "title": "", "message": "", "active": False, "style": "promo", "sentAt": "", "sentAtTs": 0})
STATE["settings"].setdefault("referral", {
    "enabled": True,
    "minRecharge": 20,
    "regBonusReach": 1.00,
    "r1BonusReach": 0.50,
    "r3BonusReach": 1.00,
    "r6BonusReach": 3.00,
    "r10BonusReach": 5.00,
    "apkShareUrl": "https://nothin-7d7i.onrender.com/?mode=customer",
})
STATE.setdefault("referral_logs", [])
STATE.setdefault("referral_claimed_devices", [])
STATE.setdefault("referral_claimed_ips", [])
STATE["settings"].setdefault("supportWhatsapp", os.environ.get("SUPPORT_WHATSAPP", "https://wa.me/919876543210"))
STATE["settings"].setdefault("supportTelegram", os.environ.get("SUPPORT_TELEGRAM", "https://t.me/jpw_support"))
STATE["settings"].setdefault("reachPrice", 20)
STATE["settings"].setdefault("pkgPrices", {"1": 20, "3": 60, "5": 100, "10": 200, "20": 400})
STATE["settings"].setdefault("gmailUser", os.environ.get("GMAIL_USER", ""))
STATE["settings"].setdefault("gmailAppPassword", os.environ.get("GMAIL_APP_PASSWORD", ""))
STATE["settings"].setdefault("freechargeCookie", os.environ.get("FREECHARGE_COOKIE", ""))
STATE["settings"].setdefault("bharatpeMid", os.environ.get("BHARATPE_MID", ""))
STATE["settings"].setdefault("bharatpeToken", os.environ.get("BHARATPE_TOKEN", ""))
STATE["settings"].setdefault("razorpayKeyId", os.environ.get("RAZORPAY_KEY_ID", ""))
STATE["settings"].setdefault("razorpayKeySecret", os.environ.get("RAZORPAY_KEY_SECRET", ""))
STATE["settings"].setdefault("webhookSecret", os.environ.get("WEBHOOK_SECRET", "jpw123"))
STATE.setdefault("payment_sessions", {})
STATE.setdefault("fc_logs", [])  # Live diagnostic logs for Freecharge app_fc Direct API
STATE.setdefault("activity_logs", [])  # ID-wise attempt, success & failure logs for Admin Page  # { session_id: { techId, baseAmount, uniqueAmount, status, createdAt, rzpOrderId } }


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


def fetch_cashtree_live_orders(prof: dict) -> dict:
    """
    Calls POST https://cashtree.space/api/app/check-status with {"telegramChatId": "WEB_<id>"}
    and returns {"ok": True, "regularOrders": [...], "srOrders": [...], "raw": data}.
    """
    cid = str(prof.get("id", "")).strip()
    chat_id = prof.get("telegramChatId") or f"WEB_{cid}"
    token = build_web_bearer_token(chat_id)
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Authorization": token if token.startswith("Bearer ") else f"Bearer {token}",
        "Origin": "https://cashtree.space",
        "Referer": "https://cashtree.space/app/",
    }
    payload = {"telegramChatId": chat_id}
    try:
        resp = requests.post(CASHTREE_CHECK_STATUS_API, json=payload, headers=headers, timeout=12)
        data = resp.json() if resp.content else {}
        return {
            "ok": resp.ok and bool(data.get("success", True)),
            "regularOrders": data.get("regularOrders") or data.get("orders") or [],
            "srOrders": data.get("srOrders") or [],
            "data": data,
        }
    except Exception as exc:
        logger.warning("Cashtree check-status API error (%s): %s", cid, exc)
        return {"ok": False, "regularOrders": [], "srOrders": [], "data": {}}


def extract_cashtree_workorder_details(ct_order: dict) -> dict:
    """
    Extracts real Work Order Number (e.g. '7127908231'), Customer Name (e.g. 'Vishram Datt Dwivedi'),
    and Customer Mobile Number (e.g. '+919721183368') from a Cashtree order dict or its text output
    (matching the exact format shown in Cashtree's LIVE ORDER TRACKER in 1000586036.jpg & 1000586037.png!).
    """
    if not isinstance(ct_order, dict):
        return {"workOrderNo": "", "custName": "", "custMobile": ""}

    wo_no = ""
    c_name = ""
    c_mob = ""

    # 1. Check direct & nested dictionary keys (e.g. workOrders array, details, data, result)
    candidates = [ct_order]
    for sub_k in ("workOrders", "work_orders", "orders", "details", "data", "result", "lead", "customer"):
        sub_v = ct_order.get(sub_k)
        if isinstance(sub_v, dict):
            candidates.append(sub_v)
        elif isinstance(sub_v, list):
            for item in sub_v:
                if isinstance(item, dict):
                    candidates.append(item)

    for d in candidates:
        if not wo_no:
            for k in ("workOrder", "workOrderNo", "workOrderId", "work_order", "woNumber", "woId", "orderNo", "orderNumber"):
                v = str(d.get(k) or "").strip()
                if v and re.match(r"^\d{7,15}$", v):
                    wo_no = v
                    break
        if not c_name:
            for k in ("customerName", "clientName", "leadName", "custName", "userName", "subscriberName", "fullName", "name"):
                v = str(d.get(k) or "").strip()
                if v and not re.match(r"^\d+$", v) and v.lower() not in ("success", "completed", "in progress", "processing", "rrysb"):
                    c_name = v
                    break
        if not c_mob:
            for k in ("customerMobile", "customerPhone", "mobileNo", "phoneNo", "mobileNumber", "phoneNumber", "contactNumber", "mobile", "phone"):
                v = str(d.get(k) or "").strip()
                # Ensure it's not the 10-digit 069... Tech ID
                if v and not v.startswith("06") and re.search(r"\+?91?[6-9]\d{9}|^[6-9]\d{9}$", re.sub(r"[\s\-]", "", v)):
                    c_mob = v
                    break

    # 2. Also parse all text fields / full dump for the emoji lines in 1000586036.jpg & 1000586037.png:
    #    📋 Work Order: 7127908231
    #    1. 7127908231 — In Progress
    #    👤 Vishram Datt Dwivedi
    #    📞 +919721183368
    text_blobs = []
    for k, v in ct_order.items():
        if isinstance(v, str) and v.strip():
            text_blobs.append(v)
        elif isinstance(v, (list, dict)):
            try:
                text_blobs.append(json.dumps(v, ensure_ascii=False))
            except Exception:
                pass
    combined_txt = "\n".join(text_blobs)

    if not wo_no:
        m_wo = re.search(r"Work\s*Order\s*[:\-#]*\s*(\d{7,15})", combined_txt, re.IGNORECASE)
        if not m_wo:
            m_wo = re.search(r"(?:^|\n|\s)1\.\s*(\d{7,15})\s*[—\-]", combined_txt)
        if m_wo:
            wo_no = m_wo.group(1).strip()

    if not c_mob:
        m_mob = re.search(r"📞\s*(\+?\d[\d\s\-]{8,14}\d)", combined_txt)
        if not m_mob:
            m_mob = re.search(r"(\+91[\s\-]?[6-9]\d{9}|(?<!\d)[6-9]\d{9}(?!\d))", combined_txt)
        if m_mob:
            cand_m = m_mob.group(1).strip()
            if cand_m != wo_no:
                c_mob = cand_m

    if not c_name:
        # Find all `👤 ...` lines that are NOT `👤 Tech ID:`
        for m_nm in re.finditer(r'👤\s*([^\n\r"📞📍📋📊{}]+)', combined_txt):
            cand_n = m_nm.group(1).strip().strip(",;'")
            if not cand_n:
                continue
            if cand_n.lower().startswith("tech id") or re.match(r"^0\d{8,10}$", cand_n):
                continue
            c_name = cand_n
            break

    return {
        "workOrderNo": wo_no,
        "custName": c_name,
        "custMobile": c_mob,
    }


def map_cashtree_order_status(ct_order: dict) -> str:
    """Maps a Cashtree regularOrders item to 'PROCESSING', 'COMPLETED', or 'FAILED'."""
    raw_st = str(
        ct_order.get("status")
        or ct_order.get("orderStatus")
        or ct_order.get("state")
        or ""
    ).strip().lower()

    if ct_order.get("isCompleted") is True or ct_order.get("completed") is True:
        return "COMPLETED"
    if any(w in raw_st for w in ("complet", "success", "done", "finish", "deliver", "approved")):
        return "COMPLETED"
    if any(w in raw_st for w in ("fail", "reject", "error", "cancel", "invalid", "wrong")):
        return "FAILED"

    # Also check full JSON dump of ct_order (e.g. '✅ REACHED SUCCESSFULLY' or '📊 Status: SUCCESS' in 1000586035.jpg!)
    try:
        dump_low = json.dumps(ct_order, ensure_ascii=False).lower()
        if "reached successfully" in dump_low or "status: success" in dump_low or '"status":"completed"' in dump_low or '"status": "completed"' in dump_low:
            return "COMPLETED"
        if "status: failed" in dump_low or "order failed" in dump_low or "invalid password" in dump_low:
            return "FAILED"
    except Exception:
        pass

    if raw_st:
        return "PROCESSING"
    return "PROCESSING"


def sync_all_pending_orders_from_cashtree() -> bool:
    """
    Checks all orders in STATE['orders'] that are currently in PROCESSING / IN PROGRESS / PENDING,
    queries POST https://cashtree.space/api/app/check-status for each profileId used,
    and updates the order status to COMPLETED (or FAILED) only when Cashtree finishes processing!
    """
    orders = STATE.get("orders", [])
    now_ms = int(time.time() * 1000)
    active_orders = [
        o for o in orders
        if str(o.get("status", "")).upper() not in ("COMPLETED", "FAILED", "REJECTED", "CANCELLED")
        or (
            str(o.get("status", "")).upper() == "COMPLETED"
            and not o.get("custMobile")
            and (now_ms - int(o.get("createdAtTs") or 0)) < 1200000
        )
    ]
    if not active_orders:
        return False

    # Group active orders by the backend profileId that placed them
    prof_map = {str(p.get("id")): p for p in STATE.get("profiles", []) if p.get("id")}
    pids_to_check = {str(o.get("profileId", "")).strip() for o in active_orders if o.get("profileId")}

    changed_any = False
    for pid in pids_to_check:
        prof = prof_map.get(pid) or {"id": pid, "telegramChatId": f"WEB_{pid}"}
        res = fetch_cashtree_live_orders(prof)
        if not res.get("ok"):
            continue

        reg_orders = res.get("regularOrders") or []
        if not isinstance(reg_orders, list):
            continue

        for ord_obj in active_orders:
            if str(ord_obj.get("profileId", "")).strip() != pid:
                continue
            target_id = str(ord_obj.get("targetId", "")).strip()
            ct_id = str(ord_obj.get("cashtreeOrderId", "")).strip()

            # Find matching order in regularOrders by cashtreeOrderId or targetId
            matched_ct = None
            for co in reg_orders:
                if not isinstance(co, dict):
                    continue
                co_id = str(co.get("_id") or co.get("id") or co.get("orderId") or "").strip()
                co_target = str(
                    co.get("targetId")
                    or co.get("target_id")
                    or co.get("techId")
                    or co.get("customId")
                    or co.get("target")
                    or co.get("reach")
                    or co.get("mobile")
                    or ""
                ).strip()
                co_dump = json.dumps(co, ensure_ascii=False)
                if (ct_id and co_id == ct_id) or (target_id and (co_target == target_id or target_id in co_dump)):
                    matched_ct = co
                    break

            if matched_ct:
                new_status = map_cashtree_order_status(matched_ct)
                ct_msg = str(matched_ct.get("message") or matched_ct.get("remarks") or matched_ct.get("status") or "").strip()
                wo_info = extract_cashtree_workorder_details(matched_ct)
                if wo_info["workOrderNo"] and wo_info["workOrderNo"] != str(ord_obj.get("workOrderNo") or ""):
                    ord_obj["workOrderNo"] = wo_info["workOrderNo"]
                    changed_any = True
                if wo_info["custName"] and wo_info["custName"] != str(ord_obj.get("custName") or ""):
                    ord_obj["custName"] = wo_info["custName"]
                    changed_any = True
                if wo_info["custMobile"] and wo_info["custMobile"] != str(ord_obj.get("custMobile") or ""):
                    ord_obj["custMobile"] = wo_info["custMobile"]
                    changed_any = True
                if new_status != ord_obj.get("status"):
                    ord_obj["status"] = new_status
                    if ct_msg and ct_msg not in str(ord_obj.get("notes", "")):
                        ord_obj["notes"] = f"{ord_obj.get('notes', '')} • Server: {ct_msg}".strip(" •")
                    changed_any = True

                    # If order failed on Cashtree and was placed by a Customer APK user, refund their ₹20 once!
                    if new_status == "FAILED" and ord_obj.get("customerTechId") and not ord_obj.get("refunded"):
                        ord_obj["refunded"] = True
                        tid = str(ord_obj["customerTechId"])
                        cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == tid), None)
                        if cust:
                            cust["balance"] = int(cust.get("balance", 0)) + 20
                            log_user_activity(tid, "ORDER", "FAILED", f"Order #{ord_obj.get('id')} Failed on Cashtree ({ct_msg}) -> ₹20 Refunded to Wallet")
                    elif new_status == "COMPLETED" and ord_obj.get("customerTechId"):
                        tid = str(ord_obj["customerTechId"])
                        log_user_activity(tid, "ORDER", "SUCCESS", f"Order #{ord_obj.get('id')} (Target: {target_id}) Marked COMPLETED by Cashtree Server!")

    if changed_any:
        save_state(STATE)
    reconcile_failed_order_refunds()
    return changed_any


def reconcile_failed_order_refunds() -> int:
    """
    Guarantees that EVERY Customer APK order marked FAILED / REJECTED / CANCELLED
    with `not o.get('refunded')` automatically refunds ₹20 to the customer's wallet ONCE!
    Works both for newly failed orders AND retroactively for any past failed orders like WO #7979245907.
    """
    refunded_count = 0
    customers = STATE.get("customers", [])
    for ord_obj in STATE.get("orders", []):
        if not isinstance(ord_obj, dict):
            continue
        st = str(ord_obj.get("status", "")).upper()
        tid = str(ord_obj.get("customerTechId") or "").strip()
        if st in ("FAILED", "REJECTED", "CANCELLED") and tid and not ord_obj.get("refunded"):
            cust = next((c for c in customers if str(c.get("techId")) == tid), None)
            if cust:
                ref_amt = int(ord_obj.get("orderPrice") or STATE["settings"].get("reachPrice") or 20)
                ord_obj["refunded"] = True
                cust["balance"] = int(cust.get("balance", 0)) + ref_amt
                if "Refunded" not in str(ord_obj.get("notes", "")):
                    ord_obj["notes"] = f"{ord_obj.get('notes', '')} • 💸 ₹{ref_amt} Refunded".strip(" •")
                log_user_activity(
                    tid,
                    "ORDER",
                    "FAILED",
                    f"Order #{ord_obj.get('id')} FAILED -> Auto-Refunded +₹{ref_amt} (New Bal: ₹{cust['balance']})",
                )
                refunded_count += 1
    if refunded_count > 0:
        save_state(STATE)
        logger.info("Auto-refunded ₹20 for %d failed customer orders!", refunded_count)
    return refunded_count


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
    stamp = datetime.utcfromtimestamp(time.time() + 19800).strftime("%Y%m%d_%H%M")
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
            now_str = datetime.utcfromtimestamp(time.time() + 19800).strftime("%I:%M %p")
            ct_ord = srv_data.get("order") or srv_data.get("data") or {}
            ct_oid = str(ct_ord.get("_id") or ct_ord.get("id") or "") if isinstance(ct_ord, dict) else ""
            init_status = map_cashtree_order_status(ct_ord) if isinstance(ct_ord, dict) and ct_ord else "PROCESSING"
            order_obj = {
                "id": wo_id,
                "cashtreeOrderId": ct_oid,
                "profileId": pid,
                "targetId": target_id,
                "notes": f"Pass: {target_pass} | {srv_msg}",
                "status": init_status,
                "time": now_str,
                "createdAtTs": int(time.time() * 1000),
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
                "createdAt": datetime.utcfromtimestamp(time.time() + 19800).strftime("%Y-%m-%d %H:%M"),
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


async def serve_service_worker(request: web.Request) -> web.Response:
    """Serves /sw.js so Android WebView / Chrome can display native top status-bar notifications via ServiceWorkerRegistration.showNotification()!"""
    sw_js = """
self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      if (clientList.length > 0) return clientList[0].focus();
      return self.clients.openWindow('/?mode=customer');
    })
  );
});
"""
    return web.Response(text=sw_js.strip(), content_type="application/javascript")


async def ping_health(request: web.Request) -> web.Response:
    """Ultra-fast 2-byte healthcheck endpoint for cron-job.org and self-pinger."""
    return web.Response(text="OK")


def get_current_admin_password() -> str:
    env_p = os.environ.get("ADMIN_PASSWORD", "").strip()
    saved_p = str(STATE.get("settings", {}).get("adminPassword") or "").strip()
    return saved_p or env_p or "786786"


def is_valid_admin_pin(pin_str: str) -> bool:
    cand = str(pin_str or "").strip()
    if not cand:
        return False
    real_p = get_current_admin_password()
    env_p = os.environ.get("ADMIN_PASSWORD", "").strip()
    if cand == real_p or (env_p and cand == env_p):
        return True
    return False


async def api_admin_auth(request: web.Request) -> web.Response:
    """Verifies the Admin's Secret PIN / Password before unlocking the Admin Dashboard."""
    try:
        body = await request.json()
        pin = str(body.get("adminPassword") or "").strip()
        if is_valid_admin_pin(pin):
            return web.json_response({"ok": True, "message": "🔓 Admin Unlocked!"})
        return web.json_response({"ok": False, "message": "❌ Wrong Admin PIN / Password!"}, status=403)
    except Exception as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=400)

async def api_broadcast_latest(request: web.Request) -> web.Response:
    """Ultra-lightweight endpoint polled by Customer APK (foreground & Android background JobService) for Swiggy/Zomato-style push alerts & live notice banner."""
    bcast = STATE.get("settings", {}).get("broadcast") or {}
    return web.json_response({
        "ok": True,
        "id": str(bcast.get("id") or ""),
        "title": str(bcast.get("title") or ""),
        "message": str(bcast.get("message") or ""),
        "active": bool(bcast.get("active", False)),
        "style": str(bcast.get("style") or "promo"),
        "sentAt": str(bcast.get("sentAt") or ""),
        "sentAtTs": int(bcast.get("sentAtTs") or 0),
    })


async def api_admin_broadcast(request: web.Request) -> web.Response:
    """Admin endpoint to send or clear a Live Notice Banner + Push Notification for all Customer APK users."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "message": "Invalid JSON body"}, status=400)

    req_pin = (request.headers.get("X-Admin-Pin") or body.get("adminPin") or "").strip()
    if not is_valid_admin_pin(req_pin):
        return web.json_response({"ok": False, "locked": True, "message": "🔒 Invalid Admin PIN!"}, status=401)

    action = str(body.get("action") or "send").strip().lower()
    cur = STATE["settings"].get("broadcast") or {}

    if action == "clear":
        updated = {
            "id": str(cur.get("id") or ""),
            "title": "",
            "message": "",
            "active": False,
            "style": "promo",
            "sentAt": "",
            "sentAtTs": 0,
        }
        STATE["settings"]["broadcast"] = updated
        save_state(STATE)
        return web.json_response({"ok": True, "message": "🗑️ Notice banner hidden for all users.", "broadcast": updated})

    title = str(body.get("title") or "").strip()[:120]
    message = str(body.get("message") or "").strip()[:400]
    style = str(body.get("style") or "promo").strip().lower()
    if style not in ("promo", "alert", "info"):
        style = "promo"
    if not title and not message:
        return web.json_response({"ok": False, "message": "⚠️ Title ya Message zaroor likhein!"}, status=400)
    if not title:
        title = "📢 JPW Reach Update"

    now_ts = int(time.time())
    bcast_id = f"BCAST_{int(time.time() * 1000)}"
    sent_at = datetime.utcfromtimestamp(time.time() + 19800).strftime("%d %b, %I:%M %p")
    updated = {
        "id": bcast_id,
        "title": title,
        "message": message,
        "active": True,
        "style": style,
        "sentAt": sent_at,
        "sentAtTs": now_ts,
    }
    STATE["settings"]["broadcast"] = updated
    save_state(STATE)
    return web.json_response({
        "ok": True,
        "message": "📢 Push Notification & Live Banner sent to all Customer APK users!",
        "broadcast": updated,
    })

def get_referral_settings() -> dict:
    defaults = {
        "enabled": True,
        "minRecharge": 20,
        "regBonusReach": 1.00,
        "r1BonusReach": 0.50,
        "r3BonusReach": 1.00,
        "r6BonusReach": 3.00,
        "r10BonusReach": 5.00,
        "apkShareUrl": "https://nothin-7d7i.onrender.com/?mode=customer",
    }
    raw = STATE.get("settings", {}).get("referral")
    if not isinstance(raw, dict):
        STATE["settings"]["referral"] = dict(defaults)
        return STATE["settings"]["referral"]
    for k, v in defaults.items():
        raw.setdefault(k, v)
    return raw



def get_client_ip(request: web.Request) -> str:
    try:
        xff = str(request.headers.get("X-Forwarded-For") or "").strip()
        if xff:
            return xff.split(",")[0].strip()[:64]
        xri = str(request.headers.get("X-Real-IP") or "").strip()
        if xri:
            return xri[:64]
        return str(getattr(request, "remote", "") or "").strip()[:64]
    except Exception:
        return ""


def make_restore_sig(tech_id: str, balance: int, total_orders: int, ref_code: str = "") -> str:
    secret = (os.environ.get("BOT_TOKEN") or get_current_admin_password() or "jpw_secret_key").encode("utf-8")
    msg = f"{str(tech_id).strip()}|{int(balance)}|{int(total_orders)}|{str(ref_code or '').strip().upper()}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:32]


def verify_restore_sig(tech_id: str, balance: int, total_orders: int, ref_code: str, sig: str) -> bool:
    if not sig:
        return False
    expected = make_restore_sig(tech_id, balance, total_orders, ref_code)
    return hmac.compare_digest(expected, str(sig).strip())


def ensure_customer_referral_code(cust: dict) -> str:
    """Assigns & persists a clean sequential referral code (RS001, RS002, RS003...) per customer."""
    if not isinstance(cust, dict):
        return ""
    existing = str(cust.get("referralCode") or "").strip().upper()
    if re.fullmatch(r"RS\d{3,}", existing):
        return existing
    max_num = int(STATE.get("settings", {}).get("referralSeq") or 0)
    for c in STATE.get("customers", []):
        rc = str(c.get("referralCode") or "").strip().upper()
        m = re.fullmatch(r"RS(\d+)", rc)
        if m:
            max_num = max(max_num, int(m.group(1)))
    next_num = max_num + 1
    new_code = f"RS{next_num:03d}"
    cust["referralCode"] = new_code
    STATE.setdefault("settings", {})["referralSeq"] = next_num
    try:
        save_state(STATE)
    except Exception:
        pass
    return new_code


def make_referral_code(tech_id: str) -> str:
    tid = str(tech_id or "").strip()
    if not tid:
        return ""
    cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == tid), None)
    if cust:
        return ensure_customer_referral_code(cust)
    return ""


def find_referrer_by_code(ref_code: str, exclude_tech_id: str = "") -> dict | None:
    raw = re.sub(r"[^A-Za-z0-9]", "", str(ref_code or "").strip()).upper()
    if not raw:
        return None
    ex_clean = str(exclude_tech_id or "").strip().upper()
    for c in STATE.get("customers", []):
        ctid = str(c.get("techId") or "").strip()
        if not ctid or ctid.upper() == ex_clean:
            continue
        c_code = ensure_customer_referral_code(c)
        if c_code == raw:
            return c
    return None


def award_referral_milestone(friend_cust: dict, milestone_key: str, device_id: str = "", client_ip: str = "") -> dict | None:
    """Awards a tiered Refer & Earn bonus (REG, R1, R3, R6 x3, R10) once per friend milestone with anti-fraud device lock for REG."""
    cfg = get_referral_settings()
    if not cfg.get("enabled", True):
        return None
    if not isinstance(friend_cust, dict):
        return None
    ref_tid = str(friend_cust.get("referredBy") or "").strip()
    friend_tid = str(friend_cust.get("techId") or "").strip()
    if not ref_tid or not friend_tid or ref_tid.lower() == friend_tid.lower():
        return None

    referrer = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == ref_tid), None)
    if not referrer:
        return None

    paid_list = friend_cust.setdefault("referralMilestonesPaid", [])
    if milestone_key in paid_list:
        return None

    # Strict Anti-Fraud for Register bonus:
    # 1) Must have a valid device_id (blocks curl/API bots)
    # 2) Cannot match referrer's own deviceId or IP (blocks self-referral on same phone/WiFi)
    # 3) Max 1 REG bonus per unique phone deviceId AND per unique IP (blocks Clear Data & Incognito farming)
    # 4) Max 5 REG bonuses per referrer per IST day
    if milestone_key == "REG":
        did = str(device_id or "").strip()
        cip = str(client_ip or "").strip()
        if not did or len(did) < 5:
            return None
        ref_did = str(referrer.get("deviceId") or "").strip()
        ref_ip = str(referrer.get("lastIp") or "").strip()
        if ref_did and did == ref_did:
            return None
        if cip and ref_ip and cip == ref_ip:
            return None
        claimed_devs = STATE.setdefault("referral_claimed_devices", [])
        claimed_ips = STATE.setdefault("referral_claimed_ips", [])
        if did in claimed_devs:
            return None
        if cip and cip in claimed_ips:
            return None
        today_ist = datetime.utcfromtimestamp(time.time() + 19800).strftime("%d %b")
        today_reg_count = sum(
            1 for x in STATE.get("referral_logs", [])
            if str(x.get("referrerId") or "") == ref_tid
            and x.get("milestone") == "REG"
            and str(x.get("timeStr") or "").startswith(today_ist)
        )
        if today_reg_count >= 5:
            return None
        claimed_devs.append(did)
        if cip:
            claimed_ips.append(cip)

    cfg_map = {
        "REG": ("regBonusReach", "Friend Register Bonus"),
        "R1": ("r1BonusReach", "1st Recharge Bonus"),
        "R3": ("r3BonusReach", "3rd Recharge Bonus"),
        "R6": ("r6BonusReach", "6th Recharge ×3 Bonus"),
        "R10": ("r10BonusReach", "10th Recharge Mega Bonus"),
    }
    if milestone_key not in cfg_map:
        return None
    cfg_key, label = cfg_map[milestone_key]
    try:
        bonus_reach = float(cfg.get(cfg_key) or 0)
    except Exception:
        bonus_reach = 0.0
    if bonus_reach <= 0:
        return None

    reach_price = max(1, int(STATE["settings"].get("reachPrice") or 20))
    bonus_rupees = max(1, int(round(bonus_reach * reach_price)))

    referrer["balance"] = int(referrer.get("balance", 0)) + bonus_rupees
    referrer["referralEarnedRupees"] = int(referrer.get("referralEarnedRupees", 0)) + bonus_rupees
    referrer["referralEarnedReach"] = round(float(referrer.get("referralEarnedReach", 0.0)) + bonus_reach, 2)
    paid_list.append(milestone_key)

    now_ts = int(time.time())
    time_str = datetime.utcfromtimestamp(now_ts + 19800).strftime("%d %b, %I:%M %p")
    entry = {
        "id": f"REF_{int(time.time() * 1000)}_{milestone_key}",
        "referrerId": ref_tid,
        "friendId": friend_tid,
        "milestone": milestone_key,
        "milestoneLabel": label,
        "bonusReach": round(bonus_reach, 2),
        "bonusRupees": bonus_rupees,
        "timeStr": time_str,
        "ts": now_ts,
    }
    STATE.setdefault("referral_logs", []).insert(0, entry)
    if len(STATE["referral_logs"]) > 500:
        STATE["referral_logs"] = STATE["referral_logs"][:500]

    log_user_activity(
        ref_tid,
        "REFERRAL",
        "SUCCESS",
        f"🎁 +{bonus_reach:.2f} Reach ({label}) from Friend {friend_tid} • New Bal: ₹{referrer['balance']}",
    )
    return entry


def build_customer_referral_summary(tech_id: str) -> dict:
    cfg = get_referral_settings()
    tid = str(tech_id or "").strip()
    friends = [c for c in STATE.get("customers", []) if str(c.get("referredBy") or "").strip() == tid]
    my_logs = [x for x in STATE.get("referral_logs", []) if str(x.get("referrerId") or "") == tid]
    cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == tid), None)
    earned_reach = round(float(cust.get("referralEarnedReach", 0.0)), 2) if cust else 0.0
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "myCode": make_referral_code(tid),
        "apkShareUrl": str(cfg.get("apkShareUrl") or "https://nothin-7d7i.onrender.com/?mode=customer"),
        "minRecharge": int(cfg.get("minRecharge") or 20),
        "milestones": {
            "reg": float(cfg.get("regBonusReach") or 0),
            "r1": float(cfg.get("r1BonusReach") or 0),
            "r3": float(cfg.get("r3BonusReach") or 0),
            "r6": float(cfg.get("r6BonusReach") or 0),
            "r10": float(cfg.get("r10BonusReach") or 0),
        },
        "invitedCount": len(friends),
        "earnedReach": earned_reach,
        "invitedFriends": [
            {
                "friendId": str(f.get("techId") or ""),
                "rechargeCount": int(f.get("rechargeCount") or 0),
                "milestonesPaid": list(f.get("referralMilestonesPaid") or []),
                "createdAt": str(f.get("createdAt") or ""),
            }
            for f in friends[:30]
        ],
        "recentBonuses": my_logs[:20],
    }


async def api_admin_referral_settings(request: web.Request) -> web.Response:
    """Admin endpoint to update Refer & Earn 5-milestone rewards & ON/OFF toggle."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "message": "Invalid JSON body"}, status=400)

    req_pin = (request.headers.get("X-Admin-Pin") or body.get("adminPin") or "").strip()
    if not is_valid_admin_pin(req_pin):
        return web.json_response({"ok": False, "locked": True, "message": "🔒 Invalid Admin PIN!"}, status=401)

    cfg = get_referral_settings()
    cfg["enabled"] = bool(body.get("enabled", True))
    cfg["minRecharge"] = max(1, int(float(body.get("minRecharge") or 20)))
    for k in ("regBonusReach", "r1BonusReach", "r3BonusReach", "r6BonusReach", "r10BonusReach"):
        if k in body:
            try:
                cfg[k] = max(0.0, round(float(body.get(k) or 0), 2))
            except Exception:
                pass
    share_url = str(body.get("apkShareUrl") or "").strip()
    if share_url:
        cfg["apkShareUrl"] = share_url
    STATE["settings"]["referral"] = cfg
    save_state(STATE)
    return web.json_response({"ok": True, "message": "🎁 Refer & Earn settings saved!", "referral": cfg})




async def api_state_get(request: web.Request) -> web.Response:
    """Returns STATE ONLY when valid X-Admin-Pin is provided; otherwise returns public settings only!"""
    req_pin = (request.headers.get("X-Admin-Pin") or request.query.get("adminPin") or "").strip()
    if not is_valid_admin_pin(req_pin):
        pub_set = STATE.get("settings", {})
        return web.json_response({
            "ok": False,
            "locked": True,
            "settings": {
                "upiId": pub_set.get("upiId", "jpwportal@freecharge"),
                "appTitle": pub_set.get("appTitle", "JPW REACH"),
                "reachPrice": int(pub_set.get("reachPrice") or 20),
                "pkgPrices": pub_set.get("pkgPrices") or {"1": 20, "3": 60, "5": 100, "10": 200, "20": 400},
                "supportWhatsapp": pub_set.get("supportWhatsapp", ""),
                "supportTelegram": pub_set.get("supportTelegram", ""),
                "broadcast": pub_set.get("broadcast") or {"id": "", "title": "", "message": "", "active": False, "style": "promo", "sentAt": "", "sentAtTs": 0},
            },
        }, status=401)

    safe_state = dict(STATE)
    raw_settings = dict(STATE.get("settings", {}))
    real_gu = str(raw_settings.get("gmailUser") or os.environ.get("GMAIL_USER") or "").strip()
    real_gp = str(raw_settings.get("gmailAppPassword") or os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    if real_gp == "****************":
        real_gp = ""
        raw_settings["gmailAppPassword"] = ""
    raw_settings["gmailImapReady"] = bool(real_gu and len(real_gp) >= 8)
    safe_state["settings"] = raw_settings
    return web.json_response(safe_state)


async def api_state_post(request: web.Request) -> web.Response:
    """Smart Two-Way Sync: safely merges profiles, deletions, orders, stats, and settings from Mini App into bot.py STATE."""
    try:
        body = await request.json()
        req_pin = (request.headers.get("X-Admin-Pin") or str(body.get("adminPin") or "")).strip()
        # Allow self-heal of default 786786 if server just restarted (updatedAt == 0), otherwise verify pin
        srv_ts = int(STATE.get("settings", {}).get("updatedAt") or 0)
        if srv_ts > 0 and not is_valid_admin_pin(req_pin):
            return web.json_response({"ok": False, "locked": True, "message": "Unauthorized Admin PIN"}, status=403)

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
                    eo = existing_omap[oid]
                    eo_st = str(eo.get("status", "")).upper()
                    io_st = str(io.get("status", "")).upper()
                    was_refunded = bool(eo.get("refunded"))
                    orig_time = eo.get("time")
                    orig_ts = eo.get("createdAtTs")
                    eo.update(io)
                    if was_refunded:
                        eo["refunded"] = True
                    if orig_time:
                        eo["time"] = orig_time
                    if orig_ts:
                        eo["createdAtTs"] = orig_ts
                    for keep_k in ("workOrderNo", "custName", "custMobile"):
                        if eo.get(keep_k) and not io.get(keep_k):
                            eo[keep_k] = eo[keep_k]
                    # Preserve terminal COMPLETED / FAILED status over PROCESSING
                    if eo_st in ("COMPLETED", "FAILED") and io_st in ("PROCESSING", "IN PROGRESS", "PENDING"):
                        eo["status"] = eo_st
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
            inc_ts = int(incoming_settings.get("updatedAt") or 0)
            srv_ts = int(STATE["settings"].get("updatedAt") or 0)
            # Restore settings if Admin clicked Save, or incoming is newer, or server just restarted (srv_ts == 0)!
            if body.get("saveSettings") is True or inc_ts > srv_ts or srv_ts == 0:
                for k, v in incoming_settings.items():
                    if k in ("gmailAppPassword", "freechargeCookie", "bharatpeToken", "razorpayKeySecret"):
                        if not v or str(v).strip() == "****************":
                            continue
                    if k == "broadcast" and isinstance(v, dict):
                        cur_b = STATE["settings"].get("broadcast") or {}
                        cur_bts = int(cur_b.get("sentAtTs") or 0)
                        inc_bts = int(v.get("sentAtTs") or 0)
                        if cur_b.get("id") and inc_bts <= cur_bts:
                            continue
                    if v is not None and v != "":
                        STATE["settings"][k] = v
                if body.get("saveSettings") is True and inc_ts <= srv_ts:
                    STATE["settings"]["updatedAt"] = int(time.time() * 1000)
            else:
                # Even if srv_ts > 0, self-heal any missing secret (like gmailUser / gmailAppPassword) from Admin's device!
                for k in ("gmailUser", "gmailAppPassword", "freechargeCookie", "bharatpeMid", "bharatpeToken"):
                    v = incoming_settings.get(k)
                    if v and str(v).strip() != "****************" and not STATE["settings"].get(k):
                        STATE["settings"][k] = v

        incoming_apk_devices = body.get("apk_devices")
        if isinstance(incoming_apk_devices, dict):
            cur_devs = STATE.setdefault("apk_devices", {})
            for d_id, d_obj in incoming_apk_devices.items():
                if not isinstance(d_obj, dict):
                    continue
                if d_id not in cur_devs:
                    cur_devs[d_id] = d_obj
                else:
                    if d_obj.get("techId") and not cur_devs[d_id].get("techId"):
                        cur_devs[d_id]["techId"] = d_obj["techId"]

        incoming_used_utrs = body.get("used_utrs")
        if isinstance(incoming_used_utrs, list):
            cur_utrs = set(STATE.setdefault("used_utrs", []))
            for u in incoming_used_utrs:
                if u and str(u) not in cur_utrs:
                    STATE["used_utrs"].append(str(u))
                    cur_utrs.add(str(u))

        incoming_customers = body.get("customers")
        if isinstance(incoming_customers, list):
            valid_inc_custs = [
                c for c in incoming_customers
                if isinstance(c, dict) and c.get("techId") and str(c.get("techId")) != "TECH101"
            ]
            if body.get("adminCustomerUpdate") is True:
                STATE["customers"] = valid_inc_custs
            else:
                # Self-Heal after GitHub code push / Render restart: restore any customer missing on server!
                existing_cmap = {str(c.get("techId")): c for c in STATE.setdefault("customers", []) if c.get("techId")}
                for ic in reversed(valid_inc_custs):
                    tid_str = str(ic["techId"])
                    if tid_str not in existing_cmap:
                        STATE["customers"].insert(0, ic)
                        existing_cmap[tid_str] = ic
                    else:
                        ec = existing_cmap[tid_str]
                        if int(ic.get("totalOrders", 0)) > int(ec.get("totalOrders", 0)):
                            ec["totalOrders"] = int(ic.get("totalOrders", 0))

        reconcile_failed_order_refunds()
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


def record_apk_device_ping(device_id: str, device_model: str = "", tech_id: str = "") -> None:
    """
    Tracks every unique phone that downloads & opens the Customer APK (`?mode=customer`),
    whether they have registered/logged in (`techId`) or not, and links their purchase stats!
    """
    did = str(device_id or "").strip()[:48]
    if not did:
        return
    ist_now = datetime.utcfromtimestamp(time.time() + 19800).strftime("%d %b, %I:%M %p")
    devices = STATE.setdefault("apk_devices", {})
    dev = devices.get(did)
    if not dev:
        dev = {
            "deviceId": did,
            "model": str(device_model or "Android Phone").strip()[:45],
            "techId": "",
            "firstSeen": ist_now,
            "firstSeenTs": int(time.time()),
            "lastSeen": ist_now,
            "lastSeenTs": int(time.time()),
            "opens": 1,
        }
        devices[did] = dev
        save_state(STATE)
    else:
        dev["lastSeen"] = ist_now
        dev["lastSeenTs"] = int(time.time())
        if device_model and (not dev.get("model") or dev.get("model") == "Android Phone"):
            dev["model"] = str(device_model).strip()[:45]

    tid = str(tech_id or "").strip()
    if tid and tid != "TECH101" and dev.get("techId") != tid:
        dev["techId"] = tid
        save_state(STATE)


def get_customer_payment_history(tech_id: str) -> list[dict]:
    """Returns all payment tries (SUCCESS / FAILED / PENDING) for `tech_id` with Order ID, Amount, and IST Time for Passbook."""
    tid = str(tech_id or "").strip()
    if not tid:
        return []
    now_ts = time.time()
    sessions = STATE.setdefault("payment_sessions", {})
    my_list = []
    for sid, s in list(sessions.items()):
        if str(s.get("techId") or "") != tid:
            continue
        st = str(s.get("status") or "PENDING").upper()
        c_at = float(s.get("createdAt") or 0.0)
        # If a payment try has been PENDING for more than 10 minutes (600s) without payment, mark it FAILED in Passbook
        if st == "PENDING" and c_at > 0 and (now_ts - c_at) > 600:
            s["status"] = "FAILED"
            st = "FAILED"
        disp_st = "SUCCESS" if st in ("COMPLETED", "SUCCESS") else ("FAILED" if st in ("FAILED", "CANCELLED", "SUPERSEDED") else "PENDING")
        t_str = s.get("timeStr")
        if not t_str:
            ts_use = c_at if c_at > 0 else now_ts
            t_str = datetime.utcfromtimestamp(ts_use + 19800).strftime("%d %b, %I:%M %p")
            s["timeStr"] = t_str
        my_list.append({
            "orderId": str(s.get("sessionId") or sid),
            "amount": int(float(s.get("baseAmount") or 20)),
            "walletCredit": int(float(s.get("walletCredit") or s.get("baseAmount") or 20)),
            "status": disp_st,
            "time": t_str,
            "createdAt": c_at,
        })
    for rl in STATE.get("referral_logs", []):
        if str(rl.get("referrerId") or "") == tid:
            my_list.append({
                "orderId": f"🎁 {rl.get('milestoneLabel', 'Referral Bonus')} ({rl.get('friendId', '')})",
                "amount": int(rl.get("bonusRupees") or 0),
                "walletCredit": int(rl.get("bonusRupees") or 0),
                "status": "SUCCESS",
                "time": str(rl.get("timeStr") or ""),
                "createdAt": float(rl.get("ts") or 0.0),
            })
    my_list.sort(key=lambda x: float(x.get("createdAt") or 0.0), reverse=True)
    return my_list[:35]


async def api_customer_me(request: web.Request) -> web.Response:
    reconcile_failed_order_refunds()
    await asyncio.to_thread(sync_pending_sessions_via_gmail_imap, False)
    await asyncio.to_thread(sync_all_pending_orders_from_cashtree)
    """Safe read-only sync endpoint for Customer APK: returns only this customer's live balance, orders, and UPI settings."""
    tech_id = str(request.query.get("techId") or "").strip()
    device_id = (request.query.get("deviceId") or "").strip()
    device_model = (request.query.get("deviceModel") or "").strip()
    if device_id:
        record_apk_device_ping(device_id, device_model, tech_id)
    upi_id = STATE["settings"].get("upiId", "jpwportal@freecharge")
    app_title = STATE["settings"].get("appTitle", "JPW REACH")
    reach_price = int(STATE["settings"].get("reachPrice") or 20)
    pkg_prices = STATE["settings"].get("pkgPrices") or {"1": 20, "3": 60, "5": 100, "10": 200, "20": 400}
    sup_wa = STATE["settings"].get("supportWhatsapp") or "https://wa.me/919876543210"
    sup_tg = STATE["settings"].get("supportTelegram") or "https://t.me/jpw_support"
    bcast = STATE["settings"].get("broadcast") or {"id": "", "title": "", "message": "", "active": False, "style": "promo", "sentAt": "", "sentAtTs": 0}
    if not tech_id:
        return web.json_response({
            "ok": True,
            "upiId": upi_id,
            "appTitle": app_title,
            "reachPrice": reach_price,
            "pkgPrices": pkg_prices,
            "supportWhatsapp": sup_wa,
            "supportTelegram": sup_tg,
            "broadcast": bcast,
        })

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
            "referralCode": ensure_customer_referral_code(cust),
            "restoreSig": make_restore_sig(tech_id, int(cust.get("balance", 0)), int(cust.get("totalOrders", 0)), ensure_customer_referral_code(cust)),
        },
        "orders": my_orders[:20],
        "payments": get_customer_payment_history(tech_id),
        "upiId": upi_id,
        "appTitle": app_title,
        "reachPrice": reach_price,
        "pkgPrices": pkg_prices,
        "supportWhatsapp": sup_wa,
        "supportTelegram": sup_tg,
        "broadcast": bcast,
        "referral": build_customer_referral_summary(tech_id),
    })


async def api_customer_auth(request: web.Request) -> web.Response:
    """Customer APK Login / Auto-Registration by Technician ID & Password."""
    try:
        body = await request.json()
        tech_id = str(body.get("techId") or "").strip()
        password = str(body.get("password") or "").strip()
        device_id = str(body.get("deviceId") or "").strip()
        device_model = str(body.get("deviceModel") or "").strip()
        client_ip = get_client_ip(request)
        if device_id and tech_id:
            record_apk_device_ping(device_id, device_model, tech_id)
        if not tech_id or not password:
            return web.json_response({"ok": False, "message": "Enter Technician ID & Password"}, status=400)

        customers = STATE.setdefault("customers", [])
        cust = next((c for c in customers if str(c.get("techId")) == tech_id), None)
        if cust:
            if str(cust.get("password")) != password:
                log_user_activity(tech_id, "LOGIN", "FAILED", f"Wrong password entered ({password})")
                save_state(STATE)
                return web.json_response({"ok": False, "message": "❌ Wrong Password for this Technician ID!"}, status=401)
            ensure_customer_referral_code(cust)
            if device_id and not cust.get("deviceId"):
                cust["deviceId"] = device_id
            if client_ip:
                cust["lastIp"] = client_ip
            log_user_activity(tech_id, "LOGIN", "SUCCESS", f"Logged in (Wallet: ₹{cust.get('balance', 0)})")
        else:
            is_verified_restore = False
            req_r_bal = max(0, int(body.get("restoredBalance") or 0))
            req_r_ords = max(0, int(body.get("restoredTotalOrders") or 0))
            req_r_rc = str(body.get("restoredReferralCode") or "").strip().upper()
            if body.get("restoreFromApk") is True:
                is_verified_restore = verify_restore_sig(tech_id, req_r_bal, req_r_ords, req_r_rc, str(body.get("restoreSig") or ""))
            restored_bal = req_r_bal if is_verified_restore else 0
            restored_ords = req_r_ords if is_verified_restore else 0
            restored_rc = req_r_rc if is_verified_restore else ""
            ref_code_in = str(body.get("referralCode") or "").strip()
            referrer_obj = None
            if ref_code_in and body.get("restoreFromApk") is not True:
                referrer_obj = find_referrer_by_code(ref_code_in, exclude_tech_id=tech_id)
            cust = {
                "techId": tech_id,
                "password": password,
                "name": str(body.get("name") or f"Tech_{tech_id[-4:]}"),
                "balance": restored_bal,
                "totalOrders": restored_ords,
                "rechargeCount": 0,
                "referralCode": restored_rc if re.fullmatch(r"RS\d{3,}", restored_rc) else "",
                "referredBy": str(referrer_obj.get("techId")) if referrer_obj else "",
                "deviceId": device_id,
                "lastIp": client_ip,
                "referralMilestonesPaid": [],
                "referralEarnedRupees": 0,
                "referralEarnedReach": 0.0,
                "createdAt": datetime.utcfromtimestamp(time.time() + 19800).strftime("%Y-%m-%d %H:%M"),
            }
            ensure_customer_referral_code(cust)
            customers.insert(0, cust)
            if referrer_obj:
                award_referral_milestone(cust, "REG", device_id=device_id, client_ip=client_ip)
            # Also restore any local orders of this customer if server restarted
            inc_cust_orders = body.get("restoredOrders")
            if body.get("restoreFromApk") is True and isinstance(inc_cust_orders, list):
                existing_oids = {int(o.get("id", 0)) for o in STATE.setdefault("orders", []) if o.get("id")}
                for ro in reversed(inc_cust_orders):
                    if isinstance(ro, dict) and ro.get("id") and int(ro["id"]) not in existing_oids:
                        STATE["orders"].insert(0, ro)
                        existing_oids.add(int(ro["id"]))
            log_user_activity(tech_id, "LOGIN", "SUCCESS", "New Technician ID Registered")
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
            "referral": build_customer_referral_summary(tech_id),
            "restoreSig": make_restore_sig(tech_id, int(cust.get("balance", 0)), int(cust.get("totalOrders", 0)), ensure_customer_referral_code(cust)),
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


def append_fc_log(msg: str) -> None:
    """Appends a timestamped entry to STATE['fc_logs'] (keeps latest 30 entries) for the Admin Page Live Logs viewer."""
    ist_now = datetime.utcfromtimestamp(time.time() + 19800).strftime("%I:%M:%S %p")
    entry = f"[{ist_now}] {msg}"
    logger.info("FC_API: %s", msg)
    logs = STATE.setdefault("fc_logs", [])
    logs.insert(0, entry)
    if len(logs) > 30:
        del logs[30:]


def log_user_activity(tech_id: str, category: str, status: str, detail: str) -> None:
    """
    Records every ID's attempt, success, or failure (LOGIN / PAYMENT / ORDER) in STATE['activity_logs']
    and increments per-user try/pass/fail counters so Admin can see which ID tried how many times and what failed!
    category: 'PAYMENT' | 'ORDER' | 'LOGIN'
    status:   'TRY' | 'SUCCESS' | 'FAILED'
    """
    ist_now = datetime.utcfromtimestamp(time.time() + 19800).strftime("%I:%M:%S %p")
    tid = str(tech_id or "UNKNOWN").strip()

    # Update per-customer counters if user exists
    cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == tid), None)
    if cust:
        cust["lastAction"] = f"[{ist_now}] {category} {status}: {detail}"
        if category == "PAYMENT":
            if status == "TRY":
                cust["payTries"] = int(cust.get("payTries", 0)) + 1
            elif status == "SUCCESS":
                cust["paySuccess"] = int(cust.get("paySuccess", 0)) + 1
            elif status == "FAILED":
                cust["payFailed"] = int(cust.get("payFailed", 0)) + 1
        elif category == "ORDER":
            if status == "TRY":
                cust["orderTries"] = int(cust.get("orderTries", 0)) + 1
            elif status == "SUCCESS":
                cust["orderSuccess"] = int(cust.get("orderSuccess", 0)) + 1
            elif status == "FAILED":
                cust["orderFailed"] = int(cust.get("orderFailed", 0)) + 1
        elif category == "LOGIN":
            if status == "FAILED":
                cust["loginFailed"] = int(cust.get("loginFailed", 0)) + 1

    act_logs = STATE.setdefault("activity_logs", [])
    act_logs.insert(0, {
        "time": ist_now,
        "techId": tid,
        "category": category,
        "status": status,
        "detail": detail,
    })
    if len(act_logs) > 100:
        del act_logs[100:]


def _extract_all_fc_tx_dicts(obj) -> list[dict]:
    """
    Recursively extracts transaction dicts from Freecharge's JSON response
    (handles data.globalTransactions, data.transactions, data.txnList, data.history, etc.).
    """
    found = []
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and any(
                k in item for k in ("amount", "txnAmount", "totalAmount", "txnId", "fcTxnId", "upiTxnId", "rrn", "title", "subtitle", "billerName")
            ):
                found.append(item)
            else:
                found.extend(_extract_all_fc_tx_dicts(item))
    elif isinstance(obj, dict):
        # Prioritize known transaction array keys
        for key in ("globalTransactions", "transactions", "txnList", "history", "items", "results", "data"):
            val = obj.get(key)
            if isinstance(val, (list, dict)):
                found.extend(_extract_all_fc_tx_dicts(val))
        for k, val in obj.items():
            if k not in ("globalTransactions", "transactions", "txnList", "history", "items", "results", "data") and isinstance(val, (list, dict)):
                found.extend(_extract_all_fc_tx_dicts(val))
    return found


def _parse_fc_amount(tx: dict) -> float:
    """Extracts float amount from any Freecharge transaction dict field."""
    for k in ("amount", "txnAmount", "totalAmount", "netAmount", "paidAmount", "value"):
        raw = tx.get(k)
        if raw is not None:
            cleaned = re.sub(r"[^0-9.]", "", str(raw))
            if cleaned:
                try:
                    return round(float(cleaned), 2)
                except Exception:
                    pass
    # Fallback: search inside title/subtitle/json for Rs./₹ amount
    tx_str = json.dumps(tx)
    m = re.search(r"(?:rs\.?|inr|₹)\s*([0-9]+(?:\.[0-9]{1,2})?)", tx_str, re.IGNORECASE)
    if m:
        try:
            return round(float(m.group(1)), 2)
        except Exception:
            pass
    return 0.0


# ================= GMAIL IMAP 24x7 PAYMENT GATEWAY ENGINE (PORTED FROM server.js) =================
GMAIL_IMAP_LOCK = threading.Lock()
GMAIL_MAIL_CACHE: dict[str, dict] = {}  # "folder:uid" -> {"low": str, "norm": str, "msg_ts": float, "uid_key": str, "subject": str, "sender": str}
LAST_GMAIL_VERIFY_AT = 0.0
LAST_EMPTY_CREDS_WARN_AT = 0.0


def _html_to_text(h: str) -> str:
    """Ported from server.js htmlToText(): strips <style>/<script>/tags and normalizes entities without breaking split numbers."""
    if not h:
        return ""
    s = re.sub(r"<(style|script)[\s\S]*?</\1>", " ", str(h), flags=re.IGNORECASE)
    # Remove inline formatting tags without adding spaces so <span>20</span><span>.01</span> stays 20.01
    s = re.sub(r"</?(?:span|b|strong|i|em|font|small|sup|sub)\b[^>]*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#8377;", "₹").replace("&rupee;", "₹")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _decode_mime_words(s: str) -> str:
    if not s:
        return ""
    out = []
    for part, enc in decode_header(s):
        if isinstance(part, bytes):
            try:
                out.append(part.decode(enc or "utf-8", errors="ignore"))
            except Exception:
                out.append(part.decode("latin1", errors="ignore"))
        else:
            out.append(str(part))
    return "".join(out)


def _extract_email_full_text(msg) -> tuple[str, float, str, str]:
    """Extracts (combined_text, timestamp_seconds, subject, sender) from an email.message.Message object."""
    subj = _decode_mime_words(msg.get("Subject", ""))
    sender = _decode_mime_words(msg.get("From", ""))
    date_hdr = msg.get("Date", "")
    msg_ts = 0.0
    if date_hdr:
        try:
            dt = parsedate_to_datetime(date_hdr)
            msg_ts = dt.timestamp()
        except Exception:
            msg_ts = 0.0

    parts_text = [subj, sender]
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition") or "")
            if "attachment" in cdisp.lower():
                continue
            if ctype in ("text/plain", "text/html"):
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        decoded = payload.decode(charset, errors="ignore")
                        parts_text.append(_html_to_text(decoded) if ctype == "text/html" else decoded)
                except Exception:
                    pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="ignore")
                parts_text.append(_html_to_text(decoded))
        except Exception:
            pass

    full_txt = " \n ".join(p for p in parts_text if p)[:40000]
    return full_txt, msg_ts, subj, sender


def _amount_matches_server_js(text: str, amount: float) -> bool:
    """
    True if the exact amount appears (e.g. 20.01 or ₹ 20.01 or 20 .01, without matching 200 or 120).
    """
    forms = {f"{amount:.2f}", str(round(amount, 2))}
    if round(amount, 2) == int(amount):
        forms.add(str(int(amount)))
    for f_str in forms:
        esc = re.escape(f_str).replace(r"\.", r"\s*\.\s*")
        pattern = rf"(?<!\d)(?<!\d[.,]){esc}(?!\d)(?![.,]\d)"
        if re.search(pattern, text):
            return True
    return False


def scan_gmail_imap_for_payments(
    expected_unique_amt: float | None = None,
    session_id: str | None = None,
    session_created_at: float = 0.0,
    force_scan: bool = False,
) -> tuple[bool, int, str]:
    """
    Connects to `imap.gmail.com:993` using `gmailUser` + `gmailAppPassword`,
    scans `INBOX` and `[Gmail]/Spam`, logs every step in `fc_logs` (`Admin Live Logs`),
    and matches Paytm Business / Freecharge / Bank emails by Order ID (`JPW...`) or Unique Paise (`20.01`)!
    """
    global LAST_GMAIL_VERIFY_AT, LAST_EMPTY_CREDS_WARN_AT
    gmail_user = (STATE["settings"].get("gmailUser") or os.environ.get("GMAIL_USER") or "").strip()
    gmail_pass = (STATE["settings"].get("gmailAppPassword") or os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    if gmail_pass == "****************":
        gmail_pass = ""

    if not gmail_user or not gmail_pass:
        now_w = time.time()
        if force_scan or (now_w - LAST_EMPTY_CREDS_WARN_AT) > 15.0:
            LAST_EMPTY_CREDS_WARN_AT = now_w
            append_fc_log(
                f"❌ [IMAP] Gmail credentials missing on server (User={'SET' if gmail_user else 'EMPTY'}, Pass={'SET' if gmail_pass else 'EMPTY'})! "
                f"Go to Admin tab -> Enter Gmail & 16-digit App Password -> Click Test & Save IMAP!"
            )
        return False, 0, ""

    now_ts = time.time()
    # Space live network IMAP fetches at least 3.5 seconds apart unless force_scan=True
    if not force_scan and (now_ts - LAST_GMAIL_VERIFY_AT) < 2.0 and GMAIL_MAIL_CACHE:
        return _match_cached_gmail_messages(expected_unique_amt, session_id, session_created_at, log_miss=False)

    with GMAIL_IMAP_LOCK:
        LAST_GMAIL_VERIFY_AT = time.time()
        conn = None
        try:
            conn = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=10)
            conn.login(gmail_user, gmail_pass)

            folders_to_check = ["INBOX"]
            total_checked = 0
            new_fetched = 0

            for folder in folders_to_check:
                try:
                    st, count_data = conn.select(folder, readonly=True)
                    if st != "OK" or not count_data or not count_data[0]:
                        append_fc_log(f"⚠️ [IMAP] Could not open {folder}: {st}")
                        continue
                    total_msgs = int(count_data[0].decode("utf-8", errors="ignore").strip() or "0")
                    if total_msgs <= 0:
                        append_fc_log(f"ℹ️ [IMAP] {folder} is empty (0 messages).")
                        continue

                    # Fetch the newest 10 emails directly by sequence number (total_msgs down to total_msgs-9)
                    # -> ZERO 'SEARCH' command needed, works on 50,000+ email inboxes in 0.1s!
                    start_seq = max(1, total_msgs - 9)
                    seq_list = list(range(total_msgs, start_seq - 1, -1))  # Newest email FIRST!
                    total_checked += len(seq_list)

                    for seq_num in seq_list:
                        # Step 1: Fetch tiny Message-ID header (~80 bytes) to check cache first!
                        st_h, hdr_data = conn.fetch(str(seq_num), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                        msg_id_hdr = f"SEQ_{seq_num}"
                        if st_h == "OK" and hdr_data:
                            for h_item in hdr_data:
                                if isinstance(h_item, tuple) and len(h_item) >= 2 and isinstance(h_item[1], bytes):
                                    hdr_msg = email.message_from_bytes(h_item[1])
                                    raw_mid = str(hdr_msg.get("Message-ID") or "").strip()
                                    if raw_mid:
                                        msg_id_hdr = raw_mid
                                    break
                        cache_key = f"{folder}:{msg_id_hdr}"
                        if cache_key in GMAIL_MAIL_CACHE:
                            continue

                        # Step 2: Only download full body if not already in GMAIL_MAIL_CACHE!
                        st_f, msg_data = conn.fetch(str(seq_num), "(BODY.PEEK[])")
                        if st_f == "OK" and msg_data:
                            for item in msg_data:
                                if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                                    msg_obj = email.message_from_bytes(item[1])
                                    full_txt, m_ts, subj, sender = _extract_email_full_text(msg_obj)
                                    low = full_txt.lower()
                                    norm = re.sub(r"[^a-z0-9]", "", low)
                                    jpw_ids = re.findall(r"jpw[a-z0-9]{8,28}", low)
                                    GMAIL_MAIL_CACHE[cache_key] = {
                                        "low": low,
                                        "norm": norm,
                                        "msg_ts": m_ts,
                                        "uid_key": f"GMAIL_{re.sub(r'[^A-Za-z0-9]', '', msg_id_hdr)[:36]}",
                                        "subject": subj[:80],
                                        "sender": sender[:50],
                                        "jpw_ids": jpw_ids,
                                    }
                                    new_fetched += 1
                                    break
                except Exception as folder_exc:
                    append_fc_log(f"⚠️ [IMAP Folder {folder}] {folder_exc}")

            if len(GMAIL_MAIL_CACHE) > 600:
                keys_to_drop = list(GMAIL_MAIL_CACHE.keys())[:-400]
                for k in keys_to_drop:
                    GMAIL_MAIL_CACHE.pop(k, None)

            if force_scan or new_fetched > 0:
                latest_entries = list(GMAIL_MAIL_CACHE.values())[-3:]
                sample_info = [
                    f"{v.get('subject', 'NoSubj')[:32]}" + (f" [ID:{v['jpw_ids'][0].upper()}]" if v.get("jpw_ids") else "")
                    for v in latest_entries
                ]
                append_fc_log(
                    f"✅ [IMAP OK] ({gmail_user}) | Checked {total_checked} mails ({new_fetched} new) | Latest: {' || '.join(sample_info) if sample_info else 'Empty'}"
                )
        except Exception as exc:
            append_fc_log(f"❌ [IMAP LOGIN/FETCH ERROR] ({gmail_user}): {exc}")
            return False, 0, ""
        finally:
            if conn:
                try:
                    conn.logout()
                except Exception:
                    pass

    return _match_cached_gmail_messages(expected_unique_amt, session_id, session_created_at, log_miss=True)


def _match_cached_gmail_messages(
    expected_unique_amt: float | None,
    session_id: str | None,
    session_created_at: float = 0.0,
    log_miss: bool = False,
) -> tuple[bool, int, str]:
    """Matches cached Gmail messages against active payment session (by Order ID JPW... or Unique Decimal Amount 20.01)."""
    if expected_unique_amt is None and not session_id:
        return False, 0, ""

    used_utrs = STATE.setdefault("used_utrs", [])
    sid_low = str(session_id or "").strip().lower()
    sid_norm = re.sub(r"[^a-z0-9]", "", sid_low)
    base_int_amt = int(float(expected_unique_amt or 20))

    # Inspect newest cached emails first
    for cache_key, m in reversed(list(GMAIL_MAIL_CACHE.items())):
        uid_key = m.get("uid_key") or cache_key
        if uid_key in used_utrs:
            continue

        low = m.get("low") or ""
        norm = m.get("norm") or ""

        # Extract 12-digit UTR from email if present
        utr_m = re.findall(r"\b([1-9]\d{11})\b", low)
        ref_id = utr_m[0] if utr_m else uid_key
        if ref_id in used_utrs or uid_key in used_utrs:
            continue

        # Match 1 (Exact Order ID `JPW...` + exact amount inside email — e.g. Paytm 'Order ID: JPW123T484622899' & '₹ 1'):
        if sid_low and len(sid_low) >= 8 and (sid_low in low or sid_norm in norm):
            text_without_id = low.replace(sid_low, " ")
            if expected_unique_amt is None or _amount_matches_server_js(text_without_id, float(expected_unique_amt)) or _amount_matches_server_js(text_without_id, float(base_int_amt)):
                append_fc_log(
                    f"🎉 [IMAP MATCHED ORDER ID] {session_id} (₹{base_int_amt}) in email '{m.get('subject','')[:40]}'!"
                )
                if uid_key != ref_id and uid_key not in used_utrs:
                    used_utrs.append(uid_key)
                return True, base_int_amt, ref_id

        # If this email already belongs to a DIFFERENT JPW Order ID, never match it by generic amount!
        if m.get("jpw_ids"):
            continue

        # For amount-only matching (bank emails without Order ID), email must have arrived AFTER session was created (minus 15s clock-skew buffer)
        msg_ts = float(m.get("msg_ts") or 0.0)
        if session_created_at > 0 and (msg_ts <= 0 or msg_ts < (session_created_at - 15)):
            continue

        # Match 2 (Fallback for Bank emails without Order ID):
        if expected_unique_amt is not None:
            exact_str = f"{float(expected_unique_amt):.2f}"
            has_pay_word = any(w in low for w in ("received", "credited", "paid", "payment", "success", "upi"))
            if has_pay_word and _amount_matches_server_js(low, float(expected_unique_amt)):
                if not exact_str.endswith(".00"):
                    append_fc_log(
                        f"🎉 [IMAP MATCHED AMOUNT] ₹{exact_str} in email '{m.get('subject','')[:40]}'! Crediting +₹{base_int_amt}"
                    )
                    if uid_key != ref_id and uid_key not in used_utrs:
                        used_utrs.append(uid_key)
                    return True, base_int_amt, ref_id

    if log_miss and session_id:
        append_fc_log(
            f"⏳ [IMAP CHECK] Waiting for Order {session_id} / ₹{float(expected_unique_amt or 20):.2f} (Cached mails: {len(GMAIL_MAIL_CACHE)})"
        )
    return False, 0, ""


def check_freecharge_transactions(
    expected_utr: str | None = None,
    expected_unique_amt: float | None = None,
    session_id: str | None = None,
    tech_id: str | None = None,
    is_manual_test: bool = False,
) -> tuple[bool, int, str]:
    """
    Checks the Admin's Freecharge account transaction history 24x7 using their `app_fc` session cookie.
    Fixed bug where `data.get('data')` was a dict (`{'globalTransactions': [...]}`) and skipped list iteration!
    Also logs every response status and transaction preview to `STATE['fc_logs']` for the Admin Dashboard.
    """
    fc_cookie = (STATE["settings"].get("freechargeCookie") or os.environ.get("FREECHARGE_COOKIE") or "").strip()
    if not fc_cookie:
        append_fc_log("⚠️ app_fc cookie is empty! Paste app_fc in Admin tab & Save.")
        return False, 0, ""

    # Clean cookie formatting: if user pasted just the raw value, wrap as app_fc=<val>
    if "app_fc=" in fc_cookie:
        cookie_hdr = fc_cookie
    elif "=" in fc_cookie and ";" in fc_cookie:
        cookie_hdr = fc_cookie
    else:
        cookie_hdr = f"app_fc={fc_cookie}"

    headers = {
        "Cookie": cookie_hdr,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
        "Origin": "https://www.freecharge.in",
        "Referer": "https://www.freecharge.in/transactions",
    }

    endpoints = [
        ("POST", "https://www.freecharge.in/thv/listv3?fcAppType=MSITE", {"isAndroid": False}),
        ("GET", "https://www.freecharge.in/thv/listv3?fcAppType=MSITE", None),
        ("POST", "https://www.freecharge.in/thv/listv3?fcAppType=ANDROID", {"isAndroid": True}),
    ]
    used_utrs = STATE.setdefault("used_utrs", [])

    for method, url, payload in endpoints:
        try:
            if method == "POST":
                resp = requests.post(url, json=payload, headers=headers, timeout=10)
            else:
                resp = requests.get(url, headers=headers, timeout=10)

            status_code = resp.status_code
            raw_text = (resp.text or "").strip()

            if not resp.ok:
                append_fc_log(f"❌ {method} {url.split('.in')[-1]} -> HTTP {status_code} | Resp: {raw_text[:120]}")
                continue

            try:
                data = resp.json() if resp.content else {}
            except Exception:
                append_fc_log(f"⚠️ Non-JSON from {url.split('.in')[-1]} (HTTP {status_code}): {raw_text[:120]}")
                continue

            tx_list = _extract_all_fc_tx_dicts(data)
            if not tx_list:
                append_fc_log(f"ℹ️ HTTP {status_code} OK but 0 txns extracted. Keys: {list(data.keys()) if isinstance(data, dict) else 'non-dict'} | Raw: {raw_text[:140]}")
                # Raw text fallback: check if exact decimal amount (e.g. "20.01") is present inside raw JSON!
                if expected_unique_amt is not None:
                    target_str = f"{float(expected_unique_amt):.2f}"
                    if f'"{target_str}"' in raw_text or f":{target_str}" in raw_text or f" {target_str}" in raw_text:
                        utr_cands = re.findall(r"\b([1-9]\d{11})\b", raw_text)
                        fallback_id = next((u for u in utr_cands if u not in used_utrs), f"FC_RAW_{target_str}_{int(time.time()//300)}")
                        if fallback_id not in used_utrs:
                            append_fc_log(f"✅ RAW JSON MATCH! Found ₹{target_str} in response (Ref: {fallback_id})")
                            return True, int(float(expected_unique_amt)), fallback_id
                continue

            # Summarize top 5 recent transactions for Admin Logs
            preview_items = []
            for t in tx_list[:5]:
                a = _parse_fc_amount(t)
                st = str(t.get("status") or t.get("txnStatus") or "OK")
                tid = str(t.get("txnId") or t.get("fcTxnId") or t.get("upiTxnId") or "")[-8:]
                preview_items.append(f"₹{a:.2f}({st}:{tid})")
            append_fc_log(
                f"✅ HTTP {status_code} | {len(tx_list)} txns | Looking for: ₹{expected_unique_amt if expected_unique_amt else expected_utr} | Latest: {', '.join(preview_items)}"
            )

            for tx in tx_list[:20]:
                tx_json = json.dumps(tx)
                status = str(tx.get("status") or tx.get("txnStatus") or tx.get("paymentStatus") or "SUCCESS").upper()
                if any(bad in status for bad in ("FAIL", "DECLINE", "REJECT", "CANCEL", "PENDING")):
                    continue

                amt_val = _parse_fc_amount(tx)
                utr_candidates = re.findall(r"\b([1-9]\d{11})\b", tx_json)
                tx_utr = str(
                    tx.get("upiTxnId")
                    or tx.get("rrn")
                    or tx.get("bankRefNo")
                    or tx.get("utr")
                    or tx.get("txnId")
                    or tx.get("fcTxnId")
                    or tx.get("orderId")
                    or (utr_candidates[0] if utr_candidates else "")
                    or ""
                ).strip()

                if not tx_utr:
                    tx_utr = f"FC_{amt_val:.2f}_{hashlib.md5(tx_json.encode()).hexdigest()[:8]}"

                if tx_utr in used_utrs:
                    continue

                # Match 1: Explicit 12-digit UTR
                if expected_utr and expected_utr in tx_json and amt_val >= 1:
                    append_fc_log(f"🎉 MATCHED UTR {expected_utr} (₹{amt_val:.2f})!")
                    return True, int(amt_val), expected_utr

                # Match 2: Exact Unique Decimal Amount (e.g. ₹20.01, ₹60.01)
                if expected_unique_amt is not None:
                    target_amt = round(float(expected_unique_amt), 2)
                    target_str = f"{target_amt:.2f}"
                    if amt_val == target_amt or f'"{target_str}"' in tx_json or f":{target_str}" in tx_json:
                        append_fc_log(f"🎉 MATCHED AMOUNT ₹{target_str} (Ref: {tx_utr}) for Tech {tech_id}!")
                        return True, int(target_amt), tx_utr

                # Match 3: Session ID or Tech ID in transaction note/remarks
                if session_id and session_id in tx_json and amt_val >= 1:
                    append_fc_log(f"🎉 MATCHED SESSION {session_id} (₹{amt_val:.2f})!")
                    return True, int(amt_val), tx_utr

            # If we got a valid transaction list from the first working endpoint, no need to hit fallback URLs unless manual test
            if not is_manual_test:
                break
        except Exception as exc:
            append_fc_log(f"⚠️ Exception calling {url.split('.in')[-1]}: {exc}")

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


def credit_customer_verified_utr(cust: dict, utr: str, amount: int, source_label: str, extra_locks: list[str] | None = None) -> bool:
    """
    Thread-safe, idempotent wallet credit function protected by PAYMENT_LOCK.
    Returns True if credited now, or False if `utr` or any `extra_locks` (sessionId / linkId) was ALREADY credited!
    """
    with PAYMENT_LOCK:
        used_utrs = STATE.setdefault("used_utrs", [])
        all_keys = [str(k).strip() for k in ([utr] + (extra_locks or [])) if k and str(k).strip()]
        if any(k in used_utrs for k in all_keys):
            return False

        for k in all_keys:
            used_utrs.append(k)

        cust["balance"] = int(cust.get("balance", 0)) + amount
        cust["totalPaidRupees"] = int(cust.get("totalPaidRupees", 0)) + amount
        ref_cfg = get_referral_settings()
        if amount >= int(ref_cfg.get("minRecharge") or 20):
            rc = int(cust.get("rechargeCount", 0)) + 1
            cust["rechargeCount"] = rc
            if rc == 1:
                award_referral_milestone(cust, "R1")
            elif rc == 3:
                award_referral_milestone(cust, "R3")
            elif rc == 6:
                award_referral_milestone(cust, "R6")
            elif rc == 10:
                award_referral_milestone(cust, "R10")
        log_user_activity(
            str(cust.get("techId", "")),
            "PAYMENT",
            "SUCCESS",
            f"+₹{amount} Credited via {source_label} (Ref: {utr}) • New Bal: ₹{cust['balance']}",
        )
        STATE["stats"]["collected"] = int(STATE["stats"].get("collected", 0)) + amount
        STATE["stats"]["cost"] = int(STATE["stats"].get("cost", 0)) + (amount // 20) * 13
        STATE["stats"]["profit"] = STATE["stats"]["collected"] - STATE["stats"]["cost"]
        STATE["pending_payments"] = [p for p in STATE.get("pending_payments", []) if p.get("utr") not in all_keys]
        save_state(STATE)
        return True


def complete_payment_session_atomically(sess: dict, cust: dict, ref_id: str, source_label: str) -> bool:
    """
    Atomically marks `sess` COMPLETED and credits `cust` ONCE AND ONLY ONCE under PAYMENT_LOCK.
    Locks `ref_id` and `sessionId` together in `used_utrs` so concurrent checks never double-credit!
    """
    if not sess or not cust:
        return False

    sid = str(sess.get("sessionId") or "").strip()
    extra_keys = [sid]

    with PAYMENT_LOCK:
        if sess.get("status") == "COMPLETED" or sess.get("credited") is True:
            return False
        used_utrs = STATE.setdefault("used_utrs", [])
        all_keys = [str(k).strip() for k in ([ref_id] + extra_keys) if k and str(k).strip()]
        if any(k in used_utrs for k in all_keys):
            sess["status"] = "COMPLETED"
            sess["credited"] = True
            return False

        for k in all_keys:
            used_utrs.append(k)

        sess["status"] = "COMPLETED"
        sess["credited"] = True
        paid_amount = int(sess.get("baseAmount", 20))
        credit_amount = int(sess.get("walletCredit") or paid_amount)
        reaches_n = int(sess.get("reaches") or max(1, credit_amount // max(1, int(STATE["settings"].get("reachPrice") or 20))))
        cust["balance"] = int(cust.get("balance", 0)) + credit_amount
        cust["totalPaidRupees"] = int(cust.get("totalPaidRupees", 0)) + paid_amount
        ref_cfg = get_referral_settings()
        if paid_amount >= int(ref_cfg.get("minRecharge") or 20):
            rc = int(cust.get("rechargeCount", 0)) + 1
            cust["rechargeCount"] = rc
            if rc == 1:
                award_referral_milestone(cust, "R1")
            elif rc == 3:
                award_referral_milestone(cust, "R3")
            elif rc == 6:
                award_referral_milestone(cust, "R6")
            elif rc == 10:
                award_referral_milestone(cust, "R10")
        log_user_activity(
            str(cust.get("techId", "")),
            "PAYMENT",
            "SUCCESS",
            f"+₹{credit_amount} Credited (Paid ₹{paid_amount}) via {source_label} (Ref: {ref_id}) • New Bal: ₹{cust['balance']}",
        )
        STATE["stats"]["collected"] = int(STATE["stats"].get("collected", 0)) + paid_amount
        STATE["stats"]["cost"] = int(STATE["stats"].get("cost", 0)) + reaches_n * 13
        STATE["stats"]["profit"] = STATE["stats"]["collected"] - STATE["stats"]["cost"]
        STATE["pending_payments"] = [p for p in STATE.get("pending_payments", []) if p.get("utr") not in all_keys]
        save_state(STATE)
        return True


def allocate_unique_amount(base_amount: int) -> float:
    """
    Returns exact round amount (e.g. ₹20.00, ₹60.00, ₹100.00, ₹200.00, ₹400.00).
    Each payment session is uniquely identified by its Order ID (`tr=JPW...`) in the Paytm/Bank confirmation email!
    """
    now_ts = time.time()
    sessions = STATE.setdefault("payment_sessions", {})
    old_keys = [k for k, v in sessions.items() if now_ts - float(v.get("createdAt", 0)) > 86400]
    for k in old_keys:
        sessions.pop(k, None)
    return float(base_amount)


async def api_customer_create_payment(request: web.Request) -> web.Response:
    """Creates a Flipkart/Amazon-style auto-tracked Payment Session (Razorpay Order + Dynamic Unique UPI Session)."""
    try:
        body = await request.json()
        tech_id = str(body.get("techId") or "").strip()
        reach_price = max(1, int(STATE["settings"].get("reachPrice") or 20))
        pkg_prices = STATE["settings"].get("pkgPrices") or {"1": 20, "3": 60, "5": 100, "10": 200, "20": 400}
        reaches_cnt = int(body.get("reaches") or 0)
        if reaches_cnt in (1, 3, 5, 10, 20) and str(reaches_cnt) in pkg_prices:
            base_amt = max(1, int(pkg_prices[str(reaches_cnt)]))
            wallet_credit = reaches_cnt * reach_price
        else:
            base_amt = max(1, int(body.get("amount") or reach_price))
            wallet_credit = max(base_amt, int(body.get("walletCredit") or base_amt))
        if not tech_id:
            return web.json_response({"ok": False, "message": "Login required"}, status=400)

        clean_tid = re.sub(r"[^A-Za-z0-9]", "", tech_id)[:12]
        session_id = f"JPW{clean_tid}T{int(time.time()) % 10000000}{random.randint(10, 99)}"
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

        raw_upi_setting = (STATE["settings"].get("upiId") or "jpwportal@freecharge").strip()
        app_title = (STATE["settings"].get("appTitle") or "JPW REACH").strip()

        # Check if Admin pasted a full Merchant QR string (e.g. upi://pay?pa=Q...@ybl&pn=...&mc=5411...)
        mc_code = ""
        if raw_upi_setting.lower().startswith("upi://"):
            pa_m = re.search(r"[?&]pa=([^&]+)", raw_upi_setting, re.IGNORECASE)
            mc_m = re.search(r"[?&]mc=([^&]+)", raw_upi_setting, re.IGNORECASE)
            upi_id = pa_m.group(1) if pa_m else raw_upi_setting
            if mc_m:
                mc_code = mc_m.group(1)
        else:
            upi_id = raw_upi_setting

        # Build clean NPCI-compliant URI (include `&tr={session_id}` for Merchant VPAs like @pty/@freecharge so Gmail IMAP matches both Order ID and ₹20.01!)
        mc_part = f"&mc={quote(mc_code)}" if mc_code else ""
        tr_part = f"&tr={quote(session_id)}"
        upi_uri = (
            f"upi://pay?pa={quote(upi_id)}&pn={quote(app_title)}"
            f"{mc_part}&am={unique_amt:.2f}{tr_part}&cu=INR"
        )

        sess_obj = {
            "sessionId": session_id,
            "techId": tech_id,
            "baseAmount": base_amt,
            "walletCredit": wallet_credit,
            "reaches": reaches_cnt or max(1, wallet_credit // reach_price),
            "uniqueAmount": unique_amt,
            "upiId": upi_id,
            "appTitle": app_title,
            "upiUri": upi_uri,
            "rzpKeyId": rzp_key_id if rzp_order_id else None,
            "rzpOrderId": rzp_order_id,
            "status": "PENDING",
            "createdAt": time.time(),
            "timeStr": datetime.utcfromtimestamp(time.time() + 19800).strftime("%d %b, %I:%M %p"),
        }
                # Keep older PENDING sessions active (each has its own unique Order ID JPW...)
        STATE.setdefault("payment_sessions", {})[session_id] = sess_obj
        log_user_activity(
            tech_id,
            "PAYMENT",
            "TRY",
            f"Initiated ₹{base_amt} Pack (Exact: ₹{unique_amt:.2f} • Order #{session_id[-6:]})",
        )
        save_state(STATE)
        return web.json_response({"ok": True, "session": sess_obj, "payments": get_customer_payment_history(tech_id)})
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
        force_flag = bool(body.get("forceScan"))
        sessions = STATE.setdefault("payment_sessions", {})
        sess = sessions.get(session_id)
        if not sess and session_id.startswith("JPW"):
            tid_m = re.match(r"^JPW([A-Za-z0-9]+)T\d+$", session_id)
            rec_tid = str(body.get("techId") or (tid_m.group(1) if tid_m else "")).strip()
            reach_p = max(1, int(STATE["settings"].get("reachPrice") or 20))
            pkg_p = STATE["settings"].get("pkgPrices") or {}
            rec_amt = max(1, int(float(body.get("amount") or reach_p)))
            w_cred = rec_amt
            r_cnt = max(1, w_cred // reach_p)
            for rk, pv in pkg_p.items():
                if int(pv) == rec_amt:
                    r_cnt = int(rk)
                    w_cred = r_cnt * reach_p
                    break
            if rec_tid:
                sess = {
                    "sessionId": session_id,
                    "techId": rec_tid,
                    "baseAmount": rec_amt,
                    "walletCredit": w_cred,
                    "reaches": r_cnt,
                    "uniqueAmount": float(rec_amt),
                    "status": "PENDING",
                    "createdAt": time.time() - 60,
                }
                sessions[session_id] = sess
        if not sess:
            return web.json_response({"ok": False, "status": "NOT_FOUND"}, status=404)

        tech_id = str(sess.get("techId"))
        customers = STATE.setdefault("customers", [])
        cust = next((c for c in customers if str(c.get("techId")) == tech_id), None)
        if not cust and tech_id:
            cust = {
                "techId": tech_id,
                "password": "123",
                "name": f"Tech_{tech_id[-4:]}",
                "balance": 0,
                "totalOrders": 0,
                "createdAt": datetime.utcfromtimestamp(time.time() + 19800).strftime("%Y-%m-%d %H:%M"),
            }
            customers.insert(0, cust)

        if sess.get("status") == "COMPLETED":
            return web.json_response({
                "ok": True,
                "status": "COMPLETED",
                "message": f"🎉 ₹{sess['baseAmount']} Added to Wallet!",
                "customer": cust,
                "payments": get_customer_payment_history(tech_id),
            })

        if body.get("cancel") is True:
            if sess.get("status") == "PENDING":
                sess["status"] = "FAILED"
                log_user_activity(tech_id, "PAYMENT", "FAILED", f"Order #{session_id} (₹{sess.get('baseAmount', 20)}) Cancelled by user")
                save_state(STATE)
            return web.json_response({
                "ok": True,
                "status": "FAILED",
                "customer": cust,
                "payments": get_customer_payment_history(tech_id),
            })

        # Check 0: 24x7 Gmail IMAP Auto-Verifier (always call so missing-credential warnings are logged too!)
        if cust:
            gm_ok, gm_amt, gm_ref = await asyncio.to_thread(
                scan_gmail_imap_for_payments,
                float(sess["uniqueAmount"]),
                session_id,
                float(sess.get("createdAt", 0.0)),
                force_flag,
            )
            if gm_ok:
                complete_payment_session_atomically(sess, cust, gm_ref, "Gmail IMAP Auto-Verify")
                if ADMIN_CHAT_ID:
                    asyncio.create_task(
                        asyncio.to_thread(
                            send_message,
                            ADMIN_CHAT_ID,
                            f"📧 <b>Gmail IMAP Payment Auto-Verified!</b>\n"
                            f"👤 Tech ID: <code>{tech_id}</code>\n"
                            f"💰 Credited: <b>+₹{sess['baseAmount']}</b> (Paid: ₹{sess['uniqueAmount']:.2f})\n"
                            f"🧾 Ref: <code>{gm_ref}</code>\n"
                            f"💳 New Balance: ₹{cust['balance']}",
                        )
                    )
                return web.json_response({
                    "ok": True,
                    "status": "COMPLETED",
                    "message": f"🎉 ₹{sess['baseAmount']} Payment Verified by Bank! Wallet Updated!",
                    "customer": cust,
                })

        # Check 1: Live Freecharge 24x7 Cloud Checker (only if freechargeCookie is configured)
        if cust and (STATE["settings"].get("freechargeCookie") or os.environ.get("FREECHARGE_COOKIE")):
            fc_ok, fc_amt, fc_utr = await asyncio.to_thread(
                check_freecharge_transactions,
                None,
                float(sess["uniqueAmount"]),
                session_id,
                tech_id,
            )
            if fc_ok:
                complete_payment_session_atomically(sess, cust, fc_utr, "Freecharge 24x7 Auto-Detect")
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
                            complete_payment_session_atomically(sess, cust, bank_ref, "BharatPe Auto-Detect")
                            return web.json_response({
                                "ok": True,
                                "status": "COMPLETED",
                                "message": f"🎉 ₹{sess['baseAmount']} Auto-Detected! Wallet Updated!",
                                "customer": cust,
                            })
            except Exception as exc:
                logger.warning("BharatPe poll check error: %s", exc)

        # Check if webhook already recorded this exact decimal amount (e.g. AMT_20.01)
        amt_key = f"AMT_{float(sess['uniqueAmount']):.2f}"
        v_bank = STATE.setdefault("verified_bank_utrs", {})
        if amt_key in v_bank and cust:
            v_bank.pop(amt_key, None)
            complete_payment_session_atomically(sess, cust, f"FC_{int(time.time())}", "Freecharge BIZ Webhook")
            return web.json_response({
                "ok": True,
                "status": "COMPLETED",
                "message": f"🎉 ₹{sess['baseAmount']} Added to Wallet!",
                "customer": cust,
            })

        latest_log = (STATE.get("fc_logs") or [""])[0]
        return web.json_response({"ok": True, "status": "PENDING", "customer": cust, "diag": latest_log})
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


async def api_admin_gmail_test(request: web.Request) -> web.Response:
    """Saves Gmail User & 16-digit App Password, runs an immediate forced IMAP scan, and matches any pending sessions (like /api/admin/recheck in server.js)."""
    try:
        body = await request.json()
        g_user = str(body.get("gmailUser") or "").strip()
        g_pass = str(body.get("gmailAppPassword") or "").replace(" ", "").strip()
        if g_user:
            STATE["settings"]["gmailUser"] = g_user
        if g_pass and g_pass != "****************":
            STATE["settings"]["gmailAppPassword"] = g_pass
        STATE["settings"]["updatedAt"] = int(time.time() * 1000)
        save_state(STATE)
    except Exception:
        pass

    await asyncio.to_thread(sync_pending_sessions_via_gmail_imap, True)
    return web.json_response({
        "ok": True,
        "logs": STATE.get("fc_logs", [])[:30],
        "pendingSessions": list(STATE.get("payment_sessions", {}).values())[-15:],
        "customers": STATE.get("customers", []),
    })


async def api_admin_mark_session(request: web.Request) -> web.Response:
    """Ported from POST /api/admin/orders/:orderId/mark in server.js (admin.html): 1-click Mark Paid or Mark Failed on a payment session."""
    try:
        body = await request.json()
        sid = str(body.get("sessionId") or "").strip()
        new_st = str(body.get("status") or "").strip().upper()
        sessions = STATE.setdefault("payment_sessions", {})
        sess = sessions.get(sid)
        if not sess:
            return web.json_response({"ok": False, "message": "Session not found"}, status=404)

        tid = str(sess.get("techId") or "")
        cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == tid), None)
        if new_st in ("SUCCESS", "COMPLETED"):
            if cust:
                complete_payment_session_atomically(sess, cust, f"ADMIN_MARK_{sid}", "Admin Manual Approval")
                append_fc_log(f"✅ Admin Marked Session {sid} PAID (+₹{sess.get('baseAmount', 20)} to ID {tid})")
        elif new_st == "FAILED":
            sess["status"] = "FAILED"
            log_user_activity(tid, "PAYMENT", "FAILED", f"Session {sid} marked FAILED by Admin")
            save_state(STATE)

        return web.json_response({
            "ok": True,
            "pendingSessions": list(STATE.get("payment_sessions", {}).values())[-15:],
            "customers": STATE.get("customers", []),
            "activityLogs": STATE.get("activity_logs", [])[:100],
            "logs": STATE.get("fc_logs", [])[:30],
        })
    except Exception as exc:
        return web.json_response({"ok": False, "message": str(exc)}, status=400)


async def api_admin_fc_test(request: web.Request) -> web.Response:
    """Tests the saved Freecharge `app_fc` cookie right now and returns live diagnostic logs & recent transactions."""
    try:
        body = await request.json()
        if body.get("freechargeCookie"):
            STATE["settings"]["freechargeCookie"] = str(body["freechargeCookie"]).strip()
            STATE["settings"]["updatedAt"] = int(time.time() * 1000)
            save_state(STATE)
    except Exception:
        pass

    await asyncio.to_thread(check_freecharge_transactions, None, 20.01, None, "ADMIN_TEST", True)
    return web.json_response({
        "ok": True,
        "logs": STATE.get("fc_logs", [])[:30],
    })


async def api_admin_fc_logs(request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "logs": STATE.get("fc_logs", [])[:30],
        "activityLogs": STATE.get("activity_logs", [])[:100],
        "customers": STATE.get("customers", []),
        "pendingSessions": list(STATE.get("payment_sessions", {}).values())[-10:],
    })


async def api_admin_clear_logs(request: web.Request) -> web.Response:
    STATE["activity_logs"] = []
    STATE["fc_logs"] = []
    save_state(STATE)
    return web.json_response({"ok": True})


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
    Receives real-time Freecharge BIZ / Bank / UPI Notification or SMS (via GET or POST)!
    Matches 'You have received Rs. 20.01 from ... via UPI' even when no 12-digit UTR is in the notification!
    """
    try:
        q_text = " ".join(f"{k}={v}" for k, v in request.query.items())
        if request.method == "GET":
            raw_text = q_text
        elif request.content_type == "application/json":
            body = await request.json()
            extra_meta = f" {body.get('sessionId', '')} {body.get('techId', '')} {body.get('source', '')} "
            raw_text = str(
                body.get("text")
                or body.get("notification")
                or body.get("title")
                or body.get("message")
                or body.get("sms")
                or body.get("body")
                or json.dumps(body)
            ) + extra_meta + q_text
        else:
            raw_text = (await request.text()) + " " + q_text

        logger.info("Incoming Payment Webhook: %s", raw_text[:250])
        append_fc_log(f"🔔 Webhook Received: {raw_text[:140]}")

        # Extract 12-digit UTR number if present & block already-used UTRs / duplicate SMS replays!
        utr_matches = re.findall(r"\b([1-9]\d{11})\b", raw_text)
        used_utrs_list = STATE.setdefault("used_utrs", [])
        if utr_matches and all(u in used_utrs_list for u in utr_matches):
            append_fc_log(f"⚠️ Ignored Replay: UTR {utr_matches[0]} was already credited earlier.")
            return web.json_response({"ok": False, "message": "UTR already used"}, status=400)
        # Extract exact decimal amount (e.g. 'Rs. 20.01', 'INR 20.02', '₹20.01')
        amt_match = re.search(r"(?:rs\.?|inr|₹|received)\s*([0-9]+(?:\.[0-9]{1,2})?)", raw_text, re.IGNORECASE)
        if not amt_match:
            amt_match = re.search(r"\b([0-9]+\.[0-9]{2})\b", raw_text)
        exact_float_amt = round(float(amt_match.group(1)), 2) if amt_match else 0.0
        detected_amt = int(exact_float_amt) if exact_float_amt >= 1 else 20

        # AUTO-MATCH ACTIVE PAYMENT SESSION BY UNIQUE DECIMAL AMOUNT (e.g. 20.01, 20.02) OR SESSION ID!
        matched_session_user = None
        sessions = STATE.setdefault("payment_sessions", {})
        # First try exact unique decimal match (e.g., 20.01 == 20.01)
        for sid, sess in list(sessions.items()):
            if sess.get("status") == "PENDING":
                if (
                    (exact_float_amt > 0 and round(float(sess.get("uniqueAmount", 0)), 2) == exact_float_amt)
                    or sid in raw_text
                    or str(sess.get("techId")) in raw_text
                ):
                    tid = sess.get("techId")
                    cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == str(tid)), None)
                    if cust:
                        utr_tag = utr_matches[0] if utr_matches else f"FC_{int(time.time())}_{random.randint(10,99)}"
                        src_lbl = "APK 5s Debit SMS" if "APK_5SEC_DEBIT_SMS" in raw_text else "Webhook Auto-Match"
                        if complete_payment_session_atomically(sess, cust, utr_tag, src_lbl):
                            matched_session_user = f"{tid} (+₹{sess.get('baseAmount', detected_amt)})"
                        else:
                            return web.json_response({"ok": True, "alreadyCredited": True})
                        if ADMIN_CHAT_ID:
                            asyncio.create_task(
                                asyncio.to_thread(
                                    send_message,
                                    ADMIN_CHAT_ID,
                                    f"⚡ <b>Freecharge BIZ Payment Auto-Verified (₹{exact_float_amt:.2f})!</b>\n"
                                    f"👤 Tech ID: <code>{tid}</code> (+₹{sess.get('baseAmount', detected_amt)})\n"
                                    f"💳 New Balance: ₹{cust['balance']}",
                                )
                            )
                        break

        # Fallback: if only 1 pending session exists for that base integer amount (e.g. ₹20) in the last 5 minutes, match it!
        if not matched_session_user and exact_float_amt >= 1:
            now_ts = time.time()
            recent_pending = [
                (sid, s) for sid, s in sessions.items()
                if s.get("status") == "PENDING"
                and int(s.get("baseAmount", 0)) == int(exact_float_amt)
                and (now_ts - float(s.get("createdAt", 0))) < 300
            ]
            if len(recent_pending) == 1:
                sid, sess = recent_pending[0]
                tid = sess.get("techId")
                cust = next((c for c in STATE.get("customers", []) if str(c.get("techId")) == str(tid)), None)
                if cust:
                    utr_tag = utr_matches[0] if utr_matches else f"FC_{int(time.time())}"
                    complete_payment_session_atomically(sess, cust, utr_tag, "Freecharge BIZ Auto-Match")
                    matched_session_user = f"{tid} (+₹{sess.get('baseAmount', detected_amt)})"

        if matched_session_user:
            append_fc_log(f"🎉 Webhook Auto-Credited: {matched_session_user} (Exact: ₹{exact_float_amt:.2f})")
            return web.json_response({"ok": True, "matchedUser": matched_session_user, "amount": exact_float_amt})

        if not utr_matches:
            # Record exact decimal amount in verified_bank_utrs under an amount key so if session starts 1s later it still matches!
            if exact_float_amt > 0:
                STATE.setdefault("verified_bank_utrs", {})[f"AMT_{exact_float_amt:.2f}"] = detected_amt
                save_state(STATE)
                return web.json_response({"ok": True, "recordedAmount": exact_float_amt})
            return web.json_response({"ok": False, "message": "No amount or UTR found in webhook"}, status=400)

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
                "time": datetime.utcfromtimestamp(time.time() + 19800).strftime("%I:%M %p"),
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

        now_ms_chk = int(time.time() * 1000)
        dup_running = next(
            (
                o for o in STATE.get("orders", [])
                if str(o.get("customerTechId") or "") == tech_id
                and str(o.get("targetId") or "") == target_id
                and str(o.get("status") or "").upper() not in ("COMPLETED", "FAILED", "REJECTED", "CANCELLED")
                and (now_ms_chk - int(o.get("createdAtTs") or 0)) < 300000
            ),
            None,
        )
        if dup_running:
            return web.json_response(
                {"ok": False, "message": f"⚠️ Target ID {target_id} ka Reach order abhi chal raha hai! Complete hone ka wait karein."},
                status=400,
            )

        customers = STATE.setdefault("customers", [])
        cust = next((c for c in customers if str(c.get("techId")) == tech_id), None)
        if not cust:
            return web.json_response({"ok": False, "message": "Technician account not found"}, status=404)

        reach_price = max(1, int(STATE["settings"].get("reachPrice") or 20))
        log_user_activity(tech_id, "ORDER", "TRY", f"Attempted Reach Order -> Target: {target_id} (Wallet: ₹{cust.get('balance', 0)}, Cost: ₹{reach_price})")
        if int(cust.get("balance", 0)) < reach_price:
            log_user_activity(tech_id, "ORDER", "FAILED", f"Failed Target {target_id} -> Insufficient Balance (₹{cust.get('balance', 0)} < ₹{reach_price})")
            save_state(STATE)
            return web.json_response(
                {"ok": False, "message": "⚠️ Insufficient Reach (0.00)! Niche Packages se Reach add karein."},
                status=400,
            )

        # Find a backend Cashtree node account that has >= 1.00 Coin (or fallback to highest coin / credit account)
        profiles = STATE.get("profiles", [])
        ready_nodes = [p for p in profiles if float(p.get("jpwCoins", 0.0)) >= 1.0]
        ready_nodes.sort(key=lambda x: float(x.get("jpwCoins", 0.0)), reverse=True)
        node_prof = ready_nodes[0] if ready_nodes else (profiles[0] if profiles else None)

        if not node_prof:
            return web.json_response(
                {"ok": False, "message": "⚠️ Server busy. Please try again shortly."},
                status=500,
            )

        # Place real order on Cashtree using the selected backend node account!
        api_res = await asyncio.to_thread(place_reach_order_on_cashtree, node_prof, target_id, target_pass)
        srv_data = api_res.get("data") or {}
        srv_msg = srv_data.get("message") or srv_data.get("error") or ("Reach Order Successful!" if api_res["ok"] else "Server rejected order")

        if not api_res["ok"]:
            # Do NOT deduct customer's ₹20 if Cashtree server rejected the order (e.g., outside service hours 09:30 AM - 10:15 PM IST)
            log_user_activity(
                tech_id,
                "ORDER",
                "FAILED",
                f"Target {target_id} Rejected by Cashtree ({srv_msg}) • Node: {node_prof['id']}",
            )
            save_state(STATE)
            safe_reaches = int(cust.get("balance", 0)) // reach_price
            return web.json_response(
                {"ok": False, "message": f"⚠️ {srv_msg} (Reach {safe_reaches} safe!)"},
                status=400,
            )

        # Order succeeded on Cashtree! Deduct reach_price from Customer Wallet and 1 Coin from Backend Node
        cust["balance"] = max(0, int(cust.get("balance", 0)) - reach_price)
        cust["totalOrders"] = int(cust.get("totalOrders", 0)) + 1

        u_obj = srv_data.get("user") or {}
        node_prof["jpwCoins"] = float(u_obj.get("jpwCoins", max(0.0, float(node_prof.get("jpwCoins", 0.0)) - 1.0)))

        wo_id = random.randint(7100000000, 7999999999)
        now_str = datetime.utcfromtimestamp(time.time() + 19800).strftime("%I:%M %p")
        ct_ord = srv_data.get("order") or srv_data.get("data") or srv_data or {}
        ct_oid = str(ct_ord.get("_id") or ct_ord.get("id") or "") if isinstance(ct_ord, dict) else ""
        init_status = map_cashtree_order_status(ct_ord) if isinstance(ct_ord, dict) and ct_ord else "PROCESSING"
        wo_init_info = extract_cashtree_workorder_details(ct_ord if isinstance(ct_ord, dict) else {})
        if wo_init_info["workOrderNo"] and wo_init_info["workOrderNo"].isdigit():
            wo_id = int(wo_init_info["workOrderNo"])
        order_obj = {
            "id": wo_id,
            "workOrderNo": wo_init_info["workOrderNo"] or str(wo_id),
            "custName": wo_init_info["custName"],
            "custMobile": wo_init_info["custMobile"],
            "cashtreeOrderId": ct_oid,
            "profileId": node_prof["id"],
            "customerTechId": tech_id,
            "orderPrice": reach_price,
            "targetId": target_id,
            "notes": f"APK User: {tech_id} | Pass: {target_pass}",
            "status": init_status,
            "time": now_str,
            "createdAtTs": int(time.time() * 1000),
        }
        STATE["orders"].insert(0, order_obj)
        log_user_activity(
            tech_id,
            "ORDER",
            "SUCCESS",
            f"Order #{wo_id} Completed -> Target: {target_id} (Used Node: {node_prof['id']}) • Bal Left: ₹{cust['balance']}",
        )
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
    """
    Human-Like Staggered Daily Bonus Scheduler (07:00 AM – 05:00 PM IST):
    1. Does NOT claim at 12:01 AM night. Waits until 07:00 AM IST.
    2. Between 07:00 AM and 04:59 PM IST:
       - Spreads claims out naturally across the day (claims only 1 account per cycle with random
         human-like intervals so accounts are NEVER claimed all at once!).
    3. At 05:00 PM IST (17:00 - 17:45 IST Final Sweep):
       - Automatically checks ALL saved accounts to verify every single account has claimed today's bonus,
         and claims any remaining/missed account so 0 accounts are left behind.
    4. Runs 100% silently in the background (does not spam ADMIN_CHAT_ID).
    """
    await asyncio.sleep(15)
    while True:
        try:
            ist_dt = datetime.utcfromtimestamp(time.time() + 19800)  # Current IST time
            ist_hour = ist_dt.hour
            ist_min = ist_dt.minute

            # Window 1: Between 07:00 AM and 04:59 PM IST -> Staggered mode (1 account at a time, spaced out!)
            if 7 <= ist_hour < 17:
                profiles = STATE.get("profiles", [])
                ready_profiles = [p for p in profiles if get_bonus_cooldown_str(p)[0]]

                if ready_profiles:
                    # Pick 1 ready account randomly so order isn't identical every day
                    target_prof = random.choice(ready_profiles)
                    if not target_prof.get("registered"):
                        await asyncio.to_thread(
                            register_on_cashtree,
                            target_prof["id"],
                            target_prof.get("password", "12345"),
                            target_prof.get("name", "RRYSB"),
                        )
                        target_prof["registered"] = True

                    res = await asyncio.to_thread(claim_daily_bonus_on_cashtree, target_prof)
                    data = res.get("data") or {}
                    msg_str = str(data.get("message") or data.get("error") or "")

                    if res["ok"]:
                        bonus = float(data.get("randomBonus") or 0.0)
                        u_obj = data.get("user") or {}
                        target_prof["jpwCoins"] = float(u_obj.get("jpwCoins", float(target_prof.get("jpwCoins", 0.0)) + bonus))
                        target_prof["bonusCoins"] = float(u_obj.get("bonusCoins", float(target_prof.get("bonusCoins", 0.0)) + bonus))
                        target_prof["lastBonusTime"] = u_obj.get("lastBonusTime") or (datetime.utcnow().isoformat() + "Z")
                        save_state(STATE)
                        logger.info("Staggered Auto-Bonus (7AM-5PM): Claimed +%.2f Coins for %s (%d remaining today)", bonus, target_prof["id"], len(ready_profiles) - 1)
                    elif "already claimed" in msg_str.lower():
                        target_prof["lastBonusTime"] = datetime.utcnow().isoformat() + "Z"
                        save_state(STATE)

                    # Calculate dynamic spacing so all remaining ready accounts finish naturally before 5:00 PM IST
                    mins_left_until_5pm = max(10, ((16 - ist_hour) * 60) + (60 - ist_min))
                    rem_count = max(1, len(ready_profiles))
                    ideal_gap_sec = int((mins_left_until_5pm * 60) / (rem_count + 1))
                    # Clamp sleep between 90 seconds and 15 minutes with random jitter
                    sleep_sec = max(90, min(900, ideal_gap_sec + random.randint(-30, 45)))
                    await asyncio.sleep(sleep_sec)
                    continue

            # Window 2: 05:00 PM IST (17:00 - 17:59 IST) -> Final Verification Sweep for any missed accounts!
            elif ist_hour == 17:
                profiles = STATE.get("profiles", [])
                missed_profiles = [p for p in profiles if get_bonus_cooldown_str(p)[0]]
                if missed_profiles:
                    logger.info("5:00 PM IST Final Sweep: Checking %d remaining un-claimed accounts...", len(missed_profiles))
                    updated_any = False
                    for p in missed_profiles:
                        if not p.get("registered"):
                            await asyncio.to_thread(
                                register_on_cashtree,
                                p["id"],
                                p.get("password", "12345"),
                                p.get("name", "RRYSB"),
                            )
                            p["registered"] = True

                        res = await asyncio.to_thread(claim_daily_bonus_on_cashtree, p)
                        data = res.get("data") or {}
                        msg_str = str(data.get("message") or data.get("error") or "")
                        if res["ok"]:
                            bonus = float(data.get("randomBonus") or 0.0)
                            u_obj = data.get("user") or {}
                            p["jpwCoins"] = float(u_obj.get("jpwCoins", float(p.get("jpwCoins", 0.0)) + bonus))
                            p["bonusCoins"] = float(u_obj.get("bonusCoins", float(p.get("bonusCoins", 0.0)) + bonus))
                            p["lastBonusTime"] = u_obj.get("lastBonusTime") or (datetime.utcnow().isoformat() + "Z")
                            updated_any = True
                        elif "already claimed" in msg_str.lower():
                            p["lastBonusTime"] = datetime.utcnow().isoformat() + "Z"
                            updated_any = True
                        # Wait 8 to 15 seconds between each missed account during the 5 PM sweep
                        await asyncio.sleep(random.randint(8, 15))

                    if updated_any:
                        save_state(STATE)
        except Exception as exc:
            logger.warning("Auto-bonus scheduler warning: %s", exc)

        await asyncio.sleep(180)  # Check clock every 3 minutes


async def api_sync_orders_status(request: web.Request) -> web.Response:
    """Triggers an immediate live check against Cashtree /api/app/check-status for all PROCESSING orders."""
    changed = await asyncio.to_thread(sync_all_pending_orders_from_cashtree)
    return web.json_response({
        "ok": True,
        "changed": changed,
        "orders": STATE.get("orders", [])[:50],
    })


def sync_pending_sessions_via_gmail_imap(force_scan: bool = False) -> bool:
    """Background Gmail IMAP scanner for all PENDING payment sessions AND auto-reconciler for any JPW<techId>T... email in INBOX!"""
    if not (STATE["settings"].get("gmailUser") or os.environ.get("GMAIL_USER")):
        return False
    now_ts = time.time()
    sessions = STATE.setdefault("payment_sessions", {})
    max_age = 86400 if force_scan else 1200  # 20 mins for auto-poll, 24h for manual Re-check
    pending = [
        (sid, s) for sid, s in list(sessions.items())
        if s.get("status") != "COMPLETED" and not s.get("credited") and (now_ts - float(s.get("createdAt", 0))) <= max_age
    ]
    if not pending and not force_scan:
        return False

    scan_gmail_imap_for_payments(None, None, 0.0, force_scan)
    credited = False
    used_utrs = STATE.setdefault("used_utrs", [])
    customers = STATE.setdefault("customers", [])

    # Step A: Check all known PENDING sessions
    for sid, sess in pending:
        tid = str(sess.get("techId") or "")
        cust = next((c for c in customers if str(c.get("techId")) == tid), None)
        if not cust and tid:
            cust = {"techId": tid, "password": "123", "name": f"Tech_{tid[-4:]}", "balance": 0, "totalOrders": 0}
            customers.insert(0, cust)
        if not cust:
            continue
        gm_ok, gm_amt, gm_ref = _match_cached_gmail_messages(
            float(sess.get("uniqueAmount", 20.0)),
            sid,
            float(sess.get("createdAt", 0.0)),
            log_miss=False,
        )
        if gm_ok:
            if complete_payment_session_atomically(sess, cust, gm_ref, "Gmail IMAP Auto-Verify"):
                credited = True

    # Step B: Also auto-reconcile any email in GMAIL_MAIL_CACHE that has `jpw<techId>t<digits>` (even if server restarted!)
    for cache_key, m in reversed(list(GMAIL_MAIL_CACHE.items())):
        uid_key = m.get("uid_key") or cache_key
        if uid_key in used_utrs:
            continue
        for jpw_id_low in (m.get("jpw_ids") or []):
            oid_up = jpw_id_low.upper()
            if oid_up in used_utrs:
                continue
            existing_s = sessions.get(oid_up)
            # If session already exists in memory, Step A already checked it with strict amount verification!
            if existing_s:
                continue
            # Only auto-reconcile missing sessions (after a server restart) if the email arrived within the last 15 minutes!
            msg_ts = float(m.get("msg_ts") or 0.0)
            if msg_ts <= 0 or (now_ts - msg_ts) > 900:
                continue
            tid_m = re.match(r"^JPW([A-Z0-9]+)T\d+$", oid_up)
            if not tid_m:
                continue
            rec_tid = tid_m.group(1)
            # Extract paid amount from email text (e.g. '₹ 1' or 'Rs. 1.00' or '₹ 20')
            low_txt = m.get("low") or ""
            amt_m = re.search(r"(?:rs\.?|inr|₹)\s*([0-9]+(?:\.[0-9]{1,2})?)", low_txt, re.IGNORECASE)
            paid_val = int(float(amt_m.group(1))) if amt_m else int(STATE["settings"].get("reachPrice") or 20)
            reach_p = max(1, int(STATE["settings"].get("reachPrice") or 20))
            pkg_p = STATE["settings"].get("pkgPrices") or {}
            # Map paid package price back to wallet credit if it matches one of the 5 packages
            wallet_cred = paid_val
            for r_k, p_v in pkg_p.items():
                if int(p_v) == paid_val:
                    wallet_cred = int(r_k) * reach_p
                    break
            if not existing_s:
                existing_s = {
                    "sessionId": oid_up,
                    "techId": rec_tid,
                    "baseAmount": paid_val,
                    "walletCredit": wallet_cred,
                    "reaches": max(1, wallet_cred // reach_p),
                    "uniqueAmount": float(paid_val),
                    "status": "PENDING",
                    "createdAt": time.time(),
                }
                sessions[oid_up] = existing_s
            cust = next((c for c in customers if str(c.get("techId")).upper() == rec_tid), None)
            if not cust:
                cust = {"techId": rec_tid, "password": "123", "name": f"Tech_{rec_tid[-4:]}", "balance": 0, "totalOrders": 0}
                customers.insert(0, cust)
            if complete_payment_session_atomically(existing_s, cust, uid_key, "Gmail IMAP Order-ID Reconcile"):
                append_fc_log(f"🎉 [IMAP AUTO-RECONCILED] Found {oid_up} (₹{paid_val}) in Gmail -> Credited +₹{wallet_cred} to ID {rec_tid}!")
                credited = True
    return credited


async def live_order_status_poller() -> None:
    """Background task: Polls Gmail IMAP for pending payments & Cashtree /api/app/check-status every 2 seconds for PROCESSING orders."""
    await asyncio.sleep(2)
    while True:
        try:
            await asyncio.to_thread(sync_pending_sessions_via_gmail_imap)
            await asyncio.to_thread(sync_all_pending_orders_from_cashtree)
        except Exception as exc:
            logger.warning("live_order_status_poller warning: %s", exc)
        await asyncio.sleep(2)


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
    reconcile_failed_order_refunds()
    app["poller"] = asyncio.create_task(poll_telegram_updates())
    app["bonus_scheduler"] = asyncio.create_task(auto_daily_bonus_scheduler())
    app["order_poller"] = asyncio.create_task(live_order_status_poller())
    app["keep_alive"] = asyncio.create_task(keep_alive_self_pinger())


async def on_cleanup(app: web.Application) -> None:
    for task_key in ("poller", "bonus_scheduler", "order_poller", "keep_alive"):
        task = app.get(task_key)
        if task:
            task.cancel()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", serve_index)
    app.router.add_get("/ping", ping_health)
    app.router.add_get("/sw.js", serve_service_worker)
    app.router.add_get("/api/state", api_state_get)
    app.router.add_post("/api/state", api_state_post)
    app.router.add_post("/api/register-proxy", api_register_proxy)
    app.router.add_get("/api/customer/me", api_customer_me)
    app.router.add_post("/api/customer/auth", api_customer_auth)
    app.router.add_post("/api/customer/pay", api_customer_pay)
    app.router.add_post("/api/customer/order", api_customer_order)
    app.router.add_post("/api/payment-webhook", api_payment_webhook)
    app.router.add_get("/api/payment-webhook", api_payment_webhook)
    app.router.add_post("/api/customer/create-payment", api_customer_create_payment)
    app.router.add_post("/api/customer/check-payment", api_customer_check_payment)
    app.router.add_post("/api/customer/verify-razorpay", api_customer_verify_razorpay)
    app.router.add_post("/api/admin/fc-send-otp", api_admin_fc_send_otp)
    app.router.add_post("/api/admin/fc-verify-otp", api_admin_fc_verify_otp)
    app.router.add_post("/api/admin/fc-test", api_admin_fc_test)
    app.router.add_post("/api/admin/auth", api_admin_auth)
    app.router.add_get("/api/broadcast/latest", api_broadcast_latest)
    app.router.add_post("/api/admin/broadcast", api_admin_broadcast)
    app.router.add_post("/api/admin/referral-settings", api_admin_referral_settings)
    app.router.add_post("/api/admin/gmail-test", api_admin_gmail_test)
    app.router.add_post("/api/admin/mark-session", api_admin_mark_session)
    app.router.add_get("/api/admin/fc-logs", api_admin_fc_logs)
    app.router.add_post("/api/admin/clear-logs", api_admin_clear_logs)
    app.router.add_post("/api/sync-orders", api_sync_orders_status)
    app.router.add_get("/api/sync-orders", api_sync_orders_status)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    logger.info("Starting JPW Mini App & Telegram Bot Server on port %s...", PORT)
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
