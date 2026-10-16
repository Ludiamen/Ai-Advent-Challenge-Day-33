"""Команда `/help` в чатах агента (День 31, spec FR-08, FR-09, plan D-06).

`/help` без вопроса — справка по командам чата; `/help вопрос` — ответ ассистента
разработчика о проекте. Разбор общий для консоли и веба (паритет, память
web-interface-must-match-cli); бот Telegram разбирает команды сам — у него `/help`
без вопроса мгновенный, а с вопросом идёт в очередь и в сервис.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any

СПРАВКА = "справка"
ВОПРОС = "вопрос"
_HELP = re.compile(r"^/help(?:@[\w_]+)?(?:\s+(?P<вопрос>.*))?$", re.I | re.S)

ПРИМЕР = "/help Как устроен проект и где что лежит?"
ЗАПАСНАЯ_МОДЕЛЬ = "loc-rag-docs"

СПРАВКА_ЧАТА = f"""\
Команды чата:
  /help вопрос   — ассистент разработчика: вопрос о проекте {{проект}} (документация, схемы данных, API, git)
  /help          — эта справка
  /review [диапазон] — AI-ревью ветки, как в PR: баги, архитектура, рекомендации (по умолчанию main...HEAD)
  /память        — память задачи · /история — реплики · /новый — начать заново · /выход
Пример: {ПРИМЕР}"""


def разобрать(текст: str) -> tuple[str, str] | None:
    """(«справка», "") или («вопрос», текст вопроса); None — это не /help."""
    м = _HELP.match((текст or "").strip())
    if not м:
        return None
    вопрос = (м.group("вопрос") or "").strip()
    return (ВОПРОС, вопрос) if вопрос else (СПРАВКА, "")


def справка(проект: str = "") -> str:
    return СПРАВКА_ЧАТА.format(проект=f"«{проект}»" if проект else "")


ХРАНИТЬ = 30   # последних ответов /help на сессию чата


def база_памяти() -> str:
    from agent.devassist.project import КОРЕНЬ
    return os.path.abspath(os.getenv("MEMORY_DIR") or os.path.join(КОРЕНЬ, "memory"))


def в_историю(чат: Any, вопрос: str, ответ: Any, база: str = "") -> None:
    """`/help`-ход — в историю чата обычными репликами и в ленту сессии (`ответы_сессии`);
    память задачи не трогается: ассистент отвечает о проекте, а не о задаче пользователя."""
    источники = list(dict.fromkeys(f"{и.get('source', '')} · {и.get('section', '')}" for и in ответ.источники))
    хвост = f"\n[Источники: {'; '.join(источники)}]" if источники else ""
    чат.короткая.append(чат.сессия_диалога, "user", f"/help {вопрос}")
    чат.короткая.append(чат.сессия_диалога, "assistant", f"{ответ.текст}{хвост}",
                        tokens=ответ.токенов_вход + ответ.токенов_выход)
    if база:
        сохранить(база, чат.сессия, чат.память.реплик, ответ.to_dict())


def _файл(база: str, сессия: str) -> str:
    # Имя сессии вводит человек: в имя файла — хэш, а не сама строка.
    return os.path.join(база, "devassist", "chat", hashlib.sha1(сессия.encode("utf-8")).hexdigest()[:16] + ".json")


def ответы_сессии(база: str, сессия: str) -> list[dict[str, Any]]:
    try:
        with open(_файл(база, сессия), encoding="utf-8") as f:
            данные = json.load(f)
        return данные if isinstance(данные, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def сохранить(база: str, сессия: str, после_хода: int, ответ: dict[str, Any]) -> None:
    """Ответ /help в ленту сессии: страница вставляет его после хода `после_хода`."""
    путь = _файл(база, сессия)
    os.makedirs(os.path.dirname(путь), exist_ok=True)
    записи = ответы_сессии(база, сессия) + [{**ответ, "after_turn": после_хода,
                                            "when": time.strftime("%Y-%m-%dT%H:%M:%S")}]
    временный = путь + ".tmp"
    with open(временный, "w", encoding="utf-8") as f:
        json.dump(записи[-ХРАНИТЬ:], f, ensure_ascii=False)
    os.replace(временный, путь)


def очистить(база: str, сессия: str) -> bool:
    try:
        os.remove(_файл(база, сессия))
        return True
    except OSError:
        return False


def создать(модель: str, канал: str, проект: str = "", клиент: Any = None):
    """Ассистент для канала: проект из projects.json, git — через MCP-сервер `git`."""
    from agent.devassist.assistant import Ассистент
    from agent.devassist.project import загрузить
    from agent.llm import Client
    from agent.local import offline
    # Модель — выбор канала («--модель», список на странице); без выбора — ds-flash, а в офлайне —
    # локальная (облачную предохранитель всё равно не пропустил бы).
    модель = модель or (ЗАПАСНАЯ_МОДЕЛЬ if offline.включён() else "ds-flash")
    return Ассистент(загрузить(проект), клиент or Client(), модель=модель, канал=канал)
