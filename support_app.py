"""Мини-сервис поддержки пользователей портала ИТ-отдела (День 33, spec FR-06, FR-07, FR-09, plan D-07).

- `/support` — чат пользователя: свои тикеты, новое обращение, переписка с ИИ-ассистентом (ответ с
  источниками и фактами тикета), оценка 👍/👎;
- `/support/desk` — пульт сотрудника: очередь, тикет с профилем и журналом входов, заявки ИИ на
  действия — подтвердить / отклонить, ответ, взять, закрыть с решением, вернуть ИИ;
- `/api/support/*`, `/api/desk/*` — то же для программ.

Вход — по ключу (`cli.py --поддержка-ключ`): ссылка `…?key=` только на первый вход, дальше cookie
(HttpOnly, SameSite=Strict, Secure под HTTPS), адрес переписывается без ключа. Лимит сообщений на
ключ, журнал запросов без текстов (`memory/support/requests.jsonl`).

    python support_app.py                    # https://<адрес>:8450 (свой центр сертификации в memory/support/tls)
    python support_app.py --http --порт 8451 # без TLS (за обратным прокси или для проверок)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import deque
from typing import Any

КОРЕНЬ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, КОРЕНЬ)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(КОРЕНЬ, ".env"))
except ImportError:
    pass

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402

from agent.support import access, data, metrics  # noqa: E402
from agent.support.assistant import Ассистент  # noqa: E402
from agent.support.desk import ОшибкаДоступа, СервисДеск  # noqa: E402
from agent.support.kb import БазаЗнаний  # noqa: E402

ПОРТ = 8450
COOKIE = "support_key"
ЛИМИТ_СООБЩЕНИЙ = int(os.getenv("SUPPORT_RATE", "12"))     # в минуту на ключ
ШАБЛОНЫ = os.path.join(КОРЕНЬ, "templates")


class Лимит:
    def __init__(self, в_минуту: int) -> None:
        self.в_минуту = в_минуту
        self.окна: dict[str, deque] = {}
        self.замок = threading.Lock()

    def можно(self, ключ: str) -> bool:
        сейчас = time.monotonic()
        with self.замок:
            окно = self.окна.setdefault(ключ, deque())
            while окно and сейчас - окно[0] > 60:
                окно.popleft()
            if len(окно) >= self.в_минуту:
                return False
            окно.append(сейчас)
            return True


def создать_приложение(хранилище: data.Хранилище | None = None, ассистент: Ассистент | None = None,
                       ключи: access.КлючиПоддержки | None = None, https: bool = True) -> FastAPI:
    хранилище = хранилище or data.Хранилище()
    ключи = ключи or access.КлючиПоддержки()
    if ассистент is None:
        ассистент = Ассистент(БазаЗнаний.загрузить(), модель=os.getenv("SUPPORT_MODEL", "ds-flash"))
    деск = СервисДеск(хранилище, ассистент, канал="веб")
    лимит = Лимит(ЛИМИТ_СООБЩЕНИЙ)
    журнал = os.path.join(os.path.dirname(хранилище.путь), "requests.jsonl")
    app = FastAPI(title="Поддержка портала ИТ-отдела", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.деск = деск

    @app.middleware("http")
    async def журнал_запросов(request: Request, call_next):
        старт = time.monotonic()
        ответ = await call_next(request)
        try:
            os.makedirs(os.path.dirname(журнал), exist_ok=True)
            with open(журнал, "a", encoding="utf-8") as f:   # без текстов, без ключей и адреса запроса с параметрами
                f.write(json.dumps({"время": time.strftime("%Y-%m-%dT%H:%M:%S"), "метод": request.method,
                                    "путь": request.url.path, "код": ответ.status_code,
                                    "мс": round((time.monotonic() - старт) * 1000)}, ensure_ascii=False) + "\n")
        except OSError:
            pass
        ответ.headers["Cache-Control"] = "no-store"
        ответ.headers["Referrer-Policy"] = "no-referrer"
        return ответ

    def кто(request: Request, роль: str) -> dict[str, Any]:
        к = ключи.проверить(request.cookies.get(COOKIE, "") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
        if not к:
            raise HTTPException(401, "нужен ключ доступа")
        if к["роль"] != роль:
            raise HTTPException(403, "нет права на этот раздел")
        return к

    def страница(имя: str) -> HTMLResponse:
        with open(os.path.join(ШАБЛОНЫ, имя), encoding="utf-8") as f:
            return HTMLResponse(f.read())

    def войти(request: Request, путь: str) -> RedirectResponse | None:
        ключ = request.query_params.get("key", "")
        if not ключ:
            return None
        if not ключи.проверить(ключ):
            raise HTTPException(401, "ключ не подошёл")
        ответ = RedirectResponse(путь, status_code=303)              # адрес без ключа
        ответ.set_cookie(COOKIE, ключ, httponly=True, samesite="strict", secure=https, max_age=30 * 24 * 3600)
        return ответ

    @app.get("/support", response_class=HTMLResponse)
    def чат(request: Request):
        return войти(request, "/support") or страница("support.html")

    @app.get("/support/desk", response_class=HTMLResponse)
    def пульт(request: Request):
        return войти(request, "/support/desk") or страница("support_desk.html")

    @app.post("/api/support/logout")
    def выйти():
        ответ = JSONResponse({"ok": True})
        ответ.delete_cookie(COOKIE)
        return ответ

    @app.get("/api/support/health")
    def здоровье():
        return {"ok": True, "модель": ассистент.модель, "фрагментов": len(ассистент.база.фрагменты)}

    # --- пользователь ------------------------------------------------------------------------

    @app.get("/api/support/me")
    def я(request: Request):
        к = кто(request, "пользователь")
        п = хранилище.пользователь(к["кто"])
        return {"пользователь": {"id": п["id"], "имя": п["имя"], "роль": п["роль"], "подразделение": п["подразделение"]},
                "тикеты": [{к_: т.get(к_) for к_ in ("номер", "тема", "статус", "создана")} for т in деск.мои_тикеты(п["id"])]}

    @app.get("/api/support/tickets/{n}")
    def мой_тикет(n: int, request: Request):
        к = кто(request, "пользователь")
        try:
            return деск.тикет_пользователя(к["кто"], n)
        except ОшибкаДоступа as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/api/support/messages")
    async def написать(request: Request):
        к = кто(request, "пользователь")
        if not лимит.можно(к["кто"]):
            raise HTTPException(429, "слишком много сообщений — подождите минуту")
        тело = await request.json()
        текст = str(тело.get("text") or "").strip()
        if not текст or len(текст) > 4000:
            raise HTTPException(400, "сообщение пустое или длиннее 4000 символов")
        номер = тело.get("ticket")
        import anyio
        try:
            return await anyio.to_thread.run_sync(lambda: деск.написать(к["кто"], текст, int(номер) if номер else None))
        except ОшибкаДоступа as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/api/support/tickets/{n}/rate")
    async def оценить(n: int, request: Request):
        к = кто(request, "пользователь")
        тело = await request.json()
        try:
            return деск.оценить(к["кто"], n, int(тело.get("score", 0)))
        except ОшибкаДоступа as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    # --- пульт ----------------------------------------------------------------------------------

    @app.get("/api/desk/queue")
    def очередь(request: Request, status: str = ""):
        кто(request, "сотрудник")
        return {"тикеты": деск.очередь(status), "заявки": деск.заявки()}

    @app.get("/api/desk/tickets/{n}")
    def карточка(n: int, request: Request):
        кто(request, "сотрудник")
        try:
            return деск.карточка(n)
        except data.ОшибкаДанных as e:
            raise HTTPException(404, str(e)) from e

    def _действие(функция, *аргументы):
        try:
            return функция(*аргументы)
        except data.ОшибкаДанных as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/desk/requests/{rid}/apply")
    def подтвердить(rid: str, request: Request):
        к = кто(request, "сотрудник")
        return _действие(деск.подтвердить, rid, к["кто"])

    @app.post("/api/desk/requests/{rid}/reject")
    async def отклонить(rid: str, request: Request):
        к = кто(request, "сотрудник")
        тело = await request.json()
        return _действие(деск.отклонить, rid, к["кто"], str(тело.get("reason") or ""))

    @app.post("/api/desk/tickets/{n}/reply")
    async def ответить(n: int, request: Request):
        к = кто(request, "сотрудник")
        тело = await request.json()
        текст = str(тело.get("text") or "").strip()
        if not текст:
            raise HTTPException(400, "пустой ответ")
        return _действие(деск.ответить, n, текст, к["кто"])

    @app.post("/api/desk/tickets/{n}/take")
    def взять(n: int, request: Request):
        к = кто(request, "сотрудник")
        return _действие(деск.взять, n, к["кто"])

    @app.post("/api/desk/tickets/{n}/close")
    async def закрыть(n: int, request: Request):
        к = кто(request, "сотрудник")
        тело = await request.json()
        return _действие(деск.закрыть, n, str(тело.get("resolution") or ""), к["кто"])

    @app.post("/api/desk/tickets/{n}/return-ai")
    async def вернуть(n: int, request: Request):
        к = кто(request, "сотрудник")
        import anyio
        return await anyio.to_thread.run_sync(lambda: _действие(деск.вернуть_ии, n, к["кто"]))

    @app.get("/api/desk/metrics")
    def метрики(request: Request):
        кто(request, "сотрудник")
        return metrics.сводка(metrics.прочитать())

    return app


def main() -> int:
    разбор = argparse.ArgumentParser(description="Мини-сервис поддержки портала ИТ-отдела.")
    разбор.add_argument("--порт", type=int, default=int(os.getenv("SUPPORT_PORT", ПОРТ)))
    разбор.add_argument("--адрес", default=os.getenv("SUPPORT_HOST", "0.0.0.0"))
    разбор.add_argument("--http", action="store_true", help="без TLS (за обратным прокси или для проверок)")
    разбор.add_argument("--ключ", nargs=2, metavar=("РОЛЬ", "КТО"), default=None,
                        help="выдать ключ доступа («пользователь u01» или «сотрудник имя») и выйти — для контейнера, "
                             "где нет cli.py: docker compose exec support python support_app.py --ключ …")
    разбор.add_argument("--сброс", action="store_true", help="вернуть данные сервис-деска к seed и выйти")
    а = разбор.parse_args()
    if а.сброс:
        д = data.Хранилище().сбросить()
        print(f"данные сброшены: пользователей {len(д['пользователи'])}, тикетов {len(д['тикеты'])}")
        return 0
    if а.ключ:
        роль, кто = а.ключ
        ключ = access.КлючиПоддержки().создать(роль, кто)
        print(ключ)
        print(f"/support{'/desk' if роль == 'сотрудник' else ''}?key={ключ}")
        return 0
    import uvicorn
    tls: dict[str, Any] = {}
    if not а.http:
        from agent.service import certs
        каталог = os.path.join(os.path.dirname(data.путь_по_умолчанию()), "tls")
        crt, key = os.path.join(каталог, "server.crt"), os.path.join(каталог, "server.key")
        # Адреса в сертификате: свой, адрес в сети и SUPPORT_TLS_HOSTS (публичный адрес VPS — в
        # контейнере его не видно). Выпуск — при первом старте или если списка адресов стало больше.
        доп = [а.strip() for а in os.getenv("SUPPORT_TLS_HOSTS", "").split(",") if а.strip()]
        адреса = ["127.0.0.1", certs.адрес_в_сети(), *[а for а in доп if а.replace(".", "").isdigit()]]
        имена = ["localhost", *[а for а in доп if not а.replace(".", "").isdigit()]]
        san = set(certs.сведения(crt).get("san", [])) if os.path.exists(crt) else set()
        if not (os.path.exists(crt) and os.path.exists(key)) or not all(any(а in с for с in san) for а in доп):
            certs.выпустить(каталог, адреса, имена)
        tls = {"ssl_certfile": crt, "ssl_keyfile": key}
    uvicorn.run(создать_приложение(https=not а.http), host=а.адрес, port=а.порт, log_level="warning", **tls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
