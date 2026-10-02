"""
Точка входа бэкенда — веб-сервер на FastAPI.

Отвечает за:
  * REST API для фронтенда (поиск, избранное, история, города);
  * проверку подлинности пользователя через Telegram initData;
  * раздачу статических файлов Mini App (HTML/CSS/JS).

Запуск:  uvicorn backend.main:app --reload
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai_service, database
from .models import SearchCriteria
from .seed import seed
from .telegram_auth import validate_init_data
from .config import config

app = FastAPI(title="HotelGenie API", version="1.0.0")

# CORS разрешает фронтенду обращаться к API. Для учебного проекта
# открыт доступ со всех источников; в продакшене список ограничивают.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache(request, call_next):
    """Отключаем кэш: после обновления фронтенда Telegram берёт свежие файлы."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/photo"):
        response.headers["Cache-Control"] = "no-store"
    return response


_photo_cache: dict[str, tuple[bytes, str]] = {}


@app.get("/api/photo")
async def photo_proxy(u: str):
    """Отдаёт фото отеля через наш сервер (Википедия может быть недоступна клиенту)."""
    from urllib.parse import urlparse
    import httpx
    from fastapi import Response

    host = urlparse(u).hostname or ""
    if urlparse(u).scheme != "https" or host not in (
            "upload.wikimedia.org", "thumb.wikimedia.org"):
        raise HTTPException(400, "bad url")
    if u not in _photo_cache:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={
                "User-Agent": "HotelGenieBot/1.0 (educational Telegram Mini App)"}) as c:
            r = await c.get(u)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
            raise HTTPException(404, "photo unavailable")
        _photo_cache[u] = (r.content, r.headers["content-type"])
    body, ctype = _photo_cache[u]
    return Response(body, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=86400"})


# --- Схемы запросов/ответов (Pydantic) ---------------------------------

class SearchRequest(BaseModel):
    """Тело запроса умного поиска по тексту."""
    query: str
    init_data: str = ""


class FavoriteRequest(BaseModel):
    """Тело запроса на переключение избранного."""
    hotel_id: int
    init_data: str = ""


# --- Вспомогательные функции -------------------------------------------

def _resolve_user_id(init_data: str) -> int:
    """
    Возвращает идентификатор пользователя Telegram после проверки
    подписи. Если данных нет (открытие в браузере при разработке),
    используется гостевой идентификатор 0.
    """
    user = validate_init_data(init_data)
    if user and "id" in user:
        return int(user["id"])
    return 0  # гостевой режим (разработка/демо в браузере)


# --- Жизненный цикл приложения -----------------------------------------

def _public_url() -> str:
    """Публичный HTTPS-адрес сервиса (на Render задаётся автоматически)."""
    return (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")


def _webhook_secret() -> str:
    return hashlib.sha256(("hg:" + config.bot_token).encode()).hexdigest()[:40]


async def _setup_bot(url: str) -> None:
    """Режим хостинга: бот работает через вебхук внутри этого же сервера."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.types import MenuButtonWebApp, WebAppInfo

    bot = Bot(token=config.bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    app.state.bot = bot
    await bot.set_webhook(
        url=f"{url}/telegram/webhook",
        secret_token=_webhook_secret(),
        allowed_updates=["message", "callback_query"],
    )
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="HotelGenie", web_app=WebAppInfo(url=url))
    )
    print(f"[webhook] Бот подключён вебхуком: {url}/telegram/webhook", flush=True)


async def _photos_job() -> None:
    try:
        from .photos import fill_missing
        await fill_missing()
    except Exception as exc:  # noqa: BLE001
        print(f"[photos] {exc}", flush=True)


@app.get("/api/ai-status")
async def ai_status() -> dict:
    """Проверка ИИ: настроен ли ключ и отвечает ли провайдер."""
    if not config.ai_enabled:
        return {"enabled": False, "ok": False, "model": None}
    out = await ai_service._call_llm(
        [{"role": "user", "content": "Ответь одним словом: ок"}], max_tokens=5)
    return {"enabled": True, "ok": out is not None, "model": config.ai_model,
            "error": None if out is not None else ai_service.last_error}


@app.on_event("startup")
async def on_startup() -> None:
    """При старте создаём схему БД, наполняем её и (на хостинге) включаем бота."""
    await seed()
    asyncio.create_task(_photos_job())
    url = _public_url()
    if url.startswith("https://") and config.bot_token:
        try:
            await _setup_bot(url)
        except Exception as exc:  # noqa: BLE001 — сайт должен работать и без бота
            print(f"[webhook] Не удалось настроить бота: {exc}", flush=True)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    bot = getattr(app.state, "bot", None)
    if bot is not None:
        await bot.session.close()


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict:
    """Приём обновлений от Telegram (режим хостинга)."""
    bot = getattr(app.state, "bot", None)
    if bot is None:
        raise HTTPException(status_code=503, detail="Бот не настроен")
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != _webhook_secret():
        raise HTTPException(status_code=403, detail="Forbidden")
    from aiogram.types import Update
    from bot.bot import dp

    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


@app.get("/healthz")
async def healthz() -> dict:
    """Проверка живости для хостинга."""
    return {"ok": True}


# --- API: поиск ---------------------------------------------------------

@app.post("/api/search")
async def smart_search(req: SearchRequest) -> dict:
    """
    Умный поиск по свободному тексту.

    Текст пользователя разбирается ИИ в структурированные критерии,
    затем по ним ищутся отели и формируется рекомендация.
    """
    user_id = _resolve_user_id(req.init_data)
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Пустой запрос")

    criteria, used_ai = await ai_service.parse_query(query)
    hotels, exact_n = await database.search_smart(criteria)
    recommendation = await ai_service.generate_recommendation(
        query, criteria, hotels
    )
    cities = await database.list_cities()
    if criteria.city and not any(
            criteria.city.lower() in c.lower() for c in cities):
        recommendation = (f"В базе пока нет отелей в городе «{criteria.city}». "
                          "Показываю близкие по условиям варианты в других городах. "
                          + recommendation)
    elif hotels and exact_n < len(hotels):
        pre = (f"Точных совпадений: {exact_n}. " if exact_n else
               "Точных совпадений нет. ") + \
              "Остальные — самые близкие варианты, у каждого указано, чем он отличается. "
        recommendation = pre + recommendation
    await database.add_history(user_id, query)
    fav_ids = await database.get_favorite_ids(user_id)

    return {
        "criteria": criteria.to_dict(),
        "criteria_text": criteria.human_readable(),
        "used_ai": used_ai,
        "recommendation": recommendation,
        "hotels": [h.to_dict() for h in hotels],
        "exact_count": exact_n,
        "favorite_ids": list(fav_ids),
    }


@app.get("/api/hotels")
async def filter_hotels(
    init_data: str = "",
    city: Optional[str] = None,
    max_price: Optional[int] = None,
    min_stars: Optional[int] = None,
    near_sea: Optional[bool] = None,
) -> dict:
    """Поиск по явным фильтрам интерфейса (без ИИ)."""
    user_id = _resolve_user_id(init_data)
    criteria = SearchCriteria(
        city=city,
        max_price=max_price,
        min_stars=min_stars,
        near_sea=near_sea,
    )
    hotels = await database.search_hotels(criteria)
    fav_ids = await database.get_favorite_ids(user_id)
    return {
        "hotels": [h.to_dict() for h in hotels],
        "favorite_ids": list(fav_ids),
    }


@app.get("/api/hotels/{hotel_id}")
async def hotel_detail(hotel_id: int) -> dict:
    """Подробная карточка одного отеля."""
    hotel = await database.get_hotel(hotel_id)
    if not hotel:
        raise HTTPException(status_code=404, detail="Отель не найден")
    return hotel.to_dict()


@app.get("/api/cities")
async def cities() -> dict:
    """Список городов для выпадающего фильтра."""
    return {"cities": await database.list_cities()}


# --- API: избранное и история ------------------------------------------

@app.post("/api/favorites/toggle")
async def favorites_toggle(req: FavoriteRequest) -> dict:
    """Добавить/убрать отель из избранного."""
    user_id = _resolve_user_id(req.init_data)
    is_fav = await database.toggle_favorite(user_id, req.hotel_id)
    return {"is_favorite": is_fav}


@app.get("/api/favorites")
async def favorites_list(init_data: str = "") -> dict:
    """Список избранных отелей пользователя."""
    user_id = _resolve_user_id(init_data)
    hotels = await database.get_favorites(user_id)
    return {"hotels": [h.to_dict() for h in hotels]}


@app.get("/api/history")
async def history(init_data: str = "") -> dict:
    """Последние поисковые запросы пользователя."""
    user_id = _resolve_user_id(init_data)
    return {"queries": await database.get_history(user_id)}


# --- Раздача статики Mini App ------------------------------------------
# Монтируется последней, чтобы не перехватывать пути /api/*.
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
