"""Регрессии checkpoint Telegram: последовательная обработка без записи текстов."""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

import httpx

from agent.service import telegram


ВЛАДЕЛЕЦ = 7001


def обновление(номер: int, текст: str = "ВОПРОС-МАРКЕР", отправитель: int = ВЛАДЕЛЕЦ) -> dict:
    return {"update_id": номер, "message": {"from": {"id": отправитель},
            "chat": {"id": отправитель, "type": "private"}, "text": текст}}


class API:
    база = "http://127.0.0.1:18080"

    def __init__(self, пакеты=()) -> None:
        self.пакеты = list(пакеты)
        self.offsets: list[int | None] = []
        self.отправлено: list[str] = []
        self.правки: list[str] = []
        self.ошибка_правки: Exception | None = None
        self.ошибка_get: Exception | None = None
        self.ошибка_отправки: Exception | None = None

    async def getUpdates(self, offset, timeout=telegram.ОПРОС_С):
        self.offsets.append(offset)
        if self.ошибка_get:
            raise self.ошибка_get
        return self.пакеты.pop(0) if self.пакеты else []

    async def sendMessage(self, чат, текст, разметка=False):
        if self.ошибка_отправки:
            raise self.ошибка_отправки
        self.отправлено.append(текст)
        return {"message_id": len(self.отправлено)}

    async def editMessageText(self, чат, сообщение, текст, разметка=False):
        if self.ошибка_правки:
            raise self.ошибка_правки
        self.правки.append(текст)
        return True

    async def sendChatAction(self, чат, действие="typing"):
        return True

    async def leaveChat(self, чат):
        if self.ошибка_отправки:
            raise self.ошибка_отправки
        return True

    async def закрыть(self):
        return None


class Сервис:
    модель = "test-model"
    адрес = "https://service.test"

    def __init__(self, ворота: asyncio.Event | None = None) -> None:
        self.ворота = ворота
        self.начат = asyncio.Event()
        self.вопросы: list[str] = []

    async def чат(self, сообщения, при_событии):
        вопрос = сообщения[-1]["content"]
        self.вопросы.append(вопрос)
        self.начат.set()
        if self.ворота is not None:
            await self.ворота.wait()
        return {"код": 200, "текст": "ОТВЕТ-МАРКЕР", "причина": "stop", "вход": 2, "ответ": 1,
                "ожидание": 0.0, "код_ошибки": None, "ошибка": "", "повтор_через": None}

    async def закрыть(self):
        return None


class НадёжныйOffset(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="telegram-offset-")
        self.addCleanup(shutil.rmtree, self.каталог, True)

    def бот(self, api: API, сервис: Сервис | None = None, пауза=None) -> telegram.Бот:
        async def без_паузы(_):
            return None
        return telegram.Бот(api, сервис or Сервис(), ВЛАДЕЛЕЦ, self.каталог,
                            пауза=пауза or без_паузы, правка_с=0)

    def offset(self) -> str | None:
        путь = pathlib.Path(self.каталог, telegram.OFFSET)
        return путь.read_text(encoding="ascii").strip() if путь.exists() else None

    async def test_падение_до_и_во_время_обработки_не_подтверждает_update(self):
        api = API([[обновление(10)]])
        бот = self.бот(api)
        with mock.patch.object(бот, "_обработать_до_конца", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                await бот.опросить_раз()
        self.assertIsNone(self.offset())

        ворота = asyncio.Event()
        api2, сервис = API([[обновление(10)]]), Сервис(ворота)
        бот2 = self.бот(api2, сервис)
        задача = asyncio.create_task(бот2.опросить_раз())
        await asyncio.wait_for(сервис.начат.wait(), 1)
        задача.cancel()  # эквивалент обработчика SIGTERM/SIGINT в telegram_bot.py
        with self.assertRaises(asyncio.CancelledError):
            await задача
        self.assertIsNone(self.offset())
        await бот.закрыть()
        await бот2.закрыть()

    async def test_ошибка_финальной_доставки_не_двигает_offset(self):
        for ошибка in (httpx.WriteError("offline"), telegram.ОшибкаTelegram(429, "slow", 1),
                       telegram.ОшибкаTelegram(503, "down")):
            with self.subTest(ошибка=type(ошибка).__name__ + str(getattr(ошибка, "код", ""))):
                api = API([[обновление(20)]])
                api.ошибка_правки = ошибка
                бот = self.бот(api)
                with self.assertRaises(type(ошибка)):
                    await бот.опросить_раз()
                self.assertIsNone(self.offset())
                await бот.закрыть()

    async def test_успешная_доставка_но_ошибка_checkpoint_откатывает_историю(self):
        api = API([[обновление(30)]])
        бот = self.бот(api)
        with mock.patch.object(бот, "_сохранить_offset", side_effect=telegram.ОшибкаCheckpoint("disk")):
            with self.assertRaises(telegram.ОшибкаCheckpoint):
                await бот.опросить_раз()
        self.assertTrue(api.правки, "окончательный ответ уже мог быть принят Telegram")
        self.assertNotIn(ВЛАДЕЛЕЦ, бот._история)
        self.assertIsNone(self.offset())
        await бот.закрыть()

    async def test_рестарт_повторяет_неподтверждённый_update(self):
        первый_api = API([[обновление(40)]])
        первый_api.ошибка_правки = httpx.WriteError("lost")
        первый = self.бот(первый_api)
        with self.assertRaises(httpx.WriteError):
            await первый.опросить_раз()
        await первый.закрыть()

        второй_api, второй_сервис = API([[обновление(40)]]), Сервис()
        второй = self.бот(второй_api, второй_сервис)
        await второй.опросить_раз()
        self.assertEqual((второй_api.offsets, второй_сервис.вопросы, self.offset()),
                         ([None], ["ВОПРОС-МАРКЕР"], "41"))
        await второй.закрыть()

    async def test_пакет_сортируется_и_останавливается_на_первом_сбое(self):
        api, сервис = API([[обновление(3, "третий"), обновление(1, "первый"),
                            обновление(2, "второй")]]), Сервис()
        бот = self.бот(api, сервис)
        исходная = бот._обработать_до_конца

        async def сбой_на_втором(update):
            if update["update_id"] == 2:
                raise httpx.WriteError("delivery")
            await исходная(update)

        with mock.patch.object(бот, "_обработать_до_конца", side_effect=сбой_на_втором):
            with self.assertRaises(httpx.WriteError):
                await бот.опросить_раз()
        self.assertEqual((сервис.вопросы, self.offset()), (["первый"], "2"))
        await бот.закрыть()

    async def test_дубликат_обрабатывается_один_раз_а_битый_id_блокирует_весь_пакет(self):
        api, сервис = API([[обновление(5), обновление(5)]]), Сервис()
        бот = self.бот(api, сервис)
        await бот.опросить_раз()
        self.assertEqual((len(сервис.вопросы), self.offset()), (1, "6"))
        await бот.закрыть()

        api2, сервис2 = API([[обновление(7), {"update_id": "8"}]]), Сервис()
        бот2 = self.бот(api2, сервис2)
        with self.assertRaises(telegram.ОшибкаTelegram):
            await бот2.опросить_раз()
        self.assertEqual(сервис2.вопросы, [])
        self.assertEqual(self.offset(), "6")
        await бот2.закрыть()

    async def test_неподдерживаемые_updates_явно_пропускаются_и_checkpoint_продвигается(self):
        api = API([[{"update_id": 50, "callback_query": {}},
                    {"update_id": 51, "message": []}]])
        бот = self.бот(api)
        await бот.опросить_раз()
        self.assertEqual(self.offset(), "52")
        self.assertEqual((api.отправлено, api.правки), ([], []))
        await бот.закрыть()

    async def test_чужие_и_группы_терминально_игнорируют_только_400_403(self):
        for вид in ("чужой", "группа"):
            for код in (400, 403, 401, 404, 409, 429, 503):
                with self.subTest(вид=вид, код=код):
                    каталог = pathlib.Path(self.каталог, f"{вид}-{код}")
                    каталог.mkdir()
                    update = обновление(80, отправитель=9001)
                    if вид == "группа":
                        update["message"]["chat"] = {"id": -9001, "type": "group"}
                    api, сервис = API([[update]]), Сервис()
                    api.ошибка_отправки = telegram.ОшибкаTelegram(код, "injected")
                    бот = telegram.Бот(api, сервис, ВЛАДЕЛЕЦ, str(каталог), правка_с=0)
                    if код in (400, 403):
                        await бот.опросить_раз()
                        self.assertEqual((каталог / telegram.OFFSET).read_text().strip(), "81")
                    else:
                        with self.assertRaises(telegram.ОшибкаTelegram):
                            await бот.опросить_раз()
                        self.assertFalse((каталог / telegram.OFFSET).exists())
                    self.assertEqual(сервис.вопросы, [], "чужой/group update не обращается к модели")
                    await бот.закрыть()

    async def test_ошибка_401_403_финального_ответа_владельцу_не_подтверждается(self):
        for код in (401, 403):
            with self.subTest(код=код):
                каталог = pathlib.Path(self.каталог, f"owner-{код}")
                каталог.mkdir()
                api = API([[обновление(90)]])
                api.ошибка_правки = telegram.ОшибкаTelegram(код, "injected")
                бот = telegram.Бот(api, Сервис(), ВЛАДЕЛЕЦ, str(каталог), правка_с=0)
                with self.assertRaises(telegram.ОшибкаTelegram):
                    await бот.опросить_раз()
                self.assertFalse((каталог / telegram.OFFSET).exists())
                await бот.закрыть()

    async def test_getupdates_429_и_5xx_используют_backoff_без_checkpoint(self):
        for ошибка, задержка in ((telegram.ОшибкаTelegram(429, "slow", 11), 11.0),
                                 (telegram.ОшибкаTelegram(503, "down"), 2.0)):
            with self.subTest(код=ошибка.код):
                стоп, паузы = asyncio.Event(), []
                api = API()
                api.ошибка_get = ошибка

                async def пауза(секунд):
                    паузы.append(секунд)
                    стоп.set()

                бот = self.бот(api, пауза=пауза)
                await бот.опрашивать(стоп)
                self.assertEqual(паузы, [задержка])
                self.assertIsNone(self.offset())
                await бот.закрыть()

    async def test_checkpoint_содержит_только_числовые_метаданные(self):
        api = API([[обновление(60, "СЕКРЕТНЫЙ-ТЕКСТ-ПОЛЬЗОВАТЕЛЯ")]])
        бот = self.бот(api)
        await бот.опросить_раз()
        await бот.закрыть()
        файлы = list(pathlib.Path(self.каталог).glob("telegram-offset*"))
        self.assertEqual([ф.name for ф in файлы], [telegram.OFFSET])
        дамп = "".join(ф.read_text(encoding="ascii") for ф in файлы)
        self.assertEqual(дамп, "61\n")
        self.assertNotIn("СЕКРЕТНЫЙ", дамп)

    async def test_pending_guard_при_ошибке_fsync_оставляет_старый_offset(self):
        бот = self.бот(API())
        бот._сохранить_offset(70)
        настоящая = telegram._синхронизировать_каталог
        вызовов = 0

        def сбой_после_replace(каталог):
            nonlocal вызовов
            вызовов += 1
            if вызовов == 2:  # main уже replace, но его запись в каталоге ещё не подтверждена
                raise OSError("fsync failed")
            настоящая(каталог)

        with mock.patch.object(telegram, "_синхронизировать_каталог", side_effect=сбой_после_replace):
            with self.assertRaises(telegram.ОшибкаCheckpoint):
                бот._сохранить_offset(71)
        self.assertEqual(бот._offset(), 70)
        self.assertEqual(pathlib.Path(self.каталог, telegram.OFFSET_ПРЕДЫДУЩИЙ).read_text().strip(), "v1 70 71")
        бот._сохранить_offset(71)
        self.assertEqual(bot_offset := бот._offset(), 71)
        self.assertFalse(pathlib.Path(self.каталог, telegram.OFFSET_ПРЕДЫДУЩИЙ).exists())
        self.assertEqual(bot_offset, 71)
        await бот.закрыть()

    async def test_атомарные_сбои_до_replace_не_продвигают_checkpoint(self):
        """Guard либо отсутствует, либо долговечно описывает только переход 70→71."""
        for место in ("guard-file-fsync", "main-file-fsync", "main-replace"):
            with self.subTest(место=место):
                каталог = pathlib.Path(self.каталог, место)
                каталог.mkdir()
                бот = telegram.Бот(API(), Сервис(), ВЛАДЕЛЕЦ, str(каталог), правка_с=0)
                бот._сохранить_offset(70)
                основной = каталог / telegram.OFFSET
                pending = каталог / telegram.OFFSET_ПРЕДЫДУЩИЙ
                настоящий_fsync = telegram.os.fsync
                настоящий_replace = telegram.os.replace
                fsync_вызовов = 0

                def fsync_с_отказом(fd):
                    nonlocal fsync_вызовов
                    fsync_вызовов += 1
                    целевой = 1 if место == "guard-file-fsync" else 3
                    if место != "main-replace" and fsync_вызовов == целевой:
                        raise OSError("injected fsync failure")
                    return настоящий_fsync(fd)

                def replace_с_отказом(src, dst):
                    if место == "main-replace" and os.fspath(dst) == os.fspath(основной):
                        raise OSError("injected replace failure")
                    return настоящий_replace(src, dst)

                with mock.patch.object(telegram.os, "fsync", side_effect=fsync_с_отказом), \
                        mock.patch.object(telegram.os, "replace", side_effect=replace_с_отказом):
                    with self.assertRaises(telegram.ОшибкаCheckpoint):
                        бот._сохранить_offset(71)

                self.assertEqual(основной.read_text(encoding="ascii"), "70\n")
                self.assertEqual(бот._offset(), 70)
                if место == "guard-file-fsync":
                    self.assertFalse(pending.exists(), "недолговечный guard не должен остаться")
                else:
                    self.assertEqual(pending.read_text(encoding="ascii"), "v1 70 71\n")
                    self.assertNotIn("ВОПРОС", pending.read_text(encoding="ascii"))
                self.assertEqual(list(каталог.glob(".telegram-offset.*")), [], "временные файлы убраны")

                бот._сохранить_offset(71)
                self.assertEqual(бот._offset(), 71)
                self.assertFalse(pending.exists())
                await бот.закрыть()

    async def test_stale_corrupt_или_несогласованный_guard_fail_closed(self):
        основной = pathlib.Path(self.каталог, telegram.OFFSET)
        pending = pathlib.Path(self.каталог, telegram.OFFSET_ПРЕДЫДУЩИЙ)
        основной.write_text("100\n", encoding="ascii")
        for значение in ("v1 50 51\n", "v1 50 40\n", "мусор\n"):
            with self.subTest(значение=значение.strip()):
                pending.write_text(значение, encoding="utf-8")
                бот = self.бот(API())
                with self.assertRaises(telegram.ОшибкаCheckpoint):
                    бот._offset()
                await бот.закрыть()


if __name__ == "__main__":
    unittest.main()


class TelegramХранит(API):
    """Как настоящий Telegram: неподтверждённые updates хранятся и отдаются, пока offset их не превысит."""

    def __init__(self, обновления) -> None:
        super().__init__()
        self.все = list(обновления)
        self.ошибка_бокового: Exception | None = None

    async def getUpdates(self, offset, timeout=telegram.ОПРОС_С):
        self.offsets.append(offset)
        if timeout == 0 and self.ошибка_бокового:
            raise self.ошибка_бокового
        return [о for о in self.все if offset is None or о["update_id"] >= offset]


class МгновенныеКоманды(unittest.IsolatedAsyncioTestCase):
    """Ручная проверка владельца: /status ждал 7 минут за длинным /doc. Решение владельца
    (2026-10-12): мгновенные команды — сразу, а durable offset Codex сохраняется."""

    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="telegram-instant-")
        self.addCleanup(shutil.rmtree, self.каталог, True)

    def бот(self, api, сервис) -> telegram.Бот:
        async def без_паузы(_):
            return None
        бот = telegram.Бот(api, сервис, ВЛАДЕЛЕЦ, self.каталог, пауза=без_паузы, правка_с=0)
        бот.опрос_во_время_с = 0.01
        бот.сервис.здоровье = self._здоровье
        return бот

    async def _здоровье(self):
        return {"status": "ok", "model": "test-model", "queue": {"идёт": 1, "ждут": 0, "мест_в_очереди": 4,
                                                                 "средняя_с": 20.0}}

    def offset(self) -> str | None:
        путь = pathlib.Path(self.каталог, telegram.OFFSET)
        return путь.read_text(encoding="ascii").strip() if путь.exists() else None

    async def ждать(self, условие, секунд: float = 2.0):
        конец = asyncio.get_running_loop().time() + секунд
        while not условие():
            if asyncio.get_running_loop().time() > конец:
                self.fail("не дождался условия")
            await asyncio.sleep(0.01)

    @staticmethod
    def статусы(api) -> list[str]:
        return [т for т in api.отправлено if т.startswith("Сервис:")]

    async def test_status_во_время_долгого_ответа_и_offset_по_префиксу(self):
        ворота = asyncio.Event()
        api, сервис = TelegramХранит([обновление(10, "долгий вопрос"), обновление(11, "/status")]), Сервис(ворота)
        бот = self.бот(api, сервис)
        задача = asyncio.create_task(бот.опросить_раз())
        await asyncio.wait_for(сервис.начат.wait(), 1)
        await self.ждать(lambda: self.статусы(api))
        self.assertFalse(задача.done(), "ответ на /status пришёл, пока модель ещё отвечает")
        self.assertIn("бот отвечает", self.статусы(api)[0])
        self.assertNotIn("ждут ещё", self.статусы(api)[0], "/status не считает занятым сам себя (видео, сцена 4)")
        self.assertIsNone(self.offset(), "незавершённый update 10 не подтверждён")
        ворота.set()
        await задача
        self.assertEqual(self.offset(), "12", "после 10 сплошной префикс включает уже отвеченный 11")
        self.assertEqual(len(self.статусы(api)), 1, "/status не отвечен дважды")
        self.assertEqual(сервис.вопросы, ["долгий вопрос"])

    async def test_падение_не_теряет_вопрос_а_status_может_повториться(self):
        ворота = asyncio.Event()
        api, сервис = TelegramХранит([обновление(10, "долгий вопрос"), обновление(11, "/status")]), Сервис(ворота)
        бот = self.бот(api, сервис)
        задача = asyncio.create_task(бот.опросить_раз())
        await self.ждать(lambda: self.статусы(api))
        задача.cancel()                       # SIGTERM посреди генерации
        with self.assertRaises(asyncio.CancelledError):
            await задача
        self.assertIsNone(self.offset())
        сервис2 = Сервис()
        бот2 = self.бот(api, сервис2)
        await бот2.опросить_раз()
        self.assertEqual(сервис2.вопросы, ["долгий вопрос"], "вопрос 10 не потерян")
        self.assertEqual(self.offset(), "12")
        self.assertEqual(len(self.статусы(api)), 2, "после аварии /status допустимо повторить")

    async def test_сбой_бокового_опроса_не_ломает_ответ(self):
        ворота = asyncio.Event()
        api, сервис = TelegramХранит([обновление(10, "долгий вопрос")]), Сервис(ворота)
        api.ошибка_бокового = httpx.ConnectError("сеть")
        бот = self.бот(api, сервис)
        задача = asyncio.create_task(бот.опросить_раз())
        await asyncio.wait_for(сервис.начат.wait(), 1)
        await self.ждать(lambda: api.offsets.count(10) >= 2)
        ворота.set()
        await задача
        self.assertEqual(self.offset(), "11")

    async def test_вне_очереди_только_команды_владельца(self):
        ворота = asyncio.Event()
        api = TelegramХранит([обновление(10, "долгий вопрос"), обновление(11, "второй вопрос"),
                              обновление(12, "/status", отправитель=7002)])
        сервис = Сервис(ворота)
        бот = self.бот(api, сервис)
        задача = asyncio.create_task(бот.опросить_раз())
        await asyncio.wait_for(сервис.начат.wait(), 1)
        await self.ждать(lambda: api.offsets.count(10) >= 3)
        self.assertEqual(сервис.вопросы, ["долгий вопрос"], "второй вопрос ждёт своей очереди")
        self.assertNotIn("Это частный бот.", api.отправлено, "чужой — в порядке очереди, не вне её")
        ворота.set()
        await задача
        self.assertEqual(сервис.вопросы, ["долгий вопрос", "второй вопрос"])
        self.assertIn("Это частный бот.", api.отправлено)
        self.assertEqual(self.offset(), "13")
