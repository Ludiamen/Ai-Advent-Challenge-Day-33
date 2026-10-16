"""GitHub для ревью PR: событие, комментарий, история и оценки (spec FR-07, FR-09, FR-11, plan D-10).

REST API через httpx, без сторонних actions и SDK. Токен — `GITHUB_TOKEN` задания Actions
(права workflow: contents read, pull-requests write) или `GITHUB_PAT` владельца из `.env`
для локального `--ревью-гитхаб N`. Токен не попадает ни в журнал, ни в текст ошибок.

Комментарий ревью один на PR: ищется по метке `<!-- ai-review:advent -->` без учёта
автора (Action пишет от github-actions[bot], локальный запуск — от владельца) и
обновляется, а не плодится. История прогонов — скрытые строки метрик в комментариях
репозитория, оценки людей — реакции 👍/👎 на них (GitHub отдаёт счётчики реакций прямо в
объекте комментария).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from agent.review import render

API = "https://api.github.com"
ТАЙМАУТ = 30.0
ПОВТОРОВ = 3
СТРАНИЦ = 5


class ОшибкаGitHub(RuntimeError):
    def __init__(self, статус: int, текст: str) -> None:
        super().__init__(f"GitHub {статус}: {текст}")
        self.статус = статус


@dataclass
class СобытиеPR:
    репозиторий: str
    номер: int
    база_ветка: str
    база: str
    голова: str
    заголовок: str
    описание: str
    форк: bool
    черновик: bool

    @property
    def ссылка(self) -> str:
        return f"https://github.com/{self.репозиторий}/pull/{self.номер}"


def из_pr(данные: dict[str, Any], репозиторий: str) -> СобытиеPR:
    голова_репо = ((данные.get("head") or {}).get("repo") or {}).get("full_name") or ""
    return СобытиеPR(
        репозиторий=репозиторий, номер=int(данные["number"]),
        база_ветка=данные["base"]["ref"], база=данные["base"]["sha"], голова=данные["head"]["sha"],
        заголовок=данные.get("title") or "", описание=данные.get("body") or "",
        форк=bool(голова_репо) and голова_репо.lower() != репозиторий.lower(),
        черновик=bool(данные.get("draft")))


def событие(путь: str = "", репозиторий: str = "") -> СобытиеPR:
    """Событие pull_request из файла GITHUB_EVENT_PATH."""
    путь = путь or os.environ.get("GITHUB_EVENT_PATH", "")
    with open(путь, encoding="utf-8") as f:
        данные = json.load(f)
    pr = данные.get("pull_request")
    if not pr:
        raise ValueError("событие не pull_request")
    репозиторий = репозиторий or os.environ.get("GITHUB_REPOSITORY") or данные["repository"]["full_name"]
    return из_pr(pr, репозиторий)


class GitHub:
    def __init__(self, токен: str, репозиторий: str, api: str = "", http: httpx.Client | None = None) -> None:
        if not токен:
            raise ОшибкаGitHub(0, "нет токена (GITHUB_TOKEN в Actions или GITHUB_PAT в .env)")
        self._токен = токен
        self.репозиторий = репозиторий
        self.api = (api or os.environ.get("GITHUB_API_URL") or API).rstrip("/")
        self.http = http or httpx.Client(timeout=ТАЙМАУТ)

    def _чисто(self, текст: str) -> str:
        return (текст or "").replace(self._токен, "***")[:300]

    def запрос(self, метод: str, путь: str, **kw: Any) -> Any:
        заголовки = {"Authorization": f"Bearer {self._токен}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ai-review-advent"}
        последняя = ""
        for попытка in range(ПОВТОРОВ):
            try:
                ответ = self.http.request(метод, f"{self.api}{путь}", headers=заголовки, **kw)
            except httpx.HTTPError as e:
                последняя = self._чисто(f"сеть: {type(e).__name__}")
            else:
                if ответ.status_code < 400:
                    return ответ.json() if ответ.content else None
                if ответ.status_code < 500 and ответ.status_code != 429:
                    raise ОшибкаGitHub(ответ.status_code, self._чисто(ответ.text))
                последняя = f"{ответ.status_code}: {self._чисто(ответ.text)}"
            time.sleep(1.5 * (попытка + 1))
        raise ОшибкаGitHub(599, последняя)

    def pr(self, номер: int) -> СобытиеPR:
        return из_pr(self.запрос("GET", f"/repos/{self.репозиторий}/pulls/{int(номер)}"), self.репозиторий)

    def комментарии(self, номер: int) -> list[dict[str, Any]]:
        итог: list[dict[str, Any]] = []
        for страница in range(1, СТРАНИЦ + 1):
            часть = self.запрос("GET", f"/repos/{self.репозиторий}/issues/{int(номер)}/comments",
                                params={"per_page": 100, "page": страница}) or []
            итог += часть
            if len(часть) < 100:
                break
        return итог

    def найти_свой(self, номер: int) -> dict[str, Any] | None:
        return next((к for к in self.комментарии(номер) if render.МЕТКА in (к.get("body") or "")), None)

    def опубликовать(self, номер: int, текст: str) -> tuple[int, str]:
        """Создать комментарий ревью или обновить прежний; вернуть (id, «создан» | «обновлён»)."""
        прежний = self.найти_свой(номер)
        if прежний:
            к = self.запрос("PATCH", f"/repos/{self.репозиторий}/issues/comments/{прежний['id']}",
                            json={"body": текст})
            return int(к["id"]), "обновлён"
        к = self.запрос("POST", f"/repos/{self.репозиторий}/issues/{int(номер)}/comments", json={"body": текст})
        return int(к["id"]), "создан"

    def история(self, предел: int = 100) -> tuple[list[dict[str, Any]], list[int]]:
        """Метрики прошлых прогонов (от старых к новым) и оценки людей (+1 / −1) из комментариев
        ревью репозитория. Комментарий обновляется при каждом прогоне, поэтому один PR — одна
        последняя строка метрик."""
        комментарии = self.запрос("GET", f"/repos/{self.репозиторий}/issues/comments",
                                  params={"sort": "updated", "direction": "desc", "per_page": min(100, предел)}) or []
        строки, оценки = [], []
        for к in комментарии:
            тело = к.get("body") or ""
            if render.МЕТКА not in тело:
                continue
            м = render.метрики_из(тело)
            if м:
                строки.append(м)
            р = к.get("reactions") or {}
            оценки += [1] * int(р.get("+1") or 0) + [-1] * int(р.get("-1") or 0)
        return list(reversed(строки)), оценки
