"""Ключи API приватного сервиса (spec FR-05, plan D-04).

Ключ — `sk-day30-` и 32 случайных байта (base64url); показывается один раз при
создании. На диске — только sha256 ключа (ключ случайный и длинный, соль не
нужна), имя, права и время создания и отзыва. Сравнение — `hmac.compare_digest`.

  Ключи(путь).создать(имя, права) -> ключ
  .проверить(ключ) -> запись | None     отозванный и неизвестный — None
  .список() / .отозвать(имя)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Any

ПРАВА = ("чат", "документы", "админ", "проект")   # «проект» — /v1/project/help, День 31
ПРЕФИКС = "sk-day30-"


class ОшибкаКлюча(ValueError):
    pass


def хэш(ключ: str) -> str:
    return hashlib.sha256(ключ.encode("utf-8")).hexdigest()


class Ключи:
    def __init__(self, путь: str) -> None:
        self.путь = путь
        self._замок = threading.Lock()

    def _читать(self) -> list[dict[str, Any]]:
        try:
            with open(self.путь, encoding="utf-8") as f:
                return list(json.load(f).get("ключи") or [])
        except FileNotFoundError:
            return []

    def _писать(self, записи: list[dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.путь) or ".", exist_ok=True)
        временный = f"{self.путь}.{os.getpid()}.tmp"
        with open(временный, "w", encoding="utf-8") as f:
            json.dump({"ключи": записи}, f, ensure_ascii=False, indent=1)
        os.chmod(временный, 0o600)
        os.replace(временный, self.путь)

    def создать(self, имя: str, права: list[str] | tuple[str, ...]) -> str:
        имя = (имя or "").strip()
        if not имя:
            raise ОшибкаКлюча("у ключа должно быть имя")
        чужие = [п for п in права if п not in ПРАВА]
        if чужие or not права:
            raise ОшибкаКлюча(f"права — из {', '.join(ПРАВА)}; получено: {', '.join(права) or 'ничего'}")
        with self._замок:
            записи = self._читать()
            if any(з["имя"] == имя and not з.get("отозван") for з in записи):
                raise ОшибкаКлюча(f"ключ «{имя}» уже есть — сначала отзовите его")
            ключ = ПРЕФИКС + secrets.token_urlsafe(32)
            записи.append({"имя": имя, "хэш": хэш(ключ), "права": sorted(set(права), key=ПРАВА.index),
                           "создан": time.strftime("%Y-%m-%dT%H:%M:%S"), "отозван": ""})
            self._писать(записи)
        return ключ

    def проверить(self, ключ: str) -> dict[str, Any] | None:
        if not ключ or not ключ.startswith(ПРЕФИКС):
            return None
        х = хэш(ключ)
        найден = None
        for з in self._читать():
            # Сравниваем со всеми, без раннего выхода: время ответа не выдаёт, на каком ключе совпало.
            if hmac.compare_digest(з["хэш"], х) and not з.get("отозван"):
                найден = з
        return {к: в for к, в in найден.items() if к != "хэш"} if найден else None

    def список(self) -> list[dict[str, Any]]:
        return [{к: в for к, в in з.items() if к != "хэш"} for з in self._читать()]

    def отозвать(self, имя: str) -> bool:
        with self._замок:
            записи = self._читать()
            было = False
            for з in записи:
                if з["имя"] == имя and not з.get("отозван"):
                    з["отозван"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    было = True
            if было:
                self._писать(записи)
        return было
