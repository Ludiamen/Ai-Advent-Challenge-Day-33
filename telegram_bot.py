#!/usr/bin/env python3
"""Telegram-бот Дня 30 — вход процесса (telegram-plan D-T01, D-T11, D-T13).

Бот — клиент приватного сервиса: ходит в https://127.0.0.1:<порт> со своим ключом
(`memory/service/telegram.key`, создаётся `python cli.py --телеграм-ключ`) и
опрашивает Telegram long polling'ом. Секреты — в `.env`: TELEGRAM_BOT_TOKEN и
TELEGRAM_OWNER_ID (единственный, кому бот отвечает).

Процесс работает с офлайн-предохранителем Дня 27: внешний хост разрешён один —
адрес Bot API (OFFLINE_ALLOW_EXTERNAL); токен не печатается и не пишется в журналы
(логгеры httpx — WARNING: на INFO они печатают URL, а в нём токен).

    python telegram_bot.py                       # служба day31-tg запускает именно так
    python telegram_bot.py --память DIR --api http://127.0.0.1:PORT   # проверка: свой каталог, подменный Bot API

Коды выхода: 0 — остановлен сигналом; 2 — нет токена, ключа или токен не принят;
3 — второй опросчик того же бота (409).
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import os
import signal
import sys
from urllib.parse import urlparse

from dotenv import dotenv_values

КОРЕНЬ = os.path.dirname(os.path.abspath(__file__))


def _секреты() -> dict[str, str]:
    """Окружение важнее файла: проверка подставляет свои значения, служба берёт .env."""
    файл = {к: з for к, з in dotenv_values(os.path.join(КОРЕНЬ, ".env")).items() if з}
    return {к: os.environ.get(к) or файл.get(к, "") for к in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "TELEGRAM_API_BASE")}


def _петля(хост: str) -> bool:
    try:
        return хост == "localhost" or ipaddress.ip_address(хост).is_loopback
    except ValueError:
        return False


def настроить_логи() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    for имя in ("httpx", "httpcore"):
        logging.getLogger(имя).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    разбор = argparse.ArgumentParser(description="Telegram-бот Дня 30: чат владельца с локальной моделью.")
    разбор.add_argument("--память", default="", metavar="DIR", help="свой каталог памяти (по умолчанию memory/)")
    разбор.add_argument("--api", default="", metavar="URL", help="адрес Bot API (по умолчанию api.telegram.org)")
    разбор.add_argument("--сервис", dest="service", default="", metavar="URL", help="адрес сервиса Дня 30")
    а = разбор.parse_args(argv)
    if а.память:
        os.environ["MEMORY_DIR"] = os.path.abspath(а.память)
    настроить_логи()
    лог = logging.getLogger("telegram_bot")

    с = _секреты()
    токен = с["TELEGRAM_BOT_TOKEN"]
    if not токен:
        лог.error("Нет TELEGRAM_BOT_TOKEN в .env — создайте бота у @BotFather и запишите токен в Day-30/.env")
        return 2
    владелец = int(с["TELEGRAM_OWNER_ID"]) if с["TELEGRAM_OWNER_ID"].strip().lstrip("-").isdigit() else None

    from agent.service import telegram as tg
    try:
        api_база = tg.проверить_api_telegram(а.api or с["TELEGRAM_API_BASE"] or tg.API_TELEGRAM)
    except ValueError:
        лог.error("Небезопасный адрес Telegram Bot API: разрешены официальный HTTPS API и loopback для тестов")
        return 2
    хост_api = urlparse(api_база).hostname or ""
    if not _петля(хост_api):
        os.environ["OFFLINE_ALLOW_EXTERNAL"] = "api.telegram.org"
    else:
        os.environ.pop("OFFLINE_ALLOW_EXTERNAL", None)
    from agent.local import offline
    offline.включить(True, "telegram_bot.py")

    from agent.service import config
    настройки = config.загрузить()
    данные = config.каталог_данных()

    файл_ключа = os.path.join(данные, "telegram.key")
    try:
        with open(файл_ключа, encoding="utf-8") as f:
            ключ = f.read().strip()
    except OSError:
        ключ = ""
    if not ключ:
        лог.error("Нет ключа сервиса для бота: python cli.py --телеграм-ключ")
        return 2
    адрес_сервиса = (а.service or f"https://127.0.0.1:{настройки.порт}").rstrip("/")
    ca = os.path.join(данные, "tls", "ca.crt")

    def сеть() -> dict:
        ж = offline.журнал(50)
        return {"внешних": ж["внешних"], "локальных": ж["локальных"], "заблокировано": ж["заблокировано"],
                "внешние_хосты": sorted({з["host"] for з in ж["recent"] if not з["local"]})}

    async def работать() -> int:
        бот = tg.Бот(tg.BotAPI(токен, база=api_база),
                     tg.КлиентСервиса(адрес_сервиса, ключ, ca=ca if os.path.exists(ca) else None,
                                      модель=настройки.модель),
                     владелец=владелец, каталог=данные, сеть=сеть)
        стоп = asyncio.Event()
        задача = asyncio.current_task()
        цикл = asyncio.get_running_loop()

        def остановить() -> None:
            стоп.set()
            if задача is not None:
                задача.cancel()

        сигналы = (signal.SIGTERM, signal.SIGINT)
        for сигнал in сигналы:
            цикл.add_signal_handler(сигнал, остановить)
        код = 0
        try:
            я = await бот.познакомиться_с_повторами(стоп)
            лог.info("Бот %s · Bot API %s · сервис %s · владелец %s · группы %s", бот.имя, хост_api,
                     адрес_сервиса, "задан" if владелец is not None else "НЕ задан — бот никому не отвечает",
                     "разрешены" if я.get("can_join_groups") else "запрещены")
            бот.записать_состояние()
            await бот.опрашивать(стоп)
        except asyncio.CancelledError:
            лог.info("Остановлен сигналом")
        except tg.ОшибкаTelegram as exc:
            лог.error("Bot API остановил бота: %s%s", exc,
                      " — бота уже опрашивает другой процесс" if exc.код == 409 else "")
            код = 3 if exc.код == 409 else 2
        finally:
            стоп.set()
            for сигнал in сигналы:
                цикл.remove_signal_handler(сигнал)
            try:
                бот.записать_состояние()
            finally:
                await бот.закрыть()
        return код

    return asyncio.run(работать())


if __name__ == "__main__":
    sys.exit(main())
