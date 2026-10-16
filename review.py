"""Точка входа AI-ревью PR (День 32, specs/032-code-review, plan D-13).

В GitHub Actions (`.github/workflows/ai-review.yml`):
    python review.py --github
Локально:
    python review.py --база main --голова HEAD          # ревью диапазона, без публикации
    python review.py --pr 3                             # PR №3 с GitHub: ревью и комментарий (GITHUB_PAT)

Код выхода: 0 — зелёный (или PR из форка/черновик — пропуск с пояснением), 1 — красный
(есть подтверждённая критичная находка), 2 — ревью не выполнено. Ненулевой код красит
проверку PR; слияние решает человек.

Модуль не импортирует cli.py и тяжёлые пакеты сервиса: на раннере ставится только
requirements-review.txt.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any

КОРЕНЬ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, КОРЕНЬ)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(КОРЕНЬ, ".env"))   # локально; на раннере .env нет — ключи из Secrets
except ImportError:
    pass

from agent.review import github as gh  # noqa: E402
from agent.review import notify, pipeline  # noqa: E402


def _сводка(текст: str) -> None:
    """Сводка задания Actions (видна на странице прогона)."""
    путь = os.environ.get("GITHUB_STEP_SUMMARY")
    if путь:
        with open(путь, "a", encoding="utf-8") as f:
            f.write(текст.rstrip() + "\n\n")


def _метрики_в_файл(метрики: dict[str, Any]) -> None:
    путь = os.environ.get("REVIEW_METRICS_PATH")
    if путь:
        with open(путь, "w", encoding="utf-8") as f:
            json.dump(метрики, f, ensure_ascii=False, indent=1)


def _ход(этап: str, подпись: str) -> None:
    print(f"· {этап}{': ' + подпись if подпись else ''}", flush=True)


_СЕРВЕР = re.compile(r"^https://[A-Za-z0-9.-]+$")
_РЕПО = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def адрес_прогона() -> str:
    """Адрес прогона GitHub Actions из окружения задания; локально — пусто. Каждая часть
    проверяется по формату: адрес идёт ссылкой в комментарий, и подменённое окружение не
    должно вставить туда «javascript:» или разметку."""
    сервер, репо, ид = (os.environ.get(к, "") for к in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    if _СЕРВЕР.match(сервер) and _РЕПО.match(репо) and ид.isdigit():
        return f"{сервер}/{репо}/actions/runs/{ид}"
    return ""


def ревью_события(событие: gh.СобытиеPR, токен: str, канал: str = "github") -> int:
    """Ревью PR по событию: конвейер, комментарий, сводка, метрики, Telegram. Код выхода."""
    if событие.черновик:
        _сводка(f"AI-ревью пропущено: PR #{событие.номер} — черновик. Запустится при «Ready for review».")
        print("черновик — пропуск")
        return 0
    if событие.форк:
        _сводка(f"AI-ревью пропущено: PR #{событие.номер} из форка — GitHub не даёт ему секретов и права "
                "писать комментарии. Владелец может запустить ревью сам: `python review.py --pr "
                f"{событие.номер}`.")
        print("PR из форка — пропуск")
        return 0
    клиент: gh.GitHub | None = None
    история: list[dict[str, Any]] = []
    оценки: list[int] = []
    сбои: list[str] = []
    try:
        клиент = gh.GitHub(токен, событие.репозиторий)
        история, оценки = клиент.история()
    except gh.ОшибкаGitHub as e:
        сбои.append(f"история метрик: {e}")
    итог = pipeline.выполнить(событие.база, событие.голова, канал=канал, заголовок=событие.заголовок,
                              описание=событие.описание, репозиторий=событие.репозиторий, pr=событие.номер,
                              история=история, этап=_ход, прогон=адрес_прогона())
    код = итог.код_выхода
    if клиент is not None:
        try:
            ид, как = клиент.опубликовать(событие.номер, итог.текст)
            print(f"комментарий {как}: {событие.ссылка}#issuecomment-{ид}")
        except gh.ОшибкаGitHub as e:
            сбои.append(f"комментарий не опубликован: {e}")
            код = max(код, 2)
    _сводка(итог.текст + ("\n\n> ⚠️ " + "; ".join(сбои) if сбои else ""))
    _метрики_в_файл(итог.метрики)
    print(итог.консоль)
    if notify.нужно(итог.вердикт, итог.тревоги) or сбои:
        статус = notify.отправить(notify.из_итога(итог, событие.номер, событие.ссылка, событие.заголовок)
                                  + ("\n⚠️ " + "; ".join(сбои)[:300] if сбои else ""))
        print(f"Telegram: {статус}")
        if статус != "отправлено":
            _сводка(f"Уведомление в Telegram: {статус}")
    if оценки:
        print(f"оценки людей по прошлым ревью: 👍 {оценки.count(1)}, 👎 {оценки.count(-1)}")
    for с in сбои:
        print(f"сбой: {с}", file=sys.stderr)
    return код


def _askpass() -> dict[str, str]:
    """Окружение git с токеном владельца через GIT_ASKPASS — токен не в аргументах процесса."""
    скрипт = os.path.join(КОРЕНЬ, "out", "askpass.sh")
    окр = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if os.path.exists(скрипт):
        окр["GIT_ASKPASS"] = скрипт
    return окр


def ревью_pr(номер: int, репозиторий: str = "") -> int:
    """Локально: PR с GitHub по номеру — подтянуть коммиты PR и базы, ревью, комментарий."""
    токен = os.environ.get("GITHUB_PAT", "")
    репозиторий = репозиторий or os.environ.get("REVIEW_REPOSITORY") or _репозиторий_origin()
    событие = gh.GitHub(токен, репозиторий).pr(номер)
    for ссылка in (f"pull/{номер}/head", событие.база_ветка):
        subprocess.run(["git", "-C", КОРЕНЬ, "fetch", "-q", "origin", ссылка], check=True, env=_askpass(),
                       timeout=120)
    событие.форк = False   # владелец запускает сам — секреты его, ограничение форка не действует
    return ревью_события(событие, токен, канал="консоль-pr")


def _репозиторий_origin() -> str:
    адрес = subprocess.run(["git", "-C", КОРЕНЬ, "remote", "get-url", "origin"], capture_output=True,
                           text=True, check=True).stdout.strip()
    return адрес.removesuffix(".git").split("github.com")[-1].lstrip("/:")


def main(argv: list[str] | None = None) -> int:
    разбор = argparse.ArgumentParser(description="AI-ревью PR (День 32).")
    разбор.add_argument("--github", action="store_true", help="режим GitHub Actions: событие pull_request")
    разбор.add_argument("--pr", type=int, default=0, help="PR с GitHub по номеру (локально, GITHUB_PAT)")
    разбор.add_argument("--база", default="main", help="база диапазона (ветка, в которую вливают)")
    разбор.add_argument("--голова", default="HEAD", help="голова диапазона")
    разбор.add_argument("--модель", default="", help="модель ревьюеров (по умолчанию ds-flash)")
    а = разбор.parse_args(argv)
    if а.github:
        try:
            событие = gh.событие()
        except (OSError, ValueError, KeyError) as e:
            print(f"нет события pull_request: {e}", file=sys.stderr)
            return 2
        return ревью_события(событие, os.environ.get("GITHUB_TOKEN", ""))
    if а.pr:
        return ревью_pr(а.pr)
    итог = pipeline.выполнить(а.база, а.голова, модель=а.модель or pipeline.reviewers.МОДЕЛЬ, этап=_ход)
    print(итог.консоль)
    return итог.код_выхода


if __name__ == "__main__":
    sys.exit(main())
