"""Сколько ресурсов съедает ответ локальной модели (День 29).

Ollama 0.34 на каждую загруженную модель запускает отдельный процесс
`llama-server --model <блоб весов>`. Его и меряем:
- ОЗУ — RSS из /proc/<pid>/status; пик — наибольший RSS за ответ, его снимает
  фоновый опрос раз в 0,5 с (VmHWM включал бы и загрузку модели);
- секунды процессора — прирост utime + stime из /proc/<pid>/stat за ответ;
- размер модели в памяти — /api/ps; нагрузка машины — loadavg.

Раннер службы работает от пользователя ollama, но его status и stat читаются
всеми (ворота 0, A-7). Ватты не меряются: счётчик RAPL читается только root.

Блоб весов модели берётся из /api/show (строка FROM модельфайла). У
qwen3.5:4b две строки FROM: веса и визуальный проектор (mmproj); раннер без
проектора — это наша модель «без проектора», с тем же блобом весов. Поэтому
раннер ищется по блобу и по наличию `--mmproj` в командной строке.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

import httpx

ПРОЦ = "/proc"
ШАГ_ОПРОСА = 0.5


def блобы(адрес: str, тег: str, http: httpx.Client | None = None) -> list[str]:
    """Блобы из строк FROM модельфайла: [веса, проектор?] — имена sha256-…"""
    к = http or httpx.Client(timeout=30)
    ответ = к.post(f"{адрес}/api/show", json={"model": тег})
    ответ.raise_for_status()
    текст = ответ.json().get("modelfile") or ""
    return [os.path.basename(м) for м in re.findall(r"^FROM\s+(\S+)", текст, re.M)]


def _командная_строка(pid: int, корень: str = ПРОЦ) -> list[str]:
    try:
        with open(os.path.join(корень, str(pid), "cmdline"), "rb") as f:
            return [ч.decode("utf-8", "replace") for ч in f.read().split(b"\0") if ч]
    except OSError:
        return []


def раннер(блоб: str, с_проектором: bool | None = None, корень: str = ПРОЦ) -> int | None:
    """PID раннера llama-server с этими весами; None — модель не загружена."""
    for имя in os.listdir(корень):
        if not имя.isdigit():
            continue
        аргументы = _командная_строка(int(имя), корень)
        if not аргументы or not аргументы[0].endswith("llama-server"):
            continue
        if not any(блоб in а for а in аргументы):
            continue
        if с_проектором is not None and ("--mmproj" in аргументы) != с_проектором:
            continue
        return int(имя)
    return None


def показания(pid: int, корень: str = ПРОЦ) -> dict[str, float] | None:
    """RSS и пик (МБ), секунды процессора; None — процесса уже нет."""
    try:
        with open(os.path.join(корень, str(pid), "status"), encoding="utf-8") as f:
            статус = f.read()
        with open(os.path.join(корень, str(pid), "stat"), encoding="utf-8") as f:
            стат = f.read()
    except OSError:
        return None

    def кб(поле: str) -> int:
        м = re.search(rf"^{поле}:\s+(\d+)\s+kB", статус, re.M)
        return int(м.group(1)) if м else 0

    # Имя процесса в stat — в скобках и может содержать пробелы: поля считаем
    # после последней закрывающей скобки. utime и stime — 14-е и 15-е поля.
    поля = стат.rsplit(")", 1)[-1].split()
    такт = os.sysconf("SC_CLK_TCK")
    return {"rss_мб": round(кб("VmRSS") / 1024, 1), "hwm_мб": round(кб("VmHWM") / 1024, 1),
            "cpu_с": round((int(поля[11]) + int(поля[12])) / такт, 2)}


def нагрузка() -> float:
    return round(os.getloadavg()[0], 2)


def в_памяти(адрес: str, http: httpx.Client | None = None) -> list[dict[str, Any]]:
    """/api/ps: какие модели загружены и сколько занимают."""
    к = http or httpx.Client(timeout=10)
    try:
        return [{"name": м.get("name", ""), "size_мб": round((м.get("size") or 0) / 2**20),
                 "vram_мб": round((м.get("size_vram") or 0) / 2**20),
                 "context": м.get("context_length")} for м in к.get(f"{адрес}/api/ps").json().get("models") or []]
    except (httpx.HTTPError, ValueError):
        return []


def выгрузить(адрес: str, тег: str, http: httpx.Client | None = None) -> None:
    """Выгрузить модель отдельным запросом (keep_alive 0) — после снятия показаний."""
    к = http or httpx.Client(timeout=120)
    try:
        к.post(f"{адрес}/api/generate", json={"model": тег, "keep_alive": 0})
    except httpx.HTTPError:
        pass


class Наблюдатель:
    """Фоновый опрос раннера во время ответа: пик RSS и прирост секунд процессора.

    with Наблюдатель(блоб, с_проектором) as н: … ответ …; н.итог —
    {"pid", "rss_мб", "пик_мб", "cpu_с", "нагрузка_до", "нагрузка_после"}.
    Раннер может появиться не сразу (модель ещё грузится) — его ищут на каждом
    шаге, пока не найдут."""

    def __init__(self, блоб: str, с_проектором: bool | None = None, корень: str = ПРОЦ,
                 шаг: float = ШАГ_ОПРОСА) -> None:
        self.блоб, self.с_проектором, self.корень, self.шаг = блоб, с_проектором, корень, шаг
        self.pid: int | None = None
        self.пик = 0.0
        self.cpu_начало: float | None = None
        self.последние: dict[str, float] | None = None
        self.итог: dict[str, Any] = {}
        self._стоп = threading.Event()
        self._поток: threading.Thread | None = None
        self._нагрузка_до = 0.0

    def _снять(self) -> None:
        if self.pid is None:
            self.pid = раннер(self.блоб, self.с_проектором, self.корень)
            if self.pid is None:
                return
        п = показания(self.pid, self.корень)
        if п is None:
            return
        if self.cpu_начало is None:
            self.cpu_начало = п["cpu_с"]
        self.пик = max(self.пик, п["rss_мб"])
        self.последние = п

    def _цикл(self) -> None:
        while not self._стоп.is_set():
            self._снять()
            self._стоп.wait(self.шаг)

    def __enter__(self) -> "Наблюдатель":
        self._нагрузка_до = нагрузка()
        self._снять()
        self._поток = threading.Thread(target=self._цикл, daemon=True)
        self._поток.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._стоп.set()
        if self._поток is not None:
            self._поток.join(timeout=5)
        self._снять()
        п = self.последние or {}
        self.итог = {"pid": self.pid, "rss_мб": п.get("rss_мб"), "пик_мб": self.пик or None,
                     "cpu_с": (round(п["cpu_с"] - self.cpu_начало, 2)
                               if п and self.cpu_начало is not None else None),
                     "нагрузка_до": self._нагрузка_до, "нагрузка_после": нагрузка()}
