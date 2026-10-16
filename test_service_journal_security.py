"""Изолированные проверки прав и содержимого журналов приватного сервиса."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent.service.journal import Журнал


def режим(путь: Path) -> int:
    return stat.S_IMODE(путь.stat().st_mode)


class БезопасностьЖурнала(unittest.TestCase):
    def test_создание_и_повторное_открытие_всегда_0600(self):
        with tempfile.TemporaryDirectory(prefix="day30-journal-") as каталог:
            путь = Path(каталог, "requests.jsonl")
            прежняя_umask = os.umask(0o002)
            try:
                журнал = Журнал(str(путь))
                журнал.записать(адрес="192.0.2.10", ключ="synthetic-name", ручка="/v1/models", код=200)
                self.assertEqual(режим(путь), 0o600)

                os.chmod(путь, 0o666)
                повторно = Журнал(str(путь))
                self.assertEqual(режим(путь), 0o600, "повторное открытие сужает случайно расширенные права")
                повторно.записать(адрес="192.0.2.11", ключ="synthetic-name", ручка="/health", код=200)
                self.assertEqual(режим(путь), 0o600)
            finally:
                os.umask(прежняя_umask)

            записи = [json.loads(строка) for строка in путь.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([запись["ручка"] for запись in записи], ["/v1/models", "/health"])

    def test_ротация_сохраняет_0600_и_jsonl(self):
        with tempfile.TemporaryDirectory(prefix="day30-journal-rotation-") as каталог:
            путь = Path(каталог, "requests.jsonl")
            прежняя_umask = os.umask(0o002)
            try:
                журнал = Журнал(str(путь), размер=1)
                первая = журнал.записать(адрес="192.0.2.20", ключ="synthetic-name",
                                         ручка="/v1/models", код=200)
                os.chmod(путь, 0o664)
                вторая = журнал.записать(адрес="192.0.2.21", ключ="synthetic-name",
                                         ручка="/v1/chat/completions", код=502, ошибка="upstream_error")
            finally:
                os.umask(прежняя_umask)

            ротированный = Path(str(путь) + ".1")
            self.assertEqual((режим(путь), режим(ротированный)), (0o600, 0o600))
            self.assertEqual(json.loads(ротированный.read_text(encoding="utf-8")), первая)
            self.assertEqual(json.loads(путь.read_text(encoding="utf-8")), вторая)

            os.chmod(ротированный, 0o664)
            Журнал(str(путь), размер=1)
            self.assertEqual(режим(ротированный), 0o600, "повторное открытие защищает и файл .1")

    def test_журнал_не_пишет_секреты_и_тексты(self):
        with tempfile.TemporaryDirectory(prefix="day30-journal-content-") as каталог:
            путь = Path(каталог, "requests.jsonl")
            api_ключ = "sk-day30-SYNTHETIC-KEY-NOT-REAL"
            токен = "SYNTHETIC_TOKEN_NOT_REAL"
            вопрос = "Синтетический приватный вопрос"
            Журнал(str(путь)).записать(
                адрес="192.0.2.30", ключ="synthetic-key-name", ручка="/v1/documents/answer", код=200,
                api_key=api_ключ, authorization=f"Bearer {api_ключ}", token=токен,
                вопрос=вопрос, текст=вопрос, произвольное="не должно попасть")

            текст = путь.read_text(encoding="utf-8")
            for секрет in (api_ключ, токен, вопрос):
                self.assertNotIn(секрет, текст)
            запись = json.loads(текст)
            self.assertEqual(запись["ключ"], "synthetic-key-name")
            self.assertEqual(set(запись), {"время", "адрес", "ключ", "ручка", "код"})
            self.assertEqual(режим(путь), 0o600)

    def test_fifo_отклоняется_без_блокировки(self):
        with tempfile.TemporaryDirectory(prefix="day30-journal-fifo-") as каталог:
            путь = Path(каталог, "requests.jsonl")
            os.mkfifo(путь, 0o600)
            сценарий = """
import sys
from agent.service.journal import Журнал

try:
    Журнал(sys.argv[1])
except OSError as exc:
    print(type(exc).__name__)
else:
    raise SystemExit("FIFO неожиданно принят как журнал")
"""
            результат = subprocess.run(
                [sys.executable, "-B", "-c", сценарий, str(путь)],
                cwd=Path(__file__).resolve().parent,
                capture_output=True,
                text=True,
                # Предел ловит вечное зависание на open() FIFO. 2 с было мало: один импорт
                # agent.service.journal на нагруженном ноутбуке — около 2 с (тест падал случайно).
                timeout=30,
                check=False,
            )

            self.assertEqual(результат.returncode, 0, результат.stderr)
            self.assertEqual(результат.stdout.strip(), "OSError")
            self.assertTrue(stat.S_ISFIFO(путь.stat().st_mode))

    def test_hardlink_отклоняется_до_chmod_и_ротации(self):
        with tempfile.TemporaryDirectory(prefix="day30-journal-hardlink-") as каталог:
            цель = Path(каталог, "target.jsonl")
            путь = Path(каталог, "requests.jsonl")
            содержимое = b'{"synthetic": true}\n'
            цель.write_bytes(содержимое)
            os.chmod(цель, 0o644)
            os.link(цель, путь)

            with self.assertRaisesRegex(OSError, "ровно одна жёсткая ссылка"):
                Журнал(str(путь), размер=1)

            self.assertEqual(режим(цель), 0o644)
            self.assertEqual(цель.read_bytes(), содержимое)
            self.assertEqual(цель.stat().st_nlink, 2)
            self.assertFalse(Path(str(путь) + ".1").exists(), "hardlink нельзя ротировать")


if __name__ == "__main__":
    unittest.main()
