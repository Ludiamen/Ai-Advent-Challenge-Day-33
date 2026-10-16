"""Регрессии этапа 3: восстановление старта и завершённость SSE, без внешней сети."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import tempfile
import unittest

import httpx

from agent.service import telegram


class _Поток(httpx.AsyncByteStream):
    def __init__(self, *куски: bytes, ошибка: Exception | None = None) -> None:
        self.куски, self.ошибка = куски, ошибка

    async def __aiter__(self):
        for кусок in self.куски:
            yield кусок
        if self.ошибка:
            raise self.ошибка


def _фрагмент(текст: str, причина=None, usage=None) -> bytes:
    объект = {"choices": [{"index": 0, "delta": {"content": текст}, "finish_reason": причина}]}
    if usage is not None:
        объект["usage"] = usage
        объект["x_queue_wait"] = 1.5
    return ("data: " + json.dumps(объект, ensure_ascii=False) + "\n\n").encode()


_USAGE = {"prompt_tokens": 4, "completion_tokens": 2}
_КОНЕЦ = _фрагмент("", "stop", _USAGE)
_DONE = b"data: [DONE]\n\n"


class ПотокSSE(unittest.IsolatedAsyncioTestCase):
    async def клиент(self, *куски: bytes, ошибка: Exception | None = None):
        def ответ(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=_Поток(*куски, ошибка=ошибка))

        return telegram.КлиентСервиса("https://service.test", "test-key",
                                      transport=httpx.MockTransport(ответ))

    async def прогон(self, *куски: bytes, ошибка: Exception | None = None):
        события: list[tuple[str, object]] = []
        клиент = await self.клиент(*куски, ошибка=ошибка)
        try:
            итог = await клиент.чат([{"role": "user", "content": "вопрос"}],
                                    lambda вид, значение: self._событие(события, вид, значение))
        finally:
            await клиент.закрыть()
        return итог, события

    @staticmethod
    async def _событие(список, вид, значение):
        список.append((вид, значение))

    async def test_корректный_поток_требует_терминальный_фрагмент_и_done(self):
        начало = (b'data: {"choices":\n'
                  b'data: [{"index":0,"delta":{"content":"\\u041f\\u0440\\u0438"},'
                  b'"finish_reason":null}]}\n\n')
        # Декодирование не зависит от границ TCP; многострочный data и data без пробела допустимы.
        тело = b": queue 3\n\n" + начало + _фрагмент("вет") + _КОНЕЦ + b"data:[DONE]\n\n"
        итог, события = await self.прогон(*[тело[i:i + 7] for i in range(0, len(тело), 7)])
        self.assertEqual((итог["текст"], итог["причина"], итог["вход"], итог["ответ"], итог["ожидание"]),
                         ("Привет", "stop", 4, 2, 1.5))
        self.assertIsNone(итог["код_ошибки"])
        self.assertIn(("очередь", 3), события)

    async def test_eof_done_без_терминала_и_терминал_без_done_не_успех(self):
        случаи = {
            "eof после текста": (_фрагмент("ЧАСТЬ"),),
            "done без терминала": (_фрагмент("ЧАСТЬ"), _DONE),
            "терминал без done": (_фрагмент("ЧАСТЬ"), _КОНЕЦ),
        }
        for имя, куски in случаи.items():
            with self.subTest(имя=имя):
                итог, _ = await self.прогон(*куски)
                self.assertEqual(итог["код_ошибки"], "invalid_sse")
                self.assertEqual((итог["текст"], итог["причина"], итог["вход"], итог["ответ"]),
                                 ("", None, None, None))

    async def test_невалидные_json_поля_utf8_и_error_событие_отклонены(self):
        ошибка = b'data: {"error":{"code":"timeout","message":"late"}}\n\n'
        случаи = (
            b"data: {oops}\n\n",
            b"data: []\n\n",
            b'data: {"choices":"bad"}\n\n',
            b'data: {"choices":[{"delta":{"content":7},"finish_reason":null}]}\n\n',
            b"data: \xff\n\n",
            ошибка,
        )
        for хвост in случаи:
            with self.subTest(хвост=хвост):
                итог, _ = await self.прогон(_фрагмент("СЕКРЕТНАЯ ЧАСТЬ"), хвост)
                self.assertEqual(итог["текст"], "")
                self.assertIsNotNone(итог["код_ошибки"])
                self.assertIsNone(итог["причина"])

    async def test_сетевой_обрыв_и_timeout_после_части_не_успех(self):
        for исключение in (httpx.ReadError("connection lost"), httpx.ReadTimeout("timed out")):
            with self.subTest(тип=type(исключение).__name__):
                итог, _ = await self.прогон(_фрагмент("СЕКРЕТНАЯ ЧАСТЬ"), ошибка=исключение)
                self.assertEqual((итог["код_ошибки"], итог["текст"], итог["вход"], итог["ответ"]),
                                 ("сеть", "", None, None))

    async def test_ошибочный_поток_не_попадает_в_историю_и_финальную_правку(self):
        клиент = await self.клиент(_фрагмент("СЕКРЕТНАЯ ЧАСТЬ"))
        api = _API()
        каталог = tempfile.mkdtemp(prefix="telegram-sse-")
        self.addCleanup(shutil.rmtree, каталог, True)
        бот = telegram.Бот(api, клиент, владелец=7, каталог=каталог, правка_с=0)
        try:
            await бот._чат(7, "вопрос")
        finally:
            await бот.закрыть()
        self.assertNotIn(7, бот._история)
        self.assertNotIn("СЕКРЕТНАЯ ЧАСТЬ", api.правки[-1])
        self.assertIn("поток прервался", api.правки[-1])


class _API:
    база = "http://127.0.0.1:18080"

    def __init__(self, ответы=None) -> None:
        self.ответы = list(ответы or [])
        self.вызовов = 0
        self.правки: list[str] = []

    async def getMe(self):
        self.вызовов += 1
        ответ = self.ответы.pop(0)
        if isinstance(ответ, Exception):
            raise ответ
        return ответ

    async def getUpdates(self, offset):
        return []

    async def sendMessage(self, чат, текст, разметка=False):
        return {"message_id": 1}

    async def editMessageText(self, чат, сообщение, текст, разметка=False):
        self.правки.append(текст)
        return True

    async def sendChatAction(self, чат, действие="typing"):
        return True

    async def закрыть(self):
        return None


class _Сервис:
    модель = "test-model"
    адрес = "https://127.0.0.1:9443"

    async def закрыть(self):
        return None


class ВосстановлениеСтарта(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="telegram-start-")
        self.addCleanup(shutil.rmtree, self.каталог, True)

    def бот(self, api, паузы, пауза=None):
        async def быстрая(секунд):
            паузы.append(секунд)
        return telegram.Бот(api, _Сервис(), владелец=7, каталог=self.каталог,
                            пауза=пауза or быстрая)

    async def test_network_5xx_и_429_восстанавливаются_с_backoff(self):
        api = _API([httpx.ConnectError("offline"), telegram.ОшибкаTelegram(503, "unavailable"),
                    telegram.ОшибкаTelegram(429, "slow down", 9),
                    {"username": "RecoveredBot", "can_join_groups": False}])
        паузы: list[float] = []
        бот = self.бот(api, паузы)
        результат = await бот.познакомиться_с_повторами(asyncio.Event())
        self.assertEqual((результат["username"], бот.имя, api.вызовов),
                         ("RecoveredBot", "@RecoveredBot", 4))
        self.assertEqual(паузы, [2.0, 4.0, 9.0])

    async def test_exponential_backoff_ограничен_60(self):
        api = _API([*[httpx.ConnectError("offline") for _ in range(7)], {"username": "Bot"}])
        паузы: list[float] = []
        бот = self.бот(api, паузы)
        await бот.познакомиться_с_повторами(asyncio.Event())
        self.assertEqual(паузы, [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0])

    async def test_retry_after_telegram_может_быть_дольше_предела_backoff(self):
        api = _API([telegram.ОшибкаTelegram(429, "slow down", 120), {"username": "Bot"}])
        паузы: list[float] = []
        бот = self.бот(api, паузы)
        await бот.познакомиться_с_повторами(asyncio.Event())
        self.assertEqual(паузы, [120.0], "retry_after — минимальный срок Telegram, а не шаг backoff")

    async def test_временный_не_json_и_неверный_result_getme_повторяются(self):
        ответы = iter((httpx.Response(200, text="proxy is starting"),
                       httpx.Response(200, json={"ok": True, "result": None}),
                       httpx.Response(200, json={"ok": True, "result": {"username": "Bot"}})))

        def ответ(_: httpx.Request) -> httpx.Response:
            return next(ответы)

        api = telegram.BotAPI("123:TEST", база="http://127.0.0.1:18080",
                              transport=httpx.MockTransport(ответ))
        паузы: list[float] = []
        бот = self.бот(api, паузы)
        try:
            результат = await бот.познакомиться_с_повторами(asyncio.Event())
        finally:
            await бот.закрыть()
        self.assertEqual(результат["username"], "Bot")
        self.assertEqual(паузы, [2.0, 4.0])

    async def test_постоянные_4xx_не_повторяются(self):
        for код in (400, 401, 403, 404, 409):
            with self.subTest(код=код):
                api = _API([telegram.ОшибкаTelegram(код, "permanent")])
                паузы: list[float] = []
                бот = self.бот(api, паузы)
                with self.assertRaises(telegram.ОшибкаTelegram):
                    await бот.познакомиться_с_повторами(asyncio.Event())
                self.assertEqual((api.вызовов, паузы), (1, []))

    async def test_остановка_прерывает_backoff(self):
        началась = asyncio.Event()

        async def долгая_пауза(_):
            началась.set()
            await asyncio.Event().wait()

        api = _API([httpx.ConnectError("offline")])
        бот = self.бот(api, [], долгая_пауза)
        стоп = asyncio.Event()
        задача = asyncio.create_task(бот.познакомиться_с_повторами(стоп))
        await asyncio.wait_for(началась.wait(), 1)
        стоп.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(задача, 1)

    async def test_polling_429_соблюдает_retry_after_и_останавливается(self):
        стоп = asyncio.Event()
        паузы: list[float] = []

        class API(_API):
            async def getUpdates(self, offset, timeout=telegram.ОПРОС_С):
                raise telegram.ОшибкаTelegram(429, "slow down", 11)

        async def пауза(секунд):
            паузы.append(секунд)
            стоп.set()

        бот = self.бот(API(), паузы, пауза)
        await бот.опрашивать(стоп)
        self.assertEqual(паузы, [11.0])

    async def test_закрытие_дожидается_отмены_работника_до_http_клиентов(self):
        события: list[str] = []
        начат = asyncio.Event()

        class API(_API):
            async def sendChatAction(self, чат, действие="typing"):
                if "api-close" in события:
                    события.append("api-call-after-close")
                return True

            async def закрыть(self):
                события.append("api-close")

        class Сервис(_Сервис):
            async def чат(self, сообщения, при_событии):
                начат.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    события.append("worker-finally")

            async def закрыть(self):
                события.append("service-close")

        бот = telegram.Бот(API(), Сервис(), владелец=7, каталог=self.каталог, правка_с=0)
        await бот._в_очередь(7, "вопрос")
        await asyncio.wait_for(начат.wait(), 1)
        задачи_хода = [задача for задача in asyncio.all_tasks()
                       if getattr(задача.get_coro(), "__qualname__", "").endswith("_Вывод._ждать")]
        self.assertTrue(задачи_хода)
        await бот.закрыть()
        await asyncio.sleep(0)
        self.assertEqual(события, ["worker-finally", "api-close", "service-close"])
        self.assertTrue(all(задача.done() for задача in задачи_хода))


if __name__ == "__main__":
    unittest.main()
