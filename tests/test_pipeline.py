import pytest

from app.bot.render import render_card
from app.config import Settings
from app.db import Database
from app.pipeline import Pipeline
from tests.conftest import NOW, make_vacancy


class FakeSource:
    name = "trudvsem"

    def __init__(self, items):
        self.items = items
        self.fail = False

    async def fetch(self, profile):
        if self.fail:
            raise RuntimeError("boom")
        return [v.model_copy(deep=True) for v in self.items]

    async def aclose(self):
        pass


class FakeSender:
    def __init__(self):
        self.digests = []
        self.texts = []
        self._id = 0

    async def send_digest(self, chat_id, profile, main, freelance, total_new):
        self.digests.append((chat_id, [v.title for v in main], [v.title for v in freelance]))
        out = []
        for v in [*main, *freelance]:
            self._id += 1
            out.append((v, self._id))
        return out

    async def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))
        return 1


@pytest.fixture
def env(monkeypatch, app_config):
    monkeypatch.setenv("GIRLFRIEND_CHAT_ID", "111")
    monkeypatch.setenv("SISTER_CHAT_ID", "222")
    items = [
        make_vacancy(external_id="1", salary_from=70000),
        make_vacancy(external_id="2", title="Администратор", salary_from=55000),
        make_vacancy(external_id="3", title="Грузчик", salary_from=90000),
        make_vacancy(external_id="4", title="Ассистент", description="Сетевой маркетинг"),
        make_vacancy(
            external_id="5",
            title="Копирайтер",
            work_format="remote",
            experience="1-3",
            city="Москва",
        ),
    ]
    src = FakeSource(items)
    sender = FakeSender()
    settings = Settings(admin_chat_id=999, _env_file=None)
    db = Database("sqlite:///:memory:")
    return Pipeline(app_config, settings, db, {"trudvsem": src}, sender), src, sender


async def test_run_sends_and_is_idempotent(env):
    pipe, _, sender = env
    rep = await pipe.run_profile("girlfriend")
    assert rep.sent == 2
    assert sender.digests[0] == (111, ["Менеджер по работе с клиентами", "Администратор"], [])
    # повторный запуск — ничего нового
    rep2 = await pipe.run_profile("girlfriend")
    assert rep2.sent == 0
    assert sender.texts[-1] == (111, "Новых подходящих вакансий нет 🙂")
    # сестре своя подборка
    rep3 = await pipe.run_profile("sister")
    assert [v.title for v in rep3.main] == ["Копирайтер"]


async def test_dry_run_does_not_record(env):
    pipe, _, sender = env
    rep = await pipe.run_profile("girlfriend", dry_run=True)
    assert len(rep.main) == 2 and rep.sent == 0
    assert sender.digests == []
    rep2 = await pipe.run_profile("girlfriend", dry_run=True)
    assert len(rep2.main) == 2


async def test_paused_profile_skipped(env):
    pipe, _, sender = env
    pipe.db.update_state("girlfriend", paused=True)
    rep = await pipe.run_profile("girlfriend")
    assert rep.skipped == "paused" and sender.digests == []
    rep = await pipe.run_profile("girlfriend", manual=True)
    assert rep.sent == 2


async def test_source_error_notifies_admin_and_unhealthy(env):
    pipe, src, sender = env
    src.fail = True
    for _ in range(3):
        rep = await pipe.run_profile("girlfriend")
        assert "trudvsem" in rep.errors
    admin_msgs = [t for c, t in sender.texts if c == 999]
    assert any("boom" in t for t in admin_msgs)
    assert any("3 запуска подряд" in t for t in admin_msgs)


def test_render_card():
    v = make_vacancy(
        salary_from=70000, salary_to=90000, salary_net=True, training=True, work_format="hybrid"
    )
    text = render_card(v, now=NOW)
    assert "💰 70 000 – 90 000 ₽ · на руки" in text
    assert "📌 <b>Менеджер по работе с клиентами</b>" in text
    assert "🏢 ООО «Ромашка» · Челябинск · гибрид" in text
    assert "🎓 Опыт не нужен · обучение" in text
    assert "🕒 сегодня · Работа России" in text
    assert 'href="https://example.org/1"' in text
    warn = render_card(make_vacancy(title="<script>", warnings=["mlm"]), now=NOW)
    assert warn.startswith("⚠️") and "&lt;script&gt;" in warn


async def test_source_cap_limits_telegram(env, app_config):
    pipe, src, sender = env
    src.items = [
        make_vacancy(
            source="telegram_channels",
            external_id=f"tg{i}",
            title="Копирайтер",
            company="",
            work_format="remote",
            description=f"пост {i}",
        )
        for i in range(12)
    ]
    rep = await pipe.run_profile("sister", dry_run=True)
    assert len(rep.main) == app_config.sources["telegram_channels"].opt("max_in_digest") == 7
