import asyncio
import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.base import BaseEventIsolation
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

XROCKET_API_KEY = os.getenv("XROCKET_API_KEY", "ae53d0c7d02396598dab6e6dc")
XROCKET_BASE = os.getenv("XROCKET_BASE", "https://pay.api.xrocket.exchange")

REWARD_PER_ANSWER = 0.05
MIN_WITHDRAW = 0.05
ADMIN_ID = 8130244626

TZ = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))
WORK_HOURS = list(range(8, 24))

if not TOKEN:
    print("!!! BOT_TOKEN не задан")
    raise SystemExit(1)
if not SUPABASE_URL or not SUPABASE_KEY:
    print("!!! SUPABASE_URL / SUPABASE_KEY не заданы")
    raise SystemExit(1)


class NoIsolation(BaseEventIsolation):
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def lock(self, key):
        return self


bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(events_isolation=NoIsolation())
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

ACTIVE_QUESTIONS = {}       # chat_id -> {"question": str, "answer": str}
ANSWERED_ATTEMPTS = {}      # chat_id -> set(user_id)
QUIZ_ENABLED = set()


# ==================== SAFE SEND ====================


async def safe_send(coro_func, *args, **kwargs):
    for _ in range(3):
        try:
            return await coro_func(*args, **kwargs)
        except TelegramRetryAfter as e:
            log.warning("FloodWait %s сек", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
    return None


# ==================== SUPABASE ====================


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
            "first_name": first_name, "balance": 0, "total_won": 0, "correct_answers": 0}


async def get_player(chat_id, user_id, username=None, first_name=None):
    return await asyncio.to_thread(get_player_sync, chat_id, user_id, username, first_name)


def add_balance_sync(chat_id, user_id, amount, count_correct=False):
    p = get_player_sync(chat_id, user_id)
    nb = float(p["balance"]) + amount
    nw = float(p["total_won"]) + max(0, amount)
    nc = int(p["correct_answers"]) + (1 if count_correct else 0)
    supabase.table("quiz_players").update({
        "balance": nb, "total_won": nw, "correct_answers": nc,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("chat_id", chat_id).eq("user_id", user_id).execute()
    return nb


async def add_balance(chat_id, user_id, amount, count_correct=False):
    return await asyncio.to_thread(add_balance_sync, chat_id, user_id, amount, count_correct)


def deduct_balance_sync(chat_id, user_id, amount):
    p = get_player_sync(chat_id, user_id)
    if float(p["balance"]) < amount:
        return False
    supabase.table("quiz_players").update({
        "balance": float(p["balance"]) - amount,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("chat_id", chat_id).eq("user_id", user_id).execute()
    return True


async def deduct_balance(chat_id, user_id, amount):
    return await asyncio.to_thread(deduct_balance_sync, chat_id, user_id, amount)


async def get_top(chat_id, limit=10):
    def _q():
        try:
            res = supabase.table("quiz_players").select(
                "user_id,username,first_name,balance,correct_answers"
            ).eq("chat_id", chat_id).order("balance", desc=True).limit(limit).execute()
            return res.data or []
        except Exception as e:
            log.warning("get_top: %s", e)
            return []
    return await asyncio.to_thread(_q)


async def get_stats():
    def _q():
        try:
            players = supabase.table("quiz_players").select("balance,total_won,correct_answers").execute().data or []
            payouts = supabase.table("quiz_payouts").select("amount,status").execute().data or []
            return players, payouts
        except Exception as e:
            log.warning("get_stats: %s", e)
            return [], []
    return await asyncio.to_thread(_q)


async def get_payouts(limit=20):
    def _q():
        try:
            res = supabase.table("quiz_payouts").select("*").order("created_at", desc=True).limit(limit).execute()
            return res.data or []
        except Exception as e:
            log.warning("get_payouts: %s", e)
            return []
    return await asyncio.to_thread(_q)


async def log_payout(chat_id, user_id, amount, payout_id, status):
    def _q():
        try:
            supabase.table("quiz_payouts").insert({
                "chat_id": chat_id, "user_id": user_id, "amount": amount,
                "xrocket_payout_id": payout_id, "status": status,
            }).execute()
        except Exception as e:
            log.warning("log_payout: %s", e)
    await asyncio.to_thread(_q)


# ==================== XROCKET (пробует несколько endpoint'ов) ====================


async def xrocket_payout(chat_id, user_id, amount):
    if not XROCKET_API_KEY:
        return False, "XROCKET_API_KEY пуст"

    payload = {
        "clientPayoutId": f"quiz_{chat_id}_{user_id}_{int(datetime.now().timestamp())}",
        "target": str(user_id),
        "targetType": "telegram_user_id",
        "asset": "USDT",
        "amount": f"{amount:.4f}",
        "description": "Quiz reward",
    }

    # Пробуем все комбинации endpoint+header
    attempts = [
        ("POST", "https://pay.api.xrocket.exchange/api/v1/payouts",
         {"Rocket-Pay-Key": XROCKET_API_KEY, "Content-Type": "application/json"}),
        ("POST", "https://api.xrocket.exchange/api/v1/payouts",
         {"Rocket-Pay-Key": XROCKET_API_KEY, "Content-Type": "application/json"}),
        ("POST", "https://pay.api.xrocket.exchange/api/v1/transfer",
         {"Rocket-Pay-Key": XROCKET_API_KEY, "Content-Type": "application/json"}),
        ("POST", "https://api.xrocket.exchange/api/v1/transfer",
         {"Rocket-Pay-Key": XROCKET_API_KEY, "Content-Type": "application/json"}),
    ]

    last_error = None
    for method, url, headers in attempts:
        try:
            timeout = aiohttp.ClientTimeout(total=8, connect=4)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.request(method, url, headers=headers, json=payload) as r:
                    text = await r.text()
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {"raw": text[:200]}
                    log.info("xRocket [%s] %s: %s", r.status, url, data)
                    if r.status in (200, 201):
                        return True, data.get("payoutId", "") or data.get("id", "")
                    last_error = data.get("detail") or data.get("title") or str(data)
        except Exception as e:
            log.warning("xrocket attempt %s: %s", url, e)
            last_error = str(e)

    return False, last_error or "Все endpoint'ы отклонили"


# ==================== ВОПРОСЫ ====================


def pick_question():
    """Случайный вопрос из 500."""
    return random_question()


async def ask_question(chat_id):
    q, a = pick_question()
    ACTIVE_QUESTIONS[chat_id] = {"question": q, "answer": a.lower()}
    ANSWERED_ATTEMPTS[chat_id] = set()
    try:
        await safe_send(
            bot.send_message,
            chat_id,
            f"🧠 <b>Вопрос!</b>\n\n"
            f"❓ {q}\n\n"
            f"💰 Первый правильный ответ → <b>${REWARD_PER_ANSWER:.2f}</b>\n"
            f"🔓 Вопрос открыт, пока кто-то не ответит верно."
        )
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
            InlineKeyboardButton(text="🧪 Проверить xRocket", callback_data="adm:xrdbg"),
        ],
    ])


def admin_text(chat_id):
    status = "🟢 включена" if chat_id in QUIZ_ENABLED else "🔴 выключена"
    current = ACTIVE_QUESTIONS.get(chat_id)
    current_txt = f"\n🔓 Открыт: {current['question']}" if current else ""
    return (
        f"🛠 <b>Админ-панель</b>\n"
        f"Викторина: {status}\n"
        f"Расписание: каждый час с 8:00 до 23:00 ({TZ.key})\n"
        f"Награда: ${REWARD_PER_ANSWER:.2f}\n"
        f"Вывод от: ${MIN_WITHDRAW:.2f}\n"
        f"Вопросов в базе: {len(QUESTIONS)}"
        f"{current_txt}"
    )


@dp.message(Command("AiAdmin"))
async def cmd_aiadmin(message: Message):
    log.info("/AiAdmin от %s", message.from_user.id if message.from_user else "?")
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    if message.chat.type not in ("group", "supergroup"):
        return
    await safe_send(message.reply, admin_text(message.chat.id), reply_markup=admin_kb(message.chat.id))


@dp.callback_query(F.data.startswith("adm:"))
async def on_admin_cb(cb: CallbackQuery):
    if not cb.from_user or cb.from_user.id != ADMIN_ID:
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
        players, payouts = await get_stats()
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
            f"🏆 Правильных: {tc}\n\n"
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
            name = row.get("first_name") or row.get("username") or str(row["user_id"])
            medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
            lines.append(f"{medal} {name} — ${float(row['balance']):.4f} ({row['correct_answers']} отв.) · <code>{row['user_id']}</code>")
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
    lines = ["🧪 <b>xRocket debug</b>", f"Key: <code>{XROCKET_API_KEY[:12]}...</code>", ""]
    tests = [
        ("GET", "https://pay.api.xrocket.exchange/api/v1/me", {"Rocket-Pay-Key": XROCKET_API_KEY}),
        ("GET", "https://api.xrocket.exchange/api/v1/me", {"Rocket-Pay-Key": XROCKET_API_KEY}),
        ("GET", "https://pay.api.xrocket.exchange/api/v1/balance", {"Rocket-Pay-Key": XROCKET_API_KEY}),
    ]
    for method, url, headers in tests:
        try:
            timeout = aiohttp.ClientTimeout(total=6)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.request(method, url, headers=headers) as r:
                    text = (await r.text())[:200]
                    lines.append(f"[{r.status}] <code>{url}</code>\n<code>{text}</code>\n")
        except Exception as e:
            lines.append(f"[ERR] {url}: <code>{e}</code>")
    return "\n".join(lines)


# ==================== ИГРОВЫЕ КОМАНДЫ ====================


@dp.message(CommandStart())
async def cmd_start(message: Message):
    log.info("/start от %s", message.from_user.id if message.from_user else "?")
    await safe_send(
        message.reply,
        f"👋 <b>Викторина!</b>\n\n"
        f"💰 ${REWARD_PER_ANSWER:.2f} за первый правильный ответ\n"
        f"⏰ Каждый час с 8:00 до 23:00\n"
        f"💸 Вывод от ${MIN_WITHDRAW:.2f}\n\n"
        f"<b>Команды:</b>\n/AiBalance · /AiWithdraw · /AiTop"
    )


@dp.message(Command("AiBalance"))
async def cmd_aibalance(message: Message):
    log.info("/AiBalance от %s", message.from_user.id if message.from_user else "?")
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    p = await get_player(message.chat.id, message.from_user.id,
                         message.from_user.username, message.from_user.first_name)
    await safe_send(
        message.reply,
        f"💰 <b>${float(p['balance']):.4f}</b>\n"
        f"🏆 Правильных: {p['correct_answers']}\n"
        f"📈 Всего: ${float(p['total_won']):.4f}"
    )


@dp.message(Command("AiTop"))
async def cmd_aitop(message: Message):
    log.info("/AiTop от %s", message.from_user.id if message.from_user else "?")
    if message.chat.type not in ("group", "supergroup"):
        return
    rows = await get_top(message.chat.id, 10)
    if not rows:
        await safe_send(message.reply, "Никто не играл.")
        return
    is_admin = message.from_user and message.from_user.id == ADMIN_ID
    lines = ["🏆 <b>Топ игроков</b>"]
    for i, row in enumerate(rows, 1):
        name = row.get("first_name") or row.get("username") or str(row["user_id"])
        medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
        uid = f" · <code>{row['user_id']}</code>" if is_admin else ""
        lines.append(f"{medal} {name} — ${float(row['balance']):.4f} ({row['correct_answers']} отв.){uid}")
    await safe_send(message.reply, "\n".join(lines))


@dp.message(Command("AiWithdraw"))
async def cmd_aiwithdraw(message: Message):
    log.info("/AiWithdraw от %s", message.from_user.id if message.from_user else "?")
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    p = await get_player(message.chat.id, message.from_user.id,
                         message.from_user.username, message.from_user.first_name)
    balance = float(p["balance"])
    if balance < MIN_WITHDRAW:
        await safe_send(message.reply, f"❌ Минимум ${MIN_WITHDRAW:.2f}. У тебя ${balance:.4f}")
        return

    msg = await safe_send(message.reply, f"⏳ Отправляю ${balance:.4f}...")
    ok, result = await xrocket_payout(message.chat.id, message.from_user.id, balance)

    if ok:
        await deduct_balance(message.chat.id, message.from_user.id, balance)
        await log_payout(message.chat.id, message.from_user.id, balance, result, "finished")
        text = f"✅ <b>Выплачено ${balance:.4f}</b>\nID: <code>{result}</code>"
    else:
        await log_payout(message.chat.id, message.from_user.id, balance, "", "failed")
        text = f"❌ <b>Ошибка</b>\n<code>{result}</code>"

    if msg:
        try:
            await msg.edit_text(text)
        except Exception:
            await safe_send(message.reply, text)
    else:
        await safe_send(message.reply, text)


# ==================== ОТВЕТЫ (ПОСЛЕДНИМ) ====================


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_answer(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    chat_id = message.chat.id
    user_id = message.from_user.id
    text = (message.text or "").strip().lower()

    q = ACTIVE_QUESTIONS.get(chat_id)
    if not q:
        return
    if user_id in ANSWERED_ATTEMPTS.get(chat_id, set()):
        return

    if text == q["answer"]:
        ANSWERED_ATTEMPTS.setdefault(chat_id, set()).add(user_id)
        await add_balance(chat_id, user_id, REWARD_PER_ANSWER, count_correct=True)
        ACTIVE_QUESTIONS.pop(chat_id, None)
        await safe_send(
            message.reply,
            f"🎉 <b>Правильно!</b>\n"
            f"{message.from_user.first_name} получает <b>${REWARD_PER_ANSWER:.2f}</b>\n"
            f"Ответ: <b>{q['answer']}</b>"
        )
        return

    # неправильный — не блокируем, пусть другие пробуют
    ANSWERED_ATTEMPTS.setdefault(chat_id, set()).add(user_id)


# ==================== СТАРТ ====================


async def main():
    print("=" * 50)
    print("Запуск Quiz Bot (без ИИ, 500 вопросов)")
    print(f"Вопросов в базе: {len(QUESTIONS)}")
    print(f"xRocket key: {XROCKET_API_KEY[:12]}...")
    print(f"Часы: {WORK_HOURS[0]}:00 - {WORK_HOURS[-1]}:00 ({TZ.key})")
    me = await bot.get_me()
    print(f"Подключился как @{me.username}")
    print(f"Админ: {ADMIN_ID}")
    asyncio.create_task(question_scheduler())
    print("Планировщик запущен.")
    print("=" * 50)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("!!! БОТ УПАЛ !!!")
        print(type(e).__name__, "-", e)
        raise
