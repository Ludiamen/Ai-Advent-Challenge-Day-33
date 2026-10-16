"""Ключи доступа мини-сервиса поддержки (День 33, plan D-07).

Ключ привязан к роли: «пользователь» (к id пользователя сервис-деска — видит свои тикеты) или
«сотрудник» (пульт). На диске — только хэш (как ключи сервиса Дня 30), ключ показывается один
раз. Ссылка `/support?key=…` нужна только на первый вход: сервис ставит cookie и переписывает
адрес без ключа — в журнале и истории браузера ключа нет.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from typing import Any

from agent.service.keys import хэш

РОЛИ = ("пользователь", "сотрудник")
ПРЕФИКС = "sk-sup-"
_замок = threading.Lock()


class ОшибкаДоступа(ValueError):
    pass


def путь_по_умолчанию() -> str:
    from agent.support.data import КОРЕНЬ
    память = os.path.abspath(os.getenv("MEMORY_DIR") or os.path.join(КОРЕНЬ, "memory"))
    return os.path.join(память, "support", "keys.json")


class КлючиПоддержки:
    def __init__(self, путь: str = "") -> None:
        self.путь = путь or путь_по_умолчанию()

    def _читать(self) -> list[dict[str, Any]]:
        try:
            with open(self.путь, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return []

    def _писать(self, записи: list[dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.путь), exist_ok=True)
        временный = self.путь + ".tmp"
        with open(временный, "w", encoding="utf-8") as f:
            json.dump(записи, f, ensure_ascii=False, indent=1)
        os.chmod(временный, 0o600)
        os.replace(временный, self.путь)

    def создать(self, роль: str, кто: str) -> str:
        """Новый ключ для роли; прежний ключ того же «кто» отзывается."""
        if роль not in РОЛИ:
            raise ОшибкаДоступа(f"роль — одна из {РОЛИ}")
        if not кто:
            raise ОшибкаДоступа("нужен id пользователя или имя сотрудника")
        ключ = ПРЕФИКС + secrets.token_urlsafe(24)
        with _замок:
            записи = [з for з in self._читать() if not (з["роль"] == роль and з["кто"] == кто)]
            записи.append({"роль": роль, "кто": кто, "хэш": хэш(ключ), "создан": time.strftime("%Y-%m-%dT%H:%M:%S")})
            self._писать(записи)
        return ключ

    def проверить(self, ключ: str) -> dict[str, Any] | None:
        if not ключ or not ключ.startswith(ПРЕФИКС):
            return None
        h = хэш(ключ)
        for з in self._читать():
            if secrets.compare_digest(з["хэш"], h):
                return {"роль": з["роль"], "кто": з["кто"]}
        return None

    def список(self) -> list[dict[str, Any]]:
        return [{к: з[к] for к in ("роль", "кто", "создан")} for з in self._читать()]

    def отозвать(self, роль: str, кто: str) -> bool:
        with _замок:
            записи = self._читать()
            остались = [з for з in записи if not (з["роль"] == роль and з["кто"] == кто)]
            self._писать(остались)
        return len(остались) != len(записи)
