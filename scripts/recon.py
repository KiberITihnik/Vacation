"""Этап 0 — разведка источников. Запускать НА СЕРВЕРЕ, где будет работать бот:

    python scripts/recon.py            # без ключей: trudvsem, hh.ru, Telegram
    SUPERJOB_APP_KEY=... python scripts/recon.py

Печатает отчёт и сохраняет реальные ответы в tests/fixtures/recon_*.json.
Только стандартная библиотека — ничего устанавливать не нужно.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = "JobAlertsBot/0.1 (personal non-commercial job digest)"
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def save(name: str, body: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(body)
        (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except json.JSONDecodeError:
        (OUT / name.replace(".json", ".txt")).write_text(body[:20000], encoding="utf-8")


def check(title: str, url: str, fixture: str | None = None, headers: dict[str, str] | None = None):
    status, body = get(url, headers)
    ok = status == 200
    print(f"[{'OK ' if ok else 'ERR'}] {title}: HTTP {status}")
    if not ok:
        print("      ", body[:300].replace("\n", " "))
    if fixture and status:
        save(fixture, body)
    return status, body


def main() -> int:
    q = urllib.parse.quote
    print("== Работа России (trudvsem) ==")
    for base in ("http://opendata.trudvsem.ru/api/v1", "https://opendata.trudvsem.ru/api/v1"):
        st, body = check(
            f"{base} регион 74, «менеджер»",
            f"{base}/vacancies/region/7400000000000?text={q('менеджер')}&limit=5",
            "recon_trudvsem_region74.json" if base.startswith("http:") else None,
        )
        if st == 200:
            try:
                data = json.loads(body)
                vacs = data.get("results", {}).get("vacancies", [])
                print(f"       total={data.get('meta', {}).get('total')}, в ответе {len(vacs)}")
                if vacs:
                    print("       поля вакансии:", ", ".join(sorted(vacs[0].get("vacancy", {}))))
            except Exception as e:  # noqa: BLE001
                print("       не удалось разобрать JSON:", e)
    check(
        "trudvsem вся РФ, «копирайтер»",
        f"http://opendata.trudvsem.ru/api/v1/vacancies?text={q('копирайтер')}&limit=5",
        "recon_trudvsem_copywriter.json",
    )

    print("\n== SuperJob ==")
    key = os.environ.get("SUPERJOB_APP_KEY", "")
    if not key:
        print("[SKIP] SUPERJOB_APP_KEY не задан — получите ключ на api.superjob.ru")
    else:
        h = {"X-Api-App-Id": key}
        st, body = check(
            "towns?keyword=Челябинск",
            f"https://api.superjob.ru/2.0/towns/?keyword={q('Челябинск')}",
            "recon_superjob_towns.json",
            h,
        )
        if st == 200:
            for t in json.loads(body).get("objects", [])[:5]:
                print(f"       town id={t.get('id')} title={t.get('title')}")
        check(
            "vacancies «копирайтер», удалёнка",
            f"https://api.superjob.ru/2.0/vacancies/?keyword={q('копирайтер')}&place_of_work=2&count=5",
            "recon_superjob_vacancies.json",
            h,
        )

    print("\n== hh.ru (ожидаем 403: API поиска закрыт, используем письма автопоиска) ==")
    check("api.hh.ru/vacancies", "https://api.hh.ru/vacancies?text=python&per_page=1")

    print("\n== Telegram ==")
    check("api.telegram.org доступен", "https://api.telegram.org/")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        check("getMe", f"https://api.telegram.org/bot{token}/getMe")

    print(f"\nОтветы сохранены в {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
