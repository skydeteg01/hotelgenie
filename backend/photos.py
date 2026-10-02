"""
Подбор реальных фотографий отелей из Википедии / Wikimedia Commons.

Для каждого отеля без фото берётся главное изображение статьи ru.wikipedia
(по заданному заголовку) либо первой подходящей статьи из поиска.
Результат кэшируется в БД; отели без найденного фото остаются с эмодзи.
Фото принадлежат своим авторам (лицензии Commons) — в интерфейсе
показывается ссылка на источник.
"""
from __future__ import annotations

import asyncio
import re

import aiosqlite
import httpx

from .config import config

API = "https://ru.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "HotelGenieBot/1.0 (educational Telegram Mini App)"}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-zа-яё0-9]{4,}", s.lower())}


async def _page_image(client: httpx.AsyncClient, title: str) -> dict | None:
    r = await client.get(API, params={
        "action": "query", "format": "json", "redirects": 1,
        "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 800,
        "titles": title,
    })
    for page in r.json().get("query", {}).get("pages", {}).values():
        th = page.get("thumbnail")
        if th and page.get("title"):
            return {"url": th["source"], "title": page["title"]}
    return None


async def _search_image(client, name: str, city: str) -> dict | None:
    r = await client.get(API, params={
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{name} {city} гостиница", "gsrlimit": 5,
        "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 800,
    })
    need = _words(name)
    pages = sorted(r.json().get("query", {}).get("pages", {}).values(),
                   key=lambda p: p.get("index", 99))
    for p in pages:
        # берём только статьи, в названии которых есть слово из названия отеля
        if p.get("thumbnail") and need & _words(p.get("title", "")):
            return {"url": p["thumbnail"]["source"], "title": p["title"]}
    return None


async def fill_missing() -> int:
    """Ищет фото для отелей, у которых его ещё нет. Возвращает число найденных."""
    found = 0
    async with aiosqlite.connect(config.db_path) as db:
        async with db.execute(
            "SELECT id, name, city, wiki_title FROM hotels "
            "WHERE photo = '' AND photo_tried = 0"
        ) as cur:
            todo = await cur.fetchall()
        if not todo:
            return 0
        async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
            for hid, name, city, wiki in todo:
                try:
                    res = None
                    if wiki:
                        res = await _page_image(client, wiki)
                    if not res:
                        res = await _search_image(client, name, city)
                except Exception as e:  # сеть недоступна — попробуем при след. запуске
                    print(f"photos: {name}: {e}")
                    continue
                if res:
                    page = "https://ru.wikipedia.org/wiki/" + res["title"].replace(" ", "_")
                    await db.execute(
                        "UPDATE hotels SET photo=?, photo_credit=?, source_url=?, "
                        "photo_tried=1 WHERE id=?",
                        (res["url"], "Фото: Википедия / Wikimedia Commons", page, hid),
                    )
                    found += 1
                else:
                    await db.execute("UPDATE hotels SET photo_tried=1 WHERE id=?", (hid,))
                await db.commit()
                await asyncio.sleep(0.2)
    print(f"photos: найдено фото: {found} из {len(todo)}")
    return found
