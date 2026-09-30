import asyncio
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
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from supabase import create_client, Client

from questions import QUESTIONS, random_question

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

# ========== ССЫЛКИ XROCKET (вставь свои!) ==========
# Ссылка на подписку (создаётся в @xRocket → Subscriptions)
XROCKET_SUBSCRIBE_URL = os.getenv("XROCKET_SUBSCRIBE_URL", "https://t.me/xRocket?start=sub_YOUR_ID")
# Твоя реферальная ссылка (из @xRocket → Referral)
XROCKET_REFERRAL_URL = os.getenv("XROCKET_REFERRAL_URL", "https://t.me/xRocket?start=ref_YOUR_ID")

# Награда и уровни
BASE_REWARD = 0.05
REWARD_STEP = 0.005
ANSWERS_PER_LEVEL = 10
MAX_LEVEL = 10

# Множитель для подписчиков
SUBSCRIBER_MULTIPLIER = 2.0
SUBSCRIPTION_PRICE = 0.50
SUBSCRIPTION_DAYS = 7

MIN_WITHDRAW = 0.05
DAILY_WITHDRAW_LIMIT = 1.00

ADMIN_IDS = {8130244626, 6173495222}

TZ = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))
WORK_HOURS = list(range(8, 24))
WITHDRAW_CONFIRM_TTL = 120

# ==================== ФРАЗЫ (пункт 41) ====================

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

# ==================== УРОВНИ ====================

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


def level_from_correct(correct_answers: int) -> int:
    lvl = correct_answers // ANSWERS_PER_LEVEL + 1
    return min(lvl, MAX_LEVEL)


def level_info(correct_answers: int):
    lvl = level_from_correct(correct_answers)
    emoji, name = LEVELS[lvl - 1][1], LEVELS[lvl - 1][2]
    reward = BASE_REWARD + (lvl - 1) * REWARD_STEP
    title_str = f"{emoji} {name} (ур. {lvl})"

    if lvl >= MAX_LEVEL:
        progress = "🏆 Максимальный уровень!"
    else:
        in_level = correct_answers - (lvl - 1) * ANSWERS_PER_LEVEL
        remaining = ANSWERS_PER_LEVEL - in_level
        progress = f"до след. уровня: {remaining} отв."

    return lvl, emoji, name, reward, title_str, progress


def reward_for(correct_answers: int, is_subscriber: bool = False) -> float:
    lvl = level_from_correct(correct_answers)
    base = BASE_REWARD + (lvl - 1) * REWARD_STEP
    if is_subscriber:
        base *= SUBSCRIBER_MULTIPLIER
    return round(base, 4)


def make_progress_bar(correct_answers: int) -> str:
    lvl = level_from_correct(correct_answers)
    if lvl >= MAX_LEVEL:
        return "▓" * 10 + " 10/10"
    in_level = correct_answers - (lvl - 1) * ANSWERS_PER_LEVEL
    filled = in_level
    bar = "▓" * filled + "▒" * (ANSWERS_PER_LEVEL - filled)
    return f"{bar} {in_level}/{ANSWERS_PER_LEVEL}"


if not TOKEN:
    print("!!! BOT_TOKEN не задан")
    raise SystemExit(1)
if not SUPABASE_URL or not SUPABASE_KEY:
    print("!!! SUPABASE_URL / SUPABASE_KEY не заданы")
    raise SystemExit(1)

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

ACTIVE_QUESTIONS = {}
QUIZ_ENABLED = set()
PENDING_WITHDRAWS = {}
BANNED_CACHE = {}
SUBSCRIBERS_CACHE = {}  # chat_id -> set(user_id)
HTTP_SESSION: aiohttp.ClientSession | None = None


def is_admin(user_id) -> bool:
    return user_id in ADMIN_IDS


# ==================== HTTP ====================


async def get_http() -> aiohttp.ClientSession:
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


# ==================== SAFE SEND ====================


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
        log.warning("get_player select: %s", e)
    try:
        supabase.table("quiz_players").insert({
            "chat_id": chat_id, "user_id": user_id,
            "username": username, "first_name": first_name,
        }).execute()
    except Exception as e:
        log.warning("get_player insert: %s", e)
    return {"chat_id": chat_id, "user_id": user_id, "username": username,
            "first_name": first_name, "balance": 0, "total_won": 0,
            "correct_answers": 0, "is_withdrawing": False}


def add_balance_sync(chat_id, user_id, amount, count_correct=False):
    r = _rpc("add_balance_atomic", {
        "p_chat_id": chat_id, "p_user_id": user_id,
        "p_amount": amount, "p_count_correct": count_correct,
    })
    if r and r.data is not None:
        return float(r.data)
    return None


def deduct_balance_sync(chat_id, user_id, amount):
    r = _rpc("deduct_balance_atomic", {
        "p_chat_id": chat_id, "p_user_id": user_id, "p_amount": amount,
    })
    return bool(r and r.data is True)


def try_lock_withdraw_sync(chat_id, user_id):
    r = _rpc("try_lock_withdraw", {"p_chat_id": chat_id, "p_user_id": user_id})
    return bool(r and r.data is True)


def unlock_withdraw_sync(chat_id, user_id):
    _rpc("unlock_withdraw", {"p_chat_id": chat_id, "p_user_id": user_id})


def withdrawn_today_sync(chat_id, user_id):
    r = _rpc("withdrawn_today", {"p_chat_id": chat_id, "p_user_id": user_id})
    if r and r.data is not None:
        return float(r.data)
    return 0.0


def load_bans_sync():
    try:
        res = supabase.table("quiz_bans").select("chat_id,user_id").execute()
        cache = {}
        for row in res.data or []:
            cache.setdefault(int(row["chat_id"]), set()).add(int(row["user_id"]))
        return cache
    except Exception as e:
        log.warning("load_bans: %s", e)
        return {}


def load_subscribers_sync():
    """Загружает активных подписчиков (у кого expires_at > now)."""
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        res = supabase.table("quiz_subscribers").select("chat_id,user_id").gt("expires_at", now_iso).execute()
        cache = {}
        for row in res.data or []:
            cache.setdefault(int(row["chat_id"]), set()).add(int(row["user_id"]))
        return cache
    except Exception as e:
        log.warning("load_subscribers: %s", e)
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


def save_active_sync(chat_id, question, answer):
    try:
        supabase.table("quiz_active").upsert({
            "chat_id": chat_id, "question": question, "answer": answer,
        }).execute()
    except Exception as e:
        log.warning("save_active: %s", e)


def clear_active_sync(chat_id):
    try:
        supabase.table("quiz_active").delete().eq("chat_id", chat_id).execute()
    except Exception as e:
        log.warning("clear_active: %s", e)


def load_active_sync():
    try:
        res = supabase.table("quiz_active").select("chat_id,question,answer").execute()
        return res.data or []
    except Exception as e:
        log.warning("load_active: %s", e)
        return []


def unlock_all_withdrawals_sync():
    try:
        supabase.table("quiz_players").update({"is_withdrawing": False}).eq("is_withdrawing", True).execute()
    except Exception as e:
        log.warning("unlock_all: %s", e)


def get_top_sync(chat_id, limit=10):
    try:
        res = supabase.table("quiz_players").select(
            "user_id,username,first_name,balance,correct_answers"
        ).eq("chat_id", chat_id).order("correct_answers", desc=True).limit(limit).execute()
        return res.data or []
    except Exception as e:
        log.warning("get_top: %s", e)
        return []


def get_stats_sync():
    try:
        players = supabase.table("quiz_players").select("balance,total_won,correct_answers").execute().data or []
        payouts = supabase.table("quiz_payouts").select("amount,status").execute().data or []
        bans = supabase.table("quiz_bans").select("user_id").execute().data or []
        subs = supabase.table("quiz_subscribers").select("user_id").execute().data or []
        return players, payouts, bans, subs
    except Exception as e:
        log.warning("get_stats: %s", e)
        return [], [], [], []


def get_payouts_sync(limit=20):
    try:
        res = supabase.table("quiz_payouts").select("*").order("created_at", desc=True).limit(limit).execute()
        return res.data or []
    except Exception as e:
        log.warning("get_payouts: %s", e)
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


async def get_player(chat_id, user_id, username=None, first_name=None):
    return await asyncio.to_thread(get_player_sync, chat_id, user_id, username, first_name)


async def add_balance(chat_id, user_id, amount, count_correct=False):
    return await asyncio.to_thread(add_balance_sync, chat_id, user_id, amount, count_correct)


async def deduct_balance(chat_id, user_id, amount):
    return await asyncio.to_thread(deduct_balance_sync, chat_id, user_id, amount)


async def try_lock_withdraw(chat_id, user_id):
    return await asyncio.to_thread(try_lock_withdraw_sync, chat_id, user_id)


async def unlock_withdraw(chat_id, user_id):
    await asyncio.to_thread(unlock_withdraw_sync, chat_id, user_id)


async def withdrawn_today(chat_id, user_id):
    return await asyncio.to_thread(withdrawn_today_sync, chat_id, user_id)


def is_banned_cached(chat_id, user_id) -> bool:
    return user_id in BANNED_CACHE.get(chat_id, set())


def is_subscriber_cached(chat_id, user_id) -> bool:
    return user_id in SUBSCRIBERS_CACHE.get(chat_id, set())


async def ban_user(chat_id, user_id, reason, admin_id):
    await asyncio.to_thread(ban_user_sync, chat_id, user_id, reason, admin_id)
    BANNED_CACHE.setdefault(chat_id, set()).add(user_id)


async def unban_user(chat_id, user_id):
    await asyncio.to_thread(unban_user_sync, chat_id, user_id)
    BANNED_CACHE.get(chat_id, set()).discard(user_id)


async def save_active(chat_id, question, answer):
    await asyncio.to_thread(save_active_sync, chat_id, question, answer)


async def clear_active(chat_id):
    await asyncio.to_thread(clear_active_sync, chat_id)


async def get_top(chat_id, limit=10):
    return await asyncio.to_thread(get_top_sync, chat_id, limit)


async def get_stats():
    return await asyncio.to_thread(get_stats_sync)


async def get_payouts(limit=20):
    return await asyncio.to_thread(get_payouts_sync, limit)


async def log_payout(chat_id, user_id, amount, payout_id, status):
    await asyncio.to_thread(log_payout_sync, chat_id, user_id, amount, payout_id, status)


# ==================== XROCKET ====================


async def xrocket_payout(chat_id, user_id, amount):
    if not XROCKET_API_KEY:
        return False, "XROCKET_API_KEY не задан"

    payload = {
        "clientPayoutId": f"quiz_{chat_id}_{user_id}_{int(datetime.now().timestamp()*1000)}",
        "target": str(user_id),
        "targetType": "telegram_user_id",
        "asset": "USDT",
        "amount": f"{amount:.4f}",
        "description": "Quiz reward",
    }
    headers = {
        "Authorization": f"Bearer {XROCKET_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{XROCKET_BASE}/api/v1/payouts"
    try:
        s = await get_http()
        async with s.post(url, headers=headers, json=payload) as r:
            text = await r.text()
            try:
                data = json.loads(text)
            except Exception:
                data = {"raw": text[:300]}
            log.info("xRocket [%s] %s", r.status, data)
            if r.status in (200, 201):
                return True, data.get("payoutId") or data.get("id") or "ok"
            err = data.get("detail") or data.get("title") or data.get("message") or str(data)
            return False, err
    except aiohttp.ClientConnectorError as e:
        return False, f"Нет соединения с xRocket: {e}"
    except asyncio.TimeoutError:
        return False, "xRocket не отвечает (таймаут)"
    except Exception as e:
        return False, str(e)


# ==================== ВОПРОСЫ ====================


def pick_question_safe():
    if not QUESTIONS:
        return ("Сколько будет 2+2?", "4")
    return random_question()


async def ask_question(chat_id):
    q, a = pick_question_safe()
    ACTIVE_QUESTIONS[chat_id] = {"question": q, "answer": a.lower()}
    await save_active(chat_id, q, a.lower())
    try:
        # Пункт 43: анимация "печатает..."
        await bot.send_chat_action(chat_id, "typing")
        msg = await safe_send(
            bot.send_message,
            chat_id,
            f"🧠 <b>Вопрос!</b>\n\n"
            f"❓ {q}\n\n"
            f"💰 Награда зависит от уровня: $0.050 — $0.095\n"
            f"💎 Подписчики получают ×2\n"
            f"🔓 Вопрос открыт, пока кто-то не ответит верно."
        )
        # Пункт 42: реакция 🧠 на вопрос
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
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=WORK_HOURS[0], minute=0, second=5, microsecond=0)


async def question_scheduler():
    await asyncio.sleep(10)
    while True:
        now = datetime.now(TZ)
        target = next_run_time(now)
        wait = (target - now).total_seconds()
        log.info("Следующий вопрос в %s (через %.0f сек)", target.strftime("%H:%M:%S %d.%m"), wait)
        await asyncio.sleep(max(1, wait))

        if not QUIZ_ENABLED:
            continue

        log.info("Задаю вопрос в %d чатах...", len(QUIZ_ENABLED))
        tasks = [ask_question(cid) for cid in list(QUIZ_ENABLED)]
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(60)


async def caches_refresh_loop():
    """Обновляем кэши банов и подписчиков раз в 60 секунд."""
    global BANNED_CACHE, SUBSCRIBERS_CACHE
    while True:
        try:
            BANNED_CACHE = await asyncio.to_thread(load_bans_sync)
            SUBSCRIBERS_CACHE = await asyncio.to_thread(load_subscribers_sync)
        except Exception as e:
            log.warning("caches refresh: %s", e)
        await asyncio.sleep(60)


# ==================== АДМИН-ПАНЕЛЬ ====================


def admin_kb(chat_id):
    enabled = chat_id in QUIZ_ENABLED
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⏸ Выключить" if enabled else "▶️ Включить",
            callback_data="adm:toggle"
        )],
        [
            InlineKeyboardButton(text="❓ Вопрос сейчас", callback_data="adm:ask"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
        ],
        [
            InlineKeyboardButton(text="💸 Выплаты", callback_data="adm:payouts"),
            InlineKeyboardButton(text="📋 Топ", callback_data="adm:top"),
        ],
        [
            InlineKeyboardButton(text="🚫 Баны", callback_data="adm:bans"),
            InlineKeyboardButton(text="🧪 xRocket", callback_data="adm:xrdbg"),
        ],
    ])


def admin_text(chat_id):
    status = "🟢 включена" if chat_id in QUIZ_ENABLED else "🔴 выключена"
    current = ACTIVE_QUESTIONS.get(chat_id)
    current_txt = f"\n🔓 Открыт: {current['question']}" if current else ""
    xr_status = "✅ задан" if XROCKET_API_KEY else "❌ не задан"
    return (
        f"🛠 <b>Админ-панель</b>\n"
        f"Викторина: {status}\n"
        f"Расписание: каждый час с 8:00 до 23:00 ({TZ.key})\n"
        f"Базовая награда: ${BASE_REWARD:.3f} · +${REWARD_STEP:.3f}/уровень\n"
        f"Макс. награда (ур. 10): ${BASE_REWARD + 9*REWARD_STEP:.3f}\n"
        f"Подписчики: ×{SUBSCRIBER_MULTIPLIER}\n"
        f"Вывод от: ${MIN_WITHDRAW:.2f} · лимит ${DAILY_WITHDRAW_LIMIT:.2f}/сутки\n"
        f"Вопросов в базе: {len(QUESTIONS)}\n"
        f"xRocket: {xr_status}"
        f"{current_txt}\n\n"
        f"<b>Админ-команды:</b>\n"
        f"/AiBan &lt;user_id&gt; [причина]\n"
        f"/AiUnban &lt;user_id&gt;"
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
    chat_id = cb.message.chat.id
    action = cb.data.split(":")[1]

    if action == "toggle":
        if chat_id in QUIZ_ENABLED:
            QUIZ_ENABLED.discard(chat_id)
            ACTIVE_QUESTIONS.pop(chat_id, None)
            await clear_active(chat_id)
            await cb.answer("Выключено")
        else:
            QUIZ_ENABLED.add(chat_id)
            await cb.answer("Включено")
        try:
            await cb.message.edit_text(admin_text(chat_id), reply_markup=admin_kb(chat_id))
        except Exception:
            pass
        return

    if action == "ask":
        QUIZ_ENABLED.add(chat_id)
        await cb.answer("Задаю...")
        asyncio.create_task(ask_question(chat_id))
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
        rows = await get_top(chat_id, 10)
        if not rows:
            await safe_send(cb.message.answer, "Никто не играл.")
            return
        lines = ["🏆 <b>Топ чата</b>"]
        for i, row in enumerate(rows, 1):
            ca = int(row.get("correct_answers", 0))
            lvl, emoji, name, reward, title_str, _ = level_info(ca)
            player_name = row.get("first_name") or row.get("username") or str(row["user_id"])
            medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
            lines.append(
                f"{medal} {emoji} {player_name} — {ca} отв. · ${float(row['balance']):.4f} · <code>{row['user_id']}</code>"
            )
        await safe_send(cb.message.answer, "\n".join(lines))
        return

    if action == "bans":
        await cb.answer("Собираю...")
        def _q():
            try:
                res = supabase.table("quiz_bans").select("*").eq("chat_id", chat_id).execute()
                return res.data or []
            except Exception:
                return []
        rows = await asyncio.to_thread(_q)
        if not rows:
            await safe_send(cb.message.answer, "Забаненных нет.")
            return
        lines = ["🚫 <b>Забаненные в чате</b>"]
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
                await safe_send(cb.message.answer, info)
        return


async def xrocket_debug():
    lines = ["🧪 <b>xRocket debug</b>", f"Key: <code>{XROCKET_API_KEY[:12]}...</code>", f"Base: <code>{XROCKET_BASE}</code>", ""]
    if not XROCKET_API_KEY:
        lines.append("❌ XROCKET_API_KEY пуст")
        return "\n".join(lines)
    try:
        s = await get_http()
        for url in (f"{XROCKET_BASE}/api/v1/me", f"{XROCKET_BASE}/api/v1/balance"):
            try:
                async with s.get(url, headers={"Authorization": f"Bearer {XROCKET_API_KEY}"}) as r:
                    text = (await r.text())[:250]
                    lines.append(f"[{r.status}] <code>{url}</code>\n<code>{text}</code>\n")
            except Exception as e:
                lines.append(f"[ERR] {url}: <code>{e}</code>")
    except Exception as e:
        lines.append(f"[ERR] http: <code>{e}</code>")
    return "\n".join(lines)


# ==================== ИГРОВЫЕ КОМАНДЫ ====================


@dp.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Оформить подписку $0.50/нед", url=XROCKET_SUBSCRIBE_URL)],
        [InlineKeyboardButton(text="🔗 Партнёрка xRocket", url=XROCKET_REFERRAL_URL)],
    ])
    await safe_send(
        message.reply,
        f"👋 <b>Викторина с уровнями!</b>\n\n"
        f"🎯 10 уровней, каждый +${REWARD_STEP:.3f} к награде\n"
        f"📈 Новый уровень за каждые {ANSWERS_PER_LEVEL} правильных ответов\n"
        f"💰 Уровень 1: ${BASE_REWARD:.3f} · Уровень 10: ${BASE_REWARD + 9*REWARD_STEP:.3f}\n"
        f"💎 Подписчики получают <b>×{SUBSCRIBER_MULTIPLIER}</b> к награде\n"
        f"⏰ Вопрос каждый час с 8:00 до 23:00\n"
        f"💸 Вывод от ${MIN_WITHDRAW:.2f}\n\n"
        f"<b>Команды:</b>\n"
        f"/AiBalance — баланс и уровень\n"
        f"/AiProfile — полный профиль\n"
        f"/AiTop — топ игроков\n"
        f"/AiLevels — все уровни\n"
        f"/AiWithdraw — вывод",
        reply_markup=kb,
    )


@dp.message(Command("AiSubscribe"))
async def cmd_aisubscribe(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Оформить подписку $0.50/нед", url=XROCKET_SUBSCRIBE_URL)],
    ])
    await safe_send(
        message.reply,
        f"💎 <b>Подписка ×{SUBSCRIBER_MULTIPLIER} к награде</b>\n\n"
        f"Цена: <b>${SUBSCRIPTION_PRICE:.2f}/нед</b>\n"
        f"Что даёт:\n"
        f"• ×{SUBSCRIBER_MULTIPLIER} к награде за правильный ответ\n"
        f"• Титул 💎 в профиле\n\n"
        f"Оформляется через @xrocket по кнопке ниже.",
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
    lvl, emoji, name, reward, title_str, progress = level_info(ca)
    bar = make_progress_bar(ca)
    sub = is_subscriber_cached(message.chat.id, message.from_user.id)
    sub_line = f"\n💎 Подписка активна · ×{SUBSCRIBER_MULTIPLIER}" if sub else ""

    await safe_send(
        message.reply,
        f"💰 <b>${float(p['balance']):.4f}</b>\n"
        f"🎖 {title_str}\n"
        f"💵 Награда за ответ: <b>${reward:.3f}</b>{sub_line}\n"
        f"🏆 Правильных: {ca}\n"
        f"<code>{bar}</code>\n"
        f"💸 Выведено сегодня: ${today:.4f} / ${DAILY_WITHDRAW_LIMIT:.2f}"
    )


@dp.message(Command("AiProfile"))
async def cmd_aiprofile(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    p = await get_player(message.chat.id, message.from_user.id,
                         message.from_user.username, message.from_user.first_name)
    ca = int(p["correct_answers"])
    lvl, emoji, name, reward, title_str, progress = level_info(ca)
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
        next_emoji = LEVELS[lvl][1]
        next_name = LEVELS[lvl][2]
        next_reward = BASE_REWARD + lvl * REWARD_STEP
        next_line = (
            f"⬆️ До {next_emoji} <b>{next_name}</b> (ур. {lvl+1}): "
            f"<b>{remaining}</b> отв. · награда будет ${next_reward:.3f}"
        )

    sub_line = f"\n💎 Подписка: <b>активна</b> (×{SUBSCRIBER_MULTIPLIER})" if sub else "\n💎 Подписка: нет"

    await safe_send(
        message.reply,
        f"👤 <b>{message.from_user.first_name}</b>\n\n"
        f"🎖 <b>{title_str}</b>\n"
        f"💵 Награда за ответ: <b>${reward:.3f}</b>{sub_line}\n\n"
        f"<code>{bar}</code>\n"
        f"{next_line}\n\n"
        f"💰 Баланс: <b>${float(p['balance']):.4f}</b>\n"
        f"📈 Всего заработано: ${float(p['total_won']):.4f}\n"
        f"💸 Выведено сегодня: ${today:.4f}\n\n"
        f"🏆 Правильных ответов: <b>{ca}</b>\n"
        f"📍 Место в чате: <b>{place_str}</b>"
    )


@dp.message(Command("AiLevels"))
async def cmd_ailevels(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    lines = ["🎖 <b>Уровни и награды</b>\n"]
    for lvl, emoji, name in LEVELS:
        reward = BASE_REWARD + (lvl - 1) * REWARD_STEP
        min_answers = (lvl - 1) * ANSWERS_PER_LEVEL
        if lvl == MAX_LEVEL:
            req = f"{min_answers}+ ответов"
        else:
            req = f"{min_answers}-{min_answers + ANSWERS_PER_LEVEL - 1} ответов"
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
    lines = ["🏆 <b>Топ по правильным ответам</b>"]
    for i, row in enumerate(rows, 1):
        ca = int(row.get("correct_answers", 0))
        lvl, emoji, name, reward, title_str, _ = level_info(ca)
        player_name = row.get("first_name") or row.get("username") or str(row["user_id"])
        medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
        uid = f" · <code>{row['user_id']}</code>" if admin_view else ""
        lines.append(
            f"{medal} {emoji} {player_name} — {ca} отв. · ${float(row['balance']):.4f}{uid}"
        )
    await safe_send(message.reply, "\n".join(lines))


# ==================== ВЫВОД ====================


@dp.message(Command("AiWithdraw"))
async def cmd_aiwithdraw(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    if is_banned_cached(chat_id, user_id):
        await safe_send(message.reply, "🚫 Ты в бане, вывод недоступен.")
        return

    p = await get_player(chat_id, user_id,
                         message.from_user.username, message.from_user.first_name)
    balance = float(p["balance"])

    if balance < MIN_WITHDRAW:
        await safe_send(message.reply, f"❌ Минимум ${MIN_WITHDRAW:.2f}. У тебя ${balance:.4f}")
        return

    today = await withdrawn_today(chat_id, user_id)
    remaining = DAILY_WITHDRAW_LIMIT - today
    if remaining <= 0:
        await safe_send(
            message.reply,
            f"❌ Дневной лимит исчерпан (${DAILY_WITHDRAW_LIMIT:.2f}).\n"
            f"Выведено сегодня: ${today:.4f}"
        )
        return
    amount = min(balance, remaining)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"wd:accept:{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd:reject:{user_id}"),
        ],
    ])
    text = (
        f"💸 <b>Подтверждение вывода</b>\n\n"
        f"Сумма: <b>${amount:.4f}</b> USDT\n"
        f"Куда: на твой Telegram ID <code>{user_id}</code>\n\n"
        f"⚠️ <b>Чтобы вывод прошёл, ты должен зайти в "
        f"<a href=\"{XROCKET_REFERRAL_URL}\">@xrocket</a></b> "
        f"и активировать там свой аккаунт. Без этого выплата не дойдёт.\n\n"
        f"Подтверди вывод кнопкой ниже. Запрос действует "
        f"{WITHDRAW_CONFIRM_TTL // 60} мин."
    )

    sent = await safe_send(message.reply, text, reply_markup=kb)
    if not sent:
        return

    PENDING_WITHDRAWS[sent.message_id] = {
        "chat_id": chat_id, "user_id": user_id,
        "amount": amount, "ts": time.time(),
    }

    async def auto_cancel():
        await asyncio.sleep(WITHDRAW_CONFIRM_TTL)
        info = PENDING_WITHDRAWS.pop(sent.message_id, None)
        if not info:
            return
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=sent.message_id,
                text="⌛ <b>Запрос на вывод истёк.</b>\nСоздай новый через /AiWithdraw.",
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
    action, owner_id_str = parts[1], parts[2]
    try:
        owner_id = int(owner_id_str)
    except ValueError:
        await cb.answer("Ошибка", show_alert=True)
        return
    if cb.from_user.id != owner_id:
        await cb.answer("⛔ Это не твой запрос.", show_alert=True)
        return

    info = PENDING_WITHDRAWS.pop(cb.message.message_id, None)
    if not info:
        await cb.answer("⌛ Запрос уже истёк или обработан.", show_alert=True)
        return

    chat_id = info["chat_id"]
    user_id = info["user_id"]
    amount = info["amount"]

    if action == "reject":
        try:
            await cb.message.edit_text(
                f"❌ <b>Вывод отменён.</b>\n"
                f"Сумма ${amount:.4f} осталась на балансе."
            )
        except Exception:
            pass
        await cb.answer("Отменено")
        return

    await cb.answer("Принято, отправляю...")

    locked = await try_lock_withdraw(chat_id, user_id)
    if not locked:
        try:
            await cb.message.edit_text("⏳ Предыдущий вывод ещё обрабатывается.")
        except Exception:
            pass
        return

    try:
        p = await get_player(chat_id, user_id)
        balance = float(p["balance"])
        if balance < amount:
            amount = balance
        if amount < MIN_WITHDRAW:
            try:
                await cb.message.edit_text(
                    f"❌ Недостаточно средств. Минимум ${MIN_WITHDRAW:.2f}, у тебя ${balance:.4f}"
                )
            except Exception:
                pass
            return

        try:
            await cb.message.edit_text(f"⏳ Отправляю ${amount:.4f} на xRocket...")
        except Exception:
            pass

        ok, result = await xrocket_payout(chat_id, user_id, amount)

        if ok:
            await deduct_balance(chat_id, user_id, amount)
            await log_payout(chat_id, user_id, amount, result, "finished")
            text = (
                f"✅ <b>Выплачено ${amount:.4f}</b>\n"
                f"ID: <code>{result}</code>\n\n"
                f"Если не получил — открой @xrocket и активируй аккаунт."
            )
        else:
            await log_payout(chat_id, user_id, amount, "", "failed")
            text = (
                f"❌ <b>Ошибка выплаты</b>\n<code>{result}</code>\n\n"
                f"Баланс не списан. Проверь, что зашёл в @xrocket."
            )

        try:
            await cb.message.edit_text(text)
        except Exception:
            await safe_send(bot.send_message, chat_id, text)

    finally:
        await unlock_withdraw(chat_id, user_id)


# ==================== ОТВЕТЫ ====================


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_answer(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    chat_id = message.chat.id
    user_id = message.from_user.id

    q = ACTIVE_QUESTIONS.get(chat_id)
    if not q:
        return

    text = (message.text or "").strip().lower()

    # Пункт 42: реакция ❌ на неправильный ответ (только если от админа)
    if text != q["answer"]:
        if is_admin(user_id):
            try:
                await bot.set_message_reaction(chat_id, message.message_id, ["❌"])
            except Exception:
                pass
        return

    if is_banned_cached(chat_id, user_id):
        return

    popped = ACTIVE_QUESTIONS.pop(chat_id, None)
    if popped is None:
        return

    p = await get_player(chat_id, user_id,
                         message.from_user.username, message.from_user.first_name)
    old_level = level_from_correct(int(p["correct_answers"]))
    sub = is_subscriber_cached(chat_id, user_id)
    reward = reward_for(int(p["correct_answers"]), is_subscriber=sub)

    await asyncio.gather(
        clear_active(chat_id),
        add_balance(chat_id, user_id, reward, count_correct=True),
        return_exceptions=True,
    )

    new_correct = int(p["correct_answers"]) + 1
    new_level = level_from_correct(new_correct)

    # Пункт 41: случайная фраза
    phrase = random.choice(CORRECT_PHRASES)
    sub_badge = " 💎×2" if sub else ""

    msg = (
        f"{phrase}{sub_badge}\n"
        f"{message.from_user.first_name} получает <b>${reward:.3f}</b>\n"
        f"<i>Ответ: {q['answer']}</i>"
    )

    if new_level > old_level:
        emoji_new = LEVELS[new_level - 1][1]
        name_new = LEVELS[new_level - 1][2]
        new_reward = reward_for(new_correct, is_subscriber=sub)
        msg += (
            f"\n\n{emoji_new} <b>НОВЫЙ УРОВЕНЬ {new_level}!</b>\n"
            f"🎖 {name_new} · теперь <b>${new_reward:.3f}</b> за ответ"
        )

    # Пункт 43: анимация + отправка
    try:
        await bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass

    sent = await safe_send(message.reply, msg)

    # Пункт 42: реакция ✅ на правильный ответ
    if sent:
        try:
            await bot.set_message_reaction(chat_id, message.message_id, ["✅"])
        except Exception:
            pass


# ==================== СТАРТ ====================


async def main():
    print("=" * 50)
    print("Quiz Bot · 10 уровней · подписка · партнёрка")
    print(f"Вопросов: {len(QUESTIONS)}")
    print(f"Награда: ${BASE_REWARD:.3f} (ур.1) → ${BASE_REWARD + 9*REWARD_STEP:.3f} (ур.10)")
    print(f"Подписка: ${SUBSCRIPTION_PRICE}/нед → ×{SUBSCRIBER_MULTIPLIER}")
    print(f"Админы: {sorted(ADMIN_IDS)}")

    await get_http()
    await asyncio.to_thread(unlock_all_withdrawals_sync)

    global BANNED_CACHE, SUBSCRIBERS_CACHE
    BANNED_CACHE = await asyncio.to_thread(load_bans_sync)
    SUBSCRIBERS_CACHE = await asyncio.to_thread(load_subscribers_sync)
    total_bans = sum(len(s) for s in BANNED_CACHE.values())
    total_subs = sum(len(s) for s in SUBSCRIBERS_CACHE.values())
    print(f"Банов в кэше: {total_bans} · Подписчиков: {total_subs}")

    active_rows = await asyncio.to_thread(load_active_sync)
    for row in active_rows:
        ACTIVE_QUESTIONS[int(row["chat_id"])] = {
            "question": row["question"],
            "answer": row["answer"].lower(),
        }
        QUIZ_ENABLED.add(int(row["chat_id"]))
    if active_rows:
        print(f"Восстановлено активных: {len(active_rows)}")

    me = await bot.get_me()
    print(f"Подключился как @{me.username}")
    asyncio.create_task(question_scheduler())
    asyncio.create_task(caches_refresh_loop())
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
        print("!!! БОТ УПАЛ !!!")
        print(type(e).__name__, "-", e)
        raise
