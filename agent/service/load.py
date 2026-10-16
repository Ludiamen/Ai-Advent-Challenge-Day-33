"""Нагрузочный прогон приватного сервиса: сводка по ступеням (spec FR-18, § 6.2).

Клиент (`service_load.py`, в контейнере Docker) пишет сырые записи — по одной
на запрос; сводка считается здесь: задержка p50/p95, ожидание в очереди,
первый токен, ответов в минуту, ошибки (5xx, обрывы, таймауты) отдельно от
отказов (429 — работа лимитов), повторы после 429, причины остановки.

  сводка(прогон) -> {"ступени": [...], "документы": {...}}
  последний(каталог) -> прогон | None
  полный(каталог) -> последний прогон со ступенями 1 → 2 → 4 → 8 | None
"""

from __future__ import annotations

import glob
import json
import math
import os
from typing import Any

ПАПКА = "service-load"
ПОЛНЫЕ_СТУПЕНИ = [1, 2, 4, 8]


def каталог_итогов(корень: str) -> str:
    путь = os.path.join(os.environ.get("RAG_DIR") or os.path.join(корень, "index"), ПАПКА)
    os.makedirs(путь, exist_ok=True)
    return путь


def перцентиль(значения: list[float], p: float) -> float | None:
    чистые = sorted(в for в in значения if isinstance(в, (int, float)))
    if not чистые:
        return None
    k = (len(чистые) - 1) * p
    н, в = math.floor(k), math.ceil(k)
    return round(чистые[н] + (чистые[в] - чистые[н]) * (k - н), 2)


def _ступень(записи: list[dict[str, Any]], ресурсы: dict[str, Any] | None) -> dict[str, Any]:
    успешные = [з for з in записи if з.get("код") == 200 and not з.get("ошибка")]
    ошибки = [з for з in записи if з.get("код") != 200 or з.get("ошибка")]
    задержки = [з.get("длительность_с") for з in успешные]
    начало = min((з.get("начало") or 0) for з in записи) if записи else 0
    конец = max((з.get("конец") or 0) for з in записи) if записи else 0
    длительность = max(0.0, конец - начало)
    причины: dict[str, int] = {}
    for з in успешные:
        причины[з.get("причина") or "—"] = причины.get(з.get("причина") or "—", 0) + 1
    return {
        "запросов": len(записи), "успешно": len(успешные), "ошибок": len(ошибки),
        "ошибки": sorted({str(з.get("ошибка") or з.get("код")) for з in ошибки}),
        "отказов_429": sum(з.get("отказов_429", 0) for з in записи),
        "повторов": sum(1 for з in записи if з.get("отказов_429")),
        "задержка_p50_с": перцентиль(задержки, 0.5), "задержка_p95_с": перцентиль(задержки, 0.95),
        "ожидание_p50_с": перцентиль([з.get("ожидание_с") for з in успешные], 0.5),
        "ожидание_p95_с": перцентиль([з.get("ожидание_с") for з in успешные], 0.95),
        "первый_токен_p50_с": перцентиль([з.get("первый_токен_с") for з in успешные], 0.5),
        "ответов_в_минуту": round(len(успешные) / длительность * 60, 2) if длительность else None,
        "длительность_с": round(длительность, 1), "токенов_ответа_p50": перцентиль([з.get("ответ") for з in успешные], 0.5),
        "причины": причины, "ресурсы": ресурсы or {},
    }


def сводка(прогон: dict[str, Any]) -> dict[str, Any]:
    записи = прогон.get("записи") or []
    ресурсы = прогон.get("ресурсы") or {}
    ступени = []
    for n in sorted({з["ступень"] for з in записи if з.get("вид") == "чат"}):
        с = _ступень([з for з in записи if з.get("вид") == "чат" and з["ступень"] == n], ресурсы.get(str(n)))
        ступени.append({"клиентов": n, **с})
    док = [з for з in записи if з.get("вид") == "документы"]
    return {"метка": прогон.get("метка"), "адрес": прогон.get("адрес"), "ступени": ступени,
            "документы": _ступень(док, ресурсы.get("документы")) if док else None,
            "всего_ошибок": sum(с["ошибок"] for с in ступени) + (len([з for з in док if з.get("код") != 200]) if док else 0)}


def прогоны(каталог: str, n: int = 5) -> list[dict[str, Any]]:
    """Сводки последних прогонов (новые первыми) — для страницы: полный ступенями и последний короткий."""
    итог = []
    for файл in sorted(glob.glob(os.path.join(каталог, "run-*.json")), key=os.path.getmtime, reverse=True)[:n]:
        try:
            with open(файл, encoding="utf-8") as f:
                итог.append({"файл": os.path.basename(файл), **сводка(json.load(f))})
        except (OSError, ValueError):
            pass
    return итог


def последний(каталог: str) -> dict[str, Any] | None:
    файлы = sorted(glob.glob(os.path.join(каталог, "run-*.json")), key=os.path.getmtime)
    if not файлы:
        return None
    with open(файлы[-1], encoding="utf-8") as f:
        прогон = json.load(f)
    прогон["файл"] = os.path.basename(файлы[-1])
    return прогон


def полный(каталог: str) -> dict[str, Any] | None:
    """Последний полный прогон (ступени 1 → 2 → 4 → 8).

    Короткие прогоны — показа («8 клиентов»), проверки консоли (2 клиента) —
    новее, но полный прогон они не заслоняют.
    """
    for файл in sorted(glob.glob(os.path.join(каталог, "run-*.json")), key=os.path.getmtime, reverse=True):
        try:
            with open(файл, encoding="utf-8") as f:
                прогон = json.load(f)
        except (OSError, ValueError):
            continue
        ступени = sorted({з.get("ступень") for з in прогон.get("записи") or [] if з.get("вид") == "чат"})
        if ступени == ПОЛНЫЕ_СТУПЕНИ:
            прогон["файл"] = os.path.basename(файл)
            return прогон
    return None
