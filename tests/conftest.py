from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import AppConfig, load_app_config
from app.models import Vacancy

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def app_config() -> AppConfig:
    return load_app_config(ROOT / "config" / "profiles.example.yaml")


@pytest.fixture
def trudvsem_json() -> dict:
    return json.loads((FIXTURES / "trudvsem_region74_sample.json").read_text(encoding="utf-8"))


def make_vacancy(**kw: object) -> Vacancy:
    base: dict[str, object] = {
        "source": "trudvsem",
        "external_id": "1",
        "title": "Менеджер по работе с клиентами",
        "company": "ООО «Ромашка»",
        "city": "Челябинск",
        "url": "https://example.org/1",
        "published_at": NOW,
        "work_format": "office",
        "experience": "none",
    }
    base.update(kw)
    return Vacancy.model_validate(base)
