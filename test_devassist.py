"""Тесты Дня 31 — ассистент разработчика (specs/031-dev-assistant).

Запуск — в составе безопасного набора: `.venv/bin/python -B run_tests.py`.
Настоящий проект (CRM) в тестах не участвует: репозитории git — временные.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from agent.service import pd


class ТочныйПрофильПД(unittest.TestCase):
    """plan D-02a: маска для документации проекта не портит техническую латиницу."""

    def setUp(self) -> None:
        self.каталог = tempfile.TemporaryDirectory()
        путь = os.path.join(self.каталог.name, "pd-names.txt")
        with open(путь, "w", encoding="utf-8") as f:
            f.write("# тестовый список\nЗаглушкин\n")
        self.окружение = mock.patch.dict(os.environ, {"PD_NAMES": путь})
        self.окружение.start()

    def tearDown(self) -> None:
        self.окружение.stop()
        self.каталог.cleanup()

    def test_техническая_латиница_не_трогается(self) -> None:
        for текст in ("Только ORM-выражения поддерева", "из «позже» в MVP владельца",
                      "адрес IP общества", "`BODY.PEEK[]` и POST запрос", "по JSON, а не по HTML",
                      "sha256:04878004256f6dae989736e437b6fb074cb4e6c3"):
            with self.subTest(текст=текст):
                итог, виды = pd.скрыть(текст, профиль="точный")
                self.assertEqual(итог, текст)
                self.assertEqual(виды, {})

    def test_полный_профиль_по_умолчанию_прежний(self) -> None:
        # Полный профиль ловит латинских двойников — потому и портит «MVP владельца».
        итог, _ = pd.скрыть("M.B Заглушкин подписал")
        self.assertNotIn("Заглушкин", итог)
        self.assertEqual(pd.скрыть("M.B Заглушкин подписал"), pd.скрыть("M.B Заглушкин подписал", профиль="полный"))

    def test_точный_ловит_настоящие_пд(self) -> None:
        случаи = {
            "список «Иван Заглушкин», и всё": "фамилия из списка",
            "автор ЗАГЛУШКИН ответил": "фамилия из списка",
            "подписал А.В. Петров вчера": "инициалы и фамилия",
            "подписал Петров А.В. вчера": "фамилия и инициалы",
            "пишите на admin@corp.example.ru": "e-mail",
            "звоните +7 (473) 123-45-67": "телефон",
            "Ирина Сергеевна сказала": "отчество",
        }
        for текст, вид in случаи.items():
            with self.subTest(текст=текст):
                итог, виды = pd.скрыть(текст, профиль="точный")
                self.assertIn(pd.МАСКА, итог)
                self.assertIn(вид, виды)

    def test_точный_не_поднимает_ошибку_на_неоднозначном(self) -> None:
        with self.assertRaises(pd.ОшибкаМаскирования):
            pd.скрыть("Подписал Анна Сергеевна Неизвестнова")
        итог, виды = pd.скрыть("Подписал Анна Сергеевна Неизвестнова", профиль="точный")
        self.assertIn(pd.МАСКА, итог)
        self.assertNotIn("Сергеевна", итог)

    def test_фамилия_перед_инициалами_и_строчное_слово(self) -> None:
        # «Петров А.В. вчера» читалось как «А.В. вчера»: маска съедала инициалы, фамилия
        # оставалась открытой — и в полном профиле (/doc Дня 30), и в точном.
        for профиль in pd.ПРОФИЛИ:
            for текст in ("подписал Петров А.В. вчера", "подписал А.В. Петров вчера"):
                with self.subTest(профиль=профиль, текст=текст):
                    итог, _ = pd.скрыть(текст, профиль=профиль)
                    self.assertNotIn("Петров", итог)
                    self.assertIn("вчера", итог)

    def test_неизвестный_профиль(self) -> None:
        with self.assertRaises(ValueError):
            pd.скрыть("текст", профиль="мягкий")


def временный_репозиторий(каталог: str, файлы: dict[str, str]) -> str:
    """git init + коммит файлов во временном каталоге; вернуть путь."""
    import subprocess
    for путь, текст in файлы.items():
        полный = os.path.join(каталог, путь)
        os.makedirs(os.path.dirname(полный), exist_ok=True)
        with open(полный, "w", encoding="utf-8") as f:
            f.write(текст)
    окр = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    for команда in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "начало"]):
        subprocess.run(["git", "-C", каталог, *команда], check=True, env=окр, capture_output=True)
    return каталог


class ПроектыИИндекс(unittest.TestCase):
    """spec FR-01–FR-03, plan D-01, D-02."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        корень = self.tmp.name
        names = os.path.join(корень, "pd-names.txt")
        with open(names, "w", encoding="utf-8") as f:
            f.write("Заглушкин\n")
        self.окружение = mock.patch.dict(os.environ, {"PD_NAMES": names})
        self.окружение.start()
        self.репо = временный_репозиторий(os.path.join(корень, "proj"), {
            "README.md": "# Проект\n\nСтек — Django. Автор Иван Заглушкин. Только ORM-выражения.\n",
            "docs/guide.md": "# Гид\n\n## Раздел\n\nТекст раздела про MVP.\n",
            "docs/reviews/r1.md": "# Ревью\n\nне индексировать\n",
            "specs/001-a/spec.md": "# Спека\n\nтребования\n",
            "backend/apps/x/models.py": "class Заявка:\n    поле = 1\n",
            "backend/apps/x/views.py": "def вид():\n    pass\n",
            "HANDOFF.md": "рабочий журнал\n",
        })
        with open(os.path.join(self.репо, "docs", "untracked.md"), "w", encoding="utf-8") as f:
            f.write("не под git\n")
        self.конфиг = os.path.join(корень, "projects.json")
        import json
        with open(self.конфиг, "w", encoding="utf-8") as f:
            json.dump({"по_умолчанию": "t", "проекты": {"t": {
                "путь": self.репо, "описание": "тест", "индекс": "idx/t",
                "включить": ["README.md", "docs/*.md", "docs/**/*.md", "specs/**/*.md", "backend/**/models.py"],
                "исключить": ["docs/reviews/**", "HANDOFF.md"]}}}, f, ensure_ascii=False)

    def tearDown(self) -> None:
        self.окружение.stop()
        self.tmp.cleanup()

    def test_загрузка_и_ошибки(self) -> None:
        from agent.devassist import project
        п = project.загрузить(путь=self.конфиг)
        self.assertEqual(п.имя, "t")
        self.assertEqual(п.индекс, os.path.join(self.tmp.name, "idx", "t"))
        with self.assertRaises(project.ОшибкаПроекта):
            project.загрузить("нет-такого", путь=self.конфиг)
        with mock.patch.dict(os.environ, {"PROJECT_T_DIR": "/нет/такого/каталога"}):
            with self.assertRaises(project.ОшибкаПроекта):
                project.файлы(project.загрузить(путь=self.конфиг))

    def test_только_файлы_под_git_по_спискам(self) -> None:
        from agent.devassist import project
        п = project.загрузить(путь=self.конфиг)
        self.assertEqual(project.файлы(п), ["README.md", "backend/apps/x/models.py", "docs/guide.md",
                                            "specs/001-a/spec.md"])

    def test_маска_до_индекса_и_повторная_подготовка(self) -> None:
        from agent.devassist import project
        п = project.загрузить(путь=self.конфиг)
        отчёт = project.подготовить(п)
        self.assertEqual(len(отчёт["изменено"]), 4)
        self.assertEqual(отчёт["замены_пд"], {"фамилия из списка": 1})
        with open(os.path.join(п.src, "README.md"), encoding="utf-8") as f:
            текст = f.read()
        self.assertNotIn("Заглушкин", текст)
        self.assertIn("ORM-выражения", текст)          # точный профиль, не полный
        повтор = project.подготовить(п)
        self.assertEqual((повтор["изменено"], повтор["удалено"]), ([], []))
        with open(os.path.join(self.репо, "docs", "guide.md"), "a", encoding="utf-8") as f:
            f.write("\nещё строка\n")
        os.remove(os.path.join(self.репо, "specs", "001-a", "spec.md"))
        import subprocess
        subprocess.run(["git", "-C", self.репо, "rm", "-q", "--cached", "specs/001-a/spec.md"], check=True)
        третий = project.подготовить(п)
        self.assertEqual(третий["изменено"], ["docs/guide.md"])
        self.assertEqual(третий["удалено"], ["specs/001-a/spec.md"])
        self.assertFalse(os.path.exists(os.path.join(п.src, "specs", "001-a", "spec.md")))

    def test_индекс_источник_путь_в_проекте(self) -> None:
        from agent.devassist import project
        п = project.загрузить(путь=self.конфиг)
        отчёт = project.построить(п, эмбеддер="хэш")
        self.assertGreater(отчёт["фрагментов"], 0)
        self.assertTrue(project.индекс_есть(п))
        найдено = project.индексатор(п, "хэш").найти("Заявка поле", "структура", 10)
        источники = {ф.чанк.паспорт()["source"] for ф in найдено}
        self.assertEqual(источники, {"README.md", "backend/apps/x/models.py", "docs/guide.md",
                                     "specs/001-a/spec.md"})
        второй = project.построить(п, эмбеддер="хэш")
        self.assertEqual(второй["из_кэша"], второй["фрагментов"])

    def test_git_только_без_блокировок(self) -> None:
        from agent.devassist import project
        окр = project.окружение_git()
        self.assertEqual((окр["GIT_OPTIONAL_LOCKS"], окр["GIT_TERMINAL_PROMPT"]), ("0", "0"))


class GitСервер(unittest.TestCase):
    """spec FR-04, plan D-03: git_server.py — только чтение, белый список, пределы."""

    @classmethod
    def setUpClass(cls) -> None:
        import subprocess
        cls.tmp = tempfile.TemporaryDirectory()
        cls.репо = временный_репозиторий(os.path.join(cls.tmp.name, "r"), {
            "README.md": "# Проект\n", "specs/001/spec.md": "# Спека\n", "docs/a.md": "# А\n"})
        окр = dict(os.environ, GIT_AUTHOR_NAME="Автор Секретов", GIT_AUTHOR_EMAIL="a@b",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL=os.devnull)
        with open(os.path.join(cls.репо, "docs", "a.md"), "a", encoding="utf-8") as f:
            f.write("вторая строка\n")
        subprocess.run(["git", "-C", cls.репо, "commit", "-qam", "вторая правка"], check=True, env=окр)
        # Ловушки локального конфига: внешний diff, textconv и fsmonitor — программы, которые
        # git исполнил бы при чтении. Сервер не должен их запускать.
        cls.метка = os.path.join(cls.tmp.name, "запущено")
        скрипт = os.path.join(cls.tmp.name, "ловушка.sh")
        with open(скрипт, "w", encoding="utf-8") as f:
            f.write(f"#!/bin/sh\necho $0 >> '{cls.метка}'\n")
        os.chmod(скрипт, 0o755)
        for ключ, значение in (("diff.external", скрипт), ("core.fsmonitor", скрипт),
                               ("diff.ловушка.textconv", скрипт)):
            subprocess.run(["git", "-C", cls.репо, "config", ключ, значение], check=True)
        with open(os.path.join(cls.репо, ".gitattributes"), "w", encoding="utf-8") as f:
            f.write("*.md diff=ловушка\n")
        with open(os.path.join(cls.репо, "README.md"), "a", encoding="utf-8") as f:
            f.write("незакоммиченное\n")
        cls.окружение = mock.patch.dict(os.environ, {"GIT_ROOT": cls.репо})
        cls.окружение.start()
        import git_server
        cls.м = git_server

    @classmethod
    def tearDownClass(cls) -> None:
        cls.окружение.stop()
        cls.tmp.cleanup()

    def вызвать(self, имя: str, аргументы: dict | None = None):
        import anyio
        from mcp import Client

        async def вызов():
            async with Client(self.м.сервер) as клиент:
                return await клиент.call_tool(имя, аргументы or {})

        return anyio.run(вызов)

    def test_инструменты_только_чтение(self) -> None:
        import anyio
        from mcp import Client

        async def список():
            async with Client(self.м.сервер) as клиент:
                return (await клиент.list_tools()).tools

        инструменты = anyio.run(список)
        self.assertEqual({и.name for и in инструменты},
                         {"git_branch", "git_status", "git_ls_files", "git_diff", "git_log", "git_show",
                          "git_changes", "git_grep"})   # два последних — День 32, ревью PR
        for и in инструменты:
            with self.subTest(и.name):
                self.assertTrue(и.annotations.read_only_hint)
                self.assertFalse(и.annotations.destructive_hint)
                self.assertFalse(и.annotations.open_world_hint)

    def test_ветка_статус_файлы(self) -> None:
        в = self.м.ветка()
        self.assertEqual(в["ветка"], "main")
        self.assertFalse(в["отделённый"])
        с = self.м.статус()
        self.assertFalse(с["чисто"])
        self.assertTrue(any("README.md" in ф for ф in с["файлы"]))
        self.assertEqual(self.м.файлы("specs/**")["файлы"], ["specs/001/spec.md"])
        self.assertEqual(self.м.файлы("*.md")["файлы"], ["README.md"])   # * — внутри каталога
        self.assertEqual(self.м.файлы("", предел=1)["обрезано"], True)

    def test_diff_и_журнал(self) -> None:
        рабочий = self.м.разница()
        self.assertIn("незакоммиченное", рабочий["diff"])
        между = self.м.разница("HEAD~1", "HEAD")
        self.assertIn("вторая строка", между["diff"])
        self.assertIn("docs/a.md", между["сводка"])
        ж = self.м.журнал(5)
        self.assertEqual([к["тема"] for к in ж["коммиты"]], ["вторая правка", "начало"])
        self.assertNotIn("Автор", str(ж))                                 # авторов нет
        with mock.patch.object(self.м, "ПРЕДЕЛ_DIFF", 10):
            self.assertTrue(self.м.разница()["обрезано"])

    def test_файл_из_коммита(self) -> None:
        ф = self.м.файл("docs/a.md")
        self.assertIn("вторая строка", ф["текст"])
        self.assertNotIn("незакоммиченное", self.м.файл("README.md")["текст"])   # из HEAD, не с диска
        self.assertNotIn("вторая строка", self.м.файл("docs/a.md", "HEAD~1")["текст"])
        for плохой, слово in (("docs", "не файл"), ("../x", "вне проекта"), ("*.md", "без шаблонов"),
                              ("нет.md", "нет")):
            with self.subTest(плохой):
                with self.assertRaises(self.м.ОшибкаЗапроса) as о:
                    self.м.файл(плохой)
                self.assertIn(слово, str(о.exception))
        with mock.patch.object(self.м, "ПРЕДЕЛ_ФАЙЛА", 3):
            self.assertTrue(self.м.файл("docs/a.md")["обрезано"])

    def test_ловушки_конфига_не_исполняются(self) -> None:
        self.м.статус(); self.м.разница(); self.м.разница("HEAD~1", "HEAD"); self.м.журнал()
        self.м.файл("docs/a.md")
        self.assertFalse(os.path.exists(self.метка), "git исполнил программу из локального конфига")

    def test_отказы_понятны(self) -> None:
        for имя, аргументы, слово in (
                ("git_diff", {"from_ref": "--output=/tmp/x"}, "не ссылка"),
                ("git_diff", {"from_ref": "нет-такой-ветки"}, "не ссылка"),
                ("git_diff", {"from_ref": "no-such-branch"}, "нет"),
                ("git_ls_files", {"pattern": "../etc"}, "вне проекта"),
                ("git_ls_files", {"pattern": "/etc/passwd"}, "вне проекта"),
                ("git_log", {"path": ":(top)x"}, "вне проекта")):
            with self.subTest(аргументы=аргументы):
                итог = self.вызвать(имя, аргументы)
                self.assertTrue(итог.is_error)
                self.assertIn(слово, итог.content[0].text)
        with self.assertRaises(self.м.ОшибкаЗапроса):
            self.м._git("commit", "-m", "x")
        with self.assertRaises(self.м.ОшибкаЗапроса):
            self.м._git("push")

    def test_без_блокировок_и_пароля(self) -> None:
        окр = self.м.окружение()
        self.assertEqual((окр["GIT_OPTIONAL_LOCKS"], окр["GIT_TERMINAL_PROMPT"]), ("0", "0"))
        индекс = os.path.join(self.репо, ".git", "index")
        до = os.stat(индекс).st_mtime_ns
        for _ in range(3):
            self.м.статус(); self.м.ветка(); self.м.разница()
        self.assertEqual(os.stat(индекс).st_mtime_ns, до)

    def test_корень_не_задан(self) -> None:
        with mock.patch.dict(os.environ, {"GIT_ROOT": ""}):
            with self.assertRaises(self.м.ОшибкаЗапроса):
                self.м.ветка()


# --- подмены для ассистента ------------------------------------------------------

class _Ответ:
    def __init__(self, текст: str = "", вызовы=None, вход: int = 100, выход: int = 20) -> None:
        self.text, self.tool_calls = текст, list(вызовы or [])
        self.prompt_tokens, self.completion_tokens = вход, выход
        self.message = {"role": "assistant", "content": текст,
                        "tool_calls": [{"id": в.ид, "type": "function",
                                        "function": {"name": в.имя, "arguments": "{}"}} for в in self.tool_calls]}


class _Клиент:
    """Сценарий ответов модели; запоминает, кого и с чем звали."""

    def __init__(self, ответы) -> None:
        self.ответы, self.звали = list(ответы), []

    def call(self, модель, сообщения, max_tokens=0, temperature=None, tools=None, **_):
        import copy
        self.звали.append({"модель": модель, "сообщения": copy.deepcopy(сообщения), "tools": tools})
        ответ = self.ответы.pop(0)
        if isinstance(ответ, Exception):
            raise ответ
        return ответ


class _Git:
    имя = "подмена"

    def __init__(self, сломан: bool = False, вывод: str = "", данные=None) -> None:
        self.сломан, self.вывод, self.данные, self.вызовы = сломан, вывод, данные or {}, []

    def инструменты(self):
        return [{"type": "function", "function": {"name": и, "description": и, "parameters": {"type": "object"}}}
                for и in ("git__git_ls_files", "git__git_diff", "git__git_log")]

    def вызвать(self, имя, аргументы=None):
        self.вызовы.append((имя, аргументы or {}))
        if self.сломан:
            raise RuntimeError("сервер git не подключился")
        if имя == "git__git_branch":
            return True, "{}", {"ветка": "main", "коммит": "abc1234", "отслеживает": "main...origin/main"}
        if имя == "git__git_status":
            return True, "{}", {"чисто": True, "изменено": 0}
        return True, self.вывод, self.данные


class _Чанк:
    def __init__(self, n: int, источник: str, текст: str) -> None:
        self.текст, self._п = текст, {"source": источник, "section": "Раздел", "chunk_id": f"c{n}"}

    def паспорт(self):
        return dict(self._п)


class _Находка:
    def __init__(self, n, источник, текст) -> None:
        self.чанк = _Чанк(n, источник, текст)


class _Поисковик:
    def __init__(self, фрагменты) -> None:
        self.фрагменты = фрагменты
        self.индексатор = mock.Mock(найти=mock.Mock(return_value=[]))

    def найти(self, вопрос, настройки):
        return mock.Mock(фрагменты=list(self.фрагменты))


ФРАГМЕНТ = "Стек проекта — Django 5 и HTMX, база PostgreSQL 17, очереди на Redis и RQ."


def _json(ответ, источники=(), цитаты=(), не_знаю=False, уточнение=""):
    import json
    return json.dumps({"ответ": ответ, "источники": list(источники), "не_знаю": не_знаю, "уточнение": уточнение,
                       "цитаты": [{"n": n, "текст": т} for n, т in цитаты]}, ensure_ascii=False)


class Ассистент(unittest.TestCase):
    """spec FR-06, FR-12, FR-14; plan D-04, D-07, D-09 — на подменах, без сети и модели."""

    def setUp(self) -> None:
        from agent.devassist.project import Проект
        self.tmp = tempfile.TemporaryDirectory()
        names = os.path.join(self.tmp.name, "pd-names.txt")
        with open(names, "w", encoding="utf-8") as f:
            f.write("Заглушкин\n")
        self.окружение = mock.patch.dict(os.environ, {"PD_NAMES": names})
        self.окружение.start()
        self.журнал = os.path.join(self.tmp.name, "metrics.jsonl")
        self.проект = Проект("t", self.tmp.name, "Тестовый портал", индекс=self.tmp.name)
        self.паузы = []

    def tearDown(self) -> None:
        self.окружение.stop()
        self.tmp.cleanup()

    def ассистент(self, ответы, git=None, фрагменты=None, модель="ds-flash"):
        from agent.devassist.assistant import Ассистент as А

        class Тестовый(А):
            def _настройки(self, локально):
                return None
        self.клиент = _Клиент(ответы)
        self.git = git or _Git()
        поиск = _Поисковик(фрагменты if фрагменты is not None else [_Находка(1, "README.md", ФРАГМЕНТ)])
        return Тестовый(self.проект, self.клиент, self.git, модель=модель, канал="тест", журнал=self.журнал,
                        поисковик=поиск, пауза=self.паузы.append)

    def test_ответ_по_документам_ветка_в_промпте_метрики_без_текстов(self) -> None:
        а = self.ассистент([_Ответ(_json("Django 5 и HTMX [1]", [1], [(1, ФРАГМЕНТ[:60])]))])
        о = а.спросить("Какой стек у проекта, секретный вопрос?")
        self.assertFalse(о.не_знаю)
        self.assertEqual(о.источники[0]["source"], "README.md")
        self.assertEqual(о.git["branch"], "main")
        промпт = self.клиент.звали[0]["сообщения"][1]["content"]
        self.assertIn("ветка: main", промпт)
        self.assertIn("[1] README.md", промпт)
        with open(self.журнал, encoding="utf-8") as f:
            строка = f.read()
        self.assertNotIn("секретный", строка)
        self.assertNotIn("Django", строка)
        self.assertIn('"канал": "тест"', строка)

    def test_круг_инструмента_git_и_ответ_из_git(self) -> None:
        from agent.llm import ToolCall
        гит = _Git(вывод="abc1234\t2026-10-14\tпоследняя правка", данные={"коммиты": [{}]})
        а = self.ассистент([_Ответ("", [ToolCall("t1", "git__git_log", {"count": 1})]),
                            _Ответ(_json("Последний коммит — «последняя правка» (git)"))], git=гит)
        о = а.спросить("Что менялось в последнем коммите?")
        self.assertFalse(о.не_знаю, о.текст)
        self.assertIn(("git__git_log", {"count": 1}), гит.вызовы)
        self.assertEqual([в.имя for в in о.вызовы][-1], "git__git_log")
        второй = self.клиент.звали[1]["сообщения"]
        self.assertEqual(второй[-1]["role"], "tool")
        self.assertIn("последняя правка", второй[-1]["content"])

    def test_локальной_модели_короче_вывод_и_меньше_кругов(self) -> None:
        from agent.devassist.assistant import КРУГОВ_ЛОКАЛЬНО, ПРЕДЕЛ_ИНСТРУМЕНТА_ЛОКАЛЬНО
        from agent.llm import ToolCall
        гит = _Git(вывод="x" * 10000, данные={})
        просьбы = [_Ответ("", [ToolCall(f"t{i}", "git__git_show", {"path": "a.md"})]) for i in range(КРУГОВ_ЛОКАЛЬНО)]
        а = self.ассистент(просьбы + [_Ответ(_json("Файл прочитан (git)"))], git=гит, модель="loc-rag-docs")
        а.спросить("что в файле?")
        self.assertEqual(len(self.клиент.звали), КРУГОВ_ЛОКАЛЬНО + 1)
        вывод = self.клиент.звали[1]["сообщения"][-1]["content"]
        self.assertLess(len(вывод), ПРЕДЕЛ_ИНСТРУМЕНТА_ЛОКАЛЬНО + 100)
        self.assertIn("обрезан", вывод)

    def test_обещание_без_вызова_напоминание(self) -> None:
        from agent.llm import ToolCall
        гит = _Git(вывод="# Конституция\n16 принципов", данные={"путь": "docs/constitution.md", "строк": 169})
        а = self.ассистент([_Ответ(_json("По фрагментам не сосчитать. Посмотрю файл целиком в git.")),
                            _Ответ("", [ToolCall("t1", "git__git_show", {"path": "docs/constitution.md"})]),
                            _Ответ(_json("В конституции 16 принципов (git)"))], git=гит)
        о = а.спросить("Сколько принципов?")
        self.assertIn(("git__git_show", {"path": "docs/constitution.md"}), гит.вызовы)
        self.assertIn("16", о.текст)
        self.assertTrue(any("напомнили" in п for п in о.пометки))
        self.assertEqual(self.клиент.звали[1]["сообщения"][-1]["role"], "user")    # напоминание

    def test_локальная_сбилась_на_вызове_инструмента(self) -> None:
        from agent.llm import LLMError
        а = self.ассистент([LLMError("Ollama ответила 500: XML syntax error"),
                            _Ответ(_json("Django 5 [1]", [1], [(1, ФРАГМЕНТ[:60])]))], модель="loc-rag-docs")
        о = а.спросить("Какой стек?")
        self.assertFalse(о.не_знаю, о.текст)
        self.assertIsNone(self.клиент.звали[1]["tools"])
        self.assertTrue(any("сбилась на вызове инструмента" in п for п in о.пометки))

    def test_меняющий_инструмент_не_вызывается(self) -> None:
        from agent.llm import ToolCall
        а = self.ассистент([_Ответ("", [ToolCall("t1", "git__git_commit", {})]),
                            _Ответ(_json("Такого инструмента нет: git только на чтение (git)"))])
        о = а.спросить("Закоммить изменения")
        self.assertNotIn("git__git_commit", [и for и, _ in self.git.вызовы])
        отказ = [в for в in о.вызовы if в.имя == "git__git_commit"][0]
        self.assertFalse(отказ.ок)
        self.assertIn("только на чтение", self.клиент.звали[1]["сообщения"][-1]["content"])

    def test_предел_кругов(self) -> None:
        from agent.devassist.assistant import КРУГОВ
        from agent.llm import ToolCall
        просьбы = [_Ответ("", [ToolCall(f"t{i}", "git__git_log", {})]) for i in range(КРУГОВ)]
        а = self.ассистент(просьбы + [_Ответ(_json("Коммиты есть (git)"))])
        а.спросить("история?")
        self.assertEqual(len(self.клиент.звали), КРУГОВ + 1)
        self.assertIsNone(self.клиент.звали[-1]["tools"])

    def test_не_знаю_без_подтверждения(self) -> None:
        а = self.ассистент([_Ответ(_json("Есть интеграция с 1С [1]", [1], [(1, "интеграция с 1С:ЗУП включена в MVP")])),
                            _Ответ(_json("Есть интеграция с 1С [1]", [1], [(1, "интеграция с 1С:ЗУП включена в MVP")]))])
        о = а.спросить("Есть ли интеграция с 1С:ЗУП?")
        self.assertTrue(о.не_знаю)
        self.assertTrue(о.текст.startswith("Не знаю"))
        self.assertEqual(len(self.клиент.звали), 2)      # один повторный запрос с нарушениями

    def test_оборванный_json_не_уходит_в_ответ(self) -> None:
        оборван = '{"ответ": "Проект устроен так (git): раз, два, три'
        а = self.ассистент([_Ответ(оборван), _Ответ(оборван)])
        о = а.спросить("Как устроен проект?")
        self.assertTrue(о.не_знаю)
        self.assertNotIn('{"ответ"', о.текст)
        self.assertTrue(any("не в формате JSON" in п for п in о.пометки))

    def test_запасной_путь_облако_недоступно(self) -> None:
        from agent.llm import LLMError
        а = self.ассистент([LLMError("сеть"), LLMError("сеть"),
                            _Ответ(_json("Django 5 [1]", [1], [(1, ФРАГМЕНТ[:60])]))])
        о = а.спросить("Какой стек?")
        self.assertEqual([з["модель"] for з in self.клиент.звали], ["ds-flash", "ds-flash", "loc-rag-docs"])
        self.assertEqual(self.паузы, [3.0])
        self.assertIn("ответила локальная модель", о.запасной)
        self.assertEqual(о.модель, "loc-rag-docs")
        self.assertFalse(о.не_знаю)

    def test_git_недоступен_ответ_по_документам(self) -> None:
        а = self.ассистент([_Ответ(_json("Django 5 [1]", [1], [(1, ФРАГМЕНТ[:60])]))], git=_Git(сломан=True))
        о = а.спросить("Какой стек?")
        self.assertFalse(о.не_знаю)
        self.assertTrue(any("git недоступен" in п for п in о.пометки))
        self.assertIn("недоступно", self.клиент.звали[0]["сообщения"][1]["content"])
        self.assertIsNone(self.клиент.звали[0]["tools"])

    def test_маска_пд_на_выходе_и_в_выводе_git(self) -> None:
        from agent.llm import ToolCall
        гит = _Git(вывод="правку сделал Заглушкин", данные={})
        а = self.ассистент([_Ответ("", [ToolCall("t1", "git__git_log", {})]),
                            _Ответ(_json("Правку сделал Заглушкин (git)"))], git=гит)
        о = а.спросить("кто правил?")
        self.assertNotIn("Заглушкин", self.клиент.звали[1]["сообщения"][-1]["content"])
        self.assertNotIn("Заглушкин", о.текст)
        self.assertIn("фамилия из списка", о.скрыто_пд)

    def test_пустой_вопрос_понятная_ошибка(self) -> None:
        о = self.ассистент([]).спросить("  ")
        self.assertIn("пустой вопрос", о.ошибка)


class _ПодменаАссистента:
    """Вместо настоящего ассистента в тестах консоли и веба: без модели, git и индекса."""

    def __init__(self, вопросы: list) -> None:
        self.вопросы = вопросы

    def спросить(self, вопрос):
        from agent.devassist.assistant import ВызовGit, ОтветАссистента
        self.вопросы.append(вопрос)
        о = ОтветАссистента(вопрос=вопрос, проект="crm", канал="тест", модель="ds-flash",
                            текст="Стек — Django 5 и HTMX [1].",
                            источники=[{"n": 1, "source": "README.md", "section": "Стек", "chunk_id": "c1"}],
                            цитаты=[{"n": 1, "text": "Стек: Django 5 + HTMX", "check": "дословно", "confirmed": True}],
                            git={"branch": "main", "commit": "abc1234", "clean": True, "changed": 0})
        о.вызовы = [ВызовGit("git__git_branch"), ВызовGit("git__git_status"),
                    ВызовGit("git__git_log", {"count": 3}, True, 0.1, "коммитов 3")]
        о.этапы = {"git": 0.1, "поиск": 0.2, "модель": 1.0, "всего": 1.3}
        return о

    def закрыть(self) -> None:
        pass


class КомандаHelp(unittest.TestCase):
    """spec FR-08, FR-09; plan D-06: /help в консольном чате и на странице — один разбор."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib
        cls.каталог = tempfile.mkdtemp(prefix="help-test-")
        cls.прежний = os.environ.get("MEMORY_DIR")
        os.environ["MEMORY_DIR"] = cls.каталог
        import web as модуль
        cls.web = importlib.reload(модуль)
        cls.клиент = cls.web.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil
        if cls.прежний is None:
            os.environ.pop("MEMORY_DIR", None)
        else:
            os.environ["MEMORY_DIR"] = cls.прежний
        shutil.rmtree(cls.каталог, ignore_errors=True)

    def setUp(self) -> None:
        self.вопросы = []
        self.подмена = mock.patch("agent.devassist.command.создать",
                                  side_effect=lambda *a, **k: _ПодменаАссистента(self.вопросы))
        self.создать = self.подмена.start()

    def tearDown(self) -> None:
        self.подмена.stop()

    def test_разбор(self) -> None:
        from agent.devassist import command
        self.assertEqual(command.разобрать("/help"), ("справка", ""))
        self.assertEqual(command.разобрать("  /HELP  "), ("справка", ""))
        self.assertEqual(command.разобрать("/help Где модели?"), ("вопрос", "Где модели?"))
        self.assertEqual(command.разобрать("/help@AIChBot где\nмодели"), ("вопрос", "где\nмодели"))
        for не in ("/helper", "help me", "/память", "что такое /help"):
            self.assertIsNone(command.разобрать(не), не)

    def test_консоль_справка_и_вопрос(self) -> None:
        import contextlib
        import io
        import cli
        вывод = io.StringIO()
        with contextlib.redirect_stdout(вывод):
            код = cli.выполнить(self.web.agent, cli.build_parser().parse_args(
                ["--чат", "кон", "--реплика", "/help", "--реплика", "/help Какой стек?"]))
        self.assertEqual(код, 0)
        текст = вывод.getvalue()
        self.assertIn("/help вопрос", текст)
        self.assertIn("Стек — Django 5", текст)
        self.assertIn("[1] README.md", текст)
        self.assertIn("git_log(count=3)", текст)
        self.assertEqual(self.вопросы, ["Какой стек?"])
        self.assertEqual(self.создать.call_args.args[1], "консоль")
        история = self.web.agent.чат("кон").история()
        self.assertEqual([с["content"][:5] for с in история][-2:], ["/help", "Стек "])
        self.assertEqual(self.web.agent.чат("кон").память.реплик, 0)   # память задачи не тронута

    def test_веб_без_потока_и_с_потоком(self) -> None:
        import json
        справка = self.клиент.post("/api/chat/say", json={"session": "веб", "text": "/help"}).get_json()
        self.assertIn("/help вопрос", справка["help_text"])
        итог = self.клиент.post("/api/chat/say", json={"session": "веб", "text": "/help Какой стек?"}).get_json()
        self.assertEqual(итог["help"]["answer"], "Стек — Django 5 и HTMX [1].")
        self.assertEqual(итог["state"]["помощь"][-1]["after_turn"], 0)
        self.assertEqual(self.создать.call_args.args[1], "веб")
        поток = self.клиент.post("/api/chat/say", json={"session": "веб", "text": "/help Ещё?", "stream": True})
        строки = [json.loads(с) for с in поток.get_data(as_text=True).splitlines() if с.strip()]
        self.assertEqual(строки[-1]["событие"], "готово")
        self.assertEqual(len(строки[-1]["state"]["помощь"]), 2)
        новый = self.клиент.post("/api/chat/new", json={"session": "веб"}).get_json()
        self.assertTrue(новый["reset"]["помощь_стёрта"])
        self.assertEqual(новый["state"]["помощь"], [])

    def test_страница_рисует_карточку(self) -> None:
        html = self.клиент.get("/chat").get_data(as_text=True)
        self.assertIn("function помощьHTML", html)
        self.assertIn("подсказка-помощи", html)


def _ручка_проекта_тесты():
    """Классы на основе _ОсноваШлюза из tests.py — импорт здесь, чтобы tests.py не грузился зря."""
    import tests as дн30

    class РучкаПроекта(дн30._ОсноваШлюза):
        """spec FR-10, FR-11, SC-06; plan D-08: /v1/project/help — тот же путь, что документы."""

        def setUp(self):
            super().setUp()
            from fastapi.testclient import TestClient
            from agent.service import gateway
            self.к_проект = self.ключи.создать("проект", ["проект"])
            self.вопросы, self.режим = [], {"режим": "облако", "модель": "ds-flash"}
            self.сервис.помощник = self._помочь
            self.сервис.режим_проекта = self._режим
            self.клиент = TestClient(gateway.создать_приложение(self.сервис), base_url="https://testserver",
                                     client=("192.168.1.31", 50000))

        def _помочь(self, вопрос):
            self.вопросы.append(вопрос)
            if вопрос == "сломайся":
                raise RuntimeError("секретный текст ошибки")
            return {"object": "project.help", "model": self.режим["модель"], "answer": "Стек — Django 5.",
                    "sources": [{"n": 1, "source": "README.md"}], "usage": {"prompt_tokens": 900, "completion_tokens": 40}}

        def _режим(self, значение):
            if значение:
                self.режим = {"режим": значение, "модель": "ds-flash" if значение == "облако" else "loc-rag-docs"}
            return dict(self.режим)

        def спросить(self, ключ, вопрос="Какой стек?"):
            return self.клиент.post("/v1/project/help", headers=self.h(ключ), json={"question": вопрос})

        def test_права_и_ответ(self):
            self.assertEqual(self.клиент.post("/v1/project/help", json={"question": "x"}).status_code, 401)
            r = self.спросить(self.к_док)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "permission_denied"))
            r = self.спросить(self.к_проект)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["object"], r.json()["model"], r.json()["answer"]),
                             ("project.help", "ds-flash", "Стек — Django 5."))
            self.assertEqual(self.вопросы, ["Какой стек?"])
            self.assertEqual(self.сервис.очередь.состояние()["идёт"], 0)

        def test_ошибки_и_журнал_без_текстов(self):
            r = self.спросить(self.к_проект, "сломайся")
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (502, "project_error"))
            self.assertNotIn("секретный", r.text)
            r = self.спросить(self.к_проект, "x" * 5000)
            self.assertEqual((r.status_code, r.json()["error"]["code"]), (413, "question_too_long"))
            self.спросить(self.к_проект, "Какой стек у секретного проекта?")
            with open(self.журнал_путь, encoding="utf-8") as f:
                журнал = f.read()
            self.assertNotIn("секрет", журнал)
            self.assertIn('"ручка": "/v1/project/help"', журнал)
            self.assertIn('"модель": "ds-flash"', журнал)

        def test_лимит_ключа(self):
            коды = [self.спросить(self.к_проект).status_code for _ in range(7)]
            self.assertEqual(коды[-1], 429)

        def test_переключатель_админа(self):
            r = self.клиент.post("/admin/api/project-model", headers=self.h(self.к_проект), json={"режим": "локально"})
            self.assertEqual(r.status_code, 403)
            r = self.клиент.post("/admin/api/project-model", headers=self.h(self.к_админ), json={"режим": "всё"})
            self.assertEqual(r.status_code, 400)
            r = self.клиент.post("/admin/api/project-model", headers=self.h(self.к_админ), json={"режим": "локально"})
            self.assertEqual(r.json()["модель"], "loc-rag-docs")
            self.assertEqual(self.спросить(self.к_проект).json()["model"], "loc-rag-docs")
            состояние = self.клиент.get("/admin/api/state", headers=self.h(self.к_админ)).json()
            self.assertEqual(состояние["project"]["режим"], "локально")

    return РучкаПроекта


РучкаПроекта = _ручка_проекта_тесты()


def _бот_тесты():
    import tests as дн30
    from agent.service import telegram as tg

    class БотHelp(unittest.IsolatedAsyncioTestCase):
        """spec FR-13; plan D-10: «/help» без текста — мгновенная справка, «/help вопрос» — ручка проекта.

        Помощники — из БотTelegram Дня 30 (без наследования: его тесты не прогоняются дважды)."""

        ВЛАДЕЛЕЦ = дн30.БотTelegram.ВЛАДЕЛЕЦ
        бот, обн, прогон, журнал = (дн30.БотTelegram.бот, дн30.БотTelegram.обн, дн30.БотTelegram.прогон,
                                    дн30.БотTelegram.журнал)

        def setUp(self):
            дн30.БотTelegram.setUp(self)
            self.names = os.path.join(self.каталог, "pd-names.txt")
            with open(self.names, "w", encoding="utf-8") as f:
                f.write("Заглушкин\n")
            окружение = mock.patch.dict(os.environ, {"PD_NAMES": self.names})
            окружение.start()
            self.addCleanup(окружение.stop)

        def ответ_проекта(self, текст="Стек — Django 5; выражения ORM, автор Заглушкин."):
            return (200, {"x-queue-wait": "1.5"}, {
                "object": "project.help", "model": "ds-flash", "answer": текст,
                "git": {"branch": "main", "commit": "abc1234", "clean": True, "changed": 0},
                "sources": [{"n": 1, "source": "README.md", "section": "Стек"}],
                "tool_calls": [{"name": "git__git_branch"}, {"name": "git__git_status"},
                               {"name": "git__git_log", "args": {"count": 3}, "ok": True, "summary": "коммитов 3"}],
                "notes": [], "usage": {"prompt_tokens": 900, "completion_tokens": 40}})

        def test_мгновенность(self):
            self.assertTrue(tg.мгновенная_команда("/help"))
            self.assertTrue(tg.мгновенная_команда("/help@AIChBot"))
            self.assertFalse(tg.мгновенная_команда("/help Какой стек?"))
            self.assertFalse(tg.мгновенная_команда("/help@AIChBot Какой стек?"))
            self.assertTrue(tg.мгновенная_команда("/status"))

        async def test_справка_упоминает_help_вопрос(self):
            await self.прогон(self.бот(), "/help")
            self.assertIn("/help вопрос", self.tg.тексты("sendMessage")[-1])
            self.assertEqual([п for п, _ in self.сервис.запросы], [])

        async def test_вопрос_уходит_в_ручку_проекта_под_точной_маской(self):
            self.сервис.документы.append(self.ответ_проекта())
            await self.прогон(self.бот(), "/help Какой стек?")
            self.assertEqual(self.сервис.запросы[-1], ("/v1/project/help", {"question": "Какой стек?"}))
            итог = self.tg.тексты("editMessageText")[-1]
            self.assertIn("Django 5", итог)
            self.assertIn("ORM", итог)                      # точный профиль не портит латиницу
            self.assertNotIn("Заглушкин", итог)
            self.assertIn("ветка main", итог)
            self.assertIn("git_log(count=3)", итог)
            запись = [з for з in self.журнал() if з.get("вид") == "проект"][-1]
            self.assertEqual((запись["код"], запись["скрыто"]), (200, 1))
            self.assertNotIn("Django", str(self.журнал()))   # журнал бота без текстов

        async def test_без_права_проект_понятный_отказ(self):
            self.сервис.документы.append(дн30._отказ(403, "permission_denied"))
            await self.прогон(self.бот(), "/help Какой стек?")
            итог = self.tg.тексты("editMessageText")[-1]
            self.assertIn("нет права", итог)
            self.assertIn("--телеграм-ключ", итог)

        def test_сборка_отказывает_на_мусоре(self):
            self.assertEqual(tg.собрать_проект({"answer": {"не": "строка"}}, 0, 0)[0], tg.ОТКАЗ_ПД)

    return БотHelp


БотHelp = _бот_тесты()


class ПереключательИПредохранитель(unittest.TestCase):
    """spec FR-11, NFR-03; plan D-08: модель проекта без перезапуска; облако — только разрешённый хост."""

    def test_модель_проекта_по_переключателю(self):
        from agent.service import config
        with tempfile.TemporaryDirectory() as к, mock.patch.dict(os.environ, {"MEMORY_DIR": к}):
            н = config.загрузить()
            self.assertEqual((н.проект.модель, н.проект.запасная, н.проект.облачный_хост),
                             ("ds-flash", "loc-rag-docs", "api.deepseek.com"))
            self.assertEqual(config.модель_проекта(н), "ds-flash")
            self.assertEqual(config.задать_модель_проекта(н, "локально")["модель"], "loc-rag-docs")
            self.assertEqual(config.режим_проекта(н)["режим"], "локально")
            config.задать_модель_проекта(н, "облако")
            self.assertEqual(config.модель_проекта(н), "ds-flash")
            with self.assertRaises(ValueError):
                config.задать_модель_проекта(н, "любая")

    def test_облачная_модель_только_с_разрешённым_хостом(self):
        from agent.llm import LLMError
        from agent.local import offline
        прежний = offline.включён()
        try:
            offline.включить(True, "тест")
            with mock.patch.dict(os.environ, {"OFFLINE_ALLOW_EXTERNAL": ""}):
                with self.assertRaises(LLMError):
                    offline.проверить_модель("ds-flash")
            with mock.patch.dict(os.environ, {"OFFLINE_ALLOW_EXTERNAL": "api.deepseek.com"}):
                offline.проверить_модель("ds-flash")              # пропущена
                with self.assertRaises(LLMError):
                    offline.проверить_модель("groq-120b")          # другой облачный хост — отказ
        finally:
            offline.включить(прежний, "тест")

    def test_право_проект_в_ключах(self):
        from agent.service import keys
        self.assertIn("проект", keys.ПРАВА)


class СлужбыДня31(unittest.TestCase):
    """spec FR-15, plan D-11: службы day31-* вместо day30-*; бот запускается после сервиса."""

    def test_имена_и_порядок(self):
        from agent.service import manage, unit
        self.assertEqual((unit.ИМЯ, unit.ИМЯ_БОТА), ("day31-llm.service", "day31-tg.service"))
        with tempfile.TemporaryDirectory() as к:
            текст = unit.текст_бота(os.path.join(к, "python"), к, os.path.join(к, "memory"))
        self.assertIn("After=network-online.target day31-llm.service", текст)
        self.assertIn("День 31", текст)
        заголовки = [с for с in текст.splitlines() if с.startswith(("Description=", "After="))]
        self.assertFalse([с for с in заголовки if "day30" in с or "День 30" in с], заголовки)
        self.assertEqual(manage.ПРАВА_БОТА, ("чат", "документы", "проект"))


class МетрикиАссистента(unittest.TestCase):
    def test_сводка_и_белый_список(self) -> None:
        from agent.devassist import metrics
        with tempfile.TemporaryDirectory() as к:
            путь = os.path.join(к, "m.jsonl")
            for всего, модель, нз in ((2.0, "ds-flash", False), (4.0, "ds-flash", True), (60.0, "loc-rag-docs", False)):
                metrics.записать({"модель": модель, "канал": "веб", "всего_с": всего, "не_знаю": нз, "код": "ок",
                                  "вопрос": "секрет", "ответ_текст": "секрет"}, путь)
            строки = metrics.прочитать(путь)
            self.assertTrue(all("вопрос" not in с and "ответ_текст" not in с for с in строки))
            с = metrics.сводка(строки)
            self.assertEqual(с["всего"]["ответов"], 3)
            self.assertEqual(с["по_моделям"]["ds-flash"]["p50_с"], 3.0)
            self.assertAlmostEqual(с["по_моделям"]["ds-flash"]["не_знаю"], 0.5)
            self.assertEqual(metrics.перцентиль([1, 2, 3, 4], 0.5), 2.5)
            self.assertAlmostEqual(metrics.перцентиль([1, 2, 3, 4], 0.95), 3.85, delta=0.06)


if __name__ == "__main__":
    unittest.main()
