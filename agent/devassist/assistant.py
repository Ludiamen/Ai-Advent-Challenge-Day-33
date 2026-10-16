"""Ассистент разработчика: ответ на `/help вопрос` о проекте (День 31, spec FR-06, plan D-04).

Один модуль на все каналы — консоль и веб зовут его в процессе агента, сервис — в своём.

1. Контекст git — всегда, кодом: ветка и короткий статус через MCP-сервер git (только чтение).
2. Поиск по индексу проекта — локально, без облака: облачной модели — 10 фрагментов по
   косинусу (меньше секунды; кросс-энкодер 16→6 на этом ноутбуке — 60–80 с, а отбор почти тот
   же, лишнее отсеивает сама модель), локальной — профиль офлайна Дней 27–30 с кросс-энкодером
   и окнами (окно 4096 у loc-rag-docs). LLM-реранкер не используется: в сервисе предохранитель
   его не пропустил бы, а документы на этапе поиска машину не покидают.
3. Модель с ролью ассистента разработчика и инструментами git_* (список файлов, diff, журнал):
   вызывает их, когда вопрос о них, — не больше КРУГОВ кругов, затем ответ без инструментов.
4. Ответ — JSON Дня 24; цитаты проверяет код (`agent/rag/cite.py`). Нет подтверждённой цитаты
   и не было вызова git — «не знаю», а не выдумка. Один повторный запрос при нарушениях формата.
5. Запасной путь (лекция недели 7): облако не ответило — повтор, затем локальная модель с
   пометкой; git недоступен — ответ по документам с пометкой. Сбой виден в ответе, не только в журнале.
6. Маска ПД точного профиля на выходе и на выводе git, который уходит модели; строка метрик
   без текстов.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from agent import catalog
from agent.devassist import metrics, prompts
from agent.devassist.project import Проект, индексатор
from agent.llm import LLMError
from agent.local import progress
from agent.rag import cite
from agent.service import pd

log = logging.getLogger(__name__)

КРУГОВ = 3
ПРЕДЕЛ_ИНСТРУМЕНТА = 24000       # символов вывода git в одно сообщение модели (git_show — файл до 20 000)
# Локальная модель на CPU читает промпт 6–7 ток/с: файл в 20 000 символов — это минуты на круг, и
# вопрос набора шёл 15–19 мин (check_devassist, День 31). Локально — короче вывод и меньше кругов.
КРУГОВ_ЛОКАЛЬНО = 2
ПРЕДЕЛ_ИНСТРУМЕНТА_ЛОКАЛЬНО = 3000
ОТВЕТ_ОБЛАКО = 2000             # 1200 не хватало на «как устроен проект»: JSON обрывался (показ, дубль 2)
ОТВЕТ_ЛОКАЛЬНО = 600
K_ОБЛАКО = 10                    # фрагментов облачной модели — по косинусу, без второго этапа
ПАУЗА_ПОВТОРА_С = 3.0
ЗАПАСНАЯ = "loc-rag-docs"
# Модель пишет «посмотрю файл целиком в git» и отвечает, так и не вызвав инструмент (показ Дня 31,
# дубль 4: тот же вопрос в проверке шёл с git_show). Обещание без вызова — одно напоминание.
_ОБЕЩАНИЕ = re.compile(r"(?i)\b(посмотрю|прочитаю|проверю|вызову|открою|загляну|сейчас (посмотрю|прочитаю))\b")
НАПОМИНАНИЕ = ("Ты написал, что посмотришь или прочитаешь, но инструмент не вызвал. Вызови нужный инструмент "
               "git_* сейчас, затем дай ответ в обязательном формате JSON.")
GIT_ИНСТРУМЕНТЫ = ("git__git_branch", "git__git_status", "git__git_ls_files", "git__git_diff", "git__git_log",
                   "git__git_show")


# --- git: через MCP или в процессе ------------------------------------------------

class ОшибкаGit(RuntimeError):
    pass


def _данные(текст: str, данные: Any) -> dict[str, Any]:
    if isinstance(данные, dict):
        return данные.get("result", данные) if set(данные) == {"result"} else данные
    try:
        итог = json.loads(текст or "{}")
        return итог if isinstance(итог, dict) else {"текст": текст}
    except json.JSONDecodeError:
        return {"текст": текст}


class GitMCP:
    """Сервер git из mcp-servers.json — отдельный процесс stdio (консоль, веб, сервис)."""

    имя = "mcp"

    def __init__(self, проект: Проект, путь_конфига: str = "") -> None:
        from agent.mcp.toolbox import Toolbox
        # Корень проекта — в окружение до чтения конфига: ${PROJECT_ROOT} раскрывается при загрузке.
        os.environ["PROJECT_ROOT"] = проект.путь
        self.ящик = Toolbox(["git"], путь_конфига)
        self._открыт = False

    def _открыть(self) -> None:
        if not self._открыт:
            осмотры = self.ящик.открыть()
            сбой = [о for о in осмотры if not getattr(о, "ок", True)]
            if not self.ящик.инструменты:
                о = сбой[0] if сбой else None
                причина = ((о.ошибка or о.этап or "") + (f" — {о.журнал.strip().splitlines()[-1][:160]}"
                                                         if о and о.журнал.strip() else "")) if о else "нет инструментов"
                raise ОшибкаGit(f"сервер git не подключился: {причина}")
            self._открыт = True

    def инструменты(self) -> list[dict[str, Any]]:
        self._открыть()
        return [и for и in self.ящик.для_модели() if и["function"]["name"] in GIT_ИНСТРУМЕНТЫ]

    def вызвать(self, имя: str, аргументы: dict[str, Any] | None = None) -> tuple[bool, str, dict[str, Any]]:
        self._открыть()
        try:
            р = self.ящик.вызвать(имя, аргументы or {})
        except Exception as e:  # noqa: BLE001 — сбой сервера тоже ответ модели
            return False, str(e), {}
        return р.ок, р.для_модели(), _данные(р.текст, р.данные)

    def закрыть(self) -> None:
        try:
            self.ящик.close()
        except Exception:  # noqa: BLE001
            pass


class GitВПроцессе:
    """Тот же сервер git_server.py через клиент MCP в памяти — без дочернего процесса (plan D-08a)."""

    имя = "в процессе"

    def __init__(self, проект: Проект) -> None:
        os.environ["GIT_ROOT"] = проект.путь
        import git_server
        self.модуль = git_server

    def _с_клиентом(self, действие: Callable[[Any], Any]) -> Any:
        import anyio
        from mcp import Client

        async def работа():
            async with Client(self.модуль.сервер) as клиент:
                return await действие(клиент)
        return anyio.run(работа)

    def инструменты(self) -> list[dict[str, Any]]:
        async def список(к):
            return (await к.list_tools()).tools
        return [{"type": "function", "function": {"name": f"git__{и.name}", "description": и.description or "",
                                                  "parameters": и.inputSchema}}
                for и in self._с_клиентом(список)]

    def вызвать(self, имя: str, аргументы: dict[str, Any] | None = None) -> tuple[bool, str, dict[str, Any]]:
        короткое = имя.split("__", 1)[-1]

        async def вызов(к):
            return await к.call_tool(короткое, аргументы or {})
        try:
            р = self._с_клиентом(вызов)
        except Exception as e:  # noqa: BLE001
            return False, str(e), {}
        текст = "\n".join(getattr(б, "text", "") for б in (р.content or []))
        return not р.is_error, текст, _данные(текст, getattr(р, "structured_content", None))

    def закрыть(self) -> None:
        pass


# --- ответ -----------------------------------------------------------------------

@dataclass
class ВызовGit:
    имя: str
    аргументы: dict[str, Any] = field(default_factory=dict)
    ок: bool = True
    секунд: float = 0.0
    кратко: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.имя, "args": self.аргументы, "ok": self.ок,
                "seconds": round(self.секунд, 2), "summary": self.кратко}


@dataclass
class ОтветАссистента:
    вопрос: str
    проект: str
    канал: str
    модель: str = ""
    текст: str = ""
    источники: list[dict[str, Any]] = field(default_factory=list)
    цитаты: list[dict[str, Any]] = field(default_factory=list)
    не_знаю: bool = False
    уточнение: str = ""
    git: dict[str, Any] = field(default_factory=dict)
    вызовы: list[ВызовGit] = field(default_factory=list)
    пометки: list[str] = field(default_factory=list)
    запасной: str = ""
    нарушения: list[str] = field(default_factory=list)
    токенов_вход: int = 0
    токенов_выход: int = 0
    этапы: dict[str, float] = field(default_factory=dict)
    фрагментов: int = 0
    скрыто_пд: dict[str, int] = field(default_factory=dict)
    ошибка: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"object": "project.help", "question": self.вопрос, "project": self.проект,
                "channel": self.канал, "model": self.модель, "answer": self.текст,
                "sources": self.источники, "quotes": self.цитаты, "abstain": self.не_знаю,
                "clarification": self.уточнение, "git": self.git,
                "tool_calls": [в.to_dict() for в in self.вызовы], "notes": self.пометки,
                "fallback": self.запасной, "violations": self.нарушения,
                "usage": {"prompt_tokens": self.токенов_вход, "completion_tokens": self.токенов_выход},
                "timings": {к: round(в, 2) for к, в in self.этапы.items()},
                "fragments": self.фрагментов, "pd_hidden": self.скрыто_пд, "error": self.ошибка}


def _кратко(имя: str, данные: dict[str, Any], текст: str) -> str:
    """Одна строка о вызове для человека — без самого diff."""
    if имя.endswith("git_log"):
        return f"коммитов {len(данные.get('коммиты') or [])}"
    if имя.endswith("git_ls_files"):
        return f"файлов {данные.get('всего', '?')} по «{данные.get('шаблон', '*')}»"
    if имя.endswith("git_diff"):
        сводка = (данные.get("сводка") or "").splitlines()
        return (сводка[-1].strip() if сводка else "изменений нет") + (" · обрезано" if данные.get("обрезано") else "")
    if имя.endswith("git_show"):
        return f"{данные.get('путь', '?')} @ {данные.get('ссылка', 'HEAD')}: строк {данные.get('строк', '?')}" + (
            " · обрезано" if данные.get("обрезано") else "")
    if имя.endswith("git_branch"):
        return f"{данные.get('ветка', '?')} @ {данные.get('коммит', '?')}"
    if имя.endswith("git_status"):
        return "чисто" if данные.get("чисто") else f"изменено {данные.get('изменено', '?')}"
    return (текст or "").strip().splitlines()[0][:80] if текст else ""


class Ассистент:
    def __init__(self, проект: Проект, клиент: Any, git: Any = None, модель: str = "ds-flash",
                 канал: str = "консоль", запасная: str = ЗАПАСНАЯ, журнал: str = "",
                 поисковик: Any = None, пауза: Callable[[float], None] = time.sleep) -> None:
        self.проект = проект
        self.клиент = клиент
        self.git = git if git is not None else GitMCP(проект)
        self.модель = модель
        self.канал = канал
        self.запасная = запасная
        self.журнал = журнал
        self._поисковик = поисковик
        self.пауза = пауза

    # --- поиск ---------------------------------------------------------------

    def поисковик(self):
        if self._поисковик is None:
            from agent.rag.retrieval import Поисковик
            self._поисковик = Поисковик(индексатор(self.проект), self.клиент, "структура")
        return self._поисковик

    def _настройки(self, локально: bool):
        from agent.rag import profile
        профиль = profile.локальный(self.поисковик())
        if локально:
            return профиль.поиск
        return replace(профиль.поиск, реранкер="", rewrite=False, k_до=K_ОБЛАКО, k_после=K_ОБЛАКО,
                       имя=f"проект · косинус {K_ОБЛАКО}")

    def _найти(self, вопрос: str, локально: bool) -> list[Any]:
        фрагменты = list(self.поисковик().найти(вопрос, self._настройки(локально)).фрагменты)
        if локально:
            return фрагменты   # окно 4096: локальной модели — только отобранное
        # Схемы данных и API (пункт задания): лучший кусок нужного models.py / urls.py — к фрагментам.
        from agent.devassist.project import файлы_схем
        есть = {ф.чанк.паспорт().get("chunk_id") for ф in фрагменты}
        пути = файлы_схем(self.проект, вопрос)
        кусков = 3 if len(пути) <= 2 else 1   # названо приложение — файл целиком (до 3 фрагментов)
        for путь in пути:
            for ф in self.поисковик().индексатор.найти(вопрос, "структура", кусков, путь):
                if ф.чанк.паспорт().get("chunk_id") not in есть:
                    фрагменты.append(ф)
                    есть.add(ф.чанк.паспорт().get("chunk_id"))
        return фрагменты

    # --- git -------------------------------------------------------------------

    def _контекст_git(self, ответ: ОтветАссистента) -> str:
        progress.этап("git", подпись="ветка и статус (MCP)")
        старт = time.monotonic()
        try:
            ок_в, текст_в, ветка = self.git.вызвать("git__git_branch")
            ок_с, текст_с, статус = self.git.вызвать("git__git_status")
            if not (ок_в and ок_с):
                raise ОшибкаGit((текст_в if not ок_в else текст_с)[:200])
            ответ.git = {"branch": ветка.get("ветка", ""), "commit": ветка.get("коммит", ""),
                         "tracking": ветка.get("отслеживает", ""), "clean": bool(статус.get("чисто")),
                         "changed": int(статус.get("изменено") or 0), "source": self.git.имя}
            for имя, данные, текст in (("git__git_branch", ветка, текст_в), ("git__git_status", статус, текст_с)):
                ответ.вызовы.append(ВызовGit(имя, {}, True, 0.0, _кратко(имя, данные, текст)))
            рабочий = "чисто" if ответ.git["clean"] else f"изменено файлов: {ответ.git['changed']}"
            return prompts.GIT_БЛОК.format(ветка=ответ.git["branch"], коммит=ответ.git["commit"],
                                           отслеживает=f" (отслеживает {ответ.git['tracking']})"
                                           if ответ.git["tracking"] else "", статус=рабочий)
        except Exception as e:  # noqa: BLE001 — git недоступен: ответ по документам с пометкой
            ответ.git = {"error": str(e)[:200], "source": getattr(self.git, "имя", "")}
            ответ.пометки.append(f"git недоступен: {str(e)[:160]}")
            return prompts.GIT_НЕДОСТУПЕН.format(причина=str(e)[:160])
        finally:
            ответ.этапы["git"] = time.monotonic() - старт

    def _инструменты(self, ответ: ОтветАссистента) -> list[dict[str, Any]]:
        if "error" in ответ.git:
            return []
        try:
            return self.git.инструменты()
        except Exception as e:  # noqa: BLE001
            ответ.пометки.append(f"инструменты git недоступны: {str(e)[:120]}")
            return []

    # --- модель ----------------------------------------------------------------

    def _сообщения(self, вопрос: str, фрагменты: list[Any], git_блок: str) -> list[dict[str, Any]]:
        куски = []
        for n, ф in enumerate(фрагменты, start=1):
            паспорт = ф.чанк.паспорт()
            куски.append(prompts.ФРАГМЕНТ.format(n=n, источник=паспорт.get("source", ""),
                                                 раздел=паспорт.get("section") or "—", текст=ф.чанк.текст))
        система = prompts.РОЛЬ.format(описание=self.проект.описание or self.проект.имя) + "\n\n" + prompts.ПРАВИЛА
        польз = prompts.ВОПРОС.format(фрагменты="\n\n".join(куски) or prompts.НЕТ_ФРАГМЕНТОВ, git=git_блок,
                                      вопрос=вопрос)
        return [{"role": "system", "content": система}, {"role": "user", "content": польз}]

    def _звать(self, ответ: ОтветАссистента, модель: str, сообщения: list[dict[str, Any]],
               инструменты: list[dict[str, Any]] | None) -> Any:
        локально = catalog.get(модель).local
        r = self.клиент.call(модель, сообщения, max_tokens=ОТВЕТ_ЛОКАЛЬНО if локально else ОТВЕТ_ОБЛАКО,
                             temperature=0, tools=инструменты or None)
        ответ.токенов_вход += r.prompt_tokens
        ответ.токенов_выход += r.completion_tokens
        return r

    def _круги(self, ответ: ОтветАссистента, модель: str, сообщения: list[dict[str, Any]],
               инструменты: list[dict[str, Any]]) -> tuple[Any, str]:
        """Модель с инструментами git: вернуть последний ответ и текст выводов git (для проверки чисел)."""
        вывод_git = []
        r = None
        локально = catalog.get(модель).local
        кругов = КРУГОВ_ЛОКАЛЬНО if локально else КРУГОВ
        предел = ПРЕДЕЛ_ИНСТРУМЕНТА_ЛОКАЛЬНО if локально else ПРЕДЕЛ_ИНСТРУМЕНТА
        напомнили = False
        for _ in range(кругов):
            try:
                r = self._звать(ответ, модель, сообщения, инструменты)
            except LLMError as e:
                # Ollama отвечает 500, когда локальная модель пишет вызов инструмента с ошибкой
                # разметки («element <function> closed by </parameter>», check_devassist Дня 31):
                # один ответ без инструментов по уже собранному контексту, с пометкой.
                if not (локально and инструменты):
                    raise
                ответ.пометки.append(f"локальная модель сбилась на вызове инструмента ({str(e)[:80]}) — "
                                     "ответ без инструментов")
                return self._звать(ответ, модель, сообщения, None), "\n".join(вывод_git)
            if not r.tool_calls:
                if (инструменты and not вывод_git and not напомнили
                        and _ОБЕЩАНИЕ.search(cite.разобрать(r.text).ответ or r.text or "")):
                    напомнили = True
                    ответ.пометки.append("модель пообещала посмотреть в git, но не вызвала инструмент — напомнили")
                    сообщения += [{"role": "assistant", "content": r.text or ""},
                                  {"role": "user", "content": НАПОМИНАНИЕ}]
                    continue
                return r, "\n".join(вывод_git)
            сообщения.append(r.message or {"role": "assistant", "content": r.text or "",
                                           "tool_calls": [{"id": в.ид, "type": "function",
                                                           "function": {"name": в.имя,
                                                                        "arguments": json.dumps(в.аргументы, ensure_ascii=False)}}
                                                          for в in r.tool_calls]})
            for в in r.tool_calls:
                progress.этап("инструмент", подпись=в.имя.split("__")[-1])
                старт = time.monotonic()
                if в.ошибка:
                    ок, текст, данные = False, f"аргументы не разобраны: {в.ошибка}", {}
                elif в.имя not in GIT_ИНСТРУМЕНТЫ:
                    ок, текст, данные = False, (f"Инструмента «{в.имя}» нет: git доступен только на чтение "
                                                f"({', '.join(GIT_ИНСТРУМЕНТЫ)})."), {}
                else:
                    ок, текст, данные = self.git.вызвать(в.имя, в.аргументы)
                if len(текст) > предел:
                    текст = текст[:предел] + f"\n… вывод обрезан до {предел} символов"
                текст, скрыто = pd.скрыть(текст, профиль="точный")
                for вид, n in скрыто.items():
                    ответ.скрыто_пд[вид] = ответ.скрыто_пд.get(вид, 0) + n
                ответ.вызовы.append(ВызовGit(в.имя, в.аргументы, ок, time.monotonic() - старт,
                                             _кратко(в.имя, данные, текст) if ок else текст[:120]))
                вывод_git.append(текст)
                сообщения.append({"role": "tool", "tool_call_id": в.ид, "content": текст})
        # Круги кончились, а модель всё просит инструменты — последний ответ без них.
        return self._звать(ответ, модель, сообщения, None), "\n".join(вывод_git)

    def _ответить(self, ответ: ОтветАссистента, модель: str, фрагменты: list[Any], git_блок: str) -> None:
        progress.этап("ответ", модель=модель, локально=catalog.get(модель).local, фрагментов=len(фрагменты))
        ответ.модель = модель
        ответ.фрагментов = len(фрагменты)
        сообщения = self._сообщения(ответ.вопрос, фрагменты, git_блок)
        инструменты = self._инструменты(ответ)
        старт = time.monotonic()
        r, вывод_git = self._круги(ответ, модель, сообщения, инструменты)
        тексты = [ф.чанк.текст for ф in фрагменты]
        # Числа в путях и разделах («specs/003-servicedesk») — тоже опора: они в заголовке фрагмента.
        заголовки = "\n".join(f"{ф.чанк.паспорт().get('source', '')} {ф.чанк.паспорт().get('section', '')}"
                              for ф in фрагменты)
        опора = f"{ответ.вопрос}\n{git_блок}\n{вывод_git}\n{заголовки}"
        разбор = cite.разобрать(r.text)
        cite.проверить_цитаты(разбор, тексты)
        нарушено = self._нарушения(разбор, len(тексты), опора, self._по_git(разбор, вывод_git, ответ))
        if нарушено:
            повтор = сообщения + [{"role": "assistant", "content": r.text or ""},
                                  {"role": "user", "content": cite.сообщение_починки(нарушено)}]
            r2 = self._звать(ответ, модель, повтор, None)
            разбор = cite.разобрать(r2.text)
            cite.проверить_цитаты(разбор, тексты)
            нарушено = self._нарушения(разбор, len(тексты), опора, self._по_git(разбор, вывод_git, ответ))
        по_git = self._по_git(разбор, вывод_git, ответ)
        ответ.этапы["модель"] = time.monotonic() - старт
        ответ.нарушения = нарушено
        ответ.цитаты = [ц.to_dict() for ц in разбор.цитаты]
        подтверждено = bool(cite.подтверждённые(разбор))
        if разбор.не_знаю or (not подтверждено and not по_git) or разбор.ошибка_формата:
            ответ.не_знаю = True
            причина = ("в документации и в git ответа нет" if разбор.не_знаю
                       else "модель не уложила ответ в обязательный формат (JSON) и после повтора"
                       if разбор.ошибка_формата
                       else "ответ не подтверждается ни цитатами из документации, ни выводом git")
            if разбор.ошибка_формата:
                ответ.пометки.append("ответ модели не в формате JSON — показан «не знаю», а не сырой текст")
            ответ.уточнение = разбор.уточнение
            ответ.текст = f"Не знаю: {причина}." + (f" {разбор.уточнение}" if разбор.уточнение else "")
            return
        ответ.текст = разбор.ответ
        for n in разбор.источники:
            if 1 <= n <= len(фрагменты):
                п = фрагменты[n - 1].чанк.паспорт()
                ответ.источники.append({"n": n, "source": п.get("source", ""), "section": п.get("section", ""),
                                        "chunk_id": п.get("chunk_id", "")})

    @staticmethod
    def _по_git(разбор, вывод_git: str, ответ: ОтветАссистента) -> bool:
        """Ответ опирается на git: был успешный вызов инструмента или пометка «(git)» при живом git
        (вопрос «на какой ветке» модель отвечает по блоку состояния, без вызова). Ответ не в формате
        JSON — никогда: иначе сырой оборванный JSON уходил в карточку (показ Дня 31, дубль 2)."""
        if разбор.ошибка_формата:
            return False
        return bool(вывод_git.strip()) or ("(git)" in (разбор.ответ or "") and "error" not in ответ.git)

    @staticmethod
    def _нарушения(разбор, всего: int, опора: str, по_git: bool) -> list[str]:
        нарушено = cite.нарушения(разбор, всего, опора)
        if по_git:
            # Ответ из git: источников и цитат из документации может не быть; числа — из вывода
            # git или посчитаны по нему («16 принципов» — подсчёт по файлу из git_show, буквально
            # числа в файле нет: проверка чисел заставляла модель убрать верный ответ, показ, дубль 5).
            нарушено = [н for н in нарушено if not н.startswith(("пустой список «источники»",
                                                                  "пустой список «цитаты»",
                                                                  "в «ответ» есть числа"))]
        return нарушено

    # --- вход ------------------------------------------------------------------

    def спросить(self, вопрос: str) -> ОтветАссистента:
        начало = time.monotonic()
        ответ = ОтветАссистента(вопрос=вопрос.strip(), проект=self.проект.имя, канал=self.канал,
                                модель=self.модель)
        try:
            if not ответ.вопрос:
                raise ValueError("пустой вопрос: /help вопрос о проекте")
            git_блок = self._контекст_git(ответ)
            локально = catalog.get(self.модель).local
            progress.этап("поиск", подпись=f"индекс проекта «{self.проект.имя}»")
            старт = time.monotonic()
            фрагменты = self._найти(ответ.вопрос, локально)
            ответ.этапы["поиск"] = time.monotonic() - старт
            try:
                self._с_повтором(ответ, self.модель, фрагменты, git_блок, облако=not локально)
            except LLMError as e:
                if локально or not self.запасная:
                    raise
                # Запасной путь: облако не ответило и после повтора — локальная модель, с пометкой.
                ответ.запасной = prompts.ЗАПАСНОЙ.format(причина=str(e)[:120], модель=self.запасная)
                ответ.пометки.append(ответ.запасной)
                ответ.вызовы = ответ.вызовы[:2]
                старт = time.monotonic()
                фрагменты = self._найти(ответ.вопрос, True)
                ответ.этапы["поиск"] += time.monotonic() - старт
                self._ответить(ответ, self.запасная, фрагменты, git_блок)
        except Exception as e:  # noqa: BLE001 — понятная ошибка вместо падения канала
            log.warning("ассистент: %s", e)
            ответ.ошибка = str(e)[:300]
            ответ.текст = f"Ассистент не смог ответить: {str(e)[:200]}"
        ответ.этапы["всего"] = time.monotonic() - начало
        self._маска(ответ)
        self._метрики(ответ)
        return ответ

    def _с_повтором(self, ответ: ОтветАссистента, модель: str, фрагменты: list[Any], git_блок: str,
                    облако: bool) -> None:
        try:
            self._ответить(ответ, модель, фрагменты, git_блок)
        except LLMError:
            if not облако:
                raise
            self.пауза(ПАУЗА_ПОВТОРА_С)
            ответ.вызовы = ответ.вызовы[:2]
            ответ.пометки.append("облако не ответило — повтор")
            self._ответить(ответ, модель, фрагменты, git_блок)

    def _маска(self, ответ: ОтветАссистента) -> None:
        def скрыть(текст: str) -> str:
            итог, виды = pd.скрыть(текст or "", профиль="точный")
            for вид, n in виды.items():
                ответ.скрыто_пд[вид] = ответ.скрыто_пд.get(вид, 0) + n
            return итог
        ответ.текст = скрыть(ответ.текст)
        ответ.уточнение = скрыть(ответ.уточнение)
        for ц in ответ.цитаты:
            ц["text"] = скрыть(ц.get("text", ""))

    def _метрики(self, ответ: ОтветАссистента) -> None:
        try:
            metrics.записать({
                "канал": ответ.канал, "проект": ответ.проект, "модель": ответ.модель,
                "запасной": bool(ответ.запасной),
                "инструменты": [в.имя for в in ответ.вызовы[2:]], "не_знаю": ответ.не_знаю,
                "вход": ответ.токенов_вход, "ответ": ответ.токенов_выход,
                "git_с": round(ответ.этапы.get("git", 0), 2), "поиск_с": round(ответ.этапы.get("поиск", 0), 2),
                "модель_с": round(ответ.этапы.get("модель", 0), 2), "всего_с": round(ответ.этапы.get("всего", 0), 2),
                "фрагментов": ответ.фрагментов, "цитат": sum(1 for ц in ответ.цитаты if ц.get("confirmed")),
                "код": "ошибка" if ответ.ошибка else "ок",
                "ошибка": ответ.ошибка.split(":")[0][:60] if ответ.ошибка else ""},
                self.журнал)
        except OSError as e:
            log.warning("метрики ассистента не записаны: %s", e)

    def закрыть(self) -> None:
        self.git.закрыть()
