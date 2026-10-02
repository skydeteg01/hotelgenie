"""
Модели предметной области (доменные модели).

Описывают структуры данных, которыми оперирует приложение:
критерии поиска, извлечённые из запроса пользователя, и карточку отеля.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import quote


@dataclass
class SearchCriteria:
    """
    Критерии поиска отеля, извлечённые из естественно-языкового запроса.

    Все поля необязательны: пользователь может указать только часть
    параметров, остальные не участвуют в фильтрации.
    """

    city: Optional[str] = None              # Город / курорт
    max_price: Optional[int] = None         # Максимальная цена за ночь, руб.
    min_price: Optional[int] = None         # Минимальная цена за ночь, руб.
    guests: Optional[int] = None            # Количество гостей
    min_stars: Optional[int] = None         # Минимальная «звёздность» (1–5)
    min_rating: Optional[float] = None      # Минимальный рейтинг (0–10)
    amenities: list[str] = field(default_factory=list)  # Удобства
    near_sea: Optional[bool] = None         # Близость к морю

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SearchCriteria":
        """Создаёт критерии из словаря (например, из ответа ИИ)."""
        return cls(
            city=data.get("city") if isinstance(data.get("city"), str) else None,
            max_price=cls._as_int(data.get("max_price")),
            min_price=cls._as_int(data.get("min_price")),
            guests=cls._as_int(data.get("guests")),
            min_stars=cls._as_int(data.get("min_stars")),
            min_rating=cls._as_float(data.get("min_rating")),
            amenities=cls._as_str_list(data.get("amenities")),
            near_sea=data.get("near_sea") if isinstance(data.get("near_sea"), bool) else None,
        )

    @staticmethod
    def _as_str_list(value) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]

    @staticmethod
    def _as_int(value) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_float(value) -> Optional[float]:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def human_readable(self) -> str:
        """Текстовое описание критериев для интерфейса."""
        parts: list[str] = []
        if self.city:
            parts.append(self.city)
        if self.guests:
            parts.append(f"{self.guests} гостей")
        if self.max_price:
            parts.append(f"до {self.max_price} ₽")
        if self.min_stars:
            parts.append(f"от {self.min_stars}★")
        if self.near_sea:
            parts.append("у моря")
        parts.extend(self.amenities)
        return " · ".join(parts) if parts else "любые отели"


@dataclass
class Hotel:
    """Карточка отеля (запись из базы данных)."""

    id: int
    name: str
    city: str
    price: int            # Цена за ночь, руб.
    stars: int            # Категория, «звёзды» (1–5)
    rating: float         # Пользовательский рейтинг (0–10)
    capacity: int         # Вместимость номера, чел.
    amenities: list[str]  # Список удобств
    near_sea: bool        # Расположен ли у моря
    description: str      # Краткое описание
    image: str            # Эмодзи-иллюстрация (запасной вариант)
    name_en: str = ""
    photo: str = ""         # URL реального фото (Википедия / Commons)
    photo_credit: str = ""  # Подпись источника фото
    source_url: str = ""    # Страница-источник фото

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "city": self.city,
            "price": self.price,
            "stars": self.stars,
            "rating": self.rating,
            "capacity": self.capacity,
            "amenities": self.amenities,
            "near_sea": self.near_sea,
            "description": self.description,
            "image": self.image,
            "photo": ("/api/photo?u=" + quote(self.photo, safe="")) if self.photo else "",
            "photo_credit": self.photo_credit,
            "source_url": self.source_url,
            "map_url": "https://yandex.ru/maps/?text="
            + quote(f"{self.name} {self.city}"),
        }
