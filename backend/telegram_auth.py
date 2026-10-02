"""
Проверка подлинности данных Telegram Mini App.

Когда Telegram открывает Mini App, он передаёт во фронтенд строку
initData, содержащую сведения о пользователе и криптографическую
подпись (hash). Бэкенд обязан проверить эту подпись, иначе
злоумышленник мог бы подделать идентификатор пользователя и получить
доступ к чужим данным (избранному, истории).

Алгоритм проверки описан в официальной документации Telegram:
  1. Из initData удаляется параметр hash, остальные пары сортируются
     и склеиваются в data_check_string.
  2. Вычисляется секретный ключ: HMAC-SHA256 от строки "WebAppData"
     с ключом = bot_token.
  3. Сравнивается HMAC-SHA256(data_check_string, secret_key) с hash.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Optional
from urllib.parse import parse_qsl

from .config import config


def validate_init_data(init_data: str) -> Optional[dict]:
    """
    Проверяет строку initData и возвращает данные пользователя (dict),
    если подпись верна, иначе None.

    В учебной/отладочной среде, когда токен бота не задан,
    проверка пропускается и данные принимаются «как есть» — это
    позволяет открывать приложение в обычном браузере для тестирования.
    """
    if not init_data:
        return None

    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=False))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)

    # Отладочный режим без токена: доверяем данным (только для разработки!).
    if not config.bot_token:
        return _extract_user(parsed)

    if not received_hash:
        return None

    # Шаг 1: формируем строку проверки.
    data_check_string = "\n".join(
        f"{key}={parsed[key]}" for key in sorted(parsed)
    )

    # Шаг 2: вычисляем секретный ключ.
    secret_key = hmac.new(
        b"WebAppData", config.bot_token.encode(), hashlib.sha256
    ).digest()

    # Шаг 3: вычисляем и сравниваем подпись.
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        return None

    return _extract_user(parsed)


def _extract_user(parsed: dict) -> Optional[dict]:
    """Извлекает объект пользователя из разобранных данных."""
    user_raw = parsed.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except json.JSONDecodeError:
        return None
