"""
Сервис искусственного интеллекта.

Отвечает за две задачи:
  1. Понимание естественного языка (NLU) — превращение свободного
     текстового запроса пользователя в структурированные критерии
     поиска (объект SearchCriteria).
  2. Генерация персональной рекомендации — краткий связный текст,
     объясняющий, почему предложенные отели подходят запросу.

Архитектура устойчива к отсутствию нейросети: если API-ключ не задан
или запрос к модели завершился ошибкой, автоматически используется
резервный детерминированный разбор на основе правил и словарей
(rule-based fallback). Благодаря этому приложение остаётся
работоспособным в любой среде, что важно для демонстрации.
"""
from __future__ import annotations

import json
import asyncio
import re
from typing import Optional

import httpx

from .config import config
from .models import Hotel, SearchCriteria


# Системная инструкция для языковой модели. Чётко задаёт формат
# ответа (только JSON), чтобы результат можно было надёжно разобрать.
PARSE_SYSTEM_PROMPT = """Ты — парсер запросов для системы поиска отелей.
Преобразуй запрос пользователя в JSON строго следующего вида (без пояснений,
без markdown, только сам объект):
{
  "city": строка или null,        // город или курорт
  "max_price": число или null,    // максимальная цена за ночь в рублях
  "min_price": число или null,    // минимальная цена за ночь в рублях
  "guests": число или null,       // количество гостей
  "min_stars": число или null,    // минимальное число звёзд 1-5
  "min_rating": число или null,   // минимальный рейтинг 0-10
  "amenities": [строки],          // удобства из списка: бассейн, wi-fi,
                                  // завтрак, спа, парковка, кондиционер,
                                  // ресторан, баня, детская комната
  "near_sea": true/false/null     // нужен ли отель у моря
}
Если параметр не упомянут — ставь null (для amenities — пустой список).
Нормализуй город к именительному падежу. Доступные города: Москва, Санкт-Петербург, Сочи, Казань, Калининград, Иркутск, Ялта."""


# --- Словари для резервного разбора ------------------------------------

# Сопоставление словоформ городов с каноническим названием.
CITY_ALIASES: dict[str, str] = {
    "сочи": "Сочи", "адлер": "Сочи",
    "москв": "Москва",
    "питер": "Санкт-Петербург", "петербург": "Санкт-Петербург",
    "спб": "Санкт-Петербург",
    "калининград": "Калининград",
    "иркутск": "Иркутск", "байкал": "Иркутск",
    "казан": "Казань",
    "ялт": "Ялта", "крым": "Ялта",
}

# Ключевые слова удобств.
AMENITY_KEYWORDS: dict[str, str] = {
    "бассейн": "бассейн",
    "wi-fi": "wi-fi", "wifi": "wi-fi", "вай-фай": "wi-fi", "интернет": "wi-fi",
    "завтрак": "завтрак",
    "спа": "спа", "spa": "спа",
    "парковк": "парковка", "паркинг": "парковка",
    "кондиционер": "кондиционер",
    "ресторан": "ресторан",
    "баня": "баня", "сауна": "баня",
    "детьми": "детская комната", "детск": "детская комната", "ребен": "детская комната",
}


# Короткие слова ищем целиком, чтобы «спа» не срабатывало на «спальня».
_EXACT_WORDS = {"спа", "spa", "баня"}


def _kw_in(text: str, keyword: str) -> bool:
    """Ищет ключевое слово с начала слова (и целиком для коротких)."""
    pattern = r"(?<![а-яёa-z])" + re.escape(keyword)
    if keyword in _EXACT_WORDS:
        pattern += r"(?![а-яёa-z])"
    return re.search(pattern, text) is not None


def rule_based_parse(text: str) -> SearchCriteria:
    """
    Резервный разбор запроса без нейросети.

    Применяет регулярные выражения и словари ключевых слов.
    Работает мгновенно и без внешних зависимостей; используется,
    когда ИИ недоступен.
    """
    low = text.lower()
    # Нормализация чисел: «15 000» -> «15000», «6 тыс» / «6к» -> «6000».
    low = re.sub(r"(?<=\d)[ \u00a0](?=\d{3}(?!\d))", "", low)
    low = re.sub(
        r"(\d+)\s*тыс\w*", lambda m: str(int(m.group(1)) * 1000), low
    )
    low = re.sub(
        r"(\d+)к(?![а-яёa-z])", lambda m: str(int(m.group(1)) * 1000), low
    )
    criteria = SearchCriteria()

    # Город — ищем по словарю синонимов.
    for alias, canonical in CITY_ALIASES.items():
        if alias in low:
            criteria.city = canonical
            break

    # Максимальная цена: «до 5000», «бюджет 5000», «за 5000».
    price_match = re.search(
        r"(?<![а-яёa-z])(?:до|бюджет|за|максимум)(?![а-яёa-z])\D{0,8}(\d{3,6})", low
    )
    if price_match:
        criteria.max_price = int(price_match.group(1))
    # Минимальная цена: «от 3000».
    min_price_match = re.search(r"от\s+(\d{3,6})\s*(?:руб|₽|р\b)", low)
    if min_price_match:
        criteria.min_price = int(min_price_match.group(1))

    # Количество гостей.
    guests_match = re.search(r"(\d+)\s*(?:гост|человек|чел|взросл)", low)
    if guests_match:
        criteria.guests = int(guests_match.group(1))
    elif "двоих" in low or "пары" in low or "вдвоём" in low or "вдвоем" in low:
        criteria.guests = 2
    elif "одного" in low or "одиночн" in low:
        criteria.guests = 1
    elif "семь" in low or "семей" in low:
        criteria.guests = 4

    # Звёздность: «5 звёзд», «5-звёздочный», «пятизвёздочный».
    stars_match = re.search(r"(\d)\s*[-–]?\s*(?:звезд|звёзд)", low)
    if stars_match:
        criteria.min_stars = int(stars_match.group(1))
    elif "пятизвезд" in low or "пятизвёзд" in low or "люкс" in low:
        criteria.min_stars = 5
    elif "четырёхзвезд" in low or "четырехзвезд" in low:
        criteria.min_stars = 4

    # Близость к морю.
    if any(w in low for w in ["море", "моря", "пляж", "берег"]):
        criteria.near_sea = True

    # Удобства.
    found: list[str] = []
    for keyword, amenity in AMENITY_KEYWORDS.items():
        if _kw_in(low, keyword) and amenity not in found:
            found.append(amenity)
    criteria.amenities = found

    return criteria


last_error: str = ""


async def _call_llm(messages: list[dict], max_tokens: int = 400) -> Optional[str]:
    """
    Низкоуровневый вызов OpenAI-совместимого Chat Completions API.
    Возвращает текст ответа модели или None при любой ошибке.
    """
    global last_error
    if not config.ai_enabled:
        return None

    url = f"{config.ai_base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {config.ai_api_key}"}
    payload = {
        "model": config.ai_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            for attempt in range(3):
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 429 and attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                break
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001 — логируем и переходим к fallback
        last_error = last_error or repr(exc)
        print(f"[ai_service] Ошибка обращения к ИИ: {exc} {last_error}")
        return None


def _extract_json(text: str) -> Optional[dict]:
    """Извлекает JSON-объект из ответа модели (на случай лишнего текста)."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\{.*\}", text or "", re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


async def parse_query(text: str) -> tuple[SearchCriteria, bool]:
    """
    Главная точка входа NLU.

    Пытается разобрать запрос с помощью ИИ; при неудаче использует
    резервный алгоритм. Возвращает кортеж (критерии, использован_ли_ИИ).
    """
    raw = await _call_llm(
        [
            {"role": "system", "content": PARSE_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        max_tokens=300,
    )
    parsed = _extract_json(raw) if raw else None
    if isinstance(parsed, dict) and parsed:
        try:
            return SearchCriteria.from_dict(parsed), True
        except Exception as exc:  # noqa: BLE001 — неожиданный формат ответа ИИ
            print(f"[ai_service] Некорректный ответ ИИ: {exc}")

    # ИИ недоступен или вернул некорректный ответ — резервный разбор.
    return rule_based_parse(text), False


async def generate_recommendation(
    query: str, criteria: SearchCriteria, hotels: list[Hotel]
) -> str:
    """
    Формирует короткий рекомендательный комментарий к выдаче.

    При наличии ИИ генерирует естественный текст; иначе собирает
    осмысленное сообщение по шаблону на основе найденных отелей.
    """
    if not hotels:
        return (
            "По вашему запросу ничего не нашлось. "
            "Попробуйте смягчить условия — например, увеличить бюджет "
            "или убрать часть требований."
        )

    if config.ai_enabled:
        top = hotels[:3]
        hotels_brief = "; ".join(
            f"{h.name} ({h.city}, {h.price}₽, {h.stars}★, рейтинг {h.rating})"
            for h in top
        )
        raw = await _call_llm(
            [
                {
                    "role": "system",
                    "content": (
                        "Ты — дружелюбный консультант по подбору отелей. "
                        "Дай короткую (2-3 предложения) рекомендацию на русском "
                        "языке, объясни выбор. Не используй markdown."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Запрос: «{query}». Найденные отели: {hotels_brief}. "
                        "Посоветуй, на что обратить внимание."
                    ),
                },
            ],
            max_tokens=200,
        )
        if raw:
            return raw.strip()

    # Шаблонная рекомендация (fallback).
    best = hotels[0]
    cheapest = min(hotels, key=lambda h: h.price)
    text = (
        f"Нашлось вариантов: {len(hotels)}. "
        f"Лучший по рейтингу — «{best.name}» ({best.rating}/10). "
    )
    if cheapest.id != best.id:
        text += f"Самый доступный — «{cheapest.name}» за {cheapest.price} ₽/ночь."
    return text
