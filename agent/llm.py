"""Единственное место, где агент ходит в сеть.

В этот день у агента много точек вызова LLM и они идут к разным моделям:
маршрутизатор реплик — к дешёвой, сжатие завершённой задачи — к средней,
основной ответ — к сильной. Держать HTTP-запрос внутри самого агента стало
неудобно, поэтому он вынесен сюда: любая часть системы просит «позови такую-то
модель с такими-то сообщениями» и получает текст плюс расход.

Все провайдеры OpenAI-совместимы, так что разница между ними сводится к адресу,
идентификатору модели и переменной с ключом — всё это лежит в catalog.py.

С Дня 17 сюда же добавлено поле tools — описания инструментов MCP в формате
function calling. Модель ничего не вызывает сама: она возвращает просьбу
(Reply.tool_calls), вызов делает агент через MCP, а результат уходит следующим
запросом сообщением роли tool. Поэтому здесь нет ни знания об MCP, ни доступа
к инструментам: этот модуль по-прежнему только ходит в сеть.

С Дня 26 модель может работать и на этой машине — через Ollama. Для неё
клиент ходит не в OpenAI-совместимый /v1, а в родной /api/chat: только там
окно контекста (num_ctx) и «размышления» (think) задаются на запрос, а в ответе
есть замеры самой Ollama — загрузка модели, чтение промпта, генерация. Снаружи
разницы нет: тот же Reply, тот же LLMError.

Публичное API:
  LLMError                — единственный тип ошибки наружу
  Reply                   — текст ответа, просьбы о вызовах и расход
  ToolCall                — одна просьба вызвать инструмент
  call(model_key, msgs)   — синхронный вызов модели
  Client                  — то же самое, но с переиспользуемым HTTP-соединением
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from dotenv import load_dotenv

from agent import catalog, tokens
from agent.local import offline   # перехватчик сети ставится при импорте

load_dotenv()

log = logging.getLogger("agent.llm")

DEFAULT_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
DEFAULT_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "2048"))
DEFAULT_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))

# Бесплатные тарифы ограничивают не только запросы в минуту, но и токены в
# минуту. Провайдер сам подсказывает, через сколько повторить, поэтому ждать
# тут осмысленно — в отличие от сетевой ошибки, где ждать нечего.
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_PAUSE = 8.0

# Локальная модель на CPU читает промпт единицы-десятки токенов в секунду
# (замер на 2 ядрах i3-1005G1 под чужой нагрузкой — 7 ток/с), а первая
# загрузка весов в память — ещё 20 с. Вызов без потока ждёт ответ целиком:
# RAG-промпт в 3 тысячи токенов только читается 7–9 минут.
LOCAL_TIMEOUT = float(os.getenv("LLM_LOCAL_TIMEOUT", "1800"))
# Сколько Ollama держит модель в памяти после запроса. По умолчанию у неё 5
# минут — между сценами показа модель выгружалась бы, и следующий ответ снова
# ждал загрузки весов.
LOCAL_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")


class LLMError(RuntimeError):
    """Не удалось получить ответ от модели."""


@dataclass
class ToolCall:
    """Просьба модели вызвать инструмент.

    Аргументы приходят строкой JSON, и строка эта не всегда корректна: модель
    их сочиняет, а не сериализует. Разбор делается здесь, один раз, и неудача
    разбора — обычное дело, а не исключение: вызывающий покажет модели ошибку
    и даст переписать.
    """

    ид: str
    имя: str
    аргументы: dict[str, Any] = field(default_factory=dict)
    сырые: str = ""
    ошибка: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ид": self.ид, "имя": self.имя, "аргументы": self.аргументы,
                "ошибка": self.ошибка}


@dataclass
class Reply:
    """Ответ модели вместе с тем, во что он обошёлся."""

    text: str
    model_key: str
    model_id: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    elapsed: float = 0.0
    finish_reason: str = ""
    # Просьбы вызвать инструменты и то самое сообщение ассистента, которое надо
    # вернуть в следующий запрос: протокол function calling требует, чтобы
    # ответы инструментов шли следом за ним и ссылались на его tool_call_id.
    tool_calls: list[ToolCall] = field(default_factory=list)
    message: dict[str, Any] = field(default_factory=dict)
    # День 26, локальная модель: «размышления» (если их просили) и замеры
    # самой Ollama — загрузка, чтение промпта, генерация, токены в секунду.
    thinking: str = ""
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_key": self.model_key,
            "model_id": self.model_id,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost": self.cost,
            "elapsed": round(self.elapsed, 2),
            "finish_reason": self.finish_reason,
            "tool_calls": [в.to_dict() for в in self.tool_calls],
            "timings": dict(self.timings),
        }


@dataclass
class Client:
    """Клиент к OpenAI-совместимым провайдерам с общим HTTP-соединением."""

    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: float = DEFAULT_TIMEOUT
    # Сводный расход по всем вызовам клиента — по нему интерфейсы показывают,
    # во что обошёлся ответ агента целиком, включая служебные вызовы.
    spent: dict[str, Any] = field(default_factory=lambda: {"calls": 0, "tokens": 0, "cost": 0.0})
    _http: httpx.Client | None = None

    def __post_init__(self) -> None:
        self._http = httpx.Client(timeout=self.timeout)

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def reset_spent(self) -> None:
        self.spent = {"calls": 0, "tokens": 0, "cost": 0.0}

    def call(
        self,
        model_key: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 0,
        temperature: float | None = None,
        low_effort: bool = False,
        tools: list[dict[str, Any]] | None = None,
        fmt: Any = None,
    ) -> Reply:
        """Зовёт модель и возвращает ответ; при сбое поднимает LLMError.

        fmt — JSON-схема ответа для локальной модели (format в Ollama, День 27):
        сервер ограничивает генерацию схемой, и маленькая модель не может
        ответить не-JSON'ом. Облачным моделям схема не уходит — им хватает
        просьбы в промпте.

        В офлайн-режиме облачная модель не вызывается: отказ до сокета
        (offline.проверить_модель) и запись в журнал сети.

        low_effort — для механических задач (маршрутизация, сжатие): у моделей
        семейства gpt-oss это убирает скрытое рассуждение, на которое иначе
        уходят сотни выходных токенов.

        tools — описания инструментов в формате function calling. Модель не
        вызывает их сама: она возвращает просьбу (Reply.tool_calls), вызов
        делает агент, а результат возвращается следующим запросом. Поле это
        уходит в КАЖДЫЙ запрос, пока инструменты подключены, и оплачивается
        как вход — поэтому его передают осознанно, а не «на всякий случай».
        """
        model = catalog.get(model_key)
        offline.проверить_модель(model_key)
        if model.local:
            # День 29: конфигурация вызова (agent/local/tuning.py) может вести
            # модель на другой сервер Ollama — второй или GPU через туннель.
            from agent.local import tuning
            model = tuning.модель_для_вызова(model)
            return self._call_ollama(model, messages, max_tokens or self.max_tokens,
                                     self.temperature if temperature is None else temperature,
                                     tools, fmt=fmt)
        ключ = model.api_key
        if not ключ:
            raise LLMError(
                f"Не задан ключ API для модели «{model.key}»: нужна переменная "
                f"{model.env_var}. Скопируйте .env.example в .env и впишите ключ."
            )
        # Ключ уходит в HTTP-заголовок, а тот допускает только ASCII. Лишний
        # символ при копировании иначе обернётся невнятной ошибкой кодировки
        # из недр HTTP-клиента.
        if not ключ.isascii():
            raise LLMError(
                f"Ключ {model.env_var} содержит недопустимые символы — вероятно, "
                "при копировании в него попал лишний знак. Проверьте .env."
            )

        payload = self._payload(model, messages, max_tokens or self.max_tokens,
                                self.temperature if temperature is None else temperature,
                                low_effort, tools)
        url = f"{model.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {ключ}", "Content-Type": "application/json"}

        http = self._http or httpx.Client(timeout=self.timeout)
        начало = time.monotonic()
        последняя_ошибка = ""
        с_лимитом = 0
        попытка = 0

        while попытка < 2 + RATE_LIMIT_RETRIES:
            попытка += 1
            log.info("Запрос к %s (попытка %d)", model.api_id, попытка)
            try:
                response = http.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                последняя_ошибка = f"сетевая ошибка — {exc}"
            else:
                if response.status_code in (401, 403):
                    raise LLMError(
                        f"Провайдер {model.provider} отклонил ключ "
                        f"({response.status_code}). Проверьте {model.env_var} в .env."
                    )
                if response.is_success:
                    ответ = self._parse(response, model, time.monotonic() - начало)
                    self.spent["calls"] += 1
                    self.spent["tokens"] += ответ.total_tokens
                    self.spent["cost"] += ответ.cost
                    return ответ
                # 413 у Groq означает не «слишком длинный запрос», а «запрос
                # больше, чем осталось в минутном лимите расхода». Лечится тем
                # же ожиданием, что и 429.
                if response.status_code in (429, 413):
                    подсказка = _подсказка(response)
                    if _суточный_лимит(подсказка):
                        # Минутный лимит проходит сам, суточный — нет. Ждать и
                        # повторять тут значит потратить несколько минут, чтобы
                        # в конце получить ту же ошибку.
                        raise LLMError(
                            f"У {model.provider} закончилась суточная квота: {подсказка} "
                            "Ждать бесполезно — квота обновится только завтра. "
                            "Возьмите модель другого провайдера "
                            "(например, glm-flash или ds-flash)."
                        )
                    с_лимитом += 1
                    if с_лимитом > RATE_LIMIT_RETRIES:
                        raise LLMError(
                            f"Провайдер {model.provider} ограничивает расход: {подсказка}"
                        )
                    пауза = _пауза(response, с_лимитом)
                    log.warning("Лимит расхода, ждём %.0f с: %s", пауза, подсказка)
                    time.sleep(пауза)
                    continue
                последняя_ошибка = (
                    f"провайдер вернул {response.status_code}: {response.text[:300]}"
                )

            if попытка >= 2 + с_лимитом:
                break
            log.warning("Повтор запроса: %s", последняя_ошибка)
            time.sleep(2)

        raise LLMError(f"Не удалось получить ответ от модели {model.key}: {последняя_ошибка}")

    def _payload(
        self,
        model: catalog.Model,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        low_effort: bool,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Тело запроса в OpenAI-совместимом формате."""
        # Просить больше, чем помещается в окно модели, бессмысленно: провайдер
        # ответит ошибкой ещё до генерации.
        потолок = max(256, model.context_window // 2)
        payload: dict[str, Any] = {
            "model": model.api_id,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": min(max_tokens, потолок),
            "stream": False,
        }
        # У DeepSeek V4 режим размышления включён по умолчанию, а в нём
        # temperature и top_p, по документации провайдера, не действуют. Агенту
        # нужна предсказуемость, поэтому режим отключается явно.
        if "api.deepseek.com" in model.base_url:
            payload["thinking"] = {"type": "disabled"}
        if low_effort and model.supports_effort:
            payload["reasoning_effort"] = "low"
        if tools:
            payload["tools"] = tools
            # «auto» — решение за моделью: звать инструмент или ответить словами.
            # Принуждать её к вызову («required») нельзя: на вопрос «что такое
            # PostGIS» вызывать в трекере нечего.
            payload["tool_choice"] = "auto"
        return payload

    @staticmethod
    def _parse(response: httpx.Response, model: catalog.Model, elapsed: float) -> Reply:
        """Достаёт текст, просьбы о вызовах и расход из ответа провайдера."""
        try:
            data = response.json()
            выбор = data["choices"][0]
            сообщение = выбор["message"]
            текст = (сообщение.get("content") or "").strip()
            finish = выбор.get("finish_reason", "")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"Неожиданный формат ответа провайдера: {response.text[:300]}"
            ) from exc

        вызовы = _разобрать_вызовы(сообщение.get("tool_calls"))
        # Пустой ответ — признак сбоя, но не тогда, когда модель просит вызвать
        # инструмент: в этом случае весь её ответ и есть просьба, а текста при
        # ней может не быть вовсе (так отвечают модели gpt-oss).
        if not текст and not вызовы:
            raise LLMError(f"Модель {model.key} вернула пустой ответ.")

        usage = data.get("usage") or {}
        вход = usage.get("prompt_tokens", 0) or 0
        выход = usage.get("completion_tokens", 0) or 0
        return Reply(
            text=текст,
            model_key=model.key,
            model_id=data.get("model", model.api_id),
            prompt_tokens=вход,
            completion_tokens=выход,
            cost=tokens.cost(вход, выход, model.price_in, model.price_out),
            elapsed=elapsed,
            finish_reason=finish,
            tool_calls=вызовы,
            message=_сообщение_ассистента(текст, сообщение.get("tool_calls")),
        )


    # --- локальная модель: родной API Ollama (День 26) --------------------------

    def _call_ollama(self, model: catalog.Model, messages: list[dict[str, Any]],
                     max_tokens: int, temperature: float,
                     tools: list[dict[str, Any]] | None = None,
                     think: bool = False, fmt: Any = None) -> Reply:
        """POST /api/chat без потока. think=False — без «размышлений»: агенту
        нужен ответ, а не рассуждение, и на CPU каждый токен на счету."""
        from agent.local import progress
        payload = ollama_payload(model, messages, max_tokens, temperature, tools, think, fmt)
        http = self._http or httpx.Client(timeout=self.timeout)
        начало = time.monotonic()
        if progress.активен():
            # За ходом кто-то наблюдает (страница чата, консоль) — берём ответ
            # потоком, чтобы показывать, сколько токенов модель уже написала:
            # на CPU это минута, и без счётчика она выглядит зависанием.
            return self._поток_ollama(http, model, payload, начало)
        последняя_ошибка = ""
        for попытка in (1, 2):
            log.info("Запрос к Ollama %s (попытка %d)", model.api_id, попытка)
            try:
                response = http.post(f"{model.base_url}/api/chat", json=payload,
                                     timeout=httpx.Timeout(LOCAL_TIMEOUT, connect=5.0))
            except httpx.ConnectError as exc:
                raise LLMError(
                    f"Ollama не отвечает на {model.base_url} ({exc}). Запустите её: "
                    "systemctl start ollama или ollama serve."
                ) from exc
            except httpx.HTTPError as exc:
                последняя_ошибка = f"сетевая ошибка — {exc}"
                continue
            if response.is_success:
                ответ = parse_ollama(response.json(), model, time.monotonic() - начало)
                self.spent["calls"] += 1
                self.spent["tokens"] += ответ.total_tokens
                return ответ
            raise LLMError(ollama_error(response, model))
        raise LLMError(f"Не удалось получить ответ от локальной модели {model.key}: {последняя_ошибка}")


    def _поток_ollama(self, http: httpx.Client, model: catalog.Model, payload: dict[str, Any],
                      начало: float) -> Reply:
        """Тот же /api/chat, но потоком NDJSON: счётчик токенов — наблюдателю."""
        from agent.local import progress
        payload = {**payload, "stream": True}
        текст: list[str] = []
        мысли: list[str] = []
        вызовы: list[dict[str, Any]] = []
        итог: dict[str, Any] = {}
        выход, первый = 0, None
        log.info("Запрос к Ollama %s потоком", model.api_id)
        try:
            with http.stream("POST", f"{model.base_url}/api/chat", json=payload,
                             timeout=httpx.Timeout(LOCAL_TIMEOUT, connect=5.0)) as response:
                if not response.is_success:
                    response.read()
                    raise LLMError(ollama_error(response, model))
                for строка in response.iter_lines():
                    if not строка.strip():
                        continue
                    данные = json.loads(строка)
                    if данные.get("error"):
                        raise LLMError(f"Ollama: {данные['error']}")
                    с = данные.get("message") or {}
                    if с.get("content") or с.get("tool_calls"):
                        выход += 1
                        if первый is None:
                            первый = time.monotonic() - начало
                            progress.токены(model.key, выход, первый, первый=первый)
                        else:
                            progress.токены(model.key, выход, time.monotonic() - начало)
                    текст.append(с.get("content") or "")
                    мысли.append(с.get("thinking") or "")
                    вызовы.extend(с.get("tool_calls") or [])
                    if данные.get("done"):
                        итог = данные
        except httpx.ConnectError as exc:
            raise LLMError(f"Ollama не отвечает на {model.base_url} ({exc}). Запустите её: "
                           "systemctl start ollama или ollama serve.") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Сетевая ошибка при потоке от локальной модели {model.key}: {exc}") from exc
        прошло = time.monotonic() - начало
        progress.токены(model.key, итог.get("eval_count") or выход, прошло, последний=True)
        ответ = parse_ollama({**итог, "message": {"content": "".join(текст), "thinking": "".join(мысли),
                                                  "tool_calls": вызовы}}, model, прошло)
        self.spent["calls"] += 1
        self.spent["tokens"] += ответ.total_tokens
        return ответ


def ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Сообщения в формате /api/chat.

    Отличия от OpenAI два: аргументы вызова инструмента — объект, а не строка
    JSON, и ответ инструмента называет инструмент по имени (tool_name), а не
    по tool_call_id."""
    имена: dict[str, str] = {}
    итог: list[dict[str, Any]] = []
    for с in messages:
        запись: dict[str, Any] = {"role": с.get("role", "user"), "content": с.get("content") or ""}
        if с.get("tool_calls"):
            вызовы = []
            for в in с["tool_calls"]:
                ф = в.get("function") or {}
                арг = ф.get("arguments") or {}
                if isinstance(арг, str):
                    try:
                        арг = json.loads(арг or "{}")
                    except json.JSONDecodeError:
                        арг = {}
                имена[в.get("id", "")] = ф.get("name", "")
                вызовы.append({"function": {"name": ф.get("name", ""), "arguments": арг}})
            запись["tool_calls"] = вызовы
        if с.get("role") == "tool":
            имя = с.get("name") or имена.get(с.get("tool_call_id", ""), "")
            if имя:
                запись["tool_name"] = имя
        итог.append(запись)
    return итог


def ollama_payload(model: catalog.Model, messages: list[dict[str, Any]], max_tokens: int,
                   temperature: float, tools: list[dict[str, Any]] | None = None,
                   think: bool = False, fmt: Any = None, stream: bool = False) -> dict[str, Any]:
    """Тело запроса /api/chat: окно контекста и длина ответа — в options."""
    payload: dict[str, Any] = {
        "model": model.api_id,
        "messages": ollama_messages(messages),
        "stream": stream,
        "think": think,
        "keep_alive": LOCAL_KEEP_ALIVE,
        "options": {"num_ctx": model.context_window, "temperature": temperature,
                    # Ответ не длиннее половины окна: остальное — промпту.
                    "num_predict": min(max_tokens, model.context_window // 2)},
    }
    if tools:
        payload["tools"] = tools
    if fmt is not None:
        payload["format"] = fmt
    # День 29: опции и веса активной конфигурации — поверх базовых; без
    # конфигурации тело то же, что в Дне 28.
    from agent.local import tuning
    return tuning.тело_для_вызова(model, payload)


_РАЗМЫШЛЕНИЯ = re.compile(r"<think>.*?</think>\s*", re.S)


def ollama_timings(data: dict[str, Any], elapsed: float) -> dict[str, float]:
    """Замеры Ollama (наносекунды) — в секунды и токены в секунду."""
    нс = 1e9
    загрузка = (data.get("load_duration") or 0) / нс
    промпт = (data.get("prompt_eval_duration") or 0) / нс
    генерация = (data.get("eval_duration") or 0) / нс
    вход, выход = data.get("prompt_eval_count") or 0, data.get("eval_count") or 0
    return {
        "load_s": round(загрузка, 2), "prompt_s": round(промпт, 2), "gen_s": round(генерация, 2),
        "total_s": round((data.get("total_duration") or 0) / нс or elapsed, 2),
        # До первого токена ответа сервер грузит модель (если она выгружена)
        # и читает промпт.
        "ttft_s": round(загрузка + промпт, 2),
        "tok_s": round(выход / генерация, 1) if генерация else 0.0,
        "prompt_tok_s": round(вход / промпт, 1) if промпт else 0.0,
    }


def parse_ollama(data: dict[str, Any], model: catalog.Model, elapsed: float) -> Reply:
    сообщение = data.get("message") or {}
    сырой = сообщение.get("content") or ""
    # «Размышления» приходят отдельным полем, но шаблон модели иногда кладёт их
    # прямо в текст тегами <think> — такой текст ответом не считается.
    текст = _РАЗМЫШЛЕНИЯ.sub("", сырой).strip()
    размышления = (сообщение.get("thinking") or "").strip()
    if not размышления and "<think>" in сырой:
        размышления = " ".join(re.findall(r"<think>(.*?)</think>", сырой, re.S)).strip()
    сырые_вызовы = [{"id": в.get("id") or f"call_{i}", "type": "function",
                     "function": {"name": (в.get("function") or {}).get("name", ""),
                                  "arguments": json.dumps((в.get("function") or {}).get("arguments") or {},
                                                          ensure_ascii=False)}}
                    for i, в in enumerate(сообщение.get("tool_calls") or [])]
    вызовы = _разобрать_вызовы(сырые_вызовы)
    if not текст and not вызовы:
        причина = data.get("done_reason") or ""
        raise LLMError(f"Локальная модель {model.key} вернула пустой ответ"
                       + (f" (done_reason: {причина})" if причина else "") + ".")
    вход, выход = data.get("prompt_eval_count") or 0, data.get("eval_count") or 0
    return Reply(
        text=текст, model_key=model.key, model_id=data.get("model", model.api_id),
        prompt_tokens=вход, completion_tokens=выход, cost=0.0, elapsed=elapsed,
        finish_reason=data.get("done_reason", ""), tool_calls=вызовы,
        message=_сообщение_ассистента(текст, сырые_вызовы),
        thinking=размышления, timings=ollama_timings(data, elapsed),
    )


def ollama_error(response: httpx.Response, model: catalog.Model) -> str:
    """Понятная ошибка Ollama: нет модели, нет поддержки инструментов, прочее."""
    try:
        текст = str(response.json().get("error") or "")
    except ValueError:
        текст = response.text[:300]
    if response.status_code == 404 or "not found" in текст.lower():
        return (f"Модель {model.api_id} не скачана в Ollama. Скачайте: ollama pull {model.api_id}")
    if "does not support tools" in текст:
        return f"Модель {model.api_id} не умеет вызывать инструменты (Ollama: {текст})."
    return f"Ollama ответила {response.status_code}: {текст[:300]}"


def _разобрать_вызовы(записи: Any) -> list[ToolCall]:
    """Просьбы о вызовах из ответа провайдера. Кривой JSON аргументов не роняет."""
    вызовы: list[ToolCall] = []
    for номер, запись in enumerate(записи or []):
        функция = (запись or {}).get("function") or {}
        сырые = функция.get("arguments") or "{}"
        аргументы: dict[str, Any] = {}
        ошибка = ""
        try:
            разобранные = json.loads(сырые) if isinstance(сырые, str) else сырые
            if isinstance(разобранные, dict):
                аргументы = разобранные
            else:
                ошибка = "аргументы пришли не объектом JSON"
        except (ValueError, TypeError) as exc:
            ошибка = f"аргументы — не JSON: {exc}"
        вызовы.append(ToolCall(
            ид=str(запись.get("id") or f"call_{номер}"),
            имя=str(функция.get("name") or ""),
            аргументы=аргументы,
            сырые=сырые if isinstance(сырые, str) else json.dumps(сырые, ensure_ascii=False),
            ошибка=ошибка,
        ))
    return вызовы


def _сообщение_ассистента(текст: str, вызовы: Any) -> dict[str, Any]:
    """Сообщение ассистента для следующего запроса — без лишних полей провайдера.

    Обратно уходит только то, что предусмотрено форматом: роль, текст и
    tool_calls. Служебные поля вроде «reasoning» у gpt-oss в запрос возвращать
    не нужно, а некоторые провайдеры на них ещё и отвечают ошибкой.
    """
    сообщение: dict[str, Any] = {"role": "assistant", "content": текст}
    if вызовы:
        сообщение["tool_calls"] = [
            {"id": з.get("id"), "type": "function",
             "function": {"name": (з.get("function") or {}).get("name"),
                          "arguments": (з.get("function") or {}).get("arguments") or "{}"}}
            for з in вызовы
        ]
    return сообщение


def _пауза(response: httpx.Response, попытка: int) -> float:
    """Сколько ждать перед повтором: по заголовку провайдера или с запасом."""
    заголовок = response.headers.get("retry-after", "")
    try:
        return max(1.0, min(60.0, float(заголовок)))
    except ValueError:
        return min(60.0, RATE_LIMIT_PAUSE * попытка)


# Суточная квота отличается от минутной только текстом сообщения: код ответа у
# них один и тот же — 429.
_СУТОЧНЫЕ = ("per day", "tokens per day", "tpd", "requests per day", "rpd",
             "daily", "в сутки")


def _суточный_лимит(сообщение: str) -> bool:
    низ = (сообщение or "").lower()
    return any(признак in низ for признак in _СУТОЧНЫЕ)


def _подсказка(response: httpx.Response) -> str:
    """Человекочитаемое пояснение провайдера про лимит."""
    try:
        сообщение = (response.json().get("error") or {}).get("message", "")
    except ValueError:
        сообщение = ""
    return (сообщение or response.text)[:200]


def call(model_key: str, messages: list[dict[str, str]], **kwargs: Any) -> Reply:
    """Разовый вызов модели без создания клиента — для скриптов и тестов."""
    client = Client()
    try:
        return client.call(model_key, messages, **kwargs)
    finally:
        client.close()
