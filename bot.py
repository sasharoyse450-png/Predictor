import asyncio
import json
import logging
import os
import random
import re
from collections import deque
from datetime import datetime, timezone

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from supabase import create_client, Client

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

# Tooken Club
TOOKEN_API_KEY = os.getenv("TOOKEN_API_KEY", "tc_live_715d21ae8549dc1e205dcbdca6d5956aa7d59b0cc7054535")
TOOKEN_BASE_URL = "https://tooken.club/v1"
TOOKEN_MODEL = "deepseek-v4-flash"

# xRocket
XROCKET_API_KEY = os.getenv("XROCKET_API_KEY", "ae53d0c7d02396598dab6e6dc")
XROCKET_BASE = "https://pay.api.xrocket.exchange"

QUESTION_INTERVAL_HOURS = 2
REWARD_PER_ANSWER = 0.01
MIN_WITHDRAW = 0.10

if not TOKEN:
    print("!!! BOT_TOKEN не задан")
    raise SystemExit(1)
if not SUPABASE_URL or not SUPABASE_KEY:
    print("!!! SUPABASE_URL / SUPABASE_KEY не заданы")
    raise SystemExit(1)

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# chat_id -> {id, question, answer, asked_at}
ACTIVE_QUESTIONS = {}
# chat_id -> set(user_id) — кто уже пытался ответить на текущий вопрос
ANSWERED_ATTEMPTS = {}
# чаты, где включена игра
QUIZ_ENABLED = set()

# ==================== SUPABASE ====================


def get_player(chat_id: int, user_id: int, username: str = None, first_name: str = None):
    try:
        res = supabase.table("quiz_players").select("*").eq("chat_id", chat_id).eq(
            "user_id", user_id
        ).execute()
        if res.data:
            return res.data[0]
    except Exception as e:
        log.warning("get_player: %s", e)

    try:
        supabase.table("quiz_players").insert({
            "chat_id": chat_id,
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
        }).execute()
    except Exception as e:
        log.warning("insert player: %s", e)

    return {
        "chat_id": chat_id, "user_id": user_id,
        "username": username, "first_name": first_name,
        "balance": 0, "total_won": 0, "correct_answers": 0,
    }


def add_balance(chat_id: int, user_id: int, amount: float):
    try:
        p = get_player(chat_id, user_id)
        new_balance = float(p["balance"]) + amount
        new_won = float(p["total_won"]) + max(0, amount)
        supabase.table("quiz_players").update({
            "balance": new_balance,
            "total_won": new_won,
            "correct_answers": int(p["correct_answers"]) + 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("chat_id", chat_id).eq("user_id", user_id).execute()
        return new_balance
    except Exception as e:
        log.warning("add_balance: %s", e)
        return None


def deduct_balance(chat_id: int, user_id: int, amount: float) -> bool:
    try:
        p = get_player(chat_id, user_id)
        if float(p["balance"]) < amount:
            return False
        supabase.table("quiz_players").update({
            "balance": float(p["balance"]) - amount,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("chat_id", chat_id).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        log.warning("deduct_balance: %s", e)
        return False


def save_question(chat_id: int, question: str, answer: str) -> int:
    try:
        res = supabase.table("quiz_questions").insert({
            "chat_id": chat_id, "question": question, "correct_answer": answer,
        }).execute()
        return res.data[0]["id"]
    except Exception as e:
        log.warning("save_question: %s", e)
        return 0


def mark_question_answered(qid: int, user_id: int):
    try:
        supabase.table("quiz_questions").update({
            "answered_by": user_id,
            "answered_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", qid).execute()
    except Exception as e:
        log.warning("mark_answered: %s", e)


def log_payout(chat_id: int, user_id: int, amount: float, payout_id: str, status: str):
    try:
        supabase.table("quiz_payouts").insert({
            "chat_id": chat_id, "user_id": user_id, "amount": amount,
            "xrocket_payout_id": payout_id, "status": status,
        }).execute()
    except Exception as e:
        log.warning("log_payout: %s", e)


# ==================== TOOKEN CLUB ====================


async def generate_question() -> tuple:
    """Возвращает (вопрос, правильный_ответ) или (None, None)."""
    prompt = (
        "Придумай один короткий вопрос для викторины на русском языке. "
        "Ответ должен быть одним словом или числом (без пробелов). "
        "Вопрос не должен быть слишком сложным. "
        'Верни строго JSON: {"question": "...", "answer": "..."}'
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{TOOKEN_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {TOOKEN_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": TOOKEN_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.9,
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    log.warning("Tooken error: %s", data)
                    return None, None
                content = data["choices"][0]["message"]["content"]
                obj = json.loads(content)
                q = obj.get("question", "").strip()
                a = obj.get("answer", "").strip().lower()
                if q and a:
                    return q, a
    except Exception as e:
        log.warning("generate_question: %s", e)
    return None, None


# ==================== XROCKET PAYOUT ====================


async def xrocket_payout(chat_id: int, user_id: int, amount: float) -> tuple:
    """Возвращает (success: bool, message: str)."""
    if not XROCKET_API_KEY:
        return False, "XROCKET_API_KEY не настроен"

    payload = {
        "clientPayoutId": f"quiz_{chat_id}_{user_id}_{int(datetime.now().timestamp())}",
        "target": str(user_id),
        "targetType": "telegram_user_id",
        "asset": "USDT",
        "amount": f"{amount:.4f}",
        "description": "Quiz reward",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{XROCKET_BASE}/api/v1/payouts",
                headers={
                    "Authorization": f"Bearer {XROCKET_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                if resp.status in (200, 201):
                    return True, data.get("payoutId", "")
                else:
                    err = data.get("detail") or data.get("title") or str(data)
                    return False, err
    except Exception as e:
        log.warning("xrocket_payout: %s", e)
        return False, str(e)


# ==================== ПЛАНИРОВЩИК ====================


async def ask_question(chat_id: int):
    q, a = await generate_question()
    if not q:
        log.warning("Не смог сгенерировать вопрос для чата %s", chat_id)
        return

    qid = save_question(chat_id, q, a)
    ACTIVE_QUESTIONS[chat_id] = {
        "id": qid, "question": q, "answer": a,
        "asked_at": datetime.now(timezone.utc),
    }
    ANSWERED_ATTEMPTS[chat_id] = set()

    try:
        await bot.send_message(
            chat_id,
            f"🧠 <b>Вопрос викторины!</b>\n\n"
            f"❓ {q}\n\n"
            f"💰 Первый правильный ответ получает <b>${REWARD_PER_ANSWER:.2f}</b>\n"
            f"⏱ Время пошло!",
        )
    except Exception as e:
        log.warning("send question: %s", e)


async def question_loop():
    """Каждые N часов задаёт вопрос в каждый активный чат."""
    await asyncio.sleep(30)
    while True:
        for chat_id in list(QUIZ_ENABLED):
            try:
                await ask_question(chat_id)
            except Exception as e:
                log.warning("ask_question loop: %s", e)
        await asyncio.sleep(QUESTION_INTERVAL_HOURS * 3600)


# ==================== ХЕНДЛЕРЫ ====================


@dp.message(Command("QuizStart"))
async def cmd_quizstart(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Только для групп.")
        return
    QUIZ_ENABLED.add(message.chat.id)
    await message.reply(
        "✅ <b>Викторина включена!</b>\n"
        f"Каждые {QUESTION_INTERVAL_HOURS} ч бот задаёт вопрос.\n"
        f"Первый правильный ответ → +${REWARD_PER_ANSWER:.2f}\n"
        f"Вывод от ${MIN_WITHDRAW:.2f} через /AiWithdraw\n\n"
        "<b>Команды:</b>\n"
        "/Quiz — задать вопрос сейчас\n"
        "/AiBalance — баланс\n"
        "/AiTop — топ игроков\n"
        "/AiWithdraw — вывод USDT\n"
        "/QuizStop — выключить"
    )
    await ask_question(message.chat.id)


@dp.message(Command("QuizStop"))
async def cmd_quizstop(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    QUIZ_ENABLED.discard(message.chat.id)
    ACTIVE_QUESTIONS.pop(message.chat.id, None)
    await message.reply("⏸ Викторина выключена.")


@dp.message(Command("Quiz"))
async def cmd_quiz(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    QUIZ_ENABLED.add(message.chat.id)
    await ask_question(message.chat.id)


@dp.message(Command("AiBalance"))
async def cmd_aibalance(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return
    p = get_player(message.chat.id, message.from_user.id,
                   message.from_user.username, message.from_user.first_name)
    await message.reply(
        f"💰 <b>Баланс:</b> ${float(p['balance']):.4f}\n"
        f"🏆 Правильных ответов: {p['correct_answers']}\n"
        f"📈 Всего заработано: ${float(p['total_won']):.4f}\n"
        f"💸 Минимум для вывода: ${MIN_WITHDRAW:.2f}"
    )


@dp.message(Command("AiTop"))
async def cmd_aitop(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    try:
        res = supabase.table("quiz_players").select(
            "user_id,username,first_name,balance,correct_answers"
        ).eq("chat_id", message.chat.id).order(
            "balance", desc=True
        ).limit(10).execute()
    except Exception as e:
        await message.reply(f"Ошибка: {e}")
        return

    if not res.data:
        await message.reply("Пока никто не играл.")
        return

    lines = ["🏆 <b>Топ игроков</b>"]
    for i, row in enumerate(res.data, 1):
        name = row.get("first_name") or row.get("username") or str(row["user_id"])
        medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
        lines.append(
            f"{medal} {name} — ${float(row['balance']):.4f} "
            f"({row['correct_answers']} отв.)"
        )
    await message.reply("\n".join(lines))


@dp.message(Command("AiWithdraw"))
async def cmd_aiwithdraw(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return

    p = get_player(message.chat.id, message.from_user.id,
                   message.from_user.username, message.from_user.first_name)
    balance = float(p["balance"])

    if balance < MIN_WITHDRAW:
        await message.reply(
            f"❌ Минимум для вывода: <b>${MIN_WITHDRAW:.2f}</b>\n"
            f"У тебя: ${balance:.4f}\n"
            f"Отвечай на вопросы, чтобы накопить!"
        )
        return

    await message.reply(f"⏳ Отправляю ${balance:.4f} на твой xRocket...")

    ok, result = await xrocket_payout(message.chat.id, message.from_user.id, balance)

    if ok:
        deduct_balance(message.chat.id, message.from_user.id, balance)
        log_payout(message.chat.id, message.from_user.id, balance, result, "finished")
        await message.reply(
            f"✅ <b>Выплата отправлена!</b>\n"
            f"Сумма: <b>${balance:.4f}</b>\n"
            f"Payout ID: <code>{result}</code>"
        )
    else:
        log_payout(message.chat.id, message.from_user.id, balance, "", "failed")
        await message.reply(
            f"❌ <b>Ошибка выплаты:</b>\n<code>{result}</code>\n\n"
            f"Проверь, что твой Telegram ID привязан к xRocket."
        )


# ==================== ОБРАБОТКА ОТВЕТОВ ====================


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

    # уже отвечал на этот вопрос?
    if user_id in ANSWERED_ATTEMPTS.get(chat_id, set()):
        return

    # проверка ответа
    correct = q["answer"].lower()
    if text == correct:
        # помечаем, что этот юзер ответил
        ANSWERED_ATTEMPTS.setdefault(chat_id, set()).add(user_id)

        # начисляем
        add_balance(chat_id, user_id, REWARD_PER_ANSWER)
        mark_question_answered(q["id"], user_id)

        # закрываем вопрос
        ACTIVE_QUESTIONS.pop(chat_id, None)

        await message.reply(
            f"🎉 <b>Правильно!</b>\n"
            f"{message.from_user.first_name} получает <b>${REWARD_PER_ANSWER:.2f}</b>\n"
            f"Проверить баланс: /AiBalance"
        )
        return

    # неправильный ответ — записываем попытку, чтобы не спамил
    ANSWERED_ATTEMPTS.setdefault(chat_id, set()).add(user_id)


# ==================== СТАРТ ====================


async def main():
    print("=" * 50)
    print("Запуск Quiz Bot...")
    print(f"Tooken: {TOOKEN_BASE_URL} / {TOOKEN_MODEL}")
    print(f"xRocket: {XROCKET_BASE}")
    me = await bot.get_me()
    print(f"Подключился как @{me.username}")
    asyncio.create_task(question_loop())
    print("Фоновая задача вопросов запущена.")
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
