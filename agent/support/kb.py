"""База знаний поддержки: FAQ + документация продукта, поиск BM25 (День 33, FR-03, FR-04, plan D-04).

Документация — фрагменты индекса CRM Дней 31–32 (`index-projects/crm/docs.db`, ПД уже
замаскированы), FAQ — `support/faq.md`, по вопросу на фрагмент. Всё собирается в
`index-support/chunks.json` (вне git: тексты спек CRM в публичный репозиторий не идут) —
`cli.py --поддержка-индекс`. В Docker-образ и на VPS едет этот файл: модели эмбеддингов там
нет, поиск — BM25 (`agent/rag/lexical.py`), он строится при старте за секунды.

FAQ написан для пользователя и короче спек — при равной близости он должен быть выше:
вес FAQ ×1,3.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from agent.rag.lexical import ЛексическийПоиск

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAQ = os.path.join(КОРЕНЬ, "support", "faq.md")
ИНДЕКС_CRM = os.path.join(КОРЕНЬ, "index-projects", "crm", "docs.db")
ВЕС_FAQ = 1.3
_ИСТОЧНИК = re.compile(r"^Источник:\s*(.+?)\s*$", re.M)


def путь_по_умолчанию() -> str:
    return os.getenv("SUPPORT_KB") or os.path.join(КОРЕНЬ, "index-support", "chunks.json")


@dataclass
class Фрагмент:
    источник: str
    раздел: str
    текст: str
    вид: str = "документация"     # «faq» или «документация»
    название: str = ""

    @property
    def подпись(self) -> str:
        return f"{self.источник} › {self.раздел}" if self.раздел else self.источник

    def to_dict(self) -> dict[str, Any]:
        return {"источник": self.источник, "раздел": self.раздел, "текст": self.текст, "вид": self.вид}


def разобрать_faq(путь: str = FAQ) -> list[Фрагмент]:
    """Вопрос FAQ — фрагмент: заголовок «### …» — раздел, текст — вопрос и ответ с источником."""
    with open(путь, encoding="utf-8") as f:
        текст = f.read()
    итог = []
    for блок in re.split(r"\n(?=### )", текст):
        if not блок.startswith("### "):
            continue
        вопрос = блок.splitlines()[0][4:].strip()
        итог.append(Фрагмент("support/faq.md", вопрос, блок.strip(), "faq"))
    return итог


def источники_faq(путь: str = FAQ) -> list[tuple[str, str]]:
    """(вопрос, строка «Источник: …») — для проверки, что источники FAQ существуют."""
    return [(ф.раздел, (_ИСТОЧНИК.search(ф.текст) or [None, ""])[1]) for ф in разобрать_faq(путь)]


def собрать(путь: str = "", индекс_crm: str = ИНДЕКС_CRM, faq: str = FAQ) -> dict[str, Any]:
    """Собрать `chunks.json` из индекса CRM и FAQ; вернуть отчёт."""
    from agent.rag.store import ХранилищеИндекса
    начало = time.monotonic()
    путь = путь or путь_по_умолчанию()
    документы = [Фрагмент(ч.источник, ч.раздел, ч.текст) for ч in ХранилищеИндекса(индекс_crm).чанки("структура")]
    вопросы = разобрать_faq(faq)
    os.makedirs(os.path.dirname(путь), exist_ok=True)
    временный = путь + ".tmp"
    with open(временный, "w", encoding="utf-8") as f:
        json.dump({"собран": time.strftime("%Y-%m-%dT%H:%M:%S"), "фрагменты": [ф.to_dict() for ф in документы + вопросы]},
                  f, ensure_ascii=False)
    os.replace(временный, путь)
    return {"путь": путь, "документация": len(документы), "faq": len(вопросы),
            "файлов": len({ф.источник for ф in документы}), "секунд": round(time.monotonic() - начало, 1)}


class БазаЗнаний:
    def __init__(self, фрагменты: list[Фрагмент]) -> None:
        self.фрагменты = фрагменты
        self.поиск = ЛексическийПоиск(фрагменты)

    @classmethod
    def загрузить(cls, путь: str = "") -> "БазаЗнаний":
        путь = путь or путь_по_умолчанию()
        with open(путь, encoding="utf-8") as f:
            данные = json.load(f)
        return cls([Фрагмент(д["источник"], д["раздел"], д["текст"], д.get("вид", "документация"))
                    for д in данные["фрагменты"]])

    def найти(self, запрос: str, сколько: int = 6) -> list[tuple[Фрагмент, float]]:
        находки = self.поиск.найти(запрос, сколько * 3)
        оценённые = [(н.чанк, н.оценка * (ВЕС_FAQ if н.чанк.вид == "faq" else 1.0)) for н in находки]
        оценённые.sort(key=lambda п: -п[1])
        return оценённые[:сколько]
