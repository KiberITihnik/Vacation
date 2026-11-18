import httpx
import pytest
import respx

from app.config import HttpConfig
from app.sources.base import SourceError
from app.sources.http import PoliteClient
from app.sources.trudvsem import TrudvsemSource, parse_vacancy

BASE = "http://opendata.trudvsem.ru/api/v1"


def fast_client() -> PoliteClient:
    return PoliteClient(HttpConfig(delay_seconds=0, retries=3, timeout_seconds=5))


def test_parse_vacancy(app_config, trudvsem_json):
    items = trudvsem_json["results"]["vacancies"]
    v = parse_vacancy(items[0], app_config)
    assert v is not None
    assert v.external_id == "a1b2c3d4-0001"
    assert v.title == "Менеджер по работе с клиентами"
    assert v.company == "ООО «Ромашка»"
    assert v.city == "Челябинск"
    assert (v.salary_from, v.salary_to, v.salary_period) == (60000, 90000, "month")
    assert v.experience == "none"
    assert v.work_format == "office"
    assert v.training is True
    assert "<p>" not in v.description
    remote = parse_vacancy(items[2], app_config)
    assert remote is not None and remote.work_format == "remote" and remote.experience == "1-3"
    only_min = parse_vacancy(items[1], app_config)
    assert only_min is not None and (only_min.salary_from, only_min.salary_to) == (70000, None)


def test_parse_tolerates_garbage(app_config):
    assert parse_vacancy({"vacancy": {}}, app_config) is None
    v = parse_vacancy(
        {"vacancy": {"id": "x", "job-name": "Оператор", "addresses": "??"}}, app_config
    )
    assert v is not None and v.url.endswith("/x")


@respx.mock
async def test_fetch_region_and_remote(app_config, trudvsem_json):
    region = respx.get(f"{BASE}/vacancies/region/7400000000000").mock(
        return_value=httpx.Response(200, json=trudvsem_json)
    )
    allru = respx.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(200, json=trudvsem_json)
    )
    profile = app_config.profiles["girlfriend"].model_copy(update={"search_queries": ["менеджер"]})
    src = TrudvsemSource(app_config, fast_client())
    out = await src.fetch(profile)
    await src.aclose()
    assert region.called and allru.called
    assert region.calls[0].request.url.params["text"] == "менеджер"
    assert "modifiedFrom" in region.calls[0].request.url.params
    # 4 из региона + удалённая из всероссийского поиска (дубликаты по id схлопнуты)
    assert {v.external_id for v in out} == {f"a1b2c3d4-000{i}" for i in range(1, 5)}


@respx.mock
async def test_fetch_retries_then_fails(app_config, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("app.sources.http.asyncio.sleep", no_sleep)
    route = respx.get(f"{BASE}/vacancies").mock(return_value=httpx.Response(503))
    profile = app_config.profiles["sister"].model_copy(update={"search_queries": ["копирайтер"]})
    src = TrudvsemSource(app_config, fast_client())
    with pytest.raises(SourceError):
        await src.fetch(profile)
    assert route.call_count == 3


@respx.mock
async def test_fetch_no_retry_on_403(app_config):
    route = respx.get(f"{BASE}/vacancies").mock(return_value=httpx.Response(403))
    profile = app_config.profiles["sister"].model_copy(update={"search_queries": ["копирайтер"]})
    with pytest.raises(SourceError):
        await TrudvsemSource(app_config, fast_client()).fetch(profile)
    assert route.call_count == 1
