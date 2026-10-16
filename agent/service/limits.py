"""Ограничения приватного сервиса (spec FR-06, FR-08, FR-09; plan D-05, D-07, D-08).

  Подсети(список).разрешён(адрес) -> bool     проверка до ключа
  Ведро(в_минуту).взять(ключ) -> (ок, ждать_с) лимит запросов ключа к /v1/*
  Очередь(ждущих).войти() / .выйти()           одна генерация за раз, ждут ≤ N, FIFO;
                                              переполнение — ОчередьПолна сразу
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import statistics
import threading
import time
from collections import deque
from typing import Any, Callable


class Подсети:
    def __init__(self, список: tuple[str, ...] | list[str]) -> None:
        self.сети = [ipaddress.ip_network(с, strict=False) for с in список]

    def разрешён(self, адрес: str) -> bool:
        try:
            ip = ipaddress.ip_address((адрес or "").split("%")[0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip.version == с.version and ip in с for с in self.сети)


class Ведро:
    """Ёмкость R жетонов, пополнение R/60 в секунду; у каждого ключа своё ведро."""

    def __init__(self, в_минуту: int, часы: Callable[[], float] = time.monotonic) -> None:
        self.ёмкость = max(1, int(в_минуту))
        self.скорость = self.ёмкость / 60.0
        self.часы = часы
        self._вёдра: dict[str, tuple[float, float]] = {}
        self._замок = threading.Lock()

    def взять(self, ключ: str) -> tuple[bool, float]:
        with self._замок:
            сейчас = self.часы()
            жетоны, когда = self._вёдра.get(ключ, (float(self.ёмкость), сейчас))
            жетоны = min(self.ёмкость, жетоны + (сейчас - когда) * self.скорость)
            if жетоны >= 1.0:
                self._вёдра[ключ] = (жетоны - 1.0, сейчас)
                return True, 0.0
            self._вёдра[ключ] = (жетоны, сейчас)
            return False, (1.0 - жетоны) / self.скорость

    def остаток(self, ключ: str) -> int:
        with self._замок:
            сейчас = self.часы()
            жетоны, когда = self._вёдра.get(ключ, (float(self.ёмкость), сейчас))
            return int(min(self.ёмкость, жетоны + (сейчас - когда) * self.скорость))


class ОчередьПолна(Exception):
    def __init__(self, повтор_с: int, ждут: int) -> None:
        super().__init__(f"очередь полна: ждут {ждут}")
        self.повтор_с = повтор_с
        self.ждут = ждут


class Очередь:
    """Одно место генерации и не больше N ждущих; порядок — FIFO.

    Ждущий, которого отменили (клиент ушёл), уходит из очереди; если отмена
    пришла в тот момент, когда место уже передано ему, место передаётся дальше."""

    def __init__(self, ждущих: int, средняя_с: float = 60.0, часы: Callable[[], float] = time.monotonic) -> None:
        self.ждущих_макс = max(0, int(ждущих))
        self.часы = часы
        self._занято = False
        self._ждут: deque[asyncio.Future] = deque()
        self._длительности: deque[float] = deque([средняя_с], maxlen=20)
        self.отказано = 0
        self.обслужено = 0

    def состояние(self) -> dict[str, Any]:
        return {"идёт": int(self._занято), "ждут": len(self._ждут), "мест_в_очереди": self.ждущих_макс,
                "средняя_с": round(self.средняя(), 1), "отказано": self.отказано, "обслужено": self.обслужено}

    def средняя(self) -> float:
        return statistics.median(self._длительности)

    def повтор_через(self) -> int:
        return max(1, math.ceil(self.средняя() * (len(self._ждут) + int(self._занято))))

    def свободно(self) -> bool:
        return not self._занято or len(self._ждут) < self.ждущих_макс

    def позиция(self) -> int:
        """Сколько впереди у нового запроса: идущая генерация и ждущие."""
        return int(self._занято) + len(self._ждут)

    def записаться(self) -> asyncio.Future:
        """Занять место СРАЗУ, без await: проверка и запись — одно действие. Иначе при всплеске
        несколько запросов проходили проверку «место есть» раньше, чем хоть один вставал в
        очередь, и лишний узнавал о переполнении уже внутри потока (нагрузка, ступень 8).
        Свободно — готовое будущее; есть место в очереди — ждущее; нет — ОчередьПолна."""
        f: asyncio.Future = asyncio.get_running_loop().create_future()
        if not self._занято and not self._ждут:
            self._занято = True
            f.set_result(True)
            return f
        if len(self._ждут) >= self.ждущих_макс:
            self.отказано += 1
            raise ОчередьПолна(self.повтор_через(), len(self._ждут))
        self._ждут.append(f)
        return f

    async def дождаться(self, f: asyncio.Future) -> float:
        """Секунды ожидания своей очереди; отмена (клиент ушёл) — место не держится."""
        if f.done() and not f.cancelled():
            return 0.0                      # место было свободно сразу
        начало = self.часы()
        try:
            await f
        except asyncio.CancelledError:
            self.отменить(f)
            raise
        return self.часы() - начало

    def отменить(self, f: asyncio.Future) -> None:
        """Снять запись: ждущая — уходит из очереди; уже получившая место — отдаёт его дальше."""
        if f in self._ждут:
            self._ждут.remove(f)
            f.cancel()
        elif f.done() and not f.cancelled():
            self.выйти(None)

    async def войти(self) -> float:
        """Записаться и дождаться; ОчередьПолна — сразу, если ждущих уже N."""
        return await self.дождаться(self.записаться())

    def выйти(self, длительность: float | None) -> None:
        if длительность is not None:
            self._длительности.append(max(1.0, длительность))
            self.обслужено += 1
        while self._ждут:
            f = self._ждут.popleft()
            if not f.done():
                f.set_result(True)          # место переходит к следующему, _занято остаётся
                return
        self._занято = False
