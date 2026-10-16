"""Три ревьюера PR параллельно: баги, архитектура, рекомендации (spec FR-05, FR-09, plan D-06, D-07).

Это делегирование из лекции недели 7: у каждого ревьюера своя роль и свой отбор опоры, они
работают одновременно (общее время — как у самого медленного, а не сумма), а сборщик
(collect.py) проверяет и объединяет их находки.

Надёжность: сеть, 5xx и 429 повторяет клиент `llm.py`; неразборчивый JSON — один повтор с
подсказкой формата; ревьюер, не уложившийся в таймаут или упавший, даёт пустой раздел с
пометкой «не выполнен» — остальные разделы публикуются. Инструмент у ревьюеров один —
`git_show` (только чтение) и только по двум коммитам этого PR.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as ТаймаутПотока
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.review import prompts
from agent.review.context import РЕВЬЮЕРЫ, Опора
from agent.review.source import ВходPR
from agent.service import pd

МОДЕЛЬ = "ds-flash"
ОТВЕТ_ТОКЕНОВ = 4000          # 2000 обрезали длинный список рекомендаций посреди JSON (PR #3 Дня 32)
ТЕМПЕРАТУРА = 0.1
КРУГОВ_ИНСТРУМЕНТА = 2
ПРЕДЕЛ_ИНСТРУМЕНТА = 8000
ТАЙМАУТ_РЕВЬЮЕРА = 150.0
ТАЙМАУТ_ЗАПРОСА = 90.0
СЕРЬЁЗНОСТИ = ("критично", "важно", "мелочь")

ИНСТРУМЕНТ = {"type": "function", "function": {
    "name": "git_show",
    "description": ("Текст файла репозитория в коммите базы или головы этого PR (только чтение). "
                    "ref — хэш базы или головы из данных PR; start_line/end_line — диапазон строк."),
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "путь файла"},
        "ref": {"type": "string", "description": "хэш базы или головы PR"},
        "start_line": {"type": "integer", "minimum": 0},
        "end_line": {"type": "integer", "minimum": 0}},
        "required": ["path", "ref"]}}}


@dataclass
class Находка:
    раздел: str
    файл: str
    строка: int
    серьёзность: str
    суть: str
    почему: str = ""
    исправить: str = ""
    фрагмент_diff: str = ""
    источники: list[int] = field(default_factory=list)
    подтверждена: bool = False
    пометки: list[str] = field(default_factory=list)
    разделы: list[str] = field(default_factory=list)   # кто ещё нашёл то же (после объединения)

    def to_dict(self) -> dict[str, Any]:
        return {"раздел": self.раздел, "файл": self.файл, "строка": self.строка,
                "серьёзность": self.серьёзность, "суть": self.суть, "почему": self.почему,
                "исправить": self.исправить, "фрагмент_diff": self.фрагмент_diff,
                "источники": list(self.источники), "подтверждена": self.подтверждена,
                "пометки": list(self.пометки), "разделы": list(self.разделы)}


@dataclass
class ИтогРевьюера:
    раздел: str
    ок: bool = False
    находки: list[Находка] = field(default_factory=list)
    итог: str = ""
    ошибка: str = ""
    секунд: float = 0.0
    токены_вход: int = 0
    токены_выход: int = 0
    стоимость: float = 0.0
    вызовов: int = 0
    инструментов: int = 0
    повтор_формата: bool = False

    def метрики(self) -> dict[str, Any]:
        return {"ок": self.ок, "находок": len(self.находки), "секунд": round(self.секунд, 1),
                "токены_вход": self.токены_вход, "токены_выход": self.токены_выход,
                "стоимость": round(self.стоимость, 5), "вызовов": self.вызовов,
                "инструментов": self.инструментов, "повтор_формата": self.повтор_формата,
                "ошибка": self.ошибка[:120]}


class ОшибкаФормата(ValueError):
    pass


def _json(текст: str) -> dict[str, Any]:
    т = (текст or "").strip()
    т = re.sub(r"^```(?:json)?\s*|\s*```$", "", т)
    начало, конец = т.find("{"), т.rfind("}")
    if начало < 0 or конец <= начало:
        raise ОшибкаФормата("нет объекта JSON")
    try:
        данные = json.loads(т[начало:конец + 1])
    except json.JSONDecodeError as e:
        raise ОшибкаФормата(f"JSON не разобран: {e}") from e
    if not isinstance(данные, dict) or not isinstance(данные.get("находки", []), list):
        raise ОшибкаФормата("нет списка «находки»")
    return данные


def разобрать(раздел: str, текст: str) -> tuple[list[Находка], str]:
    """JSON ревьюера → находки. Записи без файла или сути отбрасываются, серьёзность и номера
    приводятся к допустимым; «критично» в рекомендациях понижается до «важно»."""
    данные = _json(текст)
    итог = []
    for з in данные.get("находки") or []:
        if not isinstance(з, dict):
            continue
        файл = str(з.get("файл") or "").strip().lstrip("./")
        суть = str(з.get("суть") or "").strip()
        if not файл or not суть:
            continue
        серьёзность = str(з.get("серьёзность") or "важно").strip().lower()
        if серьёзность not in СЕРЬЁЗНОСТИ:
            серьёзность = "важно"
        if раздел == "рекомендации" and серьёзность == "критично":
            серьёзность = "важно"
        try:
            строка = int(з.get("строка") or 0)
        except (TypeError, ValueError):
            строка = 0
        сырые = з.get("источники") or []
        # Номера бывают списком чисел, списком строк «[2]» или одной строкой «[1, 2]».
        источники = list(dict.fromkeys(int(ч) for ч in re.findall(r"\d+", json.dumps(сырые))))[:12]
        итог.append(Находка(раздел, файл, max(0, строка), серьёзность, суть[:400],
                            str(з.get("почему") or "").strip()[:800], str(з.get("исправить") or "").strip()[:600],
                            str(з.get("фрагмент_diff") or "").strip()[:400], источники))
    return итог, str(данные.get("итог") or "").strip()[:300]


class Ревьюер:
    def __init__(self, раздел: str, клиент: Any, модель: str = МОДЕЛЬ, git: Any = None,
                 описание_проекта: str = "", замок: threading.Lock | None = None) -> None:
        self.раздел = раздел
        self.клиент = клиент
        self.модель = модель
        self.git = git
        self.описание = описание_проекта
        self.замок = замок or threading.Lock()

    def _звать(self, итог: ИтогРевьюера, сообщения: list[dict[str, Any]], с_инструментом: bool) -> Any:
        r = self.клиент.call(self.модель, сообщения, max_tokens=ОТВЕТ_ТОКЕНОВ, temperature=ТЕМПЕРАТУРА,
                             tools=[ИНСТРУМЕНТ] if с_инструментом else None)
        итог.вызовов += 1
        итог.токены_вход += r.prompt_tokens
        итог.токены_выход += r.completion_tokens
        итог.стоимость += r.cost
        return r

    def _инструмент(self, вход: ВходPR, аргументы: dict[str, Any]) -> str:
        ссылка = str(аргументы.get("ref") or "")
        if ссылка not in (вход.база, вход.голова) and not any(
                len(ссылка) >= 7 and полный.startswith(ссылка) for полный in (вход.база, вход.голова)):
            return f"Можно только база ({вход.база[:12]}) или голова ({вход.голова[:12]}) этого PR."
        if self.git is None:
            return "git недоступен."
        with self.замок:
            ок, текст, данные = self.git.вызвать("git__git_show", {
                "path": str(аргументы.get("path") or ""), "ref": ссылка,
                "start_line": int(аргументы.get("start_line") or 0), "end_line": int(аргументы.get("end_line") or 0),
                "limit": ПРЕДЕЛ_ИНСТРУМЕНТА})
        if not ок:
            return f"Ошибка: {текст[:300]}"
        вывод = prompts.обезвредить(данные.get("текст", текст))
        if данные.get("обрезано"):
            вывод += f"\n… обрезано до {ПРЕДЕЛ_ИНСТРУМЕНТА} символов — сузьте start_line/end_line"
        return pd.скрыть(вывод, профиль="точный")[0]

    def выполнить(self, вход: ВходPR, опора: Опора) -> ИтогРевьюера:
        итог = ИтогРевьюера(self.раздел)
        старт = time.monotonic()
        сообщения: list[dict[str, Any]] = [
            {"role": "system", "content": prompts.роль(self.раздел, self.описание)},
            {"role": "user", "content": prompts.сообщение(вход, опора.для(self.раздел), опора.функции)}]
        try:
            r = None
            for круг in range(КРУГОВ_ИНСТРУМЕНТА + 1):
                r = self._звать(итог, сообщения, с_инструментом=круг < КРУГОВ_ИНСТРУМЕНТА and self.git is not None)
                if not r.tool_calls:
                    break
                сообщения.append(r.message or {"role": "assistant", "content": r.text or "", "tool_calls": [
                    {"id": в.ид, "type": "function",
                     "function": {"name": в.имя, "arguments": json.dumps(в.аргументы, ensure_ascii=False)}}
                    for в in r.tool_calls]})
                for в in r.tool_calls:
                    итог.инструментов += 1
                    ответ = (f"аргументы не разобраны: {в.ошибка}" if в.ошибка else
                             self._инструмент(вход, в.аргументы) if в.имя == "git_show" else
                             f"Инструмента «{в.имя}» нет; есть только git_show.")
                    сообщения.append({"role": "tool", "tool_call_id": в.ид, "content": ответ})
            try:
                итог.находки, итог.итог = разобрать(self.раздел, r.text)
            except ОшибкаФормата:
                итог.повтор_формата = True
                оборван = getattr(r, "finish_reason", "") == "length"
                сообщения += [{"role": "assistant", "content": r.text or ""},
                              {"role": "user", "content": prompts.ПОДСКАЗКА_ДЛИНЫ if оборван else prompts.ПОДСКАЗКА_ФОРМАТА}]
                r = self._звать(итог, сообщения, с_инструментом=False)
                итог.находки, итог.итог = разобрать(self.раздел, r.text)
            итог.ок = True
        except ОшибкаФормата as e:
            итог.ошибка = f"ответ не в формате JSON дважды ({e})"
        except Exception as e:  # noqa: BLE001 — сбой ревьюера — пометка в ревью, а не падение конвейера
            итог.ошибка = f"{type(e).__name__}: {str(e)[:200]}"
        итог.секунд = time.monotonic() - старт
        return итог


def выполнить_все(вход: ВходPR, опора: Опора, клиент_для: Callable[[], Any], модель: str = МОДЕЛЬ,
                  git: Any = None, описание_проекта: str = "", разделы: tuple[str, ...] = РЕВЬЮЕРЫ,
                  таймаут: float = ТАЙМАУТ_РЕВЬЮЕРА,
                  этап: Callable[[str, str], None] | None = None) -> list[ИтогРевьюера]:
    """Ревьюеры одновременно; у каждого свой клиент модели, git-сервер общий под замком
    (сеанс stdio MCP не делится между потоками без очереди)."""
    замок = threading.Lock()
    ревьюеры = [Ревьюер(р, клиент_для(), модель, git, описание_проекта, замок) for р in разделы]
    итоги: dict[str, ИтогРевьюера] = {}
    пул = ThreadPoolExecutor(max_workers=len(ревьюеры), thread_name_prefix="ревьюер")
    try:
        задачи = {р.раздел: пул.submit(р.выполнить, вход, опора) for р in ревьюеры}
        срок = time.monotonic() + таймаут
        for раздел, задача in задачи.items():
            try:
                итоги[раздел] = задача.result(timeout=max(0.1, срок - time.monotonic()))
            except ТаймаутПотока:
                итоги[раздел] = ИтогРевьюера(раздел, ошибка=f"не уложился в {таймаут:.0f} с", секунд=таймаут)
            if этап:
                этап(раздел, "готов" if итоги[раздел].ок else "не выполнен")
    finally:
        пул.shutdown(wait=False, cancel_futures=True)
    return [итоги[р] for р in разделы]
