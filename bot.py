# bot.py 
import os
import asyncio
import random
import datetime
from typing import Dict, Any, Optional, Set, Tuple, List

from aiogram import Bot, Dispatcher, types
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

# Добавлено для мини-сервера
from aiohttp import web
import json

load_dotenv()

from database import (
    init_db, add_or_update_player, set_country,
    record_win, reset_streak, get_top50, get_player_rank_and_points, get_player_profile
)

# ---------- Настройки ----------
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN не задан. Установи переменную окружения TOKEN или создай .env файл с TOKEN=<токен>")

ROUND_SECONDS = int(os.getenv("ROUND_SECONDS", "25"))  # время на ход в секундах
PROJECT_DIR = os.path.dirname(__file__)
CITIES_FILE = os.path.join(PROJECT_DIR, "cities.txt")

if not os.path.exists(CITIES_FILE):
    raise RuntimeError(f"Файл cities.txt не найден по пути {CITIES_FILE}. Помести туда список городов России (UTF-8).")

# Загружаем список городов (нормализуем)
def normalize_city(s: str) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    s = " ".join(s.split())
    s = s.replace("ё", "е")
    return s

with open(CITIES_FILE, encoding="utf-8") as f:
    CITY_SET: Set[str] = set(normalize_city(line) for line in f if line.strip())

# буквы, которые не учитываются как последняя (берём предыдущую)
SKIP_LAST = set("ьъый")

def last_significant_letter(word: str) -> Optional[str]:
    word = normalize_city(word)
    if not word:
        return None
    # берём последний символ, пропуская SKIP_LAST
    for ch in reversed(word):
        if ch.isalpha():
            ch_norm = ch.replace("ё", "е")
            if ch_norm in SKIP_LAST:
                continue
            return ch_norm
    return None

# ---------- Структуры для игр ----------
games: Dict[str, Dict[str, Any]] = {}
player_game: Dict[int, str] = {}
waiting_player: Optional[int] = None

# ---------- Rematch storage ----------
rematch_offers: Dict[Tuple[int, int], Set[int]] = {}

# ---------- Aiogram init ----------
bot = Bot(token=TOKEN)
dp = Dispatcher()

# ---------- Утилиты ----------
def make_game_id(a: int, b: int) -> str:
    return f"game_{min(a,b)}_{max(a,b)}"

def pair_key(a: int, b: int) -> Tuple[int,int]:
    return (min(a,b), max(a,b))

async def cancel_and_await(task: Optional[asyncio.Task]):
    if not task:
        return
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception:
        return

async def start_turn(game_id: str):
    game = games.get(game_id)
    if not game:
        return
    user_to_move = game["turn"]
    opponent = game["players"][0] if game["players"][1] == user_to_move else game["players"][1]

    last_move = game.get("last_move")

    try:
        # дружелюбное уведомление текущему игроку
        await bot.send_message(user_to_move,
            f"🔔 Твой ход! Назови город на букву: *{(game['last_letter'] or '?').upper()}*.\n"
            f"У тебя есть {ROUND_SECONDS} секунд — не спеши, постарайся написать правильно.",
            parse_mode="Markdown")

        # показываем оппоненту последний ход, если он есть
        if last_move:
            mover_id, city = last_move
            await bot.send_message(opponent,
                                   f"✳️ Соперник <a href='tg://user?id={mover_id}'>назвал</a>: <b>{city}</b>.\n"
                                   f"Ждём ответ (ход <a href='tg://user?id={user_to_move}'>игрока</a>).",
                                   parse_mode="HTML")
        else:
            await bot.send_message(opponent, f"⌛️ Ожидаем ход соперника <a href='tg://user?id={user_to_move}'>игрока</a>...",
                                   parse_mode="HTML")
    except Exception:
        pass

    old_task = game.get("timer_task")
    if old_task and not old_task.done():
        await cancel_and_await(old_task)
    task = asyncio.create_task(turn_timeout(game_id, user_to_move))
    game["timer_task"] = task

async def create_game_between(p1: int, p2: int, first_player: Optional[int] = None):
    gid = make_game_id(p1, p2)
    if gid in games:
        return gid
    if first_player is None:
        first_player = p1
    games[gid] = {
        "players": [p1, p2],
        "turn": first_player,
        "last_letter": None,
        "used_cities": set(),
        "timer_task": None,
        "started_at": datetime.datetime.now(),
        "moves": 0,
        "last_move": None
    }
    player_game[p1] = gid
    player_game[p2] = gid

    try:
        await bot.send_message(p1, f"✅ Найден соперник! Игра началась — ты ходишь {'первым' if first_player==p1 else 'вторым'}.\n"
                                   "Отправь название любого города (Россия). Удачи!")
        await bot.send_message(p2, f"✅ Найден соперник! Игра началась. Ждём хода соперника <a href='tg://user?id={p1}'>игрока</a>.",
                               parse_mode="HTML")
    except Exception:
        pass

    await start_turn(gid)
    return gid

async def offer_rematch_to_players(p1: int, p2: int):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↻ Реванш", callback_data=f"rematch:{p1}:{p2}")]
    ])
    try:
        await bot.send_message(p1, "Нажмите кнопку, чтобы предложить или принять реванш.", reply_markup=kb)
        await bot.send_message(p2, "Нажмите кнопку, чтобы предложить или принять реванш.", reply_markup=kb)
    except Exception:
        pass

async def end_game(game_id: str, winner_id: Optional[int], reason: str):
    game = games.get(game_id)
    if not game:
        return
    p1, p2 = game["players"]
    task = game.get("timer_task")
    if task and not task.done():
        try:
            task.cancel()
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    if winner_id is None:
        # дружелюбный текст для ничьи
        text = f"🤝 Ничья — {reason}."
        try:
            await bot.send_message(p1, text)
            await bot.send_message(p2, text)
        except Exception:
            pass
        # при ничье — опционально сбрасываем серии
        try:
            await reset_streak(p1)
            await reset_streak(p2)
        except Exception:
            pass
    else:
        loser = p1 if winner_id == p2 else p2
        # отправляем персональные более тёплые сообщения (теперь победитель видит соперника)
        try:
            # победителю — показываем ссылку на соперника (loser)
            await bot.send_message(
                winner_id,
                f"🎉 Поздравляю! Ты победил <a href='tg://user?id={loser}'>соперника</a>.\nПричина: {reason}.",
                parse_mode="HTML"
            )
            # проигравшему — показываем ссылку на победителя
            await bot.send_message(
                loser,
                f"😔 Увы, ты проиграл — победил <a href='tg://user?id={winner_id}'>соперник</a>.\nПричина: {reason}.",
                parse_mode="HTML"
            )
        except Exception:
            pass
        try:
            await record_win(winner_id)
        except Exception:
            pass
        # сброс серии проигравшего
        try:
            await reset_streak(loser)
        except Exception:
            pass

    for uid in list(game["players"]):
        player_game.pop(uid, None)
    games.pop(game_id, None)

    await offer_rematch_to_players(p1, p2)

async def turn_timeout(game_id: str, user_id: int):
    try:
        await asyncio.sleep(ROUND_SECONDS)
        game = games.get(game_id)
        if not game:
            return
        if game.get("turn") == user_id:
            opponent = game["players"][0] if game["players"][1] == user_id else game["players"][1]
            await end_game(game_id, opponent, reason=f"просрочил ход (не успел за {ROUND_SECONDS} сек)")
    except asyncio.CancelledError:
        return
    except Exception:
        return

def is_user_in_game(user_id: int) -> bool:
    return user_id in player_game

# ---------- Команды ----------

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await add_or_update_player(message.from_user.id, message.from_user.username)
    await message.answer(
        "Привет! Я — бот для игры в «Города (Россия)» — дуэли 1-на-1. 😊\n\n"
        "Доступные команды:\n"
        "/play — найти соперника и сыграть 1-на-1\n"
        "/leave — выйти из очереди ожидания\n"
        "/surrender — сдаться (если ты в игре)\n"
        "/top — топ-50 по победам и лучшей серии побед\n"
        "/myrank — узнать свой ранг и количество побед\n"
        "/profile — посмотреть профиль: победы, ранг и серия побед\n"
        "/country <Название> — указать страну (будет видна в топе)\n"
        "/cancel_rematch — отменить своё предложение реванша (если было)\n"
        "/help — эта подсказка\n"
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await cmd_start(message)

@dp.message(Command("play"))
async def cmd_play(message: types.Message):
    global waiting_player
    user_id = message.from_user.id
    await add_or_update_player(user_id, message.from_user.username)

    if is_user_in_game(user_id):
        await message.reply("Ты уже в игре — сначала завершите текущую партию (/surrender) или подожди её окончания.")
        return

    if waiting_player is None:
        waiting_player = user_id
        await message.reply("Ты встал в очередь. ❤ Я подберу соперника — напиши /leave, если передумаешь.")
        return

    if waiting_player == user_id:
        await message.reply("Ты уже в очереди — подожди соперника или отправь /leave, чтобы выйти.")
        return

    p1 = waiting_player
    p2 = user_id
    waiting_player = None

    await create_game_between(p1, p2, first_player=p1)

@dp.message(Command("leave"))
async def cmd_leave(message: types.Message):
    global waiting_player
    user_id = message.from_user.id
    if waiting_player == user_id:
        waiting_player = None
        await message.reply("Окей, ты вышел из очереди ожидания. Возвращайся, когда захочешь сыграть!")
        return
    await message.reply("Ты не в очереди ожидания. Отправь /play, чтобы встать в очередь.")

@dp.message(Command("surrender"))
async def cmd_surrender(message: types.Message):
    user_id = message.from_user.id
    if not is_user_in_game(user_id):
        await message.reply("Ты сейчас не в игре.")
        return
    gid = player_game.get(user_id)
    game = games.get(gid)
    if not game:
        await message.reply("Не удалось найти игру — попробуй позже.")
        return
    opponent = game["players"][0] if game["players"][1] == user_id else game["players"][1]
    await end_game(gid, opponent, reason="сдался (/surrender)")

@dp.message(Command("top"))
async def cmd_top(message: types.Message):
    top = await get_top50()
    if not top:
        await message.reply("Пока нет побед — топ пуст. Стань первым! 🏅")
        return
    lines = ["🏆 Топ-50 по победам и лучшей серии побед:\n"]
    for rank, username, country, wins, max_streak in top:
        if country:
            lines.append(f"{rank}. {username} ({country}) — {wins} побед — лучшая серия побед: {max_streak}")
        else:
            lines.append(f"{rank}. {username} — {wins} побед — лучшая серия побед: {max_streak}")
    await message.reply("\n".join(lines))

@dp.message(Command("myrank"))
async def cmd_myrank(message: types.Message):
    user_id = message.from_user.id
    rank, wins = await get_player_rank_and_points(user_id)
    if rank is None:
        await message.reply("Похоже, у тебя ещё нет побед. Начни играть — и ты появишься в таблице! ✨")
    else:
        await message.reply(f"Твой ранг: {rank}\nПобед: {wins}\nЧтобы указать страну — /country <Название>")

@dp.message(Command("profile"))
async def cmd_profile(message: types.Message):
    user_id = message.from_user.id
    profile = await get_player_profile(user_id)
    if not profile:
        await message.reply("Профиль не найден — начни играть (/play), и я сохраню твою статистику.")
        return
    txt = (f"👤 Профиль: {profile['username']}\n"
           f"🏅 Ранг: {profile['rank']}\n"
           f"✅ Побед: {profile['wins']}\n"
           f"🔥 Текущая серия побед: {profile['current_streak']}\n"
           f"🏆 Лучшая серия побед: {profile['max_streak']}\n")
    if profile.get("country"):
        txt += f"🌍 Страна: {profile['country']}\n"
    await message.reply(txt)

@dp.message(Command("country"))
async def cmd_country(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Укажи страну: /country Россия")
        return
    country = parts[1].strip()
    await add_or_update_player(message.from_user.id, message.from_user.username)
    await set_country(message.from_user.id, country)
    await message.reply(f"Отлично — страна сохранена: {country}. Она будет видна в топе, если попадёшь в топ-50. 🌍")

@dp.message(Command("cancel_rematch"))
async def cmd_cancel_rematch(message: types.Message):
    user_id = message.from_user.id
    removed_any = False
    to_notify = []
    for key, offers in list(rematch_offers.items()):
        if user_id in offers:
            offers.discard(user_id)
            removed_any = True
            a,b = key
            other = a if b == user_id else b
            to_notify.append(other)
            if not offers:
                rematch_offers.pop(key, None)
    if not removed_any:
        await message.reply("У тебя нет активных предложений реванша.")
        return
    await message.reply("Окей — твоё предложение реванша отменено.")
    for other in to_notify:
        try:
            await bot.send_message(other, "Соперник отменил своё предложение реванша.")
        except Exception:
            pass

# ---------- Обработка ходов (только не-команды) ----------
@dp.message(lambda message: not any(getattr(e, "type", "") == "bot_command" for e in (message.entities or [])))
async def handle_move(message: types.Message):
    user_id = message.from_user.id
    if not is_user_in_game(user_id):
        return
    gid = player_game.get(user_id)
    game = games.get(gid)
    if not game:
        return

    if game["turn"] != user_id:
        await message.reply("Сейчас не твой ход. Подожди, пожалуйста — ход соперника.")
        return

    city_raw = (message.text or "").strip()
    city = normalize_city(city_raw)
    if not city:
        await message.reply("Не распознал название города. Напиши только название (текстом).")
        return

    if city not in CITY_SET:
        await message.reply("Кажется, такого города нет в базе. Проверь написание и попробуй снова.")
        return

    if city in game["used_cities"]:
        await message.reply("Этот город уже был использован в этой партии — выбери другой.")
        return

    if game["last_letter"]:
        first_letter = city[0]
        needed = game["last_letter"]
        if first_letter != needed:
            await message.reply(f"Нужно назвать город на букву *{needed.upper()}*. Попробуй ещё раз.", parse_mode="Markdown")
            return

    game["used_cities"].add(city)
    game["moves"] += 1
    nxt = last_significant_letter(city)
    game["last_letter"] = nxt

    game["last_move"] = (user_id, city)

    task = game.get("timer_task")
    if task and not task.done():
        await cancel_and_await(task)
        game["timer_task"] = None

    p1, p2 = game["players"]
    opponent = p1 if p2 == user_id else p2
    game["turn"] = opponent

    try:
        await message.reply(f"✅ Принято: {city}. Ход передан сопернику — жди его ответа.")
        await bot.send_message(opponent,
                               f"✳️ Соперник <a href='tg://user?id={user_id}'>назвал</a>: <b>{city}</b>\n"
                               f"Твой ход — ответь городом на букву <b>{(game['last_letter'] or '?').upper()}</b>.\n"
                               f"У тебя {ROUND_SECONDS} сек. Удачи!",
                               parse_mode="HTML")
    except Exception:
        pass

    task = asyncio.create_task(turn_timeout(gid, opponent))
    game["timer_task"] = task

# ---------- Callback для рематча ----------
@dp.callback_query(lambda c: c.data and c.data.startswith("rematch:"))
async def callback_rematch(cb: types.CallbackQuery):
    data = cb.data
    try:
        _, s1, s2 = data.split(":")
        p1 = int(s1); p2 = int(s2)
    except Exception:
        await cb.answer("Некорректный запрос.")
        return
    user_id = cb.from_user.id
    key = pair_key(p1, p2)
    offers = rematch_offers.get(key)
    if offers is None:
        offers = set()
        rematch_offers[key] = offers

    if user_id in offers:
        offers.discard(user_id)
        await cb.answer("Ты отменил(а) своё согласие на реванш.")
        other = p1 if p2 == user_id else p2
        try:
            await bot.send_message(other, "Соперник отменил согласие на реванш.")
        except Exception:
            pass
        if not offers:
            rematch_offers.pop(key, None)
        return
    else:
        offers.add(user_id)
        await cb.answer("Ты согласился(ась) на реванш. Ждём второго игрока...")
        other = p1 if p2 == user_id else p2
        try:
            await bot.send_message(other, "Соперник согласился на реванш — нажми кнопку, чтобы принять.")
        except Exception:
            pass

    if offers == set(key):
        rematch_offers.pop(key, None)
        if is_user_in_game(p1) or is_user_in_game(p2):
            try:
                await bot.send_message(p1, "Один из игроков сейчас в другой партии — реванш отменён.")
                await bot.send_message(p2, "Один из игроков сейчас в другой партии — реванш отменён.")
            except Exception:
                pass
            return
        await create_game_between(p1, p2, first_player=p1)

# ---------- Веб-сервер для сайта ----------
async def handle_index(request: web.Request):
    index_path = os.path.join(PROJECT_DIR, "index.html")
    if not os.path.exists(index_path):
        return web.Response(text="index.html не найден", status=404)
    return web.FileResponse(index_path)

async def handle_api_top(request: web.Request):
    top = await get_top50()
    result = []
    # get_top50 expected to return rows with (rank, username, country, wins, max_streak)
    for rank, username, country, wins, max_streak in top:
        result.append({
            "rank": rank,
            "username": username,
            "country": country,
            "wins": wins,
            "max_streak": max_streak
        })
    return web.json_response(result)

# Новый обработчик для пинга UptimeRobot / health checks
async def handle_uptime_ping(request: web.Request):
    """
    Быстрый ответ для мониторинга (UptimeRobot, Render и т.п.).
    Поддерживает GET/HEAD/POST — возвращает 200 OK.
    """
    # опционально можно логировать source ip / тело, но не обязательно
    # body = await request.text()  # не нужно, чтобы ответ был быстрым
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/top", handle_api_top)

    # маршруты для приёма пинга от UptimeRobot / health checks
    # поддерживаем основные HTTP методы — GET/HEAD/POST
    app.router.add_get("/ping", handle_uptime_ping)
    app.router.add_head("/ping", handle_uptime_ping)
    app.router.add_post("/ping", handle_uptime_ping)

    # дополнительный "стандартный" путь для health checks
    app.router.add_get("/healthz", handle_uptime_ping)
    app.router.add_head("/healthz", handle_uptime_ping)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Web server running on port {port}")

# ---------- При завершении процесса ----------
async def on_startup():
    await init_db()
    print("DB initialized.")

# ---------- Запуск ----------
async def main():
    await on_startup()

    # запускаем бот и веб-сервер параллельно
    bot_task = asyncio.create_task(dp.start_polling(bot))
    web_task = asyncio.create_task(start_web_server())

    print("Bot + Web server started")
    await asyncio.gather(bot_task, web_task)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped")
