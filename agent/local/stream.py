"""Потоковый ответ модели — локальной или облачной — с замером на стороне клиента.

Сравнивать локальную модель с облачной по счётчикам провайдеров нельзя: у
Ollama есть замеры сервера (загрузка, чтение промпта, генерация), у облака —
только итог. Поэтому сравнение идёт одним способом для обеих: ответ берётся
потоком, и клиент сам засекает

  до первого токена   от отправки запроса до первого кусочка текста ответа —
                      то, что человек ждёт, глядя на пустой экран;
  всего               до последнего кусочка;
  токены в секунду    токенов ответа на время после первого токена.

У локальной модели к этому добавляются замеры самой Ollama из последней
строки потока — по ним видно, что съело время: загрузка весов в память или
чтение промпта.

Поток локальной модели — NDJSON родного /api/chat; облачной — SSE
OpenAI-совместимого /chat/completions с usage в последнем кусочке.

Публичное API:
  поток(ключ, сообщения, ...)   — генератор событий {"тип": "текст"|"думает"|"итог", ...}
  спросить(ключ, сообщения)     — то же целиком: итоговое событие с полным текстом
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

import httpx

from agent import catalog, tokens
from agent.llm import (LLMError, LOCAL_TIMEOUT, Client, ollama_error, ollama_payload,
                       ollama_timings, _РАЗМЫШЛЕНИЯ)

ПРЕДЕЛ = 900            # токенов ответа по умолчанию
ОБЛАКО_ТАЙМАУТ = 180.0
# Скорость по ответу в одно-два слова — шум: «36» облако присылает одним
# куском, и «1 токен за 0,06 с» превращается в 16 ток/с. Меньше пяти токенов —
# скорость не считается (0, на экране — «—»).
МИН_ТОКЕНОВ_СКОРОСТИ = 5


def _итог(ключ: str, текст: str, начало: float, первый: float | None, вход: int, выход: int,
          стоимость: float, сервер: dict[str, float] | None = None, размышления: str = "",
          причина: str = "") -> dict[str, Any]:
    конец = time.monotonic()
    всего = конец - начало
    ttft = (первый - начало) if первый else всего
    генерация = конец - первый if первый else 0.0
    модель = catalog.get(ключ)
    return {"тип": "итог", "model": ключ, "label": модель.label, "local": модель.local,
            "text": _РАЗМЫШЛЕНИЯ.sub("", текст).strip(), "thinking": размышления.strip(),
            "ttft_s": round(ttft, 2), "total_s": round(всего, 2),
            "tok_s": round(выход / генерация, 1) if генерация > 0.05 and выход >= МИН_ТОКЕНОВ_СКОРОСТИ else 0.0,
            "tokens_in": вход, "tokens_out": выход, "cost": round(стоимость, 6),
            "server": сервер or {}, "finish": причина}


def поток(ключ: str, сообщения: list[dict[str, Any]], предел: int = ПРЕДЕЛ,
          температура: float = 0.2, думать: bool = False, формат: Any = None,
          http: httpx.Client | None = None) -> Iterator[dict[str, Any]]:
    """События ответа по мере генерации; последнее — «итог» с замерами."""
    from agent.local import offline
    модель = catalog.get(ключ)
    offline.проверить_модель(ключ)        # облако в офлайне — отказ до сокета (День 27)
    свой = http is None
    http = http or httpx.Client()
    try:
        if модель.local:
            yield from _поток_ollama(модель, сообщения, предел, температура, думать, формат, http)
        else:
            yield from _поток_облака(модель, сообщения, предел, температура, http)
    finally:
        if свой:
            http.close()


def _поток_ollama(модель, сообщения, предел, температура, думать, формат, http) -> Iterator[dict]:
    тело = ollama_payload(модель, сообщения, предел, температура, None, думать, формат, stream=True)
    начало, первый = time.monotonic(), None
    куски: list[str] = []
    мысли: list[str] = []
    последняя: dict[str, Any] = {}
    try:
        with http.stream("POST", f"{модель.base_url}/api/chat", json=тело,
                         timeout=httpx.Timeout(LOCAL_TIMEOUT, connect=5.0)) as ответ:
            if not ответ.is_success:
                ответ.read()
                raise LLMError(ollama_error(ответ, модель))
            for строка in ответ.iter_lines():
                if not строка.strip():
                    continue
                данные = json.loads(строка)
                if данные.get("error"):
                    raise LLMError(f"Ollama: {данные['error']}")
                сообщение = данные.get("message") or {}
                if сообщение.get("thinking"):
                    мысли.append(сообщение["thinking"])
                    yield {"тип": "думает", "текст": сообщение["thinking"]}
                if сообщение.get("content"):
                    if первый is None:
                        первый = time.monotonic()
                    куски.append(сообщение["content"])
                    yield {"тип": "текст", "текст": сообщение["content"]}
                if данные.get("done"):
                    последняя = данные
    except httpx.ConnectError as exc:
        raise LLMError(f"Ollama не отвечает на {модель.base_url}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"Сбой потока Ollama: {exc}") from exc
    yield _итог(модель.key, "".join(куски), начало, первый,
                последняя.get("prompt_eval_count") or 0, последняя.get("eval_count") or 0, 0.0,
                ollama_timings(последняя, time.monotonic() - начало), "".join(мысли),
                последняя.get("done_reason", ""))


def _поток_облака(модель, сообщения, предел, температура, http) -> Iterator[dict]:
    ключ_api = модель.api_key
    if not ключ_api:
        raise LLMError(f"Не задан ключ API для модели «{модель.key}»: нужна переменная {модель.env_var}.")
    # _payload не обращается к self — тело запроса то же, что у Client.call.
    тело = Client._payload(None, модель, сообщения, предел, температура, False)
    тело["stream"] = True
    тело["stream_options"] = {"include_usage": True}
    заголовки = {"Authorization": f"Bearer {ключ_api}", "Content-Type": "application/json"}
    начало, первый = time.monotonic(), None
    куски: list[str] = []
    вход = выход = 0
    причина = ""
    try:
        with http.stream("POST", f"{модель.base_url.rstrip('/')}/chat/completions", json=тело,
                         headers=заголовки, timeout=httpx.Timeout(ОБЛАКО_ТАЙМАУТ, connect=10.0)) as ответ:
            if not ответ.is_success:
                ответ.read()
                raise LLMError(f"Провайдер {модель.provider} ответил {ответ.status_code}: {ответ.text[:300]}")
            for строка in ответ.iter_lines():
                if not строка.startswith("data:"):
                    continue   # пустые строки и «: keep-alive» под нагрузкой
                кусок = строка[5:].strip()
                if кусок == "[DONE]":
                    break
                данные = json.loads(кусок)
                if данные.get("usage"):
                    вход = данные["usage"].get("prompt_tokens", 0) or 0
                    выход = данные["usage"].get("completion_tokens", 0) or 0
                for выбор in данные.get("choices") or []:
                    текст = (выбор.get("delta") or {}).get("content") or ""
                    if текст:
                        if первый is None:
                            первый = time.monotonic()
                        куски.append(текст)
                        yield {"тип": "текст", "текст": текст}
                    причина = выбор.get("finish_reason") or причина
    except httpx.HTTPError as exc:
        raise LLMError(f"Сбой потока {модель.provider}: {exc}") from exc
    yield _итог(модель.key, "".join(куски), начало, первый, вход, выход,
                tokens.cost(вход, выход, модель.price_in, модель.price_out), причина=причина)


def спросить(ключ: str, сообщения: list[dict[str, Any]], **параметры: Any) -> dict[str, Any]:
    """Ответ целиком: итоговое событие потока."""
    итог: dict[str, Any] = {}
    for событие in поток(ключ, сообщения, **параметры):
        if событие["тип"] == "итог":
            итог = событие
    if not итог.get("text"):
        raise LLMError(f"Модель {ключ} вернула пустой ответ"
                       + (f" ({итог.get('finish')})" if итог.get("finish") else "") + ".")
    return итог
