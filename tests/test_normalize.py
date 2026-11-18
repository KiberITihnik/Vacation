import pytest

from app.processing.normalize import (
    city_from_address,
    norm_company,
    parse_experience,
    parse_salary,
    parse_work_format,
)


@pytest.mark.parametrize(
    ("text", "lo", "hi", "period"),
    [
        ("от 50 000", 50000, None, "month"),
        ("от 50 000 ₽", 50000, None, "month"),
        ("50–70 тыс.", 50000, 70000, "month"),
        ("50 - 70 тыс. руб.", 50000, 70000, "month"),
        ("$1000", 90000, None, "month"),
        ("до 120к", None, 120000, "month"),
        ("200 ₽/час", 32000, None, "hour"),
        ("от 2000 руб. за смену", 30000, None, "shift"),
        ("от 60 000 до 90 000 руб.", 60000, 90000, "month"),
        ("90000-60000", 60000, 90000, "month"),
        ("1,5 тыс", 1500, None, "month"),
    ],
)
def test_parse_salary(text, lo, hi, period):
    s = parse_salary(text)
    assert (s.salary_from, s.salary_to, s.period) == (lo, hi, period)


def test_salary_net_and_gross():
    assert parse_salary("от 50 000 ₽ на руки").net is True
    assert parse_salary("от 50 000 ₽ до вычета налогов").net is False
    assert parse_salary("от 50 000 ₽ до вычета налогов").salary_from == 50000
    assert parse_salary("50 000").net is None


@pytest.mark.parametrize("text", ["150 ₽ за 1000 знаков", "500 руб. за статью", "сдельная"])
def test_piece_rate(text):
    s = parse_salary(text)
    assert s.period == "piece"
    assert s.salary_from is None and s.salary_to is None


def test_empty_salary():
    assert parse_salary("").salary_from is None
    assert parse_salary("по договорённости").period == "unknown"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "none"),
        (1, "1-3"),
        (3, "3-6"),
        (7, "6+"),
        ("Без опыта", "none"),
        ("Опыт не требуется", "none"),
        ("noExperience", "none"),
        ("1–3 года", "1-3"),
        ("between1And3", "1-3"),
        ("от 3 лет", "3-6"),
        ("between3And6", "3-6"),
        ("Более 6 лет", "6+"),
        ("moreThan6", "6+"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_experience(value, expected):
    assert parse_experience(value) == expected


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (("Удаленная работа",), "remote"),
        (("Копирайтер (удалённо)",), "remote"),
        (("Дистанционная работа",), "remote"),
        (("Гибридный формат, удалённо 2 дня",), "hybrid"),
        (("Полный рабочий день",), "office"),
        (("",), "unknown"),
    ],
)
def test_work_format(texts, expected):
    assert parse_work_format(*texts) == expected


def test_city_and_company():
    assert city_from_address("Челябинская область, г Челябинск, ул Кирова, д 1") == "Челябинск"
    assert city_from_address("г. Ростов-на-Дону, пр. Ленина") == "Ростов-на-Дону"
    assert city_from_address("Челябинская область") == ""
    assert norm_company("ООО «Ромашка»") == norm_company("Ромашка, ООО") == "ромашка"
