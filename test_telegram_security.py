"""Регрессии этапа 2: маска ПД и адрес Telegram Bot API, без внешней сети и секретов."""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

import httpx

import telegram_bot
from agent.service import manage
from agent.service import pd
from agent.service import telegram


class МаскаПДРегрессии(unittest.TestCase):
    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp(prefix="пд-безопасность-")
        self.addCleanup(shutil.rmtree, self.каталог, True)
        путь = pathlib.Path(self.каталог, "имена.txt")
        путь.write_text("Вымыслов\nПетров\nПридумк\n", encoding="utf-8")
        прежний = os.environ.get("PD_NAMES")
        os.environ["PD_NAMES"] = str(путь)
        self.addCleanup(lambda: os.environ.pop("PD_NAMES", None) if прежний is None
                        else os.environ.__setitem__("PD_NAMES", прежний))

    def test_фио_регистры_дефисы_и_латинские_двойники(self) -> None:
        случаи = (
            ("Подписал ВЫМЫСЛОВ ИВАН СЕРГЕЕВИЧ.", "Подписал [скрыто]."),
            ("Ответственная: ВыМыСлОва АнНа СеРгЕеВнА.", "Ответственная: [скрыто]."),
            ("Подписал И.И. ВЫМЫСЛОВ-ПЕТРОВ.", "Подписал [скрыто]."),
            ("Подписал ПЕТРОВ‐ВЫМЫСЛОВ А. В.", "Подписал [скрыто]"),
            ("Согласовала Анна Сергеевна Вымыслова‑Петрова.", "Согласовала [скрыто]."),
            ("Ответственный M.B Bымыслов-Петров.", "Ответственный [скрыто]."),
            ("контактное лицо Сергей ПЕТРОВИЧ", "контактное лицо [скрыто]"),
            ("контактное лицо Сергей петрович", "контактное лицо [скрыто]"),
            ("контактное лицо Сергей ПеТрОвИч", "контактное лицо [скрыто]"),
            ("НЕИЗВЕСТНЫЙ ИВАН СЕРГЕЕВИЧ", "[скрыто]"),
            ("ИВАН СЕРГЕЕВИЧ НЕИЗВЕСТНЫЙ", "[скрыто]"),
            ("Анна Сергеевна Неизвестная", "[скрыто]"),
        )
        for исходный, ожидаемый in случаи:
            with self.subTest(исходный=исходный):
                скрытый, виды = pd.скрыть(исходный)
                self.assertEqual(скрытый, ожидаемый)
                self.assertTrue(виды)
                self.assertEqual(pd.найти(скрытый), [])

    def test_известная_основа_скрывает_всю_дефисную_фамилию(self) -> None:
        случаи = {
            "Вымыслов-Неизвестный": "[скрыто]",
            "Неизвестный-ВЫМЫСЛОВ": "[скрыто]",
            "Неизвестный‑Вымыслова‑Другая": "[скрыто]",
        }
        for исходный, ожидаемый in случаи.items():
            with self.subTest(исходный=исходный):
                self.assertEqual(pd.скрыть(исходный)[0], ожидаемый)

    def test_неоднозначное_titlecase_фио_отклоняется_без_догадки(self) -> None:
        for исходный in ("Неизвестный Иван Сергеевич", "Подписал Сергей Петрович",
                         "Неизвестный Иван Сергеевич Подписал"):
            with self.subTest(исходный=исходный), self.assertRaises(pd.ОшибкаМаскирования):
                pd.скрыть(исходный)
            документ = {"answer": исходный, "sources": [], "quotes": [], "usage": {}}
            текст, виды = telegram.собрать_документы(документ, 0, 0, "test")
            self.assertEqual((текст, виды), (telegram.ОТКАЗ_ПД, {}))
            self.assertNotIn(исходный, текст)

    def test_обычные_прилагательные_не_считаются_отчествами(self) -> None:
        for текст in ("Схема аналогична описанной.", "Логика проста и логична.", "Информация публична."):
            with self.subTest(текст=текст):
                self.assertEqual(pd.найти(текст), [])
                self.assertEqual(pd.скрыть(текст), (текст, {}))

    def test_ошибка_или_остаток_пд_закрывает_весь_ответ(self) -> None:
        документ = {"answer": "СЕКРЕТ-МАРКЕР", "sources": [], "quotes": [], "usage": {}}
        with mock.patch.object(telegram.pd, "скрыть", side_effect=RuntimeError("сбой фильтра")):
            текст, виды = telegram.собрать_документы(документ, 0, 0, "test")
        self.assertEqual((текст, виды), (telegram.ОТКАЗ_ПД, {}))
        self.assertNotIn("МАРКЕР", текст)

        with mock.patch.object(telegram.pd, "скрыть", side_effect=lambda текст: (текст, {})), \
                mock.patch.object(telegram.pd, "найти", return_value=["остаток"]):
            текст, виды = telegram.собрать_документы(документ, 0, 0, "test")
        self.assertEqual((текст, виды), (telegram.ОТКАЗ_ПД, {}))
        self.assertNotIn("МАРКЕР", текст)

        with mock.patch.object(telegram.pd, "скрыть", return_value=("И.И. [скрыто]", {})), \
                mock.patch.object(telegram.pd, "найти", return_value=[]):
            текст, виды = telegram.собрать_документы(документ, 0, 0, "test")
        self.assertEqual((текст, виды), (telegram.ОТКАЗ_ПД, {}))

    def test_необрабатываемая_структура_документа_закрывается(self) -> None:
        документ = {"answer": "СЕКРЕТ-МАРКЕР", "sources": ["не объект"], "quotes": [], "usage": {}}
        текст, виды = telegram.собрать_документы(документ, 0, 0, "test")
        self.assertEqual((текст, виды), (telegram.ОТКАЗ_ПД, {}))
        self.assertNotIn("МАРКЕР", текст)

    def test_поля_номера_и_проверки_цитаты_тоже_маскируются(self) -> None:
        документ = {"answer": "Ответ", "sources": [], "quotes": [{
            "n": "И.И. Вымыслов", "text": "Безопасная цитата", "check": "ПЕТРОВИЧ", "confirmed": True,
        }], "usage": {}}
        текст, виды = telegram.собрать_документы(документ, 0, 0, "test")
        self.assertNotEqual(текст, telegram.ОТКАЗ_ПД)
        self.assertNotIn("Вымыслов", текст)
        self.assertNotIn("ПЕТРОВИЧ", текст)
        self.assertIn("Цитаты:\n[[скрыто]] «Безопасная цитата» — ✓ [скрыто] (сокращена фильтром ПД)", текст)
        self.assertTrue(виды)


class АдресTelegramAPI(unittest.TestCase):
    def test_допустимые_адреса(self) -> None:
        случаи = {
            "https://api.telegram.org": "https://api.telegram.org",
            "https://API.TELEGRAM.ORG:443/": "https://api.telegram.org",
            "http://127.0.0.1:8080": "http://127.0.0.1:8080",
            "https://127.77.1.2:9443/": "https://127.77.1.2:9443",
            "http://localhost:18080/": "http://localhost:18080",
            "http://[::1]:8080": "http://[::1]:8080",
        }
        for исходный, ожидаемый in случаи.items():
            with self.subTest(исходный=исходный):
                self.assertEqual(telegram.проверить_api_telegram(исходный), ожидаемый)

    def test_небезопасные_и_неоднозначные_адреса(self) -> None:
        адреса = (
            "http://api.telegram.org", "https://api.telegram.org:444", "https://api.telegram.org.evil.test",
            "https://api.telegram.org/path", "https://api.telegram.org?x=1", "https://api.telegram.org/#x",
            "https://user:pass@api.telegram.org", "https://api.telegram.org@evil.test", "https://example.test",
            "http://0.0.0.0:8080", "http://2130706433:8080", "http://localhost.evil:8080",
            "ftp://127.0.0.1:8080", "http://127.0.0.1:0", " http://127.0.0.1:8080",
            "https://api.telegram.org:", "http://127.0.0.1:", "https://api.telegram.org?",
            "https://api.telegram.org#", "https://api.tele\ngram.org", "",
        )
        for адрес in адреса:
            with self.subTest(адрес=адрес), self.assertRaisesRegex(ValueError, "небезопасный адрес") as ошибка:
                telegram.проверить_api_telegram(адрес)
            if адрес:
                self.assertNotIn(адрес, str(ошибка.exception))

    def test_отказ_происходит_до_создания_http_клиента(self) -> None:
        with mock.patch.object(telegram.httpx, "AsyncClient") as клиент:
            with self.assertRaises(ValueError):
                telegram.BotAPI("123:СЕКРЕТ", "https://evil.test")
        клиент.assert_not_called()

    def test_http_клиент_не_следует_redirect_и_не_берёт_proxy_из_окружения(self) -> None:
        with mock.patch.object(telegram.httpx, "AsyncClient") as клиент:
            telegram.BotAPI("123:TEST", "https://api.telegram.org")
        параметры = клиент.call_args.kwargs
        self.assertIs(параметры["follow_redirects"], False)
        self.assertIs(параметры["trust_env"], False)

    def test_redirect_не_уводит_токен_на_другой_хост(self) -> None:
        запросы: list[str] = []

        def ответ(запрос: httpx.Request) -> httpx.Response:
            запросы.append(str(запрос.url))
            return httpx.Response(302, headers={"location": "https://evil.test/collect"}, text="redirect")

        async def прогон() -> None:
            api = telegram.BotAPI("123:TEST", "http://127.0.0.1:18080", transport=httpx.MockTransport(ответ))
            try:
                with self.assertRaises(telegram.ОшибкаTelegram):
                    await api.getMe()
            finally:
                await api.закрыть()

        asyncio.run(прогон())
        self.assertEqual(len(запросы), 1)
        self.assertTrue(запросы[0].startswith("http://127.0.0.1:18080/"))

    def test_entrypoint_отклоняет_адрес_до_offline_и_не_показывает_его(self) -> None:
        секреты = {"TELEGRAM_BOT_TOKEN": "123:TEST", "TELEGRAM_OWNER_ID": "1", "TELEGRAM_API_BASE": ""}
        опасный = "https://evil.test/СЕКРЕТ-МАРКЕР"
        with mock.patch.object(telegram_bot, "_секреты", return_value=секреты), \
                mock.patch("agent.local.offline.включить") as включить, \
                self.assertLogs("telegram_bot", level="ERROR") as логи:
            код = telegram_bot.main(["--api", опасный])
        self.assertEqual(код, 2)
        включить.assert_not_called()
        self.assertNotIn(опасный, "\n".join(логи.output))

    def test_manage_отклоняет_адрес_до_сетевого_вызова(self) -> None:
        секреты = {"TELEGRAM_BOT_TOKEN": "123:TEST", "TELEGRAM_OWNER_ID": "1",
                   "TELEGRAM_API_BASE": "https://evil.test"}
        with mock.patch.object(manage.unit, "статус", return_value={"состояние": "inactive"}), \
                mock.patch.object(manage, "секреты_бота", return_value=секреты), \
                mock.patch("httpx.post") as post:
            with self.assertRaisesRegex(RuntimeError, "небезопасный адрес"):
                manage.кто_писал()
        post.assert_not_called()

    def test_manage_локальная_проверка_без_redirect_и_proxy(self) -> None:
        секреты = {"TELEGRAM_BOT_TOKEN": "123:TEST", "TELEGRAM_OWNER_ID": "1",
                   "TELEGRAM_API_BASE": "http://127.0.0.1:18080"}
        ответ = mock.Mock()
        ответ.json.return_value = {"ok": True, "result": []}
        with mock.patch.object(manage.unit, "статус", return_value={"состояние": "inactive"}), \
                mock.patch.object(manage, "секреты_бота", return_value=секреты), \
                mock.patch("httpx.post", return_value=ответ) as post:
            self.assertEqual(manage.кто_писал(), [])
        self.assertIs(post.call_args.kwargs["follow_redirects"], False)
        self.assertIs(post.call_args.kwargs["trust_env"], False)


if __name__ == "__main__":
    unittest.main()
