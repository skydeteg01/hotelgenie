"""
Слой доступа к данным (Data Access Layer).

Инкапсулирует всю работу с базой данных SQLite:
создание схемы, поиск отелей по критериям, управление избранным
и историей поисковых запросов пользователей.

Используется асинхронный драйвер aiosqlite, что позволяет
не блокировать цикл событий FastAPI при обращениях к БД.
"""
from __future__ import annotations

import json
from typing import Optional

import aiosqlite

from .config import config
from .models import Hotel, SearchCriteria


# --- Создание схемы -----------------------------------------------------

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS hotels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    city        TEXT    NOT NULL,
    price       INTEGER NOT NULL,
    stars       INTEGER NOT NULL,
    rating      REAL    NOT NULL,
    capacity    INTEGER NOT NULL,
    amenities   TEXT    NOT NULL,   -- JSON-массив строк
    near_sea    INTEGER NOT NULL,   -- 0/1, в SQLite нет типа BOOLEAN
    description TEXT    NOT NULL,
    image       TEXT    NOT NULL,
    name_en     TEXT    NOT NULL DEFAULT '',
    wiki_title  TEXT,
    photo       TEXT    NOT NULL DEFAULT '',
    photo_credit TEXT   NOT NULL DEFAULT '',
    source_url  TEXT    NOT NULL DEFAULT '',
    photo_tried INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS favorites (
    user_id  INTEGER NOT NULL,
    hotel_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, hotel_id),
    FOREIGN KEY (hotel_id) REFERENCES hotels(id)
);

CREATE TABLE IF NOT EXISTS search_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    query     TEXT    NOT NULL,
    created_at TEXT   NOT NULL DEFAULT (datetime('now'))
);
"""


def _ulower(value: str | None) -> str | None:
    """Unicode-корректное приведение к нижнему регистру.

    Встроенная функция SQLite LOWER() работает только с латиницей и не
    приводит кириллицу к нижнему регистру, из-за чего регистронезависимый
    поиск по русским названиям городов ломается. Поэтому регистрируем
    собственную функцию на Python, который корректно обрабатывает Unicode.
    """
    return value.lower() if value else value


async def _connect() -> aiosqlite.Connection:
    """Открывает соединение и регистрирует пользовательские функции."""
    db = await aiosqlite.connect(config.db_path)
    await db.create_function("ulower", 1, _ulower, deterministic=True)
    return db


async def init_db() -> None:
    """Создаёт таблицы, если они ещё не существуют."""
    async with aiosqlite.connect(config.db_path) as db:
        await db.executescript(CREATE_TABLES_SQL)
        await db.commit()


def _row_to_hotel(row: aiosqlite.Row) -> Hotel:
    """Преобразует строку БД в доменный объект Hotel."""
    return Hotel(
        id=row["id"],
        name=row["name"],
        city=row["city"],
        price=row["price"],
        stars=row["stars"],
        rating=row["rating"],
        capacity=row["capacity"],
        amenities=json.loads(row["amenities"]),
        near_sea=bool(row["near_sea"]),
        description=row["description"],
        image=row["image"],
        name_en=row["name_en"],
        photo=row["photo"],
        photo_credit=row["photo_credit"],
        source_url=row["source_url"],
    )


# --- Поиск отелей -------------------------------------------------------

async def search_hotels(
    criteria: SearchCriteria, limit: int = 20
) -> list[Hotel]:
    """
    Ищет отели по заданным критериям.

    Числовые и строковые ограничения переносятся в SQL-запрос
    (фильтрация на стороне БД), а удобства и вместимость, требующие
    разбора JSON, проверяются и ранжируются в Python.
    Результат сортируется по релевантности: сначала наиболее
    подходящие, при равенстве — по рейтингу.
    """
    where: list[str] = []
    params: list = []

    if criteria.city:
        # ulower — собственная Unicode-функция (см. _ulower):
        # обеспечивает регистронезависимый поиск по кириллице.
        where.append("ulower(city) LIKE ?")
        params.append(f"%{criteria.city.lower()}%")
    if criteria.max_price:
        where.append("price <= ?")
        params.append(criteria.max_price)
    if criteria.min_price:
        where.append("price >= ?")
        params.append(criteria.min_price)
    if criteria.min_stars:
        where.append("stars >= ?")
        params.append(criteria.min_stars)
    if criteria.min_rating:
        where.append("rating >= ?")
        params.append(criteria.min_rating)
    if criteria.near_sea:
        where.append("near_sea = 1")

    sql = "SELECT * FROM hotels"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY rating DESC"

    db = await _connect()
    try:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
    finally:
        await db.close()

    hotels = [_row_to_hotel(r) for r in rows]

    # Дополнительная фильтрация по вместимости и удобствам в Python:
    # эти поля плохо ложатся на простой SQL (JSON-массив удобств).
    if criteria.guests:
        hotels = [h for h in hotels if h.capacity >= criteria.guests]

    if criteria.amenities:
        wanted = {a.lower() for a in criteria.amenities}

        def match_score(h: Hotel) -> int:
            have = {a.lower() for a in h.amenities}
            return len(wanted & have)

        # Оставляем отели хотя бы с одним совпавшим удобством
        # и сортируем по количеству совпадений (релевантности).
        hotels = [h for h in hotels if match_score(h) > 0]
        hotels.sort(key=lambda h: (match_score(h), h.rating), reverse=True)

    return hotels[:limit]


def _mismatches(h: Hotel, c: SearchCriteria) -> list[str]:
    """Чем отель не дотягивает до исходного запроса."""
    out: list[str] = []
    if c.city and c.city.lower() not in h.city.lower():
        out.append(f"другой город ({h.city})")
    if c.max_price and h.price > c.max_price:
        out.append(f"дороже бюджета на {h.price - c.max_price} ₽")
    if c.min_price and h.price < c.min_price:
        out.append("дешевле заданного минимума")
    if c.min_stars and h.stars < c.min_stars:
        out.append(f"{h.stars}★ вместо {c.min_stars}★")
    if c.min_rating and h.rating < c.min_rating:
        out.append(f"рейтинг {h.rating}")
    if c.near_sea and not h.near_sea:
        out.append("не у моря")
    if c.guests and h.capacity < c.guests:
        out.append(f"номер на {h.capacity} чел.")
    if c.amenities:
        have = {a.lower() for a in h.amenities}
        miss = [a for a in c.amenities if a.lower() not in have]
        if miss:
            out.append("нет: " + ", ".join(miss))
    return out


async def search_smart(
    criteria: SearchCriteria, want: int = 6, limit: int = 12
) -> tuple[list[Hotel], int]:
    """
    Мягкий поиск: сначала точные совпадения, затем, если их мало,
    постепенно ослабляем условия (удобства, бюджет +30%, звёзды/рейтинг,
    близость к морю, город) и добавляем близкие варианты с пометкой.
    Возвращает (отели, число_точных).
    """
    from dataclasses import replace

    exact = await search_hotels(criteria, limit=limit)
    if len(exact) >= want:
        return exact, len(exact)

    seen = {h.id for h in exact}
    extra: list[Hotel] = []
    c = replace(criteria, amenities=list(criteria.amenities))

    steps = [
        lambda x: setattr(x, "amenities", []),
        lambda x: setattr(x, "max_price", int(x.max_price * 1.3)) if x.max_price else None,
        lambda x: setattr(x, "max_price", int(x.max_price * 1.6)) if x.max_price else None,
        lambda x: (setattr(x, "min_stars", max(1, x.min_stars - 1)) if x.min_stars else None,
                   setattr(x, "min_rating", None)),
        lambda x: setattr(x, "near_sea", None),
        lambda x: setattr(x, "guests", None),
        lambda x: setattr(x, "city", None),
        lambda x: setattr(x, "max_price", int(x.max_price * 1.6)) if x.max_price else None,
    ]
    for step in steps:
        step(c)
        for h in await search_hotels(c, limit=50):
            if h.id not in seen:
                seen.add(h.id)
                miss = _mismatches(h, criteria)
                h.note = "; ".join(miss)
                extra.append(h)
        if len(exact) + len(extra) >= want * 2:
            break

    def penalty(h: Hotel) -> int:
        return (h.note.count(";") + 1) + (3 if "другой город" in h.note else 0)

    extra.sort(key=lambda h: (penalty(h), -h.rating))
    return (exact + extra)[:limit], len(exact)


async def get_hotel(hotel_id: int) -> Optional[Hotel]:
    """Возвращает один отель по идентификатору."""
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM hotels WHERE id = ?", (hotel_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_hotel(row) if row else None


async def list_cities() -> list[str]:
    """Список доступных городов (для фильтров интерфейса)."""
    async with aiosqlite.connect(config.db_path) as db:
        async with db.execute(
            "SELECT DISTINCT city FROM hotels ORDER BY city"
        ) as cursor:
            rows = await cursor.fetchall()
    return [r[0] for r in rows]


# --- Избранное ----------------------------------------------------------

async def toggle_favorite(user_id: int, hotel_id: int) -> bool:
    """
    Добавляет или удаляет отель из избранного пользователя.
    Возвращает True, если отель теперь в избранном, иначе False.
    """
    async with aiosqlite.connect(config.db_path) as db:
        async with db.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND hotel_id = ?",
            (user_id, hotel_id),
        ) as cursor:
            exists = await cursor.fetchone()

        if exists:
            await db.execute(
                "DELETE FROM favorites WHERE user_id = ? AND hotel_id = ?",
                (user_id, hotel_id),
            )
            await db.commit()
            return False

        await db.execute(
            "INSERT INTO favorites (user_id, hotel_id) VALUES (?, ?)",
            (user_id, hotel_id),
        )
        await db.commit()
        return True


async def get_favorites(user_id: int) -> list[Hotel]:
    """Возвращает список избранных отелей пользователя."""
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT h.* FROM hotels h
            JOIN favorites f ON f.hotel_id = h.id
            WHERE f.user_id = ?
            ORDER BY h.rating DESC
            """,
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_hotel(r) for r in rows]


async def get_favorite_ids(user_id: int) -> set[int]:
    """Множество id избранных отелей — для пометки карточек в выдаче."""
    async with aiosqlite.connect(config.db_path) as db:
        async with db.execute(
            "SELECT hotel_id FROM favorites WHERE user_id = ?", (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
    return {r[0] for r in rows}


# --- История поиска -----------------------------------------------------

async def add_history(user_id: int, query: str) -> None:
    """Сохраняет поисковый запрос в историю пользователя."""
    if not query.strip():
        return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO search_history (user_id, query) VALUES (?, ?)",
            (user_id, query.strip()),
        )
        await db.commit()


async def get_history(user_id: int, limit: int = 8) -> list[str]:
    """Возвращает последние уникальные запросы пользователя."""
    async with aiosqlite.connect(config.db_path) as db:
        async with db.execute(
            """
            SELECT query, MAX(created_at) AS ts
            FROM search_history
            WHERE user_id = ?
            GROUP BY query
            ORDER BY ts DESC
            LIMIT ?
            """,
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    return [r[0] for r in rows]
