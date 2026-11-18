from datetime import timedelta

from app.processing.antiscam import AntiScam
from app.processing.dedupe import dedupe
from app.processing.filters import ProfileFilter
from app.processing.scoring import rank
from tests.conftest import NOW, make_vacancy


def test_dedupe_cross_source_merges_links():
    a = make_vacancy(source="trudvsem", external_id="t1", url="https://trudvsem.ru/1")
    b = make_vacancy(
        source="superjob",
        external_id="s1",
        url="https://superjob.ru/1",
        company="Ромашка ООО",
        title="Менеджер по работе с клиентами!",
        salary_from=60000,
    )
    c = make_vacancy(source="trudvsem", external_id="t1", url="https://trudvsem.ru/1")
    out = dedupe([a, b, c])
    assert len(out) == 1
    assert out[0].salary_from == 60000  # оставлена карточка с зарплатой
    assert {lk.url for lk in out[0].links} == {"https://trudvsem.ru/1", "https://superjob.ru/1"}


def test_dedupe_keeps_different():
    a = make_vacancy(external_id="1")
    b = make_vacancy(external_id="2", title="Администратор салона")
    c = make_vacancy(external_id="3", company="Другая компания")
    assert len(dedupe([a, b, c])) == 3


def test_antiscam(app_config):
    scam = AntiScam(app_config.antiscam)
    good = make_vacancy(description="Обучение за счёт компании, официальное оформление")
    mlm = make_vacancy(description="Сетевой маркетинг, приглашай друзей")
    fee = make_vacancy(description="Нужен первоначальный взнос 5000")
    crypto = make_vacancy(description="Торговля криптовалютой, без опыта")
    big = make_vacancy(description="Доход до 300 000 в первый месяц!")
    kept, hidden = scam.apply([good, mlm, fee, crypto, big])
    assert hidden == [mlm, fee]
    assert kept == [good, crypto, big]
    assert good.warnings == []
    assert crypto.warnings and big.warnings


def test_filter_girlfriend(app_config):
    p = app_config.profiles["girlfriend"]
    f = ProfileFilter(p)
    ok = make_vacancy()
    remote = make_vacancy(city="Москва", work_format="remote", title="Оператор чата")
    other_city = make_vacancy(city="Москва", title="Администратор")
    driver = make_vacancy(title="Водитель-курьер", salary_from=150000)
    low = make_vacancy(salary_from=30000, salary_to=40000)
    no_salary = make_vacancy(title="Ассистент")
    experienced = make_vacancy(title="Рекрутер", experience="3-6")
    any_job = make_vacancy(title="Мастер приёмки", salary_from=90000)
    res = f.apply([ok, remote, other_city, driver, low, no_salary, experienced, any_job])
    assert res.kept == [ok, remote, no_salary, any_job]
    assert res.rejected == {"city": 1, "stop_word": 1, "salary": 1, "experience": 1}


def test_filter_min_salary_override(app_config):
    p = app_config.profiles["girlfriend"]
    v = make_vacancy(salary_from=55000)
    assert ProfileFilter(p).reason(v) is None
    assert ProfileFilter(p, min_salary=60000).reason(v) == "salary"


def test_filter_hidden_company(app_config):
    p = app_config.profiles["girlfriend"]
    f = ProfileFilter(p, hidden_companies={"Ромашка ООО"})
    assert f.reason(make_vacancy()) == "hidden_company"


def test_filter_sister(app_config):
    p = app_config.profiles["sister"]
    f = ProfileFilter(p)
    remote = make_vacancy(title="Копирайтер", work_format="remote", experience="1-3")
    office = make_vacancy(title="Копирайтер", work_format="office")
    senior = make_vacancy(title="Редактор", work_format="remote", experience="3-6")
    designer = make_vacancy(title="Дизайнер-копирайтер", work_format="remote")
    piece = make_vacancy(
        title="Автор статей",
        work_format="remote",
        salary_period="piece",
        salary_text="200 ₽ за 1000 знаков",
    )
    res = f.apply([remote, office, senior, designer, piece])
    assert res.kept == [remote, senior]
    assert res.freelance == [piece]
    assert "опыт 3-6 лет" in senior.warnings
    assert res.rejected == {"format": 1, "stop_word": 1}


def test_rank_salary_desc(app_config):
    p = app_config.profiles["girlfriend"]
    a = make_vacancy(external_id="a", salary_from=60000)
    b = make_vacancy(external_id="b", salary_from=90000, salary_to=110000)
    c = make_vacancy(external_id="c")
    d = make_vacancy(external_id="d", salary_from=200000, warnings=["пассивный доход"])
    out = rank([a, b, c, d], p, app_config.scoring, now=NOW)
    assert [v.external_id for v in out] == ["b", "a", "d", "c"]


def test_rank_relevance_prefers_keywords_and_fresh(app_config):
    p = app_config.profiles["sister"]
    fresh = make_vacancy(external_id="f", title="Копирайтер", work_format="remote")
    old = make_vacancy(
        external_id="o",
        title="Копирайтер",
        work_format="remote",
        published_at=NOW - timedelta(days=6),
    )
    weak = make_vacancy(external_id="w", title="Специалист", work_format="remote")
    out = rank([weak, old, fresh], p, app_config.scoring, now=NOW)
    assert [v.external_id for v in out] == ["f", "o", "w"]


def test_dislike_lowers_similar(app_config):
    p = app_config.profiles["sister"]
    a = make_vacancy(external_id="a", title="Копирайтер маркетплейсов", work_format="remote")
    b = make_vacancy(external_id="b", title="Копирайтер", work_format="remote")
    out = rank([a, b], p, app_config.scoring, disliked_titles=["Копирайтер маркетплейсов"], now=NOW)
    assert out[0].external_id == "b"
