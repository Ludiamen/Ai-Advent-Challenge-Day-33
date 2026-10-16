"""Метрики конвейера ревью — без текстов (spec FR-11, plan D-12).

Строка прогона: время по этапам, токены и стоимость по ревьюерам, числа входа, опоры и
находок, вердикт, сбои. Где она живёт:
- локально — `memory/review/metrics.jsonl`;
- на GitHub — скрытой строкой в комментарии PR (render.МЕТКА_МЕТРИК), в сводке задания и
  в артефакте прогона. История прогонов репозитория собирается из комментариев.

Тревога из лекции: прогон стал вдвое дороже или дольше медианы последних 20 — значит,
что-то пошло не так (промпт, размер PR, провайдер); пока истории меньше 5 прогонов,
тревог нет. Качество — доля 👍 среди реакций людей на комментарии ревью.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
from typing import Any, Iterable

from agent.devassist.metrics import перцентиль

ПОЛЯ = ("время", "канал", "репозиторий", "pr", "голова", "база", "модель", "поиск", "секунд", "этапы",
        "токены", "токены_вход", "токены_выход", "стоимость", "ревьюеры", "вход", "опора", "сборка",
        "вердикт", "сбои", "тревоги", "скрыто_пд")
ИСТОРИЯ = 20
МИНИМУМ_ИСТОРИИ = 5
РАЗ = 2.0
_замок = threading.Lock()


def путь_по_умолчанию() -> str:
    from agent.devassist.project import КОРЕНЬ
    память = os.path.abspath(os.getenv("MEMORY_DIR") or os.path.join(КОРЕНЬ, "memory"))
    return os.path.join(память, "review", "metrics.jsonl")


def очистить(строка: dict[str, Any]) -> dict[str, Any]:
    """Только поля из белого списка; строки — короткие (вид сбоя, а не пересказ)."""
    чисто = {к: строка[к] for к in ПОЛЯ if к in строка}
    чисто.setdefault("время", time.strftime("%Y-%m-%dT%H:%M:%S"))
    if isinstance(чисто.get("сбои"), list):
        чисто["сбои"] = [str(с)[:120] for с in чисто["сбои"]]
    return чисто


def записать(строка: dict[str, Any], путь: str = "") -> dict[str, Any]:
    путь = путь or путь_по_умолчанию()
    чисто = очистить(строка)
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
        return []
    return строки


def базовая_линия(история: Iterable[dict[str, Any]]) -> dict[str, float] | None:
    """Медианы токенов и времени последних 20 выполненных прогонов (или None — истории мало)."""
    выполненные = [с for с in история if с.get("вердикт") in ("зелёный", "красный")][-ИСТОРИЯ:]
    if len(выполненные) < МИНИМУМ_ИСТОРИИ:
        return None
    return {"токены": statistics.median(float(с.get("токены") or 0) for с in выполненные),
            "секунд": statistics.median(float(с.get("секунд") or 0) for с in выполненные),
            "прогонов": len(выполненные)}


def тревоги(строка: dict[str, Any], история: Iterable[dict[str, Any]]) -> list[str]:
    линия = базовая_линия(история)
    if not линия:
        return []
    итог = []
    for поле, что in (("токены", "токенов"), ("секунд", "времени")):
        было, стало = линия[поле], float(строка.get(поле) or 0)
        if было > 0 and стало > РАЗ * было:
            итог.append(f"{что} в {стало / было:.1f} раза больше медианы последних {int(линия['прогонов'])} "
                        f"прогонов ({стало:.0f} против {было:.0f}) — проверьте промпт, размер PR и провайдера")
    return итог


def сводка(строки: Iterable[dict[str, Any]], оценки: Iterable[int] = ()) -> dict[str, Any]:
    """По моделям: прогоны, p50/p95 времени, токены и стоимость на PR, доля красных, доля без
    сбоев; оценки людей (+1 / −1) — доля 👍."""
    по_моделям: dict[str, list[dict[str, Any]]] = {}
    for с in строки:
        по_моделям.setdefault(str(с.get("модель") or "?"), []).append(с)
    итог: dict[str, Any] = {"модели": {}}
    for модель, список in по_моделям.items():
        секунды = [float(с.get("секунд") or 0) for с in список]
        токены = [float(с.get("токены") or 0) for с in список]
        стоимость = [float(с.get("стоимость") or 0) for с in список]
        выполнено = [с for с in список if с.get("вердикт") in ("зелёный", "красный")]
        итог["модели"][модель] = {
            "прогонов": len(список),
            "p50_с": round(перцентиль(секунды, 0.5), 1), "p95_с": round(перцентиль(секунды, 0.95), 1),
            "токенов_на_pr": round(statistics.mean(токены)) if токены else 0,
            "стоимость_на_pr": round(statistics.mean(стоимость), 5) if стоимость else 0.0,
            "доля_красных": round(sum(1 for с in выполнено if с["вердикт"] == "красный") / len(выполнено), 3)
            if выполнено else 0.0,
            "доля_без_сбоев": round(sum(1 for с in список if not с.get("сбои") and с.get("вердикт") != "не выполнено")
                                    / len(список), 3),
        }
    оценки = list(оценки)
    итог["оценок"] = len(оценки)
    итог["доля_👍"] = round(sum(1 for о in оценки if о > 0) / len(оценки), 3) if оценки else None
    return итог
