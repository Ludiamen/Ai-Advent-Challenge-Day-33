"""Лексический поиск BM25 по фрагментам индекса — без модели эмбеддингов (День 32, plan D-03).

Ревью PR работает на пустом облачном раннере GitHub: bge-m3 там пришлось бы ставить и
считать заново (на этой машине ≈ 5,5 с на фрагмент, весь репозиторий — часы). Для ревью
кода слова и идентификаторы значат больше, чем смысловая близость: изменённая функция
`скрыть` должна найти документ, где сказано, что `скрыть` обязательна на выходе бота.

Токены: слова и идентификаторы целиком, их части (snake_case и CamelCase), у слов длиннее
пяти букв — ещё основа из пяти первых букв (грубая, но без словаря работает и для русского,
как в эмбеддере «хэш»). Вес — BM25 (k1 = 1,2, b = 0,75), название и раздел фрагмента —
вдвойне: «def скрыть» в паспорте фрагмента важнее упоминания в тексте.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

K1 = 1.2
B = 0.75
ОСНОВА = 5

_СЛОВО = re.compile(r"[0-9A-Za-zА-Яа-яЁё_]+")
_ГОРБ = re.compile(r"[A-ZА-ЯЁ]?[a-zа-яё0-9]+|[A-ZА-ЯЁ]+(?![a-zа-яё])")
# Служебные слова Python и самые частые русские — шум для поиска по коду и документам.
_СТОП = frozenset("""
self none true false return def class import from as if else elif for in while try except finally
with not and or is pass raise lambda yield global str int dict list tuple bool any float
и в во на с со по к ко о об от до из за для не ни что это как так же бы ли то а но или
""".split())


def токены(текст: str) -> list[str]:
    итог: list[str] = []
    for слово in _СЛОВО.findall(текст):
        нижнее = слово.lower()
        части = [ч for ч in нижнее.split("_") if ч]
        if any(с.isupper() for с in слово[1:]):
            части += [ч.lower() for ч in _ГОРБ.findall(слово)]
        for т in {нижнее, *части}:
            if len(т) < 2 or т in _СТОП or т.isdigit():
                continue
            итог.append(т)
            if len(т) > ОСНОВА and not т.isascii():
                итог.append(т[:ОСНОВА] + "~")
    return итог


@dataclass
class Находка:
    чанк: Any
    оценка: float
    место: int


class ЛексическийПоиск:
    """BM25 по списку фрагментов (`Чанк` из `agent.rag.chunking` или любой объект с `.текст`,
    `.название`, `.раздел`, `.источник`). Строится за доли секунды на тысячи фрагментов."""

    def __init__(self, чанки: Iterable[Any]) -> None:
        self.чанки = list(чанки)
        self._частоты: list[Counter[str]] = []
        self._длины: list[int] = []
        документов: Counter[str] = Counter()
        for ч in self.чанки:
            паспорт = f"{getattr(ч, 'название', '')} {getattr(ч, 'раздел', '')} {getattr(ч, 'источник', '')}"
            частоты = Counter(токены(ч.текст))
            for т in токены(паспорт):
                частоты[т] += 2
            self._частоты.append(частоты)
            self._длины.append(sum(частоты.values()))
            документов.update(частоты.keys())
        n = max(1, len(self.чанки))
        self._средняя = (sum(self._длины) / n) if self._длины else 1.0
        self._idf = {т: math.log(1 + (n - d + 0.5) / (d + 0.5)) for т, d in документов.items()}

    def найти(self, запрос: str, сколько: int = 10, исключить: Iterable[str] = ()) -> list[Находка]:
        """Лучшие фрагменты по запросу; «исключить» — источники (пути), которых в выдаче быть не должно."""
        слова = Counter(токены(запрос))
        if not слова:
            return []
        запрет = set(исключить)
        оценки: list[tuple[float, int]] = []
        for i, частоты in enumerate(self._частоты):
            if getattr(self.чанки[i], "источник", "") in запрет:
                continue
            норма = K1 * (1 - B + B * self._длины[i] / self._средняя)
            сумма = 0.0
            for т, вес in слова.items():
                f = частоты.get(т)
                if f:
                    сумма += вес * self._idf[т] * f * (K1 + 1) / (f + норма)
            if сумма > 0:
                оценки.append((сумма, i))
        оценки.sort(key=lambda x: -x[0])
        return [Находка(self.чанки[i], round(о, 4), место + 1) for место, (о, i) in enumerate(оценки[:сколько])]
