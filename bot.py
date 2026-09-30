import asyncio
import io
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from PIL import Image, ImageDraw, ImageFont
from supabase import create_client, Client

from questions import QUESTIONS, MULTI_QUESTIONS, random_question

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
XROCKET_BASE = "https://pay.api.xrocket.exchange"

XROCKET_SUBSCRIBE_URL = os.getenv("XROCKET_SUBSCRIBE_URL", "https://t.me/xRocket")
XROCKET_REFERRAL_URL = os.getenv("XROCKET_REFERRAL_URL", "https://t.me/xRocket")

BASE_REWARD = 0.05
REWARD_STEP = 0.005
ANSWERS_PER_LEVEL = 10
MAX_LEVEL = 10

SUBSCRIBER_MULTIPLIER = 2.0
SUBSCRIPTION_PRICE = 0.50

MIN_WITHDRAW = 0.05
DAILY_WITHDRAW_LIMIT = 1.00

POT_PERCENT = 0.05
POT_HOUR = 21

# Дуэли
DUEL_MIN = 0.05
DUEL_MAX = 1.00
DUEL_TTL = 120

ADMIN_IDS = {8130244626, 6173495222}

TZ = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))
WORK_HOURS = list(range(8, 24))
WITHDRAW_CONFIRM_TTL = 120

CORRECT_PHRASES = [
    "🎉 <b>Правильно!</b>",
    "🔥 <b>В точку!</b>",
    "💎 <b>Красавчик!</b>",
    "⚡ <b>Молниеносно!</b>",
    "🧠 <b>Умница!</b>",
    "🏆 <b>Есть!</b>",
    "✨ <b>Верно!</b>",
    "🚀 <b>Полетели!</b>",
    "🎯 <b>Точно в цель!</b>",
    "🌟 <b>Блестяще!</b>",
]

LEVELS = [
    (1, "🐣", "Новичок"),
    (2, "🥚", "Ученик"),
    (3, "🐥", "Знаток"),
    (4, "🦅", "Эксперт"),
    (5, "🧠", "Мастер"),
    (6, "🎓", "Гуру"),
    (7, "💎", "Легенда"),
    (8, "👑", "Гений"),
    (9, "🔥", "Титан"),
    (10, "⚡", "Бог викторины"),
]


def level_from_correct(c):
    return min(c // ANSWERS_PER_LEVEL + 1, MAX_LEVEL)


def level_info(c):
    lvl = level_from_correct(c)
    emoji, name = LEVELS[lvl - 1][1], LEVELS[lvl - 1][2]
    reward = BASE_REWARD + (lvl - 1) * REWARD_STEP
    title_str = f"{emoji} {name} (ур. {lvl})"
    if lvl >= MAX_LEVEL:
        progress = "🏆 Максимальный уровень!"
    else:
        in_level = c - (lvl - 1) * ANSWERS_PER_LEVEL
        remaining = ANSWERS_PER_LEVEL - in_level
        progress = f"до след. уровня: {remaining} отв."
    return lvl, emoji, name, reward, title_str, progress


def reward_for(c, is_sub=False):
    lvl = level_from_correct(c)
    base = BASE_REWARD + (lvl - 1) * REWARD_STEP
    if is_sub:
        base *= SUBSCRIBER_MULTIPLIER
    return round(base, 4)


def make_progress_bar(c):
    lvl = level_from_correct(c)
    if lvl >= MAX_LEVEL:
        return "▓" * 10 + " 10/10"
    in_level = c - (lvl - 1) * ANSWERS_PER_LEVEL
    return f"{'▓' * in_level}{'▒' * (ANSWERS_PER_LEVEL - in_level)} {in_level}/{ANSWERS_PER_LEVEL}"


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
DUEL_BUSY = set()   # user_ids, которые уже в дуэли
HTTP_SESSION = None

_FONT_PATH = None


def is_admin(uid):
    return uid in ADMIN_IDS


# ==================== FONT ====================


def get_font(size=64):
    global _FONT_PATH
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
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

    x = (W - tw) / 2 - bbox[0]
    y = (H - th) / 2 - bbox[1]
    draw.text((x, y), text, fill=(20, 20, 80), font=font)

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
            return res.data[0]
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


def add_to_pot_sync(chat_id, amount):
    r = _rpc("add_to_pot", {"p_chat_id": chat_id, "p_amount": amount})
    return float(r.data) if r and r.data is not None else None


def payout_pot_sync(chat_id):
    r = _rpc("payout_pot", {"p_chat_id": chat_id})
    return float(r.data) if r and r.data is not None else 0.0


def get_pot_sync(chat_id):
    try:
        res = supabase.table("quiz_pot").select("amount").eq("chat_id", chat_id).execute()
        return float(res.data[0]["amount"]) if res.data else 0.0
    except Exception:
        return 0.0


def load_bans_sync():
    try:
        res = supabase.table("quiz_bans").select("chat_id,user_id").execute()
        cache = {}
        for row in res.data or []:
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


def ban_user_sync(chat_id, user_id, reason, admin_id):
    try:
        supabase.table("quiz_bans").upsert({
            "chat_id": chat_id, "user_id": user_id,
            "reason": reason, "banned_by": admin_id,
        }).execute()
    except Exception as e:
        log.warning("ban: %s", e)


def unban_user_sync(chat_id, user_id):
    try:
        supabase.table("quiz_bans").delete().eq("chat_id", chat_id).eq("user_id", user_id).execute()
    except Exception as e:
        log.warning("unban: %s", e)


def save_active_sync(chat_id, question, answers, is_multi):
    try:
        supabase.table("quiz_active").upsert({
            "chat_id": chat_id,
            "question": question,
            "answer": "||".join(answers),
            "is_multi": is_multi,
        }).execute()
    except Exception as e:
        log.warning("save_active: %s", e)


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


# ==================== SUPABASE (async) ====================


async def get_player(cid, uid, un=None, fn=None):
    return await asyncio.to_thread(get_player_sync, cid, uid, un, fn)


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


async def add_to_pot(cid, amt):
    return await asyncio.to_thread(add_to_pot_sync, cid, amt)


async def payout_pot(cid):
    return await asyncio.to_thread(payout_pot_sync, cid)


async def get_pot(cid):
    return await asyncio.to_thread(get_pot_sync, cid)


def is_banned_cached(cid, uid):
    return uid in BANNED_CACHE.get(cid, set())


def is_subscriber_cached(cid, uid):
    return uid in SUBSCRIBERS_CACHE.get(cid, set())


async def ban_user(cid, uid, reason, admin_id):
    await asyncio.to_thread(ban_user_sync, cid, uid, reason, admin_id)
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
        "target": str(uid),
        "targetType": "telegram_user_id",
        "asset": "USDT",
        "amount": f"{amount:.4f}",
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


# ==================== ВОПРОСЫ ====================


async def ask_question(chat_id):
    q, answers, is_multi, is_image = random_question()
    ACTIVE_QUESTIONS[chat_id] = {"question": q, "answers": answers, "is_multi": is_multi}
    await save_active(chat_id, q, answers, is_multi)

    try:
        await bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass

    pot = await get_pot(chat_id)
    pot_line = f"🎰 Копилка чата: <b>${pot:.3f}</b>\n" if pot >= 0.01 else ""

    try:
        if is_image:
            png = render_question_image(q)
            buf = BufferedInputFile(png, filename="q.png")
            caption = (
                f"🧠 <b>Вопрос!</b>\n\n"
                f"💰 Награда: $0.050 — $0.095\n"
                f"💎 Подписчики ×2\n"
                f"{pot_line}"
                f"🔓 Вопрос открыт, пока кто-то не ответит верно."
            )
            msg = await safe_send(bot.send_photo, chat_id, buf, caption=caption)
        else:
            msg = await safe_send(
                bot.send_message, chat_id,
                f"🧠 <b>Вопрос!</b>\n\n"
                f"❓ {q}\n\n"
                f"💰 Награда: $0.050 — $0.095\n"
                f"💎 Подписчики ×2\n"
                f"{pot_line}"
                f"🔓 Вопрос открыт, пока кто-то не ответит верно."
            )
        if msg:
            try:
                await bot.set_message_reaction(chat_id, msg.message_id, ["🧠"])
            except Exception:
                pass
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
            log.info("Задаю вопрос в %d чатах...", len(QUIZ_ENABLED))
            await asyncio.gather(*[ask_question(c) for c in list(QUIZ_ENABLED)], return_exceptions=True)
        await asyncio.sleep(60)


async def pot_payout_loop():
    await asyncio.sleep(30)
    last_payout_date = None
    while True:
        now = datetime.now(TZ)
        today = now.date()
        if now.hour == POT_HOUR and last_payout_date != today:
            last_payout_date = today
            for cid in list(QUIZ_ENABLED):
                try:
                    amount = await payout_pot(cid)
                    if amount < 0.01:
                        continue
                    rows = await get_top(cid, 10)
                    if not rows:
                        continue
                    winner = random.choice(rows)
                    wuid = int(winner["user_id"])
                    wname = winner.get("first_name") or winner.get("username") or str(wuid)
                    await add_balance(cid, wuid, amount)
                    await safe_send(
                        bot.send_message, cid,
                        f"🎰 <b>Розыгрыш копилки!</b>\n\n"
                        f"💰 Выигрыш: <b>${amount:.4f}</b>\n"
                        f"🏆 Победитель: <b>{wname}</b>\n"
                        f"<i>Случайный выбор из топ-10 активных.</i>"
                    )
                    log.info("Pot payout: chat=%s user=%s amount=%s", cid, wuid, amount)
                except Exception as e:
                    log.warning("pot payout: %s", e)
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


# ==================== ДУЭЛИ НА КУБАХ ====================


@dp.message(Command("AiDuel"))
async def cmd_duel(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    cid = message.chat.id
    uid = message.from_user.id

    if is_banned_cached(cid, uid):
        await safe_send(message.reply, "🚫 Ты в бане.")
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await safe_send(
            message.reply,
            f"🎲 <b>Дуэль на кубах</b>\n\n"
            f"Формат: <code>/AiDuel 0.20</code> — ответом на сообщение противника\n\n"
            f"Оба кидают кубик. У кого больше — забирает банк.\n"
            f"Ставка: от ${DUEL_MIN:.2f} до ${DUEL_MAX:.2f}\n"
            f"Ничья — возврат ставок."
        )
        return

    try:
        amount = float(parts[1])
    except ValueError:
        await safe_send(message.reply, "Ставка — число, например <code>0.20</code>")
        return

    amount = round(amount, 4)
    if amount < DUEL_MIN or amount > DUEL_MAX:
        await safe_send(message.reply, f"Ставка: от ${DUEL_MIN:.2f} до ${DUEL_MAX:.2f}")
        return

    # ищем противника
    opponent_id = None
    opponent_name = None
    if message.reply_to_message and message.reply_to_message.from_user:
        opp = message.reply_to_message.from_user
        opponent_id = opp.id
        opponent_name = opp.first_name
    elif message.entities:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                opponent_id = ent.user.id
                opponent_name = ent.user.first_name
                break

    if not opponent_id:
        await safe_send(message.reply, "Ответь на сообщение противника или упомяни его.")
        return
    if opponent_id == uid:
        await safe_send(message.reply, "Себе нельзя 😄")
        return
    if opponent_id == bot.id:
        await safe_send(message.reply, "С ботом нельзя 😄")
        return

    # проверяем балансы
    p_c = await get_player(cid, uid, message.from_user.username, message.from_user.first_name)
    p_o = await get_player(cid, opponent_id)
    bal_c = float(p_c["balance"])
    bal_o = float(p_o["balance"])

    if bal_c < amount:
        await safe_send(message.reply, f"❌ У тебя ${bal_c:.4f}, нужно ${amount:.2f}")
        return
    if bal_o < amount:
        await safe_send(message.reply, f"❌ У противника ${bal_o:.4f}, нужно ${amount:.2f}")
        return
    if uid in DUEL_BUSY or opponent_id in DUEL_BUSY:
        await safe_send(message.reply, "⏳ Один из вас уже в дуэли.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"duel:a:{uid}:{opponent_id}:{amount}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"duel:r:{uid}:{opponent_id}:{amount}"),
    ]])
    text = (
        f"🎲 <b>Дуэль на кубах!</b>\n\n"
        f"<b>{message.from_user.first_name}</b> вызывает <b>{opponent_name}</b>\n\n"
        f"💵 Ставка с каждого: <b>${amount:.4f}</b>\n"
        f"💰 Банк: <b>${amount*2:.4f}</b>\n\n"
        f"У кого кубик больше — забирает всё.\n"
        f"<i>У {opponent_name} {DUEL_TTL // 60} мин, чтобы принять.</i>"
    )
    sent = await safe_send(message.reply, text, reply_markup=kb)
    if not sent:
        return

    async def auto_close():
        await asyncio.sleep(DUEL_TTL)
        try:
            await bot.edit_message_reply_markup(cid, sent.message_id, reply_markup=None)
            await bot.edit_message_text(
                chat_id=cid, message_id=sent.message_id,
                text=f"⌛ <b>Дуэль истекла.</b>\n{opponent_name} не ответил.",
            )
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
    _, action, ch_str, op_str, amt_str = parts
    try:
        challenger_id = int(ch_str)
        opponent_id = int(op_str)
        amount = float(amt_str)
    except ValueError:
        await cb.answer("Ошибка", show_alert=True)
        return

    cid = cb.message.chat.id

    if cb.from_user.id != opponent_id:
        await cb.answer("Это не твой вызов.", show_alert=True)
        return

    if action == "r":
        try:
            await cb.message.edit_text(
                f"❌ <b>Дуэль отклонена.</b>\n{cb.from_user.first_name} отказался."
            )
        except Exception:
            pass
        await cb.answer("Отклонено")
        return

    # accept
    await cb.answer("Поехали!")
    if challenger_id in DUEL_BUSY or opponent_id in DUEL_BUSY:
        try:
            await cb.message.edit_text("⏳ Один из вас уже в дуэли.")
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
                await cb.message.edit_text("❌ У кого-то не хватает баланса.")
            except Exception:
                pass
            return

        # списываем ставки (эскроу)
        if not await deduct_balance(cid, challenger_id, amount):
            try:
                await cb.message.edit_text("❌ Не удалось списать у вызывающего.")
            except Exception:
                pass
            return
        if not await deduct_balance(cid, opponent_id, amount):
            await add_balance(cid, challenger_id, amount)
            try:
                await cb.message.edit_text("❌ Не удалось списать у соперника. Ставка возвращена.")
            except Exception:
                pass
            return

        name_c = p_c.get("first_name") or str(challenger_id)
        name_o = p_o.get("first_name") or str(opponent_id)

        try:
            await cb.message.edit_text(
                f"🎲 <b>Дуэль началась!</b>\n\n"
                f"💰 Банк: <b>${amount*2:.4f}</b>\n"
                f"🎯 {name_c} vs {name_o}\n\n"
                f"Бросаю кубики..."
            )
        except Exception:
            pass

        await asyncio.sleep(1)
        m1 = await safe_send(bot.send_dice, cid, emoji="🎲")
        r1 = m1.dice.value if m1 and m1.dice else 0
        await asyncio.sleep(2)
        m2 = await safe_send(bot.send_dice, cid, emoji="🎲")
        r2 = m2.dice.value if m2 and m2.dice else 0
        await asyncio.sleep(2)

        if r1 > r2:
            winner_id, winner_name = challenger_id, name_c
        elif r2 > r1:
            winner_id, winner_name = opponent_id, name_o
        else:
            # ничья — возврат
            await add_balance(cid, challenger_id, amount)
            await add_balance(cid, opponent_id, amount)
            await safe_send(
                bot.send_message, cid,
                f"🤝 <b>Ничья! {r1} : {r2}</b>\n"
                f"Ставки возвращены по ${amount:.4f}."
            )
            return

        pot = round(amount * 2, 4)
        await add_balance(cid, winner_id, pot)
        await safe_send(
            bot.send_message, cid,
            f"🏆 <b>{winner_name} победил!</b>\n\n"
            f"🎲 {name_c}: <b>{r1}</b>\n"
            f"🎲 {name_o}: <b>{r2}</b>\n\n"
            f"💰 Забирает банк: <b>${pot:.4f}</b>"
        )
    finally:
        DUEL_BUSY.discard(challenger_id)
        DUEL_BUSY.discard(opponent_id)


# ==================== АДМИН-ПАНЕЛЬ ====================


def admin_kb(cid):
    enabled = cid in QUIZ_ENABLED
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⏸ Выключить" if enabled else "▶️ Включить",
            callback_data="adm:toggle"
        )],
        [
            InlineKeyboardButton(text="❓ Вопрос", callback_data="adm:ask"),
            InlineKeyboardButton(text="📊 Стата", callback_data="adm:stats"),
        ],
        [
            InlineKeyboardButton(text="💸 Выплаты", callback_data="adm:payouts"),
            InlineKeyboardButton(text="📋 Топ", callback_data="adm:top"),
        ],
        [
            InlineKeyboardButton(text="🎰 Копилка", callback_data="adm:pot"),
            InlineKeyboardButton(text="🚫 Баны", callback_data="adm:bans"),
        ],
        [
            InlineKeyboardButton(text="🧪 xRocket", callback_data="adm:xrdbg"),
        ],
    ])


def admin_text(cid):
    status = "🟢 включена" if cid in QUIZ_ENABLED else "🔴 выключена"
    current = ACTIVE_QUESTIONS.get(cid)
    cur_txt = f"\n🔓 Открыт: {current['question']}" if current else ""
    return (
        f"🛠 <b>Админ-панель</b>\n"
        f"Викторина: {status}\n"
        f"Расписание: с 8:00 до 23:00 ({TZ.key})\n"
        f"Награда: ${BASE_REWARD:.3f} — ${BASE_REWARD + 9*REWARD_STEP:.3f}\n"
        f"Подписка ×{SUBSCRIBER_MULTIPLIER}\n"
        f"Копилка: {int(POT_PERCENT*100)}% · раздача в {POT_HOUR}:00 МСК\n"
        f"Вопросов: {len(QUESTIONS)} + {len(MULTI_QUESTIONS)} мульти"
        f"{cur_txt}"
    )


@dp.message(Command("AiAdmin"))
async def cmd_aiadmin(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    if message.chat.type not in ("group", "supergroup"):
        return
    await safe_send(message.reply, admin_text(message.chat.id), reply_markup=admin_kb(message.chat.id))


@dp.message(Command("AiBan"))
async def cmd_aiban(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await safe_send(message.reply, "Формат: <code>/AiBan &lt;user_id&gt; [причина]</code>")
        return
    try:
        target = int(parts[1])
    except ValueError:
        await safe_send(message.reply, "user_id — целое число.")
        return
    reason = parts[2] if len(parts) > 2 else "без причины"
    await ban_user(message.chat.id, target, reason, message.from_user.id)
    await safe_send(message.reply, f"🚫 <code>{target}</code> забанен.\nПричина: {reason}")


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
        await safe_send(message.reply, "user_id — целое число.")
        return
    await unban_user(message.chat.id, target)
    await safe_send(message.reply, f"✅ <code>{target}</code> разбанен.")


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

    if action == "stats":
        await cb.answer("Собираю...")
        players, payouts, bans, subs = await get_stats()
        tb = sum(float(p["balance"]) for p in players)
        tw = sum(float(p["total_won"]) for p in players)
        tc = sum(int(p["correct_answers"]) for p in players)
        fin = [p for p in payouts if p["status"] == "finished"]
        fail = [p for p in payouts if p["status"] == "failed"]
        ps = sum(float(p["amount"]) for p in fin)
        await safe_send(
            cb.message.answer,
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Игроков: {len(players)}\n"
            f"🏆 Правильных: {tc}\n"
            f"💎 Подписчиков: {len(subs)}\n"
            f"🚫 Забанено: {len(bans)}\n\n"
            f"💰 Балансов: ${tb:.4f}\n"
            f"📈 Заработано: ${tw:.4f}\n"
            f"💸 Выплат: {len(fin)} (${ps:.4f})\n"
            f"❌ Ошибок выплат: {len(fail)}"
        )
        return

    if action == "pot":
        await cb.answer()
        pot = await get_pot(cid)
        await safe_send(
            cb.message.answer,
            f"🎰 <b>Копилка чата</b>\n\n"
            f"Сейчас в фонде: <b>${pot:.4f}</b>\n"
            f"Раздача: каждый день в <b>{POT_HOUR}:00 МСК</b> "
            f"случайному из топ-10 активных.\n"
            f"Пополнение: {int(POT_PERCENT*100)}% от каждой награды."
        )
        return

    if action == "payouts":
        await cb.answer("Собираю...")
        rows = await get_payouts(20)
        if not rows:
            await safe_send(cb.message.answer, "Выплат ещё не было.")
            return
        lines = ["💸 <b>Последние выплаты</b>"]
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
        lines = ["🏆 <b>Топ чата</b>"]
        for i, row in enumerate(rows, 1):
            ca = int(row.get("correct_answers", 0))
            _, emoji, _, _, _, _ = level_info(ca)
            nm = row.get("first_name") or row.get("username") or str(row["user_id"])
            medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
            lines.append(f"{medal} {emoji} {nm} — {ca} отв. · ${float(row['balance']):.4f} · <code>{row['user_id']}</code>")
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
            lines.append(f"<code>{b['user_id']}</code> — {b.get('reason','—')}")
        await safe_send(cb.message.answer, "\n".join(lines))
        return

    if action == "xrdbg":
        await cb.answer("Проверяю...")
        msg = await safe_send(cb.message.answer, "⏳ Проверяю xRocket...")
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


@dp.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Подписка $0.50/нед", url=XROCKET_SUBSCRIBE_URL)],
        [InlineKeyboardButton(text="🔗 Партнёрка xRocket", url=XROCKET_REFERRAL_URL)],
    ])
    await safe_send(
        message.reply,
        f"👋 <b>Викторина с уровнями!</b>\n\n"
        f"🎯 10 уровней, +${REWARD_STEP:.3f} к награде каждый\n"
        f"💰 Ур.1: ${BASE_REWARD:.3f} · Ур.10: ${BASE_REWARD + 9*REWARD_STEP:.3f}\n"
        f"💎 Подписчики ×{SUBSCRIBER_MULTIPLIER}\n"
        f"🎰 Копилка чата: розыгрыш раз в день\n"
        f"🎲 Дуэли на кубах: /AiDuel 0.20\n"
        f"⏰ Вопрос каждый час с 8:00 до 23:00\n"
        f"💸 Вывод от ${MIN_WITHDRAW:.2f}\n\n"
        f"<b>Команды:</b>\n"
        f"/AiBalance · /AiProfile · /AiTop · /AiLevels · /AiWithdraw",
        reply_markup=kb,
    )


@dp.message(Command("AiSubscribe"))
async def cmd_aisub(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Оформить $0.50/нед", url=XROCKET_SUBSCRIBE_URL)],
    ])
    await safe_send(
        message.reply,
        f"💎 <b>Подписка ×{SUBSCRIBER_MULTIPLIER}</b>\n\n"
        f"Цена: <b>${SUBSCRIPTION_PRICE:.2f}/нед</b>\n"
        f"• ×{SUBSCRIBER_MULTIPLIER} к награде за ответ\n"
        f"• Бейдж 💎 в профиле\n\n"
        f"Оформить через @xrocket:",
        reply_markup=kb,
    )


@dp.message(Command("AiBalance"))
async def cmd_aibalance(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    p = await get_player(message.chat.id, message.from_user.id,
                         message.from_user.username, message.from_user.first_name)
    today = await withdrawn_today(message.chat.id, message.from_user.id)
    ca = int(p["correct_answers"])
    _, _, _, reward, title_str, _ = level_info(ca)
    bar = make_progress_bar(ca)
    sub = is_subscriber_cached(message.chat.id, message.from_user.id)
    sub_line = f"\n💎 Подписка · ×{SUBSCRIBER_MULTIPLIER}" if sub else ""
    pot = await get_pot(message.chat.id)
    pot_line = f"\n🎰 Копилка чата: ${pot:.3f}" if pot >= 0.01 else ""

    await safe_send(
        message.reply,
        f"💰 <b>${float(p['balance']):.4f}</b>\n"
        f"🎖 {title_str}\n"
        f"💵 Награда за ответ: <b>${reward:.3f}</b>{sub_line}\n"
        f"🏆 Правильных: {ca}\n"
        f"<code>{bar}</code>\n"
        f"💸 Выведено сегодня: ${today:.4f} / ${DAILY_WITHDRAW_LIMIT:.2f}"
        f"{pot_line}"
    )


@dp.message(Command("AiProfile"))
async def cmd_aiprofile(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    p = await get_player(message.chat.id, message.from_user.id,
                         message.from_user.username, message.from_user.first_name)
    ca = int(p["correct_answers"])
    lvl, _, _, reward, title_str, _ = level_info(ca)
    bar = make_progress_bar(ca)
    sub = is_subscriber_cached(message.chat.id, message.from_user.id)

    def _place():
        try:
            res = supabase.table("quiz_players").select("user_id").eq(
                "chat_id", message.chat.id
            ).order("correct_answers", desc=True).execute()
            for i, row in enumerate(res.data or [], 1):
                if int(row["user_id"]) == message.from_user.id:
                    return i
            return None
        except Exception:
            return None
    place = await asyncio.to_thread(_place)
    place_str = f"#{place}" if place else "—"

    today = await withdrawn_today(message.chat.id, message.from_user.id)

    if lvl >= MAX_LEVEL:
        next_line = "🏆 Максимальный уровень!"
    else:
        in_level = ca - (lvl - 1) * ANSWERS_PER_LEVEL
        remaining = ANSWERS_PER_LEVEL - in_level
        n_emoji = LEVELS[lvl][1]
        n_name = LEVELS[lvl][2]
        n_reward = BASE_REWARD + lvl * REWARD_STEP
        next_line = f"⬆️ До {n_emoji} <b>{n_name}</b>: <b>{remaining}</b> отв. → ${n_reward:.3f}"

    sub_line = f"\n💎 Подписка: <b>активна</b> (×{SUBSCRIBER_MULTIPLIER})" if sub else "\n💎 Подписка: нет"

    await safe_send(
        message.reply,
        f"👤 <b>{message.from_user.first_name}</b>\n\n"
        f"🎖 <b>{title_str}</b>\n"
        f"💵 Награда: <b>${reward:.3f}</b>{sub_line}\n\n"
        f"<code>{bar}</code>\n"
        f"{next_line}\n\n"
        f"💰 Баланс: <b>${float(p['balance']):.4f}</b>\n"
        f"📈 Всего: ${float(p['total_won']):.4f}\n"
        f"💸 Выведено: ${today:.4f}\n\n"
        f"🏆 Правильных: <b>{ca}</b>\n"
        f"📍 Место: <b>{place_str}</b>"
    )


@dp.message(Command("AiLevels"))
async def cmd_ailevels(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    lines = ["🎖 <b>Уровни и награды</b>\n"]
    for lvl, emoji, name in LEVELS:
        reward = BASE_REWARD + (lvl - 1) * REWARD_STEP
        mn = (lvl - 1) * ANSWERS_PER_LEVEL
        req = f"{mn}+" if lvl == MAX_LEVEL else f"{mn}-{mn + ANSWERS_PER_LEVEL - 1}"
        lines.append(f"{emoji} <b>Ур. {lvl}</b> · {name} · <b>${reward:.3f}</b> · <i>{req}</i>")
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
    lines = ["🏆 <b>Топ по ответам</b>"]
    for i, row in enumerate(rows, 1):
        ca = int(row.get("correct_answers", 0))
        _, emoji, _, _, _, _ = level_info(ca)
        nm = row.get("first_name") or row.get("username") or str(row["user_id"])
        medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
        uid = f" · <code>{row['user_id']}</code>" if admin_view else ""
        lines.append(f"{medal} {emoji} {nm} — {ca} отв. · ${float(row['balance']):.4f}{uid}")
    await safe_send(message.reply, "\n".join(lines))


# ==================== ВЫВОД ====================


@dp.message(Command("AiWithdraw"))
async def cmd_aiwithdraw(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    cid = message.chat.id
    uid = message.from_user.id

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
        await safe_send(message.reply, f"❌ Дневной лимит ${DAILY_WITHDRAW_LIMIT:.2f} исчерпан.")
        return
    amount = min(bal, rem)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"wd:accept:{uid}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd:reject:{uid}"),
    ]])
    text = (
        f"💸 <b>Подтверждение вывода</b>\n\n"
        f"Сумма: <b>${amount:.4f}</b> USDT\n"
        f"Куда: Telegram ID <code>{uid}</code>\n\n"
        f"⚠️ <b>Чтобы вывод прошёл, зайди в "
        f"<a href=\"{XROCKET_REFERRAL_URL}\">@xrocket</a></b> и активируй аккаунт.\n\n"
        f"Запрос действует {WITHDRAW_CONFIRM_TTL // 60} мин."
    )
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
            await bot.edit_message_text(
                chat_id=cid, message_id=sent.message_id,
                text="⌛ <b>Запрос истёк.</b> Создай новый /AiWithdraw.",
            )
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
    action, owner_str = parts[1], parts[2]
    try:
        owner_id = int(owner_str)
    except ValueError:
        await cb.answer("Ошибка", show_alert=True)
        return
    if cb.from_user.id != owner_id:
        await cb.answer("⛔ Не твой запрос.", show_alert=True)
        return

    info = PENDING_WITHDRAWS.pop(cb.message.message_id, None)
    if not info:
        await cb.answer("⌛ Уже истёк или обработан.", show_alert=True)
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
            await cb.message.edit_text("⏳ Другой вывод уже обрабатывается.")
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
            text = f"✅ <b>Выплачено ${amount:.4f}</b>\nID: <code>{result}</code>"
        else:
            await log_payout(cid, uid, amount, "", "failed")
            text = f"❌ <b>Ошибка</b>\n<code>{result}</code>\n\nЗайди в @xrocket."
        try:
            await cb.message.edit_text(text)
        except Exception:
            await safe_send(bot.send_message, cid, text)
    finally:
        await unlock_withdraw(cid, uid)


# ==================== ОТВЕТЫ ====================


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_answer(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    cid = message.chat.id
    uid = message.from_user.id

    q = ACTIVE_QUESTIONS.get(cid)
    if not q:
        return

    text = (message.text or "").strip().lower()
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

    p = await get_player(cid, uid, message.from_user.username, message.from_user.first_name)
    old_lvl = level_from_correct(int(p["correct_answers"]))
    sub = is_subscriber_cached(cid, uid)
    reward = reward_for(int(p["correct_answers"]), is_sub=sub)
    pot_add = round(reward * POT_PERCENT, 4)

    await asyncio.gather(
        clear_active(cid),
        add_balance(cid, uid, reward, count_correct=True),
        add_to_pot(cid, pot_add),
        return_exceptions=True,
    )

    new_correct = int(p["correct_answers"]) + 1
    new_lvl = level_from_correct(new_correct)

    phrase = random.choice(CORRECT_PHRASES)
    sub_badge = " 💎×2" if sub else ""
    if q["is_multi"]:
        answer_shown = "любой из: " + ", ".join(q["answers"][:5]) + ("..." if len(q["answers"]) > 5 else "")
    else:
        answer_shown = q["answers"][0]

    msg = (
        f"{phrase}{sub_badge}\n"
        f"{message.from_user.first_name} получает <b>${reward:.3f}</b>\n"
        f"<i>Ответ: {answer_shown}</i>"
    )

    if new_lvl > old_lvl:
        n_emoji = LEVELS[new_lvl - 1][1]
        n_name = LEVELS[new_lvl - 1][2]
        n_reward = reward_for(new_correct, is_sub=sub)
        msg += f"\n\n{n_emoji} <b>НОВЫЙ УРОВЕНЬ {new_lvl}!</b>\n🎖 {n_name} · теперь <b>${n_reward:.3f}</b>"

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
    print("Quiz Bot · уровни · картинки · копилка · дуэли")
    print(f"Вопросов: {len(QUESTIONS)} + {len(MULTI_QUESTIONS)} мульти")
    print(f"Дуэли: ставка ${DUEL_MIN} — ${DUEL_MAX}")
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
        }
        QUIZ_ENABLED.add(int(row["chat_id"]))

    me = await bot.get_me()
    print(f"Подключился как @{me.username}")
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
