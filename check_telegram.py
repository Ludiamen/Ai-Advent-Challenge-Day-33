#!/usr/bin/env python3
"""Доказательный прогон дополнения Дня 30: Telegram-бот (telegram-plan § 4, D-T13).

Настоящий Telegram не проверить автоматически — сообщения пишет человек. Поэтому
на 127.0.0.1 поднимается подменный Bot API (тот же JSON: getUpdates отдаёт
подготовленные сообщения, остальные методы записываются), и против него
запускается НАСТОЯЩИЙ процесс бота `telegram_bot.py` — к ЖИВОМУ сервису и модели.
Бот проверки — со своей памятью (временный каталог) и своим ключом
`проверка-телеграм`: живой бот, его offset и журнал не трогаются.

  Часть 1  БОТ ПОДНИМАЕТСЯ: getMe, опрос, состояние для /admin.
  Часть 2  ЧАТ: ответ потоком (заготовка, правки, подпись); история; /new.
  Часть 3  ТОЛЬКО ВЛАДЕЛЕЦ: чужому — один отказ, в сервис — ни запроса; группа — leaveChat.
  Часть 4  ДОКУМЕНТЫ И ПД: ответ с цитатой; ответ с ПД — «[скрыто]», в отправленном шаблоны ПД не находят ничего.
  Часть 4а /help (День 31): без вопроса — мгновенная справка; «/help вопрос» — ассистент
           разработчика через ручку /v1/project/help: ответ о проекте CRM, источники, ветка git.
  Часть 5  ОГРАНИЧЕНИЯ СЕРВИСА: 413 → бот укоротил историю; очередь занята → 429 → повтор → ответ;
           сервис недоступен → понятное сообщение, процесс жив.
  Часть 6  ПРИВАТНОСТЬ: журнал бота без текстов; токен нигде; сеть процесса бота — внешних 0.
  --живой  НАСТОЯЩИЙ TELEGRAM: getMe, одно сообщение владельцу, сеть живого бота — внешний только api.telegram.org.

Запуск:
    python check_telegram.py                 # ≈ 15–25 минут на CPU
    python check_telegram.py --живой         # плюс настоящий Telegram (одно сообщение владельцу)
    python check_telegram.py --в RESULTS.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.service import config, manage, pd  # noqa: E402
from check_tools import LINE, Отчёт, _секунды  # noqa: E402

КОРЕНЬ = os.path.dirname(os.path.abspath(__file__))
ВЛАДЕЛЕЦ, ЧУЖОЙ = 918273601, 918273602   # девять цифр — не спутать с PID и временем в проверках
ТОКЕН = "999000:CHECK-telegram-day30-token_xxxxxxxxxxxxxxxx"
МАРКЕР = "МАРКЕР-ТЕЛЕГРАМ-ДНЯ-30"
# Поток виден только на ответе длиннее порога правки (1,5 с): однословный ответ из кэша промпта
# приходил целиком за 1,5 с — одной итоговой правкой (пятый прогон проверки).
ВОПРОС_ЧАТА = f"Объясни в трёх предложениях, зачем организации резервное копирование. ({МАРКЕР})"
ВОПРОС_ИСТОРИИ = "Сократи свой ответ до одного предложения."
ВОПРОС_ПОСЛЕ_NEW = "Скажи «да» одним словом."
HELP_ВОПРОС = "Какие модели данных есть в приложении org?"
DOC_СРОК = "До какого числа нужно оплачивать услуги связи по договору VPN с Ростелекомом?"
DOC_ПД = ("Кто контактное лицо абонента по договору с Ростелекомом на стационарную связь (50 номеров) "
          "и как с ним связаться?")


class ПодменныйTelegram:
    """Bot API на 127.0.0.1: тот же JSON, что у api.telegram.org; вызовы записываются."""

    def __init__(self) -> None:
        self.обновления: list[dict] = []
        self.вызовы: list[tuple[float, str, dict]] = []
        self.номер_обновления = 0
        self.номер_сообщения = 1000
        self.замок = threading.Lock()
        внешний = self

        class Обработчик(BaseHTTPRequestHandler):
            def log_message(self, *а):            # тишина: в выводе не должно быть путей с токеном
                pass

            def do_POST(self):
                метод = self.path.rsplit("/", 1)[-1]
                тело = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                ответ = внешний.обработать(метод, тело)
                данные = json.dumps(ответ, ensure_ascii=False).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(данные)))
                    self.end_headers()
                    self.wfile.write(данные)
                except (BrokenPipeError, ConnectionResetError):
                    pass                          # бот остановлен посреди опроса — это не ошибка

        self.сервер = ThreadingHTTPServer(("127.0.0.1", 0), Обработчик)
        self.адрес = f"http://127.0.0.1:{self.сервер.server_address[1]}"
        threading.Thread(target=self.сервер.serve_forever, daemon=True).start()

    def обработать(self, метод: str, тело: dict) -> dict:
        with self.замок:
            self.вызовы.append((time.monotonic(), метод, тело))
        if метод == "getMe":
            return {"ok": True, "result": {"id": 999000, "is_bot": True, "username": "CheckDay30Bot", "can_join_groups": False}}
        if метод == "getUpdates":
            конец = time.monotonic() + min(float(тело.get("timeout") or 0), 1.0)
            while True:
                with self.замок:
                    выдать = [о for о in self.обновления if о["update_id"] >= (тело.get("offset") or 0)]
                if выдать or time.monotonic() >= конец:
                    return {"ok": True, "result": выдать}
                time.sleep(0.1)
        if метод == "sendMessage":
            with self.замок:
                self.номер_сообщения += 1
                return {"ok": True, "result": {"message_id": self.номер_сообщения, "text": тело.get("text")}}
        if метод == "editMessageText":
            return {"ok": True, "result": {"message_id": тело.get("message_id")}}
        return {"ok": True, "result": True}

    def написать(self, текст: str, от: int = ВЛАДЕЛЕЦ, тип: str = "private", чат: int | None = None) -> float:
        with self.замок:
            self.номер_обновления += 1
            n = self.номер_обновления
            self.обновления.append({"update_id": n, "message": {
                "message_id": n, "date": int(time.time()), "text": текст,
                "from": {"id": от, "is_bot": False, "first_name": "Проверка"},
                "chat": {"id": чат if чат is not None else от, "type": тип}}})
        return time.monotonic()

    def после(self, момент: float, чат: int | None = None, метод: str | None = None) -> list[tuple[str, dict]]:
        with self.замок:
            return [(м, т) for в, м, т in self.вызовы if в >= момент and (чат is None or т.get("chat_id") == чат)
                    and (метод is None or м == метод)]

    def дождаться_итога(self, момент: float, признак: str, секунд: float = 1500, чат: int = ВЛАДЕЛЕЦ) -> str | None:
        """Ждать отправку или правку с признаком (подпись ответа) после момента; вернуть текст."""
        конец = time.monotonic() + секунд
        while time.monotonic() < конец:
            for м, т in reversed(self.после(момент, чат)):
                if м in ("sendMessage", "editMessageText") and признак in (т.get("text") or ""):
                    return т["text"]
            time.sleep(0.5)
        return None

    def закрыть(self) -> None:
        self.сервер.shutdown()


class ПроцессБота:
    """telegram_bot.py со своей памятью, подменным Bot API и ключом проверки."""

    def __init__(self, api: str, ключ: str, сервис: str = "") -> None:
        self.память = tempfile.mkdtemp(prefix="check-telegram-")
        данные = os.path.join(self.память, "service")
        os.makedirs(os.path.join(данные, "tls"))
        shutil.copy(os.path.join(manage.каталог_tls(), "ca.crt"), os.path.join(данные, "tls", "ca.crt"))
        путь = os.path.join(данные, "telegram.key")
        Path(путь).write_text(ключ + "\n", encoding="utf-8")
        os.chmod(путь, 0o600)
        self.данные = данные
        окружение = {**os.environ, "TELEGRAM_BOT_TOKEN": ТОКЕН, "TELEGRAM_OWNER_ID": str(ВЛАДЕЛЕЦ),
                     "PYTHONIOENCODING": "utf-8", "PD_NAMES": os.environ.get("PD_NAMES") or os.path.join(КОРЕНЬ, pd.ФАЙЛ_ФАМИЛИЙ)}
        окружение.pop("TELEGRAM_API_BASE", None)
        команда = [sys.executable, os.path.join(КОРЕНЬ, "telegram_bot.py"), "--память", self.память, "--api", api]
        if сервис:
            команда += ["--сервис", сервис]
        self.вывод_путь = os.path.join(self.память, "bot.log")
        self._лог = open(self.вывод_путь, "w", encoding="utf-8")
        self.процесс = subprocess.Popen(команда, stdout=self._лог, stderr=subprocess.STDOUT, cwd=КОРЕНЬ, env=окружение)

    def состояние(self) -> dict:
        try:
            return json.loads(Path(self.данные, "telegram-state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def журнал(self) -> list[dict]:
        try:
            return [json.loads(с) for с in Path(self.данные, "telegram.jsonl").read_text(encoding="utf-8").splitlines()]
        except OSError:
            return []

    def вывод(self) -> str:
        self._лог.flush()
        return Path(self.вывод_путь).read_text(encoding="utf-8")

    def жив(self) -> bool:
        return self.процесс.poll() is None

    def остановить(self) -> None:
        if self.жив():
            self.процесс.terminate()
            try:
                self.процесс.wait(15)
            except subprocess.TimeoutExpired:
                self.процесс.kill()
        self._лог.close()

    def убрать(self) -> None:
        self.остановить()
        shutil.rmtree(self.память, ignore_errors=True)


def журнал_сервиса(ключ: str, с_момента: str) -> list[dict]:
    путь = os.path.join(config.каталог_данных(), "requests.jsonl")
    итог = []
    for с in Path(путь).read_text(encoding="utf-8").splitlines():
        try:
            з = json.loads(с)
        except ValueError:
            continue
        if з.get("ключ") == ключ and з.get("время", "") >= с_момента:
            итог.append(з)
    return итог


def ждать(условие, секунд: float, шаг: float = 0.5):
    конец = time.monotonic() + секунд
    while time.monotonic() < конец:
        значение = условие()
        if значение:
            return значение
        time.sleep(шаг)
    return None


def часть_1(отчёт: Отчёт, tg: ПодменныйTelegram, бот: ПроцессБота) -> None:
    отчёт.часть(1, "Бот поднимается: настоящий процесс telegram_bot.py, подменный Bot API на 127.0.0.1")
    состояние = ждать(lambda: бот.состояние().get("опросов") and бот.состояние(), 60)
    отчёт(f"  процесс бота PID {бот.процесс.pid} · Bot API {tg.адрес} · память проверки {бот.память}")
    отчёт.пункт(bool(состояние) and бот.жив(), "процесс бота жив и опрашивает Bot API (getUpdates)")
    методы = [м for _, м, _ in tg.вызовы]
    отчёт.пункт("getMe" in методы, "при старте — getMe: токен принят")
    с = состояние or {}
    отчёт(f"  состояние для /admin: бот {с.get('бот')} · владелец задан {с.get('владелец_задан')} · опросов {с.get('опросов')}")
    отчёт.пункт(с.get("бот") == "@CheckDay30Bot" and с.get("владелец_задан") is True and str(ВЛАДЕЛЕЦ) not in json.dumps(с),
                "состояние: имя бота, «владелец задан» — без ID владельца")


def часть_2(отчёт: Отчёт, tg: ПодменныйTelegram, ключ_имя: str) -> None:
    отчёт.часть(2, "Чат: ответ потоком, история, /new")
    старт = time.strftime("%Y-%m-%dT%H:%M:%S")
    момент = tg.написать(ВОПРОС_ЧАТА)
    итог = tg.дождаться_итога(момент, "loc-rag-docs · вход")
    вызовы = tg.после(момент, ВЛАДЕЛЕЦ)
    правки = [т["text"] for м, т in вызовы if м == "editMessageText"]
    первое = next((т.get("text") for м, т in вызовы if м == "sendMessage"), "")
    отчёт(f"  заготовка: «{первое}» · правок {len(правки)} · «печатает…» "
          f"{sum(1 for м, _ in вызовы if м == 'sendChatAction')} раз")
    отчёт(f"  итог: «{(итог or '').replace(chr(10), ' ⏎ ')[:400]}»")
    отчёт.пункт(bool(итог) and первое.startswith("⏳") and len(правки) >= 2,
                "ответ потоком: заготовка «⏳», затем правки, в конце — подпись с токенами")
    момент2 = tg.написать(ВОПРОС_ИСТОРИИ)
    итог2 = tg.дождаться_итога(момент2, "loc-rag-docs · вход")
    момент3 = tg.написать("/new")
    сброс = tg.дождаться_итога(момент3, "заново", 60)
    момент4 = tg.написать(ВОПРОС_ПОСЛЕ_NEW)
    итог4 = tg.дождаться_итога(момент4, "loc-rag-docs · вход")
    записи = журнал_сервиса(ключ_имя, старт)
    входы = [з.get("вход") for з in записи if з.get("ручка") == "/v1/chat/completions" and з.get("код") == 200]
    отчёт(f"  «{ВОПРОС_ИСТОРИИ}» → «{(итог2 or '').split(chr(10))[0][:120]}» · /new → «{сброс}» · "
          f"после /new → «{(итог4 or '').split(chr(10))[0]}»")
    отчёт(f"  журнал сервиса, ключ {ключ_имя}: вход по вопросам {входы}")
    отчёт.пункт(len(входы) >= 3 and входы[1] > входы[0] and входы[2] < входы[1],
                "история: второй вопрос ушёл вместе с первым (вход больше), после /new — снова короткий")


def часть_3(отчёт: Отчёт, tg: ПодменныйTelegram, бот: ПроцессБота, ключ_имя: str) -> None:
    отчёт.часть(3, "Только владелец: чужой и группа")
    # Журнал сервиса пишет время с точностью до секунды: сравниваются записи до и после, а не время.
    до = len(журнал_сервиса(ключ_имя, ""))
    момент = tg.написать("Привет, ответь мне", от=ЧУЖОЙ)
    ждать(lambda: tg.после(момент, ЧУЖОЙ, "sendMessage"), 30)
    tg.написать("Ну ответь же", от=ЧУЖОЙ)
    момент_группы = tg.написать("/start", от=ВЛАДЕЛЕЦ, тип="group", чат=-100777)
    ждать(lambda: tg.после(момент_группы, -100777, "leaveChat"), 30)
    time.sleep(3)
    чужому = [т.get("text") for м, т in tg.после(момент, ЧУЖОЙ) if м == "sendMessage"]
    отчёт(f"  чужому (2 сообщения): {чужому}")
    отчёт.пункт(чужому == ["Это частный бот."], "чужому — один короткий отказ, на второе сообщение — тишина")
    в_сервис = журнал_сервиса(ключ_имя, "")[до:]
    отчёт.пункт(not в_сервис, "в журнале сервиса за это время — ни одного запроса бота: чужой текст в модель не ушёл")
    записи = [з for з in бот.журнал() if з.get("кто") == "чужой"]
    отчёт(f"  журнал бота: {[(з.get('кто'), з.get('чей'), з.get('вид')) for з in записи]}")
    отчёт.пункт(len(записи) == 2 and str(ЧУЖОЙ) not in json.dumps(записи), "журнал бота: «чужой» — хэш вместо ID")
    отчёт.пункт(bool(tg.после(момент_группы, -100777, "leaveChat")), "группа — бот её покидает (leaveChat)")


def часть_4(отчёт: Отчёт, tg: ПодменныйTelegram, бот: ПроцессБота) -> None:
    отчёт.часть(4, "Документы и фильтр ПД (/doc)")
    момент = tg.написать("/doc " + DOC_СРОК)
    итог = tg.дождаться_итога(момент, "по документам · вход") or ""
    отчёт(f"  /doc {DOC_СРОК}\n    " + итог.replace("\n", "\n    "))
    отчёт.пункт("20 числа" in итог and "Источники:" in итог and "✓" in итог,
                "ответ по документам: «20 числа», источник и подтверждённая цитата")
    if not pd.список_есть():
        отчёт.пункт(False, "нет pd-names.txt — проверка маски фамилий невозможна")
        return
    момент = tg.написать("/doc " + DOC_ПД)
    итог = tg.дождаться_итога(момент, "по документам · вход") or ""
    находки = pd.найти(итог)
    скрыто = [з for з in бот.журнал() if з.get("вид") == "документы"][-1:] or [{}]
    отчёт(f"  /doc {DOC_ПД}\n    (текст ответа с ПД в отчёт не выводится) скрыто {скрыто[0].get('скрыто')} "
          f"({', '.join(скрыто[0].get('виды_пд') or [])}) · «[скрыто]» в тексте: {итог.count(pd.МАСКА)} · "
          f"шаблоны ПД в отправленном: {находки or 'ничего'}")
    отчёт.пункт(bool(итог) and pd.МАСКА in итог and not находки and (скрыто[0].get("скрыто") or 0) > 0,
                "ответ с ПД: найденное скрыто «[скрыто]», в отправленном тексте шаблоны ПД не находят ничего")
    отчёт.пункт("Фильтр не ловит" in итог or "Он не ловит" in итог, "под ответом сказано, чего фильтр не ловит")


def часть_4а(отчёт: Отчёт, tg: ПодменныйTelegram, бот: ПроцессБота) -> None:
    отчёт.часть("4а", "Ассистент разработчика: /help без вопроса и /help вопрос (День 31)")
    момент = tg.написать("/help")
    справка = tg.дождаться_итога(момент, "/help вопрос", секунд=30) or ""
    секунд = time.monotonic() - момент
    отчёт(f"  /help → справка за {секунд:.1f} с: «{справка[:90]}…»")
    отчёт.пункт("/help вопрос" in справка and секунд < 20, "/help без вопроса — мгновенная справка, в ней /help вопрос")
    момент = tg.написать("/help " + HELP_ВОПРОС)
    итог = tg.дождаться_итога(момент, "ток. · очередь") or ""
    отчёт(f"  /help {HELP_ВОПРОС} — за {time.monotonic() - момент:.0f} с\n    " + итог[:700].replace("\n", "\n    "))
    записи = [з for з in бот.журнал() if з.get("вид") == "проект"]
    отчёт.пункт("Unit" in итог and "Источники:" in итог and "models.py" in итог and "ветка " in итог,
                "/help вопрос — ответ о проекте: модели из models.py, источники и ветка git")
    отчёт.пункт(bool(записи) and записи[-1].get("код") == 200 and HELP_ВОПРОС not in json.dumps(бот.журнал(), ensure_ascii=False),
                "журнал бота: «проект» код 200, текста вопроса нет")


def часть_5(отчёт: Отчёт, tg: ПодменныйTelegram, бот: ПроцессБота, сервис_адрес: str, ca: str) -> None:
    отчёт.часть(5, "Ограничения сервиса глазами бота: 413, 429, сервис недоступен")
    # 413: два длинных вопроса подряд — вместе больше предела входа (3400), каждый — меньше
    # (≈ 18 токенов на повтор фразы: 110 повторов ≈ 2000 токенов).
    абзац = "Это проверочный текст про сеть, связь и резервное копирование данных организации. " * 110
    момент = tg.написать("Сколько раз повторяется первое слово? Ответь числом. " + абзац)
    tg.дождаться_итога(момент, "loc-rag-docs · вход")
    момент = tg.написать("А теперь ответь «готово». " + абзац)
    итог = tg.дождаться_итога(момент, "loc-rag-docs · вход") or ""
    укорочено = [з for з in бот.журнал() if з.get("вид") == "чат"][-1].get("укорочено")
    отчёт(f"  два вопроса по ≈ 2000 токенов: второй — укорочено пар {укорочено}, итог «{итог.split(chr(10))[-1]}»")
    отчёт.пункт(bool(итог) and (укорочено or 0) >= 1, "413 context_length_exceeded — бот сам убрал старую пару и ответил")

    # 429: очередь сервиса занята другими клиентами (1 + 4 потока другим ключом).
    занять = manage.создать_ключ("проверка-занять", ["чат"])
    потоки = []

    def поток():
        try:
            with httpx.Client(base_url=сервис_адрес, verify=ca, timeout=900) as к:
                к.post("/v1/chat/completions", headers={"Authorization": f"Bearer {занять}"},
                       json={"model": "loc-rag-docs", "max_tokens": 60,
                             "messages": [{"role": "user", "content": "Перечисли десять городов России."}]})
        except httpx.HTTPError:
            pass
    try:
        for _ in range(5):
            п = threading.Thread(target=поток, daemon=True)
            п.start()
            потоки.append(п)
            time.sleep(0.3)
        time.sleep(2)
        момент = tg.написать("Ответь одним словом: столица России?")
        итог = tg.дождаться_итога(момент, "loc-rag-docs · вход") or ""
        секунд = time.monotonic() - момент
        тексты = [т.get("text") or "" for м, т in tg.после(момент, ВЛАДЕЛЕЦ)]
        повтор = next((т for т in тексты if "освободится место" in т), "")
        запись = [з for з in бот.журнал() if з.get("вид") == "чат"][-1]
        # Retry-After — когда повторить, а не когда придёт ответ. Раундов 429 бывает несколько
        # (Retry-After мал, место ещё не освободилось): каждый раунд — своё сообщение «… попытка K»,
        # и каждый сверяется со своим Retry-After — до следующей правки (новый 429 или новый запрос).
        with tg.замок:
            правки = [(в, т.get("text") or "") for в, м, т in tg.вызовы
                      if в >= момент and м == "editMessageText" and т.get("chat_id") == ВЛАДЕЛЕЦ]
        раунды = []
        for в, т in правки:
            if "освободится место" not in т:
                continue
            число = re.search(r"не позже чем через (\d+(?:\.\d+)?) с", т)
            следующая = next((в2 for в2, _ in правки if в2 > в), None)
            if число and следующая is not None:
                раунды.append((следующая - в, float(число.group(1))))
        отчёт(f"  очередь занята 5 потоками другим ключом → «{повтор}» → раундов 429: {len(раунды)}, повтор через "
              + ", ".join(f"{через:.0f} с (Retry-After {ra:g} с)" for через, ra in раунды)
              + f" → итог «{итог.split(chr(10))[0]}» через {секунд:.0f} с (после повтора — своя очередь)")
        отчёт.пункт(bool(повтор) and bool(итог) and (запись.get("повторов_429") or 0) >= 1,
                    "429 queue_full — бот сообщил об очереди, дождался места и ответа")
        # Раньше Retry-After бот повторяет, только если место освободилось раньше (тест
        # test_queue_full_не_ждёт_весь_retry_after); здесь — что ни один раунд не дольше своего
        # Retry-After (запас 5 с — сам запрос и правка под нагрузкой).
        отчёт.пункт(bool(раунды) and all(через <= ra + 5 for через, ra in раунды),
                    "каждый повтор не позже своего Retry-After: место бот ждёт по /health и по часам")
    finally:
        for п in потоки:
            п.join(900)
        manage.отозвать("проверка-занять")

    # Сервис недоступен: второй бот проверки — на закрытый порт.
    tg2 = ПодменныйTelegram()
    бот2 = ПроцессБота(tg2.адрес, "sk-day30-fake-key-service-is-down", сервис="https://127.0.0.1:9")
    try:
        ждать(lambda: бот2.состояние().get("опросов"), 60)
        момент = tg2.написать("Ты здесь?")
        итог = tg2.дождаться_итога(момент, "Сервис не отвечает", 120) or ""
        отчёт(f"  сервис недоступен (порт 9): «{итог}» · процесс бота жив: {бот2.жив()}")
        отчёт.пункт("Сервис не отвечает" in итог and бот2.жив(), "сервис недоступен — понятное сообщение, бот не падает")
    finally:
        бот2.убрать()
        tg2.закрыть()


def часть_6(отчёт: Отчёт, бот: ПроцессБота) -> None:
    отчёт.часть(6, "Приватность: журнал бота, токен, сеть процесса бота")
    журнал = Path(бот.данные, "telegram.jsonl").read_text(encoding="utf-8")
    состояние = бот.состояние()
    вывод = бот.вывод()
    отчёт(f"  журнал бота: {len(журнал.splitlines())} строк; маркер вопросов проверки: "
          f"{'есть' if МАРКЕР in журнал else 'нет'}")
    отчёт.пункт(МАРКЕР not in журнал and str(ВЛАДЕЛЕЦ) not in журнал, "в журнале бота нет текстов вопросов и ID владельца")
    секрет = ТОКЕН.split(":")[1]
    где = [имя for имя, текст in (("журнал бота", журнал), ("состояние", json.dumps(состояние)), ("вывод процесса", вывод))
           if секрет in текст]
    отчёт(f"  токен Bot API найден: {где or 'нигде'} (журнал, состояние, вывод процесса)")
    отчёт.пункт(not где, "токен бота не попадает ни в журнал, ни в состояние, ни в вывод процесса")
    сеть = состояние.get("сеть") or {}
    отчёт(f"  сеть процесса бота: внешних {сеть.get('внешних')} {сеть.get('внешние_хосты')}, локальных "
          f"{сеть.get('локальных')}, заблокировано {сеть.get('заблокировано')}")
    отчёт.пункт(сеть.get("внешних") == 0 and сеть.get("заблокировано") == 0 and (сеть.get("локальных") or 0) > 0,
                "офлайн-предохранитель в процессе бота: с подменным Bot API на 127.0.0.1 внешних 0, блокировок 0")


def живая_часть(отчёт: Отчёт) -> None:
    отчёт.часть(7, "Живая: настоящий Telegram — getMe, сообщение владельцу, сеть живого бота")
    с = manage.секреты_бота()
    if not (с["TELEGRAM_BOT_TOKEN"] and с["TELEGRAM_OWNER_ID"]):
        отчёт.пункт(False, "нет TELEGRAM_BOT_TOKEN или TELEGRAM_OWNER_ID в .env")
        return
    база = f"https://api.telegram.org/bot{с['TELEGRAM_BOT_TOKEN']}"
    with httpx.Client(timeout=30) as к:
        я = к.post(f"{база}/getMe").json().get("result") or {}
        отчёт(f"  getMe: @{я.get('username')} · can_join_groups {я.get('can_join_groups')}")
        отчёт.пункт(bool(я.get("username")) and я.get("can_join_groups") is False, "бот отвечает; в группы его не добавить")
        ответ = к.post(f"{база}/sendMessage", json={"chat_id": int(с["TELEGRAM_OWNER_ID"]),
                                                    "text": "Проверка связи (check_telegram.py --живой): бот на домашнем "
                                                            "сервере доступен через Telegram."}).json()
        отчёт.пункт(bool(ответ.get("ok")), "сообщение владельцу ушло (ok)")
    служба = manage.состояние_бота()
    сеть = ((служба.get("состояние") or {}).get("сеть") or {})
    if служба["служба"].get("состояние") == "active":
        отчёт(f"  живой бот (служба day31-tg): внешние хосты {сеть.get('внешние_хосты')}, заблокировано {сеть.get('заблокировано')}")
        отчёт.пункт(сеть.get("внешние_хосты") == ["api.telegram.org"] and сеть.get("заблокировано") == 0,
                    "сеть живого бота: внешний — только api.telegram.org, блокировок 0")
    else:
        отчёт("  служба day31-tg не запущена — сеть живого бота не проверялась")


def main() -> int:
    разбор = argparse.ArgumentParser(description="Проверка Дня 30, дополнение: Telegram-бот.")
    разбор.add_argument("--живой", action="store_true", help="плюс настоящий Telegram: getMe и сообщение владельцу")
    разбор.add_argument("--в", dest="out", default="", metavar="ФАЙЛ")
    а = разбор.parse_args()
    отчёт = Отчёт()
    начало = time.monotonic()
    н = config.загрузить()
    сервис_адрес = f"https://127.0.0.1:{н.порт}"
    ca = os.path.join(manage.каталог_tls(), "ca.crt")
    ключ_имя = "проверка-телеграм"
    отчёт(LINE)
    отчёт(f"Проверка Дня 30, дополнение: Telegram-бот · сервис {сервис_адрес} · модель {н.модель}")
    tg, бот = None, None
    try:
        manage.отозвать(ключ_имя)
        ключ = manage.создать_ключ(ключ_имя, list(manage.ПРАВА_БОТА))
        tg = ПодменныйTelegram()
        бот = ПроцессБота(tg.адрес, ключ)
        часть_1(отчёт, tg, бот)
        часть_2(отчёт, tg, ключ_имя)
        часть_3(отчёт, tg, бот, ключ_имя)
        часть_4(отчёт, tg, бот)
        часть_4а(отчёт, tg, бот)
        часть_5(отчёт, tg, бот, сервис_адрес, ca)
        часть_6(отчёт, бот)
        if а.живой:
            живая_часть(отчёт)
    except (httpx.HTTPError, subprocess.SubprocessError, RuntimeError, OSError, ValueError, KeyError, IndexError) as exc:
        отчёт.пункт(False, f"прервано: {type(exc).__name__}: {exc}")
    finally:
        if бот:
            if бот.жив() is False:
                отчёт(f"  вывод процесса бота:\n{бот.вывод()[-1500:]}")
            бот.убрать()
        if tg:
            tg.закрыть()
        manage.отозвать(ключ_имя)
    отчёт(LINE)
    отчёт(f"ИТОГ: проверок {отчёт.проверок}, провалов {отчёт.провалов}. Прогон {_секунды(time.monotonic() - начало)}.")
    if а.out:
        with open(а.out, "a", encoding="utf-8") as f:
            f.write("\n```text\n" + "\n".join(отчёт.строки) + "\n```\n")
    return 1 if отчёт.провалов else 0


if __name__ == "__main__":
    sys.exit(main())
