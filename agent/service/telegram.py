"""Telegram-бот Дня 30: владелец говорит с локальной моделью через приватный сервис.

Бот — ещё один клиент сервиса (telegram-plan D-T01): ходит в /v1/chat/completions и
/v1/documents/answer по HTTPS со своим ключом, модель напрямую не вызывает; лимиты,
очередь и предел контекста — сервиса. Сообщения получает long polling'ом (D-T03):
входящих соединений нет. Отвечает только владельцу (D-T04); ответ идёт потоком —
бот правит своё сообщение не чаще раза в `правка_с` (D-T07); история чата — в памяти
процесса (D-T06); ответы по документам перед отправкой проходят маску ПД (D-T10).

  BotAPI(токен, база)                 getMe, getUpdates, sendMessage, editMessageText, …
  КлиентСервиса(адрес, ключ, ca)      чат(сообщения, при_событии), документы(вопрос), здоровье()
  Бот(api, сервис, владелец, каталог) обработать(update), опросить_раз(), опрашивать(стоп)

Журнал бота (`telegram.jsonl`) и состояние для /admin (`telegram-state.json`) — без
текстов вопросов и ответов, без ID владельца; ID чужих — первые 8 знаков sha256.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import html
import ipaddress
import json
import math
import os
import re
import ssl
import tempfile
import time
from collections import Counter
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse, urlsplit

import httpx

from agent.service import pd
from agent.service.journal import Журнал

API_TELEGRAM = "https://api.telegram.org"
ОТКАЗ_ПД = "Ответ скрыт: не удалось безопасно проверить персональные данные."
ИСТОРИЯ = 12                 # реплик в памяти на чат (6 пар)
ПРАВКА_С = 1.5               # правка сообщения не чаще (ворота 0, A-T2: 40 правок без 429)
ПЕЧАТАЕТ_С = 4.0             # «печатает…» пока ждём
ДОКУМЕНТЫ_ХОД_С = 15.0       # правка «ищу в документах… N с»
СРОК_С = 1200.0              # общий срок ответа с повторами после 429 (как service_load.py)
ОПРОС_МЕСТА_С = 5.0          # queue_full: раз в N с смотреть /health — не освободилось ли место
ДЛИНА = 4000                 # предел Telegram — 4096 символов на сообщение
ОПРОС_С = 25                 # ожидание getUpdates (A-T1)
ОПРОС_ВО_ВРЕМЯ_ОТВЕТА_С = 2.0  # пока модель отвечает — короткий getUpdates: не ждут ли /status, /help, /start
ЧУЖОМУ_РАЗ_В_С = 86400.0     # «это частный бот» — не чаще раза в сутки на человека
ЖУРНАЛ = "telegram.jsonl"
# Белый список журнала бота: текстов и ID владельца в нём нет и быть не может.
ПОЛЯ_ЖУРНАЛА = ("время", "кто", "чей", "вид", "команда", "код", "ошибка", "вход", "ответ", "ожидание_с",
                "длительность_с", "правок", "отказов_telegram", "повторов_429", "укорочено", "скрыто", "виды_пд")
СОСТОЯНИЕ = "telegram-state.json"
OFFSET = "telegram-offset"
OFFSET_ПРЕДЫДУЩИЙ = "telegram-offset.pending"

_ТОКЕН = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


def без_токена(текст: str) -> str:
    """Токен Bot API стоит в пути запроса — из любого текста ошибки он убирается."""
    return _ТОКЕН.sub("/bot…", текст or "")


def проверить_api_telegram(база: str) -> str:
    """Проверить адрес до передачи токена: официальный HTTPS либо корневой loopback для тестов."""
    ошибка = ValueError("небезопасный адрес Telegram Bot API")
    if (not isinstance(база, str) or not база or база != база.strip() or "?" in база or "#" in база or
            any(ord(знак) < 32 or ord(знак) == 127 for знак in база)):
        raise ошибка
    try:
        адрес = urlsplit(база)
        хост, порт = адрес.hostname, адрес.port
    except ValueError:
        raise ошибка from None
    if (not хост or адрес.netloc.endswith(":") or адрес.username is not None or адрес.password is not None or
            адрес.query or адрес.fragment or адрес.path not in ("", "/")):
        raise ошибка
    хост = хост.lower()
    if хост == "api.telegram.org":
        if адрес.scheme != "https" or порт not in (None, 443):
            raise ошибка
        return API_TELEGRAM
    локальный = хост == "localhost"
    if not локальный:
        try:
            ip = ipaddress.ip_address(хост)
            локальный = (ip.version == 4 and ip.is_loopback) or ip == ipaddress.ip_address("::1")
        except ValueError:
            локальный = False
    if not локальный or адрес.scheme not in ("http", "https") or порт == 0:
        raise ошибка
    узел = f"[{хост}]" if ":" in хост else хост
    return f"{адрес.scheme}://{узел}" + (f":{порт}" if порт is not None else "")


_ДВОЙНИКИ_КОНТРОЛЬ = str.maketrans("ABCEHKMOPTXYaceopxy", "АВСЕНКМОРТХУасеорху")
_БУКВЫ_КОНТРОЛЬ = "А-ЯЁа-яё"
_ДЕФИСЫ_КОНТРОЛЬ = "-‐‑"
_ОКОНЧАНИЯ_ОТЧЕСТВ = ("ович", "евич", "ьич", "овна", "евна", "ична", "инична")


def _остатки_пд(текст: str) -> bool:
    """Независимая защита после маски: простые лексические признаки без шаблонов ``pd.скрыть``."""
    if not isinstance(текст, str):
        return True
    норм = текст.translate(_ДВОЙНИКИ_КОНТРОЛЬ)
    if re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", норм):
        return True
    for начало in re.finditer(r"(?<!\d)(?:\+7|8)", норм):
        окно = норм[начало.start():начало.start() + 32]
        цифры = "".join(с for с in окно if с.isdecimal())
        if len(цифры) >= 11:
            return True
    слова = re.findall(rf"[{_БУКВЫ_КОНТРОЛЬ}]+(?:[{_ДЕФИСЫ_КОНТРОЛЬ}][{_БУКВЫ_КОНТРОЛЬ}]+)*", норм)
    основы = tuple(с.translate(_ДВОЙНИКИ_КОНТРОЛЬ).casefold() for с in pd.фамилии())
    for слово in слова:
        for часть in re.split(rf"[{_ДЕФИСЫ_КОНТРОЛЬ}]", слово):
            нижнее = часть.casefold()
            if any(нижнее.startswith(основа) for основа in основы):
                return True
            if any(нижнее.endswith(окончание) and len(нижнее) > len(окончание) + 1
                   for окончание in _ОКОНЧАНИЯ_ОТЧЕСТВ):
                return True
    маска = re.escape(pd.МАСКА)
    слово = rf"[{_БУКВЫ_КОНТРОЛЬ}]+"
    дефис = rf"[{_ДЕФИСЫ_КОНТРОЛЬ}]"
    if re.search(rf"(?:{слово}{дефис})+{маска}|{маска}(?:{дефис}{слово})+", норм):
        return True
    инициалы = r"(?:[А-ЯЁ]\.\s?[А-ЯЁ]\.?|[А-ЯЁ]\.?\s[А-ЯЁ]\.)"
    return bool(re.search(rf"{инициалы}\s*{маска}|{маска}\s*{инициалы}", норм))


def _хэш(значение: Any) -> str:
    return hashlib.sha256(str(значение).encode()).hexdigest()[:8]


def _секунды(с: float) -> str:
    return f"{с:g}" if float(с).is_integer() else f"{с:.1f}"


class ОшибкаTelegram(Exception):
    def __init__(self, код: int, описание: str, повтор: float | None = None) -> None:
        super().__init__(f"Telegram {код}: {без_токена(описание)}")
        self.код, self.описание, self.повтор = код, без_токена(описание), повтор


class _ОшибкаSSE(ValueError):
    """Поток сервиса нарушил согласованный формат OpenAI SSE."""


class ОшибкаCheckpoint(OSError):
    """Offset нельзя безопасно прочитать или долговечно сохранить."""


async def _события_sse(r: httpx.Response):
    """Разобрать SSE как UTF-8, включая фрагменты байтов и многострочные ``data``.

    httpx декодирует ``aiter_lines`` с заменой битых символов. Для ответа модели это
    опасно: повреждённый поток выглядел бы законченным текстом, поэтому декодер здесь
    строгий. Неизвестные поля SSE разрешены стандартом и пропускаются.
    """
    декодер = codecs.getincrementaldecoder("utf-8")("strict")
    буфер = ""
    данные: list[str] = []

    def строка(значение: str):
        nonlocal данные
        if значение == "":
            if данные:
                событие = "\n".join(данные)
                данные = []
                return "data", событие
            return None
        if значение.startswith(":"):
            return "comment", значение[1:].lstrip(" ")
        поле, разделитель, значение_поля = значение.partition(":")
        if разделитель and значение_поля.startswith(" "):
            значение_поля = значение_поля[1:]
        if поле == "data":
            данные.append(значение_поля if разделитель else "")
        return None

    try:
        async for кусок in r.aiter_bytes():
            буфер += декодер.decode(кусок)
            while True:
                конец = re.search(r"\r\n|\r|\n", буфер)
                if not конец or (конец.group() == "\r" and конец.end() == len(буфер)):
                    break
                текущая, буфер = буфер[:конец.start()], буфер[конец.end():]
                событие = строка(текущая)
                if событие is not None:
                    yield событие
        буфер += декодер.decode(b"", final=True)
    except UnicodeDecodeError:
        raise _ОшибкаSSE("поток не является UTF-8") from None

    # EOF завершает последнюю строку/событие по правилам SSE. Успех всё равно
    # требует отдельного [DONE], поэтому оборванный поток ниже будет отклонён.
    if буфер.endswith("\r"):
        буфер = буфер[:-1]
    событие = строка(буфер)
    if событие is not None:
        yield событие
    событие = строка("")
    if событие is not None:
        yield событие


class BotAPI:
    """Шесть методов Bot API на httpx (D-T02): токен — только в пути запроса."""

    def __init__(self, токен: str, база: str = API_TELEGRAM, transport: httpx.AsyncBaseTransport | None = None) -> None:
        база = проверить_api_telegram(база)
        self._токен = токен
        self.база = база
        self._http = httpx.AsyncClient(base_url=self.база, transport=transport,
                                       timeout=httpx.Timeout(ОПРОС_С + 15, connect=10),
                                       follow_redirects=False, trust_env=False)

    async def вызвать(self, метод: str, **поля: Any) -> Any:
        тело = {к: в for к, в in поля.items() if в is not None}
        r = await self._http.post(f"/bot{self._токен}/{метод}", json=тело)
        try:
            д = r.json()
        except ValueError:
            код = r.status_code if r.status_code >= 400 else 502
            raise ОшибкаTelegram(код, f"ответ не JSON (HTTP {r.status_code})") from None
        if not isinstance(д, dict):
            код = r.status_code if r.status_code >= 400 else 502
            raise ОшибкаTelegram(код, f"ответ неверного типа (HTTP {r.status_code})")
        if not д.get("ok"):
            запасной_код = r.status_code if r.status_code >= 400 else 502
            try:
                код = int(д.get("error_code") or запасной_код)
            except (TypeError, ValueError, OverflowError):
                код = запасной_код
            параметры = д.get("parameters")
            повтор = параметры.get("retry_after") if isinstance(параметры, dict) else None
            raise ОшибкаTelegram(код, str(д.get("description") or ""), повтор)
        return д.get("result")

    async def getMe(self) -> dict:
        результат = await self.вызвать("getMe")
        if not isinstance(результат, dict):
            raise ОшибкаTelegram(502, "поле result метода getMe неверного типа")
        return результат

    async def getUpdates(self, offset: int | None, timeout: int = ОПРОС_С) -> list[dict]:
        результат = await self.вызвать("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message"])
        if not isinstance(результат, list):
            raise ОшибкаTelegram(502, "поле result метода getUpdates неверного типа")
        return результат

    async def sendMessage(self, чат: int, текст: str, разметка: bool = False) -> dict:
        return await self.вызвать("sendMessage", chat_id=чат, text=текст, parse_mode="HTML" if разметка else None)

    async def editMessageText(self, чат: int, сообщение: int, текст: str, разметка: bool = False) -> Any:
        return await self.вызвать("editMessageText", chat_id=чат, message_id=сообщение, text=текст,
                                  parse_mode="HTML" if разметка else None)

    async def sendChatAction(self, чат: int, действие: str = "typing") -> Any:
        return await self.вызвать("sendChatAction", chat_id=чат, action=действие)

    async def leaveChat(self, чат: int) -> Any:
        return await self.вызвать("leaveChat", chat_id=чат)

    async def закрыть(self) -> None:
        await self._http.aclose()


class КлиентСервиса:
    """Приватный сервис Дня 30 глазами бота: HTTPS, ключ «телеграм», сертификат — по своему центру."""

    def __init__(self, адрес: str, ключ: str, ca: str | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 модель: str = "loc-rag-docs") -> None:
        self.адрес, self.модель = адрес.rstrip("/"), модель
        проверка: Any = ssl.create_default_context(cafile=ca) if ca else True
        self._http = httpx.AsyncClient(base_url=self.адрес, transport=transport, verify=проверка,
                                       headers={"Authorization": f"Bearer {ключ}"},
                                       timeout=httpx.Timeout(СРОК_С, connect=10))

    @staticmethod
    def _отказ(r: httpx.Response, тело: bytes) -> dict[str, Any]:
        try:
            ошибка = (json.loads(тело) or {}).get("error") or {}
        except ValueError:
            ошибка = {}
        повтор = r.headers.get("retry-after")
        return {"код": r.status_code, "код_ошибки": ошибка.get("code") or str(r.status_code),
                "ошибка": str(ошибка.get("message") or ""), "повтор_через": float(повтор) if повтор else None}

    async def чат(self, сообщения: list[dict], при_событии: Callable[[str, Any], Awaitable[None]]) -> dict[str, Any]:
        итог: dict[str, Any] = {"код": None, "текст": "", "причина": None, "вход": None, "ответ": None,
                                "ожидание": 0.0, "код_ошибки": None, "ошибка": "", "повтор_через": None}
        try:
            async with self._http.stream("POST", "/v1/chat/completions",
                                         json={"model": self.модель, "stream": True, "messages": сообщения}) as r:
                if r.status_code != 200:
                    итог.update(self._отказ(r, await r.aread()))
                    return итог
                итог["код"] = 200
                завершение: dict[str, Any] | None = None
                готово = False
                async for вид, данные in _события_sse(r):
                    if вид == "comment":
                        число = re.search(r"(\d+)\s*$", данные)
                        if число:
                            await при_событии("очередь", int(число.group(1)))
                        continue
                    if данные == "[DONE]":
                        if завершение is None:
                            raise _ОшибкаSSE("[DONE] до завершающего фрагмента")
                        готово = True
                        break
                    try:
                        ч = json.loads(данные)
                    except (UnicodeError, ValueError, RecursionError):
                        raise _ОшибкаSSE("data не является JSON") from None
                    if not isinstance(ч, dict):
                        raise _ОшибкаSSE("JSON-событие не является объектом")
                    if "error" in ч:
                        ошибка = ч["error"]
                        if not isinstance(ошибка, dict):
                            raise _ОшибкаSSE("поле error не является объектом")
                        итог["код_ошибки"] = str(ошибка.get("code") or "error")
                        итог["ошибка"] = str(ошибка.get("message") or "")
                        break
                    if завершение is not None:
                        raise _ОшибкаSSE("данные после завершающего фрагмента")
                    варианты = ч.get("choices")
                    if not isinstance(варианты, list) or not варианты or not isinstance(варианты[0], dict):
                        raise _ОшибкаSSE("неверное поле choices")
                    выбор = варианты[0]
                    delta = выбор.get("delta")
                    if not isinstance(delta, dict):
                        raise _ОшибкаSSE("неверное поле delta")
                    кусок = delta.get("content", "")
                    if not isinstance(кусок, str):
                        raise _ОшибкаSSE("content не является строкой")
                    if кусок:
                        итог["текст"] += кусок
                        await при_событии("текст", итог["текст"])
                    причина = выбор.get("finish_reason")
                    if причина is not None:
                        if причина not in ("stop", "length"):
                            raise _ОшибкаSSE("неверная причина завершения")
                        usage = ч.get("usage")
                        if not isinstance(usage, dict):
                            raise _ОшибкаSSE("нет usage в завершающем фрагменте")
                        вход, ответ = usage.get("prompt_tokens"), usage.get("completion_tokens")
                        if (not isinstance(вход, int) or isinstance(вход, bool) or вход < 0 or
                                not isinstance(ответ, int) or isinstance(ответ, bool) or ответ < 0):
                            raise _ОшибкаSSE("неверное usage в завершающем фрагменте")
                        ожидание = ч.get("x_queue_wait", 0.0)
                        if (not isinstance(ожидание, (int, float)) or isinstance(ожидание, bool) or
                                not math.isfinite(float(ожидание)) or ожидание < 0):
                            raise _ОшибкаSSE("неверное x_queue_wait")
                        завершение = {"причина": причина, "вход": вход, "ответ": ответ,
                                      "ожидание": float(ожидание)}
                if not итог["код_ошибки"]:
                    if not готово:
                        raise _ОшибкаSSE("EOF до [DONE]")
                    итог.update(завершение or {})
        except _ОшибкаSSE as exc:
            итог.update({"причина": None, "вход": None, "ответ": None, "ожидание": 0.0,
                         "код_ошибки": "invalid_sse", "ошибка": str(exc)})
        except httpx.HTTPError as exc:
            итог.update({"причина": None, "вход": None, "ответ": None, "ожидание": 0.0,
                         "код_ошибки": "сеть", "ошибка": без_токена(f"{type(exc).__name__}: {exc}")})
        if итог["код_ошибки"]:
            # Части уже могли появиться в промежуточной правке Telegram, но ошибочный
            # поток не становится результатом и не должен попасть в историю.
            итог["текст"] = ""
        return итог

    async def документы(self, вопрос: str) -> dict[str, Any]:
        return await self._вопрос("/v1/documents/answer", вопрос)

    async def проект(self, вопрос: str) -> dict[str, Any]:
        """Ассистент разработчика (День 31): ключу бота нужно право «проект»."""
        return await self._вопрос("/v1/project/help", вопрос)

    async def _вопрос(self, путь: str, вопрос: str) -> dict[str, Any]:
        итог: dict[str, Any] = {"код": None, "код_ошибки": None, "ошибка": "", "повтор_через": None,
                                "данные": None, "ожидание": 0.0}
        try:
            r = await self._http.post(путь, json={"question": вопрос})
        except httpx.HTTPError as exc:
            итог.update({"код_ошибки": "сеть", "ошибка": f"{type(exc).__name__}: {exc}"})
            return итог
        if r.status_code != 200:
            итог.update(self._отказ(r, r.content))
            return итог
        итог.update({"код": 200, "данные": r.json(), "ожидание": float(r.headers.get("x-queue-wait") or 0.0)})
        return итог

    async def здоровье(self) -> dict[str, Any]:
        r = await self._http.get("/health")
        r.raise_for_status()
        return r.json()

    async def закрыть(self) -> None:
        await self._http.aclose()


ПОМОЩЬ = (
    "Это частный бот: модель loc-rag-docs работает на домашнем сервере и отвечает только владельцу.\n\n"
    "• Напишите вопрос — ответ придёт потоком. Бот помнит последние {пар} пар реплик (до своего перезапуска).\n"
    "• /new — начать разговор заново.\n"
    "• /doc вопрос — ответ по документам организации с источником и цитатой. Перед отправкой в Telegram "
    "ответ проходит фильтр ПД: скрываются фамилии из списка, отчества, инициалы, e-mail и телефоны. "
    "Фильтр не ловит: {не_ловит}.\n"
    "• /help вопрос — ассистент разработчика: вопрос о проекте CRM (документация, схемы данных, API и "
    "git — только чтение), например: /help Какие модели данных есть в проекте?\n"
    "• /status — состояние сервиса и очереди.\n\n"
    "Сообщения идут через серверы Telegram; модель и документы — только на домашнем сервере."
)
СРАЗУ = ("/start", "/help", "/status")      # мгновенные команды — вне очереди бота (ручная проверка)


def мгновенная_команда(текст: str | None) -> bool:
    """/start, /status и /help БЕЗ вопроса — без модели, их можно дать вне очереди. «/help вопрос»
    (День 31) — ответ ассистента через сервис: обычная очередь чата, иначе боковой опрос
    встал бы на минуты."""
    команда, _, остаток = (текст or "").partition(" ")
    команда = команда.split("@")[0].lower()
    return команда in СРАЗУ and not (команда == "/help" and остаток.strip())


def реплик(n: int) -> str:
    форма = "реплика" if n % 10 == 1 and n % 100 != 11 else (
        "реплики" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "реплик")
    return f"{n} {форма}"


def в_html(текст: str) -> str:
    """Частая разметка модели (Markdown) — в HTML Telegram, остальное экранируется. Модель пишет
    «**жирно**», заголовки «###» и списки «*   …»; простым текстом они видны звёздочками (ручная
    проверка владельца). Курсив «*…*» не трогается: звёздочка бывает умножением."""
    т = html.escape(текст or "", quote=False)
    т = re.sub(r"```(?:[\w+-]+\n)?(.+?)```", r"<pre>\1</pre>", т, flags=re.S)
    т = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", т)
    т = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", т)
    т = re.sub(r"(?m)^#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", т)
    т = re.sub(r"(?m)^(\s*)[*-]\s+", r"\1• ", т)
    return т


ПРИМЕР_DOC = "Напишите вопрос после /doc, например: /doc До какого числа нужно оплачивать услуги связи по договору VPN?"


class _Вывод:
    """Одно сообщение бота, которое дописывается: заготовка, правки не чаще порога, итог, продолжение."""

    def __init__(self, бот: "Бот", чат: int) -> None:
        self.бот, self.чат = бот, чат
        self.ид: int | None = None
        self.показано = ""
        self.момент = 0.0
        self.правок = 0
        self.отказов_telegram = 0
        self._ход: asyncio.Task | None = None

    async def начать(self, текст: str, ход: Callable[[float], str] | None = None) -> None:
        ответ = await self.бот.api.sendMessage(self.чат, текст)
        self.ид, self.показано, self.момент = ответ["message_id"], текст, self.бот.часы()
        await self._печатает()
        self._ход = asyncio.ensure_future(self._ждать(ход))

    async def _печатает(self) -> None:
        try:
            await self.бот.api.sendChatAction(self.чат)
        except (ОшибкаTelegram, httpx.HTTPError):
            pass

    async def _ждать(self, ход: Callable[[float], str] | None) -> None:
        """«Печатает…» раз в ПЕЧАТАЕТ_С и, для документов, «… N с» раз в ДОКУМЕНТЫ_ХОД_С — пока нет текста."""
        начало = time.monotonic()
        прошлый_ход = начало
        while True:
            await asyncio.sleep(ПЕЧАТАЕТ_С)
            await self._печатает()
            if ход and time.monotonic() - прошлый_ход >= ДОКУМЕНТЫ_ХОД_С:
                прошлый_ход = time.monotonic()
                await self.правка(ход(time.monotonic() - начало), срочно=True)

    async def хватит_ждать(self) -> None:
        задача, self._ход = self._ход, None
        if задача is None:
            return
        if not задача.done():
            задача.cancel()
        await asyncio.gather(задача, return_exceptions=True)

    async def правка(self, текст: str, срочно: bool = False, разметка: bool = False) -> None:
        текст = текст[:ДЛИНА + 50]
        if текст == self.показано or self.ид is None:
            return
        if not срочно and self.бот.часы() - self.момент < self.бот.правка_с:
            return
        отказов_429 = 0
        while True:
            try:
                await self.бот.api.editMessageText(self.чат, self.ид, в_html(текст) if разметка else текст, разметка)
            except ОшибкаTelegram as exc:
                if exc.повтор:
                    self.отказов_telegram += 1
                    отказов_429 += 1
                    if not срочно:
                        await self.бот.пауза(float(exc.повтор))
                        return
                    if отказов_429 >= 3:
                        raise
                    await self.бот.пауза(float(exc.повтор))
                    continue
                if разметка and exc.код == 400 and "parse" in exc.описание:
                    разметка = False           # Telegram не разобрал HTML — тот же текст простым
                    continue
                if "not modified" not in exc.описание:
                    raise
            self.правок += 1
            self.показано, self.момент = текст, self.бот.часы()
            return

    async def событие(self, вид: str, значение: Any) -> None:
        if вид == "очередь":
            await self.правка(f"⏳ в очереди сервиса: впереди {значение}" if значение else "⏳ модель отвечает…")
        elif вид == "текст":
            await self.хватит_ждать()
            await self.правка(значение[:ДЛИНА] + " ▍")

    async def закончить(self, текст: str, разметка: bool = True) -> None:
        """Итог: первая часть — правкой заготовки, остальное — новыми сообщениями по ДЛИНА; разметка
        модели — HTML Telegram (не разобрал — простой текст)."""
        await self.хватит_ждать()
        части = [текст[i:i + ДЛИНА] for i in range(0, len(текст), ДЛИНА)] or [""]
        await self.правка(части[0], срочно=True, разметка=разметка)
        for часть in части[1:]:
            try:
                await self.бот.api.sendMessage(self.чат, в_html(часть) if разметка else часть, разметка)
            except ОшибкаTelegram as exc:
                if not (разметка and exc.код == 400 and "parse" in exc.описание):
                    raise
                await self.бот.api.sendMessage(self.чат, часть)


class Бот:
    """Логика бота: кто пишет, команды, очередь бота, история, поток, документы, журнал и состояние."""

    def __init__(self, api: BotAPI, сервис: КлиентСервиса, владелец: int | None, каталог: str,
                 пауза: Callable[[float], Awaitable[None]] = asyncio.sleep, правка_с: float = ПРАВКА_С,
                 часы: Callable[[], float] = time.monotonic, сеть: Callable[[], dict] | None = None) -> None:
        self.api, self.сервис, self.владелец = api, сервис, владелец
        self.каталог, self.пауза, self.правка_с, self.часы, self.сеть = каталог, пауза, правка_с, часы, сеть
        os.makedirs(каталог, exist_ok=True)
        self.журнал = Журнал(os.path.join(каталог, ЖУРНАЛ), поля=ПОЛЯ_ЖУРНАЛА)
        self.имя = ""
        self._история: dict[int, list[dict]] = {}
        self._очереди: dict[int, asyncio.Queue] = {}
        self._работники: dict[int, asyncio.Task] = {}
        self._в_работе: Counter = Counter()
        self._чужие: dict[str, float] = {}
        self.события: Counter = Counter()
        self.опросов = 0
        self.ошибок_сети_подряд = 0
        self.последняя_ошибка = ""
        self.последний_опрос = ""
        # Мгновенные команды, отвеченные вне очереди: offset дойдёт до них по порядку и только
        # подтвердит. В памяти — после аварии команда может прийти повторно (безвредно).
        self.опрос_во_время_с = ОПРОС_ВО_ВРЕМЯ_ОТВЕТА_С
        self._отвечено_вне_очереди: set[int] = set()

    # --- кто пишет --------------------------------------------------------------------

    async def обработать(self, обновление: dict) -> None:
        """Совместимый путь: поставить обычный вопрос во внутреннюю очередь."""
        await self._обработать(обновление, последовательно=False)

    async def _обработать_до_конца(self, обновление: dict) -> None:
        """Production polling: вернуть управление лишь после окончательной доставки ответа."""
        await self._обработать(обновление, последовательно=True)

    async def _обработать(self, обновление: dict, последовательно: bool) -> None:
        if not isinstance(обновление, dict):
            self._записать(кто="?", вид="update неверного типа")
            return
        сообщение = обновление.get("message") or {}
        if not isinstance(сообщение, dict) or not сообщение:
            self._записать(кто="?", вид="update пропущен")
            return
        чат = сообщение.get("chat") or {}
        отправитель = сообщение.get("from") or {}
        if not isinstance(чат, dict) or not isinstance(отправитель, dict):
            self._записать(кто="?", вид="update неверного типа")
            return
        от = отправитель.get("id")
        if чат.get("type") != "private":
            self._записать(кто="группа", вид="покинута")
            try:
                await self.api.leaveChat(чат.get("id"))
            except ОшибкаTelegram as exc:
                # Для неподдерживаемой группы только per-update 400/403 терминальны.
                # Общие ошибки токена/API и временные отказы оставляют update для повтора.
                if последовательно and exc.код not in (400, 403):
                    raise
            except httpx.HTTPError:
                if последовательно:
                    raise
            return
        if self.владелец is None:
            self._записать(кто="?", вид="владелец не задан")
            return
        if от != self.владелец:
            await self._чужой(чат.get("id"), от, последовательно)
            return
        текст = сообщение.get("text")
        if not isinstance(текст, str):
            текст = None
        if not isinstance(чат.get("id"), int) or isinstance(чат.get("id"), bool):
            self._записать(кто="владелец", вид="update неверного типа")
            return
        мгновенная = мгновенная_команда(текст)
        if последовательно or мгновенная:
            # Мгновенная команда очередь не занимает: иначе /status, отвеченный во время
            # генерации, считал бы занятым сам себя («ждут ещё: 1» в видео, сцена 4).
            занимает = последовательно and not мгновенная
            if занимает:
                self._в_работе[чат["id"]] += 1
            try:
                await self._ответить(чат["id"], текст)
            finally:
                if занимает:
                    self._в_работе[чат["id"]] -= 1
            return
        await self._в_очередь(чат["id"], текст)

    async def _чужой(self, чат: int, от: Any, последовательно: bool = False) -> None:
        чей = _хэш(от)
        сейчас = self.часы()
        if сейчас - self._чужие.get(чей, -ЧУЖОМУ_РАЗ_В_С) >= ЧУЖОМУ_РАЗ_В_С:
            self._записать(кто="чужой", чей=чей, вид="отказ")
            try:
                await self.api.sendMessage(чат, "Это частный бот.")
            except ОшибкаTelegram as exc:
                # Чужой update не даёт доступа к модели. Только per-update 400/403 у
                # courtesy-сообщения терминальны; прочие отказы требуют повтора.
                if последовательно and exc.код not in (400, 403):
                    raise
            except httpx.HTTPError:
                if последовательно:
                    raise
            else:
                self._чужие[чей] = сейчас
        else:
            self._записать(кто="чужой", чей=чей, вид="тишина")

    # --- одно дело за раз (D-T08) -------------------------------------------------------

    async def _в_очередь(self, чат: int, текст: str | None) -> None:
        очередь = self._очереди.setdefault(чат, asyncio.Queue())
        if self._в_работе[чат]:
            try:
                await self.api.sendMessage(чат, "⏳ Принял — отвечу после текущего ответа.")
            except (ОшибкаTelegram, httpx.HTTPError):
                pass
        self._в_работе[чат] += 1
        await очередь.put(текст)
        if чат not in self._работники or self._работники[чат].done():
            self._работники[чат] = asyncio.ensure_future(self._работник(чат))

    async def _работник(self, чат: int) -> None:
        очередь = self._очереди[чат]
        while True:
            текст = await очередь.get()
            try:
                await self._ответить(чат, текст)
            except Exception as exc:  # noqa: BLE001 — работник не должен умирать от одного сообщения
                self._записать(кто="владелец", вид="ошибка бота", ошибка=без_токена(type(exc).__name__))
                try:
                    await self.api.sendMessage(чат, f"Внутренняя ошибка бота: {type(exc).__name__}.")
                except (ОшибкаTelegram, httpx.HTTPError):
                    pass
            finally:
                self._в_работе[чат] -= 1
                очередь.task_done()

    async def дождаться(self) -> None:
        for очередь in list(self._очереди.values()):
            await очередь.join()

    # --- команды ------------------------------------------------------------------------

    async def _ответить(self, чат: int, текст: str | None) -> None:
        if not текст:
            await self.api.sendMessage(чат, "Пока понимаю только текст: вопрос модели, /doc вопрос, /help вопрос, "
                                           "/new, /status.")
            self._записать(кто="владелец", вид="не текст")
            return
        if not текст.startswith("/"):
            await self._чат(чат, текст)
            return
        команда, _, остаток = текст.partition(" ")
        команда = команда.split("@")[0].lower()
        if команда == "/help" and остаток.strip():
            await self._проект(чат, остаток.strip())
            return
        if команда in ("/start", "/help"):
            ответ = ПОМОЩЬ.format(пар=ИСТОРИЯ // 2, не_ловит=pd.НЕ_ЛОВИТ)
            if not pd.список_есть():
                ответ += "\n\n⚠ Список фамилий не найден — фамилии сейчас не проверяются."
            await self.api.sendMessage(чат, ответ)
        elif команда == "/new":
            self._история.pop(чат, None)
            await self.api.sendMessage(чат, "История очищена — начинаем заново.")
            if остаток.strip():                 # «/new вопрос» — вопрос не теряется (ручная проверка)
                self._записать(кто="владелец", вид="команда", команда=команда)
                await self._чат(чат, остаток.strip())
                return
        elif команда == "/status":
            await self.api.sendMessage(чат, await self._статус(чат))
        elif команда == "/doc":
            if not остаток.strip():
                await self.api.sendMessage(чат, ПРИМЕР_DOC)
            else:
                await self._документы(чат, остаток.strip())
                return
        else:
            await self.api.sendMessage(чат, "Не знаю такой команды. /help — что умею.")
        self._записать(кто="владелец", вид="команда", команда=команда)

    async def _статус(self, чат: int) -> str:
        try:
            з = await self.сервис.здоровье()
        except httpx.HTTPError as exc:
            return f"Сервис не отвечает ({type(exc).__name__}): python cli.py --сервис статус"
        о = з.get("queue") or {}
        занят = self._в_работе[чат]
        return (f"Сервис: {з.get('status')} · модель {з.get('model')}\n"
                f"Очередь: идёт {о.get('идёт')}, ждут {о.get('ждут')} из {о.get('мест_в_очереди')} · "
                f"средняя генерация {о.get('средняя_с')} с\n"
                + ("Сейчас бот отвечает на предыдущее сообщение" + (f"; ждут ещё: {занят - 1}" if занят > 1 else "")
                   + ".\n" if занят else "")
                + f"История этого чата: {реплик(len(self._история.get(чат, [])))} (до перезапуска бота).")

    # --- отказы сервиса (D-T09) -----------------------------------------------------------

    @staticmethod
    def _текст_отказа(итог: dict) -> str:
        код, code, сообщение = итог.get("код"), итог.get("код_ошибки"), итог.get("ошибка") or ""
        if code == "сеть":
            return f"Сервис не отвечает ({сообщение}): python cli.py --сервис статус"
        if код == 401:
            return "Ключ бота не принят сервисом — создайте новый: python cli.py --телеграм-ключ"
        if код == 403:
            return f"У ключа бота нет права на это: {сообщение}"
        if code == "question_too_long":
            return "Вопрос длиннее 1000 знаков — сократите его."
        if code == "context_length_exceeded":
            return f"Вопрос длиннее окна модели: {сообщение}"
        return f"Ошибка сервиса: {код or ''} {code}".strip()

    async def _есть_место(self) -> bool:
        try:
            о = (await self.сервис.здоровье()).get("queue") or {}
        except httpx.HTTPError:
            return False
        return int(о.get("ждут") or 0) < int(о.get("мест_в_очереди") or 0)

    async def _с_повторами(self, вывод: _Вывод, запрос: Callable[[], Awaitable[dict]], начало: float,
                           учёт: dict) -> dict:
        """429 — оба кода повторяемые, до общего срока СРОК_С.

        queue_full: Retry-After сервиса — оценка «медиана недавних генераций × очередь», и она бывает
        и мала (1,5 с при генерации ≈ 70 с), и велика (741 с при очереди, разобравшейся за полторы
        минуты — нашёл check_telegram.py). Поэтому бот раз в ОПРОС_МЕСТА_С смотрит /health (вне лимита
        ключа, модель не трогает) и повторяет, как только место освободилось, но не позже Retry-After.
        rate_limited: пауза по Retry-After — она точная (ведро ключа)."""
        ждали = 0.0                     # сумма пауз: срок считается и по ней, и по часам
        while True:
            итог = await запрос()
            if итог.get("код") != 429:
                return итог
            повтор = float(итог.get("повтор_через") or 5.0)
            прошло = max(self.часы() - начало, ждали)
            if итог.get("код_ошибки") == "queue_full":
                предел = min(повтор, СРОК_С - прошло)
                if предел <= 0:
                    итог["код_ошибки"] = "срок"
                    return итог
                учёт["повторов_429"] += 1
                await вывод.правка("⏳ Очередь сервиса занята (queue_full) — повторю, как только освободится "
                                   f"место (не позже чем через {_секунды(повтор)} с; попытка "
                                   f"{учёт['повторов_429'] + 1}).", срочно=True)
                # Предел — и по сумме пауз (детерминированно в тестах), и по часам: под нагрузкой
                # /health отвечает секундами, и одна сумма пауз отодвигала повтор за Retry-After.
                начало_ожидания = self.часы()
                ждали_места = 0.0
                while True:
                    осталось = предел - max(ждали_места, self.часы() - начало_ожидания)
                    if осталось <= 0:
                        break
                    шаг = min(ОПРОС_МЕСТА_С, осталось)
                    await self.пауза(шаг)
                    ждали_места += шаг
                    if await self._есть_место():
                        break
                ждали += max(ждали_места, self.часы() - начало_ожидания)
            else:
                if прошло + повтор > СРОК_С:
                    итог["код_ошибки"] = "срок"
                    return итог
                учёт["повторов_429"] += 1
                await вывод.правка(f"⏳ Сервис просит подождать ({итог.get('код_ошибки')}) — повторю через "
                                   f"{_секунды(повтор)} с.", срочно=True)
                await self.пауза(повтор)
                ждали += повтор

    # --- чат ------------------------------------------------------------------------------

    async def _чат(self, чат: int, вопрос: str) -> None:
        начало = self.часы()
        вывод = _Вывод(self, чат)
        await вывод.начать("⏳ …")
        сообщения = self._история.get(чат, []) + [{"role": "user", "content": вопрос}]
        учёт = {"повторов_429": 0, "укорочено": 0}
        try:
            while True:
                итог = await self._с_повторами(вывод, lambda: self.сервис.чат(сообщения, вывод.событие), начало, учёт)
                if (итог.get("код") == 413 and итог.get("код_ошибки") == "context_length_exceeded" and
                        len(сообщения) > 2):
                    сообщения = сообщения[2:]
                    учёт["укорочено"] += 1
                    continue
                break
        except BaseException:
            await вывод.хватит_ждать()
            raise
        всего = self.часы() - начало
        if итог.get("код") == 200 and not итог.get("код_ошибки"):
            подпись = (f"— {self.сервис.модель} · вход {итог['вход']} · ответ {итог['ответ']} токенов · "
                       f"очередь {итог['ожидание']:.1f} с · всего {всего:.1f} с")
            if итог.get("причина") == "length":
                подпись += " · обрезано пределом ответа"
            await вывод.закончить(f"{итог['текст']}\n\n{подпись}")
            self._история[чат] = (сообщения + [{"role": "assistant", "content": итог["текст"]}])[-ИСТОРИЯ:]
        elif итог.get("код") == 200:
            await вывод.закончить(f"{итог['текст']}\n\n⚠ поток прервался: {итог['код_ошибки']}".lstrip())
        elif итог.get("код_ошибки") == "срок":
            await вывод.закончить("Очередь сервиса не освободилась за 20 минут — попробуйте позже.")
        else:
            await вывод.закончить(self._текст_отказа(итог))
        self._записать(кто="владелец", вид="чат", код=итог.get("код"), ошибка=итог.get("код_ошибки") or "",
                       вход=итог.get("вход"), ответ=итог.get("ответ"), ожидание_с=round(итог.get("ожидание") or 0, 1),
                       длительность_с=round(всего, 1), правок=вывод.правок,
                       отказов_telegram=вывод.отказов_telegram, **учёт)

    # --- документы (D-T10) ----------------------------------------------------------------

    async def _документы(self, чат: int, вопрос: str) -> None:
        начало = self.часы()
        вывод = _Вывод(self, чат)
        await вывод.начать("⏳ Ищу в документах и отвечаю на этой машине — на CPU это 1–5 минут.",
                           ход=lambda с: f"⏳ Ищу в документах и отвечаю… {с:.0f} с")
        учёт = {"повторов_429": 0}
        try:
            итог = await self._с_повторами(вывод, lambda: self.сервис.документы(вопрос), начало, учёт)
        except BaseException:
            await вывод.хватит_ждать()
            raise
        всего = self.часы() - начало
        скрыто: dict[str, int] = {}
        if итог.get("код") == 200:
            текст, скрыто = собрать_документы(итог["данные"], итог["ожидание"], всего, self.сервис.модель)
        elif итог.get("код_ошибки") == "срок":
            текст = "Очередь сервиса не освободилась за 20 минут — попробуйте позже."
        else:
            текст = self._текст_отказа(итог)
        await вывод.закончить(текст)
        usage = ((итог.get("данные") or {}).get("usage") or {})
        self._записать(кто="владелец", вид="документы", код=итог.get("код"), ошибка=итог.get("код_ошибки") or "",
                       вход=usage.get("prompt_tokens"), ответ=usage.get("completion_tokens"),
                       ожидание_с=round(итог.get("ожидание") or 0, 1), длительность_с=round(всего, 1),
                       правок=вывод.правок, отказов_telegram=вывод.отказов_telegram,
                       скрыто=sum(скрыто.values()), виды_пд=sorted(скрыто), **учёт)

    async def _проект(self, чат: int, вопрос: str) -> None:
        """«/help вопрос» — ассистент разработчика через ручку сервиса /v1/project/help (День 31, FR-13)."""
        начало = self.часы()
        вывод = _Вывод(self, чат)
        await вывод.начать("⏳ Ассистент разработчика: смотрю git и документацию проекта…",
                           ход=lambda с: f"⏳ Ищу в документации проекта и в git… {с:.0f} с")
        учёт = {"повторов_429": 0}
        try:
            итог = await self._с_повторами(вывод, lambda: self.сервис.проект(вопрос), начало, учёт)
        except BaseException:
            await вывод.хватит_ждать()
            raise
        всего = self.часы() - начало
        скрыто: dict[str, int] = {}
        if итог.get("код") == 200:
            текст, скрыто = собрать_проект(итог["данные"], итог["ожидание"], всего)
        elif итог.get("код_ошибки") == "срок":
            текст = "Очередь сервиса не освободилась за 20 минут — попробуйте позже."
        else:
            текст = self._текст_отказа(итог)
            if итог.get("код_ошибки") == "permission_denied":
                текст += ("\nКлючу бота нужно право «проект»: python cli.py --телеграм-ключ, "
                          "затем python cli.py --телеграм перезапуск.")
        await вывод.закончить(текст)
        usage = ((итог.get("данные") or {}).get("usage") or {})
        self._записать(кто="владелец", вид="проект", код=итог.get("код"), ошибка=итог.get("код_ошибки") or "",
                       вход=usage.get("prompt_tokens"), ответ=usage.get("completion_tokens"),
                       ожидание_с=round(итог.get("ожидание") or 0, 1), длительность_с=round(всего, 1),
                       правок=вывод.правок, отказов_telegram=вывод.отказов_telegram,
                       скрыто=sum(скрыто.values()), виды_пд=sorted(скрыто), **учёт)

    # --- опрос (D-T03) --------------------------------------------------------------------

    def _состояние_offset(self) -> tuple[int | None, int | None]:
        основной = os.path.join(self.каталог, OFFSET)
        pending = os.path.join(self.каталог, OFFSET_ПРЕДЫДУЩИЙ)

        def прочитать(путь: str) -> str | None:
            try:
                with open(путь, encoding="ascii") as f:
                    return f.read().strip()
            except FileNotFoundError:
                return None
            except UnicodeError as exc:
                raise ОшибкаCheckpoint("checkpoint повреждён") from exc
            except OSError as exc:
                raise ОшибкаCheckpoint(f"не удалось прочитать checkpoint: {type(exc).__name__}") from exc

        переход = прочитать(pending)
        значение = прочитать(основной)
        if переход is not None:
            части = переход.split()
            if (len(части) != 3 or части[0] != "v1" or
                    части[1] != "none" and not части[1].isdigit() or not части[2].isdigit()):
                raise ОшибкаCheckpoint("pending checkpoint повреждён")
            старый = None if части[1] == "none" else int(части[1])
            новый = int(части[2])
            if старый is not None and новый <= старый:
                raise ОшибкаCheckpoint("pending checkpoint не монотонен")
            допустимые = ({str(старый)} if старый is not None else {None}) | {str(новый)}
            if значение not in допустимые:
                raise ОшибкаCheckpoint("checkpoint не соответствует pending переходу")
            return старый, новый
        if значение is None:
            return None, None
        if not значение.isascii() or not значение.isdigit():
            raise ОшибкаCheckpoint("checkpoint повреждён")
        return int(значение), None

    def _offset(self) -> int | None:
        return self._состояние_offset()[0]

    def _сохранить_offset(self, offset: int) -> None:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ОшибкаCheckpoint("неверный offset")
        основной = os.path.join(self.каталог, OFFSET)
        pending = os.path.join(self.каталог, OFFSET_ПРЕДЫДУЩИЙ)
        предыдущий, незавершённый = self._состояние_offset()
        if незавершённый is not None:
            if offset != незавершённый:
                raise ОшибкаCheckpoint("offset не соответствует незавершённому переходу")
        elif предыдущий is not None and offset <= предыдущий:
            raise ОшибкаCheckpoint("offset должен расти")
        try:
            if незавершённый is None:
                старый = "none" if предыдущий is None else str(предыдущий)
                _записать_атомарно(pending, f"v1 {старый} {offset}\n")
            _записать_атомарно(основной, f"{offset}\n")
            os.unlink(pending)
            _синхронизировать_каталог(self.каталог)
        except OSError as exc:
            raise ОшибкаCheckpoint(f"не удалось сохранить checkpoint: {type(exc).__name__}") from exc

    async def познакомиться(self) -> dict:
        я = await self.api.getMe()
        self.имя = "@" + str(я.get("username") or "")
        return я

    @staticmethod
    def _задержка_telegram(exc: ОшибкаTelegram, пауза: float) -> float | None:
        """Вернуть задержку временного отказа; ``None`` означает постоянный отказ.

        Собственный exponential backoff ограничен 60 секундами. Положительный
        ``retry_after`` Telegram — минимальный срок запрета и потому может быть дольше.
        """
        if exc.код == 429:
            try:
                повтор = float(exc.повтор)
            except (TypeError, ValueError, OverflowError):
                повтор = 0.0
            if not math.isfinite(повтор) or повтор <= 0:
                повтор = 0.0
            return max(пауза, повтор)
        if 500 <= exc.код < 600:
            return пауза
        return None

    async def _пауза_до_стопа(self, стоп: asyncio.Event, секунд: float) -> bool:
        """Подождать задержку с возможностью немедленной остановки процесса."""
        if стоп.is_set():
            return True
        сон = asyncio.ensure_future(self.пауза(секунд))
        остановка = asyncio.ensure_future(стоп.wait())
        try:
            готово, _ = await asyncio.wait({сон, остановка}, return_when=asyncio.FIRST_COMPLETED)
            return остановка in готово and стоп.is_set()
        finally:
            for задача in (сон, остановка):
                if not задача.done():
                    задача.cancel()
            await asyncio.gather(сон, остановка, return_exceptions=True)

    async def познакомиться_с_повторами(self, стоп: asyncio.Event) -> dict:
        """Проверить токен, переживая сеть, Telegram 5xx и 429 с backoff 2…60 с."""
        пауза = 2.0
        while not стоп.is_set():
            try:
                return await self.познакомиться()
            except ОшибкаTelegram as exc:
                задержка = self._задержка_telegram(exc, пауза)
                if задержка is None:
                    raise
                остановлен = await self._сбой(f"Telegram {exc.код}", задержка, стоп)
            except httpx.HTTPError as exc:
                остановлен = await self._сбой(type(exc).__name__, пауза, стоп)
            if остановлен:
                raise asyncio.CancelledError
            пауза = min(пауза * 2, 60.0)
        raise asyncio.CancelledError

    async def опросить_раз(self) -> int:
        """Обработать пакет строго по update_id; checkpoint следует за окончательной доставкой."""
        offset = self._offset()
        обновления = await self.api.getUpdates(offset)
        self.опросов += 1
        self.ошибок_сети_подряд = 0
        self.последний_опрос = time.strftime("%Y-%m-%dT%H:%M:%S")
        проверенные: list[tuple[int, dict]] = []
        for о in обновления:
            if not isinstance(о, dict):
                raise ОшибкаTelegram(502, "элемент getUpdates неверного типа")
            номер = о.get("update_id")
            if not isinstance(номер, int) or isinstance(номер, bool) or номер < 0:
                raise ОшибкаTelegram(502, "update_id неверного типа")
            проверенные.append((номер, о))
        последний: int | None = None
        for номер, о in sorted(проверенные, key=lambda пара: пара[0]):
            if (offset is not None and номер < offset) or номер == последний:
                continue
            if номер in self._отвечено_вне_очереди:
                # Уже отвечено, пока шла генерация: только сдвинуть сплошной префикс.
                self._сохранить_offset(номер + 1)
                self._отвечено_вне_очереди.discard(номер)
                offset, последний = номер + 1, номер
                continue
            история = {чат: list(сообщения) for чат, сообщения in self._история.items()}
            try:
                await self._с_боковым_опросом(номер, self._обработать_до_конца(о))
                self._сохранить_offset(номер + 1)
            except BaseException:
                self._история = история
                raise
            offset, последний = номер + 1, номер
        self.записать_состояние()
        return len(обновления)

    def _мгновенная(self, о: Any) -> bool:
        """/status, /help, /start владельца в личном чате — ответ без модели, его можно дать вне очереди."""
        сообщение = о.get("message") if isinstance(о, dict) else None
        if not isinstance(сообщение, dict):
            return False
        чат, от, текст = сообщение.get("chat"), (сообщение.get("from") or {}), сообщение.get("text")
        if (not isinstance(чат, dict) or чат.get("type") != "private" or not isinstance(от, dict) or
                self.владелец is None or от.get("id") != self.владелец or not isinstance(текст, str)):
            return False
        return мгновенная_команда(текст)

    async def _с_боковым_опросом(self, номер: int, работа: Awaitable[None]) -> None:
        """Выполнить update; пока он идёт, раз в опрос_во_время_с отвечать на мгновенные команды.

        Основной цикл ждёт окончательной доставки (durable offset), поэтому новых getUpdates
        во время генерации нет — и /status ждал минутами (ручная проверка владельца). Боковой
        getUpdates с offset = номер ничего не подтверждает сверх уже подтверждённого: Telegram
        хранит текущий и последующие updates, пока checkpoint до них не дойдёт."""
        задача = asyncio.ensure_future(работа)
        try:
            while True:
                готово, _ = await asyncio.wait({задача}, timeout=self.опрос_во_время_с)
                if готово:
                    return задача.result()
                await self._ответить_вне_очереди(номер)
        finally:
            if not задача.done():
                задача.cancel()
                await asyncio.gather(задача, return_exceptions=True)

    async def _ответить_вне_очереди(self, номер: int) -> None:
        try:
            свежие = await self.api.getUpdates(номер, timeout=0)
        except (ОшибкаTelegram, httpx.HTTPError):
            return                                # боковой опрос — по возможности; ошибки разберёт основной
        for о in свежие if isinstance(свежие, list) else []:
            n = о.get("update_id") if isinstance(о, dict) else None
            if (not isinstance(n, int) or isinstance(n, bool) or n <= номер or
                    n in self._отвечено_вне_очереди or not self._мгновенная(о)):
                continue
            try:
                await self._обработать_до_конца(о)
            except (ОшибкаTelegram, httpx.HTTPError):
                continue                          # не доставлено — ответит основной цикл по порядку
            self._отвечено_вне_очереди.add(n)

    async def опрашивать(self, стоп: asyncio.Event) -> None:
        """Опрос до остановки. Сеть/5xx/429 повторяются; остальные 4xx завершают процесс."""
        пауза = 2.0
        while not стоп.is_set():
            try:
                await self.опросить_раз()
                пауза = 2.0
            except ОшибкаTelegram as exc:
                задержка = self._задержка_telegram(exc, пауза)
                if задержка is None:
                    raise
                if await self._сбой(f"Telegram {exc.код}", задержка, стоп):
                    return
                пауза = min(пауза * 2, 60.0)
            except httpx.HTTPError as exc:
                if await self._сбой(type(exc).__name__, пауза, стоп):
                    return
                пауза = min(пауза * 2, 60.0)

    async def _сбой(self, что: str, пауза: float, стоп: asyncio.Event) -> bool:
        self.ошибок_сети_подряд += 1
        self.последняя_ошибка = f"{time.strftime('%H:%M:%S')} {без_токена(что)}"
        self.события["сбой сети"] += 1
        self.записать_состояние()
        return await self._пауза_до_стопа(стоп, пауза)

    # --- журнал и состояние (D-T12) -------------------------------------------------------

    def _записать(self, **поля: Any) -> None:
        self.события[поля.get("кто", "?")] += 1
        self.журнал.записать(**{к: в for к, в in поля.items() if в is not None})

    def записать_состояние(self) -> None:
        состояние = {"pid": os.getpid(), "бот": self.имя, "владелец_задан": self.владелец is not None,
                     "сервис": self.сервис.адрес, "api": urlparse(self.api.база).hostname,
                     "последний_опрос": self.последний_опрос, "опросов": self.опросов,
                     "ошибок_сети_подряд": self.ошибок_сети_подряд, "последняя_ошибка": self.последняя_ошибка,
                     "события": dict(self.события), "сеть": self.сеть() if self.сеть else None,
                     "обновлено": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _записать_атомарно(os.path.join(self.каталог, СОСТОЯНИЕ), json.dumps(состояние, ensure_ascii=False, indent=1))

    async def закрыть(self) -> None:
        работники = list(self._работники.values())
        for задача in работники:
            задача.cancel()
        if работники:
            await asyncio.gather(*работники, return_exceptions=True)
        try:
            await self.api.закрыть()
        finally:
            await self.сервис.закрыть()


def собрать_документы(д: dict, ожидание: float, всего: float, модель: str) -> tuple[str, dict[str, int]]:
    """Собрать ответ только если все его части удалось отфильтровать и проверить."""
    try:
        return _собрать_документы(д, ожидание, всего, модель)
    except Exception:  # noqa: BLE001 — при любой ошибке исходный документ не должен уйти в Telegram
        return ОТКАЗ_ПД, {}


def _собрать_документы(д: dict, ожидание: float, всего: float, модель: str) -> tuple[str, dict[str, int]]:
    """Ответ по документам для Telegram: текст, источники, цитаты, подпись — каждая часть под маской ПД,
    затем контрольная проверка всего текста (D-T10)."""
    if not isinstance(д, dict):
        raise TypeError("ответ по документам должен быть объектом")
    скрыто: Counter = Counter()

    def поле(значение: Any) -> str:
        if значение is None:
            return ""
        if not isinstance(значение, (str, int, float, bool)):
            raise TypeError("поле ответа по документам должно быть скаляром")
        return str(значение)

    def маска(текст: str) -> tuple[str, bool]:
        чисто, виды = pd.скрыть(текст)
        if pd.найти(чисто) or _остатки_пд(чисто):
            raise ValueError("после маскирования остались признаки ПД")
        скрыто.update(виды)
        return чисто, bool(виды)

    if "abstain" in д and not isinstance(д["abstain"], bool):
        raise TypeError("abstain должен быть логическим")
    if д.get("abstain"):
        части = ["Не знаю: в документах ответа не нашлось."]
        if д.get("clarification"):
            части.append(маска(поле(д["clarification"]))[0])
    else:
        части = [маска(поле(д.get("answer") or ""))[0]]
    источники = д.get("sources") or []
    if not isinstance(источники, list):
        raise TypeError("sources должен быть списком")
    if источники:
        строки = []
        for и in источники:
            if not isinstance(и, dict):
                raise TypeError("элемент sources должен быть объектом")
            номер, источник, раздел = поле(и.get("n")), поле(и.get("source")), поле(и.get("section"))
            строки.append(маска(f"[{номер}] {источник}" + (f" · {раздел}" if раздел else ""))[0])
        части.append("Источники:\n" + "\n".join(строки))
    цитаты = []
    цитаты_ответа = д.get("quotes") or []
    if not isinstance(цитаты_ответа, list):
        raise TypeError("quotes должен быть списком")
    for ц in цитаты_ответа:
        if not isinstance(ц, dict) or not isinstance(ц.get("confirmed"), bool):
            raise TypeError("элемент quotes имеет неверный тип")
        отметка = f"✓ {поле(ц.get('check') or 'подтверждена')}" if ц["confirmed"] else "✗ не подтверждена"
        строка, было = маска(f"[{поле(ц.get('n'))}] «{поле(ц.get('text') or '')}» — {отметка}")
        цитаты.append(строка + (" (сокращена фильтром ПД)" if было else ""))
    if цитаты:
        части.append("Цитаты:\n" + "\n".join(цитаты))
    usage = д.get("usage") or {}
    if not isinstance(usage, dict):
        raise TypeError("usage должен быть объектом")
    вход, ответ = поле(usage.get("prompt_tokens")), поле(usage.get("completion_tokens"))
    части.append(f"— {модель} · по документам · вход {вход or None} · ответ "
                 f"{ответ or None} · очередь {ожидание:.1f} с · всего {всего:.1f} с")
    if скрыто:
        части.append(f"Скрыто фильтром ПД: {sum(скрыто.values())} ({', '.join(sorted(скрыто))}). "
                     f"Фильтр не ловит: {pd.НЕ_ЛОВИТ}.")
    else:
        части.append(f"Фильтр ПД ничего не нашёл. Он не ловит: {pd.НЕ_ЛОВИТ}.")
    if not pd.список_есть():
        части.append("⚠ Список фамилий не найден — фамилии не проверялись.")
    итог = "\n\n".join(ч for ч in части if ч)
    if pd.найти(итог) or _остатки_пд(итог):
        raise ValueError("контрольная проверка нашла признаки ПД")
    return итог, dict(скрыто)


def собрать_проект(д: dict, ожидание: float, всего: float) -> tuple[str, dict[str, int]]:
    """Ответ ассистента разработчика для Telegram (День 31, FR-13): каждая часть — под маской ПД
    ТОЧНОГО профиля (plan D-02a: полный профиль портит ORM, MVP, IP), затем контрольная проверка
    тем же профилем. Любая ошибка — отказ, исходный текст в Telegram не уходит."""
    try:
        return _собрать_проект(д, ожидание, всего)
    except Exception:  # noqa: BLE001
        return ОТКАЗ_ПД, {}


def _собрать_проект(д: dict, ожидание: float, всего: float) -> tuple[str, dict[str, int]]:
    if not isinstance(д, dict):
        raise TypeError("ответ ассистента должен быть объектом")
    скрыто: Counter = Counter()

    def маска(значение: Any) -> str:
        if значение is None:
            return ""
        if not isinstance(значение, (str, int, float, bool)):
            raise TypeError("поле ответа ассистента должно быть скаляром")
        чисто, виды = pd.скрыть(str(значение), профиль="точный")
        скрыто.update(виды)
        return чисто

    части = [маска(д.get("answer") or "")]
    git = д.get("git") or {}
    if not isinstance(git, dict):
        raise TypeError("git должен быть объектом")
    if git.get("branch"):
        части.append(маска(f"git: ветка {git.get('branch')} @ {git.get('commit')}"
                           + (" · рабочий каталог чист" if git.get("clean") else f" · изменено {git.get('changed')}")))
    источники = д.get("sources") or []
    if not isinstance(источники, list):
        raise TypeError("sources должен быть списком")
    if источники:
        части.append("Источники:\n" + "\n".join(
            маска(f"[{и.get('n')}] {и.get('source')}" + (f" · {и.get('section')}" if и.get("section") else ""))
            for и in источники if isinstance(и, dict)))
    вызовы = [в for в in (д.get("tool_calls") or [])[2:] if isinstance(в, dict)]
    if вызовы:
        части.append("Вызовы git (только чтение):\n" + "\n".join(
            маска(f"{'✓' if в.get('ok') else '✗'} {str(в.get('name', '')).split('__')[-1]}"
                  f"({', '.join(f'{к}={з}' for к, з in (в.get('args') or {}).items())}) → {в.get('summary', '')}")
            for в in вызовы))
    for пометка in д.get("notes") or []:
        части.append("⚠ " + маска(пометка))
    usage = д.get("usage") or {}
    части.append(f"— {маска(д.get('model') or '')} · вход {usage.get('prompt_tokens') or 0} · ответ "
                 f"{usage.get('completion_tokens') or 0} ток. · очередь {ожидание:.1f} с · всего {всего:.1f} с")
    итог = "\n\n".join(ч for ч in части if ч)
    if pd.скрыть(итог, профиль="точный")[1]:
        raise ValueError("контрольная проверка нашла признаки ПД")
    return итог, dict(скрыто)


def _синхронизировать_каталог(каталог: str) -> None:
    fd = os.open(каталог or ".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _записать_атомарно(путь: str, текст: str) -> None:
    """Долговечная атомарная замена: уникальный файл, fsync файла, rename и fsync каталога."""
    каталог = os.path.dirname(путь) or "."
    временный = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=каталог,
                                         prefix="." + os.path.basename(путь) + ".", delete=False) as f:
            временный = f.name
            f.write(текст)
            f.flush()
            os.fsync(f.fileno())
        os.replace(временный, путь)
        временный = ""
        _синхронизировать_каталог(каталог)
    finally:
        if временный:
            try:
                os.unlink(временный)
            except FileNotFoundError:
                pass
