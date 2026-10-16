"""Шлюз приватного сервиса Дня 30 (spec FR-01…FR-17, plan D-01…D-18).

Порядок проверок у каждого запроса: подсеть клиента → заявленный размер тела →
ключ и права → лимит ключа (ручки /v1/*) → фактический размер тела и входа в токенах
(до вызова модели) →
очередь (одна генерация за раз, ждут ≤ N). Всё, что не прошло, — ошибка в
формате OpenAI, текст по-русски. Модель — одна (`loc-rag-docs`); в Ollama
шлюз ходит так же, как агент: /api/chat, think: false, keep_alive, окно и предел.

  Сервис(настройки, ключи, журнал, токенизатор, ollama, …) — состояние
  создать_приложение(сервис) -> FastAPI
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.requests import ClientDisconnect

from agent.service.config import КОРЕНЬ, Настройки
from agent.service.journal import Журнал
from agent.service.keys import Ключи
from agent.service.limits import Ведро, Очередь, ОчередьПолна, Подсети
from agent.service.tokens import Токенизатор

РОЛИ = ("system", "user", "assistant")
СООБЩЕНИЕ_OLLAMA_НЕДОСТУПНА = "Локальная модель временно недоступна."
СООБЩЕНИЕ_ДОКУМЕНТЫ_НЕДОСТУПНЫ = "Ответ по документам временно недоступен."
СООБЩЕНИЕ_ПРОЕКТ_НЕДОСТУПЕН = "Ассистент проекта временно недоступен."
СООБЩЕНИЕ_TELEGRAM_НЕДОСТУПЕН = "Операция Telegram временно недоступна."


class КлиентУшёл(Exception):
    pass


def ошибка(код: int, сообщение: str, тип: str, code: str, заголовки: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": сообщение, "type": тип, "code": code}}, status_code=код,
                        headers=заголовки or {})


def _текст(содержимое: Any) -> str | None:
    """content OpenAI: строка или список частей {"type": "text", "text": …}."""
    if isinstance(содержимое, str):
        return содержимое
    if isinstance(содержимое, list):
        части = []
        for ч in содержимое:
            if not isinstance(ч, dict) or ч.get("type") != "text" or not isinstance(ч.get("text"), str):
                return None
            части.append(ч["text"])
        return "".join(части)
    return None


async def _json_объект(request: Request, предел: int) -> dict[str, Any] | JSONResponse:
    """Read at most ``предел`` bytes, then parse a JSON object."""
    def неконечная_константа(значение: str) -> None:
        raise ValueError(f"Недопустимая JSON-константа: {значение}")

    тело = bytearray()
    прочитано = 0
    try:
        async for кусок in request.stream():
            прочитано += len(кусок)
            if прочитано > предел:
                request.state.журнал["ошибка"] = "request_too_large"
                return ошибка(413, f"Тело запроса больше предела {предел} байт.",
                              "invalid_request_error", "request_too_large")
            тело.extend(кусок)
    except ClientDisconnect:
        return ошибка(400, "Тело запроса передано не полностью.", "invalid_request_error",
                      "incomplete_request")
    try:
        данные = json.loads(тело, parse_constant=неконечная_константа)
    except (UnicodeError, ValueError, RecursionError):
        return ошибка(400, "Тело запроса — не JSON.", "invalid_request_error", "invalid_json")
    if not isinstance(данные, dict):
        return ошибка(400, "Тело JSON должно быть объектом.", "invalid_request_error",
                      "invalid_request_body")
    def есть_суррогат(значение: Any) -> bool:
        стек = [значение]
        while стек:
            текущее = стек.pop()
            if isinstance(текущее, str) and any(0xD800 <= ord(с) <= 0xDFFF for с in текущее):
                return True
            if isinstance(текущее, list):
                стек.extend(текущее)
            elif isinstance(текущее, dict):
                стек.extend(текущее.keys())
                стек.extend(текущее.values())
        return False
    if есть_суррогат(данные):
        return ошибка(400, "JSON содержит недопустимый Unicode.", "invalid_request_error", "invalid_json")
    return данные


def _целое(значение: Any) -> int | None:
    """An integer or the integer string accepted by the previous gateway."""
    if isinstance(значение, bool):
        return None
    if isinstance(значение, int):
        return значение
    if isinstance(значение, str) and re.fullmatch(r"[+-]?\d+", значение.strip()):
        try:
            return int(значение)
        except ValueError:
            return None
    return None


def _число(значение: Any) -> float | None:
    """A finite JSON number; numeric strings remain backwards compatible."""
    if isinstance(значение, bool) or not isinstance(значение, (int, float, str)):
        return None
    try:
        число = float(значение)
    except (OverflowError, ValueError):
        return None
    return число if math.isfinite(число) else None


class Сервис:
    """Состояние шлюза: настройки, ключи, лимиты, очередь, журнал и клиенты.

    ollama — httpx.AsyncClient с base_url Ollama (в тестах — с MockTransport);
    отвечатель — фабрика ответа по документам (в тестах — подставная)."""

    def __init__(self, настройки: Настройки, ключи: Ключи, журнал: Журнал, токенизатор: Токенизатор,
                 ollama: httpx.AsyncClient, отвечатель: Callable[[str], dict[str, Any]] | None = None,
                 состояние: Callable[[], dict[str, Any]] | None = None,
                 шаг: Callable[[str, str], dict[str, Any]] | None = None,
                 нагрузка: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                 телеграм: Callable[[str], dict[str, Any]] | None = None,
                 помощник: Callable[[str], dict[str, Any]] | None = None,
                 режим_проекта: Callable[[str | None], dict[str, Any]] | None = None) -> None:
        self.настройки = настройки
        self.ключи = ключи
        self.журнал = журнал
        self.токенизатор = токенизатор
        self.ollama = ollama
        self.подсети = Подсети(настройки.подсети)
        л = настройки.лимиты
        self.ведро = Ведро(л.запросов_в_минуту)
        self.очередь = Очередь(л.очередь)
        self.отвечатель = отвечатель
        self.помощник = помощник          # ассистент разработчика: /v1/project/help (День 31)
        self.режим_проекта = режим_проекта  # None — прочитать; «облако» / «локально» — задать
        self.телеграм = телеграм          # служба Telegram-бота: старт / стоп / перезапуск (telegram-plan D-T12)
        self.доп_состояние = состояние
        self.шаг = шаг
        self.нагрузка = нагрузка

    # --- ключи и лимит ---------------------------------------------------------------

    def ключ(self, request: Request, право: str | None) -> dict[str, Any] | JSONResponse:
        заголовок = request.headers.get("authorization", "")
        if not заголовок.lower().startswith("bearer "):
            request.state.журнал["ошибка"] = "missing_api_key"
            return ошибка(401, "Нужен ключ API: заголовок Authorization: Bearer sk-day30-…", "authentication_error",
                          "missing_api_key", {"WWW-Authenticate": "Bearer"})
        запись = self.ключи.проверить(заголовок[7:].strip())
        if запись is None:
            request.state.журнал["ошибка"] = "invalid_api_key"
            return ошибка(401, "Ключ API неизвестен или отозван.", "authentication_error", "invalid_api_key",
                          {"WWW-Authenticate": "Bearer"})
        request.state.ключ = запись["имя"]
        if право and право not in запись["права"]:
            request.state.журнал["ошибка"] = "permission_denied"
            return ошибка(403, f"У ключа «{запись['имя']}» нет права «{право}».", "permission_error",
                          "permission_denied")
        return запись

    def лимит(self, request: Request, запись: dict[str, Any]) -> JSONResponse | None:
        ок, ждать = self.ведро.взять(запись["имя"])
        if ок:
            return None
        request.state.журнал["ошибка"] = "rate_limited"
        секунд = max(1, int(ждать + 0.999))
        в_минуту = self.настройки.лимиты.запросов_в_минуту
        return ошибка(429, f"Лимит ключа «{запись['имя']}» — {в_минуту} запросов в минуту. Повторите через {секунд} с.",
                      "rate_limit_error", "rate_limited",
                      {"Retry-After": str(секунд), "X-RateLimit-Limit": str(в_минуту), "X-RateLimit-Remaining": "0"})

    def полна(self, засчитать: bool = True) -> JSONResponse:
        """429 queue_full. засчитать=False — отказ уже посчитала сама очередь (гонка за место)."""
        if засчитать:
            self.очередь.отказано += 1
        с = self.очередь.состояние()
        повтор = self.очередь.повтор_через()
        return ошибка(429, f"Очередь полна: идёт генерация и ждут {с['ждут']} из {с['мест_в_очереди']}. "
                           f"Повторите через {повтор} с.", "rate_limit_error", "queue_full",
                      {"Retry-After": str(повтор)})

    # --- Ollama ----------------------------------------------------------------------

    def тело_ollama(self, сообщения: list[dict[str, str]], предел: int, температура: float, поток: bool) -> dict:
        н = self.настройки
        return {"model": н.модель, "messages": сообщения, "stream": поток, "think": False, "keep_alive": н.keep_alive,
                "options": {"num_ctx": н.лимиты.окно, "num_predict": предел, "temperature": температура}}

    async def прогреть(self) -> None:
        """Модель — в память до первого запроса: живой сервис не платит загрузку на посетителе."""
        try:
            await self.ollama.post("/api/chat", json={"model": self.настройки.модель, "messages": [],
                                                      "keep_alive": self.настройки.keep_alive}, timeout=300)
        except httpx.HTTPError:
            pass

    async def с_обрывом(self, request: Request, работа: Any) -> Any:
        """Ждать работу, пока клиент на связи; ушёл — работа отменяется (очередь освобождается)."""
        задача = asyncio.ensure_future(работа)
        try:
            while True:
                готово, _ = await asyncio.wait({задача}, timeout=0.5)
                if готово:
                    return задача.result()
                if await request.is_disconnected():
                    задача.cancel()
                    raise КлиентУшёл()
        except asyncio.CancelledError:
            задача.cancel()
            raise


def создать_приложение(с: Сервис) -> FastAPI:
    app = FastAPI(title="День 30 — приватный AI-сервис", docs_url=None, redoc_url=None,
                  description="OpenAI-совместимый шлюз перед локальной Ollama (loc-rag-docs).")
    л = с.настройки.лимиты

    @app.middleware("http")
    async def сеть_и_размер(request: Request, следующий):
        адрес = request.client.host if request.client else ""
        начало = time.monotonic()
        if not с.подсети.разрешён(адрес):
            с.журнал.записать(адрес=адрес, ручка=request.url.path, код=403, ошибка="forbidden_network")
            return ошибка(403, f"Адрес {адрес} не входит в разрешённые подсети сервиса.", "permission_error",
                          "forbidden_network")
        размер = request.headers.get("content-length")
        цифры = (размер or "").lstrip("0") or "0"
        предел_строкой = str(л.тело_байт)
        заявлено_больше = bool(размер and размер.isdigit() and
                                (len(цифры) > len(предел_строкой) or
                                 len(цифры) == len(предел_строкой) and цифры > предел_строкой))
        if заявлено_больше:
            с.журнал.записать(адрес=адрес, ручка=request.url.path, код=413, ошибка="request_too_large")
            return ошибка(413, f"Заявленное тело запроса больше предела {л.тело_байт} байт.",
                          "invalid_request_error", "request_too_large")
        request.state.ключ = ""
        request.state.журнал = {}
        ответ = await следующий(request)
        if not request.url.path.startswith(("/admin/api/state", "/health")) and not request.state.журнал.get("поток"):
            с.журнал.записать(адрес=адрес, ключ=request.state.ключ, ручка=request.url.path, код=ответ.status_code,
                              длительность_с=round(time.monotonic() - начало, 2), **request.state.журнал)
        return ответ

    # --- открытые ---------------------------------------------------------------------

    @app.get("/health")
    async def здоровье() -> dict[str, Any]:
        return {"status": "ok", "model": с.настройки.модель, "queue": с.очередь.состояние(),
                "time": time.strftime("%Y-%m-%dT%H:%M:%S")}

    @app.get("/chat", response_class=HTMLResponse)
    async def чат_страница() -> HTMLResponse:
        return HTMLResponse(Path(КОРЕНЬ, "templates", "service_chat.html").read_text(encoding="utf-8"))

    @app.get("/admin", response_class=HTMLResponse)
    async def админ_страница() -> HTMLResponse:
        return HTMLResponse(Path(КОРЕНЬ, "templates", "service_admin.html").read_text(encoding="utf-8"))

    # --- /v1 ------------------------------------------------------------------------------

    @app.get("/v1/models")
    async def модели(request: Request):
        запись = с.ключ(request, None)
        if isinstance(запись, JSONResponse):
            return запись
        if (отказ := с.лимит(request, запись)) is not None:
            return отказ
        return {"object": "list", "data": [{"id": с.настройки.модель, "object": "model", "created": 0,
                                             "owned_by": "day30-private"}]}

    @app.post("/v1/chat/completions")
    async def чат(request: Request):
        запись = с.ключ(request, "чат")
        if isinstance(запись, JSONResponse):
            return запись
        if (отказ := с.лимит(request, запись)) is not None:
            return отказ
        тело = await _json_объект(request, л.тело_байт)
        if isinstance(тело, JSONResponse):
            return тело
        модель = тело.get("model")
        if модель is None or модель == "":
            модель = с.настройки.модель
        if not isinstance(модель, str):
            return ошибка(400, "model — непустая строка.", "invalid_request_error", "invalid_model")
        if модель != с.настройки.модель:
            return ошибка(404, f"Модели «{модель}» нет. Есть одна: {с.настройки.модель}.", "invalid_request_error",
                          "model_not_found")
        if тело.get("stream") is not None and not isinstance(тело["stream"], bool):
            return ошибка(400, "stream — true или false.", "invalid_request_error", "invalid_stream")
        сырые_сообщения = тело.get("messages")
        if not isinstance(сырые_сообщения, list):
            return ошибка(400, "messages — список {role: system|user|assistant, content: текст}.",
                          "invalid_request_error", "invalid_messages")
        сообщения = []
        for м in сырые_сообщения:
            текст = _текст(м.get("content")) if isinstance(м, dict) else None
            if not isinstance(м, dict) or м.get("role") not in РОЛИ or текст is None:
                return ошибка(400, "messages — список {role: system|user|assistant, content: текст}.",
                              "invalid_request_error", "invalid_messages")
            сообщения.append({"role": м["role"], "content": текст})
        if not сообщения:
            return ошибка(400, "Пустой список messages.", "invalid_request_error", "invalid_messages")
        вход = await asyncio.to_thread(с.токенизатор.вход, сообщения)
        request.state.журнал["вход"] = вход
        if вход > л.вход_токенов:
            request.state.журнал["ошибка"] = "context_length_exceeded"
            return ошибка(413, f"Вход {вход} токенов больше предела {л.вход_токенов} (окно {л.окно} − ответ "
                               f"{л.ответ_токенов} − запас). Сократите историю или текст.",
                          "invalid_request_error", "context_length_exceeded",
                          {"X-Prompt-Tokens": str(вход), "X-Prompt-Tokens-Limit": str(л.вход_токенов)})
        for имя in ("max_tokens", "max_completion_tokens"):
            if тело.get(имя) is not None and _целое(тело[имя]) is None:
                return ошибка(400, f"{имя} — целое число.", "invalid_request_error",
                              "invalid_max_tokens")
        просили = _целое(тело.get("max_tokens") or тело.get("max_completion_tokens") or
                          л.ответ_токенов)
        if просили is None:  # защита от некорректного лимита в конфигурации
            return ошибка(400, "max_tokens — целое число.", "invalid_request_error",
                          "invalid_max_tokens")
        предел = max(1, min(просили, л.ответ_токенов))
        заголовки = {"X-Prompt-Tokens": str(вход)}
        if просили > л.ответ_токенов:
            заголовки["X-Max-Tokens-Clamped"] = f"{просили}->{л.ответ_токенов}"
        температура = _число(тело.get("temperature") if тело.get("temperature") is not None else 0)
        if температура is None:
            return ошибка(400, "temperature — конечное число.", "invalid_request_error",
                          "invalid_temperature")
        температура = min(2.0, max(0.0, температура))
        позиция = с.очередь.позиция()
        try:
            заявка = с.очередь.записаться()          # место — сразу, до любого await (гонка при всплеске)
        except ОчередьПолна:
            request.state.журнал["ошибка"] = "queue_full"
            return с.полна(засчитать=False)
        заголовки["X-Queue-Position"] = str(позиция)
        тело_ollama = с.тело_ollama(сообщения, предел, температура, bool(тело.get("stream")))
        if тело.get("stream"):
            request.state.журнал["поток"] = True
            взята = {"да": False}

            def сторож() -> None:
                # Поток так и не начался (клиент ушёл до первого байта) — место не держится.
                if not взята["да"]:
                    с.очередь.отменить(заявка)
            asyncio.get_running_loop().call_later(30, сторож)
            return StreamingResponse(_поток(request, тело_ollama, позиция, вход, запись["имя"], заявка, взята),
                                     media_type="text/event-stream", headers=заголовки)
        try:
            ожидание = await с.с_обрывом(request, с.очередь.дождаться(заявка))
        except КлиентУшёл:
            request.state.журнал["ошибка"] = "client_gone"
            return Response(status_code=499)
        начало = time.monotonic()
        try:
            ответ = await с.с_обрывом(request, asyncio.wait_for(
                с.ollama.post("/api/chat", json=тело_ollama, timeout=л.таймаут_с + 10), л.таймаут_с))
            ответ.raise_for_status()
            д = ответ.json()
        except КлиентУшёл:
            request.state.журнал["ошибка"] = "client_gone"
            return Response(status_code=499)
        except asyncio.TimeoutError:
            request.state.журнал["ошибка"] = "timeout"
            return ошибка(504, f"Генерация дольше {int(л.таймаут_с)} с.", "server_error", "timeout")
        except httpx.HTTPError:
            request.state.журнал["ошибка"] = "ollama_error"
            return ошибка(502, СООБЩЕНИЕ_OLLAMA_НЕДОСТУПНА, "server_error", "upstream_error")
        finally:
            с.очередь.выйти(time.monotonic() - начало)
        заголовки["X-Queue-Wait"] = f"{ожидание:.1f}"
        причина = "length" if д.get("done_reason") == "length" else "stop"
        request.state.журнал.update({"ожидание_с": round(ожидание, 2), "ответ": д.get("eval_count"),
                                     "причина": причина, "очередь": позиция})
        вход_ollama, выход = д.get("prompt_eval_count") or 0, д.get("eval_count") or 0
        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion", "created": int(time.time()),
            "model": с.настройки.модель,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": (д.get("message") or {}).get("content", "")},
                         "finish_reason": причина}],
            "usage": {"prompt_tokens": вход_ollama, "completion_tokens": выход, "total_tokens": вход_ollama + выход},
        }, headers=заголовки)

    async def _поток(request: Request, тело_ollama: dict, позиция: int, вход: int, ключ: str,
                     заявка: asyncio.Future, взята: dict) -> AsyncIterator[str]:
        """SSE в формате OpenAI. Пока ждём очередь — комментарии «: очередь N» (клиенты их пропускают)."""
        ид, создан = f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time())
        адрес = request.client.host if request.client else ""
        кусок = lambda delta, причина=None, **ещё: "data: " + json.dumps(
            {"id": ид, "object": "chat.completion.chunk", "created": создан, "model": с.настройки.модель,
             "choices": [{"index": 0, "delta": delta, "finish_reason": причина}], **ещё}, ensure_ascii=False) + "\n\n"
        запись = {"адрес": адрес, "ключ": ключ, "ручка": "/v1/chat/completions", "поток": True, "вход": вход,
                  "очередь": позиция}
        взята["да"] = True
        yield f": очередь {позиция}\n\n"
        вход_в_очередь = asyncio.ensure_future(с.очередь.дождаться(заявка))
        начало = time.monotonic()
        код, ошибка_кода, ожидание, выход, причина = 200, None, 0.0, None, None
        try:
            while True:
                готово, _ = await asyncio.wait({вход_в_очередь}, timeout=2.0)
                if готово:
                    break
                yield f": ждём, впереди {max(0, с.очередь.позиция() - 1)}\n\n"
            ожидание = вход_в_очередь.result()
            генерация = time.monotonic()
            try:
                первый = True
                async with с.ollama.stream("POST", "/api/chat", json=тело_ollama,
                                           timeout=с.настройки.лимиты.таймаут_с + 10) as r:
                    r.raise_for_status()
                    async for строка in r.aiter_lines():
                        if not строка.strip():
                            continue
                        ч = json.loads(строка)
                        if ч.get("done"):
                            причина = "length" if ч.get("done_reason") == "length" else "stop"
                            вход_о, выход = ч.get("prompt_eval_count") or 0, ч.get("eval_count") or 0
                            yield кусок({}, причина, usage={"prompt_tokens": вход_о, "completion_tokens": выход,
                                                             "total_tokens": вход_о + выход},
                                        x_queue_wait=round(ожидание, 1))
                            break
                        текст = (ч.get("message") or {}).get("content", "")
                        if первый:
                            yield кусок({"role": "assistant", "content": текст})
                            первый = False
                        elif текст:
                            yield кусок({"content": текст})
                        if time.monotonic() - генерация > с.настройки.лимиты.таймаут_с:
                            код, ошибка_кода = 504, "timeout"
                            yield "data: " + json.dumps({"error": {"message": "Генерация дольше предела.",
                                                                   "type": "server_error", "code": "timeout"}}) + "\n\n"
                            break
                yield "data: [DONE]\n\n"
            finally:
                с.очередь.выйти(time.monotonic() - генерация)
        except asyncio.CancelledError:
            вход_в_очередь.cancel()
            код, ошибка_кода = 499, "client_gone"
            raise
        except httpx.HTTPError:
            код, ошибка_кода = 502, "ollama_error"
            yield "data: " + json.dumps({"error": {"message": СООБЩЕНИЕ_OLLAMA_НЕДОСТУПНА, "type": "server_error",
                                                   "code": "upstream_error"}}, ensure_ascii=False) + "\n\n"
        finally:
            с.журнал.записать(**запись, код=код, ошибка=ошибка_кода, ожидание_с=round(ожидание, 2),
                              длительность_с=round(time.monotonic() - начало, 2), ответ=выход, причина=причина)

    async def вопрос_в_очередь(request: Request, право: str, отвечатель: Callable[[str], dict[str, Any]] | None,
                               выключено: tuple[str, str], сбой: tuple[str, str], что: str, объект: str,
                               модель: Callable[[dict[str, Any]], str]):
        """Вопрос → синхронный отвечатель в отдельном потоке через общую очередь шлюза.

        Один путь у ответа по документам (День 30) и у ассистента проекта (День 31): ключ с
        правом, лимит, предел тела и вопроса, очередь, обрыв клиента, таймаут, журнал без текстов."""
        запись = с.ключ(request, право)
        if isinstance(запись, JSONResponse):
            return запись
        if (отказ := с.лимит(request, запись)) is not None:
            return отказ
        тело = await _json_объект(request, л.тело_байт)
        if isinstance(тело, JSONResponse):
            return тело
        if тело.get("question") is not None and not isinstance(тело["question"], str):
            return ошибка(400, "question — строка.", "invalid_request_error", "invalid_question")
        вопрос = (тело.get("question") or "").strip()
        if not вопрос:
            return ошибка(400, "Пустой question.", "invalid_request_error", "empty_question")
        if len(вопрос) > л.вопрос_символов:
            request.state.журнал["ошибка"] = "question_too_long"
            return ошибка(413, f"Вопрос {len(вопрос)} символов больше предела {л.вопрос_символов}.",
                          "invalid_request_error", "question_too_long")
        if отвечатель is None:
            return ошибка(503, выключено[0], "server_error", выключено[1])
        позиция = с.очередь.позиция()
        try:
            заявка = с.очередь.записаться()
        except ОчередьПолна:
            request.state.журнал["ошибка"] = "queue_full"
            return с.полна(засчитать=False)
        try:
            ожидание = await с.с_обрывом(request, с.очередь.дождаться(заявка))
        except КлиентУшёл:
            request.state.журнал["ошибка"] = "client_gone"
            return Response(status_code=499)
        # Отвечатель — синхронный код в отдельном потоке. Поток не прервать: место в очереди
        # освобождается, когда он закончит, даже если клиент уже ушёл.
        начало = time.monotonic()
        задача = asyncio.ensure_future(asyncio.to_thread(отвечатель, вопрос))
        задача.add_done_callback(lambda _: с.очередь.выйти(time.monotonic() - начало))
        try:
            итог = await с.с_обрывом(request, asyncio.wait_for(asyncio.shield(задача), л.таймаут_с))
        except КлиентУшёл:
            request.state.журнал["ошибка"] = "client_gone"
            return Response(status_code=499)
        except asyncio.TimeoutError:
            request.state.журнал["ошибка"] = "timeout"
            return ошибка(504, f"{что} дольше {int(л.таймаут_с)} с.", "server_error", "timeout")
        except Exception:  # noqa: BLE001 — ошибка отвечателя: клиенту — безопасный 502, а не обрыв
            request.state.журнал["ошибка"] = сбой[1]
            return ошибка(502, сбой[0], "server_error", сбой[1])
        request.state.журнал.update({"ожидание_с": round(ожидание, 2), "очередь": позиция,
                                     "вход": (итог.get("usage") or {}).get("prompt_tokens"),
                                     "ответ": (итог.get("usage") or {}).get("completion_tokens")})
        return JSONResponse({**итог, "object": объект, "model": модель(итог)},
                            headers={"X-Queue-Wait": f"{ожидание:.1f}", "X-Queue-Position": str(позиция)})

    @app.post("/v1/documents/answer")
    async def документы(request: Request):
        return await вопрос_в_очередь(
            request, "документы", с.отвечатель, ("Ответ по документам на этом сервере выключен.", "documents_disabled"),
            (СООБЩЕНИЕ_ДОКУМЕНТЫ_НЕДОСТУПНЫ, "documents_error"), "Ответ по документам", "documents.answer",
            lambda _: с.настройки.модель)

    @app.post("/v1/project/help")
    async def проект(request: Request):
        """Ассистент разработчика (День 31, spec FR-10): ответ о проекте по документации и git."""
        ответ = await вопрос_в_очередь(
            request, "проект", с.помощник, ("Ассистент проекта на этом сервере выключен.", "project_disabled"),
            (СООБЩЕНИЕ_ПРОЕКТ_НЕДОСТУПЕН, "project_error"), "Ответ ассистента проекта", "project.help",
            lambda итог: str(итог.get("model") or ""))
        if isinstance(ответ, JSONResponse) and ответ.status_code == 200:
            try:
                request.state.журнал["модель"] = json.loads(ответ.body).get("model")
            except (ValueError, AttributeError):
                pass
        return ответ

    # --- служебные (ключ «админ») -------------------------------------------------------

    @app.get("/admin/api/state")
    async def состояние(request: Request):
        запись = с.ключ(request, "админ")
        if isinstance(запись, JSONResponse):
            return запись
        н = с.настройки
        итог = {"service": {"адрес": н.адрес, "порт": н.порт, "подсети": list(н.подсети), "модель": н.модель,
                            "лимиты": н.лимиты.__dict__, "keep_alive": н.keep_alive},
                "queue": с.очередь.состояние(), "keys": с.ключи.список(), "requests": с.журнал.последние(50),
                "clients": с.журнал.клиенты()}
        if с.доп_состояние is not None:
            итог.update(await asyncio.to_thread(с.доп_состояние))
        if с.режим_проекта is not None:
            итог["project"] = await asyncio.to_thread(с.режим_проекта, None)
        return итог

    @app.post("/admin/api/step")
    async def ступень(request: Request):
        запись = с.ключ(request, "админ")
        if isinstance(запись, JSONResponse):
            return запись
        тело = await _json_объект(request, л.тело_байт)
        if isinstance(тело, JSONResponse):
            return тело
        событие, ступень_значение = тело.get("событие"), тело.get("ступень")
        if событие not in ("начало", "конец") or isinstance(ступень_значение, bool) or not isinstance(
                ступень_значение, (str, int)) or not str(ступень_значение).strip():
            return ошибка(400, "Нужны событие «начало» или «конец» и непустая ступень.",
                          "invalid_request_error", "invalid_step")
        if с.шаг is None:
            return {"ok": False}
        return await asyncio.to_thread(с.шаг, событие, str(ступень_значение))

    @app.post("/admin/api/load")
    async def нагрузка(request: Request):
        запись = с.ключ(request, "админ")
        if isinstance(запись, JSONResponse):
            return запись
        тело = await _json_объект(request, л.тело_байт)
        if isinstance(тело, JSONResponse):
            return тело
        if "ступени" in тело:
            ступени = тело["ступени"]
            if not isinstance(ступени, list) or not ступени or any(
                    _целое(з) is None or _целое(з) <= 0 for з in ступени):
                return ошибка(400, "ступени — непустой список положительных целых чисел.",
                              "invalid_request_error", "invalid_load")
        for имя, минимум in (("клиенты", 1), ("вопросов", 1), ("документы", 0), ("max_tokens", 1)):
            if имя in тело and (_целое(тело[имя]) is None or _целое(тело[имя]) < минимум):
                return ошибка(400, f"{имя} — целое число не меньше {минимум}.",
                              "invalid_request_error", "invalid_load")
        if с.нагрузка is None:
            return ошибка(503, "Нагрузка с этой страницы недоступна.", "server_error", "load_disabled")
        итог = await asyncio.to_thread(с.нагрузка, тело)
        return JSONResponse(итог, status_code=409 if итог.get("error") else 200)

    @app.post("/admin/api/project-model")
    async def модель_проекта(request: Request):
        """Переключатель модели ассистента проекта: «облако» (ds-flash) или «локально» — без перезапуска."""
        запись = с.ключ(request, "админ")
        if isinstance(запись, JSONResponse):
            return запись
        тело = await _json_объект(request, л.тело_байт)
        if isinstance(тело, JSONResponse):
            return тело
        режим = тело.get("режим")
        if режим not in ("облако", "локально"):
            return ошибка(400, "Режим — облако или локально.", "invalid_request_error", "bad_mode")
        if с.режим_проекта is None:
            return ошибка(503, "Ассистент проекта на этом сервере выключен.", "server_error", "project_disabled")
        return JSONResponse(await asyncio.to_thread(с.режим_проекта, режим))

    @app.post("/admin/api/telegram")
    async def телеграм(request: Request):
        запись = с.ключ(request, "админ")
        if isinstance(запись, JSONResponse):
            return запись
        тело = await _json_объект(request, л.тело_байт)
        if isinstance(тело, JSONResponse):
            return тело
        действие = тело.get("действие")
        if not isinstance(действие, str):
            return ошибка(400, "Действие — строка: старт, стоп или перезапуск.",
                          "invalid_request_error", "bad_action")
        if действие not in ("старт", "стоп", "перезапуск"):
            return ошибка(400, "Действие — старт, стоп или перезапуск.", "invalid_request_error", "bad_action")
        if с.телеграм is None:
            return ошибка(503, "Управление ботом с этой страницы недоступно.", "server_error", "telegram_disabled")
        try:
            итог = await asyncio.to_thread(с.телеграм, действие)
        except (RuntimeError, ValueError, OSError):
            return ошибка(409, СООБЩЕНИЕ_TELEGRAM_НЕДОСТУПЕН, "server_error", "telegram_error")
        return JSONResponse(итог)

    return app
