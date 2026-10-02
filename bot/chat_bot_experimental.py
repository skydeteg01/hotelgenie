"""
Telegram-бот «HotelGenie» — полноценный интерфейс подбора отелей в чате.

Не требует ни туннеля, ни публичного HTTPS-адреса: достаточно запустить
бота — он сам работает с базой и ИИ-сервисом напрямую.

Возможности:
  * умный поиск обычным текстом («отель в Сочи у моря на двоих до 6000»);
  * точные фильтры (город, цена, звёзды, у моря) кнопками;
  * избранное и история запросов (привязаны к вашему Telegram-аккаунту);
  * по желанию — кнопка открытия Mini App (ENABLE_WEBAPP=1 и https-адрес).

Запуск:  python -m bot.bot
Если Telegram у вас доступен только через прокси, задайте в .env
TELEGRAM_PROXY=http://127.0.0.1:порт (или включите VPN в режиме TUN).
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonDefault,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

# Позволяем импортировать пакет backend из соседней папки.
sys.path.append(str(Path(__file__).resolve().parent.parent))
from backend import ai_service, database  # noqa: E402
from backend.config import config  # noqa: E402
from backend.models import Hotel, SearchCriteria  # noqa: E402
from backend.seed import seed  # noqa: E402

log = logging.getLogger("hotelgenie.bot")
dp = Dispatcher()

PAGE_SIZE = 5
PRICE_STEPS = [None, 3000, 5000, 8000, 12000]
STARS_STEPS = [None, 3, 4, 5]

# Состояние в памяти (для учебного проекта достаточно): последние
# результаты поиска и выбранные фильтры по каждому пользователю.
_results: dict[int, list[Hotel]] = {}
_shown: dict[int, int] = {}
_filters: dict[int, dict] = {}

BTN_FAV = "❤ Избранное"
BTN_HISTORY = "🕘 История"
BTN_FILTERS = "⚙ Фильтры"
BTN_HELP = "ℹ Помощь"


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


# --- Вспомогательные функции ---------------------------------------------

def webapp_enabled() -> bool:
    return (
        os.getenv("ENABLE_WEBAPP", "").strip().lower() in ("1", "true", "yes")
        and config.webapp_url.strip().startswith("https://")
    )


def main_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_FILTERS), KeyboardButton(text=BTN_FAV)],
        [KeyboardButton(text=BTN_HISTORY), KeyboardButton(text=BTN_HELP)],
    ]
    if webapp_enabled():
        rows.append([
            KeyboardButton(
                text="🏨 Открыть приложение",
                web_app=WebAppInfo(url=config.webapp_url.strip()),
            )
        ])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Опишите отель: Сочи, у моря, до 6000…",
    )


def hotel_card(h: Hotel) -> str:
    price = f"{h.price:,}".replace(",", " ")
    sea = " · у моря 🌊" if h.near_sea else ""
    amen = ", ".join(h.amenities)
    return (
        f"{esc(h.image)} <b>{esc(h.name)}</b>  {'★' * h.stars}\n"
        f"📍 {esc(h.city)}{sea}\n"
        f"💰 <b>{price} ₽</b>/ночь · ⭐ {h.rating} · 👥 до {h.capacity}\n"
        f"{esc(h.description)}\n"
        f"<i>✓ {esc(amen)}</i>"
    )


def fav_keyboard(hotel_id: int, is_fav: bool) -> InlineKeyboardMarkup:
    text = "♥ Убрать из избранного" if is_fav else "♡ В избранное"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=f"fav:{hotel_id}")]]
    )


async def send_page(bot: Bot, chat_id: int, user_id: int) -> None:
    """Отправляет очередную порцию карточек из последних результатов."""
    hotels = _results.get(user_id, [])
    start = _shown.get(user_id, 0)
    chunk = hotels[start:start + PAGE_SIZE]
    favs = await database.get_favorite_ids(user_id)
    for h in chunk:
        await bot.send_message(
            chat_id, hotel_card(h), reply_markup=fav_keyboard(h.id, h.id in favs)
        )
    _shown[user_id] = start + len(chunk)
    left = len(hotels) - _shown[user_id]
    if left > 0:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"➕ Показать ещё ({left})", callback_data="more")
        ]])
        await bot.send_message(
            chat_id, f"Показано {_shown[user_id]} из {len(hotels)}", reply_markup=kb
        )


async def run_search(message: Message, user_id: int, query: str) -> None:
    """Умный поиск по свободному тексту."""
    status = await message.answer("🔎 Разбираю запрос…")
    try:
        criteria, _used_ai = await ai_service.parse_query(query)
        hotels = await database.search_hotels(criteria, limit=30)
        recommendation = await ai_service.generate_recommendation(
            query, criteria, hotels
        )
        await database.add_history(user_id, query)
    except Exception:  # noqa: BLE001
        log.exception("Ошибка поиска")
        await status.edit_text("⚠ Не удалось выполнить поиск. Попробуйте ещё раз.")
        return

    _results[user_id] = hotels
    _shown[user_id] = 0
    await status.edit_text(
        f"<b>Понял так:</b> {esc(criteria.human_readable())}\n\n{esc(recommendation)}"
    )
    await send_page(message.bot, message.chat.id, user_id)


# --- Команды и кнопки меню ----------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    name = esc(message.from_user.first_name) if message.from_user else "путешественник"
    await message.answer(
        f"Привет, {name}! 👋\n\n"
        "Я — <b>HotelGenie</b>, помогу подобрать отель. Просто напишите, "
        "что вам нужно, например:\n"
        "<i>«отель в Сочи у моря на двоих до 6000 с бассейном»</i>\n\n"
        "Или используйте кнопки внизу: фильтры, избранное, история.",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("help"))
@dp.message(F.text == BTN_HELP)
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Как пользоваться</b>\n"
        "• Напишите запрос обычными словами — город, бюджет, число гостей, "
        "звёзды, удобства, «у моря».\n"
        "• ⚙ Фильтры — выбор параметров кнопками.\n"
        "• ♡ под карточкой — добавить отель в избранное.\n"
        "• ❤ Избранное и 🕘 История — ваши сохранённые отели и запросы.\n\n"
        "Примеры: <i>5-звёздочный отель в Москве</i>, "
        "<i>недорого у моря в Ялте</i>, <i>спа-отель на Байкале для двоих</i>.",
        reply_markup=main_keyboard(),
    )


@dp.message(Command("favorites"))
@dp.message(F.text == BTN_FAV)
async def cmd_favorites(message: Message) -> None:
    user_id = message.from_user.id
    hotels = await database.get_favorites(user_id)
    if not hotels:
        await message.answer("Пока пусто. Нажимайте «♡ В избранное» под карточками отелей.")
        return
    await message.answer(f"❤ <b>Избранное</b> ({len(hotels)}):")
    for h in hotels[:15]:
        await message.answer(hotel_card(h), reply_markup=fav_keyboard(h.id, True))


@dp.message(Command("history"))
@dp.message(F.text == BTN_HISTORY)
async def cmd_history(message: Message) -> None:
    queries = await database.get_history(message.from_user.id)
    if not queries:
        await message.answer("История пуста — сделайте первый запрос.")
        return
    rows = [
        [InlineKeyboardButton(
            text=(q if len(q) <= 55 else q[:53] + "…"), callback_data=f"hist:{i}")]
        for i, q in enumerate(queries)
    ]
    await message.answer(
        "🕘 <b>Недавние запросы</b> — нажмите, чтобы повторить:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# --- Фильтры -------------------------------------------------------------

def _flt(user_id: int) -> dict:
    return _filters.setdefault(
        user_id, {"city": None, "price": None, "stars": None, "sea": False}
    )


def filters_keyboard(user_id: int) -> InlineKeyboardMarkup:
    f = _flt(user_id)
    price = f"{f['price']} ₽" if f["price"] else "любая"
    stars = f"{f['stars']}★" if f["stars"] else "любые"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📍 Город: {f['city'] or 'любой'}", callback_data="flt:city")],
        [InlineKeyboardButton(text=f"💰 Цена до: {price}", callback_data="flt:price")],
        [InlineKeyboardButton(text=f"⭐ Звёзд от: {stars}", callback_data="flt:stars")],
        [InlineKeyboardButton(
            text=f"🌊 У моря: {'да ✅' if f['sea'] else 'нет'}", callback_data="flt:sea")],
        [
            InlineKeyboardButton(text="🔎 Показать", callback_data="flt:go"),
            InlineKeyboardButton(text="♻ Сброс", callback_data="flt:reset"),
        ],
    ])


FILTERS_TEXT = "⚙ <b>Точные фильтры</b>\nНажимайте на параметры, затем «Показать»."


@dp.message(Command("filters"))
@dp.message(F.text == BTN_FILTERS)
async def cmd_filters(message: Message) -> None:
    await message.answer(FILTERS_TEXT, reply_markup=filters_keyboard(message.from_user.id))


async def _safe_edit(message: Message, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass  # «message is not modified» — безвредно


@dp.callback_query(F.data.startswith("flt:"))
async def cb_filters(cb: CallbackQuery) -> None:
    user_id = cb.from_user.id
    f = _flt(user_id)
    parts = cb.data.split(":")
    action = parts[1]

    if action == "city" and len(parts) == 2:
        cities = await database.list_cities()
        rows = [[InlineKeyboardButton(text="Любой город", callback_data="flt:city:-1")]]
        pair: list[InlineKeyboardButton] = []
        for i, c in enumerate(cities):
            pair.append(InlineKeyboardButton(text=c, callback_data=f"flt:city:{i}"))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        await _safe_edit(cb.message, "📍 Выберите город:", InlineKeyboardMarkup(inline_keyboard=rows))
        await cb.answer()
        return

    if action == "city":
        idx = int(parts[2])
        cities = await database.list_cities()
        f["city"] = cities[idx] if 0 <= idx < len(cities) else None
    elif action == "price":
        i = PRICE_STEPS.index(f["price"]) if f["price"] in PRICE_STEPS else 0
        f["price"] = PRICE_STEPS[(i + 1) % len(PRICE_STEPS)]
    elif action == "stars":
        i = STARS_STEPS.index(f["stars"]) if f["stars"] in STARS_STEPS else 0
        f["stars"] = STARS_STEPS[(i + 1) % len(STARS_STEPS)]
    elif action == "sea":
        f["sea"] = not f["sea"]
    elif action == "reset":
        _filters[user_id] = {"city": None, "price": None, "stars": None, "sea": False}
    elif action == "go":
        criteria = SearchCriteria(
            city=f["city"], max_price=f["price"], min_stars=f["stars"],
            near_sea=True if f["sea"] else None,
        )
        hotels = await database.search_hotels(criteria, limit=30)
        _results[user_id] = hotels
        _shown[user_id] = 0
        await cb.answer()
        if not hotels:
            await cb.message.answer("По этим фильтрам ничего не нашлось. Смягчите условия.")
            return
        await cb.message.answer(f"Найдено отелей: <b>{len(hotels)}</b> ({esc(criteria.human_readable())})")
        await send_page(cb.bot, cb.message.chat.id, user_id)
        return

    await _safe_edit(cb.message, FILTERS_TEXT, filters_keyboard(user_id))
    await cb.answer()


# --- Кнопки под карточками, «ещё», история -------------------------------

@dp.callback_query(F.data.startswith("fav:"))
async def cb_favorite(cb: CallbackQuery) -> None:
    hotel_id = int(cb.data.split(":")[1])
    if not await database.get_hotel(hotel_id):
        await cb.answer("Отель не найден", show_alert=True)
        return
    is_fav = await database.toggle_favorite(cb.from_user.id, hotel_id)
    try:
        await cb.message.edit_reply_markup(reply_markup=fav_keyboard(hotel_id, is_fav))
    except TelegramBadRequest:
        pass
    await cb.answer("Добавлено в избранное ❤" if is_fav else "Убрано из избранного")


@dp.callback_query(F.data == "more")
async def cb_more(cb: CallbackQuery) -> None:
    await cb.answer()
    try:
        await cb.message.delete()
    except TelegramBadRequest:
        pass
    await send_page(cb.bot, cb.message.chat.id, cb.from_user.id)


@dp.callback_query(F.data.startswith("hist:"))
async def cb_history(cb: CallbackQuery) -> None:
    queries = await database.get_history(cb.from_user.id)
    idx = int(cb.data.split(":")[1])
    await cb.answer()
    if 0 <= idx < len(queries):
        await run_search(cb.message, cb.from_user.id, queries[idx])


# --- Обычный текст = умный поиск -----------------------------------------

@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    query = message.text.strip()
    if len(query) > 300:
        await message.answer("Запрос слишком длинный — опишите короче (до 300 символов).")
        return
    await run_search(message, message.from_user.id, query)


@dp.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    await cmd_help(message)


# --- Запуск ---------------------------------------------------------------

async def connect(bot: Bot):
    """Ждёт связи с Telegram: не падает, если VPN включили позже."""
    warned = False
    while True:
        try:
            return await bot.get_me(request_timeout=15)
        except TelegramUnauthorizedError:
            raise SystemExit(
                "[bot] Telegram отклонил токен. Проверьте BOT_TOKEN в .env "
                "(новый токен можно получить у @BotFather)."
            )
        except (TelegramNetworkError, asyncio.TimeoutError):
            if not warned:
                print(
                    "[bot] Нет связи с Telegram (api.telegram.org недоступен).\n"
                    "      Включите VPN (режим TUN) или задайте TELEGRAM_PROXY в .env.\n"
                    "      Повторяю попытки каждые 5 секунд…",
                    flush=True,
                )
                warned = True
            await asyncio.sleep(5)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not config.bot_token:
        raise SystemExit(
            "[bot] Не задан BOT_TOKEN. Укажите токен в файле .env "
            "(получить можно у @BotFather)."
        )

    await seed()  # создаёт таблицы и наполняет базу, если она пустая

    proxy = os.getenv("TELEGRAM_PROXY", "").strip()
    session = AiohttpSession(proxy=proxy) if proxy else AiohttpSession()
    bot = Bot(
        token=config.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        print("[bot] Подключаюсь к Telegram…", flush=True)
        me = await connect(bot)
        print(f"[bot] Подключился как @{me.username}", flush=True)

        await bot.set_my_commands([
            BotCommand(command="start", description="Начать"),
            BotCommand(command="filters", description="Точные фильтры"),
            BotCommand(command="favorites", description="Избранное"),
            BotCommand(command="history", description="История запросов"),
            BotCommand(command="help", description="Помощь"),
        ])
        # Кнопка-меню: Mini App (если включён) либо стандартное меню команд.
        # Это также убирает устаревшую кнопку со старым адресом туннеля.
        try:
            if webapp_enabled():
                await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                    text="HotelGenie", web_app=WebAppInfo(url=config.webapp_url.strip())))
            else:
                await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
        except Exception as exc:  # noqa: BLE001
            print(f"[bot] Не удалось обновить кнопку-меню: {exc}", flush=True)

        await bot.delete_webhook(drop_pending_updates=True)
        print(f"[bot] Готово! Откройте https://t.me/{me.username} и нажмите /start. "
              "Остановка: Ctrl+C.", flush=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            print(exc.code)
        print("\n[bot] Бот остановлен.")
