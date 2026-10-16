"""Метрики ассистента разработчика (День 31, spec FR-14, plan D-09; лекция недели 7).

Строка JSONL на каждый ответ — **без текстов** вопроса и ответа: канал, проект, модель,
запасной путь, имена вызванных инструментов, «не знаю», токены, время по этапам, итог.
Поля — по белому списку: всё прочее молча отбрасывается (как в журнале сервиса Дня 30),
чтобы текст не попал в журнал по ошибке вызывающего.

Сводка: число ответов, доля «не знаю», доля запасного пути, доля ошибок, p50/p95 времени,
токены на ответ — по моделям и каналам.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from typing import Any, Iterable

ПОЛЯ = ("время", "канал", "проект", "модель", "запасной", "инструменты", "не_знаю", "вход", "ответ",
        "git_с", "поиск_с", "модель_с", "всего_с", "фрагментов", "цитат", "код", "ошибка")
_замок = threading.Lock()


def путь_по_умолчанию() -> str:
    from agent.devassist.project import КОРЕНЬ
    память = os.path.abspath(os.getenv("MEMORY_DIR") or os.path.join(КОРЕНЬ, "memory"))
    return os.path.join(память, "devassist", "metrics.jsonl")


def записать(строка: dict[str, Any], путь: str = "") -> dict[str, Any]:
    путь = путь or путь_по_умолчанию()
    чисто = {к: строка[к] for к in ПОЛЯ if к in строка}
    чисто.setdefault("время", time.strftime("%Y-%m-%dT%H:%M:%S"))
    if isinstance(чисто.get("ошибка"), str):
        чисто["ошибка"] = чисто["ошибка"][:120]   # вид сбоя, а не пересказ ответа
    os.makedirs(os.path.dirname(путь), exist_ok=True)
    with _замок, open(путь, "a", encoding="utf-8") as f:
        f.write(json.dumps(чисто, ensure_ascii=False) + "\n")
    return чисто


def прочитать(путь: str = "") -> list[dict[str, Any]]:
    путь = путь or путь_по_умолчанию()
    строки = []
    try:
        with open(путь, encoding="utf-8") as f:
            for с in f:
                try:
                    строки.append(json.loads(с))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return строки


def перцентиль(значения: list[float], доля: float) -> float:
    if not значения:
        return 0.0
    по_порядку = sorted(значения)
    место = доля * (len(по_порядку) - 1)
    низ, верх = int(место), min(int(место) + 1, len(по_порядку) - 1)
    return round(по_порядку[низ] + (по_порядку[верх] - по_порядку[низ]) * (место - низ), 1)


def _итог(строки: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(строки)
    время = [float(с.get("всего_с") or 0) for с in строки if с.get("код") == "ок"]
    return {
        "ответов": n,
        "не_знаю": round(sum(1 for с in строки if с.get("не_знаю")) / n, 3) if n else 0.0,
        "запасной": round(sum(1 for с in строки if с.get("запасной")) / n, 3) if n else 0.0,
        "ошибок": round(sum(1 for с in строки if с.get("код") != "ок") / n, 3) if n else 0.0,
        "p50_с": перцентиль(время, 0.5), "p95_с": перцентиль(время, 0.95),
        "вход_ср": round(sum(int(с.get("вход") or 0) for с in строки) / n) if n else 0,
        "ответ_ср": round(sum(int(с.get("ответ") or 0) for с in строки) / n) if n else 0,
        "с_инструментами": round(sum(1 for с in строки if с.get("инструменты")) / n, 3) if n else 0.0,
    }


def сводка(строки: Iterable[dict[str, Any]]) -> dict[str, Any]:
    строки = list(строки)
    по: dict[str, dict[str, list]] = {"модель": defaultdict(list), "канал": defaultdict(list)}
    for с in строки:
        по["модель"][с.get("модель") or "?"].append(с)
        по["канал"][с.get("канал") or "?"].append(с)
    return {"всего": _итог(строки),
            "по_моделям": {к: _итог(в) for к, в in sorted(по["модель"].items())},
            "по_каналам": {к: _итог(в) for к, в in sorted(по["канал"].items())}}
