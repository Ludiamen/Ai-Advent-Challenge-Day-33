"""Ollama на этой машине: что запущено, что скачано, как к ней обратиться (День 26).

Задание дня — доказать три вещи: модель запускается ЛОКАЛЬНО, к ней можно
обратиться через CLI или HTTP API, и она отвечает. Этот модуль собирает
доказательства из самой Ollama, а не из наших предположений:

  служба      `systemctl is-active ollama` и версия из GET /api/version;
  адрес       127.0.0.1 — петлевой интерфейс: запрос не выходит с машины;
  модели      GET /api/tags — что скачано: размер, семейство, параметры,
              квантование; файлы весов лежат в каталоге моделей Ollama;
  в памяти    GET /api/ps — какая модель загружена, сколько занимает и где
              считается: size_vram = 0 значит «100 % CPU», видеокарты нет;
  сведения    POST /api/show — окно контекста модели, умения (tools,
              thinking, vision), шаблон;
  CLI         `ollama run МОДЕЛЬ "вопрос" --think=false --verbose` отдельным
              процессом — ответ и замеры самой CLI;
  HTTP        тот же вопрос сырым запросом в родной /api/chat и в
              OpenAI-совместимый /v1/chat/completions — запрос и ответ как есть.

Публичное API:
  адрес(), версия(), модели(), загружены(), сведения(имя), служба()
  выгрузить(имя)                    — освободить память (keep_alive 0)
  каталог_моделей()                 — где лежат веса и сколько места занимают
  состояние()                       — всё вместе, для консоли и страницы
  через_cli(имя, вопрос)            — ответ команды `ollama run`
  через_http(вид, имя, вопрос)      — сырой запрос и ответ (native | openai)
  ОшибкаOllama
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from agent import catalog

КОРОТКО = 5.0           # служебные запросы: версия, список, сведения
КАТАЛОГИ_МОДЕЛЕЙ = ("/usr/share/ollama/.ollama/models", os.path.expanduser("~/.ollama/models"))


class ОшибкаOllama(RuntimeError):
    """Ollama не отвечает или ответила ошибкой."""


def адрес() -> str:
    return catalog._OLLAMA


def петля() -> bool:
    """Адрес Ollama — петлевой интерфейс этой машины."""
    хост = urlparse(адрес()).hostname or ""
    return хост in ("127.0.0.1", "localhost", "::1") or хост.startswith("127.")


def _get(путь: str) -> Any:
    try:
        ответ = httpx.get(f"{адрес()}{путь}", timeout=КОРОТКО)
        ответ.raise_for_status()
        return ответ.json()
    except httpx.ConnectError as exc:
        raise ОшибкаOllama(f"Ollama не отвечает на {адрес()}: запустите `systemctl start ollama` "
                           f"или `ollama serve` ({exc})") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ОшибкаOllama(f"Ollama ответила ошибкой на {путь}: {exc}") from exc


def версия() -> str:
    return str(_get("/api/version").get("version", ""))


def _гб(байт: int | float) -> float:
    return round((байт or 0) / 1e9, 2)


def модели() -> list[dict[str, Any]]:
    """Скачанные модели: имя, размер, семейство, параметры, квантование."""
    итог = []
    for м in _get("/api/tags").get("models", []):
        д = м.get("details") or {}
        итог.append({"name": м.get("name", ""), "size_gb": _гб(м.get("size", 0)),
                     "family": д.get("family", ""), "params": д.get("parameter_size", ""),
                     "quant": д.get("quantization_level", ""), "format": д.get("format", ""),
                     "digest": (м.get("digest") or "")[:12], "modified": м.get("modified_at", "")[:19],
                     "key": _ключ_каталога(м.get("name", ""))})
    return итог


def _ключ_каталога(имя: str) -> str:
    for ключ in catalog.local_models():
        модель = catalog.get(ключ)
        if модель.api_id == имя or f"{модель.api_id}:latest" == имя:
            return ключ
    return ""


def загружены() -> list[dict[str, Any]]:
    """Модели в памяти (GET /api/ps): сколько занимают и где считаются."""
    итог = []
    for м in _get("/api/ps").get("models", []):
        размер, видео = м.get("size", 0) or 0, м.get("size_vram", 0) or 0
        доля_gpu = round(100 * видео / размер) if размер else 0
        итог.append({"name": м.get("name", ""), "size_gb": _гб(размер), "vram_gb": _гб(видео),
                     "processor": "100% CPU" if доля_gpu == 0 else
                                  ("100% GPU" if доля_gpu == 100 else f"{100 - доля_gpu}%/{доля_gpu}% CPU/GPU"),
                     "context": м.get("context_length", 0), "until": (м.get("expires_at") or "")[:19],
                     "key": _ключ_каталога(м.get("name", ""))})
    return итог


def сведения(имя: str) -> dict[str, Any]:
    """POST /api/show: окно контекста модели, умения, параметры по умолчанию."""
    try:
        ответ = httpx.post(f"{адрес()}/api/show", json={"model": имя}, timeout=КОРОТКО)
    except httpx.HTTPError as exc:
        raise ОшибкаOllama(f"Ollama не отвечает на {адрес()}: {exc}") from exc
    if ответ.status_code == 404:
        raise ОшибкаOllama(f"Модель {имя} не скачана: ollama pull {имя}")
    ответ.raise_for_status()
    данные = ответ.json()
    инфо = данные.get("model_info") or {}
    окно = next((з for к, з in инфо.items() if к.endswith(".context_length")), 0)
    д = данные.get("details") or {}
    return {"name": имя, "capabilities": данные.get("capabilities") or [],
            "context_length": окно, "family": д.get("family", ""),
            "params": д.get("parameter_size", ""), "quant": д.get("quantization_level", ""),
            "parameters": (данные.get("parameters") or "").strip(),
            "parameter_count": инфо.get("general.parameter_count", 0)}


def выгрузить(имя: str) -> bool:
    """Выгрузить модель из памяти (keep_alive 0). Ollama молчит — не беда."""
    try:
        ответ = httpx.post(f"{адрес()}/api/generate", json={"model": имя, "keep_alive": 0}, timeout=30.0)
        return ответ.is_success
    except httpx.HTTPError:
        return False


def служба() -> str:
    """Состояние systemd-службы ollama: active | inactive | нет systemd."""
    if not shutil.which("systemctl"):
        return "нет systemctl"
    готово = subprocess.run(["systemctl", "is-active", "ollama"], capture_output=True, text=True, timeout=10)
    return готово.stdout.strip() or готово.stderr.strip()


def каталог_моделей() -> dict[str, Any]:
    """Где Ollama хранит веса и сколько места они занимают на диске."""
    for каталог in [os.getenv("OLLAMA_MODELS", "")] + list(КАТАЛОГИ_МОДЕЛЕЙ):
        blobs = os.path.join(каталог, "blobs") if каталог else ""
        if blobs and os.path.isdir(blobs):
            размер = sum(os.path.getsize(os.path.join(blobs, ф)) for ф in os.listdir(blobs)
                         if not ф.endswith("-partial") and "-partial-" not in ф)
            return {"path": каталог, "size_gb": _гб(размер)}
    return {"path": "", "size_gb": 0.0}


def состояние() -> dict[str, Any]:
    """Всё, что доказывает «модель работает здесь»: служба, адрес, модели, память."""
    итог: dict[str, Any] = {"address": адрес(), "loopback": петля(), "service": служба(),
                            "num_ctx": catalog.LOCAL_CTX, "cli": shutil.which("ollama") or ""}
    try:
        итог.update({"version": версия(), "models": модели(), "loaded": загружены(), "error": ""})
    except ОшибкаOllama as exc:
        итог.update({"version": "", "models": [], "loaded": [], "error": str(exc)})
    итог["disk"] = каталог_моделей()
    скачанные = {м["name"] for м in итог["models"]}
    итог["catalog"] = [{"key": к, "api_id": catalog.get(к).api_id, "label": catalog.get(к).label,
                        "installed": catalog.get(к).api_id in скачанные
                        or f"{catalog.get(к).api_id}:latest" in скачанные}
                       for к in catalog.local_models()]
    return итог


# --- обращение через CLI и через HTTP ---------------------------------------------

# Индикатор ожидания `ollama run` (крутилка ⠋⠙⠹…) и управляющие коды терминала.
_ТЕРМИНАЛ = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[⠀-⣿]")


def через_cli(имя: str, вопрос: str, предел: float = 600) -> dict[str, Any]:
    """`ollama run ИМЯ "вопрос" --think=false --verbose` отдельным процессом.

    Ответ — stdout; --verbose печатает в stderr замеры самой CLI (загрузка,
    чтение промпта, скорость генерации)."""
    if not shutil.which("ollama"):
        raise ОшибкаOllama("Команды ollama нет в PATH — установите Ollama (curl -fsSL https://ollama.com/install.sh | sh).")
    команда = ["ollama", "run", имя, вопрос, "--think=false", "--verbose", "--nowordwrap"]
    начало = time.monotonic()
    готово = subprocess.run(команда, capture_output=True, text=True, timeout=предел,
                            stdin=subprocess.DEVNULL, env={**os.environ, "NO_COLOR": "1"})
    секунд = time.monotonic() - начало
    замеры = {}
    for строка in _ТЕРМИНАЛ.sub("", готово.stderr).splitlines():
        к, _, з = строка.partition(":")
        if к.strip() in ("total duration", "load duration", "prompt eval count", "prompt eval rate",
                         "eval count", "eval rate", "prompt eval duration", "eval duration"):
            замеры[к.strip()] = з.strip()
    return {"command": " ".join(команда[:3]) + f' "{вопрос}" --think=false --verbose',
            "code": готово.returncode, "stdout": _ТЕРМИНАЛ.sub("", готово.stdout).strip(),
            "stderr": "" if готово.returncode == 0 else _ТЕРМИНАЛ.sub("", готово.stderr).strip()[-500:],
            "timings": замеры, "seconds": round(секунд, 1)}


ВИДЫ_HTTP = ("native", "openai")


def через_http(вид: str, имя: str, вопрос: str, предел: float = 600) -> dict[str, Any]:
    """Сырой HTTP-запрос к Ollama и её ответ как есть — родной или OpenAI-совместимый."""
    if вид not in ВИДЫ_HTTP:
        raise ValueError(f"Вид запроса: {', '.join(ВИДЫ_HTTP)}")
    сообщения = [{"role": "user", "content": вопрос}]
    # Окно контекста не задаётся: CLI и /v1 работают с окном Ollama по умолчанию,
    # и смена окна между запросами — это перезагрузка модели (~20 с у 4B).
    # Все три примера доступа делят одну загрузку.
    if вид == "native":
        url = f"{адрес()}/api/chat"
        тело: dict[str, Any] = {"model": имя, "messages": сообщения, "stream": False, "think": False}
    else:
        url = f"{адрес()}/v1/chat/completions"
        # В OpenAI-совместимом API «размышления» выключаются reasoning_effort:
        # иначе модель Qwen сперва думает сотни токенов.
        тело = {"model": имя, "messages": сообщения, "stream": False, "reasoning_effort": "none"}
    начало = time.monotonic()
    try:
        ответ = httpx.post(url, json=тело, timeout=httpx.Timeout(предел, connect=5.0))
    except httpx.HTTPError as exc:
        raise ОшибкаOllama(f"Ollama не отвечает на {url}: {exc}") from exc
    try:
        данные: Any = ответ.json()
    except ValueError:
        данные = ответ.text[:2000]
    return {"kind": вид, "url": url, "request": тело, "status": ответ.status_code,
            "response": данные, "seconds": round(time.monotonic() - начало, 2),
            "curl": f"curl {url} -d '{json.dumps(тело, ensure_ascii=False)}'",
            "text": _текст_ответа(вид, данные)}


def _текст_ответа(вид: str, данные: Any) -> str:
    if not isinstance(данные, dict):
        return ""
    if вид == "native":
        return ((данные.get("message") or {}).get("content") or "").strip()
    выбор = (данные.get("choices") or [{}])[0]
    return ((выбор.get("message") or {}).get("content") or "").strip()
