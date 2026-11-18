"""Общий интерфейс источника. Новый источник = один файл с классом + запись в REGISTRY."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.config import ProfileConfig
from app.models import Vacancy


@runtime_checkable
class Source(Protocol):
    name: str

    async def fetch(self, profile: ProfileConfig) -> list[Vacancy]: ...

    async def aclose(self) -> None: ...


class SourceError(RuntimeError):
    pass
