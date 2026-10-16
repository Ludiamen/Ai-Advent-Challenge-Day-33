#!/usr/bin/env python3
"""Приватный AI-сервис Дней 30–31 — запуск шлюза перед локальной Ollama.

    python service.py                 # адрес, порт, лимиты — из service.json
    python service.py --порт 8444     # стенд: со своим MEMORY_DIR

Обычно запускается службой пользователя systemd (`python cli.py --сервис
установить`, затем `--сервис старт`). Ключи, журнал запросов и сертификаты —
в $MEMORY_DIR/service/ (вне репозитория): сертификат выпускает `python cli.py
--сервис-сертификат`, ключи — `python cli.py --сервис-ключ ИМЯ --права чат`.

Сервис работает в офлайн-режиме Дня 27: облако и внешние адреса заблокированы,
журнал сети показывает, что запросы не уходят с машины. Исключение одно (День 31):
ассистент проекта (/v1/project/help) в режиме «облако» ходит в api.deepseek.com.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from typing import Any

КОРЕНЬ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, КОРЕНЬ)


def собрать(порт: int | None = None):
    """Настоящий шлюз: офлайн, токенизатор из GGUF, ответ по документам, состояние для /admin."""
    import httpx

    from agent.service import certs, config, gateway, journal, keys, load, manage, tokens, unit

    н = config.загрузить()
    # День 31: единственный внешний хост — облачная модель ассистента проекта (api.deepseek.com).
    # Разрешение — с запуска, чтобы переключатель «облако / локально» работал без перезапуска;
    # облачную модель зовёт только ассистент ручки /v1/project/help (чат и документы — loc-rag-docs).
    внешние = {х.strip() for х in os.environ.get("OFFLINE_ALLOW_EXTERNAL", "").split(",") if х.strip()}
    os.environ["OFFLINE_ALLOW_EXTERNAL"] = ",".join(sorted(внешние | {н.проект.облачный_хост}))
    from agent.local import offline, resources
    offline.включить(True, "service.py")
    данные = config.каталог_данных()
    tls = manage.каталог_tls()
    crt, key = os.path.join(tls, "server.crt"), os.path.join(tls, "server.key")
    if not (os.path.exists(crt) and os.path.exists(key)):
        raise SystemExit("Нет сертификата сервиса — выпустите: python cli.py --сервис-сертификат")
    путь_весов = tokens.путь_весов(н.ollama, н.модель)
    блоб = os.path.basename(путь_весов)
    токенизатор = tokens.Токенизатор(путь_весов)
    токенизатор.число("прогрев")                       # словарь — сразу, не на первом посетителе

    отвечатель: dict[str, Any] = {}
    замок = threading.Lock()

    def ответить(вопрос: str) -> dict[str, Any]:
        with замок:
            if not отвечатель:
                from agent.llm import Client
                from agent.rag import Индексатор, profile
                from agent.rag.answer import Отвечатель
                о = Отвечатель(Индексатор(), Client(), "структура", 5)
                отвечатель.update(о=о, профиль=profile.локальный(о.поисковик))
        д = отвечатель["о"].ответить(вопрос, "rag", н.модель, формат="цитаты", профиль=отвечатель["профиль"]).to_dict()
        if д.get("error"):
            raise RuntimeError(д["error"])
        return {"answer": д.get("text", ""), "abstain": bool(д.get("abstain")),
                "clarification": д.get("clarification", ""),
                "sources": [{к: и.get(к) for к in ("n", "source", "section", "page", "chunk_id")}
                            for и in д.get("sources") or []],
                "quotes": [{к: ц.get(к) for к in ("n", "text", "check", "confirmed")} for ц in д.get("quotes") or []],
                "usage": {"prompt_tokens": д.get("tokens_in"), "completion_tokens": д.get("tokens_out")},
                "timings": {"search_s": д.get("search_seconds"), "model_s": д.get("model_seconds")}}

    помощники: dict[str, Any] = {}
    замок_помощника = threading.Lock()

    def помочь(вопрос: str) -> dict[str, Any]:
        """Ассистент разработчика (День 31): тот же модуль, что в консоли и вебе; модель — по переключателю.

        Очередь шлюза пускает одну генерацию за раз, поэтому ассистент один и вопросы идут по одному."""
        with замок_помощника:
            if not помощники:
                from agent.devassist import project
                from agent.devassist.assistant import Ассистент
                from agent.llm import Client
                п = project.загрузить(н.проект.имя)
                помощники["а"] = Ассистент(п, Client(), модель=н.проект.модель, канал="сервис",
                                           запасная=н.проект.запасная,
                                           журнал=os.path.join(данные, "devassist.jsonl"))
            а = помощники["а"]
            а.модель = config.модель_проекта(н)
            итог = а.спросить(вопрос).to_dict()
        if итог.get("error"):
            raise RuntimeError(итог["error"])
        return итог

    def режим(значение: str | None) -> dict[str, Any]:
        if значение is not None:
            config.задать_модель_проекта(н, значение)
        return config.режим_проекта(н)

    наблюдатели: dict[str, Any] = {}

    def шаг(событие: str, ступень: str) -> dict[str, Any]:
        """Отметка ступени нагрузки: пик памяти и секунды процессора раннера за ступень (День 29)."""
        if событие == "начало":
            набл = resources.Наблюдатель(блоб, None)
            набл.__enter__()
            наблюдатели[ступень] = набл
            return {"ok": True}
        набл = наблюдатели.pop(ступень, None)
        if набл is None:
            return {"ok": False}
        набл.__exit__(None, None, None)
        return {"ok": True, **набл.итог}

    def состояние() -> dict[str, Any]:
        pid = resources.раннер(блоб, None)
        каталог_нагрузки = load.каталог_итогов(КОРЕНЬ)
        прогоны = load.прогоны(каталог_нагрузки)
        п = load.полный(каталог_нагрузки)
        полный = {"файл": п["файл"], **load.сводка(п)} if п else None
        return {"network": {**offline.снимок_журнала(), "внешние_хосты": offline.внешние_хосты()},
                "unit": unit.статус(),
                "cert": {**certs.сведения(crt), "адрес_в_сети": certs.адрес_в_сети()},
                "runner": ({"pid": pid, **(resources.показания(pid) or {})} if pid else None),
                "load": {"идёт": manage.идёт_нагрузка(), "последний": прогоны[0] if прогоны else None,
                         "полный": полный},
                "telegram": manage.состояние_бота(),
                "pid": os.getpid()}

    ollama = httpx.AsyncClient(base_url=н.ollama, timeout=httpx.Timeout(н.лимиты.таймаут_с + 30, connect=10))
    с = gateway.Сервис(н, keys.Ключи(os.path.join(данные, "keys.json")),
                       journal.Журнал(os.path.join(данные, "requests.jsonl")), токенизатор, ollama,
                       отвечатель=ответить, состояние=состояние, шаг=шаг, нагрузка=manage.нагрузка_фоном,
                       телеграм=manage.бот_служба, помощник=помочь, режим_проекта=режим)
    app = gateway.создать_приложение(с)

    async def прогрев() -> None:
        import asyncio
        asyncio.ensure_future(с.прогреть())

    app.router.on_startup.append(прогрев)
    return app, н, crt, key


def main() -> int:
    разбор = argparse.ArgumentParser(description="Приватный AI-сервис Дня 30 (шлюз перед Ollama)")
    разбор.add_argument("--порт", type=int, default=0, help="порт (по умолчанию — из service.json)")
    разбор.add_argument("--память", default="", help="каталог памяти (MEMORY_DIR) — для стенда")
    а = разбор.parse_args()
    if а.память:
        os.environ["MEMORY_DIR"] = os.path.abspath(а.память)
    os.chdir(КОРЕНЬ)
    import uvicorn
    app, н, crt, key = собрать()
    порт = а.порт or н.порт
    print(f"Приватный сервис: https://{н.адрес}:{порт} · модель {н.модель} · очередь {н.лимиты.очередь} · "
          f"лимит {н.лимиты.запросов_в_минуту}/мин на ключ · вход ≤ {н.лимиты.вход_токенов} ток.", flush=True)
    uvicorn.run(app, host=н.адрес, port=порт, ssl_certfile=crt, ssl_keyfile=key, log_level="warning",
                timeout_keep_alive=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
