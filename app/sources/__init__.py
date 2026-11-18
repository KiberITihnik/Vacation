"""Реестр источников. Чтобы добавить источник: файл-адаптер + строка в REGISTRY."""

from __future__ import annotations

from collections.abc import Callable

from app.config import AppConfig
from app.sources.base import Source
from app.sources.telegram_channels import TelegramChannelsSource
from app.sources.trudvsem import TrudvsemSource

REGISTRY: dict[str, Callable[[AppConfig], Source]] = {
    "trudvsem": TrudvsemSource,
    "telegram_channels": TelegramChannelsSource,
}


def build_sources(app: AppConfig) -> dict[str, Source]:
    return {name: factory(app) for name, factory in REGISTRY.items() if app.source(name).enabled}
