import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from PIL import Image, ImageDraw, ImageFont
from supabase import create_client, Client

from questions import (
    EASY_QUESTIONS, MEDIUM_QUESTIONS, HARD_QUESTIONS, EXTREME_QUESTIONS,
    DIFFICULTY_LABELS, random_question,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quizbot")

# ==================== НАСТРОЙКИ ====================

TOKEN = os.getenv("BOT_TOKEN", "8781607065:AAFn0AbFLUHkcEaQtSgvn2Ix52HksW3_j-0")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

XROCKET_API_KEY = os.getenv("XROCKET_API_KEY", "")
XROCKET_WEBHOOK_SECRET = os.getenv("XROCKET_WEBHOOK_SECRET", "")
XROCKET_BASE = "https://pay.api.xrocket.exchange"
XROCKET_SUBSCRIBE_URL = os.getenv("XROCKET_SUBSCRIBE_URL", "https://t.me/xRocket")
XROCKET_REFERRAL_URL = os.getenv("XROCKET_REFERRAL_URL", "https://t.me/xRocket")

PORT = int(os.getenv("PORT", 8080))

ANSWERS_PER_LEVEL = 10
MAX_LEVEL = 10
RAKE_PCT = 0.05

SUBSCRIBER_MULTIPLIER = 2.0
SUBSCRIPTION_PRICE = 0.50

MIN_WITHDRAW = 0.05
DAILY_WITHDRAW_LIMIT = 5.00

DEPOSIT_MIN = 0.05
DEPOSIT_MAX = 50.0

DUEL_MIN = 0.05
DUEL_MAX = 1.00
DUEL_TTL = 120

TIMER_PROBABILITY = 0.20
TIMER_SECONDS = 10

POT_PERCENT = 0.05
POT_HOUR = 21

SPONSOR_PRICE = 5.00
SPONSOR_QUESTIONS = 20

DIFFICULTY_REWARDS = {
    "easy": 1,
    "medium": 2,
    "hard": 3,
    "extreme": 5,
}

ADMIN_IDS = {8130244626, 6173495222}

TZ = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))
WORK_HOURS = list(range(8, 24))
WITHDRAW_CONFIRM_TTL = 120

CORRECT_PHRASES = [
    "🎉 <b>Правильно!</b>", "🔥 <b>В точку!</b>", "💎 <b>Красавчик!</b>",
    "⚡ <b>Молниеносно!</b>", "🧠 <b>Умница!</b>", "🏆 <b>Есть!</b>",
    "✨ <b>Верно!</b>", "🚀 <b>Полетели!</b>", "🎯 <b>Точно в цель!</b>",
    "🌟 <b>Блестяще!</b>",
]

LEVELS = [
    (1, "🐣", "Новичок"), (2, "🥚", "Ученик"), (3, "🐥", "Знаток"),
    (4, "🦅", "Эксперт"), (5, "🧠", "Мастер"), (6, "🎓", "Гуру"),
    (7, "💎", "Легенда"), (8, "👑", "Гений"), (9, "🔥", "Титан"),
    (10, "⚡", "Бог викторины"),
]

if not TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    print("!!! Не заданы BOT_TOKEN / SUPABASE_URL / SUPABASE_KEY")
    raise SystemExit(1)

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

ACTIVE_QUESTIONS = {}
QUIZ_ENABLED = set()
PENDING_WITHDRAWS = {}
BANNED_CACHE = {}
SUBSCRIBERS_CACHE = {}
DUEL_BUSY = set()
TOP_CACHE = {}
HTTP_SESSION = None
_FONT_PATH = None

TOURNAMENT_STATE = {}
TOURNAMENT_ACTIVE = {}
TOURNAMENT_LOBBY = {}
TOURNAMENT_EDIT = {}

SPONSOR_SESSION = {}


def is_admin(uid):
    return uid in ADMIN_IDS


def level_from_correct(c):
    return min(c // ANSWERS_PER_LEVEL + 1, MAX_LEVEL)


def level_info(c):
    lvl = level_from_correct(c)
    emoji, name = LEVELS[lvl - 1][1], LEVELS[lvl - 1][2]
    title_str = f"{emoji} {name} (ур. {lvl})"
    if lvl >= MAX_LEVEL:
        progress = "🏆 Максимальный уровень!"
    else:
        in_level = c - (lvl - 1) * ANSWERS_PER_LEVEL
        progress = f"до след. уровня: {ANSWERS_PER_LEVEL - in_level} отв."
    return lvl, emoji, name, 0, title_str, progress


def make_progress_bar(c):
    lvl = level_from_correct(c)
    if lvl >= MAX_LEVEL:
        return "▓" * 10 + " 10/10"
    in_level = c - (lvl - 1) * ANSWERS_PER_LEVEL
    return f"{'▓' * in_level}{'▒' * (ANSWERS_PER_LEVEL - in_level)} {in_level}/{ANSWERS_PER_LEVEL}"


# ==================== FONT ====================


def get_font(size=64):
    global _FONT_PATH
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]
    if _FONT_PATH is None:
        for path in candidates:
            if os.path.exists(path):
                _FONT_PATH = path
                break
        if _FONT_PATH is None:
            _FONT_PATH = ""
    try:
        if _FONT_PATH:
            return ImageFont.truetype(_FONT_PATH, size)
    except Exception:
        pass
    return ImageFont.load_default()


def render_question_image(text: str) -> bytes:
    W, H = 800, 300
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    font = get_font(80)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    while tw > W - 60 and font.size > 20:
        font = get_font(font.size - 5)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((W - tw) / 2 - bbox[0], (H - th) / 2 - bbox[1]), text,
              fill=(20, 20, 80), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ==================== HTTP ====================


async def get_http():
    global HTTP_SESSION
    if HTTP_SESSION is None or HTTP_SESSION.closed:
        HTTP_SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15, connect=5),
            connector=aiohttp.TCPConnector(limit=100, ttl_dns_cache=300),
        )
    return HTTP_SESSION


async def close_http():
    global HTTP_SESSION
    if HTTP_SESSION and not HTTP_SESSION.closed:
        await HTTP_SESSION.close()


async def safe_send(coro_func, *args, **kwargs):
    for _ in range(3):
        try:
            return await coro_func(*args, **kwargs)
        except TelegramRetryAfter as e:
            log.warning("FloodWait %s сек", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
        except TelegramBadRequest as e:
            if "can't parse entities" in str(e) and "parse_mode" in kwargs:
                kwargs.pop("parse_mode", None)
                try:
                    return await coro_func(*args, **kwargs)
                except Exception:
                    return None
            log.warning("safe_send bad request: %s", e)
            return None
        except Exception as e:
            log.warning("safe_send error: %s", e)
            return None
    return None


# ==================== SUPABASE (sync) ====================


def _rpc(name, params):
    try:
        return supabase.rpc(name, params).execute()
    except Exception as e:
        log.warning("rpc %s: %s", name, e)
        return None


def get_player_sync(chat_id, user_id, username=None, first_name=None):
    try:
        res = supabase.table("quiz_players").select("*").eq("chat_id", chat_id).eq("user_id", user_id).execute()
        if res.data:
            row = res.data[0]
            upd = {}
            if not row.get("first_name") and first_name:
                upd["first_name"] = first_name
            if not row.get("username") and username:
                upd["username"] = username
            if upd:
                try:
                    supabase.table("quiz_players").update(upd).eq(
                        "chat_id", chat_id).eq("user_id", user_id).execute()
                    row.update(upd)
                except Exception:
                    pass
            return row
    except Exception as e:
        log.warning("get_player: %s", e)
    try:
        supabase.table("quiz_players").insert({
            "chat_id": chat_id, "user_id": user_id,
            "username": username, "first_name": first_name,
        }).execute()
    except Exception:
        pass
    return {"chat_id": chat_id, "user_id": user_id, "username": username,
            "first_name": first_name, "balance": 0, "total_won": 0,
            "correct_answers": 0, "is_withdrawing": False}


def add_score_sync(chat_id, user_id):
    r = _rpc("add_balance_atomic", {
        "p_chat_id": chat_id, "p_user_id": user_id,
        "p_amount": 0, "p_count_correct": True,
    })
    return bool(r)


def add_score_multi_sync(chat_id, user_id, points):
    """Начисляет points очков."""
    try:
        get_player_sync(chat_id, user_id)
        res = supabase.table("quiz_players").select("correct_answers").eq(
            "chat_id", chat_id).eq("user_id", user_id).execute()
        cur = int(res.data[0]["correct_answers"]) if res.data else 0
        supabase.table("quiz_players").update({
            "correct_answers": cur + points,
        }).eq("chat_id", chat_id).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        log.warning("add_score_multi: %s", e)
        return False


def add_balance_sync(chat_id, user_id, amount, count_correct=False):
    r = _rpc("add_balance_atomic", {
        "p_chat_id": chat_id, "p_user_id": user_id,
        "p_amount": amount, "p_count_correct": count_correct,
    })
    return float(r.data) if r and r.data is not None else None


def deduct_balance_sync(chat_id, user_id, amount):
    r = _rpc("deduct_balance_atomic", {"p_chat_id": chat_id, "p_user_id": user_id, "p_amount": amount})
    return bool(r and r.data is True)


def try_lock_withdraw_sync(chat_id, user_id):
    r = _rpc("try_lock_withdraw", {"p_chat_id": chat_id, "p_user_id": user_id})
    return bool(r and r.data is True)


def unlock_withdraw_sync(chat_id, user_id):
    _rpc("unlock_withdraw", {"p_chat_id": chat_id, "p_user_id": user_id})


def withdrawn_today_sync(chat_id, user_id):
    r = _rpc("withdrawn_today", {"p_chat_id": chat_id, "p_user_id": user_id})
    return float(r.data) if r and r.data is not None else 0.0


def log_house_income_sync(chat_id, amount, source):
    try:
        supabase.table("quiz_house").insert({
            "chat_id": chat_id, "amount": amount, "source": source,
        }).execute()
    except Exception as e:
        log.warning("house_income: %s", e)


def get_house_total_sync(chat_id=None):
    try:
        q = supabase.table("quiz_house").select("amount")
        if chat_id is not None:
            q = q.eq("chat_id", chat_id)
        res = q.execute()
        return sum(float(r["amount"]) for r in (res.data or []))
    except Exception:
        return 0.0


def get_house_by_source_sync():
    try:
        res = supabase.table("quiz_house").select("amount,source").execute()
        by = {}
        for r in res.data or []:
            by[r["source"]] = by.get(r["source"], 0.0) + float(r["amount"])
        return by
    except Exception:
        return {}


def add_to_pot_sync(chat_id, amount):
    try:
        res = supabase.table("quiz_pot").select("amount").eq("chat_id", chat_id).execute()
        current = float(res.data[0]["amount"]) if res.data else 0.0
        new_amount = round(current + amount, 4)
        if res.data:
            supabase.table("quiz_pot").update({"amount": new_amount}).eq("chat_id", chat_id).execute()
        else:
            supabase.table("quiz_pot").insert({"chat_id": chat_id, "amount": new_amount}).execute()
        return new_amount
    except Exception as e:
        log.warning("add_to_pot: %s", e)
        return None


def payout_pot_sync(chat_id):
    try:
        res = supabase.table("quiz_pot").select("amount").eq("chat_id", chat_id).execute()
        if not res.data:
            return 0.0
        amount = float(res.data[0]["amount"])
        supabase.table("quiz_pot").update({"amount": 0}).eq("chat_id", chat_id).execute()
        return amount
    except Exception as e:
        log.warning("payout_pot: %s", e)
        return 0.0


def get_pot_sync(chat_id):
    try:
        res = supabase.table("quiz_pot").select("amount").eq("chat_id", chat_id).execute()
        return float(res.data[0]["amount"]) if res.data else 0.0
    except Exception:
        return 0.0


def pot_take_sync(chat_id, amount):
    try:
        res = supabase.table("quiz_pot").select("amount").eq("chat_id", chat_id).execute()
        current = float(res.data[0]["amount"]) if res.data else 0.0
        if current < amount:
            return False, current
        new_amount = round(current - amount, 4)
        supabase.table("quiz_pot").update({"amount": new_amount}).eq("chat_id", chat_id).execute()
        return True, new_amount
    except Exception as e:
        log.warning("pot_take: %s", e)
        return False, 0.0


def create_invoice_sync(client_invoice_id, user_id, chat_id, amount, message_id=None, kind="deposit"):
    try:
        res = supabase.table("quiz_invoices").insert({
            "client_invoice_id": client_invoice_id,
            "user_id": user_id, "chat_id": chat_id, "amount": amount,
            "message_id": message_id, "kind": kind,
        }).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        log.warning("create_invoice: %s", e)
        return None


def mark_invoice_paid_sync(client_invoice_id):
    try:
        res = supabase.table("quiz_invoices").select("*").eq(
            "client_invoice_id", client_invoice_id).execute()
        if not res.data:
            return None
        inv = res.data[0]
        if inv["status"] == "paid":
            return None
        supabase.table("quiz_invoices").update({
            "status": "paid",
            "paid_at": datetime.now(timezone.utc).isoformat(),
        }).eq("client_invoice_id", client_invoice_id).execute()
        return inv
    except Exception as e:
        log.warning("mark_invoice_paid: %s", e)
        return None


def load_bans_sync():
    try:
        res = supabase.table("quiz_bans").select("chat_id,user_id,banned_until").execute()
        cache = {}
        now = datetime.now(timezone.utc)
        for row in res.data or []:
            bu = row.get("banned_until")
            if bu:
                try:
                    until = datetime.fromisoformat(bu.replace("Z", "+00:00"))
                    if until <= now:
                        continue
                except Exception:
                    pass
            cache.setdefault(int(row["chat_id"]), set()).add(int(row["user_id"]))
        return cache
    except Exception:
        return {}


def load_subscribers_sync():
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        res = supabase.table("quiz_subscribers").select("chat_id,user_id").gt("expires_at", now_iso).execute()
        cache = {}
        for row in res.data or []:
            cache.setdefault(int(row["chat_id"]), set()).add(int(row["user_id"]))
        return cache
    except Exception:
        return {}


def ban_user_sync(chat_id, user_id, reason, admin_id, banned_until=None):
    try:
        supabase.table("quiz_bans").upsert({
            "chat_id": chat_id, "user_id": user_id,
            "reason": reason, "banned_by": admin_id,
            "banned_until": banned_until,
        }).execute()
    except Exception as e:
        log.warning("ban: %s", e)


def unban_user_sync(chat_id, user_id):
    try:
        supabase.table("quiz_bans").delete().eq("chat_id", chat_id).eq("user_id", user_id).execute()
    except Exception:
        pass


def save_active_sync(chat_id, question, answers, is_multi):
    try:
        supabase.table("quiz_active").upsert({
            "chat_id": chat_id, "question": question,
            "answer": "||".join(answers), "is_multi": is_multi,
        }).execute()
    except Exception:
        pass


def clear_active_sync(chat_id):
    try:
        supabase.table("quiz_active").delete().eq("chat_id", chat_id).execute()
    except Exception:
        pass


def load_active_sync():
    try:
        res = supabase.table("quiz_active").select("*").execute()
        return res.data or []
    except Exception:
        return []


def unlock_all_withdrawals_sync():
    try:
        supabase.table("quiz_players").update({"is_withdrawing": False}).eq("is_withdrawing", True).execute()
    except Exception:
        pass


def get_top_sync(chat_id, limit=10):
    try:
        res = supabase.table("quiz_players").select(
            "user_id,username,first_name,balance,correct_answers"
        ).eq("chat_id", chat_id).order("correct_answers", desc=True).limit(limit).execute()
        return res.data or []
    except Exception:
        return []


def get_top1_sync(chat_id):
    rows = get_top_sync(chat_id, 1)
    return int(rows[0]["user_id"]) if rows else None


def get_stats_sync():
    try:
        players = supabase.table("quiz_players").select("balance,total_won,correct_answers").execute().data or []
        payouts = supabase.table("quiz_payouts").select("amount,status").execute().data or []
        bans = supabase.table("quiz_bans").select("user_id").execute().data or []
        subs = supabase.table("quiz_subscribers").select("user_id").execute().data or []
        return players, payouts, bans, subs
    except Exception:
        return [], [], [], []


def get_payouts_sync(limit=20):
    try:
        res = supabase.table("quiz_payouts").select("*").order("created_at", desc=True).limit(limit).execute()
        return res.data or []
    except Exception:
        return []


def log_payout_sync(chat_id, user_id, amount, payout_id, status):
    try:
        supabase.table("quiz_payouts").insert({
            "chat_id": chat_id, "user_id": user_id, "amount": amount,
            "xrocket_payout_id": payout_id, "status": status,
        }).execute()
    except Exception as e:
        log.warning("log_payout: %s", e)


def get_coins_stats_sync(chat_id=None):
    try:
        pq = supabase.table("quiz_players").select("balance,correct_answers,user_id")
        if chat_id is not None:
            pq = pq.eq("chat_id", chat_id)
        players = pq.execute().data or []
        total_balance = sum(float(p["balance"]) for p in players)
        total_points = sum(int(p["correct_answers"]) for p in players)
        pq2 = supabase.table("quiz_payouts").select("amount,status")
        if chat_id is not None:
            pq2 = pq2.eq("chat_id", chat_id)
        payouts = pq2.execute().data or []
        total_paid = sum(float(p["amount"]) for p in payouts if p["status"] == "finished")
        hq = supabase.table("quiz_house").select("amount")
        if chat_id is not None:
            hq = hq.eq("chat_id", chat_id)
        house = hq.execute().data or []
        total_house = sum(float(h["amount"]) for h in house)
        return {"balance": total_balance, "points": total_points,
                "players": len(players), "paid": total_paid, "house": total_house}
    except Exception as e:
        log.warning("get_coins_stats: %s", e)
        return None


# ==================== НАСТРОЙКИ ЧАТА (сложность викторины) ====================


def get_chat_settings_sync(chat_id):
    try:
        res = supabase.table("quiz_settings").select("*").eq("chat_id", chat_id).execute()
        if res.data:
            return res.data[0]
    except Exception:
        pass
    default = {"chat_id": chat_id, "difficulty": "medium"}
    try:
        supabase.table("quiz_settings").insert(default).execute()
    except Exception:
        pass
    return default


def update_chat_setting_sync(chat_id, field, value):
    try:
        get_chat_settings_sync(chat_id)
        supabase.table("quiz_settings").update({field: value}).eq("chat_id", chat_id).execute()
        return True
    except Exception as e:
        log.warning("update_chat_setting: %s", e)
        return False


# ==================== ТУРНИР (sync) ====================


def tournament_create_sync(chat_id, prize):
    try:
        res = supabase.table("quiz_tournaments").insert({
            "chat_id": chat_id, "prize": prize, "status": "lobby",
        }).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        log.warning("tournament_create: %s", e)
        return None


def tournament_add_player_sync(tid, uid):
    try:
        supabase.table("quiz_tournament_players").upsert({
            "tournament_id": tid, "user_id": uid, "correct": 0,
        }).execute()
        return True
    except Exception as e:
        log.warning("tourn_add_player: %s", e)
        return False


def tournament_inc_correct_sync(tid, uid):
    try:
        res = supabase.table("quiz_tournament_players").select("correct").eq(
            "tournament_id", tid).eq("user_id", uid).execute()
        current = int(res.data[0]["correct"]) if res.data else 0
        supabase.table("quiz_tournament_players").update({
            "correct": current + 1,
        }).eq("tournament_id", tid).eq("user_id", uid).execute()
    except Exception as e:
        log.warning("tourn_inc: %s", e)


def tournament_finish_sync(tid, winner_id):
    try:
        supabase.table("quiz_tournaments").update({
            "status": "finished",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "winner_id": winner_id,
        }).eq("id", tid).execute()
    except Exception as e:
        log.warning("tourn_finish: %s", e)


def tournament_get_players_sync(tid):
    try:
        res = supabase.table("quiz_tournament_players").select("*").eq(
            "tournament_id", tid).order("correct", desc=True).execute()
        return res.data or []
    except Exception:
        return []


def get_t_settings_sync(chat_id):
    try:
        res = supabase.table("quiz_tournament_settings").select("*").eq("chat_id", chat_id).execute()
        if res.data:
            return res.data[0]
    except Exception as e:
        log.warning("get_t_settings: %s", e)
    default = {"chat_id": chat_id, "min_players": 2, "lobby_seconds": 60,
               "question_seconds": 30, "questions": 10, "prize": 0.30,
               "difficulty": "medium"}
    try:
        supabase.table("quiz_tournament_settings").insert(default).execute()
    except Exception:
        pass
    return default


def update_t_setting_sync(chat_id, field, value):
    try:
        get_t_settings_sync(chat_id)
        supabase.table("quiz_tournament_settings").update({field: value}).eq("chat_id", chat_id).execute()
        return True
    except Exception as e:
        log.warning("update_t_setting: %s", e)
        return False


# ==================== СПОНСОР (sync) ====================


def sponsor_add_sync(user_id, chat_id, question, answer, position):
    try:
        res = supabase.table("quiz_sponsor_questions").insert({
            "sponsor_id": user_id, "chat_id": chat_id,
            "question": question, "answer": answer, "position": position,
        }).execute()
        return res.data[0]["id"] if res.data else None
    except Exception as e:
        log.warning("sponsor_add: %s", e)
        return None


def sponsor_get_next_sync(chat_id):
    try:
        res = supabase.table("quiz_sponsor_questions").select("*").eq(
            "chat_id", chat_id).eq("used", False).order("position").limit(1).execute()
        if not res.data:
            return None
        return res.data[0]
    except Exception as e:
        log.warning("sponsor_get_next: %s", e)
        return None


def sponsor_mark_used_sync(qid):
    try:
        supabase.table("quiz_sponsor_questions").update({"used": True}).eq("id", qid).execute()
    except Exception as e:
        log.warning("sponsor_mark_used: %s", e)


# ==================== SUPABASE (async) ====================


async def get_player(cid, uid, un=None, fn=None):
    return await asyncio.to_thread(get_player_sync, cid, uid, un, fn)


async def add_score(cid, uid):
    return await asyncio.to_thread(add_score_sync, cid, uid)


async def add_score_multi(cid, uid, points):
    return await asyncio.to_thread(add_score_multi_sync, cid, uid, points)


async def add_balance(cid, uid, amt, count_correct=False):
    return await asyncio.to_thread(add_balance_sync, cid, uid, amt, count_correct)


async def deduct_balance(cid, uid, amt):
    return await asyncio.to_thread(deduct_balance_sync, cid, uid, amt)


async def try_lock_withdraw(cid, uid):
    return await asyncio.to_thread(try_lock_withdraw_sync, cid, uid)


async def unlock_withdraw(cid, uid):
    await asyncio.to_thread(unlock_withdraw_sync, cid, uid)


async def withdrawn_today(cid, uid):
    return await asyncio.to_thread(withdrawn_today_sync, cid, uid)


async def log_house_income(cid, amt, source):
    await asyncio.to_thread(log_house_income_sync, cid, amt, source)


async def get_house_total(cid=None):
    return await asyncio.to_thread(get_house_total_sync, cid)


async def get_house_by_source():
    return await asyncio.to_thread(get_house_by_source_sync)


async def add_to_pot(cid, amt):
    return await asyncio.to_thread(add_to_pot_sync, cid, amt)


async def payout_pot(cid):
    return await asyncio.to_thread(payout_pot_sync, cid)


async def get_pot(cid):
    return await asyncio.to_thread(get_pot_sync, cid)


async def pot_take(cid, amt):
    return await asyncio.to_thread(pot_take_sync, cid, amt)


async def create_invoice(client_invoice_id, uid, cid, amt, message_id=None, kind="deposit"):
    return await asyncio.to_thread(create_invoice_sync, client_invoice_id, uid, cid, amt, message_id, kind)


async def mark_invoice_paid(client_invoice_id):
    return await asyncio.to_thread(mark_invoice_paid_sync, client_invoice_id)


async def get_top1(cid):
    return await asyncio.to_thread(get_top1_sync, cid)


async def get_coins_stats(cid=None):
    return await asyncio.to_thread(get_coins_stats_sync, cid)


async def get_chat_settings(cid):
    return await asyncio.to_thread(get_chat_settings_sync, cid)


async def update_chat_setting(cid, field, value):
    return await asyncio.to_thread(update_chat_setting_sync, cid, field, value)


async def tournament_create(cid, prize):
    return await asyncio.to_thread(tournament_create_sync, cid, prize)


async def tournament_add_player(tid, uid):
    return await asyncio.to_thread(tournament_add_player_sync, tid, uid)


async def tournament_inc_correct(tid, uid):
    await asyncio.to_thread(tournament_inc_correct_sync, tid, uid)


async def tournament_finish(tid, winner_id):
    await asyncio.to_thread(tournament_finish_sync, tid, winner_id)


async def tournament_get_players(tid):
    return await asyncio.to_thread(tournament_get_players_sync, tid)


async def get_t_settings(cid):
    return await asyncio.to_thread(get_t_settings_sync, cid)


async def update_t_setting(cid, field, value):
    return await asyncio.to_thread(update_t_setting_sync, cid, field, value)


async def sponsor_add(uid, cid, q, a, pos):
    return await asyncio.to_thread(sponsor_add_sync, uid, cid, q, a, pos)


async def sponsor_get_next(cid):
    return await asyncio.to_thread(sponsor_get_next_sync, cid)


async def sponsor_mark_used(qid):
    await asyncio.to_thread(sponsor_mark_used_sync, qid)


def is_banned_cached(cid, uid):
    return uid in BANNED_CACHE.get(cid, set())


def is_subscriber_cached(cid, uid):
    return uid in SUBSCRIBERS_CACHE.get(cid, set())


async def ban_user(cid, uid, reason, admin_id, banned_until=None):
    await asyncio.to_thread(ban_user_sync, cid, uid, reason, admin_id, banned_until)
    BANNED_CACHE.setdefault(cid, set()).add(uid)


async def unban_user(cid, uid):
    await asyncio.to_thread(unban_user_sync, cid, uid)
    BANNED_CACHE.get(cid, set()).discard(uid)


async def save_active(cid, q, answers, is_multi):
    await asyncio.to_thread(save_active_sync, cid, q, answers, is_multi)


async def clear_active(cid):
    await asyncio.to_thread(clear_active_sync, cid)


async def get_top(cid, limit=10):
    return await asyncio.to_thread(get_top_sync, cid, limit)


async def get_stats():
    return await asyncio.to_thread(get_stats_sync)


async def get_payouts(limit=20):
    return await asyncio.to_thread(get_payouts_sync, limit)


async def log_payout(cid, uid, amt, pid, status):
    await asyncio.to_thread(log_payout_sync, cid, uid, amt, pid, status)


# ==================== XROCKET ====================


async def xrocket_payout(cid, uid, amount):
    if not XROCKET_API_KEY:
        return False, "XROCKET_API_KEY не задан"
    payload = {
        "clientPayoutId": f"quiz_{cid}_{uid}_{int(datetime.now().timestamp()*1000)}",
        "target": str(uid), "targetType": "telegram_user_id",
        "asset": "USDT", "amount": f"{amount:.4f}",
        "description": "Quiz reward",
    }
    try:
        s = await get_http()
        async with s.post(
            f"{XROCKET_BASE}/api/v1/payouts",
            headers={"Authorization": f"Bearer {XROCKET_API_KEY}", "Content-Type": "application/json"},
            json=payload,
        ) as r:
            data = await r.json()
            log.info("xRocket [%s] %s", r.status, data)
            if r.status in (200, 201):
                return True, data.get("payoutId") or data.get("id") or "ok"
            return False, data.get("detail") or data.get("title") or str(data)
    except Exception as e:
        return False, str(e)


async def xrocket_create_invoice(client_invoice_id: str, amount: float, description: str):
    if not XROCKET_API_KEY:
        return False, "XROCKET_API_KEY не задан"
    payload = {
        "priceAmount": f"{amount:.4f}",
        "priceCurrency": "USDT",
        "numPayments": 1,
        "clientInvoiceId": client_invoice_id,
        "description": description[:1000],
        "expiresIn": 3600000,
    }
    try:
        s = await get_http()
        async with s.post(
            f"{XROCKET_BASE}/api/v1/invoices",
            headers={"Authorization": f"Bearer {XROCKET_API_KEY}", "Content-Type": "application/json"},
            json=payload,
        ) as r:
            data = await r.json()
            log.info("xRocket invoice [%s] %s", r.status, data)
            if r.status in (200, 201):
                inv_id = data.get("id")
                url = data.get("links", {}).get("telegramBotLink") or f"https://t.me/xRocket?start={inv_id}"
                return True, url
            return False, data.get("detail") or data.get("title") or str(data)
    except Exception as e:
        return False, str(e)


# ==================== WEBHOOK ====================


def verify_webhook_signature(raw_body, signature, timestamp, secret):
    if not signature or not timestamp or not secret:
        return False
    signed = f"{timestamp}.{raw_body.decode('utf-8')}"
    expected = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def handle_webhook(request: web.Request) -> web.Response:
    try:
        raw_body = await request.read()
        signature = request.headers.get("Signature", "")
        sig_version = request.headers.get("Signature-Version", "")
        sig_timestamp = request.headers.get("Signature-Timestamp", "")

        if sig_version != "v1":
            return web.Response(status=401, text="bad version")
        if not verify_webhook_signature(raw_body, signature, sig_timestamp, XROCKET_WEBHOOK_SECRET):
            return web.Response(status=401, text="bad signature")

        try:
            event = json.loads(raw_body)
        except Exception:
            return web.Response(status=400, text="bad json")

        ev_type = event.get("type")
        data = event.get("data", {})
        log.info("Webhook: type=%s", ev_type)

        if ev_type == "invoice":
            if data.get("event") == "invoice_status_changed":
                inv = data.get("invoice", {})
                if inv.get("status") == "paid":
                    cid = inv.get("clientInvoiceId")
                    if cid:
                        await process_paid_invoice(cid)

        return web.Response(status=200, text="ok")
    except Exception as e:
        log.error("Webhook error: %s", e)
        return web.Response(status=200, text="ok")


async def process_paid_invoice(client_invoice_id: str):
    inv = await mark_invoice_paid(client_invoice_id)
    if not inv:
        return

    user_id = int(inv["user_id"])
    chat_id = int(inv["chat_id"]) if inv.get("chat_id") else user_id
    amount = float(inv["amount"])
    msg_id = inv.get("message_id")
    kind = inv.get("kind") or "deposit"

    if kind == "sponsor":
        SPONSOR_SESSION[user_id] = {"chat_id": None, "collected": 0, "target": SPONSOR_QUESTIONS}
        try:
            await bot.send_message(user_id,
                f"✅ <b>Оплата ${amount:.2f} получена!</b>\n\n"
                f"Теперь напиши <b>ID чата</b>, куда постить вопросы.\n"
                f"<i>(например: -1002712583382)</i>")
        except Exception:
            pass
        return

    new_balance = await add_balance(chat_id, user_id, amount)
    if new_balance is None:
        p = await get_player(chat_id, user_id)
        new_balance = float(p.get("balance", 0))

    if msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=int(msg_id),
                text=(f"✅ <b>Пополнение успешно!</b>\n\n"
                      f"💳 Зачислено: <b>${amount:.4f}</b> USDT\n"
                      f"💰 Баланс: <b>${new_balance:.4f}</b>\n\n"
                      f"<i>Спасибо!</i>"))
        except Exception:
            pass

    try:
        await bot.send_message(user_id,
            f"✅ <b>Баланс пополнен!</b>\n\n"
            f"💰 Сумма: <b>${amount:.4f}</b>\n"
            f"💼 Баланс: <b>${new_balance:.4f}</b>")
    except Exception:
        pass


async def start_webhook_server():
    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Webhook-сервер на порту %s", PORT)


# ==================== ВОПРОСЫ ====================


async def ask_question(chat_id):
    if chat_id in TOURNAMENT_ACTIVE:
        return False

    chat_settings = await get_chat_settings(chat_id)
    difficulty = chat_settings.get("difficulty", "medium")
    reward_points = DIFFICULTY_REWARDS.get(difficulty, 2)
    diff_label = DIFFICULTY_LABELS.get(difficulty, "🟡 Средне")

    sponsor_q = await sponsor_get_next(chat_id)
    if sponsor_q:
        q = sponsor_q["question"]
        answers = [sponsor_q["answer"].lower()]
        is_multi = False
        is_image = False
        await sponsor_mark_used(sponsor_q["id"])
        sponsor_note = "\n🎁 <i>Спонсорский вопрос</i>"
    else:
        q, answers, is_multi, is_image = random_question(difficulty=difficulty)
        sponsor_note = ""

    ACTIVE_QUESTIONS[chat_id] = {
        "question": q, "answers": answers, "is_multi": is_multi,
        "timer": False, "timer_task": None,
    }
    await save_active(chat_id, q, answers, is_multi)

    pot = await get_pot(chat_id)
    pot_line = f"\n🎰 Копилка: <b>${pot:.3f}</b>" if pot >= 0.01 else ""

    use_timer = random.random() < TIMER_PROBABILITY
    if use_timer:
        ACTIVE_QUESTIONS[chat_id]["timer"] = True
        timer_line = f"\n⏱ <b>Таймер: {TIMER_SECONDS} сек!</b>"
    else:
        timer_line = ""

    try:
        await bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass

    try:
        if is_image:
            png = render_question_image(q)
            buf = BufferedInputFile(png, filename="q.png")
            caption = (f"🧠 <b>Вопрос!</b>{sponsor_note}\n\n"
                       f"{diff_label} · 🏆 +{reward_points} очк. за правильный\n"
                       f"🔓 Вопрос открыт до правильного ответа."
                       f"{pot_line}{timer_line}")
            msg = await safe_send(bot.send_photo, chat_id, buf, caption=caption)
        else:
            msg = await safe_send(bot.send_message, chat_id,
                f"🧠 <b>Вопрос!</b>{sponsor_note}\n\n❓ {q}\n\n"
                f"{diff_label} · 🏆 +{reward_points} очк. за правильный\n"
                f"🔓 Вопрос открыт до правильного ответа."
                f"{pot_line}{timer_line}")
        if msg:
            try:
                await bot.set_message_reaction(chat_id, msg.message_id, ["🧠"])
            except Exception:
                pass

        if use_timer:
            async def timer_task():
                await asyncio.sleep(TIMER_SECONDS)
                cur = ACTIVE_QUESTIONS.get(chat_id)
                if cur and cur.get("timer"):
                    ACTIVE_QUESTIONS.pop(chat_id, None)
                    await clear_active(chat_id)
                    await safe_send(bot.send_message, chat_id,
                        f"⌛ <b>Время вышло!</b>\nОтвет: <b>{answers[0]}</b>")
            ACTIVE_QUESTIONS[chat_id]["timer_task"] = asyncio.create_task(timer_task())
        return True
    except Exception as e:
        log.warning("send_question: %s", e)
        return False


def next_run_time(now):
    for h in WORK_HOURS:
        target = now.replace(hour=h, minute=0, second=5, microsecond=0)
        if target > now:
            return target
    return (now + timedelta(days=1)).replace(hour=WORK_HOURS[0], minute=0, second=5, microsecond=0)


async def question_scheduler():
    await asyncio.sleep(10)
    while True:
        now = datetime.now(TZ)
        target = next_run_time(now)
        wait = (target - now).total_seconds()
        log.info("Следующий вопрос в %s (через %.0f сек)", target.strftime("%H:%M:%S"), wait)
        await asyncio.sleep(max(1, wait))
        if QUIZ_ENABLED:
            await asyncio.gather(*[ask_question(c) for c in list(QUIZ_ENABLED)], return_exceptions=True)
        await asyncio.sleep(60)


async def caches_refresh_loop():
    global BANNED_CACHE, SUBSCRIBERS_CACHE
    while True:
        try:
            BANNED_CACHE = await asyncio.to_thread(load_bans_sync)
            SUBSCRIBERS_CACHE = await asyncio.to_thread(load_subscribers_sync)
        except Exception as e:
            log.warning("caches refresh: %s", e)
        await asyncio.sleep(60)


async def distribute_pot(cid, reason="manual"):
    amount = await payout_pot(cid)
    if amount < 0.01:
        return False, "Копилка пуста (меньше $0.01)"
    rows = await get_top(cid, 10)
    if not rows:
        await add_to_pot(cid, amount)
        return False, "Нет игроков"
    winner = random.choice(rows)
    wuid = int(winner["user_id"])
    wname = winner.get("first_name") or winner.get("username") or str(wuid)
    await add_balance(cid, wuid, amount)

    head = "🎰 <b>Розыгрыш копилки!</b>" if reason == "auto" else "🎉 <b>Копилка разыграна вручную!</b>"
    await safe_send(bot.send_message, cid,
        f"{head}\n\n"
        f"💰 Выигрыш: <b>${amount:.4f}</b>\n"
        f"🏆 Получатель: <b>{wname}</b> (ID <code>{wuid}</code>)\n"
        f"<i>Случайный из топ-10</i>")
    return True, f"${amount:.4f} → {wname}"


async def pot_payout_loop():
    await asyncio.sleep(30)
    last = None
    while True:
        now = datetime.now(TZ)
        today = now.date()
        if now.hour == POT_HOUR and last != today:
            last = today
            for cid in list(QUIZ_ENABLED):
                try:
                    await distribute_pot(cid, reason="auto")
                except Exception as e:
                    log.warning("pot auto: %s", e)
        await asyncio.sleep(60)


# ==================== ТУРНИР ЛОГИКА ====================


async def run_tournament(tid, chat_id, settings):
    TOURNAMENT_ACTIVE[chat_id] = tid
    total_q = int(settings["questions"])
    q_secs = int(settings["question_seconds"])
    prize = float(settings["prize"])
    difficulty = settings.get("difficulty", "medium")
    diff_label = DIFFICULTY_LABELS.get(difficulty, "🟡 Средне")

    try:
        await safe_send(bot.send_message, chat_id,
            f"🏁 <b>ТУРНИР НАЧАЛСЯ!</b>\n\n"
            f"🎯 Сложность: <b>{diff_label}</b>\n"
            f"❓ {total_q} вопросов подряд\n"
            f"⏱ {q_secs} сек на каждый\n"
            f"💰 Приз: <b>${prize:.2f}</b>\n\n"
            f"Побеждает тот, у кого больше правильных!")

        for q_num in range(total_q):
            q, answers, is_multi, _ = random_question(difficulty=difficulty)
            TOURNAMENT_STATE[chat_id] = {
                "tid": tid, "q_num": q_num,
                "answers": answers, "answered_by": None,
            }
            await safe_send(bot.send_message, chat_id,
                f"❓ <b>Вопрос {q_num + 1}/{total_q}</b>\n\n"
                f"{q}\n\n⏱ {q_secs} сек")

            for _ in range(q_secs):
                await asyncio.sleep(1)
                st = TOURNAMENT_STATE.get(chat_id)
                if st and st.get("answered_by"):
                    break

            st = TOURNAMENT_STATE.get(chat_id) or {}
            winner_uid = st.get("answered_by")

            if winner_uid:
                await tournament_inc_correct(tid, winner_uid)
                try:
                    p = await get_player(chat_id, winner_uid)
                    nm = p.get("first_name") or p.get("username") or str(winner_uid)
                    await safe_send(bot.send_message, chat_id,
                        f"✅ <b>{nm}</b> ответил правильно! +1")
                except Exception:
                    pass
            else:
                await safe_send(bot.send_message, chat_id,
                    f"⌛ Никто не ответил. Правильный: <b>{answers[0]}</b>")
            await asyncio.sleep(2)

        players = await tournament_get_players(tid)
        if not players:
            await safe_send(bot.send_message, chat_id, "❌ Никто не участвовал.")
            return

        top_correct = int(players[0]["correct"])
        winners = [p for p in players if int(p["correct"]) == top_correct]

        if top_correct == 0:
            await safe_send(bot.send_message, chat_id,
                "🏁 <b>Турнир окончен.</b>\nНикто не ответил. Приз не вручён.")
            await tournament_finish(tid, None)
            return

        winner = random.choice(winners) if len(winners) > 1 else winners[0]
        winner_id = int(winner["user_id"])
        await add_balance(chat_id, winner_id, prize)
        await tournament_finish(tid, winner_id)

        try:
            p = await get_player(chat_id, winner_id)
            nm = p.get("first_name") or p.get("username") or str(winner_id)
        except Exception:
            nm = str(winner_id)

        lines = ["🏆 <b>ТУРНИР ЗАВЕРШЁН!</b>", ""]
        medals = ["🥇", "🥈", "🥉"]
        for i, pl in enumerate(players[:10]):
            uid_p = int(pl["user_id"])
            try:
                pp = await get_player(chat_id, uid_p)
                pnm = pp.get("first_name") or pp.get("username") or str(uid_p)
            except Exception:
                pnm = str(uid_p)
            med = medals[i] if i < 3 else f"{i + 1}."
            lines.append(f"{med} {pnm} — {pl['correct']} прав.")
        lines.append("")
        lines.append(f"👑 <b>Победитель: {nm}</b>")
        lines.append(f"💰 Приз: <b>${prize:.2f}</b>")
        await safe_send(bot.send_message, chat_id, "\n".join(lines))
    finally:
        TOURNAMENT_ACTIVE.pop(chat_id, None)
        TOURNAMENT_STATE.pop(chat_id, None)


# ==================== МЕНЮ ТУРНИРА ====================


def turik_kb(s):
    diff = s.get("difficulty", "medium")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"👥 Мин. игроков: {s['min_players']}", callback_data="turik:set:min_players"),
         InlineKeyboardButton(text=f"⏱ Лобби: {s['lobby_seconds']}с", callback_data="turik:set:lobby_seconds")],
        [InlineKeyboardButton(text=f"⏱ На вопрос: {s['question_seconds']}с", callback_data="turik:set:question_seconds"),
         InlineKeyboardButton(text=f"❓ Вопросов: {s['questions']}", callback_data="turik:set:questions")],
        [InlineKeyboardButton(text=f"💰 Приз: ${float(s['prize']):.2f}", callback_data="turik:set:prize")],
        [InlineKeyboardButton(text=f"🎯 Сложность: {DIFFICULTY_LABELS.get(diff, diff)}", callback_data="turik:difficulty")],
        [InlineKeyboardButton(text="🚀 Запустить турнир", callback_data="turik:start")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="turik:refresh")],
    ])


def turik_text(s):
    diff = s.get("difficulty", "medium")
    return (f"🏁 <b>Настройки турнира</b>\n\n"
            f"👥 Минимум игроков: <b>{s['min_players']}</b>\n"
            f"⏱ Лобби: <b>{s['lobby_seconds']}</b> сек\n"
            f"⏱ На вопрос: <b>{s['question_seconds']}</b> сек\n"
            f"❓ Вопросов: <b>{s['questions']}</b>\n"
            f"💰 Приз: <b>${float(s['prize']):.2f}</b>\n"
            f"🎯 Сложность: <b>{DIFFICULTY_LABELS.get(diff, diff)}</b>\n\n"
            f"Жми кнопку → напиши число в чат.")


@dp.message(Command("AiTurik"))
async def cmd_turik(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    if message.chat.type not in ("group", "supergroup"):
        return
    TOURNAMENT_EDIT.pop(message.from_user.id, None)
    s = await get_t_settings(message.chat.id)
    await safe_send(message.reply, turik_text(s), reply_markup=turik_kb(s))


@dp.message(F.chat.type.in_({"group", "supergroup"}), F.text, ~F.text.startswith("/"))
async def turik_value_input(message: Message):
    if not message.from_user or message.from_user.id not in TOURNAMENT_EDIT:
        raise SkipHandler()
    edit = TOURNAMENT_EDIT[message.from_user.id]
    if edit["chat_id"] != message.chat.id:
        raise SkipHandler()

    field = edit["field"]
    raw = message.text.strip().replace(",", ".")

    try:
        if field == "prize":
            val = float(raw)
            if not (0.1 <= val <= 10):
                raise ValueError
            val = round(val, 4)
        else:
            val = int(raw)
            ranges = {
                "min_players": (2, 50),
                "lobby_seconds": (10, 600),
                "question_seconds": (5, 300),
                "questions": (3, 50),
            }
            lo, hi = ranges[field]
            if not (lo <= val <= hi):
                raise ValueError
    except ValueError:
        await safe_send(message.reply, "❌ Неверное значение. Попробуй снова или /AiTurik для отмены.")
        return

    await update_t_setting(edit["chat_id"], field, val)
    TOURNAMENT_EDIT.pop(message.from_user.id, None)

    s = await get_t_settings(edit["chat_id"])
    try:
        await message.delete()
    except Exception:
        pass
    await safe_send(message.answer, turik_text(s), reply_markup=turik_kb(s))


@dp.callback_query(F.data.startswith("turik:"))
async def on_turik_cb(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        await cb.answer("⛔", show_alert=True)
        return
    if not isinstance(cb.message, Message):
        await cb.answer()
        return
    cid = cb.message.chat.id
    parts = cb.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "refresh":
        s = await get_t_settings(cid)
        try:
            await cb.message.edit_text(turik_text(s), reply_markup=turik_kb(s))
        except Exception:
            pass
        await cb.answer()
        return

    if action == "menu":
        s = await get_t_settings(cid)
        try:
            await cb.message.edit_text(turik_text(s), reply_markup=turik_kb(s))
        except Exception:
            pass
        await cb.answer()
        return

    if action == "difficulty":
        s = await get_t_settings(cid)
        cur = s.get("difficulty", "medium")
        def m(d):
            mark = "✅ " if d == cur else ""
            return f"{mark}{DIFFICULTY_LABELS[d]}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=m("easy"), callback_data="turik:diff_set:easy")],
            [InlineKeyboardButton(text=m("medium"), callback_data="turik:diff_set:medium")],
            [InlineKeyboardButton(text=m("hard"), callback_data="turik:diff_set:hard")],
            [InlineKeyboardButton(text=m("extreme"), callback_data="turik:diff_set:extreme")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="turik:menu")],
        ])
        try:
            await cb.message.edit_text(
                f"🎯 <b>Сложность турнира</b>\n\n"
                f"🟢 Легко — простые вопросы\n"
                f"🟡 Средне — обычные\n"
                f"🟠 Сложно — посложнее\n"
                f"🔴 Экстрим — только хардкор",
                reply_markup=kb,
            )
        except Exception:
            pass
        await cb.answer()
        return

    if action == "diff_set":
        d = parts[2] if len(parts) > 2 else ""
        if d not in ("easy", "medium", "hard", "extreme"):
            await cb.answer("Ошибка", show_alert=True)
            return
        await update_t_setting(cid, "difficulty", d)
        await cb.answer(f"Сложность: {DIFFICULTY_LABELS[d]}")
        s = await get_t_settings(cid)
        try:
            await cb.message.edit_text(turik_text(s), reply_markup=turik_kb(s))
        except Exception:
            pass
        return

    if action == "set":
        field = parts[2]
        if field not in ("min_players", "lobby_seconds", "question_seconds", "questions", "prize"):
            await cb.answer("Ошибка", show_alert=True)
            return
        TOURNAMENT_EDIT[cb.from_user.id] = {"chat_id": cid, "field": field}
        prompts = {
            "min_players": "👥 Сколько минимум игроков для старта? (2-50)",
            "lobby_seconds": "⏱ Сколько секунд на регистрацию? (10-600)",
            "question_seconds": "⏱ Сколько секунд на один вопрос? (5-300)",
            "questions": "❓ Сколько всего вопросов? (3-50)",
            "prize": "💰 Какой приз в USDT? (0.1-10)",
        }
        await cb.answer("Жду число...")
        await safe_send(cb.message.answer, prompts[field] + "\n\n<i>Отмена: /AiTurik</i>")
        return

    if action == "start":
        if cid in TOURNAMENT_ACTIVE or cid in TOURNAMENT_LOBBY:
            await cb.answer("⏳ Уже идёт.", show_alert=True)
            return

        s = await get_t_settings(cid)
        tid = await tournament_create(cid, float(s["prize"]))
        if not tid:
            await cb.answer("Ошибка", show_alert=True)
            return

        TOURNAMENT_LOBBY[tid] = {"chat_id": cid, "players": set(), "started": False, "settings": s}
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎮 Участвовать", callback_data=f"tourn:join:{tid}"),
        ]])
        sent = await safe_send(cb.message.answer,
            f"🏁 <b>ТУРНИР!</b>\n\n"
            f"🎯 Сложность: <b>{DIFFICULTY_LABELS.get(s.get('difficulty','medium'), '?')}</b>\n"
            f"📝 Вопросов: <b>{s['questions']}</b>\n"
            f"⏱ Секунд на вопрос: <b>{s['question_seconds']}</b>\n"
            f"👥 Минимум участников: <b>{s['min_players']}</b>\n"
            f"💰 Приз: <b>${float(s['prize']):.2f}</b>\n\n"
            f"Регистрация: <b>{s['lobby_seconds']} сек</b>\n"
            f"Жми «🎮 Участвовать»!",
            reply_markup=kb)
        await cb.answer("Запущено")

        if not sent:
            return

        lobby_seconds = int(s["lobby_seconds"])
        min_players = int(s["min_players"])

        async def start_after_lobby():
            await asyncio.sleep(lobby_seconds)
            lobby = TOURNAMENT_LOBBY.pop(tid, None)
            if not lobby or lobby["started"]:
                return
            try:
                await bot.edit_message_reply_markup(cid, sent.message_id, reply_markup=None)
            except Exception:
                pass

            if len(lobby["players"]) < min_players:
                await safe_send(bot.send_message, cid,
                    f"❌ Турнир отменён: набралось {len(lobby['players'])}/{min_players}.")
                return

            await safe_send(bot.send_message, cid,
                f"👥 Участников: <b>{len(lobby['players'])}</b>\nЗапускаю через 3 сек...")
            await asyncio.sleep(3)
            asyncio.create_task(run_tournament(tid, cid, lobby["settings"]))

        asyncio.create_task(start_after_lobby())
        return


@dp.callback_query(F.data.startswith("tourn:join:"))
async def on_tournament_join(cb: CallbackQuery):
    if not cb.from_user or not isinstance(cb.message, Message):
        await cb.answer()
        return
    try:
        tid = int(cb.data.split(":")[2])
    except (ValueError, IndexError):
        await cb.answer("Ошибка", show_alert=True)
        return

    lobby = TOURNAMENT_LOBBY.get(tid)
    if not lobby:
        await cb.answer("Регистрация закрыта.", show_alert=True)
        return

    uid = cb.from_user.id
    if uid in lobby["players"]:
        await cb.answer("Ты уже участвуешь.")
        return

    lobby["players"].add(uid)
    await tournament_add_player(tid, uid)
    await cb.answer(f"✅ Записан! Всего: {len(lobby['players'])}")


# ==================== START / HELP / RULES ====================


@dp.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Подписка $0.50/нед", url=XROCKET_SUBSCRIBE_URL)],
        [InlineKeyboardButton(text="🔗 Партнёрка xRocket", url=XROCKET_REFERRAL_URL)],
    ])
    await safe_send(message.reply,
        f"👋 <b>Викторина с дуэлями!</b>\n\n"
        f"🎯 Квиз: правильный ответ → очки\n"
        f"📈 10 уровней за очки\n"
        f"🎲 Дуэли: <code>/AiDuel 0.20</code>\n"
        f"💳 Пополнить: <code>/AiDeposit 1.0</code>\n"
        f"💸 Вывод от ${MIN_WITHDRAW:.2f}\n\n"
        f"📖 /AiHelp — все команды\n"
        f"📜 /AiRules — правила",
        reply_markup=kb)


@dp.message(Command("AiHelp"))
async def cmd_aihelp(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    text = (
        "📖 <b>СПРАВКА</b>\n\n"
        "<b>🎮 Игра</b>\n"
        "/AiBalance — баланс и уровень\n"
        "/AiProfile — профиль\n"
        "/AiTop — топ-10\n"
        "/AiLevels — все уровни\n"
        "/AiCoins — экономика чата\n"
        "/AiDuel 0.20 — дуэль на кубах\n\n"
        "<b>💰 Деньги</b>\n"
        f"/AiDeposit 0.05 — пополнить (от ${DEPOSIT_MIN:.2f})\n"
        f"/AiWithdraw — вывод от ${MIN_WITHDRAW:.2f}\n"
        f"/AiSubscribe — подписка ×{SUBSCRIBER_MULTIPLIER}\n"
        f"/AiSponsor — купить спонсорские вопросы (в ЛС)\n\n"
        "<b>📜 Общее</b>\n"
        "/AiRules · /AiHelp\n"
    )
    if message.from_user and is_admin(message.from_user.id):
        text += (
            "\n<b>🛠 Админ</b>\n"
            "/AiAdmin — панель (сложность викторины, касса, баны)\n"
            "/AiTurik — настройка турниров\n"
            "\n<b>🎰 Управление копилкой</b>\n"
            "/AiPot — сколько сейчас в фонде\n"
            "/AiPotAdd &lt;сумма&gt; — пополнить фонд\n"
            "/AiPotTake &lt;сумма&gt; — снять из фонда себе\n"
            "/AiPotGive — раздать фонд сейчас\n"
            "\n/AiBan &lt;id&gt; [время] [причина]\n"
            "/AiUnban &lt;id&gt;\n"
        )
    await safe_send(message.reply, text)


@dp.message(Command("AiRules"))
async def cmd_airules(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    await safe_send(message.reply,
        "📜 <b>ПРАВИЛА</b>\n\n"
        "<b>1.</b> Оскорбления проекта, админов, участников — <b>бан</b>.\n"
        "<b>2.</b> Обход бана (твинк) — <b>перманентный бан</b>.\n"
        "<b>3.</b> Скрипты, боты, мультиаккаунты — <b>бан + сброс</b>.\n"
        "<b>4.</b> Спам ответами — <b>бан</b>.\n"
        "<b>5.</b> Фиктивные дуэли, сговор — <b>бан обоим</b>.\n"
        "<b>6.</b> Обман системы вывода — <b>бан + обнуление</b>.\n\n"
        f"💸 Вывод: от ${MIN_WITHDRAW:.2f} · лимит ${DAILY_WITHDRAW_LIMIT:.2f}/сутки\n"
        f"💳 Депозит: от ${DEPOSIT_MIN:.2f}\n"
        f"🎲 Рейк с дуэлей: {RAKE_PCT*100:.0f}%\n\n"
        "<i>Незнание правил не освобождает от ответственности.</i>")


# ==================== /AiCoins ====================


@dp.message(Command("AiCoins"))
async def cmd_aicoins(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    stats = await get_coins_stats(message.chat.id)
    if not stats:
        await safe_send(message.reply, "Ошибка при подсчёте.")
        return
    await safe_send(message.reply,
        f"💼 <b>Экономика чата</b>\n\n"
        f"👥 Игроков: <b>{stats['players']}</b>\n"
        f"💰 На балансах: <b>${stats['balance']:.4f}</b>\n"
        f"🎯 Очков всего: <b>{stats['points']}</b>\n\n"
        f"💸 Выплачено: <b>${stats['paid']:.4f}</b>\n"
        f"🏦 В кассе: <b>${stats['house']:.4f}</b>")


# ==================== /AiSponsor ====================


@dp.message(Command("AiSponsor"))
async def cmd_sponsor(message: Message):
    if message.chat.type != "private":
        await safe_send(message.reply, "Эта команда работает только в личке бота.")
        return

    uid = message.from_user.id

    if uid in SPONSOR_SESSION:
        session = SPONSOR_SESSION[uid]
        if not session.get("chat_id"):
            await safe_send(message.reply,
                "📌 Укажи <b>ID чата</b>, куда постить вопросы.\n"
                "Например: <code>-1002712583382</code>")
        else:
            left = session["target"] - session["collected"]
            await safe_send(message.reply,
                f"📝 Режим спонсора\n"
                f"Осталось собрать: <b>{left}</b> вопросов\n\n"
                f"Формат: <code>вопрос | ответ</code>")
        return

    parts = (message.text or "").split()
    if len(parts) != 2 or parts[1] != "5":
        await safe_send(message.reply,
            f"🎁 <b>Спонсорские вопросы</b>\n\n"
            f"Цена: <b>${SPONSOR_PRICE:.2f}</b> = <b>{SPONSOR_QUESTIONS} вопросов</b>\n\n"
            f"Твои вопросы появляются в чате по одному, в заданном порядке.\n\n"
            f"Для оплаты: <code>/AiSponsor 5</code>")
        return

    client_inv_id = f"sponsor_{uid}_{uuid.uuid4().hex[:12]}"

    ok, result = await xrocket_create_invoice(client_inv_id, SPONSOR_PRICE,
        f"Sponsor pack for quiz bot (user {uid})")
    if not ok:
        await safe_send(message.reply, f"❌ <code>{result}</code>")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить ${SPONSOR_PRICE:.2f}", url=result)],
    ])
    await create_invoice(client_inv_id, uid, 0, SPONSOR_PRICE, None, kind="sponsor")
    await safe_send(message.reply,
        f"🎁 <b>Пакет спонсора</b>\n\n"
        f"💰 Сумма: <b>${SPONSOR_PRICE:.2f}</b>\n"
        f"📝 Вопросов: <b>{SPONSOR_QUESTIONS}</b>\n\n"
        f"После оплаты бот попросит ID чата и примёт вопросы.",
        reply_markup=kb)


@dp.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def handle_sponsor_input(message: Message):
    if not message.from_user or not message.text:
        raise SkipHandler()
    uid = message.from_user.id
    session = SPONSOR_SESSION.get(uid)
    if not session:
        raise SkipHandler()

    if not session.get("chat_id"):
        try:
            chat_id = int(message.text.strip())
        except ValueError:
            await safe_send(message.reply, "❌ Неверный ID. Пример: <code>-1002712583382</code>")
            return
        session["chat_id"] = chat_id
        await safe_send(message.reply,
            f"✅ Чат: <code>{chat_id}</code>\n\n"
            f"Отправляй вопросы: <code>вопрос | ответ</code>\n"
            f"Осталось: <b>{session['target']}</b>")
        return

    text = message.text.strip()
    if "|" not in text:
        await safe_send(message.reply, "❌ Формат: <code>вопрос | ответ</code>")
        return

    q, a = text.split("|", 1)
    q, a = q.strip(), a.strip().lower()
    if not q or not a:
        await safe_send(message.reply, "❌ Пустой вопрос или ответ.")
        return

    position = session["collected"] + 1
    await sponsor_add(uid, session["chat_id"], q, a, position)
    session["collected"] += 1
    left = session["target"] - session["collected"]

    if left <= 0:
        await safe_send(message.reply,
            f"🎉 <b>Все {session['target']} вопросов приняты!</b>\n"
            f"Они появятся в чате по порядку. Спасибо!")
        SPONSOR_SESSION.pop(uid, None)
    else:
        await safe_send(message.reply, f"✅ Принято. Осталось: <b>{left}</b>")


# ==================== АДМИН-ПАНЕЛЬ ====================


def admin_kb(cid):
    enabled = cid in QUIZ_ENABLED
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏸ Выключить" if enabled else "▶️ Включить", callback_data="adm:toggle")],
        [InlineKeyboardButton(text="❓ Вопрос", callback_data="adm:ask"),
         InlineKeyboardButton(text="📊 Стата", callback_data="adm:stats")],
        [InlineKeyboardButton(text="💼 Касса", callback_data="adm:house"),
         InlineKeyboardButton(text="💸 Выплаты", callback_data="adm:payouts")],
        [InlineKeyboardButton(text="📋 Топ", callback_data="adm:top"),
         InlineKeyboardButton(text="🚫 Баны", callback_data="adm:bans")],
        [InlineKeyboardButton(text="🎯 Сложность", callback_data="adm:difficulty"),
         InlineKeyboardButton(text="🎰 Копилка", callback_data="adm:pot")],
        [InlineKeyboardButton(text="🧪 xRocket", callback_data="adm:xrdbg")],
    ])


def admin_text(cid):
    status = "🟢 включена" if cid in QUIZ_ENABLED else "🔴 выключена"
    current = ACTIVE_QUESTIONS.get(cid)
    cur_txt = f"\n🔓 Открыт: {current['question']}" if current else ""
    return (f"🛠 <b>Админ-панель</b>\n"
            f"Викторина: {status}\n"
            f"Рейк с дуэлей: <b>{RAKE_PCT*100:.0f}%</b>\n"
            f"Депозит: от ${DEPOSIT_MIN:.2f}\n"
            f"Вывод: от ${MIN_WITHDRAW:.2f}{cur_txt}")


@dp.message(Command("AiAdmin"))
async def cmd_aiadmin(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    if message.chat.type not in ("group", "supergroup"):
        return
    await safe_send(message.reply, admin_text(message.chat.id), reply_markup=admin_kb(message.chat.id))


@dp.message(Command("AiHouse"))
async def cmd_house(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    if message.chat.type not in ("group", "supergroup"):
        return
    total = await get_house_total(message.chat.id)
    total_all = await get_house_total(None)
    by = await get_house_by_source()
    lines = [f"💼 <b>Касса</b>\n",
             f"💰 В этом чате: <b>${total:.4f}</b>",
             f"📈 Всего: <b>${total_all:.4f}</b>\n"]
    if by:
        lines.append("<b>По источникам:</b>")
        for src, amt in sorted(by.items(), key=lambda x: -x[1]):
            lines.append(f"• {src}: ${amt:.4f}")
    await safe_send(message.reply, "\n".join(lines))


# ==================== КОПИЛКА ====================


@dp.message(Command("AiPot"))
async def cmd_aipot(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    if message.chat.type not in ("group", "supergroup"):
        return
    pot = await get_pot(message.chat.id)
    await safe_send(message.reply,
        f"🎰 <b>Копилка чата</b>\n\n"
        f"Сейчас в фонде: <b>${pot:.4f}</b>\n\n"
        f"/AiPotAdd &lt;сумма&gt; — пополнить\n"
        f"/AiPotTake &lt;сумма&gt; — снять себе\n"
        f"/AiPotGive — раздать сейчас\n\n"
        f"Авто-раздача в <b>{POT_HOUR}:00 МСК</b>\n"
        f"Пополнение: {int(POT_PERCENT*100)}% с каждой награды.")


@dp.message(Command("AiPotAdd"))
async def cmd_aipotadd(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await safe_send(message.reply, "Формат: <code>/AiPotAdd 0.50</code>")
        return
    try:
        amount = round(float(parts[1]), 4)
    except ValueError:
        await safe_send(message.reply, "Сумма — число.")
        return
    if amount <= 0:
        await safe_send(message.reply, "Сумма должна быть больше нуля.")
        return
    new_pot = await add_to_pot(message.chat.id, amount)
    if new_pot is None:
        await safe_send(message.reply, "Ошибка при пополнении.")
        return
    await safe_send(message.reply,
        f"✅ В копилку добавлено <b>${amount:.4f}</b>\n"
        f"🎰 Теперь в фонде: <b>${new_pot:.4f}</b>")


@dp.message(Command("AiPotTake"))
async def cmd_aipottake(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await safe_send(message.reply, "Формат: <code>/AiPotTake 0.50</code>")
        return
    try:
        amount = round(float(parts[1]), 4)
    except ValueError:
        await safe_send(message.reply, "Сумма — число.")
        return
    if amount <= 0:
        await safe_send(message.reply, "Сумма должна быть больше нуля.")
        return
    ok, new_pot = await pot_take(message.chat.id, amount)
    if not ok:
        await safe_send(message.reply,
            f"❌ В копилке только <b>${new_pot:.4f}</b>, снять ${amount:.4f} нельзя.")
        return
    await add_balance(message.chat.id, message.from_user.id, amount)
    await safe_send(message.reply,
        f"✅ Из копилки снято <b>${amount:.4f}</b>\n"
        f"💰 Зачислено тебе на баланс\n"
        f"🎰 Осталось в фонде: <b>${new_pot:.4f}</b>")


@dp.message(Command("AiPotGive"))
async def cmd_aipotgive(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    if message.chat.type not in ("group", "supergroup"):
        return
    ok, info = await distribute_pot(message.chat.id, reason="manual")
    if not ok:
        await safe_send(message.reply, f"❌ {info}")


# ==================== BAN ====================


def parse_duration(s):
    s = s.lower().strip()
    if s in ("perm", "forever", "навсегда", "permanent"):
        return None
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if s and s[-1] in units:
        try:
            return timedelta(seconds=int(s[:-1]) * units[s[-1]])
        except ValueError:
            return "error"
    return "error"


@dp.message(Command("AiBan"))
async def cmd_aiban(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 2:
        await safe_send(message.reply, "📛 <code>/AiBan &lt;id&gt; [время] [причина]</code>")
        return
    try:
        target = int(parts[1])
    except ValueError:
        await safe_send(message.reply, "user_id — целое число.")
        return
    duration = None
    reason = "без причины"
    banned_until = None
    if len(parts) >= 3:
        parsed = parse_duration(parts[2])
        if parsed == "error":
            reason = " ".join(parts[2:])
        else:
            duration = parsed
            reason = parts[3] if len(parts) > 3 else "без причины"
            if duration is not None:
                banned_until = (datetime.now(timezone.utc) + duration).isoformat()
    await ban_user(message.chat.id, target, reason, message.from_user.id, banned_until)

    if banned_until:
        await safe_send(message.reply,
            f"🔨 <code>{target}</code> забанен до <b>{banned_until[:19].replace('T', ' ')} UTC</b>\n"
            f"Причина: {reason}")
    else:
        await safe_send(message.reply,
            f"🔨 <code>{target}</code> забанен <b>навсегда</b>\nПричина: {reason}")


@dp.message(Command("AiUnban"))
async def cmd_aiunban(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await safe_send(message.reply, "Формат: <code>/AiUnban &lt;user_id&gt;</code>")
        return
    try:
        target = int(parts[1])
    except ValueError:
        await safe_send(message.reply, "user_id — целое.")
        return
    await unban_user(message.chat.id, target)
    await safe_send(message.reply, f"✅ <code>{target}</code> разбанен.")


# ==================== CALLBACK ====================


@dp.callback_query(F.data.startswith("adm:"))
async def on_admin_cb(cb: CallbackQuery):
    if not cb.from_user or not is_admin(cb.from_user.id):
        await cb.answer("⛔", show_alert=True)
        return
    if not isinstance(cb.message, Message):
        await cb.answer()
        return
    cid = cb.message.chat.id
    action = cb.data.split(":")[1]

    if action == "toggle":
        if cid in QUIZ_ENABLED:
            QUIZ_ENABLED.discard(cid)
            ACTIVE_QUESTIONS.pop(cid, None)
            await clear_active(cid)
            await cb.answer("Выключено")
        else:
            QUIZ_ENABLED.add(cid)
            await cb.answer("Включено")
        try:
            await cb.message.edit_text(admin_text(cid), reply_markup=admin_kb(cid))
        except Exception:
            pass
        return

    if action == "ask":
        QUIZ_ENABLED.add(cid)
        await cb.answer("Задаю...")
        asyncio.create_task(ask_question(cid))
        return

    if action == "difficulty":
        s = await get_chat_settings(cid)
        cur = s.get("difficulty", "medium")
        def m(d):
            mark = "✅ " if d == cur else ""
            return f"{mark}{DIFFICULTY_LABELS[d]}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=m("easy"), callback_data="adm:diff_set:easy")],
            [InlineKeyboardButton(text=m("medium"), callback_data="adm:diff_set:medium")],
            [InlineKeyboardButton(text=m("hard"), callback_data="adm:diff_set:hard")],
            [InlineKeyboardButton(text=m("extreme"), callback_data="adm:diff_set:extreme")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:menu")],
        ])
        try:
            await cb.message.edit_text(
                f"🎯 <b>Сложность викторины</b>\n\n"
                f"Текущая: <b>{DIFFICULTY_LABELS.get(cur, cur)}</b>\n\n"
                f"Награда за ответ:\n"
                f"🟢 Легко — +1 очко\n"
                f"🟡 Средне — +2 очка\n"
                f"🟠 Сложно — +3 очка\n"
                f"🔴 Экстрим — +5 очков",
                reply_markup=kb,
            )
        except Exception:
            pass
        await cb.answer()
        return

    if action == "diff_set":
        d = cb.data.split(":")[2]
        if d not in ("easy", "medium", "hard", "extreme"):
            await cb.answer("Ошибка", show_alert=True)
            return
        await update_chat_setting(cid, "difficulty", d)
        await cb.answer(f"Установлено: {DIFFICULTY_LABELS[d]}")
        s = await get_chat_settings(cid)
        cur = s.get("difficulty", "medium")
        def m(x):
            mark = "✅ " if x == cur else ""
            return f"{mark}{DIFFICULTY_LABELS[x]}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=m("easy"), callback_data="adm:diff_set:easy")],
            [InlineKeyboardButton(text=m("medium"), callback_data="adm:diff_set:medium")],
            [InlineKeyboardButton(text=m("hard"), callback_data="adm:diff_set:hard")],
            [InlineKeyboardButton(text=m("extreme"), callback_data="adm:diff_set:extreme")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:menu")],
        ])
        try:
            await cb.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        return

    if action == "menu":
        try:
            await cb.message.edit_text(admin_text(cid), reply_markup=admin_kb(cid))
        except Exception:
            pass
        await cb.answer()
        return

    if action == "stats":
        await cb.answer("Собираю...")
        players, payouts, bans, subs = await get_stats()
        tb = sum(float(p["balance"]) for p in players)
        tc = sum(int(p["correct_answers"]) for p in players)
        fin = [p for p in payouts if p["status"] == "finished"]
        ps = sum(float(p["amount"]) for p in fin)
        house = await get_house_total(cid)
        pot = await get_pot(cid)
        await safe_send(cb.message.answer,
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Игроков: {len(players)}\n"
            f"🏆 Очков: {tc}\n"
            f"💎 Подписчиков: {len(subs)}\n"
            f"🚫 Забанено: {len(bans)}\n\n"
            f"💰 Балансов: ${tb:.4f}\n"
            f"💼 Касса: ${house:.4f}\n"
            f"🎰 Копилка: ${pot:.4f}\n"
            f"💸 Выплат: {len(fin)} (${ps:.4f})")
        return

    if action == "pot":
        await cb.answer()
        pot = await get_pot(cid)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎉 Раздать сейчас", callback_data="adm:potgive")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:pot")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:menu")],
        ])
        await safe_send(cb.message.answer,
            f"🎰 <b>Копилка чата</b>\n\n"
            f"Сейчас в фонде: <b>${pot:.4f}</b>\n\n"
            f"Пополнить: <code>/AiPotAdd 0.50</code>\n"
            f"Снять: <code>/AiPotTake 0.50</code>\n"
            f"Авто-раздача в <b>{POT_HOUR}:00 МСК</b>",
            reply_markup=kb)
        return

    if action == "potgive":
        await cb.answer("Раздаю...")
        ok, info = await distribute_pot(cid, reason="manual")
        if not ok:
            await safe_send(cb.message.answer, f"❌ {info}")
        return

    if action == "house":
        await cb.answer()
        total = await get_house_total(cid)
        total_all = await get_house_total(None)
        by = await get_house_by_source()
        lines = [f"💼 <b>Касса</b>\n",
                 f"💰 В этом чате: <b>${total:.4f}</b>",
                 f"📈 Всего: <b>${total_all:.4f}</b>\n"]
        if by:
            lines.append("<b>По источникам:</b>")
            for src, amt in sorted(by.items(), key=lambda x: -x[1]):
                lines.append(f"• {src}: ${amt:.4f}")
        await safe_send(cb.message.answer, "\n".join(lines))
        return

    if action == "payouts":
        await cb.answer()
        rows = await get_payouts(20)
        if not rows:
            await safe_send(cb.message.answer, "Выплат не было.")
            return
        lines = ["💸 <b>Выплаты</b>"]
        for p in rows:
            dt = (p.get("created_at") or "")[:19].replace("T", " ")
            em = "✅" if p["status"] == "finished" else "❌"
            lines.append(f"{em} ${float(p['amount']):.4f} · <code>{p['user_id']}</code> · {dt}")
        await safe_send(cb.message.answer, "\n".join(lines))
        return

    if action == "top":
        await cb.answer()
        rows = await get_top(cid, 10)
        if not rows:
            await safe_send(cb.message.answer, "Никто не играл.")
            return
        lines = ["🏆 <b>Топ по очкам</b>"]
        for i, row in enumerate(rows, 1):
            ca = int(row.get("correct_answers", 0))
            _, emoji, _, _, _, _ = level_info(ca)
            nm = row.get("first_name") or row.get("username") or str(row["user_id"])
            medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
            lines.append(f"{medal} {emoji} {nm} — {ca} очк. · ${float(row['balance']):.4f}")
        await safe_send(cb.message.answer, "\n".join(lines))
        return

    if action == "bans":
        await cb.answer()
        def _q():
            try:
                return supabase.table("quiz_bans").select("*").eq("chat_id", cid).execute().data or []
            except Exception:
                return []
        rows = await asyncio.to_thread(_q)
        if not rows:
            await safe_send(cb.message.answer, "Забаненных нет.")
            return
        lines = ["🚫 <b>Забаненные</b>"]
        for b in rows:
            until = b.get("banned_until")
            if until:
                lines.append(f"<code>{b['user_id']}</code> — до {until[:19].replace('T', ' ')} UTC · {b.get('reason','—')}")
            else:
                lines.append(f"<code>{b['user_id']}</code> — навсегда · {b.get('reason','—')}")
        await safe_send(cb.message.answer, "\n".join(lines))
        return

    if action == "xrdbg":
        await cb.answer("Проверяю...")
        msg = await safe_send(cb.message.answer, "⏳ Проверка...")
        info = await xrocket_debug()
        if msg:
            try:
                await msg.edit_text(info)
            except Exception:
                pass
        return


async def xrocket_debug():
    lines = ["🧪 <b>xRocket</b>", f"Key: <code>{XROCKET_API_KEY[:12]}...</code>", ""]
    if not XROCKET_API_KEY:
        lines.append("❌ XROCKET_API_KEY пуст")
        return "\n".join(lines)
    try:
        s = await get_http()
        for url in (f"{XROCKET_BASE}/api/v1/me", f"{XROCKET_BASE}/api/v1/balance"):
            try:
                async with s.get(url, headers={"Authorization": f"Bearer {XROCKET_API_KEY}"}) as r:
                    text = (await r.text())[:200]
                    lines.append(f"[{r.status}] <code>{url}</code>\n<code>{text}</code>\n")
            except Exception as e:
                lines.append(f"[ERR] {url}: <code>{e}</code>")
    except Exception as e:
        lines.append(f"[ERR] {e}")
    return "\n".join(lines)


# ==================== ИГРОВЫЕ КОМАНДЫ ====================


@dp.message(Command("AiSubscribe"))
async def cmd_aisub(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Оформить $0.50/нед", url=XROCKET_SUBSCRIBE_URL)],
    ])
    await safe_send(message.reply,
        f"💎 <b>Подписка ×{SUBSCRIBER_MULTIPLIER}</b>\n\n"
        f"Цена: <b>${SUBSCRIPTION_PRICE:.2f}/нед</b>\n"
        f"• ×{SUBSCRIBER_MULTIPLIER} к очкам\n"
        f"• Бейдж 💎\n\n"
        f"Оформить через @xrocket:",
        reply_markup=kb)


@dp.message(Command("AiBalance"))
async def cmd_aibalance(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    p = await get_player(message.chat.id, message.from_user.id,
                         message.from_user.username, message.from_user.first_name)
    today = await withdrawn_today(message.chat.id, message.from_user.id)
    ca = int(p["correct_answers"])
    _, _, _, _, title_str, _ = level_info(ca)
    bar = make_progress_bar(ca)
    sub = is_subscriber_cached(message.chat.id, message.from_user.id)
    sub_line = f"\n💎 Подписка · ×{SUBSCRIBER_MULTIPLIER}" if sub else ""
    await safe_send(message.reply,
        f"💰 <b>${float(p['balance']):.4f} USDT</b>\n"
        f"🎖 {title_str}\n"
        f"🏆 Очков: <b>{ca}</b>\n"
        f"<code>{bar}</code>{sub_line}\n"
        f"💸 Выведено сегодня: ${today:.4f} / ${DAILY_WITHDRAW_LIMIT:.2f}\n"
        f"💳 Пополнить: /AiDeposit")


@dp.message(Command("AiProfile"))
async def cmd_aiprofile(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    p = await get_player(message.chat.id, message.from_user.id,
                         message.from_user.username, message.from_user.first_name)
    ca = int(p["correct_answers"])
    lvl, _, _, _, title_str, _ = level_info(ca)
    bar = make_progress_bar(ca)
    sub = is_subscriber_cached(message.chat.id, message.from_user.id)

    def _place():
        try:
            res = supabase.table("quiz_players").select("user_id").eq(
                "chat_id", message.chat.id).order("correct_answers", desc=True).execute()
            for i, row in enumerate(res.data or [], 1):
                if int(row["user_id"]) == message.from_user.id:
                    return i
        except Exception:
            pass
        return None
    place = await asyncio.to_thread(_place)
    place_str = f"#{place}" if place else "—"
    today = await withdrawn_today(message.chat.id, message.from_user.id)

    if lvl >= MAX_LEVEL:
        next_line = "🏆 Максимальный уровень!"
    else:
        in_lvl = ca - (lvl - 1) * ANSWERS_PER_LEVEL
        rem = ANSWERS_PER_LEVEL - in_lvl
        next_line = f"⬆️ До {LEVELS[lvl][1]} <b>{LEVELS[lvl][2]}</b>: <b>{rem}</b> очк."

    sub_line = f"\n💎 Подписка: <b>активна</b>" if sub else "\n💎 Подписка: нет"
    await safe_send(message.reply,
        f"👤 <b>{message.from_user.first_name}</b>\n\n"
        f"🎖 <b>{title_str}</b>{sub_line}\n\n"
        f"<code>{bar}</code>\n{next_line}\n\n"
        f"💰 Баланс: <b>${float(p['balance']):.4f}</b>\n"
        f"💸 Выведено: ${today:.4f}\n"
        f"🏆 Очков: <b>{ca}</b>\n"
        f"📍 Место: <b>{place_str}</b>")


@dp.message(Command("AiLevels"))
async def cmd_ailevels(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    lines = ["🎖 <b>Уровни</b>\n"]
    for lvl, emoji, name in LEVELS:
        mn = (lvl - 1) * ANSWERS_PER_LEVEL
        req = f"{mn}+" if lvl == MAX_LEVEL else f"{mn}-{mn + ANSWERS_PER_LEVEL - 1}"
        lines.append(f"{emoji} <b>Ур. {lvl}</b> · {name} · <i>{req} очк.</i>")
    await safe_send(message.reply, "\n".join(lines))


@dp.message(Command("AiTop"))
async def cmd_aitop(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    rows = await get_top(message.chat.id, 10)
    if not rows:
        await safe_send(message.reply, "Никто не играл.")
        return
    admin_view = message.from_user and is_admin(message.from_user.id)
    lines = ["🏆 <b>Топ по очкам</b>"]
    for i, row in enumerate(rows, 1):
        ca = int(row.get("correct_answers", 0))
        _, emoji, _, _, _, _ = level_info(ca)
        nm = row.get("first_name") or row.get("username") or str(row["user_id"])
        medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
        uid = f" · <code>{row['user_id']}</code>" if admin_view else ""
        lines.append(f"{medal} {emoji} {nm} — {ca} очк. · ${float(row['balance']):.4f}{uid}")
    await safe_send(message.reply, "\n".join(lines))


# ==================== ДЕПОЗИТ ====================


@dp.message(Command("AiDeposit"))
async def cmd_deposit(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await safe_send(message.reply,
            f"💳 <b>Пополнение</b>\n\n"
            f"Формат: <code>/AiDeposit 1.0</code>\n"
            f"Мин: ${DEPOSIT_MIN:.2f} · Макс: ${DEPOSIT_MAX:.2f}")
        return
    try:
        amount = round(float(parts[1]), 4)
    except ValueError:
        await safe_send(message.reply, "Сумма — число.")
        return
    if amount < DEPOSIT_MIN or amount > DEPOSIT_MAX:
        await safe_send(message.reply, f"Сумма: ${DEPOSIT_MIN:.2f} — ${DEPOSIT_MAX:.2f}")
        return

    client_inv_id = f"dep_{message.from_user.id}_{uuid.uuid4().hex[:12]}"
    ok, result = await xrocket_create_invoice(client_inv_id, amount,
        f"Deposit (user {message.from_user.id})")
    if not ok:
        await safe_send(message.reply, f"❌ <code>{result}</code>")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить ${amount:.2f}", url=result)],
    ])
    sent = await safe_send(message.reply,
        f"💳 <b>Счёт на пополнение</b>\n\n"
        f"Сумма: <b>${amount:.4f}</b> USDT\n"
        f"Действителен 1 час.\n\n"
        f"Оплати в @xrocket — баланс зачислится автоматически.",
        reply_markup=kb)
    msg_id = sent.message_id if sent else None
    await create_invoice(client_inv_id, message.from_user.id, message.chat.id, amount, msg_id, kind="deposit")


# ==================== ДУЭЛИ ====================


@dp.message(Command("AiDuel"))
async def cmd_duel(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    cid, uid = message.chat.id, message.from_user.id
    if is_banned_cached(cid, uid):
        await safe_send(message.reply, "🚫 Ты в бане.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await safe_send(message.reply,
            f"🎲 <b>Дуэль на кубах</b>\n\n"
            f"Формат: <code>/AiDuel 0.20</code> — ответом на сообщение\n"
            f"Ставка: ${DUEL_MIN:.2f} — ${DUEL_MAX:.2f}")
        return
    try:
        amount = round(float(parts[1]), 4)
    except ValueError:
        await safe_send(message.reply, "Ставка — число.")
        return
    if amount < DUEL_MIN or amount > DUEL_MAX:
        await safe_send(message.reply, f"Ставка: ${DUEL_MIN:.2f} — ${DUEL_MAX:.2f}")
        return

    opponent_id = opponent_name = None
    if message.reply_to_message and message.reply_to_message.from_user:
        opp = message.reply_to_message.from_user
        opponent_id, opponent_name = opp.id, opp.first_name
    elif message.entities:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                opponent_id, opponent_name = ent.user.id, ent.user.first_name
                break

    if not opponent_id:
        await safe_send(message.reply, "Ответь на сообщение противника.")
        return
    if opponent_id == uid:
        await safe_send(message.reply, "Себе нельзя 😄")
        return
    if opponent_id == bot.id:
        await safe_send(message.reply, "С ботом нельзя 😄")
        return

    p_c = await get_player(cid, uid, message.from_user.username, message.from_user.first_name)
    p_o = await get_player(cid, opponent_id)
    if float(p_c["balance"]) < amount:
        await safe_send(message.reply, f"❌ У тебя ${float(p_c['balance']):.4f}")
        return
    if float(p_o["balance"]) < amount:
        await safe_send(message.reply, f"❌ У противника ${float(p_o['balance']):.4f}")
        return
    if uid in DUEL_BUSY or opponent_id in DUEL_BUSY:
        await safe_send(message.reply, "⏳ Кто-то уже в дуэли.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"duel:a:{uid}:{opponent_id}:{amount}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"duel:r:{uid}:{opponent_id}:{amount}"),
    ]])
    text = (f"🎲 <b>Дуэль!</b>\n\n"
            f"<b>{message.from_user.first_name}</b> вызывает <b>{opponent_name}</b>\n\n"
            f"💵 Ставка: <b>${amount:.4f}</b>\n"
            f"💰 Банк: <b>${amount*2:.4f}</b>\n"
            f"<i>Рейк {RAKE_PCT*100:.0f}%</i>")
    sent = await safe_send(message.reply, text, reply_markup=kb)
    if not sent:
        return

    async def auto_close():
        await asyncio.sleep(DUEL_TTL)
        try:
            await bot.edit_message_reply_markup(cid, sent.message_id, reply_markup=None)
            await bot.edit_message_text(chat_id=cid, message_id=sent.message_id,
                text=f"⌛ <b>Дуэль истекла.</b>\n{opponent_name} не ответил.")
        except Exception:
            pass
    asyncio.create_task(auto_close())


@dp.callback_query(F.data.startswith("duel:"))
async def on_duel_cb(cb: CallbackQuery):
    if not cb.from_user or not isinstance(cb.message, Message):
        await cb.answer()
        return
    parts = cb.data.split(":")
    if len(parts) != 5:
        await cb.answer("Ошибка", show_alert=True)
        return
    _, action, ch_s, op_s, amt_s = parts
    try:
        challenger_id = int(ch_s)
        opponent_id = int(op_s)
        amount = float(amt_s)
    except ValueError:
        await cb.answer("Ошибка", show_alert=True)
        return
    cid = cb.message.chat.id
    if cb.from_user.id != opponent_id:
        await cb.answer("Это не твой вызов.", show_alert=True)
        return
    if action == "r":
        try:
            await cb.message.edit_text(f"❌ <b>Отклонено.</b>\n{cb.from_user.first_name} отказался.")
        except Exception:
            pass
        await cb.answer("Отклонено")
        return

    await cb.answer("Поехали!")
    if challenger_id in DUEL_BUSY or opponent_id in DUEL_BUSY:
        try:
            await cb.message.edit_text("⏳ Кто-то уже в дуэли.")
        except Exception:
            pass
        return
    DUEL_BUSY.add(challenger_id)
    DUEL_BUSY.add(opponent_id)
    try:
        p_c = await get_player(cid, challenger_id)
        p_o = await get_player(cid, opponent_id)
        if float(p_c["balance"]) < amount or float(p_o["balance"]) < amount:
            try:
                await cb.message.edit_text("❌ У кого-то не хватает.")
            except Exception:
                pass
            return
        if not await deduct_balance(cid, challenger_id, amount):
            try:
                await cb.message.edit_text("❌ Не списать у вызывающего.")
            except Exception:
                pass
            return
        if not await deduct_balance(cid, opponent_id, amount):
            await add_balance(cid, challenger_id, amount)
            try:
                await cb.message.edit_text("❌ Не списать у соперника.")
            except Exception:
                pass
            return
        name_c = p_c.get("first_name") or str(challenger_id)
        name_o = p_o.get("first_name") or str(opponent_id)
        try:
            await cb.message.edit_text(
                f"🎲 <b>Дуэль началась!</b>\n\n💰 Банк: <b>${amount*2:.4f}</b>\n🎯 {name_c} vs {name_o}")
        except Exception:
            pass
        await asyncio.sleep(1)
        m1 = await safe_send(bot.send_dice, cid, emoji="🎲")
        r1 = m1.dice.value if m1 and m1.dice else 0
        await asyncio.sleep(2)
        m2 = await safe_send(bot.send_dice, cid, emoji="🎲")
        r2 = m2.dice.value if m2 and m2.dice else 0
        await asyncio.sleep(2)

        if r1 == r2:
            await add_balance(cid, challenger_id, amount)
            await add_balance(cid, opponent_id, amount)
            await safe_send(bot.send_message, cid,
                f"🤝 <b>Ничья! {r1} : {r2}</b>\nСтавки возвращены.")
            return

        winner_id, winner_name = (challenger_id, name_c) if r1 > r2 else (opponent_id, name_o)
        loser_id = opponent_id if winner_id == challenger_id else challenger_id

        total_pot = round(amount * 2, 4)
        rake = round(total_pot * RAKE_PCT, 4)
        payout = round(total_pot - rake, 4)
        await add_balance(cid, winner_id, payout)
        await log_house_income(cid, rake, "duel")

        await safe_send(bot.send_message, cid,
            f"🏆 <b>{winner_name} победил!</b>\n\n"
            f"🎲 {name_c}: <b>{r1}</b>\n🎲 {name_o}: <b>{r2}</b>\n\n"
            f"💰 Забирает: <b>${payout:.4f}</b>\n"
            f"<i>рейк {RAKE_PCT*100:.0f}% = ${rake:.4f}</i>")

        try:
            await bot.send_message(loser_id,
                f"💔 <b>Проиграл дуэль</b> против {winner_name}\n"
                f"Ставка <b>${amount:.4f}</b> списана.")
        except Exception:
            pass
    finally:
        DUEL_BUSY.discard(challenger_id)
        DUEL_BUSY.discard(opponent_id)


# ==================== ВЫВОД ====================


@dp.message(Command("AiWithdraw"))
async def cmd_aiwithdraw(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    cid, uid = message.chat.id, message.from_user.id
    if is_banned_cached(cid, uid):
        await safe_send(message.reply, "🚫 Ты в бане.")
        return
    p = await get_player(cid, uid, message.from_user.username, message.from_user.first_name)
    bal = float(p["balance"])
    if bal < MIN_WITHDRAW:
        await safe_send(message.reply, f"❌ Минимум ${MIN_WITHDRAW:.2f}. У тебя ${bal:.4f}")
        return
    today = await withdrawn_today(cid, uid)
    rem = DAILY_WITHDRAW_LIMIT - today
    if rem <= 0:
        await safe_send(message.reply, f"❌ Дневной лимит исчерпан.")
        return
    amount = min(bal, rem)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"wd:accept:{uid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd:reject:{uid}"),
    ]])
    text = (f"💸 <b>Вывод</b>\n\n"
            f"Сумма: <b>${amount:.4f}</b> USDT\n"
            f"Куда: ID <code>{uid}</code>\n\n"
            f"⚠️ Зайди в <a href=\"{XROCKET_REFERRAL_URL}\">@xrocket</a>.\n\n"
            f"Запрос: {WITHDRAW_CONFIRM_TTL // 60} мин.")
    sent = await safe_send(message.reply, text, reply_markup=kb)
    if not sent:
        return
    PENDING_WITHDRAWS[sent.message_id] = {"chat_id": cid, "user_id": uid, "amount": amount, "ts": time.time()}

    async def auto_cancel():
        await asyncio.sleep(WITHDRAW_CONFIRM_TTL)
        info = PENDING_WITHDRAWS.pop(sent.message_id, None)
        if not info:
            return
        try:
            await bot.edit_message_text(chat_id=cid, message_id=sent.message_id,
                text="⌛ <b>Запрос истёк.</b> /AiWithdraw снова.")
        except Exception:
            pass
    asyncio.create_task(auto_cancel())


@dp.callback_query(F.data.startswith("wd:"))
async def on_withdraw_cb(cb: CallbackQuery):
    if not cb.from_user or not isinstance(cb.message, Message):
        await cb.answer()
        return
    parts = cb.data.split(":")
    if len(parts) != 3:
        await cb.answer("Ошибка", show_alert=True)
        return
    action, owner_s = parts[1], parts[2]
    try:
        owner_id = int(owner_s)
    except ValueError:
        await cb.answer("Ошибка", show_alert=True)
        return
    if cb.from_user.id != owner_id:
        await cb.answer("⛔ Не твой запрос.", show_alert=True)
        return
    info = PENDING_WITHDRAWS.pop(cb.message.message_id, None)
    if not info:
        await cb.answer("⌛ Уже истёк.", show_alert=True)
        return
    cid, uid, amount = info["chat_id"], info["user_id"], info["amount"]

    if action == "reject":
        try:
            await cb.message.edit_text(f"❌ Отменено. ${amount:.4f} на балансе.")
        except Exception:
            pass
        await cb.answer("Отменено")
        return

    await cb.answer("Принято...")
    if not await try_lock_withdraw(cid, uid):
        try:
            await cb.message.edit_text("⏳ Другой вывод обрабатывается.")
        except Exception:
            pass
        return
    try:
        p = await get_player(cid, uid)
        bal = float(p["balance"])
        amount = min(bal, amount)
        if amount < MIN_WITHDRAW:
            try:
                await cb.message.edit_text(f"❌ Недостаточно. Минимум ${MIN_WITHDRAW:.2f}.")
            except Exception:
                pass
            return
        try:
            await cb.message.edit_text(f"⏳ Отправляю ${amount:.4f}...")
        except Exception:
            pass
        ok, result = await xrocket_payout(cid, uid, amount)
        if ok:
            await deduct_balance(cid, uid, amount)
            await log_payout(cid, uid, amount, result, "finished")
            try:
                await cb.message.edit_text(f"✅ <b>Выплачено ${amount:.4f}</b>\nID: <code>{result}</code>")
            except Exception:
                pass
        else:
            await log_payout(cid, uid, amount, "", "failed")
            try:
                await cb.message.edit_text(f"❌ <b>Ошибка</b>\n<code>{result}</code>")
            except Exception:
                pass
    finally:
        await unlock_withdraw(cid, uid)


# ==================== ОТВЕТЫ ====================


@dp.message(F.text & ~F.text.startswith("/") & F.chat.type.in_({"group", "supergroup"}))
async def handle_answer(message: Message):
    if not message.from_user:
        return

    if message.from_user.id in TOURNAMENT_EDIT:
        raise SkipHandler()

    cid, uid = message.chat.id, message.from_user.id
    text = (message.text or "").strip().lower()

    if cid in TOURNAMENT_ACTIVE:
        st = TOURNAMENT_STATE.get(cid)
        if st and not st.get("answered_by"):
            if text in st["answers"]:
                st["answered_by"] = uid
        return

    q = ACTIVE_QUESTIONS.get(cid)
    if not q:
        return
    if text not in q["answers"]:
        if is_admin(uid):
            try:
                await bot.set_message_reaction(cid, message.message_id, ["❌"])
            except Exception:
                pass
        return
    if is_banned_cached(cid, uid):
        return
    popped = ACTIVE_QUESTIONS.pop(cid, None)
    if popped is None:
        return

    tsk = popped.get("timer_task")
    if tsk:
        tsk.cancel()

    chat_settings = await get_chat_settings(cid)
    difficulty = chat_settings.get("difficulty", "medium")
    reward_points = DIFFICULTY_REWARDS.get(difficulty, 2)

    p = await get_player(cid, uid, message.from_user.username, message.from_user.first_name)
    old_lvl = level_from_correct(int(p["correct_answers"]))
    old_top1 = TOP_CACHE.get(cid)

    await asyncio.gather(
        clear_active(cid),
        add_score_multi(cid, uid, reward_points),
        return_exceptions=True,
    )

    new_correct = int(p["correct_answers"]) + reward_points
    new_lvl = level_from_correct(new_correct)

    new_top1 = await get_top1(cid)
    if new_top1 and new_top1 != old_top1:
        TOP_CACHE[cid] = new_top1
        if old_top1 is not None:
            try:
                tp = await get_player(cid, new_top1)
                nm = tp.get("first_name") or tp.get("username") or str(new_top1)
                await safe_send(bot.send_message, cid, f"👑 <b>{nm}</b> вышел на первое место!")
            except Exception:
                pass

    phrase = random.choice(CORRECT_PHRASES)
    if q["is_multi"]:
        answer_shown = "любой из: " + ", ".join(q["answers"][:5]) + ("..." if len(q["answers"]) > 5 else "")
    else:
        answer_shown = q["answers"][0]

    msg = (f"{phrase}\n"
           f"{message.from_user.first_name} +{reward_points} очк.\n"
           f"<i>Ответ: {answer_shown}</i>")

    if new_lvl > old_lvl:
        n_emoji = LEVELS[new_lvl - 1][1]
        n_name = LEVELS[new_lvl - 1][2]
        msg += f"\n\n{n_emoji} <b>НОВЫЙ УРОВЕНЬ {new_lvl}!</b>\n🎖 {n_name}"

    try:
        await bot.send_chat_action(cid, "typing")
    except Exception:
        pass

    sent = await safe_send(message.reply, msg)
    if sent:
        try:
            await bot.set_message_reaction(cid, message.message_id, ["✅"])
        except Exception:
            pass


# ==================== СТАРТ ====================


async def main():
    print("=" * 50)
    print("Quiz Bot · сложности · турниры · копилка · спонсоры")
    print(f"Easy: {len(EASY_QUESTIONS)} · Medium: {len(MEDIUM_QUESTIONS)} · Hard: {len(HARD_QUESTIONS)} · Extreme: {len(EXTREME_QUESTIONS)}")
    print(f"Админы: {sorted(ADMIN_IDS)}")

    await get_http()
    await asyncio.to_thread(unlock_all_withdrawals_sync)

    global BANNED_CACHE, SUBSCRIBERS_CACHE
    BANNED_CACHE = await asyncio.to_thread(load_bans_sync)
    SUBSCRIBERS_CACHE = await asyncio.to_thread(load_subscribers_sync)

    active_rows = await asyncio.to_thread(load_active_sync)
    for row in active_rows:
        answers = row["answer"].split("||")
        ACTIVE_QUESTIONS[int(row["chat_id"])] = {
            "question": row["question"],
            "answers": [a.lower() for a in answers],
            "is_multi": row.get("is_multi", False),
            "timer": False,
            "timer_task": None,
        }
        QUIZ_ENABLED.add(int(row["chat_id"]))

    for cid in QUIZ_ENABLED:
        try:
            t = await get_top1(cid)
            if t:
                TOP_CACHE[cid] = t
        except Exception:
            pass

    me = await bot.get_me()
    print(f"Подключился как @{me.username}")

    await start_webhook_server()

    asyncio.create_task(question_scheduler())
    asyncio.create_task(caches_refresh_loop())
    asyncio.create_task(pot_payout_loop())
    print("Запущен.")
    print("=" * 50)

    try:
        await dp.start_polling(bot)
    finally:
        await close_http()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("!!! УПАЛ !!!")
        print(type(e).__name__, "-", e)
        raise
