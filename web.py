#!/usr/bin/env python3
"""Веб-интерфейс: чат с агентом и наглядная раскладка памяти по слоям.

Запуск:
    python web.py
Затем открыть http://127.0.0.1:5000

Главная мысль страницы — не чат, а то, что справа от него: слои памяти со своим
содержимым и профиль, который пользователь правит прямо здесь. Изменил
предпочтение — следующий ответ собирается уже по-новому, и это видно в разборе
промпта и в проверке ответа на соответствие профилю.

Как и cli.py, этот файл — только интерфейс: он вызывает публичные методы агента
и рисует результат.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid

from flask import Flask, Response, g, jsonify, render_template, request, stream_with_context

from agent import AgentError, MemoryAgent
from agent import catalog
from agent.memory.manager import ASK, AUTO, LONG, OFF, SHORT, WORKING
from agent.memory.working import STAGE_LABELS, STAGES
from agent.transitions import ПереходОтклонён
from agent import mcp
from agent import rag
from agent.rag import evaluate as rag_evaluate
from agent.rag import qa as rag_qa
from agent.rag import calibrate as rag_calibrate
from agent.rag import retrieval as rag_retrieval
from agent.rag import abstain as rag_abstain, citecheck as rag_citecheck
from agent.rag.extract import ocr_доступен

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("web")

app = Flask(__name__)
agent = MemoryAgent()

# --- демонстрационный стенд ---------------------------------------------------
# Страница задумана как местная: 127.0.0.1, никакой защиты. Но показать её
# бывает нужно тому, кто не сидит за этой машиной, — например браузерному
# расширению, которое до локального адреса не достаёт, и тогда стенд выносят
# наружу туннелем. Открытый наружу стенд — это чужой доступ к ключам провайдера
# и к чужим системам агента, поэтому появляется общий секрет: переменная
# DEMO_KEY. Пока её нет, всё работает как раньше и ни одной проверки не
# добавляется.
#
# Секрет спрашивается один раз: адрес со «?key=…» ставит куку, дальше страница
# и её запросы идут как обычно. Имя параметра и куки латиницей намеренно — это
# не поле формы, а часть адреса и заголовка.
ДЕМО_КЛЮЧ = os.getenv("DEMO_KEY", "").strip()
ДЕМО_КУКА = "demo"


@app.before_request
def _ворота_стенда():
    if not ДЕМО_КЛЮЧ:
        return None
    if request.cookies.get(ДЕМО_КУКА) == ДЕМО_КЛЮЧ:
        return None
    if request.args.get("key", "") == ДЕМО_КЛЮЧ:
        g.выдать_куку = True
        return None
    return jsonify({"error": "Стенд закрыт: откройте адрес с «?key=…»."}), 401


@app.after_request
def _запомнить_ключ(ответ):
    if getattr(g, "выдать_куку", False):
        # samesite=Lax: расширение открывает адрес переходом, и кука должна
        # доехать; httponly — чтобы её не читал скрипт страницы.
        ответ.set_cookie(ДЕМО_КУКА, ДЕМО_КЛЮЧ, httponly=True, samesite="Lax")
    return ответ

# Режим, которого у самого агента нет: запускать ли сценарий по триггеру. В
# консоли это ключ «--без-сценариев», здесь — переключатель на странице.

# Прогон сценария идёт в отдельном потоке, а страница спрашивает о ходе дела.
# Иначе один HTTP-запрос держится всё время прогона — а это от полутора минут до
# нескольких, если шаги упираются в минутный лимит провайдера или уходят на
# повтор. Пользователь в это время видит неподвижный экран и не может отличить
# работу от зависания; именно так и выглядела первая версия.
_прогон: dict = {}
_замок = threading.Lock()


def _прогон_идёт() -> bool:
    return bool(_прогон) and not _прогон.get("готово")

# Имена слоёв в том виде, в каком их присылает страница.
ИМЕНА_СЛОЁВ = {"кратко": SHORT, "рабочая": WORKING, "долго": LONG}


def _применить(данные: dict) -> None:
    """Приводит агента в состояние, выбранное на странице.

    Страница живёт дольше запроса, поэтому набор слоёв, режим маршрутизатора и
    текущая задача присылаются с каждым обращением: так после перезапуска
    сервера страница не окажется рассинхронизирована с агентом.
    """
    слои = данные.get("layers")
    if isinstance(слои, list):
        agent.set_layers({ИМЕНА_СЛОЁВ[с] for с in слои if с in ИМЕНА_СЛОЁВ})

    режим = (данные.get("router") or "").strip()
    if режим in (AUTO, ASK, OFF):
        agent.memory.router_mode = режим

    модель = (данные.get("model") or "").strip()
    agent.model_key = модель if модель in catalog.MODELS else ""

    пользователь = (данные.get("user") or "").strip()
    if пользователь and пользователь != agent.user_id:
        _сменить_пользователя(пользователь)

    if "tools" in данные:
        _переключить_инструменты(данные.get("tools"))

    задача = (данные.get("task") or "").strip()
    if задача and (agent.task is None or agent.task.task_id != задача):
        agent.use_task(задача)
    elif not задача and agent.task is not None:
        agent.drop_task()


def _переключить_инструменты(значение) -> None:
    """Включает или выключает инструменты MCP по состоянию переключателя.

    Страница присылает список серверов, когда переключатель включён, и пустоту,
    когда выключен. Соединения при выключении закрываются: stdio-сервер — это
    живой процесс, и держать его «на всякий случай» незачем.
    """
    имена = [str(и).strip() for и in значение if str(и).strip()] if isinstance(значение, list) else []
    if not имена:
        agent.выключить_инструменты()
        return
    if agent.toolbox is not None and agent._серверы_ключом == [и for и in имена if и != mcp.ВСЕ]:
        return
    # Соединения не закрываются: с Дня 20 у каждого набора серверов свой
    # инструментарий, и агент ведёт их сам — закроет все разом в close().
    agent.включить_инструменты(имена)


def _состояние() -> dict:
    """Всё, что странице нужно, чтобы нарисовать текущее положение дел.

    С Дня 28 на главной остались только задача (TaskStates) и инструменты MCP:
    профиль, сценарии, инварианты, условия, слои памяти, планировщик, конвейеры и
    оркестр ушли из веба (spec § 9), и их состояние страница больше не берёт."""
    return {
        "info": agent.info(),
        "task_state": agent.task_state(),
        "tasks": agent.tasks(),
        "stages": list(STAGES),
        "stage_labels": dict(STAGE_LABELS),
        "dialog": agent.memory.short.all(agent.session)[-20:],
    }


def _сменить_пользователя(user_id: str) -> None:
    """Пересоздаёт агента под другого пользователя.

    Профиль, сценарии и знания привязаны к user_id, а агент держит открытыми
    файлы одного из них. Подменять путь на лету значило бы оставить половину
    ссылок на прежнего пользователя, поэтому агент создаётся заново.
    """
    global agent
    прежний = agent
    agent = MemoryAgent(
        user_id=user_id, session=прежний.session, layers=set(прежний.layers),
        router_mode=прежний.memory.router_mode, model_key=прежний.model_key,
        soft_check=прежний.soft_check, follow_profile=прежний.follow_profile,
    )
    прежний.close()


@app.after_request
def _не_кэшировать(ответ):
    """Страница меняется вместе с кодом, и старая в кэше выглядит как поломка.

    Именно так и вышло: сервер перезапустили, а браузер показывал прежнюю
    страницу — со старым, синхронным запуском сценария.
    """
    ответ.headers["Cache-Control"] = "no-store, must-revalidate"
    return ответ


@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index.html",
        info=agent.info(),
        models=catalog.describe(),
        roles=catalog.describe_roles(),
        stages=list(STAGES),
    )


@app.get("/api/state")
def api_state():
    """Состояние всех слоёв — этим страница и оживает при открытии."""
    return jsonify(_состояние())


@app.post("/api/ask")
def api_ask():
    if _прогон_идёт():
        return jsonify({"error": "Сейчас идёт сценарий — дождитесь его конца."}), 409
    данные = request.get_json(silent=True) or {}
    вопрос = (данные.get("question") or "").strip()
    if not вопрос:
        return jsonify({"error": "Пустой вопрос."}), 400
    if данные.get("stream"):
        return _вопрос_потоком(данные, вопрос)
    try:
        _применить(данные)
        ответ = agent.ask(вопрос)
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"answer": ответ.to_dict(), "state": _состояние()})


def _вопрос_потоком(данные: dict, вопрос: str) -> Response:
    """Вопрос агенту потоком NDJSON (День 27): круги инструментов, вызовы,
    счётчик токенов, в конце — {"событие": "готово", "answer", "state"}."""
    import json as _json
    import queue
    from agent.local import offline, progress
    очередь: queue.Queue = queue.Queue()

    def отвечать() -> None:
        метка = progress.наблюдать(очередь.put)
        try:
            _применить(данные)
            ответ = agent.ask(вопрос)
            очередь.put({"событие": "готово", "answer": ответ.to_dict(), "state": _состояние(),
                         "net": offline.журнал(0)})
        except AgentError as exc:
            очередь.put({"событие": "ошибка", "error": str(exc)})
        except Exception as exc:   # noqa: BLE001 — поток не должен умереть молча
            очередь.put({"событие": "ошибка", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            progress.снять(метка)
            очередь.put(None)

    threading.Thread(target=отвечать, daemon=True).start()

    def строки():
        while (событие := очередь.get()) is not None:
            yield _json.dumps(событие, ensure_ascii=False, default=str) + "\n"

    return Response(stream_with_context(строки()), mimetype="application/x-ndjson")


@app.post("/api/plan")
def api_plan():
    данные = request.get_json(silent=True) or {}
    try:
        _применить(данные)
        ответ = agent.plan()
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"answer": ответ.to_dict(), "state": _состояние()})


@app.post("/api/task")
def api_task():
    """Заводит задачу, берёт существующую, меняет стадию, ставит на паузу."""
    данные = request.get_json(silent=True) or {}
    действие = (данные.get("action") or "").strip()

    # Пауза — единственное действие, разрешённое во время прогона, и это не
    # поблажка, а смысл кнопки: остановить то, что идёт прямо сейчас. Флаг
    # ставится на том же состоянии задачи, с которым работает исполнитель, и
    # тот увидит его перед следующим шагом.
    if действие == "пауза":
        try:
            задача = agent.pause_task()
        except AgentError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"paused": задача.состояние_словами, "state": _состояние()})

    if _прогон_идёт():
        return jsonify({"error": "Сейчас идёт сценарий: он ведёт свою задачу."}), 409
    try:
        if действие == "создать":
            agent.start_task((данные.get("task_id") or "").strip(),
                             (данные.get("title") or "").strip())
        elif действие == "взять":
            agent.use_task((данные.get("task_id") or "").strip())
        elif действие == "стадия":
            agent.transition((данные.get("stage") or "").strip(),
                             (данные.get("note") or "").strip())
        elif действие == "закрыть-шаг":
            agent.close_step((данные.get("value") or "").strip())
        elif действие == "утвердить-план":
            подпись = agent.approve_plan((данные.get("кем") or "").strip(),
                                         (данные.get("пояснение") or "").strip())
            return jsonify({"approved": подпись, "state": _состояние()})
        elif действие == "снять-утверждение":
            agent.unapprove_plan((данные.get("почему") or "").strip())
        elif действие == "валидация":
            отчёт = agent.validate_task(ревизор=bool(данные.get("ревизор", True)))
            return jsonify({"report": отчёт, "state": _состояние()})
        elif действие == "шаг":
            agent.remember_step((данные.get("key") or "").strip(),
                                (данные.get("value") or "").strip())
        elif действие == "завершить":
            запись = agent.finish_task((данные.get("note") or "").strip())
            return jsonify({"decision": запись, "state": _состояние()})
        elif действие == "продолжить":
            agent.continue_task()
        elif действие == "ответ":
            agent.answer_task((данные.get("text") or "").strip())
        elif действие == "отпустить":
            agent.drop_task()
        else:
            return jsonify({"error": f"Неизвестное действие «{действие}»."}), 400
    except ПереходОтклонён as отклонено:
        # Отказ в переходе — не ошибка интерфейса, а содержательный ответ, и
        # страница показывает его так же, как отказ по инварианту: правилом,
        # обоснованием и тем, что нужно сделать, чтобы переход открылся.
        return jsonify({"error": str(отклонено), "refusal": отклонено.отказ.to_dict(),
                        "state": _состояние()}), 400
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"state": _состояние()})


# День 28: ручки профиля, сценариев, инвариантов, условий перехода и «Записать вручную»
# убраны из веба вместе с их панелями (spec § 9) — в консоли всё это осталось.


@app.get("/api/verify")
def api_verify():
    """Проверка каталога: отвечают ли сегодня модели, на которые он указывает."""
    return jsonify({"models": catalog.verify()})


@app.post("/api/forget")
def api_forget():
    """Очищает краткосрочную память. Остальные слои не трогает — это разные слои."""
    стёрто = agent.memory.short.clear(agent.session)
    return jsonify({"cleared": стёрто, "state": _состояние()})


# --- MCP ---------------------------------------------------------------------------
# Агент в этих ручках не участвует: серверы и их инструменты — отдельный слой, и
# список получают у самих серверов. Файл серверов перечитывается на каждый
# запрос: его правят руками, и перезапускать ради этого страницу незачем.

@app.get("/api/mcp")
def api_mcp():
    """Серверы из mcp-servers.json — без подключения, мгновенно."""
    try:
        реестр = mcp.Registry()
    except mcp.MCPConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"path": реестр.path, "servers": [с.to_dict() for с in реестр.servers]})


@app.post("/api/mcp/inspect")
def api_mcp_inspect():
    """Подключиться к серверу (или ко всем) и получить список инструментов.

    Сбой соединения — не ошибка запроса: он приходит внутри осмотра, с этапом,
    причиной и подсказкой, и страница показывает его у своего сервера.
    """
    данные = request.get_json(silent=True) or {}
    имя = (данные.get("server") or "").strip()
    try:
        реестр = mcp.Registry()
        осмотры = реестр.inspect([имя] if имя else None)
    except mcp.MCPConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "path": реестр.path,
        "inspections": [о.to_dict() for о in осмотры],
        "summary": mcp.summary(осмотры),
    })


@app.post("/api/tools")
def api_tools():
    """Инструменты, которые получит модель, и во что обходится их список."""
    данные = request.get_json(silent=True) or {}
    try:
        _переключить_инструменты(данные.get("tools"))
        if agent.toolbox is None:
            return jsonify({"enabled": False, "tools": [], "summary": {}})
        сводка = agent.подключить_инструменты()
        инструменты = [и.to_dict() for и in agent.toolbox.инструменты]
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"enabled": True, "tools": инструменты, "summary": сводка,
                    "confirm_writes": agent.confirm_writes})


@app.post("/api/mcp/call")
def api_mcp_call():
    """Вызов инструмента человеком со страницы — без модели и без заявки."""
    данные = request.get_json(silent=True) or {}
    имя = (данные.get("tool") or "").strip()
    аргументы = данные.get("args")
    if not имя:
        return jsonify({"error": "Не указан инструмент."}), 400
    if аргументы is not None and not isinstance(аргументы, dict):
        return jsonify({"error": "Аргументы должны быть объектом JSON."}), 400
    try:
        результат = agent.call_tool(имя, аргументы or {})
    except AgentError as exc:
        # Нет такого инструмента, сервер не поднялся, фильтр закрыл — всё это
        # ошибка запроса, а не поломка: страница показывает её текстом.
        return jsonify({"error": str(exc)}), 400
    return jsonify({"result": результат.to_dict()})


@app.get("/api/calls")
def api_calls():
    """Заявки на изменяющие вызовы, ждущие решения человека."""
    return jsonify({"calls": agent.pending_calls()})


@app.post("/api/calls")
def api_calls_resolve():
    """Подтвердить заявку (и выполнить вызов) или отклонить её."""
    данные = request.get_json(silent=True) or {}
    try:
        номер = int(данные.get("номер") or 0)
    except (TypeError, ValueError):
        номер = 0
    действие = (данные.get("действие") or "").strip()
    if not номер:
        return jsonify({"error": "Не указан номер заявки."}), 400
    try:
        if действие == "отклонить":
            решена = agent.reject_call(номер, (данные.get("почему") or "").strip())
            return jsonify({"rejected": решена, "calls": agent.pending_calls(),
                            "state": _состояние()})
        if действие == "подтвердить":
            результат = agent.confirm_call(номер)
            return jsonify({"result": результат.to_dict(), "calls": agent.pending_calls(),
                            "state": _состояние()})
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"error": "Действие: «подтвердить» или «отклонить»."}), 400


# День 28: ручки планировщика, конвейеров и оркестра убраны из веба вместе с
# панелями (spec § 9) — в консоли и в своих MCP-серверах всё это осталось.


# --- индекс документов (День 21) ----------------------------------------------
# Сборка индекса на CPU идёт минуты: OCR сканов и эмбеддинги сотен чанков.
# Поэтому, как прогон сценария, она живёт в своём потоке, а страница раз в
# секунду спрашивает ход дела. Сравнение стратегий идёт тем же путём: ему нужен
# эмбеддер, а первый запрос к Ollama поднимает модель в память секунды.
# Работа с индексом одна на весь сервер — вторая сборка поверх первой
# перетёрла бы индекс на середине.
_индексация: dict = {}
_ЗАМОК_ИНДЕКСА = threading.Lock()


def _индексатор(данные: dict | None = None) -> "rag.Индексатор":
    данные = данные or {}
    return rag.Индексатор(эмбеддер=(данные.get("embedder") or "").strip(),
                          ocr=bool(данные.get("ocr", True)))


def _число(данные: dict, ключ: str, по_умолчанию: int, мин: int, макс: int) -> int:
    try:
        значение = int(данные.get(ключ) or по_умолчанию)
    except (TypeError, ValueError):
        raise ValueError(f"«{ключ}» — целое число")
    if not мин <= значение <= макс:
        raise ValueError(f"«{ключ}» должно быть от {мин} до {макс}")
    return значение


def _состояние_индекса() -> dict:
    настройки = rag.настройки_по_умолчанию()
    хранилище = rag.ХранилищеИндекса(настройки["база"])
    индексы = хранилище.индексы()
    документы: dict[str, dict] = {}
    for и in индексы:
        for д in хранилище.документы(и["стратегия"]):
            запись = документы.setdefault(д["источник"], {
                "источник": д["источник"], "название": д["название"], "вид": д["вид"],
                "страниц": д["страниц"], "символов": д["символов"],
                "распознано": д["распознано"], "замечания": д["замечания"], "чанков": {}})
            запись["чанков"][и["стратегия"]] = д["чанков"]
    можно_ocr, почему = ocr_доступен()
    try:
        эмбеддер = rag.создать_эмбеддер(настройки["эмбеддер"])
        беда = эмбеддер.проверить()
        имя = эмбеддер.имя
    except rag.ОшибкаЭмбеддера as exc:
        беда, имя = str(exc), настройки["эмбеддер"]
    return {
        "каталог": настройки["документы"], "база": настройки["база"],
        "индексы": индексы, "документы": list(документы.values()),
        "эмбеддер": {"имя": имя, "готов": not беда, "почему": беда},
        "ocr": {"готов": можно_ocr, "почему": почему},
        "стратегии": list(rag.СТРАТЕГИИ),
        "по_умолчанию": {"размер": rag.chunking.РАЗМЕР, "перекрытие": rag.chunking.ПЕРЕКРЫТИЕ,
                         "макс": rag.chunking.МАКС},
        "сравнение": rag_evaluate.прочитать(настройки["индекс"]),
        "контроль": rag_qa.прочитать(настройки["индекс"]),
        "подбор": rag_calibrate.прочитать(настройки["индекс"]),
        "режимы_поиска": {и: н.to_dict() for и, н in rag_retrieval.режимы().items()},
        "настройки_поиска": rag_retrieval.прочитать_настройки()["источник"],
        # День 24: ответ с цитатами, порог «не знаю» и проверка на вопросах.
        "цитаты": rag_citecheck.прочитать(настройки["индекс"]),
        "порог_уверенности": rag_retrieval.прочитать_настройки()["порог_уверенности"],
        "уверенность": _подбор_уверенности(),
        "работа": dict(_индексация) if _индексация else None,
    }


def _подбор_уверенности() -> dict | None:
    """Сетка подбора порога «не знаю» из rag-settings.json (abstain.записать)."""
    путь = rag_retrieval.путь_настроек()
    if not os.path.exists(путь):
        return None
    import json
    with open(путь, encoding="utf-8") as f:
        return json.load(f).get("уверенность")


def _в_фоне(вид: str, тело) -> None:
    """Запустить работу с индексом в потоке; замок уже взят вызывающим."""
    _индексация.clear()
    _индексация.update({"id": uuid.uuid4().hex[:12], "вид": вид, "этап": "", "сделано": 0,
                        "всего": 0, "что": "", "готово": False, "ошибка": "", "итог": None})

    def ход(этап: str, сделано: int, всего: int, что: str) -> None:
        _индексация.update({"этап": этап, "сделано": сделано, "всего": всего, "что": что})

    def прогнать() -> None:
        try:
            _индексация["итог"] = тело(ход)
        except Exception as exc:                     # сеть, Ollama, OCR — всё в состояние
            log.exception("Работа с индексом прервалась")
            _индексация["ошибка"] = str(exc)
        finally:
            _индексация["готово"] = True
            _ЗАМОК_ИНДЕКСА.release()

    threading.Thread(target=прогнать, daemon=True).start()


@app.get("/rag")
def rag_page():
    from agent.rag.answer import РОЛЬ_МОДЕЛИ
    from agent.rag import localbench
    try:
        набор = [{"id": в.get("id", ""), "вид": в.get("вид", ""), "вопрос": в["вопрос"]} for в in localbench.вопросы()]
    except (OSError, ValueError):
        набор = []
    return render_template("rag.html", models=catalog.describe(),
                           rag_model=catalog.for_role(РОЛЬ_МОДЕЛИ, 0), набор=набор,
                           участники=[у.to_dict() for у in localbench.УЧАСТНИКИ])


@app.get("/api/rag")
def api_rag():
    """Что собрано, что идёт сейчас и последнее сравнение. Страница опрашивает раз в секунду."""
    return jsonify(_состояние_индекса())


@app.post("/api/rag/build")
def api_rag_build():
    данные = request.get_json(silent=True) or {}
    стратегии = [с for с in (данные.get("strategies") or rag.СТРАТЕГИИ) if с in rag.СТРАТЕГИИ]
    if not стратегии:
        return jsonify({"error": f"Стратегии: {', '.join(rag.СТРАТЕГИИ)}"}), 400
    try:
        размер = _число(данные, "size", rag.chunking.РАЗМЕР, 50, 4000)
        перекрытие = _число(данные, "overlap", rag.chunking.ПЕРЕКРЫТИЕ, 0, 2000)
        макс = _число(данные, "max", rag.chunking.МАКС, 100, 4000)
        if перекрытие >= размер:
            raise ValueError("Перекрытие должно быть меньше размера чанка")
        индексатор = _индексатор(данные)
    except (ValueError, rag.ОшибкаЭмбеддера) as exc:
        return jsonify({"error": str(exc)}), 400
    беда = индексатор.эмбеддер.проверить()
    if беда:
        return jsonify({"error": беда}), 400
    if not _ЗАМОК_ИНДЕКСА.acquire(blocking=False):
        return jsonify({"error": "С индексом уже идёт работа — дождитесь её конца."}), 409
    _в_фоне("сборка", lambda ход: индексатор.построить(стратегии, размер, перекрытие, макс,
                                                       прогресс=ход))
    return jsonify({"work": dict(_индексация)}), 202


@app.post("/api/rag/compare")
def api_rag_compare():
    данные = request.get_json(silent=True) or {}
    try:
        индексатор = _индексатор(данные)
    except rag.ОшибкаЭмбеддера as exc:
        return jsonify({"error": str(exc)}), 400
    if not индексатор.хранилище.индексы():
        return jsonify({"error": "Индекс пуст — сначала соберите его."}), 400
    if not _ЗАМОК_ИНДЕКСА.acquire(blocking=False):
        return jsonify({"error": "С индексом уже идёт работа — дождитесь её конца."}), 409

    def сравнить(ход):
        отчёт = rag_evaluate.сравнить(индексатор, прогресс=ход)
        rag_evaluate.записать(отчёт, os.path.dirname(индексатор.база))
        return {"лучшая": rag_evaluate.лучшая(отчёт)}

    _в_фоне("сравнение", сравнить)
    return jsonify({"work": dict(_индексация)}), 202


@app.post("/api/rag/search")
def api_rag_search():
    данные = request.get_json(silent=True) or {}
    запрос = (данные.get("query") or "").strip()
    if not запрос:
        return jsonify({"error": "Пустой запрос."}), 400
    стратегия = (данные.get("strategy") or "обе").strip()
    try:
        сколько = _число(данные, "limit", 5, 1, 20)
        индексатор = _индексатор(данные)
        if стратегия == "обе":
            итог = индексатор.найти_везде(запрос, сколько)
        elif стратегия in rag.СТРАТЕГИИ:
            итог = {стратегия: индексатор.найти(запрос, стратегия, сколько)}
        else:
            return jsonify({"error": f"Нет стратегии «{стратегия}»."}), 400
    except (ValueError, rag.ОшибкаИндекса, rag.ОшибкаЭмбеддера) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"query": запрос, "embedder": индексатор.эмбеддер.имя, "results": {
        с: [н.кратко(600) for н in находки] for с, находки in итог.items()}})


@app.get("/api/rag/chunk/<chunk_id>")
def api_rag_chunk(chunk_id: str):
    хранилище = rag.ХранилищеИндекса(rag.настройки_по_умолчанию()["база"])
    чанк = хранилище.чанк(chunk_id)
    if чанк is None:
        return jsonify({"error": f"Фрагмента {chunk_id} нет."}), 404
    до, после = хранилище.соседи(чанк)
    return jsonify({"chunk": {**чанк.паспорт(), "text": чанк.текст},
                    "previous": {**до.паспорт(), "text": до.текст} if до else None,
                    "next": {**после.паспорт(), "text": после.текст} if после else None})


# --- вопрос по документам: RAG-запрос (День 22) ---------------------------------
# Ответ идёт через того же агента, что и в консоли (agent.спросить_документы):
# страница — только интерфейс. Модель берётся из шапки страницы, как у обычного
# вопроса, — иначе выбранная человеком модель до RAG-запроса не доезжала бы.

@app.post("/api/rag/ask")
def api_rag_ask():
    данные = request.get_json(silent=True) or {}
    вопрос = (данные.get("question") or "").strip()
    режим = (данные.get("mode") or "оба").strip()
    поиск = (данные.get("search") or "").strip()
    if not вопрос:
        return jsonify({"error": "Пустой вопрос."}), 400
    if режим not in ("оба", "rag", "без", "поиск"):
        return jsonify({"error": "Режим: оба, rag, без или поиск (все режимы поиска)."}), 400
    if поиск and поиск not in rag_retrieval.РЕЖИМЫ_ПОИСКА:
        return jsonify({"error": f"Поиск: {', '.join(rag_retrieval.РЕЖИМЫ_ПОИСКА)}."}), 400
    try:
        сколько = _число(данные, "limit", 5, 1, 10)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    стратегия = данные.get("strategy") if данные.get("strategy") in rag.СТРАТЕГИИ else "структура"
    модель = (данные.get("model") or "").strip()
    прежняя = agent.model_key
    agent.model_key = модель if модель in catalog.MODELS else ""
    try:
        if режим == "поиск":
            ответы = agent.сравнить_поиск(вопрос, стратегия=стратегия)
        elif режим == "оба" and поиск:
            ответы = {"без": agent.спросить_документы(вопрос, "без", стратегия, сколько),
                      "rag": agent.спросить_документы(вопрос, "rag", стратегия, сколько, поиск=поиск)}
        elif режим == "оба":
            ответы = agent.сравнить_режимы(вопрос, стратегия, сколько)
        else:
            ответы = {режим: agent.спросить_документы(вопрос, режим, стратегия, сколько,
                                                     поиск=поиск if режим == "rag" else "")}
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        agent.model_key = прежняя
    return jsonify({"question": вопрос, "answers": {к: о.to_dict() for к, о in ответы.items()}})


@app.post("/api/rag/qa")
def api_rag_qa():
    """Контрольные вопросы в обоих режимах — в фоне, как сборка и сравнение."""
    данные = request.get_json(silent=True) or {}
    try:
        вопросы = rag_qa.загрузить(rag_qa.путь_по_умолчанию())
    except (OSError, ValueError) as exc:
        return jsonify({"error": f"Контрольные вопросы не прочитаны: {exc}"}), 400
    модель = (данные.get("model") or "").strip()
    модель = модель if модель in catalog.MODELS else ""
    судья = bool(данные.get("judge", True))
    режимы = данные.get("modes") or ["без", "база", "фильтр", "rewrite", "полный"]
    if not isinstance(режимы, list) or any(р not in rag_qa.ИМЕНА_РЕЖИМОВ for р in режимы):
        return jsonify({"error": f"Режимы: {', '.join(rag_qa.ИМЕНА_РЕЖИМОВ)}."}), 400
    if not _ЗАМОК_ИНДЕКСА.acquire(blocking=False):
        return jsonify({"error": "С индексом уже идёт работа — дождитесь её конца."}), 409

    def прогнать(ход):
        отвечатель = agent._отвечатель()
        прогон = rag_qa.прогнать(отвечатель, вопросы, судья, модель, прогресс=ход,
                                 режимы=режимы)
        rag_qa.записать(прогон, os.path.dirname(отвечатель.индексатор.база))
        return {"итог": прогон.to_dict()["totals"]}

    _в_фоне("контроль", прогнать)
    return jsonify({"work": dict(_индексация)}), 202


# --- второй этап поиска (День 23) ------------------------------------------------

@app.post("/api/rag/select")
def api_rag_select():
    """Только поиск в два этапа: запросы, кандидаты, оценки, что отсечено и почему.

    Поправки режима (реранкер, k_до, k_после, порог, rewrite) приходят со
    страницы — так порог и top-K настраиваются вживую, не трогая подбор.
    """
    данные = request.get_json(silent=True) or {}
    вопрос = (данные.get("question") or "").strip()
    поиск = (данные.get("search") or "полный").strip()
    if not вопрос:
        return jsonify({"error": "Пустой вопрос."}), 400
    if поиск not in rag_retrieval.РЕЖИМЫ_ПОИСКА:
        return jsonify({"error": f"Поиск: {', '.join(rag_retrieval.РЕЖИМЫ_ПОИСКА)}."}), 400
    поправки: dict = {}
    try:
        if "reranker" in данные:
            р = (данные.get("reranker") or "").strip()
            if р not in ("", *rag.rerank_names()):
                raise ValueError(f"Реранкер: {', '.join(rag.rerank_names())} или пусто")
            поправки["реранкер"] = р
        if данные.get("k_before"):
            поправки["k_до"] = _число(данные, "k_before", 20, 1, 50)
        if данные.get("k_after"):
            поправки["k_после"] = _число(данные, "k_after", 5, 1, 20)
        if данные.get("cutoff") not in (None, ""):
            порог = float(данные["cutoff"])
            if not 0 <= порог <= 1:
                raise ValueError("Порог отсечения — от 0 до 1")
            поправки["порог"] = порог
        if "rewrite" in данные:
            поправки["rewrite"] = bool(данные["rewrite"])
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    модель = (данные.get("model") or "").strip()
    прежняя = agent.model_key
    agent.model_key = модель if модель in catalog.MODELS else ""
    try:
        отбор = agent.отбор(вопрос, поиск, настройки=поправки or None)
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        agent.model_key = прежняя
    return jsonify({"selection": отбор.to_dict()})


@app.post("/api/rag/calibrate")
def api_rag_calibrate():
    """Подбор реранкера, порога и top-K — в фоне: с кросс-энкодером это минуты."""
    if not _ЗАМОК_ИНДЕКСА.acquire(blocking=False):
        return jsonify({"error": "С индексом уже идёт работа — дождитесь её конца."}), 409

    def подобрать(ход):
        поисковик = agent._отвечатель().поисковик
        итог = rag_calibrate.подобрать(поисковик, rag_evaluate.путь_вопросов_по_умолчанию(),
                                       прогресс=ход)
        rag_calibrate.записать(итог, os.path.dirname(поисковик.индексатор.база),
                               rag_retrieval.путь_настроек())
        return {"выбрано": итог["выбрано"]}

    _в_фоне("подбор", подобрать)
    return jsonify({"work": dict(_индексация)}), 202


# --- ответ с источниками и цитатами (День 24) -------------------------------------

@app.post("/api/rag/cited")
def api_rag_cited():
    """Ответ с обязательными источниками и цитатами; «не знаю» при слабом контексте."""
    данные = request.get_json(silent=True) or {}
    вопрос = (данные.get("question") or "").strip()
    поиск = (данные.get("search") or "фильтр").strip()
    формат = (данные.get("format") or "цитаты").strip()
    if not вопрос:
        return jsonify({"error": "Пустой вопрос."}), 400
    if поиск not in rag_retrieval.РЕЖИМЫ_ПОИСКА:
        return jsonify({"error": f"Поиск: {', '.join(rag_retrieval.РЕЖИМЫ_ПОИСКА)}."}), 400
    if формат not in ("цитаты", "текст"):
        return jsonify({"error": "Формат: цитаты или текст."}), 400
    порог = None
    if данные.get("confidence") not in (None, ""):
        try:
            порог = float(данные["confidence"])
        except (TypeError, ValueError):
            return jsonify({"error": "Порог уверенности — число от 0 до 1."}), 400
        if not 0 <= порог <= 1:
            return jsonify({"error": "Порог уверенности — число от 0 до 1."}), 400
    модель = (данные.get("model") or "").strip()
    if (данные.get("profile") or "") == "локальный":
        # День 28: локальный RAG — поиск без LLM, ответ локальной модели, ход по этапам.
        from agent.local import offline
        if модель and (модель not in catalog.MODELS or not catalog.get(модель).local):
            return jsonify({"error": f"Локальный RAG отвечает только локальной моделью (loc-…), "
                                     f"а «{модель}» — облачная."}), 400
        # День 29: по умолчанию — модель роли «ответ-по-документам» из карты (как в консоли):
        # при «основная» — основная, после «Собрать «после»» — оптимизированная loc-rag-docs.
        from agent.rag.answer import РОЛЬ_МОДЕЛИ
        модель = модель or offline.модель_роли(РОЛЬ_МОДЕЛИ)

        def локально() -> dict:
            from agent.rag import profile as rag_profile
            отвечатель = agent._отвечатель()
            профиль = rag_profile.локальный(отвечатель.поисковик)
            ответ = отвечатель.ответить(вопрос, "rag", модель, формат=формат, профиль=профиль, ход=True,
                                        порог_уверенности=порог)
            return {"question": вопрос, "answer": ответ.to_dict(), "profile": профиль.to_dict()}

        if данные.get("stream"):
            return _rag_потоком(локально)
        try:
            return jsonify(локально())
        except (AgentError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
    прежняя = agent.model_key
    agent.model_key = модель if модель in catalog.MODELS else ""
    try:
        ответ = agent.спросить_документы(вопрос, "rag", поиск=поиск, формат=формат,
                                         порог_уверенности=порог)
    except AgentError as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        agent.model_key = прежняя
    return jsonify({"question": вопрос, "answer": ответ.to_dict()})


def _rag_потоком(работа) -> Response:
    """Долгий ответ RAG потоком NDJSON (День 28): этапы и токены из progress, в
    конце — {"событие": "готово", …итог работы, "net": сеть за работу}. Работа
    идёт в своём потоке, наблюдатель стоит только в нём (как у чата Дня 27)."""
    import json as _json
    import queue
    from agent.local import offline, progress
    очередь: queue.Queue = queue.Queue()

    def идти() -> None:
        метка = progress.наблюдать(очередь.put)
        до = offline.снимок_журнала()
        try:
            итог = работа()
            очередь.put({"событие": "готово", **итог, "net": offline.разница(до, offline.снимок_журнала()),
                         "journal": offline.журнал(0)})
        except (AgentError, ValueError) as exc:
            очередь.put({"событие": "ошибка", "error": str(exc)})
        except Exception as exc:   # noqa: BLE001 — поток не должен умереть молча
            очередь.put({"событие": "ошибка", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            progress.снять(метка)
            очередь.put(None)

    threading.Thread(target=идти, daemon=True).start()

    def строки():
        while (событие := очередь.get()) is not None:
            yield _json.dumps(событие, ensure_ascii=False, default=str) + "\n"

    return Response(stream_with_context(строки()), mimetype="application/x-ndjson")


# --- День 28: локальный RAG рядом с облаком ------------------------------------------

_СРАВНЕНИЕ: dict = {"поток": None, "стоп": None, "ошибка": ""}
_ЗАМОК_СРАВНЕНИЯ = threading.Lock()


def _каталог_сравнения() -> str:
    from agent.rag import localbench
    return os.path.join(os.path.dirname(agent._отвечатель().индексатор.база), localbench.КАТАЛОГ)


def _вопрос_rag(данные: dict) -> dict:
    from agent.rag import localbench
    текст = (данные.get("question") or "").strip()
    по_id = {в.get("id"): в for в in localbench.вопросы()}
    if текст in по_id:
        return по_id[текст]
    return {"id": "свой", "вид": "с ответом", "вопрос": текст, "факты": [], "источники": []}


@app.post("/api/rag/side")
def api_rag_side():
    """Один вопрос — участники рядом: локальные 4B и 1.7B, облако на том же поиске и
    на лучшем конвейере Дня 24 (FR-09). stream — потоком, с этапами и токенами."""
    from agent.local import offline
    from agent.rag import localbench
    данные = request.get_json(silent=True) or {}
    вопрос = _вопрос_rag(данные)
    if not вопрос["вопрос"]:
        return jsonify({"error": "Пустой вопрос."}), 400
    try:
        список = localbench.участники(данные.get("participants") or None)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if offline.включён() and any(not catalog.get(у.модель).local for у in список):
        return jsonify({"error": "Сравнение с облаком идёт с выключенным офлайном: облачные участники "
                                 "ходят в сеть. Локальные участники и так работают только на этой машине — "
                                 "это видно по счётчику внешних запросов у каждой колонки."}), 400
    судья = "" if данные.get("judge") is False else localbench.СУДЬЯ

    def рядом() -> dict:
        записи = localbench.рядом(agent._отвечатель(), вопрос, список, судья=судья, ход=True)
        отметки = localbench.отметки(agent.memory.base_dir)
        return {"question": вопрос, "records": записи,
                "participants": [у.to_dict() for у in список],
                "marks": {f"{в}|{у}": з["mark"] for (в, у), з in отметки.items()}}

    if данные.get("stream"):
        return _rag_потоком(рядом)
    try:
        return jsonify(рядом())
    except (AgentError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


def _состояние_сравнения() -> dict:
    from agent.rag import localbench
    каталог = _каталог_сравнения()
    итог = localbench.прочитать(os.path.dirname(каталог)) or {}
    отметки = localbench.отметки(agent.memory.base_dir)
    поток = _СРАВНЕНИЕ["поток"]
    return {"running": bool(поток and поток.is_alive()), "error": _СРАВНЕНИЕ["ошибка"],
            "state": localbench.прочитать_состояние(каталог), "summary": итог.get("сводка"),
            "conditions": итог.get("условия"), "report": localbench.ФАЙЛ_ОТЧЁТА if итог else "",
            "participants": [у.to_dict() for у in localbench.УЧАСТНИКИ],
            "marks": {f"{в}|{у}": з["mark"] for (в, у), з in отметки.items()}}


@app.get("/api/rag/localbench")
def api_rag_localbench_state():
    return jsonify(_состояние_сравнения())


@app.post("/api/rag/localbench")
def api_rag_localbench():
    """Прогон сравнения в фоне (FR-11): start — начать или продолжить, stop — остановить
    между ответами. Ход — в state.json, тот же, что у консоли."""
    from agent.rag import localbench
    данные = request.get_json(silent=True) or {}
    действие = (данные.get("action") or "start").strip()
    if действие == "stop":
        if _СРАВНЕНИЕ["стоп"] is not None:
            _СРАВНЕНИЕ["стоп"].set()
        return jsonify(_состояние_сравнения())
    if действие != "start":
        return jsonify({"error": "action — start или stop."}), 400
    if not _ЗАМОК_СРАВНЕНИЯ.acquire(blocking=False):
        return jsonify({"error": "Прогон сравнения уже идёт.", **_состояние_сравнения()}), 409
    try:
        каталог = _каталог_сравнения()
        прежнее = localbench.прочитать_состояние(каталог) or {}
        метка = (данные.get("label") or "").strip() or (
            прежнее.get("метка") if прежнее and not прежнее.get("завершён") else "")
        продолжение = bool(метка and метка == прежнее.get("метка"))
        список = localbench.участники(данные.get("participants") or (прежнее.get("участники") if продолжение else None))
        повторов = int(данные.get("repeats") or (прежнее.get("повторов") if продолжение else 0) or localbench.ПОВТОРОВ)
        ids = данные.get("questions") or (прежнее.get("вопросы_ids") if продолжение else None) or None
        судья = "" if данные.get("judge") is False else localbench.СУДЬЯ
        прогон = localbench.Прогон(каталог, метка, localbench.вопросы(ids=ids), список, повторов, судья)
    except (ValueError, TypeError) as exc:
        _ЗАМОК_СРАВНЕНИЯ.release()
        return jsonify({"error": str(exc)}), 400
    стоп = threading.Event()
    _СРАВНЕНИЕ.update({"стоп": стоп, "ошибка": ""})

    def прогнать() -> None:
        try:
            отвечатель = agent._отвечатель()
            прогон.выполнить(отвечатель, agent.client, стоп=стоп.is_set)
            записи = прогон.записи()
            св = localbench.сводка(записи, прогон.вопросы, прогон.участники, прогон.повторов)
            localbench.записать(os.path.dirname(каталог), св, записи, localbench.условия(отвечатель))
        except Exception as exc:   # noqa: BLE001 — ошибка видна на странице
            _СРАВНЕНИЕ["ошибка"] = f"{type(exc).__name__}: {exc}"
        finally:
            _ЗАМОК_СРАВНЕНИЯ.release()

    поток = threading.Thread(target=прогнать, daemon=True)
    _СРАВНЕНИЕ["поток"] = поток
    поток.start()
    return jsonify({"started": True, "label": прогон.метка, "total": прогон.всего, **_состояние_сравнения()})


# --- День 29: оптимизация локальной LLM под задачу -----------------------------------

_ОПТИМИЗАЦИЯ: dict = {"поток": None, "стоп": None, "ошибка": "", "вид": ""}
# Одна 4B-модель в памяти и честные замеры: «рядом» и фоновый прогон не идут вместе.
_ЗАМОК_ОПТИМИЗАЦИИ = threading.Lock()


def _каталоги_оптимизации() -> tuple[str, str]:
    from agent.rag import optbench
    индекс = os.path.dirname(agent._отвечатель().индексатор.база)
    return индекс, os.path.join(индекс, optbench.КАТАЛОГ)


def _состояние_оптимизации() -> dict:
    from agent.local import server2, tuning
    from agent.rag import localbench, optbench
    индекс, каталог = _каталоги_оптимизации()
    try:
        нет = optbench.недостающие(tuning.загрузить())
    except Exception as exc:   # noqa: BLE001 — Ollama молчит или файл с ошибкой: страница всё равно открывается
        нет = [f"не проверить: {exc}"]
    import json as _json
    отчёт = {}
    try:
        with open(os.path.join(индекс, optbench.ФАЙЛ_ИТОГА), encoding="utf-8") as f:
            отчёт = _json.load(f)
    except (OSError, ValueError):
        pass
    поток = _ОПТИМИЗАЦИЯ["поток"]
    ход = optbench.прочитать_состояние(каталог)
    снаружи = optbench.идёт_в_другом_процессе(ход)
    return {"tuning": tuning.описание(), "server2": server2.состояние(), "missing": нет,
            "state": ход, "running": bool(поток and поток.is_alive()) or снаружи, "outside": снаружи,
            "busy": _ЗАМОК_ОПТИМИЗАЦИИ.locked() or снаружи, "kind": _ОПТИМИЗАЦИЯ["вид"],
            "error": _ОПТИМИЗАЦИЯ["ошибка"],
            "report": {к: отчёт.get(к) for к in ("скрининг", "качество", "решение", "gpu", "условия", "микро")},
            "questions": [{"id": в.get("id"), "question": в.get("вопрос"), "kind": в.get("вид")}
                          for в in localbench.вопросы()],
            "screening": list(optbench.СКРИНИНГ), "quality": list(optbench.КАЧЕСТВО)}


@app.get("/api/opt")
def api_opt():
    """Конфигурации, второй сервер, веса в Ollama, ход прогона, последний отчёт."""
    return jsonify(_состояние_оптимизации())


@app.post("/api/opt/server2")
def api_opt_server2():
    from agent.local import server2
    данные = request.get_json(silent=True) or {}
    действие = (данные.get("action") or "status").strip()
    try:
        if действие == "start":
            if _ЗАМОК_ОПТИМИЗАЦИИ.locked():
                return jsonify({"error": "Идёт прогон или ответ «рядом» — второй сервер управляется им."}), 409
            итог = server2.старт({str(к): str(з) for к, з in (данные.get("vars") or {}).items()})
        elif действие == "stop":
            if _ЗАМОК_ОПТИМИЗАЦИИ.locked():
                return jsonify({"error": "Идёт прогон или ответ «рядом» — остановлю после него."}), 409
            итог = server2.стоп()
        elif действие == "status":
            итог = server2.состояние()
        else:
            return jsonify({"error": "action — start, stop или status."}), 400
    except server2.ОшибкаСервера as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(итог)


@app.post("/api/opt/side")
def api_opt_side():
    """Один вопрос в двух (и более) конфигурациях на одном поиске; stream — потоком
    с этапами (загрузка, ответ), токенами и итогом каждой."""
    from agent.local import server2, tuning
    from agent.rag import optbench
    данные = request.get_json(silent=True) or {}
    вопрос = _вопрос_rag(данные)
    if not вопрос["вопрос"]:
        return jsonify({"error": "Пустой вопрос."}), 400
    имена = [str(и) for и in (данные.get("configs") or ["до", "после"]) if str(и).strip()]
    try:
        конфигурации = tuning.загрузить()
    except tuning.ОшибкаКонфигурации as exc:
        return jsonify({"error": str(exc)}), 400
    чужие = [и for и in имена if и not in конфигурации]
    if чужие:
        return jsonify({"error": f"Нет конфигурации «{чужие[0]}» — «после» появляется после "
                                 "«Собрать «после» и создать модель»."}), 400
    if optbench.идёт_в_другом_процессе(optbench.прочитать_состояние(_каталоги_оптимизации()[1])) or \
            not _ЗАМОК_ОПТИМИЗАЦИИ.acquire(blocking=False):
        return jsonify({"error": "Идёт прогон оптимизации: ответ «рядом» помешал бы его замерам. "
                                 "Дождитесь конца или остановите прогон."}), 409
    судья = "" if данные.get("judge") is False else optbench.СУДЬЯ

    def рядом() -> dict:
        try:
            записи = optbench.рядом(agent._отвечатель(), вопрос, имена, судья=судья, ход=True)
        finally:
            if any(конфигурации[и].сервер == tuning.ВТОРОЙ for и in имена):
                server2.стоп()
            _ЗАМОК_ОПТИМИЗАЦИИ.release()
        return {"question": вопрос, "records": записи,
                "configs": [конфигурации[и].to_dict() for и in имена]}

    if данные.get("stream"):
        return _rag_потоком(рядом)
    try:
        return jsonify(рядом())
    except (AgentError, ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/opt/run")
def api_opt_run():
    """Скрининг или прогон качества в фоне: start — начать или продолжить, stop — остановить
    между ответами. Ход — в state.json, тот же, что у консоли."""
    from datetime import datetime as _дт
    from agent.local import server2, tuning
    from agent.rag import optbench
    данные = request.get_json(silent=True) or {}
    действие = (данные.get("action") or "start").strip()
    if действие == "stop":
        if _ОПТИМИЗАЦИЯ["стоп"] is not None:
            _ОПТИМИЗАЦИЯ["стоп"].set()
        return jsonify(_состояние_оптимизации())
    if действие != "start":
        return jsonify({"error": "action — start или stop."}), 400
    вид = (данные.get("kind") or "скрининг").strip()
    if вид not in ("скрининг", "качество", "микрозамер"):
        return jsonify({"error": "kind — скрининг, качество или микрозамер."}), 400
    if optbench.идёт_в_другом_процессе(optbench.прочитать_состояние(_каталоги_оптимизации()[1])):
        return jsonify({**_состояние_оптимизации(), "error": "Прогон уже идёт из консоли."}), 409
    if not _ЗАМОК_ОПТИМИЗАЦИИ.acquire(blocking=False):
        return jsonify({**_состояние_оптимизации(), "error": "Прогон или ответ «рядом» уже идёт."}), 409
    try:
        индекс, каталог = _каталоги_оптимизации()
        прежнее = optbench.прочитать_состояние(каталог) or {}
        метка = (данные.get("label") or "").strip() or (
            прежнее.get("метка") if прежнее.get("вид") == вид and not прежнее.get("завершён") else "") \
            or f"{вид}-{_дт.now():%Y%m%d-%H%M}"
        конфигурации = tuning.загрузить()
        if вид == "микрозамер":
            имена = данные.get("configs") or (прежнее.get("конфигурации") if метка == прежнее.get("метка") else None) \
                or list(optbench.МИКРО)
            замер = optbench.МикроЗамер(каталог, метка if метка == прежнее.get("метка") else "", имена,
                                        int(данные.get("rounds") or прежнее.get("раундов") or optbench.РАУНДОВ),
                                        конфигурации)
            стоп_м = threading.Event()
            _ОПТИМИЗАЦИЯ.update({"стоп": стоп_м, "ошибка": "", "вид": вид})

            def мерить() -> None:
                try:
                    if "р-без-проектора" in имена:
                        optbench.подготовить_текстовую(os.path.join(os.path.dirname(индекс), "out"))
                    замер.выполнить(стоп=стоп_м.is_set)
                    optbench.отчёт(индекс)
                except Exception as exc:   # noqa: BLE001 — ошибка видна на странице
                    _ОПТИМИЗАЦИЯ["ошибка"] = f"{type(exc).__name__}: {exc}"
                finally:
                    if any(конфигурации[и].сервер == tuning.ВТОРОЙ for и in имена):
                        server2.стоп()
                    _ЗАМОК_ОПТИМИЗАЦИИ.release()

            поток = threading.Thread(target=мерить, daemon=True)
            _ОПТИМИЗАЦИЯ["поток"] = поток
            поток.start()
            return jsonify({"started": True, "label": замер.метка, "total": len(замер.ключи()),
                            **_состояние_оптимизации()})
        if метка == прежнее.get("метка") and прежнее.get("задания") and not данные.get("configs"):
            задания = [optbench.Задание(*к.split("|")[:2], int(к.split("|")[2])) for к in прежнее["задания"]]
            вопросы = optbench.вопросы(list(dict.fromkeys(з.вопрос_id for з in задания)))
        else:
            имена = данные.get("configs") or list(optbench.СКРИНИНГ if вид == "скрининг" else optbench.КАЧЕСТВО)
            вопросы = optbench.вопросы(данные.get("questions") or
                                       (list(optbench.ВОПРОСЫ_СКРИНИНГА) if вид == "скрининг" else None))
            задания = (optbench.задания_скрининга(вопросы, имена) if вид == "скрининг" else
                       optbench.задания_качества(вопросы, имена, "после" if "после" in имена else ""))
        судья = "" if данные.get("judge") is False else optbench.СУДЬЯ
        прогон = optbench.Прогон(каталог, метка, задания, вопросы, конфигурации, судья, вид)
        нет = optbench.недостающие({з.конфигурация: конфигурации[з.конфигурация] for з in задания
                                    if з.конфигурация != "р-без-проектора"})
        if нет:
            raise ValueError(f"в Ollama нет весов {', '.join(нет)}: ollama pull …")
    except (ValueError, TypeError, KeyError, tuning.ОшибкаКонфигурации) as exc:
        _ЗАМОК_ОПТИМИЗАЦИИ.release()
        return jsonify({"error": str(exc)}), 400
    стоп = threading.Event()
    _ОПТИМИЗАЦИЯ.update({"стоп": стоп, "ошибка": "", "вид": вид})

    def прогнать() -> None:
        try:
            if any(з.конфигурация == "р-без-проектора" for з in задания):
                optbench.подготовить_текстовую(os.path.join(os.path.dirname(индекс), "out"))
            прогон.выполнить(agent._отвечатель(), agent.client, стоп=стоп.is_set)
            optbench.отчёт(индекс)
        except Exception as exc:   # noqa: BLE001 — ошибка видна на странице
            _ОПТИМИЗАЦИЯ["ошибка"] = f"{type(exc).__name__}: {exc}"
        finally:
            if any(конфигурации[з.конфигурация].сервер == tuning.ВТОРОЙ for з in задания):
                server2.стоп()
            _ЗАМОК_ОПТИМИЗАЦИИ.release()

    поток = threading.Thread(target=прогнать, daemon=True)
    _ОПТИМИЗАЦИЯ["поток"] = поток
    поток.start()
    return jsonify({"started": True, "label": метка, "total": len(задания), **_состояние_оптимизации()})


@app.post("/api/opt/model")
def api_opt_model():
    """«После» по скринингу → Modelfile.rag-docs, ollama create loc-rag-docs, карта ролей."""
    from agent.rag import optbench
    if _ЗАМОК_ОПТИМИЗАЦИИ.locked():
        return jsonify({"error": "Идёт прогон — модель соберу после него."}), 409
    индекс, _ = _каталоги_оптимизации()
    try:
        итог = optbench.создать_модель(индекс, os.path.dirname(os.path.abspath(__file__)))
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    catalog.forget_installed()
    return jsonify({**итог, **_состояние_оптимизации()})


@app.post("/api/opt/report")
def api_opt_report():
    from agent.rag import optbench
    индекс, _ = _каталоги_оптимизации()
    md, js, _ = optbench.отчёт(индекс)
    return jsonify({"report": os.path.basename(md), **_состояние_оптимизации()})


@app.post("/api/rag/mark")
def api_rag_mark():
    """👍 / 👎 ответу участника на вопрос (FR-10)."""
    from agent.rag import localbench
    данные = request.get_json(silent=True) or {}
    try:
        з = localbench.отметить(agent.memory.base_dir, (данные.get("id") or "").strip(),
                                (данные.get("participant") or "").strip(), (данные.get("mark") or "").strip(),
                                (данные.get("note") or "").strip())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    отметки = localbench.отметки(agent.memory.base_dir)
    return jsonify({"mark": з, "marks": {f"{в}|{у}": о["mark"] for (в, у), о in отметки.items()}})


@app.post("/api/rag/citecheck")
def api_rag_citecheck():
    """Проверка цитат на 10 контрольных и расплывчатых вопросах — в фоне."""
    данные = request.get_json(silent=True) or {}
    try:
        вопросы = rag_citecheck.загрузить_вопросы()
    except (OSError, ValueError) as exc:
        return jsonify({"error": f"Вопросы не прочитаны: {exc}"}), 400
    модель = (данные.get("model") or "").strip()
    модель = модель if модель in catalog.MODELS else ""
    судья = bool(данные.get("judge", True))
    форматы = ("цитаты", "текст") if данные.get("compare", True) else ("цитаты",)
    if not _ЗАМОК_ИНДЕКСА.acquire(blocking=False):
        return jsonify({"error": "С индексом уже идёт работа — дождитесь её конца."}), 409

    def прогнать(ход):
        отвечатель = agent._отвечатель()
        прогон = rag_citecheck.прогнать(отвечатель, вопросы, судья, модель, прогресс=ход,
                                        форматы=форматы)
        rag_citecheck.записать(прогон, os.path.dirname(отвечатель.индексатор.база))
        return {"итог": прогон.to_dict()["totals"]}

    _в_фоне("цитаты", прогнать)
    return jsonify({"work": dict(_индексация)}), 202


@app.post("/api/rag/confidence")
def api_rag_confidence():
    """Подбор порога «не знаю» на вопросах Дня 21 — в фоне (оценки реранкера в кэше)."""
    if not _ЗАМОК_ИНДЕКСА.acquire(blocking=False):
        return jsonify({"error": "С индексом уже идёт работа — дождитесь её конца."}), 409

    def подобрать(ход):
        ход("порог «не знаю»", 0, 1, "оценки реранкера на стенде")
        поисковик = agent._отвечатель().поисковик
        итог = rag_abstain.подобрать(поисковик, rag_evaluate.путь_вопросов_по_умолчанию())
        rag_abstain.записать(итог, rag_retrieval.путь_настроек())
        return {"порог": итог["порог"], "выбрано": итог["выбрано"]}

    _в_фоне("уверенность", подобрать)
    return jsonify({"work": dict(_индексация)}), 202


# --- мини-чат с RAG и памятью задачи (День 25) -----------------------------------

def _сессия_чата(значение) -> str:
    return (str(значение or "").strip() or "основная")[:64]


def _ответы_помощи(сессия: str) -> list:
    from agent.devassist import command
    return command.ответы_сессии(agent.memory.base_dir, сессия)


def _состояние_чата(сессия: str) -> dict:
    from agent.chat import scenarios as chat_scenarios
    чат = agent.чат(сессия)
    память = чат.память
    данные = память.to_dict()
    ходы = данные.pop("журнал", [])
    работа = dict(_индексация) if _индексация and _индексация.get("вид") == "сценарии" else None
    return {
        "сессия": сессия, "сессии": agent.чаты(),
        "память": {**данные, "коды": list(память.коды()), "пуста": память.пуста},
        "ходы": ходы,
        "помощь": _ответы_помощи(сессия),
        "история": [{"role": с["role"], "content": с["content"], "when": с["created_at"]}
                    for с in чат.история()],
        "сценарии": chat_scenarios.прочитать(agent.memory.base_dir),
        "работа": работа,
        "порог_уверенности": rag_retrieval.прочитать_настройки()["порог_уверенности"],
    }


@app.get("/chat")
def chat_page():
    from agent.rag.answer import РОЛЬ_МОДЕЛИ
    return render_template("chat.html", models=catalog.describe(),
                           rag_model=catalog.for_role(РОЛЬ_МОДЕЛИ, 0))


@app.get("/api/chat")
def api_chat():
    """История, память задачи и журнал ходов сессии; последний прогон сценариев."""
    try:
        return jsonify(_состояние_чата(_сессия_чата(request.args.get("session"))))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/chat/say")
def api_chat_say():
    """Реплика в чат: разбор, поиск по базе, ответ с источниками, память задачи."""
    from agent.chat import ОшибкаЧата
    данные = request.get_json(silent=True) or {}
    текст = (данные.get("text") or "").strip()
    if not текст:
        return jsonify({"error": "Пустая реплика."}), 400
    сессия = _сессия_чата(данные.get("session"))
    модель = (данные.get("model") or "").strip()
    from agent.local import offline
    if offline.включён() and модель in catalog.MODELS and not catalog.get(модель).local:
        return jsonify({"error": f"Офлайн-режим: облачная модель «{модель}» недоступна — выберите "
                                 "локальную или «по ролям»."}), 400
    помощь = _помощь_в_чате(сессия, текст, модель, bool(данные.get("stream")))
    if помощь is not None:
        return помощь
    ревью = _ревью_в_чате(сессия, текст, модель, bool(данные.get("stream")))
    if ревью is not None:
        return ревью
    if данные.get("stream"):
        return _ход_чата_потоком(сессия, текст, модель)
    прежняя = agent.model_key
    agent.model_key = модель if модель in catalog.MODELS else ""
    try:
        ход = agent.чат(сессия).ход(текст)
    except (ОшибкаЧата, AgentError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        agent.model_key = прежняя
    итог = ход.to_dict()
    итог.pop("rag", None)
    return jsonify({"turn": итог, "state": _состояние_чата(сессия)})


def _помощь_в_чате(сессия: str, текст: str, модель: str, поток: bool) -> Response | None:
    """`/help` на странице чата (День 31, spec FR-09): то же, что в консоли — паритет.

    None — это не /help. Ответ ассистента хранится в ленте сессии (command.сохранить), и
    страница рисует его карточкой после хода, за которым его задали."""
    import json as _json
    import queue
    from agent.devassist import command
    from agent.devassist.project import ОшибкаПроекта
    from agent.local import offline, progress
    разбор = command.разобрать(текст)
    if разбор is None:
        return None
    вид, вопрос = разбор
    if вид == command.СПРАВКА:
        итог = {"событие": "готово", "help_text": command.справка("crm"), "state": _состояние_чата(сессия)}
        if поток:
            return Response(_json.dumps(итог, ensure_ascii=False) + "\n", mimetype="application/x-ndjson")
        return jsonify(итог)
    модель = модель if модель in catalog.MODELS else ""

    def ответить() -> dict:
        ассистент = command.создать(модель, "веб")
        try:
            ответ = ассистент.спросить(вопрос)
        finally:
            ассистент.закрыть()
        command.в_историю(agent.чат(сессия), вопрос, ответ, agent.memory.base_dir)
        return {"событие": "готово", "help": ответ.to_dict(), "state": _состояние_чата(сессия),
                "net": offline.журнал(0)}

    if not поток:
        try:
            return jsonify(ответить())
        except (ОшибкаПроекта, OSError) as exc:
            return jsonify({"error": str(exc)}), 400
    очередь: queue.Queue = queue.Queue()

    def работать() -> None:
        метка = progress.наблюдать(очередь.put)
        try:
            очередь.put(ответить())
        except Exception as exc:   # noqa: BLE001 — поток не должен умереть молча
            очередь.put({"событие": "ошибка", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            progress.снять(метка)
            очередь.put(None)

    threading.Thread(target=работать, daemon=True).start()

    def строки():
        while (событие := очередь.get()) is not None:
            yield _json.dumps(событие, ensure_ascii=False, default=str) + "\n"

    return Response(stream_with_context(строки()), mimetype="application/x-ndjson")


def _ревью_в_чате(сессия: str, текст: str, модель: str, поток: bool) -> Response | None:
    """`/review [диапазон]` на странице чата (День 32, spec FR-13): тот же конвейер, что в консоли
    и GitHub Actions; итог — карточка в ленте сессии (паритет с консолью). None — это не /review."""
    import json as _json
    import queue
    from agent.devassist import command as help_command
    from agent.local import progress
    from agent.review import command
    try:
        разбор = command.разобрать(текст)
    except command.ОшибкаКоманды as exc:
        return jsonify({"error": str(exc)}), 400
    if разбор is None:
        return None
    от, до = разбор

    def выполнить(этап=None) -> dict:
        итог = command.выполнить(от, до, модель, "веб", этап=этап)
        command.в_историю(agent.чат(сессия), от, до, итог, agent.memory.base_dir)
        return {"событие": "готово", "review": command.карточка(от, до, итог), "state": _состояние_чата(сессия)}

    if not поток:
        return jsonify(выполнить())
    очередь: queue.Queue = queue.Queue()

    def работать() -> None:
        метка = progress.наблюдать(очередь.put)
        try:
            # Этапы конвейера приходят наблюдателю progress (этап «ревью» с подписью) — те же
            # события, что у хода чата: страница рисует ход строками.
            очередь.put(выполнить())
        except Exception as exc:   # noqa: BLE001 — поток не должен умереть молча
            очередь.put({"событие": "ошибка", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            progress.снять(метка)
            очередь.put(None)

    threading.Thread(target=работать, daemon=True).start()

    def строки():
        while (событие := очередь.get()) is not None:
            yield _json.dumps(событие, ensure_ascii=False, default=str) + "\n"

    return Response(stream_with_context(строки()), mimetype="application/x-ndjson")


def _ход_чата_потоком(сессия: str, текст: str, модель: str) -> Response:
    """Ход чата потоком NDJSON (День 27): на локальной модели он идёт минуты.

    Строки — события хода: {"событие": "этап", "этап": "разбор-реплики",
    "модель": …}, {"событие": "токены", "выход": 57, "секунд": 31.2}; последняя —
    {"событие": "готово", "turn": …, "state": …} или {"событие": "ошибка"}.
    Ход идёт в своём потоке, наблюдатель (progress) стоит только в нём."""
    import json as _json
    import queue
    from agent.chat import ОшибкаЧата
    from agent.local import offline, progress
    очередь: queue.Queue = queue.Queue()

    def ходить() -> None:
        метка = progress.наблюдать(очередь.put)
        прежняя = agent.model_key
        agent.model_key = модель if модель in catalog.MODELS else ""
        try:
            ход = agent.чат(сессия).ход(текст)
            итог = ход.to_dict()
            итог.pop("rag", None)
            очередь.put({"событие": "готово", "turn": итог, "state": _состояние_чата(сессия),
                         "net": offline.журнал(0)})
        except (ОшибкаЧата, AgentError, ValueError) as exc:
            очередь.put({"событие": "ошибка", "error": str(exc)})
        except Exception as exc:   # noqa: BLE001 — поток не должен умереть молча
            очередь.put({"событие": "ошибка", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            agent.model_key = прежняя
            progress.снять(метка)
            очередь.put(None)

    threading.Thread(target=ходить, daemon=True).start()

    def строки():
        while (событие := очередь.get()) is not None:
            yield _json.dumps(событие, ensure_ascii=False, default=str) + "\n"

    return Response(stream_with_context(строки()), mimetype="application/x-ndjson")


@app.post("/api/chat/new")
def api_chat_new():
    данные = request.get_json(silent=True) or {}
    сессия = _сессия_чата(данные.get("session"))
    итог = agent.чат(сессия).новый()
    from agent.devassist import command
    итог["помощь_стёрта"] = command.очистить(agent.memory.base_dir, сессия)
    return jsonify({"reset": итог, "state": _состояние_чата(сессия)})


@app.post("/api/chat/scenarios")
def api_chat_scenarios():
    """Длинные сценарии чата — в фоне: проверки кодом по ходам, судья в конце."""
    from agent.chat import scenarios as chat_scenarios
    данные = request.get_json(silent=True) or {}
    try:
        сценарии = chat_scenarios.загрузить()
    except (OSError, ValueError) as exc:
        return jsonify({"error": f"Сценарии не прочитаны: {exc}"}), 400
    нужные = [str(и) for и in данные.get("ids") or [] if str(и).strip()]
    if нужные:
        сценарии = [с for с in сценарии if с["id"] in нужные]
        if not сценарии:
            return jsonify({"error": f"Нет сценариев {', '.join(нужные)}."}), 400
    судья = bool(данные.get("judge", True))
    if not _ЗАМОК_ИНДЕКСА.acquire(blocking=False):
        return jsonify({"error": "С индексом уже идёт работа — дождитесь её конца."}), 409

    def прогнать(ход):
        прогон = chat_scenarios.прогнать(agent, сценарии, судья, ход)
        chat_scenarios.записать(прогон, agent.memory.base_dir)
        return {"итог": прогон.итог()}

    _в_фоне("сценарии", прогнать)
    return jsonify({"work": dict(_индексация)}), 202


# --- локальная модель (День 26) -------------------------------------------------

РЕЖИМЫ_ВОПРОСА = ("local", "cloud", "both")


def _облачная_по_умолчанию() -> str:
    """Облачная модель для сравнения: первая облачная роли ответа. Не for_role:
    в офлайне (День 27) та отдаёт локальную модель из карты ролей."""
    return next((к for к in catalog.ROLES["ответ-по-документам"]["models"] if not catalog.get(к).local),
                "ds-flash")


def _состояние_офлайна(журнал: int = 20) -> dict:
    """Офлайн-режим: включён ли, карта ролей, поиск, MCP-серверы, журнал сети."""
    from agent.local import offline
    from agent.mcp import config as mcp_config
    try:
        карта = offline.карта().to_dict()
        ошибка = ""
    except (OSError, ValueError) as exc:
        карта, ошибка = {}, str(exc)
    try:
        серверы = [{"name": с.имя, "ready": с.готов, "outside": с.наружу, "why": с.почему_не_готов}
                   for с in mcp_config.load(agent.mcp_config or "")]
    except Exception as exc:   # noqa: BLE001 — без файла серверов офлайн всё равно работает
        серверы = [{"name": "—", "ready": False, "outside": False, "why": str(exc)}]
    поиск = offline.поиск()
    return {"on": offline.включён(), "source": offline.источник(), "map": карта, "map_error": ошибка,
            "search": поиск.to_dict() if поиск else None,
            "confidence": offline.порог_уверенности() if not ошибка else None,
            "servers": серверы, "net": offline.журнал(журнал)}


@app.get("/api/offline")
def api_offline():
    """Состояние офлайн-режима и журнал сети процесса."""
    return jsonify(_состояние_офлайна())


@app.post("/api/offline")
def api_offline_set():
    """Включить или выключить офлайн-режим для всего сервера (переключатель в шапке)."""
    from agent.local import offline
    данные = request.get_json(silent=True) or {}
    if "on" not in данные:
        return jsonify({"error": "Нужно поле on: true или false."}), 400
    offline.включить(bool(данные["on"]), "страница" if данные["on"] else "")
    if данные.get("reset_net"):
        offline.сбросить_журнал()
    return jsonify(_состояние_офлайна())


@app.post("/api/offline/agent")
def api_offline_agent():
    """Облегчённый агент офлайна: сжатый промпт, самоотчёт, судья — в карту ролей."""
    from agent.local import offline
    данные = request.get_json(silent=True) or {}
    try:
        offline.задать_агента(данные)
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_состояние_офлайна())


@app.post("/api/offline/net/reset")
def api_offline_net_reset():
    from agent.local import offline
    offline.сбросить_журнал()
    return jsonify(_состояние_офлайна())


def _состояние_локальной() -> dict:
    from agent.local import bench, ollama
    работа = dict(_индексация) if _индексация and _индексация.get("вид") == "бенчмарк" else None
    return {"ollama": ollama.состояние(), "main": bench.основная(),
            "cloud": _облачная_по_умолчанию(), "offline": _состояние_офлайна(),
            "bench": bench.прочитать(agent.memory.base_dir), "работа": работа,
            "tasks": bench.загрузить_задачи()}


@app.get("/local")
def local_page():
    from agent.local import bench
    return render_template("local.html", models=catalog.describe(), main=bench.основная(),
                           cloud=_облачная_по_умолчанию())


@app.get("/api/local")
def api_local():
    """Ollama: служба, модели, память; основная модель; последний замер."""
    return jsonify(_состояние_локальной())


@app.post("/api/local/ask")
def api_local_ask():
    """Вопрос локальной и/или облачной модели — потоком NDJSON.

    Каждая строка — событие одной модели: {"model": ключ, "тип": "текст" |
    "думает" | "итог" | "ошибка", ...}. В режиме «оба» модели отвечают
    одновременно: облако не ждёт, пока локальная модель допишет."""
    import json as _json
    import queue
    from agent.llm import LLMError
    from agent.local import stream
    данные = request.get_json(silent=True) or {}
    вопрос = (данные.get("question") or "").strip()
    режим = (данные.get("mode") or "local").strip()
    if not вопрос:
        return jsonify({"error": "Пустой вопрос."}), 400
    if режим not in РЕЖИМЫ_ВОПРОСА:
        return jsonify({"error": f"Режим: {', '.join(РЕЖИМЫ_ВОПРОСА)}."}), 400
    from agent.local import bench
    локальная = (данные.get("model") or bench.основная()).strip()
    облачная = (данные.get("cloud") or _облачная_по_умолчанию()).strip()
    if локальная not in catalog.MODELS or not catalog.get(локальная).local:
        return jsonify({"error": f"«{локальная}» — не локальная модель."}), 400
    if облачная not in catalog.MODELS or catalog.get(облачная).local:
        return jsonify({"error": f"«{облачная}» — не облачная модель."}), 400
    модели = {"local": [локальная], "cloud": [облачная], "both": [локальная, облачная]}[режим]
    думать = bool(данные.get("think"))
    # «Как в замере»: та же системная подсказка, что у задач замера, — иначе
    # модель Qwen на «ответь числом» пишет сотни токенов рассуждений.
    сообщения = [{"role": "user", "content": вопрос}]
    if данные.get("short"):
        система = bench.системная_подсказка()
        if система:
            сообщения.insert(0, {"role": "system", "content": система})
    очередь: queue.Queue = queue.Queue()

    def ответить(ключ: str) -> None:
        try:
            for событие in stream.поток(ключ, сообщения, думать=думать):
                очередь.put({"model": ключ, **событие})
        except LLMError as exc:
            очередь.put({"model": ключ, "тип": "ошибка", "текст": str(exc)})
        finally:
            очередь.put({"model": ключ, "тип": "конец"})

    for ключ in модели:
        threading.Thread(target=ответить, args=(ключ,), daemon=True).start()

    def строки():
        осталось = len(модели)
        while осталось:
            событие = очередь.get()
            if событие["тип"] == "конец":
                осталось -= 1
                continue
            yield _json.dumps(событие, ensure_ascii=False) + "\n"

    return Response(stream_with_context(строки()), mimetype="application/x-ndjson")


@app.post("/api/local/cli")
def api_local_cli():
    """Тот же вопрос через CLI: `ollama run МОДЕЛЬ «…» --verbose` отдельным процессом."""
    import subprocess
    from agent.local import bench, ollama
    данные = request.get_json(silent=True) or {}
    вопрос = (данные.get("question") or "").strip()
    ключ = (данные.get("model") or bench.основная()).strip()
    if not вопрос:
        return jsonify({"error": "Пустой вопрос."}), 400
    if ключ not in catalog.MODELS or not catalog.get(ключ).local:
        return jsonify({"error": f"«{ключ}» — не локальная модель."}), 400
    try:
        return jsonify(ollama.через_cli(catalog.get(ключ).api_id, вопрос))
    except (ollama.ОшибкаOllama, subprocess.TimeoutExpired) as exc:
        return jsonify({"error": str(exc)}), 502


@app.post("/api/local/http")
def api_local_http():
    """Тот же вопрос сырым HTTP-запросом: родной /api/chat или OpenAI /v1."""
    from agent.local import bench, ollama
    данные = request.get_json(silent=True) or {}
    вопрос = (данные.get("question") or "").strip()
    вид = (данные.get("kind") or "native").strip()
    ключ = (данные.get("model") or bench.основная()).strip()
    if not вопрос:
        return jsonify({"error": "Пустой вопрос."}), 400
    if вид not in ollama.ВИДЫ_HTTP:
        return jsonify({"error": f"Вид: {', '.join(ollama.ВИДЫ_HTTP)}."}), 400
    if ключ not in catalog.MODELS or not catalog.get(ключ).local:
        return jsonify({"error": f"«{ключ}» — не локальная модель."}), 400
    try:
        return jsonify(ollama.через_http(вид, catalog.get(ключ).api_id, вопрос))
    except ollama.ОшибкаOllama as exc:
        return jsonify({"error": str(exc)}), 502


@app.post("/api/local/bench")
def api_local_bench():
    """Запросы трёх уровней сложности — в фоне; выбор основной модели."""
    from agent.local import bench
    данные = request.get_json(silent=True) or {}
    try:
        задачи = bench.загрузить_задачи()
    except (OSError, ValueError) as exc:
        return jsonify({"error": f"Задачи не прочитаны: {exc}"}), 400
    модели = [м for м in (данные.get("models") or []) if м in catalog.MODELS]
    if not модели:
        модели = [к for к in catalog.local_models() if catalog.installed(к)]
        if not модели:
            return jsonify({"error": "Ни одной локальной модели не скачано: ollama pull qwen3:4b"}), 400
        if данные.get("cloud", True):
            модели.append(catalog.for_role("ответ-по-документам", 0))
    if not _ЗАМОК_ИНДЕКСА.acquire(blocking=False):
        return jsonify({"error": "Уже идёт долгая работа — дождитесь её конца."}), 409

    def прогнать(ход):
        отвечатель = agent._отвечатель() if any(з["проверка"]["вид"] == "rag" for з in задачи) else None
        прогон = bench.прогнать(модели, задачи, ход, отвечатель)
        bench.записать(прогон, agent.memory.base_dir)
        выбор = bench.записать_выбор(прогон) if any(catalog.get(м).local for м in модели) else {}
        return {"выбор": выбор.get("основная", ""), "итог": {к: прогон.итог(к) for к in модели}}

    _в_фоне("бенчмарк", прогнать)
    return jsonify({"work": dict(_индексация)}), 202


@app.post("/api/local/mark")
def api_local_mark():
    """Отметка человека ответу: 👍 или 👎."""
    from agent.local import bench
    данные = request.get_json(silent=True) or {}
    try:
        запись = bench.отметить(agent.memory.base_dir, str(данные.get("task") or "").strip(),
                                str(данные.get("model") or "").strip(), str(данные.get("mark") or ""),
                                str(данные.get("note") or ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"mark": запись, "marks": bench.отметки(agent.memory.base_dir)})


@app.get("/api/health")
def api_health():
    инфо = agent.info()
    return jsonify({"ok": True, "model": инфо["model_key"], "layers": инфо["layers"]})


if __name__ == "__main__":
    # Порт и адрес берутся из окружения: 5000 бывает занят — хотя бы прошлым
    # экземпляром этого же сервера. Тогда Flask пишет «Address already in use»
    # и завершается, а браузер продолжает показывать прежний экземпляр со
    # старым кодом; отличить это от поломки трудно.
    порт = int(os.getenv("PORT", "5000"))
    адрес = os.getenv("HOST", "127.0.0.1")
    print(f"Открыть: http://{адрес}:{порт}")
    app.run(debug=True, host=адрес, port=порт)
