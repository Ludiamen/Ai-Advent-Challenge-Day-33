"""Службы systemd пользователя Дня 30: сервис (spec FR-15, plan D-14) и Telegram-бот
(telegram-spec FR-T13, telegram-plan D-T01).

Без sudo: `~/.config/systemd/user/day31-llm.service` и `day31-tg.service`,
`Restart=on-failure`. Без linger службы живут, пока владелец в системе (ворота 0, A-6).

  текст(python, корень, память) / текст_бота(...) -> содержимое unit-файла
  установить(..., имя) / systemctl(действие) / статус(имя) -> dict
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

ИМЯ = "day31-llm.service"
ИМЯ_БОТА = "day31-tg.service"


def путь_unit(имя: str = ИМЯ) -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "systemd", "user", имя)


def текст(python: str, корень: str, память: str) -> str:
    return (
        "[Unit]\n"
        "Description=День 31: приватный AI-сервис (шлюз перед Ollama, loc-rag-docs; ассистент проекта)\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"WorkingDirectory={корень}\n"
        f"Environment=MEMORY_DIR={память}\n"
        "Environment=PYTHONIOENCODING=utf-8\n"
        f"ExecStart={python} {os.path.join(корень, 'service.py')}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def текст_бота(python: str, корень: str, память: str) -> str:
    """Бот — после сервиса. Коды 2 (нет токена или ключа) и 3 (второй опросчик) не перезапускаются:
    повтор их не исправит. Сеть Telegram недоступна — бот ждёт сам, не падая (ворота 0, A-T6)."""
    return (
        "[Unit]\n"
        "Description=День 31: Telegram-бот (чат владельца с loc-rag-docs и /help о проекте через приватный сервис)\n"
        f"After=network-online.target {ИМЯ}\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        f"WorkingDirectory={корень}\n"
        f"Environment=MEMORY_DIR={память}\n"
        "Environment=PYTHONIOENCODING=utf-8\n"
        f"ExecStart={python} {os.path.join(корень, 'telegram_bot.py')}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "RestartPreventExitStatus=2 3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def systemctl(*аргументы: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *аргументы], capture_output=True, text=True, timeout=60)


def установить(python: str, корень: str, память: str, имя: str = ИМЯ) -> str:
    путь = путь_unit(имя)
    os.makedirs(os.path.dirname(путь), exist_ok=True)
    with open(путь, "w", encoding="utf-8") as f:
        f.write((текст_бота if имя == ИМЯ_БОТА else текст)(python, корень, память))
    systemctl("daemon-reload")
    return путь


def статус(имя: str = ИМЯ) -> dict[str, Any]:
    готово = systemctl("show", имя, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", "-p", "NRestarts",
                       "-p", "ActiveEnterTimestamp", "-p", "LoadState")
    поля = dict(с.split("=", 1) for с in готово.stdout.splitlines() if "=" in с)
    return {"установлена": поля.get("LoadState") == "loaded", "состояние": поля.get("ActiveState", ""),
            "подсостояние": поля.get("SubState", ""), "pid": int(поля.get("MainPID") or 0),
            "перезапусков": int(поля.get("NRestarts") or 0), "с": поля.get("ActiveEnterTimestamp", "")}
