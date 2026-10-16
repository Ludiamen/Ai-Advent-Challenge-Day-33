"""Эскалация человеку: сообщение владельцу в Telegram (spec FR-10, plan D-11).

Лекция недели 7: сбой и опасный результат не должны оставаться только в журнале. Красный
вердикт, невыполненное ревью и тревога метрик — короткое сообщение через бота: номер и
ссылка PR, вердикт, счёт находок по разделам, причина. Ни кода, ни текстов находок — их
читают в PR. Отправка — прямой `sendMessage` Bot API (раннер GitHub не видит сеть
владельца, сервис Дня 30 ему недоступен); опросчик `getUpdates` службы бота этим не
задевается. Токен бота не попадает в журнал и тексты ошибок.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from agent.service import pd

API = "https://api.telegram.org"
ТАЙМАУТ = 15.0


def нужно(вердикт: str, тревоги: list[str]) -> bool:
    return вердикт in ("красный", "не выполнено") or bool(тревоги)


def текст(заголовок: str, номер: int, ссылка: str, вердикт: str, причина: str,
          по_разделам: dict[str, int], тревоги: list[str]) -> str:
    значок = {"красный": "🔴", "не выполнено": "⚠️"}.get(вердикт, "🟢")
    строки = [f"{значок} AI-ревью PR #{номер}: {вердикт}",
              f"«{заголовок[:120]}»" if заголовок else "",
              причина[:300],
              "Находки: " + ", ".join(f"{р} — {n}" for р, n in по_разделам.items()) if по_разделам else "",
              *[f"📈 {т[:200]}" for т in тревоги],
              ссылка]
    return pd.скрыть("\n".join(с for с in строки if с), профиль="точный")[0]


def отправить(сообщение: str, токен: str = "", чат: str = "", api: str = "",
              http: httpx.Client | None = None) -> str:
    """Отправить сообщение; вернуть «отправлено» или причину, почему нет (без токена в тексте)."""
    токен = токен or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    чат = чат or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not токен or not чат:
        return "пропущено: нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID"
    клиент = http or httpx.Client(timeout=ТАЙМАУТ)
    последняя = ""
    for _ in range(2):
        try:
            ответ = клиент.post(f"{(api or API).rstrip('/')}/bot{токен}/sendMessage",
                                json={"chat_id": чат, "text": сообщение, "disable_web_page_preview": True})
        except httpx.HTTPError as e:
            последняя = f"сеть: {type(e).__name__}"
            continue
        if ответ.status_code == 200 and (ответ.json() or {}).get("ok"):
            return "отправлено"
        последняя = f"Telegram {ответ.status_code}"
        if ответ.status_code < 500 and ответ.status_code != 429:
            break
    return f"не отправлено: {последняя}".replace(токен, "***")


def из_итога(итог: Any, номер: int, ссылка: str, заголовок: str) -> str:
    сборка = итог.сборка.метрики()
    return текст(заголовок, номер, ссылка, итог.вердикт, итог.сборка.причина, сборка.get("по_разделам", {}),
                 итог.тревоги)
