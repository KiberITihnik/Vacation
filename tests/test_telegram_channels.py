from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from app.config import HttpConfig
from app.processing.antiscam import AntiScam
from app.processing.filters import ProfileFilter, phrase_in
from app.sources.http import PoliteClient
from app.sources.telegram_channels import (
    TelegramChannelsSource,
    extract_title,
    is_vacancy_post,
    parse_channel_page,
    post_to_vacancy,
)

HTML = (Path(__file__).parent / "fixtures" / "tg_channel_sample.html").read_text(encoding="utf-8")


def test_parse_channel_page():
    posts = parse_channel_page(HTML, "test_jobs")
    assert [p.post_id for p in posts] == [101, 102, 103, 104, 90, 105]
    p = posts[0]
    assert p.url == "https://t.me/test_jobs/101"
    assert p.published_at == datetime(2026, 11, 18, 7, 15, tzinfo=UTC)
    assert "Компания: ООО «Текстовик»" in p.text.splitlines()
    assert "https://t.me/hr_textovik" in p.links


def test_post_classification():
    posts = {p.post_id: p for p in parse_channel_page(HTML, "test_jobs")}
    assert is_vacancy_post(posts[101].text, 120) is None
    assert is_vacancy_post(posts[102].text, 120) == "ad"
    assert is_vacancy_post(posts[103].text, 120) == "resume"
    assert is_vacancy_post(posts[104].text, 120) is None
    assert is_vacancy_post("Привет всем! Как дела?", 10) == "not_vacancy"


def test_post_to_vacancy_fields(app_config):
    posts = {p.post_id: p for p in parse_channel_page(HTML, "test_jobs")}
    v = post_to_vacancy(posts[101], app_config, assume_remote=True)
    assert v is not None
    assert v.title == "Копирайтер в SEO-агентство"
    assert v.company == "ООО «Текстовик»"
    assert (v.salary_from, v.salary_to, v.salary_net) == (60000, 80000, True)
    assert v.work_format == "remote" and v.experience == "1-3"
    assert v.links[0].label == "@test_jobs"

    piece = post_to_vacancy(posts[104], app_config, assume_remote=True)
    assert piece is not None
    assert piece.title.startswith("Автор статей для медиа")
    assert piece.salary_period == "piece" and piece.experience == "none" and piece.training


def test_extract_title_variants():
    assert extract_title("Должность — SMM-менеджер\nОплата 40к") == "SMM-менеджер"
    assert extract_title("#вакансия\n🚀 Контент-менеджер в онлайн-школу\nУсловия…") == (
        "Контент-менеджер в онлайн-школу"
    )


def test_same_text_reposted_has_same_id(app_config):
    post = parse_channel_page(HTML, "test_jobs")[0]
    other = parse_channel_page(HTML.replace("test_jobs/101", "another/7"), "another")[0]
    a = post_to_vacancy(post, app_config, assume_remote=True)
    b = post_to_vacancy(other, app_config, assume_remote=True)
    assert a and b and a.external_id == b.external_id and a.url != b.url


def test_scam_post_is_hidden(app_config):
    post = parse_channel_page(HTML, "test_jobs")[-1]
    v = post_to_vacancy(post, app_config, assume_remote=True)
    assert v is not None
    verdict, reasons = AntiScam(app_config.antiscam).check(v)
    assert verdict == "hide", reasons


def test_phrase_matching_with_word_forms():
    assert phrase_in("менеджер по продажам", "Ищем менеджера по продажам в команду")
    assert phrase_in("копирайтер", "Нужен копирайтера")  # опечатки и падежи
    assert phrase_in("автор статей", "Требуется автор статей для медиа")
    assert not phrase_in("менеджер по продажам", "менеджер по закупкам")
    assert phrase_in("smm", "SMM-специалист")
    assert not phrase_in("hr-ассистент", "ассистент руководителя")


def test_sister_filter_accepts_tg_post(app_config):
    posts = {p.post_id: p for p in parse_channel_page(HTML, "test_jobs")}
    v = post_to_vacancy(posts[101], app_config, assume_remote=True)
    f = ProfileFilter(app_config.profiles["sister"], text_sources=frozenset({"telegram_channels"}))
    assert f.reason(v) is None
    gf = ProfileFilter(
        app_config.profiles["girlfriend"], text_sources=frozenset({"telegram_channels"})
    )
    assert gf.reason(v) == "experience"


@respx.mock
async def test_fetch_filters_and_caches(app_config, monkeypatch):
    monkeypatch.setattr(
        "app.sources.telegram_channels.datetime",
        type(
            "D",
            (datetime,),
            {"now": staticmethod(lambda tz=None: datetime(2026, 11, 18, 12, tzinfo=UTC))},
        ),
    )
    route = respx.get("https://t.me/s/test_jobs").mock(return_value=httpx.Response(200, text=HTML))
    dead = respx.get("https://t.me/s/dead_channel").mock(
        return_value=httpx.Response(200, text="<html><body>no preview</body></html>")
    )
    cfg = app_config.model_copy(deep=True)
    cfg.sources["telegram_channels"].__pydantic_extra__["common_channels"] = ["@test_jobs"]
    profile = cfg.profiles["sister"].model_copy(
        update={"telegram_channels": ["dead_channel", "test_jobs"]}
    )
    src = TelegramChannelsSource(cfg, PoliteClient(HttpConfig(delay_seconds=0)))
    out = await src.fetch(profile)
    titles = sorted(v.title for v in out)
    # реклама, резюме и старый пост отсеяны; мошеннический пост остаётся до антискама
    assert len(out) == 3
    assert "Копирайтер в SEO-агентство" in titles
    assert src.last_stats["skipped"] == {"ad": 1, "resume": 1, "old": 1}
    assert src.last_stats["dead"] == ["dead_channel"]
    await src.fetch(profile)
    assert route.call_count == 1 and dead.call_count == 1  # кэш между профилями
    await src.aclose()


@respx.mock
async def test_fetch_all_channels_down_raises(app_config, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("app.sources.http.asyncio.sleep", no_sleep)
    respx.get(url__regex=r"https://t\.me/s/.*").mock(return_value=httpx.Response(503))
    profile = app_config.profiles["sister"].model_copy(update={"telegram_channels": ["a"]})
    cfg = app_config.model_copy(deep=True)
    cfg.sources["telegram_channels"].__pydantic_extra__["common_channels"] = []
    src = TelegramChannelsSource(cfg, PoliteClient(HttpConfig(delay_seconds=0)))
    with pytest.raises(Exception, match="503"):
        await src.fetch(profile)
