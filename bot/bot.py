"""
Бот-лаунчер Telegram Mini App «HotelGenie».

Задача бота — дать пользователю кнопку, открывающую Mini App.
Вся логика находится в веб-интерфейсе и бэкенде.

Требование Telegram: адрес Mini App (WEBAPP_URL) обязан быть публичным
HTTPS. Запуск проекта целиком — `python run.py` (поднимает бэкенд,
туннель и этого бота).

Отдельный запуск:  python -m bot.bot
Если Telegram доступен только через прокси, задайте в .env
TELEGRAM_PROXY=http://127.0.0.1:порт (не нужно при VPN в режиме TUN).
"""
from __future__ import annotations

import asyncio
import html
import os
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

# Позволяем импортировать конфиг из соседнего пакета backend.
sys.path.append(str(Path(__file__).resolve().parent.parent))
from backend.config import config  # noqa: E402

dp = Dispatcher()

# Файл с актуальным адресом туннеля пишет run.py. Бот читает его на лету,
# поэтому перезапускать бота при смене адреса не нужно.
URL_FILE = Path(__file__).resolve().parent.parent / ".tunnel_url"


def current_url() -> str:
    """Актуальный https-адрес Mini App ('' если его ещё нет)."""
    candidates = []
    # На хостинге (Render и т. п.) адрес известен из переменных окружения.
    for var in ("PUBLIC_URL", "RENDER_EXTERNAL_URL"):
        value = os.getenv(var, "").strip().rstrip("/")
        if value:
            candidates.append(value)
    try:
        candidates.append(URL_FILE.read_text(encoding="utf-8").strip())
    except OSError:
        pass
    # Адрес из .env (WEBAPP_URL) используется только если запуск идёт без run.py
    # и задан явно: иначе там часто лежит устаревший адрес прошлого туннеля.
    if os.getenv("USE_ENV_WEBAPP_URL", "").strip().lower() in ("1", "true", "yes"):
        candidates.append(config.webapp_url.strip())
    for url in candidates:
        if url.startswith("https://") and "example.com" not in url:
            return url
    return ""


def build_keyboard() -> InlineKeyboardMarkup | None:
    """Кнопка под сообщением, открывающая Mini App (с актуальным адресом)."""
    url = current_url()
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🏨 Открыть HotelGenie", web_app=WebAppInfo(url=url))
    ]])


NOT_READY = (
    "⏳ Приложение ещё запускается (поднимается туннель). "
    "Подождите полминуты и отправьте /start снова."
)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Приветствие и кнопка запуска приложения."""
    name = html.escape(message.from_user.first_name) if message.from_user else "путешественник"
    keyboard = build_keyboard()
    if keyboard is None:
        await message.answer(NOT_READY)
        return
    await message.answer(
        f"Привет, {name}! 👋\n\n"
        "Я — <b>HotelGenie</b>, умный помощник по подбору отелей. "
        "Опишите желаемый отель обычными словами, например:\n"
        "<i>«отель в Сочи у моря на двоих до 6000 с бассейном»</i>, "
        "а искусственный интеллект подберёт подходящие варианты.\n\n"
        "Нажмите кнопку ниже, чтобы открыть приложение 👇",
        reply_markup=keyboard,
    )


@dp.message(F.text)
async def fallback(message: Message) -> None:
    """Подсказка для всех прочих сообщений."""
    keyboard = build_keyboard()
    if keyboard is None:
        await message.answer(NOT_READY)
        return
    await message.answer(
        "Весь поиск удобнее вести в приложении — нажмите кнопку "
        "«🏨 Открыть HotelGenie».",
        reply_markup=keyboard,
    )


async def connect(bot: Bot):
    """Ждёт связи с Telegram: не падает, если VPN включили позже."""
    warned = False
    while True:
        try:
            return await bot.get_me(request_timeout=15)
        except TelegramUnauthorizedError:
            raise SystemExit(
                "[bot] Telegram отклонил токен. Проверьте BOT_TOKEN в .env "
                "(новый токен выдаёт @BotFather)."
            )
        except (TelegramNetworkError, asyncio.TimeoutError):
            if not warned:
                print(
                    "[bot] Нет связи с Telegram (api.telegram.org недоступен).\n"
                    "      Включите VPN или задайте TELEGRAM_PROXY в .env.\n"
                    "      Повторяю попытки каждые 5 секунд…",
                    flush=True,
                )
                warned = True
            await asyncio.sleep(5)


async def watch_url(bot: Bot) -> None:
    """Следит за адресом туннеля и обновляет кнопку-меню при его смене."""
    last = None
    while True:
        url = current_url()
        if url and url != last:
            try:
                await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                    text="HotelGenie", web_app=WebAppInfo(url=url)))
                last = url
                print(f"[bot] Mini App: {url}", flush=True)
            except (TelegramNetworkError, asyncio.TimeoutError):
                pass  # повторим на следующей итерации
            except Exception as exc:  # noqa: BLE001
                last = url
                print(f"[bot] Не удалось обновить кнопку-меню: {exc}", flush=True)
        await asyncio.sleep(5)


async def main() -> None:
    if not config.bot_token:
        raise SystemExit(
            "[bot] Не задан BOT_TOKEN. Укажите токен бота в файле .env "
            "(получить можно у @BotFather)."
        )
    proxy = os.getenv("TELEGRAM_PROXY", "").strip()
    bot = Bot(
        token=config.bot_token,
        session=AiohttpSession(proxy=proxy) if proxy else AiohttpSession(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        print("[bot] Подключаюсь к Telegram…", flush=True)
        me = await connect(bot)
        print(f"[bot] Подключился как @{me.username}", flush=True)

        await bot.delete_webhook(drop_pending_updates=True)
        watcher = asyncio.create_task(watch_url(bot))
        print(f"[bot] Готово. Откройте https://t.me/{me.username} и нажмите /start. "
              "Стоп: Ctrl+C.", flush=True)
        try:
            await dp.start_polling(bot)
        finally:
            watcher.cancel()
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            print(exc.code)
        print("\n[bot] Бот остановлен.")
