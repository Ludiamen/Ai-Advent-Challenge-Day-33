"""Журнал запросов приватного сервиса (spec FR-13, plan D-12).

Строка JSONL на запрос — только метаданные: время, адрес клиента, имя ключа,
ручка, код ответа, ожидание в очереди, длительность, токены. Тексты вопросов и
ответов не пишутся никогда: журнал можно показывать на странице и в видео.
Ротация — по размеру (5 МБ): старый файл становится `.1`.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from collections import deque
from typing import Any

РАЗМЕР = 5 * 1024 * 1024
ПОЛЯ = ("время", "адрес", "ключ", "ручка", "код", "ошибка", "поток", "ожидание_с", "длительность_с",
        "вход", "ответ", "причина", "очередь", "модель")   # «модель» — ручка проекта (День 31): облако или локально


class Журнал:
    def __init__(self, путь: str, размер: int = РАЗМЕР, поля: tuple[str, ...] = ПОЛЯ) -> None:
        self.путь = путь
        self.размер = размер
        self.поля = поля             # белый список: поле не из него в журнал не попадает
        self._замок = threading.Lock()
        self._последние: deque[dict[str, Any]] = deque(maxlen=200)
        self._защитить_существующий(self.путь)
        self._защитить_существующий(self.путь + ".1")
        for з in self._хвост(200):
            self._последние.append(з)

    @staticmethod
    def _проверить_дескриптор(fd: int, путь: str) -> None:
        """Принять только обычный файл с единственным именем."""
        сведения = os.fstat(fd)
        if not stat.S_ISREG(сведения.st_mode):
            raise OSError(f"Журнал должен быть обычным файлом: {путь}")
        if сведения.st_nlink != 1:
            raise OSError(f"У файла журнала должна быть ровно одна жёсткая ссылка: {путь}")

    @staticmethod
    def _защитить_существующий(путь: str) -> None:
        """Существующий журнал — обычный файл 0600; ссылки не разыменовываем."""
        флаги = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) |
                 getattr(os, "O_NONBLOCK", 0))
        try:
            fd = os.open(путь, флаги)
        except FileNotFoundError:
            return
        try:
            Журнал._проверить_дескриптор(fd, путь)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)

    @staticmethod
    def _открыть_для_добавления(путь: str):
        """Атомарно создать/открыть журнал без зависимости от umask."""
        флаги = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) |
                 getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        fd = os.open(путь, флаги, 0o600)
        try:
            Журнал._проверить_дескриптор(fd, путь)
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, "a", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise

    def _хвост(self, n: int) -> list[dict[str, Any]]:
        try:
            with open(self.путь, encoding="utf-8") as f:
                строки = f.readlines()[-n:]
        except FileNotFoundError:
            return []
        итог = []
        for с in строки:
            try:
                итог.append(json.loads(с))
            except ValueError:
                pass
        return итог

    def записать(self, **поля: Any) -> dict[str, Any]:
        запись = {"время": time.strftime("%Y-%m-%dT%H:%M:%S")}
        запись.update({к: в for к, в in поля.items() if к in self.поля and в is not None})
        with self._замок:
            os.makedirs(os.path.dirname(self.путь) or ".", exist_ok=True)
            if os.path.exists(self.путь) and os.path.getsize(self.путь) > self.размер:
                self._защитить_существующий(self.путь)
                os.replace(self.путь, self.путь + ".1")
            with self._открыть_для_добавления(self.путь) as f:
                f.write(json.dumps(запись, ensure_ascii=False) + "\n")
            self._последние.append(запись)
        return запись

    def последние(self, n: int = 50) -> list[dict[str, Any]]:
        with self._замок:
            return list(self._последние)[-n:][::-1]

    def клиенты(self, строк: int = 5000) -> list[dict[str, Any]]:
        """Кто ходил в сервис: адрес → запросов, отказов, последний раз (по хвосту журнала)."""
        итог: dict[str, dict[str, Any]] = {}
        for з in self._хвост(строк):
            к = итог.setdefault(з.get("адрес", ""), {"адрес": з.get("адрес", ""), "запросов": 0, "отказов": 0,
                                                    "последний": ""})
            к["запросов"] += 1
            к["отказов"] += int((з.get("код") or 0) >= 400)
            к["последний"] = max(к["последний"], з.get("время", ""))
        return sorted(итог.values(), key=lambda к: к["последний"], reverse=True)
