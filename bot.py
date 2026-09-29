import asyncio
import json
import logging
import os
import random
from collections import Counter, deque
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("caspredict")

TOKEN = "8781607065:AAFn0AbFLUHkcEaQtSgvn2Ix52HksW3_j-0"
TARGET_ID = 6173495222

BASE_DIR = "/data" if os.path.isdir("/data") else "."
HISTORY_FILE = os.path.join(BASE_DIR, "dice_history.json")
LASTSEEN_FILE = os.path.join(BASE_DIR, "last_seen.json")
HISTORY_LIMIT = 50

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

last_seen = {}
bot_messages = {}
HISTORY = {}

DICE_EMOJI_MAP = {
    "⚽": "football",
    "🏀": "basketball",
    "🎲": "dice",
    "🎯": "darts",
    "🎳": "bowling",
    "🎰": "slot",
}


# ==================== ФАЙЛЫ ====================


def load_history():
    global HISTORY
    if not os.path.exists(HISTORY_FILE):
        log.info("Файл истории не найден: %s", HISTORY_FILE)
        HISTORY = {}
        return
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        HISTORY = {}
        for chat_id_str, games in raw.items():
            try:
                chat_id = int(chat_id_str)
            except ValueError:
                continue
            HISTORY[chat_id] = {}
            for game, values in games.items():
                HISTORY[chat_id][game] = deque(values[-HISTORY_LIMIT:], maxlen=HISTORY_LIMIT)
        log.info("История загружена: чатов %d", len(HISTORY))
    except Exception as e:
        log.warning("Не смог прочитать %s: %s", HISTORY_FILE, e)
        HISTORY = {}


def save_history():
    try:
        raw = {
            str(cid): {g: list(dq) for g, dq in games.items()}
            for cid, games in HISTORY.items()
        }
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        os.replace(tmp, HISTORY_FILE)
    except Exception as e:
        log.warning("Не смог сохранить %s: %s", HISTORY_FILE, e)


def load_last_seen():
    global last_seen
    if not os.path.exists(LASTSEEN_FILE):
        last_seen = {}
        return
    try:
        with open(LASTSEEN_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        last_seen = {}
        for chat_id_str, (dt_str, text) in raw.items():
            try:
                dt = datetime.fromisoformat(dt_str)
            except Exception:
                continue
            last_seen[int(chat_id_str)] = (dt, text)
        log.info("last_seen загружен: чатов %d", len(last_seen))
    except Exception as e:
        log.warning("Не смог прочитать %s: %s", LASTSEEN_FILE, e)
        last_seen = {}


def save_last_seen():
    try:
        raw = {str(cid): (dt.isoformat(), text) for cid, (dt, text) in last_seen.items()}
        tmp = LASTSEEN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        os.replace(tmp, LASTSEEN_FILE)
    except Exception as e:
        log.warning("Не смог сохранить %s: %s", LASTSEEN_FILE, e)


# ==================== УТИЛЫ ====================


def now_utc():
    return datetime.now(timezone.utc)


def get_hist(chat_id: int, game: str) -> deque:
    return HISTORY.setdefault(chat_id, {}).setdefault(game, deque(maxlen=HISTORY_LIMIT))


async def tracked_reply(message: Message, text: str, **kwargs) -> Message:
    sent = await message.reply(text, **kwargs)
    bot_messages.setdefault(message.chat.id, []).append(sent.message_id)
    return sent


# ==================== ПРЕДСКАЗАНИЯ ====================


def _streak(results: list) -> int:
    if not results:
        return 0
    streak = 1
    for i in range(len(results) - 2, -1, -1):
        if results[i] == results[-1]:
            streak += 1
        else:
            break
    return streak


def predict_binary(values: list, goal_values: set) -> tuple:
    if not values:
        return random.choice(["гол", "мимо"]), "нет истории — 50/50", "история: 0"
    results = ["гол" if v in goal_values else "мимо" for v in values]
    goals = results.count("гол")
    misses = len(results) - goals
    st = _streak(results)
    last = results[-1]
    stats = f"всего: {len(results)} | гол: {goals} | мимо: {misses} | серия: {st}x {last}"
    if st >= 2:
        pred = "мимо" if last == "гол" else "гол"
        return pred, f"серия {st}x «{last}» — вероятен перелом", stats
    if goals < misses:
        return "гол", f"гол реже ({goals} vs {misses})", stats
    if misses < goals:
        return "мимо", f"мимо реже ({misses} vs {goals})", stats
    return random.choice(["гол", "мимо"]), "равный счёт — 50/50", stats


def predict_parity(values: list) -> tuple:
    if not values:
        return random.choice(["Чёт", "Нечет"]), "нет истории", "история: 0"
    results = ["Чёт" if v % 2 == 0 else "Нечет" for v in values]
    even = results.count("Чёт")
    odd = results.count("Нечет")
    st = _streak(results)
    last = results[-1]
    stats = f"всего: {len(results)} | Чёт: {even} | Нечет: {odd} | серия: {st}x {last}"
    if st >= 2:
        pred = "Нечет" if last == "Чёт" else "Чёт"
        return pred, f"серия {st}x «{last}» — вероятен перелом", stats
    if even < odd:
        return "Чёт", f"Чёт реже ({even} vs {odd})", stats
    if odd < even:
        return "Нечет", f"Нечет реже ({odd} vs {even})", stats
    return random.choice(["Чёт", "Нечет"]), "равный счёт", stats


def predict_dice_numbers(values: list, count: int) -> tuple:
    if not values:
        nums = random.sample(range(1, 7), count)
        return nums, "нет истории — случайно", "история: 0"
    freq = Counter(values)
    base = list(range(1, 7))
    random.shuffle(base)
    base.sort(key=lambda n: freq.get(n, 0))
    nums = sorted(base[:count])
    freq_str = " ".join(f"{n}:{freq.get(n, 0)}" for n in range(1, 7))
    stats = f"всего: {len(values)} | частоты: {freq_str}"
    return nums, "числа с минимальной частотой", stats


# ==================== СЛЕЖКА ЗА ЭМОДЗИ ====================


@dp.message(F.dice)
async def track_dice(message: Message):
    d = message.dice
    log.info(
        "🎲 DICE ПОЛУЧЕН: emoji=%s value=%s chat=%s type=%s",
        d.emoji, d.value, message.chat.id, message.chat.type,
    )
    if message.chat.type not in ("group", "supergroup"):
        return
    game = DICE_EMOJI_MAP.get(d.emoji)
    if not game:
        return
    hist = get_hist(message.chat.id, game)
    hist.append(d.value)
    save_history()
    log.info("  → записано в %s (всего: %d)", game, len(hist))


# ==================== КОМАНДЫ ====================


@dp.message(Command("kapchenkainfo"))
async def kapchenkainfo(message: Message):
    log.info(
        "/kapchenkainfo от %s в чате %s (type=%s)",
        message.from_user.id if message.from_user else "?",
        message.chat.id,
        message.chat.type,
    )
    if message.chat.type not in ("group", "supergroup"):
        await tracked_reply(message, "Только для групп.")
        return

    data = last_seen.get(message.chat.id)
    if not data:
        await tracked_reply(
            message,
            f"Пользователь {TARGET_ID} ещё не писал(а) в этом чате "
            f"с момента запуска (или последнего сброса) бота.\n"
            f"<i>В памяти чатов: {len(last_seen)}</i>",
        )
        return

    dt, text = data
    total = int((now_utc() - dt).total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)

    await tracked_reply(
        message,
        f"<b>ID {TARGET_ID}</b>\n"
        f"Последнее сообщение: {dt:%d.%m.%Y %H:%M:%S} UTC\n"
        f"Прошло: {h} ч {m} мин {s} сек\n"
        f"Текст: {text}",
    )


@dp.message(Command("deleteall"))
async def deleteall(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    chat_id = message.chat.id
    ids = bot_messages.get(chat_id, [])
    deleted = 0
    for mid in ids:
        try:
            await bot.delete_message(chat_id, mid)
            deleted += 1
        except Exception:
            pass
    bot_messages[chat_id] = []
    try:
        await bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass
    try:
        report = await message.answer(f"Удалено моих сообщений: {deleted}")
        bot_messages.setdefault(chat_id, []).append(report.message_id)
    except Exception:
        pass


@dp.message(Command("Cashistory"))
async def cashistory(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    games = HISTORY.get(message.chat.id, {})
    lines = ["<b>📊 История эмодзи</b>"]
    for game in ("dice", "football", "basketball", "darts", "bowling", "slot"):
        dq = games.get(game)
        if not dq:
            lines.append(f"<b>{game}</b>: нет данных")
            continue
        values = list(dq)
        lines.append(f"<b>{game}</b>: {len(values)} значений\n<code>{values[-20:]}</code>")
    await tracked_reply(message, "\n".join(lines))


@dp.message(Command("Cashistorydebug"))
async def cashistorydebug(message: Message):
    ls = last_seen.get(message.chat.id)
    ls_info = f"{ls[0]:%Y-%m-%d %H:%M:%S} / {ls[1][:30]}" if ls else "нет"
    text = (
        f"chat.id = <code>{message.chat.id}</code>\n"
        f"chat.type = <code>{message.chat.type}</code>\n"
        f"твой user.id = <code>{message.from_user.id if message.from_user else '?'}</code>\n"
        f"TARGET_ID = <code>{TARGET_ID}</code>\n"
        f"чатов в истории: <code>{len(HISTORY)}</code>\n"
        f"ключи: <code>{list(HISTORY.keys())}</code>\n"
        f"last_seen для этого чата: <code>{ls_info}</code>\n"
        f"всего чатов в last_seen: <code>{len(last_seen)}</code>"
    )
    await tracked_reply(message, text)


@dp.message(Command("Casheset"))
async def casheset(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await tracked_reply(
            message,
            "Формат: <code>/Casheset &lt;игра&gt; &lt;значения&gt;</code>\n"
            "Игры: dice, football, basketball, darts, bowling, slot\n"
            "Пример: <code>/Casheset football 4 1 5 2 4 5</code>",
        )
        return
    game = parts[1].lower()
    if game not in DICE_EMOJI_MAP.values():
        await tracked_reply(message, f"Неизвестная игра: {game}")
        return
    try:
        values = [int(x) for x in parts[2:]]
    except ValueError:
        await tracked_reply(message, "Значения должны быть числами.")
        return
    dq = get_hist(message.chat.id, game)
    dq.extend(values)
    save_history()
    await tracked_reply(message, f"✅ В <b>{game}</b> добавлено {len(values)}. Всего: {len(dq)}")


@dp.message(Command("Cashesetclear"))
async def cashesetclear(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    HISTORY.pop(message.chat.id, None)
    save_history()
    await tracked_reply(message, "🗑 История эмодзи очищена.")


@dp.message(Command("Cashistoryreset_lastseen"))
async def cashistoryreset_lastseen(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    last_seen.pop(message.chat.id, None)
    save_last_seen()
    await tracked_reply(message, "🗑 last_seen для этого чата сброшен.")


# ==================== /CasPredict ====================


def caspredict_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Куб", callback_data="cas:dice")],
        [InlineKeyboardButton(text="⚽ Футбол", callback_data="cas:football")],
        [InlineKeyboardButton(text="🏀 Баскетбол", callback_data="cas:basketball")],
    ])


def caspredict_dice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Чёт/Нечет", callback_data="cas:dice_parity")],
        [InlineKeyboardButton(text="1 число (1-6)", callback_data="cas:dice_num:1")],
        [InlineKeyboardButton(text="2 числа (1-6)", callback_data="cas:dice_num:2")],
        [InlineKeyboardButton(text="3 числа (1-6)", callback_data="cas:dice_num:3")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="cas:menu")],
    ])


def caspredict_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="cas:menu")]
    ])


@dp.message(Command("CasPredict", "caspredict"))
async def caspredict(message: Message):
    log.info(
        "/CasPredict от %s в чате %s (type=%s)",
        message.from_user.id if message.from_user else "?",
        message.chat.id,
        message.chat.type,
    )
    if message.chat.type not in ("group", "supergroup"):
        await tracked_reply(message, "Только для групп.")
        return
    await tracked_reply(message, "<b>🎰 CasPredict</b>\nВыбери игру:", reply_markup=caspredict_main_kb())


@dp.callback_query(F.data == "cas:menu")
async def caspredict_menu_cb(cb: CallbackQuery):
    if not isinstance(cb.message, Message):
        await cb.answer("Сообщение недоступно", show_alert=True)
        return
    await cb.message.edit_text("<b>🎰 CasPredict</b>\nВыбери игру:", reply_markup=caspredict_main_kb())
    await cb.answer()


@dp.callback_query(F.data == "cas:dice")
async def caspredict_dice_cb(cb: CallbackQuery):
    if not isinstance(cb.message, Message):
        await cb.answer("Сообщение недоступно", show_alert=True)
        return
    await cb.message.edit_text("<b>🎲 Куб</b>\nВыбери тип предсказания:", reply_markup=caspredict_dice_kb())
    await cb.answer()


@dp.callback_query(F.data == "cas:dice_parity")
async def caspredict_dice_parity_cb(cb: CallbackQuery):
    if not isinstance(cb.message, Message):
        await cb.answer("Сообщение недоступно", show_alert=True)
        return
    hist = list(get_hist(cb.message.chat.id, "dice"))
    pred, reason, stats = predict_parity(hist)
    await cb.message.edit_text(
        f"<b>🎲 Куб: чётность</b>\nПредсказание: <b>{pred}</b>\n<i>{reason}</i>\n<code>{stats}</code>",
        reply_markup=caspredict_back_kb(),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("cas:dice_num:"))
async def caspredict_dice_num_cb(cb: CallbackQuery):
    if not isinstance(cb.message, Message):
        await cb.answer("Сообщение недоступно", show_alert=True)
        return
    try:
        count = int(cb.data.split(":")[-1])
    except (ValueError, IndexError):
        await cb.answer("Ошибка выбора", show_alert=True)
        return
    if count not in (1, 2, 3):
        await cb.answer("Ошибка выбора", show_alert=True)
        return
    hist = list(get_hist(cb.message.chat.id, "dice"))
    nums, reason, stats = predict_dice_numbers(hist, count)
    pred = ", ".join(str(n) for n in nums)
    await cb.message.edit_text(
        f"<b>🎲 Куб: числа</b>\nПредсказание ({count}): <b>{pred}</b>\n<i>{reason}</i>\n<code>{stats}</code>",
        reply_markup=caspredict_back_kb(),
    )
    await cb.answer()


@dp.callback_query(F.data == "cas:football")
async def caspredict_football_cb(cb: CallbackQuery):
    if not isinstance(cb.message, Message):
        await cb.answer("Сообщение недоступно", show_alert=True)
        return
    hist = list(get_hist(cb.message.chat.id, "football"))
    pred, reason, stats = predict_binary(hist, goal_values={4, 5})
    await cb.message.edit_text(
        f"<b>⚽ Футбол</b>\nПредсказание: <b>{pred}</b>\n<i>{reason}</i>\n<code>{stats}</code>",
        reply_markup=caspredict_back_kb(),
    )
    await cb.answer()


@dp.callback_query(F.data == "cas:basketball")
async def caspredict_basketball_cb(cb: CallbackQuery):
    if not isinstance(cb.message, Message):
        await cb.answer("Сообщение недоступно", show_alert=True)
        return
    hist = list(get_hist(cb.message.chat.id, "basketball"))
    pred, reason, stats = predict_binary(hist, goal_values={3, 4, 5})
    await cb.message.edit_text(
        f"<b>🏀 Баскетбол</b>\nПредсказание: <b>{pred}</b>\n<i>{reason}</i>\n<code>{stats}</code>",
        reply_markup=caspredict_back_kb(),
    )
    await cb.answer()


# ==================== СЛЕЖКА ЗА TARGET_ID (В КОНЦЕ, ЧТОБЫ НЕ ЕЛ КОМАНДЫ) ====================


@dp.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.from_user.id == TARGET_ID,
    ~F.text.startswith("/"),  # не трогаем команды
)
async def collect(message: Message):
    text = message.text or message.caption or "<без текста>"
    last_seen[message.chat.id] = (now_utc(), text[:100])
    save_last_seen()
    log.info(
        "👤 TARGET %s написал в чате %s: %s",
        TARGET_ID, message.chat.id, text[:50],
    )


# ==================== СТАРТ ====================


async def main():
    print("Запуск бота...")
    print(f"BASE_DIR = {BASE_DIR}")
    load_history()
    load_last_seen()
    me = await bot.get_me()
    print(f"Подключился как @{me.username}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
