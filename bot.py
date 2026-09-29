import asyncio
import json
import logging
import os
import random
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

TOOKEN_API_KEY = os.getenv("TOOKEN_API_KEY", "tc_live_715d21ae8549dc1e205dcbdca6d5956aa7d59b0cc7054535")
TOOKEN_BASE_URL = "https://tooken.club/v1"
TOOKEN_MODEL = "deepseek-v4-flash"

XROCKET_API_KEY = os.getenv("XROCKET_API_KEY", "ae53d0c7d02396598dab6e6dc")
XROCKET_BASE = "https://pay.api.xrocket.exchange"

QUESTION_INTERVAL_HOURS = 2
REWARD_PER_ANSWER = 0.01
MIN_WITHDRAW = 0.10
ADMIN_ID = 8130244626

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
ANSWERED_ATTEMPTS = {}
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
            "chat_id": chat_id, "user_id": user_id,
            "username": username, "first_name": first_name,
        }).execute()
    except Exception as e:
        log.warning("insert player: %s", e)

    return {
        "chat_id": chat_id, "user_id": user_id,
        "username": username, "first_name": first_name,
        "balance": 0, "total_won": 0, "correct_answers": 0,
    }


def add_balance(chat_id: int, user_id: int, amount: float, count_correct: bool = False):
    try:
        p = get_player(chat_id, user_id)
        new_balance = float(p["balance"]) + amount
        new_won = float(p["total_won"]) + max(0, amount)
        new_correct = int(p["correct_answers"]) + (1 if count_correct else 0)
        supabase.table("quiz_players").update({
            "balance": new_balance,
            "total_won": new_won,
            "correct_answers": new_correct,
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


def set_balance(chat_id: int, user_id: int, amount: float):
    try:
        get_player(chat_id, user_id)
        supabase.table("quiz_players").update({
            "balance": amount,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("chat_id", chat_id).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        log.warning("set_balance: %s", e)
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


def is_banned(chat_id: int, user_id: int) -> bool:
    try:
        res = supabase.table("quiz_bans").select("user_id").eq(
            "chat_id", chat_id
        ).eq("user_id", user_id).execute()
        return bool(res.data)
    except Exception:
        return False


def ban_user(chat_id: int, user_id: int, reason: str, admin_id: int):
    try:
        supabase.table("quiz_bans").upsert({
            "chat_id": chat_id, "user_id": user_id,
            "reason": reason, "banned_by": admin_id,
        }).execute()
    except Exception as e:
        log.warning("ban_user: %s", e)


def unban_user(chat_id: int, user_id: int):
    try:
        supabase.table("quiz_bans").delete().eq("chat_id", chat_id).eq(
            "user_id", user_id
        ).execute()
    except Exception as e:
        log.warning("unban_user: %s", e)


def admin_check(message: Message) -> bool:
    return message.from_user and message.from_user.id == ADMIN_ID


# ==================== TOOKEN CLUB ====================


async def generate_question() -> tuple:
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


# ==================== XROCKET ====================


async def xrocket_payout(chat_id: int, user_id: int, amount: float) -> tuple:
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
                err = data.get("detail") or data.get("title") or str(data)
                return False, err
    except Exception as e:
        log.warning("xrocket_payout: %s", e)
        return False, str(e)


# ==================== ВОПРОСЫ ====================


async def ask_question(chat_id: int) -> bool:
    q, a = await generate_question()
    if not q:
        log.warning("Не смог сгенерировать вопрос для чата %s", chat_id)
        return False

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
        return True
    except Exception as e:
        log.warning("send question: %s", e)
        return False


async def question_loop():
    await asyncio.sleep(30)
    while True:
        for chat_id in list(QUIZ_ENABLED):
            try:
                await ask_question(chat_id)
            except Exception as e:
                log.warning("ask_question loop: %s", e)
        await asyncio.sleep(QUESTION_INTERVAL_HOURS * 3600)


# ==================== ИГРОВЫЕ КОМАНДЫ ====================


@dp.message(Command("QuizStart"))
async def cmd_quizstart(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Только для групп.")
        return
    if message.from_user.id != ADMIN_ID:
        await message.reply("⛔ Только админ может включить викторину.")
        return

    QUIZ_ENABLED.add(message.chat.id)
    await message.reply(
        "✅ <b>Викторина включена!</b>\n"
        f"Каждые {QUESTION_INTERVAL_HOURS} ч бот задаёт вопрос.\n"
        f"Первый правильный ответ → +${REWARD_PER_ANSWER:.2f}\n"
        f"Вывод от ${MIN_WITHDRAW:.2f} через /AiWithdraw"
    )
    await ask_question(message.chat.id)


@dp.message(Command("QuizStop"))
async def cmd_quizstop(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    if message.from_user.id != ADMIN_ID:
        return
    QUIZ_ENABLED.discard(message.chat.id)
    ACTIVE_QUESTIONS.pop(message.chat.id, None)
    await message.reply("⏸ Викторина выключена.")


@dp.message(Command("Quiz"))
async def cmd_quiz(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    if message.from_user.id != ADMIN_ID:
        await message.reply("⛔ Только админ может запускать вопрос вручную.")
        return
    QUIZ_ENABLED.add(message.chat.id)
    ok = await ask_question(message.chat.id)
    if not ok:
        await message.reply("⚠️ Не смог сгенерировать вопрос. Попробуй ещё раз.")


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

    is_admin = message.from_user and message.from_user.id == ADMIN_ID
    lines = ["🏆 <b>Топ игроков</b>"]
    for i, row in enumerate(res.data, 1):
        name = row.get("first_name") or row.get("username") or str(row["user_id"])
        medal = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
        uid = f" · <code>{row['user_id']}</code>" if is_admin else ""
        lines.append(
            f"{medal} {name} — ${float(row['balance']):.4f} "
            f"({row['correct_answers']} отв.){uid}"
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


# ==================== ОТВЕТЫ ====================


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_answer(message: Message):
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    text = (message.text or "").strip().lower()

    if is_banned(chat_id, user_id):
        return

    q = ACTIVE_QUESTIONS.get(chat_id)
    if not q:
        return

    if user_id in ANSWERED_ATTEMPTS.get(chat_id, set()):
        return

    correct = q["answer"].lower()
    if text == correct:
        ANSWERED_ATTEMPTS.setdefault(chat_id, set()).add(user_id)
        add_balance(chat_id, user_id, REWARD_PER_ANSWER, count_correct=True)
        mark_question_answered(q["id"], user_id)
        ACTIVE_QUESTIONS.pop(chat_id, None)

        await message.reply(
            f"🎉 <b>Правильно!</b>\n"
            f"{message.from_user.first_name} получает <b>${REWARD_PER_ANSWER:.2f}</b>\n"
            f"Проверить баланс: /AiBalance"
        )
        return

    ANSWERED_ATTEMPTS.setdefault(chat_id, set()).add(user_id)


# ==================== АДМИНКА ====================


@dp.message(Command("AiAdmin"))
async def cmd_aiadmin(message: Message):
    if not admin_check(message):
        return
    await message.reply(
        "🛠 <b>Админ-панель</b>\n\n"
        "<b>Викторина</b>\n"
        "/QuizStart — включить + сразу задать вопрос\n"
        "/Quiz — задать вопрос сейчас\n"
        "/QuizStop — выключить\n\n"
        "<b>Статистика</b>\n"
        "/AiStats — глобальная статистика\n"
        "/AiPayouts — последние 20 выплат\n\n"
        "<b>Управление балансом</b>\n"
        "/AiGive &lt;user_id&gt; &lt;сумма&gt; — начислить\n"
        "/AiTake &lt;user_id&gt; &lt;сумма&gt; — списать\n"
        "/AiReset &lt;user_id&gt; — обнулить баланс\n\n"
        "<b>Баны</b>\n"
        "/AiBan &lt;user_id&gt; [причина] — забанить\n"
        "/AiUnban &lt;user_id&gt; — разбанить\n\n"
        "💡 user_id видно в /AiTop (в скобках)."
    )


@dp.message(Command("AiStats"))
async def cmd_aistats(message: Message):
    if not admin_check(message):
        return
    try:
        players = supabase.table("quiz_players").select(
            "balance,total_won,correct_answers"
        ).execute().data or []
        questions = supabase.table("quiz_questions").select("id").execute().data or []
        payouts = supabase.table("quiz_payouts").select(
            "amount,status"
        ).execute().data or []
        bans = supabase.table("quiz_bans").select("user_id").execute().data or []
    except Exception as e:
        await message.reply(f"Ошибка: {e}")
        return

    total_balance = sum(float(p["balance"]) for p in players)
    total_won = sum(float(p["total_won"]) for p in players)
    total_correct = sum(int(p["correct_answers"]) for p in players)
    finished = [p for p in payouts if p["status"] == "finished"]
    failed = [p for p in payouts if p["status"] == "failed"]
    paid_sum = sum(float(p["amount"]) for p in finished)

    await message.reply(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Игроков: {len(players)}\n"
        f"❓ Вопросов задано: {len(questions)}\n"
        f"🏆 Правильных ответов: {total_correct}\n\n"
        f"💰 Сумма балансов: ${total_balance:.4f}\n"
        f"📈 Всего заработано: ${total_won:.4f}\n\n"
        f"💸 Выплат: {len(finished)} (${paid_sum:.4f})\n"
        f"❌ Ошибок выплат: {len(failed)}\n\n"
        f"🚫 Забанено: {len(bans)}"
    )


@dp.message(Command("AiPayouts"))
async def cmd_aipayouts(message: Message):
    if not admin_check(message):
        return
    try:
        res = supabase.table("quiz_payouts").select("*").order(
            "created_at", desc=True
        ).limit(20).execute()
    except Exception as e:
        await message.reply(f"Ошибка: {e}")
        return

    if not res.data:
        await message.reply("Выплат ещё не было.")
        return

    lines = ["💸 <b>Последние выплаты</b>"]
    for p in res.data:
        dt = p["created_at"][:19].replace("T", " ")
        status_emoji = "✅" if p["status"] == "finished" else "❌"
        lines.append(
            f"{status_emoji} ${float(p['amount']):.4f} → "
            f"<code>{p['user_id']}</code> · {dt}"
        )
    await message.reply("\n".join(lines))


@dp.message(Command("AiGive"))
async def cmd_aigive(message: Message):
    if not admin_check(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.reply("Формат: <code>/AiGive &lt;user_id&gt; &lt;сумма&gt;</code>")
        return
    try:
        target = int(parts[1])
        amount = float(parts[2])
    except ValueError:
        await message.reply("user_id — целое, сумма — число.")
        return
    if amount <= 0:
        await message.reply("Сумма должна быть > 0.")
        return

    new_bal = add_balance(message.chat.id, target, amount)
    if new_bal is None:
        await message.reply("Не получилось.")
        return
    await message.reply(
        f"✅ Начислено ${amount:.4f} → <code>{target}</code>\n"
        f"Новый баланс: ${new_bal:.4f}"
    )


@dp.message(Command("AiTake"))
async def cmd_aitake(message: Message):
    if not admin_check(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.reply("Формат: <code>/AiTake &lt;user_id&gt; &lt;сумма&gt;</code>")
        return
    try:
        target = int(parts[1])
        amount = float(parts[2])
    except ValueError:
        await message.reply("user_id — целое, сумма — число.")
        return
    if amount <= 0:
        await message.reply("Сумма должна быть > 0.")
        return

    ok = deduct_balance(message.chat.id, target, amount)
    if not ok:
        await message.reply("Недостаточно средств или игрок не найден.")
        return
    p = get_player(message.chat.id, target)
    await message.reply(
        f"✅ Списано ${amount:.4f} у <code>{target}</code>\n"
        f"Новый баланс: ${float(p['balance']):.4f}"
    )


@dp.message(Command("AiReset"))
async def cmd_aireset(message: Message):
    if not admin_check(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.reply("Формат: <code>/AiReset &lt;user_id&gt;</code>")
        return
    try:
        target = int(parts[1])
    except ValueError:
        await message.reply("user_id — целое число.")
        return
    set_balance(message.chat.id, target, 0)
    await message.reply(f"✅ Баланс <code>{target}</code> обнулён.")


@dp.message(Command("AiBan"))
async def cmd_aiban(message: Message):
    if not admin_check(message):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.reply("Формат: <code>/AiBan &lt;user_id&gt; [причина]</code>")
        return
    try:
        target = int(parts[1])
    except ValueError:
        await message.reply("user_id — целое число.")
        return
    reason = parts[2] if len(parts) > 2 else "без причины"
    ban_user(message.chat.id, target, reason, ADMIN_ID)
    await message.reply(
        f"🚫 <code>{target}</code> забанен в этом чате.\n"
        f"Причина: {reason}"
    )


@dp.message(Command("AiUnban"))
async def cmd_aiunban(message: Message):
    if not admin_check(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.reply("Формат: <code>/AiUnban &lt;user_id&gt;</code>")
        return
    try:
        target = int(parts[1])
    except ValueError:
        await message.reply("user_id — целое число.")
        return
    unban_user(message.chat.id, target)
    await message.reply(f"✅ <code>{target}</code> разбанен.")


# ==================== СТАРТ ====================


async def main():
    print("=" * 50)
    print("Запуск Quiz Bot...")
    print(f"Tooken: {TOOKEN_BASE_URL} / {TOOKEN_MODEL}")
    print(f"xRocket: {XROCKET_BASE}")
    me = await bot.get_me()
    print(f"Подключился как @{me.username}")
    print(f"Админ: {ADMIN_ID}")
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
