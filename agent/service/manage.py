"""Управление приватным сервисом — общее для консоли и страницы /admin (веб = консоль).

  сертификат() / создать_ключ() / ключи() / отозвать() / служба(действие)
  ключи_нагрузки(чат, документы) -> {"чат": [...], "документы": [...], "админ": "…"}
  запустить_нагрузку(...) -> итог прогона (клиент — в контейнере Docker)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from agent.service import certs, config, keys, load, unit

ОБРАЗ = "day30-load:local"
ВОПРОСЫ_ЧАТА = (
    "Что такое SLA? Ответь в двух предложениях.", "Чем VPN отличается от прокси-сервера? Кратко.",
    "Назови три признака фишингового письма.", "Что такое правило резервного копирования 3-2-1?",
    "Объясни одним абзацем, что такое DNS.", "Чем HTTPS отличается от HTTP?",
    "Что такое двухфакторная аутентификация?", "Зачем организации файрвол?",
    "Что такое IP-телефония? Кратко.", "Дай три совета, как выбрать надёжный пароль.",
    "Что такое облачное хранилище файлов?", "Чем SSD отличается от HDD?",
    "Что такое пропускная способность канала связи?", "Зачем вовремя обновлять операционную систему?",
    "Что такое электронная подпись?", "Что такое ИБП и зачем он серверу?",
    "Что такое DHCP? Кратко.", "Чем полезен журнал событий сервера?",
)


def каталог_tls() -> str:
    return os.path.join(config.каталог_данных(), "tls")


def хранилище_ключей() -> keys.Ключи:
    return keys.Ключи(os.path.join(config.каталог_данных(), "keys.json"))


def сертификат() -> dict[str, Any]:
    н = config.загрузить()
    с = н.сертификат
    адреса = [*с.get("адреса", []), certs.адрес_в_сети()]
    return certs.выпустить(каталог_tls(), адреса, list(с.get("имена", [])), int(с.get("дней", 825)))


def создать_ключ(имя: str, права: list[str]) -> str:
    return хранилище_ключей().создать(имя, права)


def ключи() -> list[dict[str, Any]]:
    return хранилище_ключей().список()


def отозвать(имя: str) -> bool:
    return хранилище_ключей().отозвать(имя)


def служба(действие: str) -> dict[str, Any]:
    if действие == "установить":
        путь = unit.установить(sys.executable, config.КОРЕНЬ, os.path.dirname(config.каталог_данных()))
        return {"unit": путь, **unit.статус()}
    if действие in ("старт", "стоп", "перезапуск"):
        готово = unit.systemctl({"старт": "start", "стоп": "stop", "перезапуск": "restart"}[действие], unit.ИМЯ)
        if готово.returncode != 0:
            raise RuntimeError(готово.stderr.strip() or f"systemctl {действие}: код {готово.returncode}")
        if действие != "стоп":
            готов(30)
    return unit.статус()


def готов(секунд: float = 30) -> bool:
    """Ждать, пока сервис ответит на /health (импорт и словарь модели — несколько секунд)."""
    import httpx
    н = config.загрузить()
    ca = os.path.join(каталог_tls(), "ca.crt")
    конец = time.monotonic() + секунд
    while time.monotonic() < конец:
        try:
            if httpx.get(f"https://127.0.0.1:{н.порт}/health", verify=ca, timeout=3).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def ключи_нагрузки(чат: int = 8, документы: int = 2) -> dict[str, Any]:
    """Свой ключ у каждого клиента нагрузки (мерим очередь, а не лимит ключа) и ключ «админ»
    для отметок ступеней. Открытые ключи — в $MEMORY_DIR/service/load-keys.json (0600)."""
    путь = os.path.join(config.каталог_данных(), "load-keys.json")
    хранилище = хранилище_ключей()
    try:
        с = json.loads(Path(путь).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        с = {}
    живые = {з["имя"] for з in хранилище.список() if not з.get("отозван")}
    нужны = [(f"нагрузка-{i}", ["чат"]) for i in range(1, чат + 1)] + \
            [(f"нагрузка-д{i}", ["документы"]) for i in range(1, документы + 1)] + [("нагрузка-админ", ["админ"])]
    for имя, права in нужны:
        if имя in с and имя in живые and хранилище.проверить(с[имя]):
            continue
        if имя in живые:
            хранилище.отозвать(имя)
        с[имя] = хранилище.создать(имя, права)
    временный = путь + ".tmp"
    with open(временный, "w", encoding="utf-8") as f:
        json.dump(с, f, ensure_ascii=False, indent=1)
    os.chmod(временный, 0o600)
    os.replace(временный, путь)
    return {"чат": [с[f"нагрузка-{i}"] for i in range(1, чат + 1)],
            "документы": [с[f"нагрузка-д{i}"] for i in range(1, документы + 1)], "админ": с["нагрузка-админ"]}


def вопросы_документов(n: int) -> list[str]:
    """Только вопросы, проверенные без ПД (local-opt.json → gpu.годные, День 29)."""
    from agent.local import tuning
    from agent.rag import localbench
    годные = list((tuning.прочитать().get("gpu") or {}).get("годные") or [])
    return [в["вопрос"] for в in localbench.вопросы(ids=годные)][:n]


def образ() -> None:
    if subprocess.run(["docker", "image", "inspect", ОБРАЗ], capture_output=True).returncode == 0:
        return
    готово = subprocess.run(["docker", "build", "-q", "-t", ОБРАЗ, "-f", os.path.join(config.КОРЕНЬ, "docker", "load.Dockerfile"),
                             os.path.join(config.КОРЕНЬ, "docker")], capture_output=True, text=True, timeout=900)
    if готово.returncode != 0:
        raise RuntimeError(f"docker build: {готово.stderr.strip()[-400:]}")


_идёт = threading.Event()


def идёт_нагрузка() -> bool:
    return _идёт.is_set()


def запустить_нагрузку(ступени: list[int], вопросов: int = 2, документы: int = 2, max_tokens: int = 200,
                       адрес: str = "", метка: str = "", сеть: str = "", вывод=None) -> dict[str, Any]:
    """Клиент в контейнере Docker ходит на сервис по сети (адрес в домашней сети) и пишет
    итог в index/service-load/run-<метка>.json. Возвращает сводку."""
    if _идёт.is_set():
        return {"error": "нагрузка уже идёт"}
    _идёт.set()
    try:
        н = config.загрузить()
        адрес = адрес or f"https://{certs.адрес_в_сети() or '127.0.0.1'}:{н.порт}"
        метка = метка or time.strftime("%Y%m%d-%H%M%S")
        образ()
        к = ключи_нагрузки(max(ступени or [1]), документы)
        рабочий = os.path.join(config.каталог_данных(), "load-run")
        os.makedirs(рабочий, exist_ok=True)
        задание = {"адрес": адрес, "ступени": ступени, "вопросов": вопросов, "max_tokens": max_tokens,
                   "вопросы_чата": list(ВОПРОСЫ_ЧАТА), "вопросы_документов": вопросы_документов(документы),
                   "ключи": к, "метка": метка}
        путь_задания = os.path.join(рабочий, "task.json")
        with open(путь_задания, "w", encoding="utf-8") as f:
            json.dump(задание, f, ensure_ascii=False)
        os.chmod(путь_задания, 0o600)
        итоги = load.каталог_итогов(config.КОРЕНЬ)
        команда = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
                   *(["--network", сеть] if сеть else []),
                   "-v", f"{os.path.join(config.КОРЕНЬ, 'service_load.py')}:/app/service_load.py:ro",
                   "-v", f"{os.path.join(каталог_tls(), 'ca.crt')}:/app/ca.crt:ro",
                   "-v", f"{путь_задания}:/app/task.json:ro", "-v", f"{итоги}:/out",
                   ОБРАЗ, "python", "/app/service_load.py", "--задание", "/app/task.json", "--ca", "/app/ca.crt",
                   "--выход", f"/out/run-{метка}.json"]
        процесс = subprocess.Popen(команда, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for строка in процесс.stdout:
            if вывод:
                вывод(строка.rstrip())
        процесс.wait()
        файл = os.path.join(итоги, f"run-{метка}.json")
        if процесс.returncode != 0 or not os.path.exists(файл):
            return {"error": f"клиент нагрузки завершился с кодом {процесс.returncode}"}
        прогон = json.loads(Path(файл).read_text(encoding="utf-8"))
        return {"файл": файл, "сводка": load.сводка(прогон)}
    finally:
        # В задании — открытые ключи нагрузки: файл не должен пережить прогон.
        путь_задания = os.path.join(config.каталог_данных(), "load-run", "task.json")
        if os.path.exists(путь_задания):
            os.remove(путь_задания)
        _идёт.clear()


def нагрузка_фоном(параметры: dict[str, Any]) -> dict[str, Any]:
    """Со страницы /admin: прогон в фоне; ход и итог — в /admin/api/state."""
    if _идёт.is_set():
        return {"error": "нагрузка уже идёт"}
    ступени = [int(с) for с in (параметры.get("ступени") or [параметры.get("клиенты") or 1])]
    threading.Thread(target=запустить_нагрузку, daemon=True,
                     kwargs={"ступени": ступени, "вопросов": int(параметры.get("вопросов") or 1),
                             "документы": int(параметры.get("документы") or 0),
                             "max_tokens": int(параметры.get("max_tokens") or 200)}).start()
    return {"ok": True, "ступени": ступени}


# --- Telegram-бот (telegram-plan D-T05, D-T12, D-T15) ------------------------------------

ИМЯ_КЛЮЧА_БОТА = "телеграм"
ПРАВА_БОТА = ("чат", "документы", "проект")   # «проект» — /help вопрос через /v1/project/help (День 31)


def секреты_бота() -> dict[str, str]:
    """TELEGRAM_* из окружения или .env. Наружу (консоль, страница) — только «задан / не задан»."""
    from dotenv import dotenv_values
    файл = {к: з for к, з in dotenv_values(os.path.join(config.КОРЕНЬ, ".env")).items() if з}
    return {к: os.environ.get(к) or файл.get(к, "") for к in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "TELEGRAM_API_BASE")}


def файл_ключа_бота() -> str:
    return os.path.join(config.каталог_данных(), "telegram.key")


def бот_ключ() -> dict[str, Any]:
    """Ключ «телеграм» (чат, документы) для бота — в файл 0600; прежний ключ с этим именем отзывается.
    Сам ключ не возвращается: его читает только процесс бота."""
    хранилище = хранилище_ключей()
    if any(з["имя"] == ИМЯ_КЛЮЧА_БОТА and not з.get("отозван") for з in хранилище.список()):
        хранилище.отозвать(ИМЯ_КЛЮЧА_БОТА)
    ключ = хранилище.создать(ИМЯ_КЛЮЧА_БОТА, ПРАВА_БОТА)
    путь = файл_ключа_бота()
    временный = путь + ".tmp"
    дескриптор = os.open(временный, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(дескриптор, "w", encoding="utf-8") as f:
        f.write(ключ + "\n")
    os.replace(временный, путь)
    return {"имя": ИМЯ_КЛЮЧА_БОТА, "права": list(ПРАВА_БОТА), "файл": путь}


def бот_служба(действие: str) -> dict[str, Any]:
    """Служба day31-tg: установить, старт, стоп, перезапуск, статус — то же у консоли и /admin."""
    if действие == "установить":
        путь = unit.установить(sys.executable, config.КОРЕНЬ, os.path.dirname(config.каталог_данных()), unit.ИМЯ_БОТА)
        return {"unit": путь, **unit.статус(unit.ИМЯ_БОТА)}
    if действие in ("старт", "стоп", "перезапуск"):
        готово = unit.systemctl({"старт": "start", "стоп": "stop", "перезапуск": "restart"}[действие], unit.ИМЯ_БОТА)
        if готово.returncode != 0:
            raise RuntimeError(готово.stderr.strip() or f"systemctl {действие}: код {готово.returncode}")
        if действие != "стоп":
            time.sleep(3)                    # getMe и первое состояние — пара секунд
    elif действие != "статус":
        raise ValueError(f"нет такого действия: {действие}")
    return unit.статус(unit.ИМЯ_БОТА)


def состояние_бота(записей: int = 15) -> dict[str, Any]:
    """Для /admin и консоли: служба, состояние процесса бота, хвост его журнала — без текстов и ID владельца."""
    from agent.service import telegram
    from agent.service.journal import Журнал
    каталог = config.каталог_данных()
    try:
        состояние = json.loads(Path(каталог, telegram.СОСТОЯНИЕ).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        состояние = None
    журнал = Журнал(os.path.join(каталог, telegram.ЖУРНАЛ), поля=telegram.ПОЛЯ_ЖУРНАЛА)
    хвост = журнал.последние(200)
    с = секреты_бота()
    return {"служба": unit.статус(unit.ИМЯ_БОТА), "состояние": состояние, "журнал": хвост[:записей],
            "чужих": sum(1 for з in хвост if з.get("кто") == "чужой"),
            "токен_задан": bool(с["TELEGRAM_BOT_TOKEN"]), "владелец_задан": bool(с["TELEGRAM_OWNER_ID"]),
            "ключ_есть": os.path.exists(файл_ключа_бота())}


def кто_писал() -> list[dict[str, Any]]:
    """Кто писал боту (ID, имя пользователя, время) — чтобы записать TELEGRAM_OWNER_ID. Тексты не читаются.
    Только при остановленном боте: два опросчика одного бота Telegram не допускает (409)."""
    import httpx
    from agent.service import telegram
    if unit.статус(unit.ИМЯ_БОТА)["состояние"] == "active":
        raise RuntimeError("бот работает — сначала python cli.py --телеграм стоп (иначе два опросчика)")
    с = секреты_бота()
    if not с["TELEGRAM_BOT_TOKEN"]:
        raise RuntimeError("нет TELEGRAM_BOT_TOKEN в .env")
    try:
        база = telegram.проверить_api_telegram(с["TELEGRAM_API_BASE"] or telegram.API_TELEGRAM)
    except ValueError:
        raise RuntimeError("небезопасный адрес Telegram Bot API") from None
    try:
        ответ = httpx.post(f"{база}/bot{с['TELEGRAM_BOT_TOKEN']}/getUpdates", json={"timeout": 0}, timeout=30,
                           follow_redirects=False, trust_env=False).json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(telegram.без_токена(f"Bot API: {type(exc).__name__}: {exc}")) from None
    if not ответ.get("ok"):
        raise RuntimeError(telegram.без_токена(f"Bot API: {ответ.get('description')}"))
    кто: dict[int, dict[str, Any]] = {}
    for о in ответ.get("result") or []:
        с_ = о.get("message") or {}
        от = с_.get("from") or {}
        if от.get("id") is not None:
            кто[от["id"]] = {"id": от["id"], "имя_пользователя": от.get("username") or "", "имя": от.get("first_name") or "",
                            "чат": (с_.get("chat") or {}).get("type"), "когда": time.strftime(
                                "%Y-%m-%d %H:%M", time.localtime(с_.get("date") or 0))}
    return list(кто.values())
