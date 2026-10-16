"""MCP-сервер мини-сервис-деска поддержки (День 33, spec FR-02, plan D-02).

Два набора инструментов над одними данными (`agent/support/data.py`):

- **ассистент** (`--набор ассистент`) — первая линия поддержки, ИИ. Сервер **закреплён за одним
  тикетом** (`SUPPORT_TICKET`, задаётся при запуске на каждый ответ): любой номер, кроме этого,
  — отказ. Видно: тикет, его заявитель (профиль и состояние учётной записи), журнал входов и
  прошлые тикеты этого заявителя, политика портала. Можно: ответить в тикет, поставить статус
  ИИ, **завести заявку** на действие над учётной записью — выполнить её нельзя, инструмента
  выполнения в наборе нет. Чужого пользователя назвать нельзя: инструменты не принимают id
  пользователя, только номер закреплённого тикета.
- **пульт** (`--набор пульт`) — сотрудник поддержки: очередь, любой тикет, заявки, выполнить или
  отклонить заявку, ответить, закрыть с решением. Модели не выдаётся (в `mcp-servers.json` —
  отдельный сервер, ассистент его не подключает).

    python support_server.py --набор ассистент        # stdio, SUPPORT_TICKET=1001
    python support_server.py --набор пульт
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Annotated, Any

from pydantic import Field

КОРЕНЬ_СКРИПТА = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, КОРЕНЬ_СКРИПТА)

from mcp.server import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp_types import ToolAnnotations  # noqa: E402

from agent.support import data  # noqa: E402

ВЕРСИЯ = "33.0"
ПРЕДЕЛ_ВХОДОВ = 20
ЧТЕНИЕ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
ЗАПИСЬ = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
ДЕЙСТВИЕ = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

Номер = Annotated[int, Field(description="Номер тикета (закреплённого за этим ответом).", ge=1)]


class ОшибкаЗапроса(ToolError):
    """Понятный отказ — текст доходит до модели."""


def хранилище() -> data.Хранилище:
    return data.Хранилище()


def закреплённый() -> int:
    значение = os.environ.get("SUPPORT_TICKET", "")
    if not значение.isdigit():
        raise ОшибкаЗапроса("сервер ассистента не закреплён за тикетом (SUPPORT_TICKET)")
    return int(значение)


def _свой(номер: int) -> int:
    свой = закреплённый()
    if int(номер) != свой:
        raise ОшибкаЗапроса(f"доступен только тикет №{свой} — данные других тикетов и пользователей недоступны")
    return свой


def _обернуть(функция, *аргументы: Any, **поля: Any) -> Any:
    try:
        return функция(*аргументы, **поля)
    except data.ОшибкаДанных as e:
        raise ОшибкаЗапроса(str(e)) from e


def _профиль(п: dict[str, Any]) -> dict[str, Any]:
    """Профиль без почты: ассистенту адрес не нужен, а в ответ он не должен попасть."""
    return {"id": п["id"], "имя": п["имя"], "роль": п["роль"], "подразделение": п["подразделение"],
            "учётная_запись": п["учётная_запись"] or "нет — заявитель без учётной записи портала",
            **({"примечание": п["примечание"]} if п.get("примечание") else {})}


def _кратко(т: dict[str, Any]) -> dict[str, Any]:
    return {к: т.get(к) for к in ("номер", "тема", "категория", "статус", "создана", "решение")}


# --- ассистент ------------------------------------------------------------------------------

ассистент = MCPServer("support", title="Сервис-деск поддержки — ассистент (один тикет)", version=ВЕРСИЯ,
                      instructions="Первая линия поддержки портала ИТ-отдела. Данные — только закреплённого тикета "
                                   "и его заявителя. Действия над учётной записью — только заявкой "
                                   "(support_request_action): выполняет сотрудник после подтверждения.",
                      log_level="WARNING")


@ассистент.tool(title="Тикет", annotations=ЧТЕНИЕ)
def support_ticket(ticket: Номер) -> dict[str, Any]:
    """Тикет: тема, категория, статус, сообщения (автор, время, текст)."""
    return _обернуть(хранилище().тикет, _свой(ticket))


@ассистент.tool(title="Заявитель и его учётная запись", annotations=ЧТЕНИЕ)
def support_requester(ticket: Номер) -> dict[str, Any]:
    """Заявитель тикета: роль, подразделение, состояние учётной записи (блокировка, второй фактор,
    резервные коды, пароль, приглашение, устройства, сессия)."""
    return _профиль(_обернуть(хранилище().заявитель, _свой(ticket)))


@ассистент.tool(title="Журнал входов заявителя", annotations=ЧТЕНИЕ)
def support_logins(ticket: Номер,
                   limit: Annotated[int, Field(description="Сколько последних записей, 1–20.", ge=1, le=ПРЕДЕЛ_ВХОДОВ)] = 10
                   ) -> dict[str, Any]:
    """Последние попытки входа заявителя: время, итог (успешно, неверный пароль, неверный код,
    блокировка…), устройство, адрес."""
    х = хранилище()
    заявитель = _обернуть(х.заявитель, _свой(ticket))
    return {"входы": х.входы(заявитель["id"], limit)}


@ассистент.tool(title="Прошлые тикеты заявителя", annotations=ЧТЕНИЕ)
def support_history(ticket: Номер) -> dict[str, Any]:
    """Другие тикеты этого же заявителя — кратко (номер, тема, статус, решение)."""
    х = хранилище()
    свой = _свой(ticket)
    заявитель = _обернуть(х.заявитель, свой)
    return {"тикеты": [_кратко(т) for т in х.тикеты_пользователя(заявитель["id"]) if т["номер"] != свой]}


@ассистент.tool(title="Политика портала", annotations=ЧТЕНИЕ)
def support_policy() -> dict[str, Any]:
    """Действующие значения политики: попыток до блокировки, минут блокировки, бездействия, длина
    пароля, срок пароля, срок ссылок, резервных кодов, автозакрытие заявок."""
    return хранилище().политика()


@ассистент.tool(title="Ответить в тикет", annotations=ЗАПИСЬ)
def support_reply(ticket: Номер, text: Annotated[str, Field(description="Текст ответа заявителю.", min_length=1,
                                                             max_length=4000)],
                  sources: Annotated[list[str], Field(description="Подписи источников ответа.", max_length=10)] = [],
                  facts: Annotated[list[str], Field(description="Ключи фактов тикета, на которые опирается ответ.",
                                                    max_length=20)] = [],
                  decision: str = "") -> dict[str, Any]:
    """Сообщение ассистента в тикет (с источниками и фактами — их видит пользователь и сотрудник)."""
    return _обернуть(хранилище().добавить_сообщение, _свой(ticket), "ассистент", text,
                     **{"источники": [str(и)[:200] for и in sources][:10], "факты": [str(ф)[:80] for ф in facts][:20],
                        **({"решение": decision[:40]} if decision else {})})


@ассистент.tool(title="Статус тикета (ИИ)", annotations=ЗАПИСЬ)
def support_set_status(ticket: Номер, status: Annotated[str, Field(
        description="Ждёт ответа заявителя | Передан специалисту | Ждёт подтверждения действия")]) -> dict[str, Any]:
    """Статус, который может поставить ассистент."""
    return _кратко(_обернуть(хранилище().сменить_статус, _свой(ticket), status, "ассистент"))


@ассистент.tool(title="Заявка на действие (ждёт подтверждения)", annotations=ДЕЙСТВИЕ)
def support_request_action(ticket: Номер,
                           action: Annotated[str, Field(description="разблокировать | сбросить_второй_фактор | "
                                                        "новые_резервные_коды | новое_приглашение | ссылка_пароля | "
                                                        "закрыть_сессии")],
                           reason: Annotated[str, Field(description="Почему нужно — по фактам тикета.", min_length=3,
                                                        max_length=300)]) -> dict[str, Any]:
    """Заводит заявку на действие над учётной записью заявителя. Ничего не выполняет: выполнит
    сотрудник поддержки после проверки (подтверждение человеком)."""
    з = _обернуть(хранилище().завести_заявку, _свой(ticket), action, reason, "ассистент")
    return {**з, "пояснение": "заявка ждёт подтверждения сотрудника — действие ещё не выполнено"}


# --- пульт ------------------------------------------------------------------------------------

пульт = MCPServer("support-desk", title="Сервис-деск поддержки — пульт сотрудника", version=ВЕРСИЯ,
                  instructions="Пульт сотрудника поддержки: очередь, тикеты, заявки на действия. Не для модели.",
                  log_level="WARNING")


@пульт.tool(title="Очередь", annotations=ЧТЕНИЕ)
def desk_queue(status: str = "") -> dict[str, Any]:
    """Тикеты (кратко), фильтр по статусу."""
    return {"тикеты": [_кратко(т) | {"заявитель": т["заявитель"]} for т in хранилище().очередь((status,) if status else ())]}


@пульт.tool(title="Тикет", annotations=ЧТЕНИЕ)
def desk_ticket(ticket: Номер) -> dict[str, Any]:
    х = хранилище()
    т = _обернуть(х.тикет, ticket)
    п = х.пользователь(т["заявитель"])
    return {"тикет": т, "заявитель": п, "входы": х.входы(п["id"], 10),
            "заявки": [з for з in х.заявки() if з["тикет"] == т["номер"]]}


@пульт.tool(title="Заявки", annotations=ЧТЕНИЕ)
def desk_requests(status: str = "ждёт подтверждения") -> dict[str, Any]:
    return {"заявки": хранилище().заявки(status)}


@пульт.tool(title="Выполнить заявку", annotations=ДЕЙСТВИЕ)
def desk_apply(request: str, staff: str = "сотрудник") -> dict[str, Any]:
    return _обернуть(хранилище().выполнить_заявку, request, staff)


@пульт.tool(title="Отклонить заявку", annotations=ЗАПИСЬ)
def desk_reject(request: str, reason: str, staff: str = "сотрудник") -> dict[str, Any]:
    return _обернуть(хранилище().отклонить_заявку, request, staff, reason)


@пульт.tool(title="Ответить", annotations=ЗАПИСЬ)
def desk_reply(ticket: Номер, text: str) -> dict[str, Any]:
    return _обернуть(хранилище().добавить_сообщение, ticket, "исполнитель", text)


@пульт.tool(title="Закрыть с решением", annotations=ЗАПИСЬ)
def desk_close(ticket: Номер, resolution: str, staff: str = "сотрудник") -> dict[str, Any]:
    return _кратко(_обернуть(хранилище().закрыть, ticket, resolution, staff))


НАБОРЫ = {"ассистент": ассистент, "пульт": пульт}


def main() -> int:
    разбор = argparse.ArgumentParser(description="MCP-сервер сервис-деска поддержки.")
    разбор.add_argument("--набор", dest="set", choices=list(НАБОРЫ), default="ассистент")
    а = разбор.parse_args()
    if а.set == "ассистент":
        закреплённый()                      # понятная ошибка до старта
    НАБОРЫ[а.set].run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
