"""
Подбор реальных фотографий отелей без платных API.

Источники (по очереди, до первого подходящего):
  1. статья ru.wikipedia (заданный заголовок);
  2. поиск по ru.wikipedia;
  3. поиск по en.wikipedia (по английскому названию);
  4. поиск файлов в Wikimedia Commons (по названию и городу).

Фото берётся только если в названии статьи/файла есть значимое слово из
названия отеля — чтобы не подставлять чужие картинки. Результат кэшируется
в БД; у отелей без найденного фото остаётся эмодзи.
"""
from __future__ import annotations

import asyncio
import html
import re

import aiosqlite
import httpx

from .config import config

HEADERS = {"User-Agent": "HotelGenieBot/1.0 (educational Telegram Mini App)"}
GENERIC = {
    "hotel", "resort", "spa", "grand", "palace", "park", "city", "center",
    "centre", "отель", "гостиница", "курорт", "парк", "центр", "the",
    "by", "inn", "saint", "petersburg", "moscow", "sochi", "kazan",
    "москва", "сочи", "казань", "санкт", "петербург", "marriott", "hilton",
}
BAD_FILE = re.compile(r"\.(svg|pdf|tif|tiff|gif|ogg|webm)$|logo|logotype|map|plan|схема|логотип|герб", re.I)


def _words(s: str, strict: bool = False) -> set[str]:
    ws = set(re.findall(r"[a-zа-яё0-9]{4,}", s.lower()))
    return ws - GENERIC if strict else ws


def _title_ok(name_words: set[str], title: str) -> bool:
    return bool(name_words & _words(title))


async def _wiki_page(client, lang: str, title: str) -> dict | None:
    r = await client.get(f"https://{lang}.wikipedia.org/w/api.php", params={
        "action": "query", "format": "json", "redirects": 1,
        "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 800,
        "titles": title,
    })
    for page in r.json().get("query", {}).get("pages", {}).values():
        th = page.get("thumbnail")
        if th and page.get("title"):
            return {"url": th["source"], "title": page["title"], "lang": lang}
    return None


async def _wiki_search(client, lang: str, query: str, need: set[str]) -> dict | None:
    r = await client.get(f"https://{lang}.wikipedia.org/w/api.php", params={
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": query, "gsrlimit": 6,
        "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 800,
    })
    pages = sorted(r.json().get("query", {}).get("pages", {}).values(),
                   key=lambda p: p.get("index", 99))
    for p in pages:
        if p.get("thumbnail") and _title_ok(need, p.get("title", "")):
            return {"url": p["thumbnail"]["source"], "title": p["title"], "lang": lang}
    return None


async def _commons_search(client, query: str, need: set[str]) -> dict | None:
    r = await client.get("https://commons.wikimedia.org/w/api.php", params={
        "action": "query", "format": "json", "generator": "search",
        "gsrnamespace": 6, "gsrsearch": query, "gsrlimit": 10,
        "prop": "imageinfo", "iiprop": "url|extmetadata|mime",
        "iiurlwidth": 800,
    })
    pages = sorted(r.json().get("query", {}).get("pages", {}).values(),
                   key=lambda p: p.get("index", 99))
    for p in pages:
        title = p.get("title", "")
        info = (p.get("imageinfo") or [{}])[0]
        if not info.get("thumburl") or BAD_FILE.search(title):
            continue
        if not info.get("mime", "").startswith("image/jpeg"):
            continue
        if not _title_ok(need, title):
            continue
        meta = info.get("extmetadata", {})
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip()
        lic = meta.get("LicenseShortName", {}).get("value", "")
        credit = "Фото: " + (html.unescape(artist)[:60] or "Wikimedia Commons")
        if lic:
            credit += f" ({lic})"
        return {"url": info["thumburl"], "title": title, "commons": True,
                "credit": credit,
                "page": info.get("descriptionurl") or
                "https://commons.wikimedia.org/wiki/" + title.replace(" ", "_")}
    return None


async def _find(client, name: str, name_en: str, city: str, wiki: str | None):
    need = _words(name, True) | _words(name_en, True)
    if not need:
        need = _words(name) | _words(name_en)
    tries = []
    if wiki:
        tries.append(lambda: _wiki_page(client, "ru", wiki))
    tries += [
        lambda: _wiki_search(client, "ru", f"{name} {city} гостиница", need),
        lambda: _wiki_search(client, "en", name_en or name, need),
        lambda: _commons_search(client, f"{name_en or name} {city}", need),
        lambda: _commons_search(client, name_en or name, need),
        lambda: _commons_search(client, f"{name} гостиница {city}", need),
    ]
    for t in tries:
        try:
            res = await t()
        except Exception as e:  # noqa: BLE001
            print(f"photos: {name}: {e!r}")
            continue
        if res:
            return res
    return None


async def fill_missing() -> int:
    """Ищет фото для отелей без фото. Возвращает число найденных."""
    found = 0
    async with aiosqlite.connect(config.db_path) as db:
        async with db.execute(
            "SELECT id, name, name_en, city, wiki_title FROM hotels "
            "WHERE photo = '' AND photo_tried = 0"
        ) as cur:
            todo = await cur.fetchall()
        if not todo:
            return 0
        async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
            for hid, name, name_en, city, wiki in todo:
                res = await _find(client, name, name_en, city, wiki)
                if res:
                    if res.get("commons"):
                        credit, page = res["credit"], res["page"]
                    else:
                        credit = "Фото: Википедия / Wikimedia Commons"
                        page = (f"https://{res['lang']}.wikipedia.org/wiki/"
                                + res["title"].replace(" ", "_"))
                    await db.execute(
                        "UPDATE hotels SET photo=?, photo_credit=?, source_url=?, "
                        "photo_tried=1 WHERE id=?", (res["url"], credit, page, hid))
                    found += 1
                else:
                    await db.execute("UPDATE hotels SET photo_tried=1 WHERE id=?", (hid,))
                await db.commit()
                await asyncio.sleep(0.15)
    print(f"photos: найдено фото: {found} из {len(todo)}", flush=True)
    return found
