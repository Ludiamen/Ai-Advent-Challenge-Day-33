"""Isolated regressions for Day-30 gateway request validation."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import tempfile
import unittest

import httpx
from fastapi.testclient import TestClient

from agent.service import config, gateway, journal, keys

МАРКЕРЫ = ("PRIVATE_TEST_PATH_12345", "PRIVATE_TEST_TOKEN_67890")


class _Tokenizer:
    def вход(self, сообщения):
        return sum(len(м["content"]) for м in сообщения)


class ВалидацияШлюза(unittest.TestCase):
    ПРЕДЕЛ = 8192

    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="gateway-validation-")
        self.addCleanup(shutil.rmtree, self.каталог, True)
        self.ключи = keys.Ключи(os.path.join(self.каталог, "keys.json"))
        self.ключ_чат = self.ключи.создать("чат", ["чат"])
        self.ключ_док = self.ключи.создать("док", ["документы"])
        self.ключ_админ = self.ключи.создать("админ", ["админ"])
        self.вызовы_модели = []

        async def ollama(request):
            self.вызовы_модели.append(json.loads(request.content))
            return httpx.Response(200, json={"message": {"content": "ok"}, "done": True,
                                             "prompt_eval_count": 1, "eval_count": 1})

        self.вызовы_документов = []
        self.вызовы_шага = []
        self.вызовы_нагрузки = []
        self.вызовы_телеграм = []
        лимиты = config.Лимиты(запросов_в_минуту=100, тело_байт=self.ПРЕДЕЛ)
        self.сервис = gateway.Сервис(
            config.Настройки(лимиты=лимиты), self.ключи,
            journal.Журнал(os.path.join(self.каталог, "requests.jsonl")), _Tokenizer(),
            httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(ollama)),
            отвечатель=lambda в: self.вызовы_документов.append(в) or {"answer": "ok", "usage": {}},
            шаг=lambda с, ш: self.вызовы_шага.append((с, ш)) or {"ok": True},
            нагрузка=lambda п: self.вызовы_нагрузки.append(п) or {"ok": True},
            телеграм=lambda д: self.вызовы_телеграм.append(д) or {"ok": True})
        self.app = gateway.создать_приложение(self.сервис)
        self.клиент = TestClient(self.app, base_url="https://testserver", client=("192.168.1.31", 50000))

    @staticmethod
    def _h(ключ):
        return {"Authorization": f"Bearer {ключ}", "Content-Type": "application/json"}

    def _без_маркеров(self, response):
        клиентские_данные = response.text + "\n" + "\n".join(
            f"{имя}: {значение}" for имя, значение in response.headers.items())
        for маркер in МАРКЕРЫ:
            self.assertNotIn(маркер, клиентские_данные)

    async def _raw(self, method, path, chunks, headers=(), client=("192.168.1.31", 50000), complete=True):
        """Invoke ASGI directly and expose how many request chunks were pulled."""
        получено = 0
        ответ = []

        async def receive():
            nonlocal получено
            if получено >= len(chunks):
                return {"type": "http.disconnect"}
            body = chunks[получено]
            получено += 1
            return {"type": "http.request", "body": body,
                    "more_body": получено < len(chunks) or not complete}

        async def send(message):
            ответ.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": b"",
                 "root_path": "", "headers": list(headers), "client": client,
                 "server": ("testserver", 443)}
        await self.app(scope, receive, send)
        статус = next(m["status"] for m in ответ if m["type"] == "http.response.start")
        тело = b"".join(m.get("body", b"") for m in ответ if m["type"] == "http.response.body")
        return статус, тело, получено

    def test_фактический_лимит_без_и_с_заниженным_content_length(self):
        половина = self.ПРЕДЕЛ // 2
        куски = [b"x" * половина, b"y" * (половина + 1), b"must-not-be-read"]
        авторизация = (b"authorization", f"Bearer {self.ключ_док}".encode())
        for имя, headers in (("без", (авторизация,)),
                              ("занижен", (авторизация, (b"content-length", b"1")))):
            with self.subTest(content_length=имя):
                код, тело, прочитано = asyncio.run(self._raw(
                    "POST", "/v1/documents/answer", куски, headers))
                self.assertEqual((код, json.loads(тело)["error"]["code"], прочитано),
                                 (413, "request_too_large", 2))
        код, _, прочитано = asyncio.run(self._raw(
            "POST", "/v1/documents/answer", [b"x" * (self.ПРЕДЕЛ - 1), "я".encode(), b"must-not-be-read"],
            (авторизация,)))
        self.assertEqual((код, прочитано), (413, 2), "лимит считает UTF-8 байты, а не символы")
        self.assertEqual(self.вызовы_документов, [])
        записи = pathlib.Path(self.каталог, "requests.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(записи), 3, "по одной записи на каждый 413")

    def test_ровно_предел_принят_больше_отклонен(self):
        основа = json.dumps({"question": "ok"}).encode()
        тело_на_границе = основа + b" " * (self.ПРЕДЕЛ - len(основа))
        заголовки = ((b"authorization", f"Bearer {self.ключ_док}".encode()),)
        код, _, прочитано = asyncio.run(self._raw(
            "POST", "/v1/documents/answer", [тело_на_границе], заголовки))
        self.assertEqual((код, прочитано), (200, 1))
        код, тело, _ = asyncio.run(self._raw(
            "POST", "/v1/documents/answer", [тело_на_границе + b" "], заголовки))
        self.assertEqual((код, json.loads(тело)["error"]["code"]), (413, "request_too_large"))

    def test_фрагментированный_json_доходит_до_всех_post_ручек(self):
        случаи = (
            ("/v1/chat/completions", self.ключ_чат, {"messages": [{"role": "user", "content": "x"}]}),
            ("/v1/documents/answer", self.ключ_док, {"question": "ok"}),
            ("/admin/api/step", self.ключ_админ, {"событие": "начало", "ступень": "1"}),
            ("/admin/api/load", self.ключ_админ, {"ступени": [1], "вопросов": 1, "документы": 0}),
            ("/admin/api/telegram", self.ключ_админ, {"действие": "старт"}),
        )
        for путь, ключ, объект in случаи:
            тело = json.dumps(объект, ensure_ascii=False).encode()
            середина = len(тело) // 2
            заголовки = ((b"authorization", f"Bearer {ключ}".encode()),
                         (b"content-type", b"application/json"))
            with self.subTest(path=путь):
                код, _, прочитано = asyncio.run(self._raw(
                    "POST", путь, [тело[:середина], тело[середина:]], заголовки))
                self.assertEqual((код, прочитано), (200, 2))

    def test_тело_не_читается_до_проверки_сети_и_ключа(self):
        for имя, headers, client, ожидаем in (
                ("без ключа", (), ("192.168.1.31", 1), 401),
                ("неверный ключ", ((b"authorization", b"Bearer sk-day30-no-such-key"),),
                 ("192.168.1.31", 1), 401),
                ("нет права", ((b"authorization", f"Bearer {self.ключ_чат}".encode()),),
                 ("192.168.1.31", 1), 403),
                ("чужая сеть", (), ("10.99.0.2", 1), 403)):
            with self.subTest(case=имя):
                код, _, прочитано = asyncio.run(self._raw(
                    "POST", "/v1/documents/answer", [b"x" * (self.ПРЕДЕЛ + 1)], headers, client))
                self.assertEqual((код, прочитано), (ожидаем, 0))

        self.сервис.ведро._вёдра["док"] = (0.0, self.сервис.ведро.часы())
        headers = ((b"authorization", f"Bearer {self.ключ_док}".encode()),)
        код, _, прочитано = asyncio.run(self._raw(
            "POST", "/v1/documents/answer", [b"x" * (self.ПРЕДЕЛ + 1)], headers))
        self.assertEqual((код, прочитано), (429, 0), "rate limit проверяется до чтения тела")

    def test_обрыв_тела_даёт_согласованную_400(self):
        заголовки = ((b"authorization", f"Bearer {self.ключ_док}".encode()),)
        код, тело, прочитано = asyncio.run(self._raw(
            "POST", "/v1/documents/answer", [b'{"question":'], заголовки, complete=False))
        self.assertEqual((код, json.loads(тело)["error"]["code"], прочитано),
                         (400, "incomplete_request", 1))

    def test_огромный_content_length_отклоняется_без_чтения(self):
        заявлено = b"9" * 5000
        код, тело, прочитано = asyncio.run(self._raw(
            "POST", "/v1/chat/completions", [b'{}'], ((b"content-length", заявлено),)))
        self.assertEqual((код, json.loads(тело)["error"]["code"], прочитано),
                         (413, "request_too_large", 0))

    def test_невалидный_json_и_не_объект_на_всех_post(self):
        ручки = (("/v1/chat/completions", self.ключ_чат),
                  ("/v1/documents/answer", self.ключ_док),
                  ("/admin/api/step", self.ключ_админ),
                  ("/admin/api/load", self.ключ_админ),
                  ("/admin/api/telegram", self.ключ_админ))
        for путь, ключ in ручки:
            for тело in (b"{", b'"text"', b"42", b"null", b"[]", b'{"x":"\xff"}', b'{"x":NaN}'):
                with self.subTest(path=путь, body=тело):
                    r = self.клиент.post(путь, headers=self._h(ключ), content=тело)
                    self.assertEqual(r.status_code, 400, r.text)
                    self.assertEqual(r.json()["error"]["type"], "invalid_request_error")
                    self.assertIn(r.json()["error"]["code"], ("invalid_json", "invalid_request_body"))

    def test_слишком_глубокий_json_даёт_400(self):
        тело = (b'{"messages":' + b"[" * 1100 + b"]" * 1100 + b"}")
        r = self.клиент.post("/v1/chat/completions", headers=self._h(self.ключ_чат), content=тело)
        self.assertEqual((r.status_code, r.json()["error"]["type"]), (400, "invalid_request_error"))

    def test_экранированный_unicode_суррогат_даёт_400(self):
        тело = b'{"messages":[{"role":"user","content":"\\ud800"}]}'
        r = self.клиент.post("/v1/chat/completions", headers=self._h(self.ключ_чат), content=тело)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_json"))

        валидная_пара = b'{"messages":[{"role":"user","content":"\\ud83d\\ude00"}]}'
        r = self.клиент.post("/v1/chat/completions", headers=self._h(self.ключ_чат), content=валидная_пара)
        self.assertEqual(r.status_code, 200, r.text)

    def test_типы_полей_чата_отклонены_до_модели(self):
        база = {"messages": [{"role": "user", "content": "x"}]}
        невалидные = [
            {"messages": None}, {"messages": {}}, {"messages": [1]},
            {"messages": [{"role": "other", "content": "x"}]},
            {"messages": [{"role": "user", "content": 1}]},
            {"messages": [{"role": "user", "content": [{"type": "text", "text": 1}]}]},
            {**база, "model": 1}, {**база, "model": False}, {**база, "model": 0},
            {**база, "model": []}, {**база, "model": {}}, {**база, "stream": "false"},
            {**база, "max_tokens": True}, {**база, "max_tokens": 1.5},
            {**база, "max_tokens": 10, "max_completion_tokens": []},
            {**база, "temperature": False}, {**база, "temperature": 1e309},
        ]
        for тело in невалидные:
            with self.subTest(body=тело):
                r = self.клиент.post("/v1/chat/completions", headers=self._h(self.ключ_чат),
                                             content=json.dumps(тело).encode())
                self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.вызовы_модели, [])

    def test_огромные_числа_дают_http_400(self):
        префикс = b'{"messages":[{"role":"user","content":"x"}],"temperature":'
        r = self.клиент.post("/v1/chat/completions", headers=self._h(self.ключ_чат),
                                     content=префикс + b"9" * 1000 + b"}")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_temperature"))

        тело = {"messages": [{"role": "user", "content": "x"}], "max_tokens": "9" * 5000}
        r = self.клиент.post("/v1/chat/completions", headers=self._h(self.ключ_чат), json=тело)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "invalid_max_tokens"))

    def test_числовые_строки_остались_совместимы(self):
        r = self.клиент.post("/v1/chat/completions", headers=self._h(self.ключ_чат), json={
            "messages": [{"role": "user", "content": [{"type": "text", "text": "x"}]}],
            "max_tokens": "7", "temperature": "0.25", "unknown": "preserved-compatible"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.вызовы_модели[-1]["options"]["num_predict"], 7)
        self.assertEqual(self.вызовы_модели[-1]["options"]["temperature"], 0.25)

        r = self.клиент.post("/v1/chat/completions", headers=self._h(self.ключ_чат), json={
            "messages": [{"role": "user", "content": "x"}], "model": None, "stream": None,
            "max_tokens": None, "max_completion_tokens": None, "temperature": None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.вызовы_модели[-1]["options"],
                         {"num_ctx": 4096, "num_predict": 600, "temperature": 0.0})

    def test_max_tokens_ноль_сохраняет_прежний_fallback(self):
        база = {"messages": [{"role": "user", "content": "x"}], "max_tokens": 0}
        r = self.клиент.post("/v1/chat/completions", headers=self._h(self.ключ_чат),
                                     json={**база, "max_completion_tokens": 7})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.вызовы_модели[-1]["options"]["num_predict"], 7)

        r = self.клиент.post("/v1/chat/completions", headers=self._h(self.ключ_чат), json=база)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.вызовы_модели[-1]["options"]["num_predict"], 600)

    def test_документы_и_admin_проверяют_типы_до_callback(self):
        случаи = (
            ("/v1/documents/answer", self.ключ_док, {"question": ["x"]}),
            ("/admin/api/step", self.ключ_админ, {"событие": "нет", "ступень": 1}),
            ("/admin/api/step", self.ключ_админ, {"событие": "начало", "ступень": []}),
            ("/admin/api/load", self.ключ_админ, {"ступени": [1, True]}),
            ("/admin/api/load", self.ключ_админ, {"вопросов": "1.5"}),
            ("/admin/api/telegram", self.ключ_админ, {"действие": 1}),
        )
        for путь, ключ, тело in случаи:
            with self.subTest(path=путь, body=тело):
                r = self.клиент.post(путь, headers=self._h(ключ), json=тело)
                self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual((self.вызовы_документов, self.вызовы_шага,
                          self.вызовы_нагрузки, self.вызовы_телеграм), ([], [], [], []))

    def test_корректные_admin_параметры_доходят_до_callback(self):
        r = self.клиент.post("/admin/api/step", headers=self._h(self.ключ_админ),
                                     json={"событие": "начало", "ступень": 2})
        self.assertEqual((r.status_code, self.вызовы_шага), (200, [("начало", "2")]))
        r = self.клиент.post("/admin/api/load", headers=self._h(self.ключ_админ),
                                     json={"ступени": ["1", 2], "вопросов": "1", "документы": 0,
                                           "max_tokens": "200", "extra": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.вызовы_нагрузки[-1]["extra"], True)

    def test_исключение_rag_не_раскрывает_внутренние_данные(self):
        успешный = self.клиент.post("/v1/documents/answer", headers=self._h(self.ключ_док),
                                    json={"question": "успешный синтетический вопрос"})
        self.assertEqual((успешный.status_code, успешный.json()["answer"]), (200, "ok"))

        вызовы = []

        def сломанный_rag(вопрос):
            вызовы.append(вопрос)
            raise RuntimeError(f"{МАРКЕРЫ[0]} {МАРКЕРЫ[1]}")

        self.сервис.отвечатель = сломанный_rag
        отказ = self.клиент.post("/v1/documents/answer", headers=self._h(self.ключ_чат),
                                 json={"question": "не должен дойти"})
        self.assertEqual((отказ.status_code, отказ.json()["error"]["code"]), (403, "permission_denied"))
        self.assertEqual(вызовы, [], "отказ по праву не вызывает RAG")

        r = self.клиент.post("/v1/documents/answer", headers=self._h(self.ключ_док),
                             json={"question": "контролируемый сбой"})
        self.assertEqual((r.status_code, r.json()["error"]), (502, {
            "message": gateway.СООБЩЕНИЕ_ДОКУМЕНТЫ_НЕДОСТУПНЫ,
            "type": "server_error",
            "code": "documents_error",
        }))
        self.assertEqual(вызовы, ["контролируемый сбой"], "после исключения RAG не повторяется")
        self._без_маркеров(r)

    def test_ошибки_ollama_безопасны_в_json_и_sse(self):
        успешный = self.клиент.post("/v1/chat/completions", headers=self._h(self.ключ_чат), json={
            "messages": [{"role": "user", "content": "успешный синтетический вопрос"}]})
        self.assertEqual((успешный.status_code, успешный.json()["choices"][0]["message"]["content"]),
                         (200, "ok"))

        вызовы = []

        async def сломанная_ollama(request):
            вызовы.append(json.loads(request.content))
            raise httpx.ConnectError(f"{МАРКЕРЫ[0]} {МАРКЕРЫ[1]}", request=request)

        self.сервис.ollama = httpx.AsyncClient(
            base_url="http://ollama", transport=httpx.MockTransport(сломанная_ollama))

        отказ = self.клиент.post("/v1/chat/completions", headers=self._h(self.ключ_док), json={
            "messages": [{"role": "user", "content": "не должен дойти"}]})
        self.assertEqual((отказ.status_code, отказ.json()["error"]["code"]), (403, "permission_denied"))
        self.assertEqual(вызовы, [], "отказ по праву не вызывает Ollama")

        обычный = self.клиент.post("/v1/chat/completions", headers=self._h(self.ключ_чат), json={
            "messages": [{"role": "user", "content": "контролируемый сбой JSON"}]})
        self.assertEqual((обычный.status_code, обычный.json()["error"]), (502, {
            "message": gateway.СООБЩЕНИЕ_OLLAMA_НЕДОСТУПНА,
            "type": "server_error",
            "code": "upstream_error",
        }))
        self.assertEqual(len(вызовы), 1, "обычный запрос к Ollama не повторяется")
        self._без_маркеров(обычный)

        with self.клиент.stream("POST", "/v1/chat/completions", headers=self._h(self.ключ_чат), json={
                "stream": True,
                "messages": [{"role": "user", "content": "контролируемый сбой SSE"}]}) as поток:
            тело = "".join(поток.iter_text())
        self.assertEqual(поток.status_code, 200, "после начала SSE HTTP-статус изменить нельзя")
        события = [json.loads(строка[6:]) for строка in тело.splitlines() if строка.startswith("data: {")]
        ошибки = [событие["error"] for событие in события if "error" in событие]
        self.assertEqual(ошибки, [{
            "message": gateway.СООБЩЕНИЕ_OLLAMA_НЕДОСТУПНА,
            "type": "server_error",
            "code": "upstream_error",
        }])
        self.assertEqual(len(вызовы), 2, "потоковый запрос к Ollama не повторяется")
        клиентские_данные = тело + "\n" + "\n".join(
            f"{имя}: {значение}" for имя, значение in поток.headers.items())
        for маркер in МАРКЕРЫ:
            self.assertNotIn(маркер, клиентские_данные)

    def test_исключение_telegram_не_раскрывает_внутренние_данные(self):
        успешный = self.клиент.post("/admin/api/telegram", headers=self._h(self.ключ_админ),
                                    json={"действие": "старт"})
        self.assertEqual((успешный.status_code, успешный.json(), self.вызовы_телеграм),
                         (200, {"ok": True}, ["старт"]))

        вызовы = []

        def сломанный_telegram(действие):
            вызовы.append(действие)
            raise RuntimeError(f"{МАРКЕРЫ[0]} {МАРКЕРЫ[1]}")

        self.сервис.телеграм = сломанный_telegram
        отказ = self.клиент.post("/admin/api/telegram", headers=self._h(self.ключ_чат),
                                 json={"действие": "стоп"})
        self.assertEqual((отказ.status_code, отказ.json()["error"]["code"]), (403, "permission_denied"))
        self.assertEqual(вызовы, [], "отказ по праву не вызывает обработчик Telegram")

        r = self.клиент.post("/admin/api/telegram", headers=self._h(self.ключ_админ),
                             json={"действие": "перезапуск"})
        self.assertEqual((r.status_code, r.json()["error"]), (409, {
            "message": gateway.СООБЩЕНИЕ_TELEGRAM_НЕДОСТУПЕН,
            "type": "server_error",
            "code": "telegram_error",
        }))
        self.assertEqual(вызовы, ["перезапуск"], "после исключения обработчик Telegram не повторяется")
        self._без_маркеров(r)


if __name__ == "__main__":
    unittest.main()
