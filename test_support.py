"""Тесты Дня 33 — ассистент поддержки пользователей (specs/033-support-assistant, plan § 5).

Модель в тестах — заглушка; живые прогоны ds-flash — check_support.py.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from unittest import mock

КОРЕНЬ = os.path.dirname(os.path.abspath(__file__))


class _Данные(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.путь = os.path.join(self.tmp.name, "desk.json")
        self.окр = mock.patch.dict(os.environ, {"SUPPORT_DATA": self.путь})
        self.окр.start()
        from agent.support import data
        self.data = data
        self.х = data.Хранилище()
        self.х.сбросить()

    def tearDown(self) -> None:
        self.окр.stop()
        self.tmp.cleanup()


class ДанныеСервисДеска(_Данные):
    """spec FR-01, plan D-01: seed, рабочая копия, переходы, заявки."""

    def test_seed_покрывает_сценарии(self) -> None:
        с = self.х.снимок()
        self.assertGreaterEqual(len(с["пользователи"]), 8)
        self.assertGreaterEqual(len(с["тикеты"]), 12)
        состояния = {п["учётная_запись"]["статус"] for п in с["пользователи"] if п["учётная_запись"]}
        self.assertTrue(any(п["учётная_запись"] is None for п in с["пользователи"]))   # заявители без учётной записи
        self.assertTrue({"активна", "заблокирована", "приглашён"} <= состояния - {None})
        записи = [п["учётная_запись"] for п in с["пользователи"] if п["учётная_запись"]]
        self.assertTrue(any(у["второй_фактор"] == "не подключён" for у in записи))
        self.assertTrue(any(у["срок_пароля_истёк"] for у in записи))
        self.assertTrue(any(т["вид"] == "closed" for т in с["тикеты"]))

    def test_синтетика_без_пд(self) -> None:
        with open(os.path.join(КОРЕНЬ, "support-seed.json"), encoding="utf-8") as f:
            текст = f.read()
        почты = set(re.findall(r"[\w.+-]+@[\w.-]+", текст))
        self.assertTrue(почты and all(п.endswith("@example.invalid") for п in почты), почты)
        адреса = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", текст))
        self.assertTrue(all(а.startswith(("203.0.113.", "198.51.100.", "192.0.2.")) for а in адреса), адреса)
        from agent.service import pd
        _, виды = pd.скрыть(текст.replace("@example.invalid", ""), профиль="точный")
        self.assertEqual(виды, {})

    def test_рабочая_копия_и_сброс(self) -> None:
        self.х.добавить_сообщение(1001, "ассистент", "проверка")
        self.assertEqual(self.х.тикет(1001)["сообщения"][-1]["текст"], "проверка")
        self.х.сбросить()
        self.assertNotEqual(self.х.тикет(1001)["сообщения"][-1]["текст"], "проверка")

    def test_переходы_статусов(self) -> None:
        self.х.сменить_статус(1001, "Передан специалисту", "ассистент")
        with self.assertRaises(self.data.ОшибкаДанных):
            self.х.сменить_статус(1001, "Решена", "ассистент")          # закрывает только сотрудник
        with self.assertRaises(self.data.ОшибкаДанных):
            self.х.закрыть(1001, "  ")                                  # без решения не закрыть
        т = self.х.закрыть(1001, "Блокировка снята")
        self.assertEqual((т["статус"], т["вид"]), ("Решена", "closed"))
        with self.assertRaises(self.data.ОшибкаДанных):
            self.х.сменить_статус(1001, "Ждёт ответа заявителя", "ассистент")
        self.х.добавить_сообщение(1001, "заявитель", "не помогло")
        self.assertEqual(self.х.тикет(1001)["статус"], "В работе")       # открыта заново

    def test_заявка_ждёт_подтверждения_и_выполняется(self) -> None:
        з = self.х.завести_заявку(1001, "разблокировать", "5 неудачных попыток")
        self.assertEqual(з["статус"], "ждёт подтверждения")
        self.assertEqual(self.х.пользователь("u01")["учётная_запись"]["статус"], "заблокирована")  # ещё не выполнено
        self.assertEqual(self.х.тикет(1001)["статус"], "Ждёт подтверждения действия")
        self.assertEqual(self.х.завести_заявку(1001, "разблокировать", "повтор")["id"], з["id"])   # не плодится
        self.х.выполнить_заявку(з["id"], "сотрудник-1")
        уз = self.х.пользователь("u01")["учётная_запись"]
        self.assertEqual((уз["статус"], уз["блокировка"]), ("активна", None))
        self.assertIn("Блокировка входа снята", self.х.тикет(1001)["сообщения"][-1]["текст"])
        with self.assertRaises(self.data.ОшибкаДанных):
            self.х.выполнить_заявку(з["id"], "сотрудник-1")
        with self.assertRaises(self.data.ОшибкаДанных):
            self.х.завести_заявку(1001, "удалить_всех", "x")

    def test_отклонение_и_сброс_второго_фактора(self) -> None:
        з = self.х.завести_заявку(1005, "сбросить_второй_фактор", "нет телефона и кодов")
        self.х.отклонить_заявку(з["id"], "сотрудник-1", "сначала подтвердить личность")
        self.assertEqual(self.х.пользователь("u05")["учётная_запись"]["второй_фактор"], "подключён")
        з2 = self.х.завести_заявку(1005, "сбросить_второй_фактор", "личность подтверждена")
        self.х.выполнить_заявку(з2["id"], "сотрудник-1")
        self.assertEqual(self.х.пользователь("u05")["учётная_запись"]["второй_фактор"], "сброшен")

    def test_создать_тикет(self) -> None:
        т = self.х.создать_тикет("u03", "", "Не пускает в портал")
        self.assertEqual((т["номер"], т["статус"], т["заявитель"]), (1013, "Новая", "u03"))
        with self.assertRaises(self.data.ОшибкаДанных):
            self.х.создать_тикет("u99", "x", "y")


class СерверПоддержки(_Данные):
    """spec FR-02, SC-02, plan D-02: ассистент — в границах тикета; пульт — отдельно."""

    def вызвать(self, сервер, имя: str, аргументы: dict | None = None, тикет: str = "1001"):
        import anyio
        from mcp import Client

        async def вызов():
            async with Client(сервер) as клиент:
                return await клиент.call_tool(имя, аргументы or {})
        with mock.patch.dict(os.environ, {"SUPPORT_TICKET": тикет}):
            return anyio.run(вызов)

    def инструменты(self, сервер) -> set[str]:
        import anyio
        from mcp import Client

        async def список():
            async with Client(сервер) as клиент:
                return (await клиент.list_tools()).tools
        return {и.name for и in anyio.run(список)}

    def данные(self, р) -> dict:
        return json.loads("\n".join(getattr(б, "text", "") for б in р.content))

    def test_наборы_разделены(self) -> None:
        import support_server as с
        ассистент, пульт = self.инструменты(с.ассистент), self.инструменты(с.пульт)
        self.assertTrue(all(и.startswith("support_") for и in ассистент))
        self.assertTrue(all(и.startswith("desk_") for и in пульт))
        self.assertNotIn("desk_apply", ассистент)                        # выполнить заявку модель не может
        self.assertFalse(any("user" in и for и in ассистент))

    def test_границы_тикета(self) -> None:
        import support_server as с
        р = self.вызвать(с.ассистент, "support_requester", {"ticket": 1001})
        self.assertFalse(р.is_error)
        self.assertEqual(self.данные(р)["id"], "u01")
        self.assertNotIn("почта", self.данные(р))                         # адрес ассистенту не нужен
        чужой = self.вызвать(с.ассистент, "support_requester", {"ticket": 1002})
        self.assertTrue(чужой.is_error)
        self.assertIn("доступен только тикет №1001", чужой.content[0].text)
        без = self.вызвать(с.ассистент, "support_ticket", {"ticket": 1001}, тикет="")
        self.assertTrue(без.is_error)

    def test_журнал_и_история(self) -> None:
        import support_server as с
        входы = self.данные(self.вызвать(с.ассистент, "support_logins", {"ticket": 1001, "limit": 3}))["входы"]
        self.assertEqual(len(входы), 3)
        self.assertEqual(входы[0]["итог"], "блокировка")
        история = self.данные(self.вызвать(с.ассистент, "support_history", {"ticket": 1001}))["тикеты"]
        self.assertEqual([т["номер"] for т in история], [990])

    def test_заявка_через_mcp_не_выполняется(self) -> None:
        import support_server as с
        р = self.вызвать(с.ассистент, "support_request_action", {"ticket": 1001, "action": "разблокировать",
                                                                  "reason": "5 неудачных попыток подряд"})
        self.assertIn("ещё не выполнено", self.данные(р)["пояснение"])
        self.assertEqual(self.х.пользователь("u01")["учётная_запись"]["статус"], "заблокирована")
        ид = self.данные(р)["id"]
        self.данные(self.вызвать(с.пульт, "desk_apply", {"request": ид}))
        self.assertEqual(self.х.пользователь("u01")["учётная_запись"]["статус"], "активна")

    def test_статус_ии(self) -> None:
        import support_server as с
        self.assertTrue(self.вызвать(с.ассистент, "support_set_status", {"ticket": 1001, "status": "Решена"}).is_error)
        ок = self.вызвать(с.ассистент, "support_set_status", {"ticket": 1001, "status": "Передан специалисту"})
        self.assertEqual(self.данные(ок)["статус"], "Передан специалисту")


if __name__ == "__main__":
    unittest.main()
