"""Второй сервер Ollama (День 29): свои переменные, служба не трогается.

Параметры сервера — flash attention, тип KV-кэша, число параллельных
запросов — задаются переменными окружения `ollama serve`. Служба Ollama
работает от пользователя ollama, её переменные меняются только через
`sudo systemctl edit`, а владелец решил службу не трогать. Поэтому для
рычагов сервера запускаем второй `ollama serve` от нашего пользователя на
своём порту 11435 (решение владельца, spec § 5):
- модели — из каталога службы (`OLLAMA_MODELS`), только чтение: скачивает
  по-прежнему служба (ворота 0, A-4);
- `OLLAMA_NOPRUNE=1` — второй сервер ничего не чистит в чужом каталоге;
- PID, переменные и журнал — в memory/local/server2.*;
- остановить можно только свой процесс: PID сверяется с командной строкой.

В памяти держим не больше одной 4B-модели: перед загрузкой на одном сервере
модель выгружается на другом (это делает стенд, optbench.py).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from typing import Any

import httpx

ПОРТ = 11435
АДРЕС = f"http://127.0.0.1:{ПОРТ}"
КАТАЛОГ_МОДЕЛЕЙ = "/usr/share/ollama/.ollama/models"
# Какие переменные можно задать второму серверу.
ПЕРЕМЕННЫЕ = ("OLLAMA_FLASH_ATTENTION", "OLLAMA_KV_CACHE_TYPE", "OLLAMA_NUM_PARALLEL", "OLLAMA_KEEP_ALIVE",
              "OLLAMA_CONTEXT_LENGTH", "OLLAMA_MAX_LOADED_MODELS")


class ОшибкаСервера(RuntimeError):
    pass


def _каталог_памяти() -> str:
    from agent.local import tuning
    к = os.getenv("MEMORY_DIR") or os.path.join(tuning.КОРЕНЬ, "memory")
    п = os.path.join(к, "local")
    os.makedirs(п, exist_ok=True)
    return п


def _файл() -> str:
    return os.path.join(_каталог_памяти(), "server2.json")


def журнал() -> str:
    return os.path.join(_каталог_памяти(), "server2.log")


def бинарник() -> str:
    путь = shutil.which("ollama") or "/usr/local/bin/ollama"
    if not os.path.exists(путь):
        raise ОшибкаСервера("Не нашёл программу ollama: второй сервер запускать нечем.")
    return путь


def окружение(переменные: dict[str, str] | None = None) -> dict[str, str]:
    """Окружение второго сервера: свой порт, каталог моделей службы, переменные рычага."""
    лишние = sorted(set(переменные or {}) - set(ПЕРЕМЕННЫЕ))
    if лишние:
        raise ОшибкаСервера(f"Второму серверу нельзя задать {', '.join(лишние)}. Можно: {', '.join(ПЕРЕМЕННЫЕ)}.")
    итог = {к: з for к, з in os.environ.items() if not к.startswith("OLLAMA_")}
    итог.update({"OLLAMA_HOST": f"127.0.0.1:{ПОРТ}",
                 "OLLAMA_MODELS": os.getenv("OLLAMA_MODELS_DIR", КАТАЛОГ_МОДЕЛЕЙ),
                 "OLLAMA_NOPRUNE": "1", "OLLAMA_KEEP_ALIVE": "5m"})
    итог.update({к: str(з) for к, з in (переменные or {}).items()})
    return итог


def _записанное() -> dict[str, Any]:
    try:
        with open(_файл(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _свой(pid: int) -> bool:
    """Это наш `ollama serve`? Чужой процесс с тем же PID не трогаем."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            аргументы = [ч.decode("utf-8", "replace") for ч in f.read().split(b"\0") if ч]
        with open(f"/proc/{pid}/environ", "rb") as f:
            окр = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    return (len(аргументы) >= 2 and аргументы[0].endswith("ollama") and аргументы[1] == "serve"
            and f"OLLAMA_HOST=127.0.0.1:{ПОРТ}" in окр)


def _отвечает(http: httpx.Client | None = None) -> str:
    try:
        return str((http or httpx.Client(timeout=2)).get(f"{АДРЕС}/api/version").json().get("version") or "")
    except (httpx.HTTPError, ValueError):
        return ""


def состояние(http: httpx.Client | None = None) -> dict[str, Any]:
    з = _записанное()
    pid = з.get("pid")
    работает = bool(pid) and _свой(int(pid))
    версия = _отвечает(http) if работает else ""
    from agent.local import resources
    return {"running": работает and bool(версия), "pid": pid if работает else None, "address": АДРЕС,
            "version": версия, "vars": з.get("переменные") or {}, "since": з.get("когда", ""),
            "loaded": resources.в_памяти(АДРЕС, http) if версия else [], "log": журнал()}


def старт(переменные: dict[str, str] | None = None, ждать: float = 30.0) -> dict[str, Any]:
    """Запустить второй сервер с переменными; уже работает с другими — перезапустить."""
    переменные = {к: str(з) for к, з in (переменные or {}).items()}
    сейчас = состояние()
    if сейчас["running"]:
        if сейчас["vars"] == переменные:
            return сейчас
        стоп()
    окр = окружение(переменные)
    with open(журнал(), "ab") as лог:
        процесс = subprocess.Popen([бинарник(), "serve"], env=окр, stdout=лог, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
    with open(_файл(), "w", encoding="utf-8") as f:
        json.dump({"pid": процесс.pid, "переменные": переменные,
                   "когда": time.strftime("%Y-%m-%dT%H:%M:%S")}, f, ensure_ascii=False)
    срок = time.monotonic() + ждать
    while time.monotonic() < срок:
        if процесс.poll() is not None:
            raise ОшибкаСервера(f"Второй сервер завершился сразу (код {процесс.returncode}); журнал: {журнал()}")
        if _отвечает():
            return состояние()
        time.sleep(0.5)
    raise ОшибкаСервера(f"Второй сервер не ответил за {ждать:.0f} с; журнал: {журнал()}")


def стоп(ждать: float = 15.0) -> dict[str, Any]:
    """Остановить свой второй сервер; служба Ollama не трогается."""
    з = _записанное()
    pid = з.get("pid")
    if pid and _свой(int(pid)):
        os.kill(int(pid), signal.SIGTERM)
        срок = time.monotonic() + ждать
        while time.monotonic() < срок and _свой(int(pid)):
            time.sleep(0.3)
        if _свой(int(pid)):
            os.kill(int(pid), signal.SIGKILL)
    try:
        os.remove(_файл())
    except OSError:
        pass
    return {"running": False, "pid": None, "address": АДРЕС}
