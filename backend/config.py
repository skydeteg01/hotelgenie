"""
Модуль конфигурации приложения.

Все секреты и изменяемые параметры считываются из переменных
окружения (файл .env). Это стандартная практика безопасной
разработки: токены и ключи не хранятся в исходном коде.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    """Неизменяемый контейнер настроек приложения."""

    # Токен Telegram-бота (нужен для проверки подписи initData
    # и для запуска бота-лаунчера).
    bot_token: str

    # Параметры языковой модели (любой OpenAI-совместимый эндпоинт:
    # OpenAI, локальный Ollama, прокси к GigaChat/YandexGPT и т. п.).
    ai_api_key: str
    ai_base_url: str
    ai_model: str

    # Путь к файлу базы данных SQLite.
    db_path: str

    # Публичный HTTPS-адрес, по которому открывается Mini App.
    # Требуется Telegram для кнопки WebApp.
    webapp_url: str

    @property
    def ai_enabled(self) -> bool:
        """ИИ активен только при наличии API-ключа."""
        return bool(self.ai_api_key)


def load_config() -> Config:
    return Config(
        bot_token=os.getenv("BOT_TOKEN", ""),
        ai_api_key=os.getenv("AI_API_KEY", ""),
        ai_base_url=os.getenv("AI_BASE_URL", "https://api.openai.com/v1"),
        ai_model=os.getenv("AI_MODEL", "gpt-4o-mini"),
        db_path=os.getenv("DB_PATH", "hotels.db"),
        webapp_url=os.getenv("WEBAPP_URL", "http://localhost:8000"),
    )


config = load_config()
