"""Загрузка секретов из .env и критериев из config/profiles.yaml."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Experience = Literal["none", "1-3", "3-6", "6+", "unknown"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: SecretStr = SecretStr("")
    admin_chat_id: int | None = None
    imap_host: str = ""
    imap_user: str = ""
    imap_password: SecretStr = SecretStr("")
    superjob_app_key: SecretStr = SecretStr("")
    tz: str = "Asia/Yekaterinburg"
    llm_api_key: SecretStr = SecretStr("")
    database_url: str = "sqlite:///data/jobs.db"
    log_dir: str = "logs"
    profiles_path: str = "config/profiles.yaml"

    @field_validator("admin_chat_id", mode="before")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        return None if v in ("", None) else v


class ScheduleConfig(BaseModel):
    times: list[str] = ["09:00", "18:00"]
    max_per_digest: int = 15
    send_empty_notice: bool = True
    summary_if_more_than: int = 5
    now_cooldown_minutes: int = 30


class HttpConfig(BaseModel):
    user_agent: str = "JobAlertsBot/0.1 (personal non-commercial job digest)"
    timeout_seconds: float = 20
    retries: int = 3
    delay_seconds: float = 1.0


class SourceConfig(BaseModel):
    model_config = {"extra": "allow"}
    enabled: bool = True

    def opt(self, key: str, default: Any = None) -> Any:
        return (self.model_extra or {}).get(key, default)


class MoneyConfig(BaseModel):
    usd_rate: float = 90
    eur_rate: float = 100
    hours_per_month: int = 160


class Locations(BaseModel):
    cities: list[str] = []
    allow_remote: bool = True
    allow_office: bool = True


class ProfileConfig(BaseModel):
    id: str = ""
    chat_id_env: str
    title: str = "Подборка вакансий"
    locations: Locations = Locations()
    trudvsem_regions: list[str] = []
    experience: list[Experience] = ["none", "unknown"]
    warn_experience: list[Experience] = []
    min_salary: int = 0
    include_keywords: list[str] = []
    search_queries: list[str] = []
    any_job_if_salary_from: int | None = None
    exclude_keywords: list[str] = []
    sort: Literal["salary_desc", "relevance"] = "relevance"
    show_freelance_block: bool = False
    sources: list[str] = []
    about: str = ""

    @property
    def queries(self) -> list[str]:
        return self.search_queries or self.include_keywords

    @property
    def chat_id(self) -> int | None:
        raw = os.environ.get(self.chat_id_env, "").strip()
        return int(raw) if raw.lstrip("-").isdigit() else None


class AntiscamConfig(BaseModel):
    hide: list[str] = []
    warn: list[str] = []


class Weights(BaseModel):
    salary: float = 1.0
    freshness: float = 0.3
    keywords: float = 0.5
    remote: float = 0.0


class ScoringConfig(BaseModel):
    salary_cap: int = 200_000
    freshness_days: int = 7
    warn_penalty: float = 0.5
    dislike_penalty: float = 0.3
    weights: dict[str, Weights] = {}

    def for_profile(self, profile_id: str) -> Weights:
        return self.weights.get(profile_id, Weights())


class AppConfig(BaseModel):
    schedule: ScheduleConfig = ScheduleConfig()
    http: HttpConfig = HttpConfig()
    sources: dict[str, SourceConfig] = {}
    money: MoneyConfig = MoneyConfig()
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)
    antiscam: AntiscamConfig = AntiscamConfig()
    scoring: ScoringConfig = ScoringConfig()

    @field_validator("profiles")
    @classmethod
    def _set_ids(cls, v: dict[str, ProfileConfig]) -> dict[str, ProfileConfig]:
        for key, p in v.items():
            p.id = key
        return v

    def source(self, name: str) -> SourceConfig:
        return self.sources.get(name, SourceConfig(enabled=False))


def load_app_config(path: str | Path) -> AppConfig:
    p = Path(path)
    if not p.exists():
        example = p.with_name("profiles.example.yaml")
        if example.exists():
            p = example
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig.model_validate(data)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(override=False)  # чтобы chat_id_env читались из os.environ
    return Settings()


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return load_app_config(get_settings().profiles_path)
