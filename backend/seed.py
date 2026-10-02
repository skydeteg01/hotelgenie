"""
Заполнение базы реальными отелями из hotels_data.py.

При смене SEED_VERSION таблица отелей пересоздаётся (избранное
очищается, т.к. идентификаторы отелей меняются). Фото подтягиваются
отдельно (photos.py) и кэшируются в БД.
"""
from __future__ import annotations

import json

import aiosqlite

from .config import config
from .database import init_db
from .hotels_data import HOTELS

SEED_VERSION = "2"


async def seed() -> None:
    await init_db()
    async with aiosqlite.connect(config.db_path) as db:
        async with db.execute(
            "SELECT value FROM meta WHERE key='seed_version'"
        ) as cur:
            row = await cur.fetchone()
        if row and row[0] == SEED_VERSION:
            return
        # старая схема без новых колонок -> пересоздаём таблицу отелей
        await db.execute("DELETE FROM favorites")
        await db.execute("DROP TABLE IF EXISTS hotels")
        await db.commit()
    await init_db()
    async with aiosqlite.connect(config.db_path) as db:
        await db.executemany(
            """INSERT INTO hotels
               (name, name_en, city, stars, price, rating, capacity,
                amenities, near_sea, description, image, wiki_title)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (n, en, c, st, pr, rt, cap,
                 json.dumps(am, ensure_ascii=False), 1 if sea else 0,
                 desc, img, wiki)
                for (n, en, c, st, pr, rt, cap, am, sea, desc, img, wiki)
                in HOTELS
            ],
        )
        await db.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('seed_version',?)",
            (SEED_VERSION,),
        )
        await db.commit()
    print(f"Загружено отелей: {len(HOTELS)}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(seed())
