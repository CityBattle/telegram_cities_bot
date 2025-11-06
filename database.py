# database.py
import aiosqlite
from typing import List, Tuple, Optional, Dict, Any

DB_FILE = "game.db"

async def init_db():
    """Инициализация БД. Создаёт таблицу players с нужными колонками и выполняет миграцию
    (добавляет колонки current_streak и max_streak, если их нет)."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                country TEXT DEFAULT '',
                wins INTEGER DEFAULT 0,
                current_streak INTEGER DEFAULT 0,
                max_streak INTEGER DEFAULT 0
            )
        """)
        await db.commit()

        # Проверяем, есть ли колонки (на случай, если таблица была создана старой версией)
        cursor = await db.execute("PRAGMA table_info(players)")
        cols = await cursor.fetchall()  # rows: (cid, name, type, notnull, dflt_value, pk)
        col_names = [r[1] for r in cols]
        # добавляем недостающие колонки безопасно через ALTER TABLE
        if "current_streak" not in col_names:
            try:
                await db.execute("ALTER TABLE players ADD COLUMN current_streak INTEGER DEFAULT 0")
                await db.commit()
            except Exception:
                pass
        if "max_streak" not in col_names:
            try:
                await db.execute("ALTER TABLE players ADD COLUMN max_streak INTEGER DEFAULT 0")
                await db.commit()
            except Exception:
                pass

async def add_or_update_player(user_id: int, username: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO players (user_id, username) VALUES (?, ?)",
            (user_id, username or "Player")
        )
        await db.execute("UPDATE players SET username = ? WHERE user_id = ?", (username or "Player", user_id))
        await db.commit()

async def set_country(user_id: int, country: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR IGNORE INTO players (user_id, username) VALUES (?, ?)", (user_id, "Player"))
        await db.execute("UPDATE players SET country = ? WHERE user_id = ?", (country, user_id))
        await db.commit()

async def record_win(user_id: int):
    """Увеличить wins, инкремент current_streak и при необходимости обновить max_streak."""
    async with aiosqlite.connect(DB_FILE) as db:
        # убедимся, что запись существует
        await db.execute("INSERT OR IGNORE INTO players (user_id, username) VALUES (?, ?)", (user_id, "Player"))
        # увеличим wins и current_streak
        await db.execute("UPDATE players SET wins = wins + 1, current_streak = current_streak + 1 WHERE user_id = ?", (user_id,))
        # обновим max_streak, если current_streak превысил max_streak
        await db.execute("""
            UPDATE players
            SET max_streak = current_streak
            WHERE user_id = ? AND current_streak > max_streak
        """, (user_id,))
        await db.commit()

async def reset_streak(user_id: int):
    """Сброс текущей серии побед у игрока (current_streak -> 0)."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR IGNORE INTO players (user_id, username) VALUES (?, ?)", (user_id, "Player"))
        await db.execute("UPDATE players SET current_streak = 0 WHERE user_id = ?", (user_id,))
        await db.commit()

async def get_top50() -> List[Tuple[int, str, str, int, int]]:
    """Возвращает топ 50 по победам. Каждый элемент: (rank, username, country, wins, max_streak)."""
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT user_id, username, country, wins, max_streak FROM players ORDER BY wins DESC, username ASC LIMIT 50")
        rows = await cursor.fetchall()
        result = []
        rank = 1
        for user_id, username, country, wins, max_streak in rows:
            result.append((rank, username or "Player", country or "", wins, max_streak or 0))
            rank += 1
        return result

async def get_player_rank_and_points(user_id: int) -> Tuple[Optional[int], int]:
    """Возвращает (rank, wins) для пользователя. Если пользователя нет — (None, 0)."""
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT wins FROM players WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row:
            return None, 0
        wins = row[0]
        cursor = await db.execute("SELECT COUNT(*) FROM players WHERE wins > ?", (wins,))
        higher = (await cursor.fetchone())[0]
        return higher + 1, wins

async def get_player_profile(user_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает профиль игрока как dict: username, country, wins, current_streak, max_streak, rank."""
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT username, country, wins, current_streak, max_streak FROM players WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        username, country, wins, current_streak, max_streak = row
        cursor = await db.execute("SELECT COUNT(*) FROM players WHERE wins > ?", (wins,))
        higher = (await cursor.fetchone())[0]
        rank = higher + 1
        return {
            "user_id": user_id,
            "username": username or "Player",
            "country": country or "",
            "wins": wins or 0,
            "current_streak": current_streak or 0,
            "max_streak": max_streak or 0,
            "rank": rank
        }
