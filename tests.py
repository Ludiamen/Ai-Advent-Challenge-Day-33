#!/usr/bin/env python3
"""Тесты модели памяти и компонентов проекта.

    .venv/bin/python -B run_tests.py  — безопасный запуск всех пяти модулей
    python tests.py --живые           — намеренно включает проверки внешнего API

Прямой запуск этого файла не включает отдельные регрессии HTTP и Telegram и не
изолирует настоящий .env. Обычный runner работает во временном снимке, блокирует
внешнюю сеть и допускает только локальные подменные сервисы. Флаг ``--живые`` в
runner запрещён и остаётся отдельным осознанным действием.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import catalog, interview, preferences, seed as seed_module
from agent.builder import POLICY, PromptBuilder
from agent.memory.long import LongTermError, LongTermMemory
from agent.memory.manager import LONG, OFF, SHORT, WORKING, MemoryManager
from agent.memory.router import Routing, _parse
from agent.memory.short import ShortTermMemory
from agent.memory.working import (
    DONE, EXECUTION, PLANNING, STAGES, VALIDATION, TaskState, TaskStep, TransitionError,
    WorkingMemory, WorkingMemoryError, ЖДЁТ, ГОТОВ, ЗАКРЫТ_ПЕРЕХОД, ЗАПУСТИТЬ,
    ИЗ_ПЛАНА, ИЗ_СЦЕНАРИЯ, НАРУШЕН_ИНВАРИАНТ, НА_ПЕРЕХОДЕ, НЕТ_СВЕДЕНИЙ,
    НИЧЕГО, ОЖИДАНИЯ, ОТВЕТ, ПОДТВЕРДИТЬ, ПО_КОМАНДЕ, ПРОДОЛЖИТЬ, РЕШЕНИЕ,
)
from agent import transitions as tr
from agent.transitions import (
    БАЗОВЫЕ, ЛИЧНЫЙ, МОДЕЛЬ, СЦЕНАРИЙ, ЧЕЛОВЕК, ConditionStore, TransitionConfigError,
    Условие, Ворота, ПереходОтклонён,
)
from agent.preferences import PreferenceChecker, PreferenceError
from agent.scenarios import Scenario, ScenarioError, ScenarioStore, Step
from agent import invariants as inv
from agent.invariants import Invariant, InvariantError, InvariantStore
from agent.validator import Refusal, StateValidator

ЖИВЫЕ = "--живые" in sys.argv
if ЖИВЫЕ:
    sys.argv.remove("--живые")


class ВременнаяПамять(unittest.TestCase):
    """Общий каркас: каждый тест работает на своей копии памяти."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp(prefix="тест-памяти-")
        self.память = MemoryManager(base_dir=self.каталог, router_mode=OFF)

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)


# --- краткосрочная память -----------------------------------------------------

class КраткосрочнаяПамять(unittest.TestCase):

    def setUp(self) -> None:
        self.память = ShortTermMemory(":memory:")

    def test_окно_ограничено_числом_сообщений(self):
        for i in range(20):
            self.память.append("с", "user" if i % 2 == 0 else "assistant", f"реплика {i}")
        окно = self.память.window("с", max_messages=6)
        self.assertEqual(len(окно), 6)
        self.assertEqual(окно[-1]["content"], "реплика 19")

    def test_окно_ограничено_символами(self):
        self.память.append("с", "user", "х" * 5000)
        self.память.append("с", "assistant", "короткая")
        окно = self.память.window("с", max_messages=10, max_chars=1000)
        # Первая реплика не влезает по символам, но одна запись остаётся всегда:
        # пустое окно хуже, чем окно из одного сообщения.
        self.assertEqual(len(окно), 1)
        self.assertEqual(окно[0]["content"], "короткая")

    def test_сессии_не_смешиваются(self):
        self.память.append("работа", "user", "про работу")
        self.память.append("черновик", "user", "про черновик")
        self.assertEqual(len(self.память.all("работа")), 1)
        self.assertEqual(len(self.память.all("черновик")), 1)

    def test_пустая_реплика_не_сохраняется(self):
        with self.assertRaises(Exception):
            self.память.append("с", "user", "   ")

    def test_неизвестная_роль_отклоняется(self):
        with self.assertRaises(Exception):
            self.память.append("с", "system", "текст")


# --- рабочая память -----------------------------------------------------------

class РабочаяПамять(unittest.TestCase):

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp()
        self.память = WorkingMemory(self.каталог)

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    def test_разрешённый_маршрут_проходит_целиком(self):
        задача = self.память.create("з1", "тест")
        for стадия in (EXECUTION, VALIDATION, DONE):
            задача.transition(стадия)
        self.assertTrue(задача.finished)
        self.assertEqual(len(задача.transitions), 3)

    def test_прыжок_через_стадию_отклоняется(self):
        задача = self.память.create("з2")
        with self.assertRaises(TransitionError):
            задача.transition(DONE)
        self.assertEqual(задача.stage, PLANNING)

    def test_возвраты_разрешены(self):
        задача = self.память.create("з3")
        задача.transition(EXECUTION)
        задача.transition(PLANNING)          # план оказался негодным
        задача.transition(EXECUTION)
        задача.transition(VALIDATION)
        задача.transition(EXECUTION)         # нашли дефект
        self.assertEqual(задача.stage, EXECUTION)

    def test_из_done_никуда(self):
        задача = self.память.create("з4")
        for стадия in (EXECUTION, VALIDATION, DONE):
            задача.transition(стадия)
        self.assertEqual(задача.allowed(), ())
        with self.assertRaises(TransitionError):
            задача.transition(PLANNING)

    def test_состояние_переживает_перезапуск(self):
        задача = self.память.create("з5", "перенос")
        задача.transition(EXECUTION)
        задача.remember("таблиц", "37")
        задача.set_plan(["шаг один", "шаг два"])
        self.память.save(задача)

        другая = WorkingMemory(self.каталог).load("з5")
        self.assertEqual(другая.stage, EXECUTION)
        self.assertEqual(другая.collected["таблиц"], "37")
        self.assertEqual(другая.plan, ["шаг один", "шаг два"])

    def test_повторное_создание_отклоняется(self):
        self.память.create("з6")
        with self.assertRaises(WorkingMemoryError):
            self.память.create("з6")

    def test_недопустимый_идентификатор(self):
        for плохой in ("../побег", "имя с пробелом", "", "a" * 100):
            with self.assertRaises(WorkingMemoryError):
                self.память.create(плохой)

    def test_отсутствующая_задача_даёт_понятную_ошибку(self):
        with self.assertRaises(WorkingMemoryError):
            self.память.load("нет-такой")


class СостояниеЗадачи(unittest.TestCase):
    """Три части состояния: этап, шаг и ожидаемое действие."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp()
        self.память = WorkingMemory(self.каталог)

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    def _с_шагами(self, ид="з"):
        задача = self.память.create(ид, "проверка")
        задача.set_steps([
            TaskStep(1, "аналитик", ИЗ_СЦЕНАРИЯ, PLANNING, может_спросить=True),
            TaskStep(2, "backend", ИЗ_СЦЕНАРИЯ, EXECUTION),
        ], сценарий="проба")
        return задача

    def test_свежая_задача_ждёт_запуска(self):
        задача = self.память.create("новая")
        self.assertEqual(задача.ожидание, ЗАПУСТИТЬ)
        # Пояснение заполняется само, иначе интерфейс печатает «ожидание: — ».
        self.assertTrue(задача.ожидание_текст)

    def test_указатель_шага_двигается_при_завершении(self):
        задача = self._с_шагами()
        self.assertEqual(задача.шаг.имя, "аналитик")
        задача.начать_шаг()
        self.assertEqual(задача.шаг.состояние, "идёт")
        задача.закончить_шаг("требования собраны")
        self.assertEqual(задача.шаг.имя, "backend")
        self.assertEqual(задача.шаги[0].состояние, "готов")
        self.assertIn("требования", задача.шаги[0].выжимка)

    def test_пройденные_шаги_видно_по_состоянию(self):
        задача = self._с_шагами()
        задача.закончить_шаг("раз")
        задача.закончить_шаг("два")
        self.assertTrue(задача.шаги_пройдены)

    def test_пауза_не_меняет_этап(self):
        # Пауза — флаг поверх стадии, а не пятая стадия автомата.
        задача = self._с_шагами()
        задача.transition(EXECUTION)
        задача.остановить(ПО_КОМАНДЕ)
        self.assertTrue(задача.пауза)
        self.assertEqual(задача.stage, EXECUTION)
        self.assertEqual(задача.allowed(), (VALIDATION, PLANNING))

    def test_пауза_снимается(self):
        задача = self._с_шагами()
        задача.остановить(НА_ПЕРЕХОДЕ, ПОДТВЕРДИТЬ, "перейти к исполнению?")
        self.assertEqual(задача.ожидание, ПОДТВЕРДИТЬ)
        задача.продолжить()
        self.assertFalse(задача.пауза)
        self.assertEqual(задача.ожидание, ПРОДОЛЖИТЬ)

    def test_ответ_снимает_паузу_и_ложится_отдельно(self):
        задача = self._с_шагами()
        задача.остановить(НЕТ_СВЕДЕНИЙ, ОТВЕТ, "какой SRID?")
        задача.ответить("3857")
        self.assertFalse(задача.пауза)
        self.assertEqual(len(задача.ответы), 1)
        self.assertEqual(задача.ответы[0]["ответ"], "3857")
        # Ответ человека — не то же, что собранное агентом.
        self.assertEqual(задача.collected, {})

    def test_пустой_ответ_отклоняется(self):
        задача = self._с_шагами()
        задача.остановить(НЕТ_СВЕДЕНИЙ, ОТВЕТ, "какой SRID?")
        with self.assertRaises(WorkingMemoryError):
            задача.ответить("   ")

    def test_неизвестное_ожидание_отклоняется(self):
        задача = self._с_шагами()
        with self.assertRaises(WorkingMemoryError):
            задача.ждать("подумать")

    def test_неизвестная_причина_паузы_отклоняется(self):
        задача = self._с_шагами()
        with self.assertRaises(WorkingMemoryError):
            задача.остановить("настроение")

    def test_смена_этапа_сбрасывает_прежнее_ожидание(self):
        # Иначе после перехода на экране остаётся «подтвердите переход».
        задача = self._с_шагами()
        задача.остановить(НА_ПЕРЕХОДЕ, ПОДТВЕРДИТЬ, "перейти?")
        задача.продолжить()
        задача.transition(EXECUTION)
        self.assertEqual(задача.ожидание, ПРОДОЛЖИТЬ)

    def test_завершение_ставит_ожидание_ничего(self):
        задача = self._с_шагами()
        for стадия in (EXECUTION, VALIDATION, DONE):
            задача.transition(стадия)
        self.assertEqual(задача.ожидание, НИЧЕГО)

    def test_состояние_переживает_запись_и_чтение(self):
        # Главное свойство: продолжить можно из другого процесса.
        задача = self._с_шагами("живучая")
        задача.начать_шаг()
        задача.закончить_шаг("готово")
        задача.запрос = "исходный запрос"
        задача.остановить(НЕТ_СВЕДЕНИЙ, ОТВЕТ, "какой SRID?")
        self.память.save(задача)

        другая = WorkingMemory(self.каталог).load("живучая")
        self.assertTrue(другая.пауза)
        self.assertEqual(другая.ожидание, ОТВЕТ)
        self.assertEqual(другая.текущий_шаг, 2)
        self.assertEqual(другая.запрос, "исходный запрос")
        # Шаги должны подняться объектами, а не словарями.
        self.assertIsInstance(другая.шаг, TaskStep)
        self.assertEqual(другая.шаг.имя, "backend")

    def test_план_превращается_в_шаги(self):
        задача = self.память.create("ручная", "без сценария")
        задача.set_plan(["выписать таблицы", "описать модели"])
        self.assertEqual(задача.шагов, 2)
        self.assertEqual(задача.шаг.источник, ИЗ_ПЛАНА)

    def test_план_не_затирает_шаги_сценария(self):
        задача = self._с_шагами()
        задача.set_plan(["посторонний пункт"])
        self.assertEqual([ш.имя for ш in задача.шаги], ["аналитик", "backend"])

    def test_состояние_словами_называет_все_три_части(self):
        задача = self._с_шагами()
        строка = задача.состояние_словами
        for кусок in ("этап", "шаг", "ждём"):
            self.assertIn(кусок, строка)

    def test_все_ожидания_имеют_пояснение(self):
        from agent.memory.working import ОЖИДАНИЯ_СЛОВАМИ
        self.assertEqual(set(ОЖИДАНИЯ), set(ОЖИДАНИЯ_СЛОВАМИ))


# --- долговременная память ----------------------------------------------------

class ДолговременнаяПамять(unittest.TestCase):

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp()
        self.память = LongTermMemory(self.каталог, "кто-то")

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    def test_каждое_хранилище_в_своём_файле(self):
        self.память.profile.update("ограничения", "бд", "PostgreSQL")
        self.память.decisions.add("Стек", "Django")
        self.память.knowledge.add("ф1", "факт")
        self.память.scenarios.add(Scenario(имя="с", шаги=[Step("а", "делай")]))
        self.память.conditions.add(Условие(
            код="своё", откуда="validation", куда="done", что="есть-в-собранном",
            значение="ссылка", правило="в готово — только со ссылкой на репозиторий"))
        пути = set(self.память.files().values())
        # Профиль, сценарии, решения и знания — четыре разных файла: у каждого
        # свой режим записи, и смешивать их значит терять это различие.
        # Пятый файл — личные условия перехода, заведённые в этом дне.
        self.assertEqual(len(пути), 5)
        for путь in пути:
            self.assertTrue(os.path.exists(путь), путь)

    def test_профиль_перезаписывается_а_решения_дописываются(self):
        self.память.profile.update("ограничения", "бд", "PostgreSQL 16")
        self.память.profile.update("ограничения", "бд", "PostgreSQL 17")
        self.assertEqual(self.память.profile.load()["ограничения"]["бд"], "PostgreSQL 17")

        self.память.decisions.add("Первое", "текст один")
        self.память.decisions.add("Второе", "текст два")
        self.assertEqual(len(self.память.decisions.all()), 2)

    def test_имя_пользователя_не_выводит_за_каталог(self):
        # Имя пользователя превращается в путь. Без проверки «--кто ../../чужой»
        # записывает профиль за пределы каталога памяти — проверено, записывал.
        for плохое in ("../../чужой", "кто/то", "..", "", "a" * 100):
            with self.assertRaises(LongTermError, msg=плохое):
                LongTermMemory(self.каталог, плохое)

    def test_обычное_имя_принимается(self):
        память = LongTermMemory(self.каталог, "инженер-2")
        self.assertTrue(os.path.realpath(память.directory).startswith(
            os.path.realpath(self.каталог)))

    def test_неизвестный_раздел_профиля(self):
        with self.assertRaises(LongTermError):
            self.память.profile.update("настроение", "тон", "бодрый")

    def test_недопустимое_значение_предпочтения_отклоняется(self):
        # Записать «длина: очень кратко» значило бы сохранить то, что не попадёт
        # ни в промпт, ни в проверку, — и никто бы не понял почему.
        with self.assertRaises(LongTermError):
            self.память.profile.update("формат", "длина", "очень кратко")

    def test_профиль_дополняется_умолчаниями_при_чтении(self):
        профиль = self.память.profile.load()
        for раздел in preferences.SECTIONS:
            for поле in preferences.fields(раздел):
                self.assertIn(поле.key, профиль[раздел])

    def test_жёсткий_инвариант_без_значений_отклоняется(self):
        # Инвариант, который нечем проверить, хуже, чем его отсутствие:
        # он создаёт ложное чувство защиты.
        with self.assertRaises(LongTermError):
            self.память.profile.add_invariant(
                {"код": "пустой", "правило": "нельзя", "тип": "запрет-слов", "значения": []}
            )

    def test_негодная_регулярка_отклоняется(self):
        with self.assertRaises(LongTermError):
            self.память.profile.add_invariant(
                {"код": "битый", "правило": "нельзя", "тип": "запрет-регулярок",
                 "значения": ["[незакрытая"]}
            )

    def test_инвариант_обновляется_по_коду(self):
        for правило in ("первая версия", "вторая версия"):
            self.память.profile.add_invariant(
                {"код": "один", "правило": правило, "тип": "мягкий"}
            )
        правила = self.память.profile.invariants()
        self.assertEqual(len(правила), 1)
        self.assertEqual(правила[0]["правило"], "вторая версия")

    def test_знания_отбираются_по_релевантности(self):
        self.память.knowledge.add("схема", "Схема gissys: account, group, organization",
                                  tags=["planning", "бд"])
        self.память.knowledge.add("фронт", "OpenLayers 2.13 рисует слои", tags=["execution"])
        отобрано = [ф["id"] for ф in self.память.knowledge.relevant("что в схеме gissys")]
        self.assertEqual(отобрано, ["схема"])

    def test_несовпавший_запрос_не_тянет_ничего(self):
        self.память.knowledge.add("схема", "Схема gissys", tags=["planning"])
        self.assertEqual(self.память.knowledge.relevant("погода в Москве"), [])

    def test_факт_уточняется_а_не_дублируется(self):
        self.память.knowledge.add("в", "PostGIS 3.4")
        self.память.knowledge.add("в", "PostGIS 3.6")
        факты = self.память.knowledge.all()
        self.assertEqual(len(факты), 1)
        self.assertEqual(факты[0]["текст"], "PostGIS 3.6")


# --- правила маршрутизации ----------------------------------------------------

class ПравилаМаршрутизации(ВременнаяПамять):

    def test_реплика_идёт_в_короткую_память(self):
        self.память.remember_message("user", "вопрос")
        self.assertEqual(self.память.stats()[SHORT]["реплик"], 1)
        последняя = self.память.journal(1)[0]
        self.assertEqual(последняя["правило"], "реплика-диалога")
        self.assertEqual(последняя["слой"], SHORT)

    def test_шаг_задачи_идёт_в_рабочую_память(self):
        задача = self.память.working.create("з", "тест")
        self.память.remember_step(задача, "таблиц", "37")
        self.assertEqual(self.память.working.load("з").collected["таблиц"], "37")
        self.assertEqual(self.память.journal(1)[0]["слой"], WORKING)

    def test_явное_указание_идёт_куда_сказано(self):
        self.память.remember_explicit("знания", "В gisdata 37 таблиц", key="состав")
        запись = self.память.journal(1)[0]
        self.assertEqual(запись["правило"], "явное-указание")
        self.assertEqual(запись["подслой"], "знания")
        self.assertEqual(len(self.память.long.knowledge.all()), 1)

    def test_явное_указание_в_неизвестный_слой_отклоняется(self):
        with self.assertRaises(LongTermError):
            self.память.remember_explicit("подсознание", "что-то")

    def test_свёртка_идёт_на_выбранной_модели(self):
        # Иначе пять шагов честно идут на выбранной модели, а свёртка в конце
        # уходит к своей роли — и роняет весь прогон на последнем шаге.
        задача = self.память.working.create("з", "перенос")
        for стадия in (EXECUTION, VALIDATION):
            задача.transition(стадия)
        self.память.working.save(задача)

        class Считающий:
            def __init__(self): self.модели = []
            spent = {"calls": 0, "tokens": 0, "cost": 0.0}
            def call(self, model_key, messages, **kwargs):
                from agent.llm import Reply
                self.модели.append(model_key)
                return Reply(text='{"заголовок":"и","решение":"р","причина":"п"}',
                             model_key=model_key)
            def close(self): pass

        считающий = Считающий()
        self.память.client = считающий
        self.память.finish_task(задача, model_key="ds-flash")
        self.assertEqual(считающий.модели, ["ds-flash"])

    def test_без_выбора_свёртка_идёт_по_роли(self):
        задача = self.память.working.create("з2", "перенос")
        for стадия in (EXECUTION, VALIDATION):
            задача.transition(стадия)
        self.память.working.save(задача)
        self.assertEqual(self.память.summarizer_model, catalog.for_role("сжатие", offset=0))

    def test_завершение_задачи_переносит_её_в_решения(self):
        задача = self.память.working.create("з", "перенос моделей")
        self.память.remember_step(задача, "итог", "модели описаны")
        for стадия in (EXECUTION, VALIDATION):
            задача.transition(стадия)
        self.память.working.save(задача)

        запись = self.память.finish_task(задача)
        self.assertEqual(len(self.память.long.decisions.all()), 1)
        self.assertIn("перенос моделей", запись["заголовок"])
        # Рабочая память задачи очищена: её итог теперь живёт в журнале решений.
        self.assertEqual(self.память.working.tasks(), [])

    def test_очистка_диалога_не_трогает_другие_слои(self):
        self.память.remember_message("user", "реплика")
        задача = self.память.working.create("з")
        self.память.remember_step(задача, "к", "з")
        self.память.remember_explicit("знания", "факт", key="ф")

        self.память.short.clear(self.память.session)
        сводка = self.память.stats()
        self.assertEqual(сводка[SHORT]["реплик"], 0)
        self.assertEqual(сводка[WORKING]["задач"], 1)
        self.assertEqual(сводка[LONG]["знаний"], 1)

    def test_маршрутизатор_выключен_ничего_не_пишет(self):
        было = self.память.long.profile.load()
        предложение, запись = self.память.route("Отвечай кратко")
        self.assertFalse(предложение.wants_write)
        self.assertFalse(запись["применено"])
        # Профиль не пуст даже без записей: предпочтения всегда имеют умолчания.
        # Значит, проверять надо неизменность, а не пустоту.
        self.assertEqual(self.память.long.profile.load()["формат"], было["формат"])

    def test_отклонённое_предложение_видно_в_журнале(self):
        # Ниже порога — записи нет, но след остаётся: потом видно, что именно
        # агент решил не запоминать.
        self.память.router_mode = "авто"
        self.память.router = _ЗаглушкаМаршрутизатора(
            Routing(target="знания", key="к", value="факт", confidence=0.3)
        )
        предложение, запись = self.память.route("какая-то реплика")
        self.assertFalse(запись["применено"])
        self.assertIn("ниже порога", запись["причина"])
        self.assertEqual(len(self.память.long.knowledge.all()), 0)

    def test_уверенное_предложение_применяется(self):
        self.память.router_mode = "авто"
        self.память.router = _ЗаглушкаМаршрутизатора(
            Routing(target="знания", key="версия", value="PostGIS 3.6", confidence=0.9)
        )
        _, запись = self.память.route("у нас PostGIS 3.6")
        self.assertTrue(запись["применено"])
        self.assertEqual(self.память.long.knowledge.all()[0]["текст"], "PostGIS 3.6")

    def test_выдуманный_раздел_профиля_не_роняет_ответ(self):
        """Найдено при проходе чек-листа Дня 16: на «Перепиши сервис на Laravel»
        маршрутизатор предложил записать реплику в раздел профиля «переписать».
        Хранилище верно отказало, но исключение не ловилось, и страница вместо
        мгновенного отказа по инварианту получила ошибку 500."""
        self.память.router_mode = "авто"
        self.память.router = _ЗаглушкаМаршрутизатора(
            Routing(target="профиль", section="переписать", key="сервис",
                    value="на Laravel", confidence=0.9)
        )
        было = self.память.long.profile.load()
        предложение, запись = self.память.route("Перепиши сервис на Laravel")
        self.assertFalse(запись["применено"])
        self.assertIn("предложение отклонено", запись["причина"])
        self.assertIn("переписать", запись["причина"])
        self.assertEqual(self.память.long.profile.load(), было)

    def test_сбой_маршрутизатора_не_ломает_запись(self):
        self.память.router_mode = "авто"
        self.память.router = _ЗаглушкаМаршрутизатора(Routing(failed=True))
        предложение, запись = self.память.route("реплика")
        self.assertTrue(предложение.failed)
        self.assertFalse(запись["применено"])


class _ЗаглушкаМаршрутизатора:
    """Маршрутизатор с заранее известным ответом — чтобы тесты не ходили в сеть."""

    def __init__(self, routing: Routing) -> None:
        self.routing = routing

    def classify(self, text: str) -> Routing:
        return self.routing


class _СчётчикМаршрутизатора(_ЗаглушкаМаршрутизатора):
    """Та же заглушка, но помнит, сколько раз её звали."""

    def __init__(self, routing: Routing) -> None:
        super().__init__(routing)
        self.вызовы: list[str] = []

    def classify(self, text: str) -> Routing:
        self.вызовы.append(text)
        return self.routing


# --- разбор ответа маршрутизатора ---------------------------------------------

class РазборОтветаМодели(unittest.TestCase):

    def test_чистый_json(self):
        разбор = _parse('{"слой":"знания","ключ":"к","значение":"з","уверенность":0.8}')
        self.assertEqual(разбор.target, "знания")
        self.assertAlmostEqual(разбор.confidence, 0.8)

    def test_json_в_markdown(self):
        разбор = _parse('```json\n{"слой":"профиль","раздел":"стиль","значение":"кратко",'
                        '"уверенность":0.9}\n```')
        self.assertEqual(разбор.target, "профиль")
        self.assertEqual(разбор.section, "стиль")

    def test_json_с_болтовнёй_вокруг(self):
        разбор = _parse('Конечно! Вот ответ: {"слой":"нет","уверенность":0} — надеюсь, помог.')
        self.assertEqual(разбор.target, "нет")

    def test_хвост_после_объекта_не_мешает(self):
        # Слабые модели присылают валидный объект и следом обрывок служебного
        # тега. Срез «от первой { до последней }» на этом ломается.
        разбор = _parse('{"слой":"нет","уверенность":0}</think>{обрывок')
        self.assertEqual(разбор.target, "нет")

    def test_берётся_первый_из_двух_объектов(self):
        разбор = _parse('{"слой":"знания","значение":"факт","уверенность":0.9} {"слой":"нет"}')
        self.assertEqual(разбор.value, "факт")

    def test_скобка_внутри_строки_не_обрывает_разбор(self):
        разбор = _parse('{"слой":"знания","значение":"вот } скобка","уверенность":0.9}')
        self.assertEqual(разбор.value, "вот } скобка")

    def test_мусор_даёт_none(self):
        self.assertIsNone(_parse("я не понял вопроса"))

    def test_неизвестный_слой_даёт_none(self):
        self.assertIsNone(_parse('{"слой":"подсознание","уверенность":1}'))

    def test_уверенность_загоняется_в_границы(self):
        self.assertEqual(_parse('{"слой":"нет","уверенность":7}').confidence, 1.0)
        self.assertEqual(_parse('{"слой":"нет","уверенность":-3}').confidence, 0.0)


# --- сборка промпта -----------------------------------------------------------

class СборкаПромпта(ВременнаяПамять):

    def setUp(self) -> None:
        super().setUp()
        seed_module.seed(self.память)
        self.память.remember_message("user", "прошлая реплика")
        self.сборщик = PromptBuilder(self.память)

    def test_выключенный_слой_не_даёт_записей(self):
        промпт = self.сборщик.build("вопрос", layers={SHORT})
        self.assertNotIn(LONG, промпт.by_layer())
        причины = [б.why for б in промпт.blocks if б.layer == LONG]
        self.assertTrue(all("выключена" in п for п in причины))

    def test_инварианты_идут_всегда(self):
        for стадия in (PLANNING, EXECUTION, VALIDATION, DONE):
            задача = TaskState(task_id="з", stage=стадия)
            промпт = self.сборщик.build("вопрос", задача)
            блок = [б for б in промпт.blocks if б.name == "инварианты"][0]
            self.assertTrue(блок.included, f"инварианты пропали на стадии {стадия}")

    @staticmethod
    def _фактов(промпт) -> int:
        """Сколько записей знаний попало в промпт (0, если блок не включён)."""
        блоки = [б for б in промпт.included if б.name == "знания"]
        return len(блоки[0].entries) if блоки else 0

    def test_знания_зависят_от_стадии(self):
        # Факты о системе нужны, когда строят план, и мешают, когда проверяют
        # уже написанный код. Политика стадий именно это и задаёт.
        вопрос = "как перенести схему gissys"
        планирование = self.сборщик.build(вопрос, TaskState("з", stage=PLANNING))
        проверка = self.сборщик.build(вопрос, TaskState("з", stage=VALIDATION))
        self.assertGreater(self._фактов(планирование), self._фактов(проверка))

    def test_на_завершённой_задаче_знаний_нет(self):
        промпт = self.сборщик.build("итог?", TaskState("з", stage=DONE))
        self.assertEqual(self._фактов(промпт), 0)

    def test_настройки_идут_на_всех_стадиях(self):
        # Иначе стадия, где профиль не показали, гарантированно дала бы
        # расхождение с ним и лишний повтор.
        for стадия in (PLANNING, EXECUTION, VALIDATION, DONE):
            промпт = self.сборщик.build("вопрос", TaskState("з", stage=стадия))
            блок = [б for б in промпт.blocks if б.name == "настройки пользователя"][0]
            self.assertTrue(блок.included, f"настройки пропали на стадии {стадия}")

    def test_на_промежуточном_шаге_настроек_нет(self):
        промпт = self.сборщик.build("вопрос", personal=False)
        блок = [б for б in промпт.blocks if б.name == "настройки пользователя"][0]
        self.assertFalse(блок.included)
        self.assertIn("следующий агент", блок.why)

    def test_роль_шага_попадает_в_ядро(self):
        промпт = self.сборщик.build("вопрос", step_role="РОЛЬ НА ЭТОМ ШАГЕ: аналитик.")
        блок = [б for б in промпт.blocks if б.name == "роль шага"][0]
        self.assertTrue(блок.included)
        имена = [б.name for б in промпт.blocks]
        self.assertLess(имена.index("роль агента"), имена.index("роль шага"))

    def test_план_и_собранное_попадают_на_исполнении(self):
        задача = TaskState("з", stage=EXECUTION, plan=["шаг раз"], collected={"к": "з"})
        промпт = self.сборщик.build("вопрос", задача)
        имена = {б.name for б in промпт.included}
        self.assertIn("план", имена)
        self.assertIn("собранные данные", имена)

    def test_трейс_объясняет_каждый_блок(self):
        промпт = self.сборщик.build("вопрос")
        for блок in промпт.blocks:
            self.assertTrue(блок.why, f"блок «{блок.name}» без объяснения")

    def test_порядок_блоков_фиксирован(self):
        промпт = self.сборщик.build("вопрос")
        имена = [б.name for б in промпт.blocks]
        self.assertLess(имена.index("инварианты"), имена.index("настройки пользователя"))
        self.assertEqual(имена[-1], "вопрос пользователя")

    def test_без_задачи_берётся_политика_планирования(self):
        промпт = self.сборщик.build("вопрос")
        self.assertEqual(промпт.stage, PLANNING)

    def test_все_стадии_описаны_политикой(self):
        for стадия in (PLANNING, EXECUTION, VALIDATION, DONE):
            self.assertIn(стадия, POLICY)

    def test_шагу_сценария_рабочая_память_не_дублируется(self):
        # Иначе результат предыдущего шага уходит в запрос дважды: как явный
        # вход шага и как собранные данные задачи.
        задача = TaskState("з", stage=EXECUTION, collected={"шаг «аналитик»": "требования"})
        промпт = self.сборщик.build("вопрос", задача, step_role="РОЛЬ: backend")
        блок = [б for б in промпт.blocks if б.name == "собранные данные"][0]
        self.assertFalse(блок.included)
        self.assertIn("получает вход явно", блок.why)

    def test_вне_сценария_собранные_данные_показываются(self):
        задача = TaskState("з", stage=EXECUTION, collected={"к": "з"})
        промпт = self.сборщик.build("вопрос", задача)
        блок = [б for б in промпт.blocks if б.name == "собранные данные"][0]
        self.assertTrue(блок.included)

    def test_длинная_запись_обрезается_для_промпта(self):
        # Рабочая память хранит результат шага целиком, а в промпт идёт столько,
        # сколько туда помещается.
        задача = TaskState("з", stage=EXECUTION, collected={"шаг": "х" * 5000})
        промпт = self.сборщик.build("вопрос", задача)
        блок = [б for б in промпт.included if б.name == "собранные данные"][0]
        self.assertLess(len(блок.text), 3000)
        self.assertIn("обрезано", блок.text)


# --- проверка инвариантов -----------------------------------------------------

class ПроверкаИнвариантов(ВременнаяПамять):

    def setUp(self) -> None:
        super().setUp()
        seed_module.seed(self.память)
        # Валидатор берёт инварианты вызовом: их правят посреди разговора.
        self.валидатор = StateValidator(self.память.all_invariants)

    def test_предложение_чужого_стека_ловится(self):
        нарушения = self.валидатор.check("Возьмём Laravel, на нём быстрее.")
        self.assertEqual(len(нарушения), 1)
        self.assertEqual(нарушения[0].код, "стек-бэкенд")

    def test_отказ_от_чужого_стека_не_считается_нарушением(self):
        чисто = self.валидатор.check(
            "Laravel здесь не подойдёт: геометрия только через сырой SQL, берём GeoDjango."
        )
        self.assertEqual(чисто, [])

    def test_упоминание_legacy_разрешено(self):
        чисто = self.валидатор.check(
            "Контроллер userpgplace.php из CodeIgniter 1 превращается в Django-вьюху."
        )
        self.assertEqual(чисто, [])

    def test_код_на_старом_стеке_ловится(self):
        нарушения = self.валидатор.check('```php\n<?php\n$this->load->model("x");\n```')
        self.assertTrue(нарушения)
        self.assertEqual(нарушения[0].где, "коде")

    def test_чужая_субд_в_коде_ловится(self):
        нарушения = self.валидатор.check("```python\nDATABASES = {'ENGINE': 'mysql'}\n```")
        self.assertTrue(нарушения)

    def test_секрет_в_url_ловится(self):
        нарушения = self.валидатор.check("Дёргайте /api/export?token=abc123")
        self.assertEqual(нарушения[0].код, "секреты-в-url")

    def test_чистый_ответ_проходит(self):
        self.assertEqual(self.валидатор.check(
            "```python\nfrom django.contrib.gis.db import models\n\n"
            "class Pipe(models.Model):\n    geom = models.LineStringField(srid=3857)\n```"
        ), [])

    def test_напоминание_содержит_нарушение(self):
        нарушения = self.валидатор.check("Сделаем на Laravel.")
        напоминание = self.валидатор.reminder(нарушения)
        self.assertIn("laravel", напоминание.lower())

    def test_переход_проверяется_без_изменения_состояния(self):
        задача = TaskState("з", stage=PLANNING)
        можно, пояснение = StateValidator.check_transition(задача, DONE)
        self.assertFalse(можно)
        self.assertIn("не разрешён", пояснение)
        self.assertEqual(задача.stage, PLANNING)   # состояние не тронуто

    def test_смысловые_инварианты_не_проверяются_кодом(self):
        жёсткие = {и.код for и in self.валидатор.hard()}
        self.assertNotIn("1С-источник-истины", жёсткие)
        # Но и не забыты: они проверяются самоотчётом и моделью.
        self.assertIn("1С-источник-истины", {и.код for и in self.валидатор.semantic()})


# --- предпочтения -------------------------------------------------------------

class Предпочтения(unittest.TestCase):

    def test_умолчания_заполняют_все_поля(self):
        каркас = preferences.blank()
        for поле in preferences.FIELDS:
            self.assertIn(поле.key, каркас[поле.section])

    def test_недопустимое_значение_отклоняется(self):
        with self.assertRaises(PreferenceError):
            preferences.set_value(preferences.blank(), "формат", "длина", "как-нибудь")

    def test_неизвестное_поле_отклоняется(self):
        with self.assertRaises(PreferenceError):
            preferences.set_value(preferences.blank(), "формат", "цвет", "синий")

    def test_нормализация_чинит_испорченный_профиль(self):
        # Профиль правят руками и присылают формы: значение может оказаться чем
        # угодно, и дальше по коду оно должно быть уже корректным.
        профиль = preferences.normalize({"формат": {"длина": "ОЧЕНЬ КРАТКО"},
                                         "обращение": {"на_ты": "да"}})
        self.assertEqual(профиль["формат"]["длина"], "подробно")   # умолчание
        self.assertIs(профиль["обращение"]["на_ты"], True)

    def test_предел_длины_соответствует_выбору(self):
        для_кратко = preferences.set_value(preferences.blank(), "формат", "длина", "кратко")
        self.assertEqual(preferences.word_limit(для_кратко), 180)
        подробно = preferences.set_value(preferences.blank(), "формат", "длина", "подробно")
        self.assertEqual(preferences.word_limit(подробно), 0)

    def test_язык_кода_молчит_когда_код_не_нужен(self):
        # Иначе в промпт уходит противоречие: «кода не показывай» и «примеры на
        # Python» одновременно.
        профиль = preferences.set_value(preferences.blank(), "формат", "код", "не_нужен")
        self.assertFalse(any("Python" in с for с in preferences.describe(профиль)))

    def test_каждое_поле_умеет_попасть_в_промпт_или_молчать(self):
        профиль = preferences.normalize(preferences.blank())
        for поле in preferences.FIELDS:
            текст = поле.to_prompt(профиль[поле.section][поле.key])
            self.assertIsInstance(текст, str)


class ПроверкаПредпочтений(unittest.TestCase):

    @staticmethod
    def _профиль(**значения):
        профиль = preferences.blank()
        for путь, значение in значения.items():
            раздел, _, ключ = путь.partition("__")
            профиль = preferences.set_value(профиль, раздел, ключ, значение)
        return профиль

    def коды(self, профиль, ответ):
        return {о.code for о in PreferenceChecker(профиль).check(ответ)}

    def test_длина_считается_без_кода(self):
        # Десять строк модели — это не многословие, а ровно то, что просили.
        профиль = self._профиль(формат__длина="кратко")
        ответ = "Коротко.\n```python\n" + "x = 1\n" * 300 + "```"
        self.assertNotIn("длина", self.коды(профиль, ответ))

    def test_длинный_текст_ловится(self):
        профиль = self._профиль(формат__длина="кратко")
        self.assertIn("длина", self.коды(профиль, "слово " * 300))

    def test_допуск_не_придирается_к_паре_слов(self):
        профиль = self._профиль(формат__длина="кратко")
        self.assertNotIn("длина", self.коды(профиль, "слово " * 190))

    def test_код_запрещён_и_найден(self):
        профиль = self._профиль(формат__код="не_нужен")
        self.assertIn("код", self.коды(профиль, "Вот как:\n```python\nx=1\n```"))

    def test_отсутствие_кода_только_замечание(self):
        профиль = self._профиль(формат__код="обязательно")
        расхождения = PreferenceChecker(профиль).check("Объясню словами.")
        по_коду = [о for о in расхождения if о.code == "код"]
        self.assertTrue(по_коду)
        self.assertFalse(по_коду[0].hard, "требовать код на любой вопрос нельзя")

    def test_обращение_на_вы_при_профиле_на_ты(self):
        профиль = self._профиль(обращение__на_ты=True)
        self.assertIn("на_ты", self.коды(профиль, "Вам нужно перенести вашу таблицу."))

    def test_обращение_на_ты_при_профиле_на_вы(self):
        профиль = self._профиль(обращение__на_ты=False)
        self.assertIn("на_ты", self.коды(профиль, "Тебе нужно перенести твою таблицу."))

    def test_вы_внутри_кода_не_считается(self):
        профиль = self._профиль(обращение__на_ты=True)
        ответ = "Сделай так:\n```python\n# передай вам параметр\nf(вам=1)\n```"
        self.assertNotIn("на_ты", self.коды(профиль, ответ))

    def test_имя_требуется_только_когда_просили(self):
        без_имени = self._профиль(обращение__имя="Максим", обращение__по_имени=False)
        self.assertNotIn("имя", self.коды(без_имени, "Ответ без имени."))
        с_именем = self._профиль(обращение__имя="Максим", обращение__по_имени=True)
        self.assertIn("имя", self.коды(с_именем, "Ответ без имени."))
        self.assertNotIn("имя", self.коды(с_именем, "Максим, вот ответ."))

    def test_язык_ответа(self):
        профиль = self._профиль(формат__язык="русский")
        английский = "This is a long answer written entirely in English without any Russian."
        self.assertIn("язык", self.коды(профиль, английский))
        self.assertNotIn("язык", self.коды(профиль, "Это длинный ответ по-русски, "
                                                    "с именами вроде LineStringField."))

    def test_короткая_строка_не_считается_сменой_языка(self):
        профиль = self._профиль(формат__язык="русский")
        self.assertNotIn("язык", self.коды(профиль, "OK"))

    def test_структура_проверяется_мягко(self):
        профиль = self._профиль(формат__структура="таблицы")
        расхождения = PreferenceChecker(профиль).check("Просто текст без таблицы.")
        self.assertTrue(расхождения)
        self.assertFalse(any(о.hard for о in расхождения if о.code == "структура"))

    def test_подходящий_ответ_проходит_чисто(self):
        профиль = self._профиль(обращение__имя="Максим", обращение__по_имени=True,
                                обращение__на_ты=True, формат__длина="кратко",
                                формат__код="не_нужен", формат__структура="списки")
        ответ = ("Максим, порядок такой:\n"
                 "- выгрузи схему таблицы\n"
                 "- опиши модель\n"
                 "- прогони миграцию")
        self.assertEqual(PreferenceChecker(профиль).check(ответ), [])

    def test_напоминание_говорит_что_исправить(self):
        профиль = self._профиль(формат__длина="кратко")
        расхождения = PreferenceChecker(профиль).check("слово " * 300)
        self.assertIn("180", PreferenceChecker(профиль).reminder(расхождения))


# --- мастер настройки ---------------------------------------------------------

class МастерНастройки(unittest.TestCase):

    def test_вопросы_берутся_из_описания_полей(self):
        # Одно описание на всё приложение: добавили предпочтение — вопрос
        # появился сам, и разъехаться им негде.
        self.assertEqual(len(interview.questions()), len(preferences.FIELDS))

    def test_пустой_профиль_просит_настройки(self):
        self.assertTrue(interview.needs_setup({}))

    def test_после_мастера_настройка_не_нужна(self):
        профиль = interview.apply({}, {"формат/длина": "кратко"})
        self.assertFalse(interview.needs_setup(профиль))

    def test_пропущенный_вопрос_ничего_не_меняет(self):
        профиль = interview.apply({}, {"формат/длина": "кратко", "обращение/имя": "  "})
        self.assertEqual(профиль["формат"]["длина"], "кратко")
        self.assertEqual(preferences.normalize(профиль)["обращение"]["имя"], "")

    def test_ключ_без_раздела_тоже_понимается(self):
        профиль = interview.apply({}, {"длина": "средне"})
        self.assertEqual(профиль["формат"]["длина"], "средне")

    def test_негодный_ответ_отклоняется(self):
        with self.assertRaises(PreferenceError):
            interview.apply({}, {"формат/длина": "быстро"})

    def test_заготовка_стирает_то_чего_в_ней_нет(self):
        # Иначе человек берёт «тимлид» с именем Максим, переключается на
        # «инженер», у которой имени нет, — и остаётся Максимом.
        профиль = interview.from_template("тимлид")
        self.assertEqual(preferences.normalize(профиль)["обращение"]["имя"], "Максим")
        профиль = interview.from_template("инженер", профиль)
        self.assertEqual(preferences.normalize(профиль)["обращение"]["имя"], "")
        self.assertFalse(preferences.normalize(профиль)["обращение"]["по_имени"])

    def test_пропущенный_вопрос_мастера_по_прежнему_не_стирает(self):
        # У мастера правило обратное: пустой ответ — «оставить как есть».
        профиль = interview.from_template("тимлид")
        профиль = interview.apply(профиль, {"обращение/имя": "   "})
        self.assertEqual(preferences.normalize(профиль)["обращение"]["имя"], "Максим")

    def test_все_заготовки_корректны(self):
        for имя in interview.TEMPLATES:
            профиль = interview.from_template(имя)
            self.assertFalse(interview.needs_setup(профиль))
            self.assertTrue(профиль.get("контекст"))

    def test_заготовки_действительно_разные(self):
        сводки = {и: preferences.summary(interview.from_template(и))
                  for и in interview.TEMPLATES}
        self.assertEqual(len(set(сводки.values())), len(сводки), сводки)


# --- сценарии -----------------------------------------------------------------

class Сценарии(unittest.TestCase):

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp()
        self.хранилище = ScenarioStore(os.path.join(self.каталог, "scenarios.json"))

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    @staticmethod
    def _рабочий():
        return Scenario(
            имя="напиши фичу", триггеры=["напиши фичу"],
            шаги=[
                Step("аналитик", "собрать требования", роль="планирование", стадия=PLANNING),
                Step("backend", "написать код", роль="исполнение", стадия=EXECUTION,
                     вход=["аналитик"]),
                Step("ревьюер", "проверить", роль="исполнение", стадия=VALIDATION,
                     вход=["backend"]),
            ],
        )

    def test_корректный_сценарий_проходит(self):
        self._рабочий().validate()

    def test_маршрут_по_стадиям_проверяется(self):
        # Сценарий, который сломается на середине, должен отвалиться до первого
        # вызова модели, а не после того, как потратил токены.
        кривой = Scenario(имя="кривой", шаги=[
            Step("а", "раз", стадия=PLANNING), Step("б", "два", стадия=DONE),
        ])
        with self.assertRaises(ScenarioError):
            кривой.validate()

    def test_повтор_имён_шагов_отклоняется(self):
        двойной = Scenario(имя="двойной", шаги=[
            Step("а", "раз", стадия=PLANNING), Step("а", "два", стадия=EXECUTION),
        ])
        with self.assertRaises(ScenarioError):
            двойной.validate()

    def test_неизвестная_роль_модели_отклоняется(self):
        плохой = Scenario(имя="п", шаги=[Step("а", "раз", роль="телепатия")])
        with self.assertRaises(ScenarioError):
            плохой.validate()

    def test_сценарий_без_шагов_отклоняется(self):
        with self.assertRaises(ScenarioError):
            Scenario(имя="пустой").validate()

    def test_несколько_шагов_на_одной_стадии_разрешены(self):
        подряд = Scenario(имя="подряд", шаги=[
            Step("а", "раз", стадия=PLANNING), Step("б", "два", стадия=PLANNING),
            Step("в", "три", стадия=EXECUTION),
        ])
        подряд.validate()

    def test_триггер_ищется_в_запросе(self):
        сценарий = self._рабочий()
        self.assertTrue(сценарий.matches("Слушай, напиши фичу для отключений"))
        self.assertFalse(сценарий.matches("Как устроена схема gissys?"))

    def test_хранилище_переживает_перезапись(self):
        self.хранилище.add(self._рабочий())
        другое = ScenarioStore(self.хранилище.path)
        self.assertEqual(len(другое.all()), 1)
        self.assertEqual(другое.get("напиши фичу").шаги[0].агент, "аналитик")

    def test_совпадение_по_триггеру_из_хранилища(self):
        self.хранилище.add(self._рабочий())
        self.assertIsNotNone(self.хранилище.match("напиши фичу: подсветка участков"))
        self.assertIsNone(self.хранилище.match("что такое PostGIS?"))

    def test_удаление(self):
        self.хранилище.add(self._рабочий())
        self.assertTrue(self.хранилище.remove("напиши фичу"))
        self.assertFalse(self.хранилище.remove("напиши фичу"))

    def test_негодный_сценарий_не_сохраняется(self):
        with self.assertRaises(ScenarioError):
            self.хранилище.add(Scenario(имя="пустой"))
        self.assertEqual(self.хранилище.all(), [])

    def test_вход_шага_собирается_из_названных_источников(self):
        from agent.scenarios import ScenarioRunner
        шаг = Step("backend", "написать код", вход=["аналитик"])
        текст = ScenarioRunner._вход(шаг, "исходный запрос", {"аналитик": "требования"})
        self.assertIn("требования", текст)
        self.assertNotIn("исходный запрос", текст)
        self.assertIn("написать код", текст)

    def test_отсутствующий_вход_не_ломает_шаг(self):
        from agent.scenarios import ScenarioRunner
        шаг = Step("backend", "написать код", вход=["архитектор"])
        текст = ScenarioRunner._вход(шаг, "исходный запрос", {})
        self.assertIn("исходный запрос", текст)

    def test_длинный_вход_обрезается(self):
        from agent.scenarios import ScenarioRunner, ВХОД_ШАГА
        шаг = Step("b", "делай", вход=["a"])
        текст = ScenarioRunner._вход(шаг, "q", {"a": "х" * (ВХОД_ШАГА * 3)})
        self.assertIn("обрезано", текст)
        self.assertLess(len(текст), ВХОД_ШАГА * 2)


class _ЗаглушкаКлиента:
    """Клиент, который всегда отвечает одним и тем же — и помнит, кого звали."""

    def __init__(self, текст: str) -> None:
        self.текст = текст
        self.вызовы: list[str] = []
        self.spent = {"calls": 0, "tokens": 0, "cost": 0.0}

    def call(self, model_key, messages, **kwargs):
        from agent.llm import Reply
        self.вызовы.append(model_key)
        return Reply(text=self.текст, model_key=model_key)

    def close(self) -> None:
        pass


class ЛестницаПовторов(unittest.TestCase):
    """Из-за чего агент повторяет запрос и из-за чего меняет модель."""

    def setUp(self) -> None:
        from agent import MemoryAgent
        self.каталог = tempfile.mkdtemp()
        self.агент = MemoryAgent(base_dir=self.каталог, router_mode=OFF,
                                 model_key="groq-20b", require_self_report=False,
                                 judge_semantic=False)

    def tearDown(self) -> None:
        self.агент.close()
        shutil.rmtree(self.каталог, ignore_errors=True)

    def _подменить(self, текст: str) -> _ЗаглушкаКлиента:
        заглушка = _ЗаглушкаКлиента(текст)
        self.агент.client = заглушка
        return заглушка

    def test_расхождение_с_профилем_повторяет_на_той_же_модели(self):
        # Платить за ответ вчетверо дороже потому, что он на двадцать слов
        # длиннее просимого, — плохая сделка.
        self.агент.set_preference("формат", "длина", "кратко")
        заглушка = self._подменить("слово " * 400)
        ответ = self.агент.ask("вопрос")
        self.assertTrue(ответ.deviations)
        self.assertGreater(len(заглушка.вызовы), 1, "повтора не было")
        self.assertEqual(set(заглушка.вызовы), {"groq-20b"})
        self.assertEqual(ответ.escalated_to, "")

    def test_нарушение_инварианта_поднимает_модель(self):
        заглушка = self._подменить("Возьмём Laravel, на нём быстрее.")
        ответ = self.агент.ask("вопрос")
        self.assertTrue(ответ.violations)
        self.assertGreater(len(set(заглушка.вызовы)), 1, "эскалации не было")
        self.assertEqual(заглушка.вызовы[0], "groq-20b")

    def test_подходящий_ответ_не_повторяется(self):
        self.агент.set_preference("формат", "длина", "кратко")
        заглушка = self._подменить("Перенесите таблицу миграцией Django.")
        ответ = self.агент.ask("вопрос")
        self.assertEqual(ответ.attempts, 1)
        self.assertEqual(len(заглушка.вызовы), 1)

    def test_служебный_вызов_не_трогает_память(self):
        # Шаг сценария получает на вход машинный текст из результатов предыдущих
        # шагов. Разбирать его маршрутизатором и класть в диалог нельзя: в базу
        # знаний так попадали куски ответов агентов, принятые за слова человека.
        self.агент.memory.router_mode = "авто"
        self.агент.memory.router = _ЗаглушкаМаршрутизатора(
            Routing(target="знания", key="к", value="машинный текст", confidence=0.99)
        )
        self._подменить("Готово.")
        было_знаний = len(self.агент.memory.long.knowledge.all())
        было_реплик = self.агент.memory.short.stats(self.агент.session)["messages"]

        ответ = self.агент.ask("РЕЗУЛЬТАТ ШАГА «архитектор»: …", internal=True)

        self.assertTrue(ответ.text)
        self.assertEqual(len(self.агент.memory.long.knowledge.all()), было_знаний)
        self.assertEqual(
            self.агент.memory.short.stats(self.агент.session)["messages"], было_реплик
        )

    def test_обычный_вызов_память_пополняет(self):
        self.агент.memory.router_mode = "выкл"
        self._подменить("Готово.")
        было = self.агент.memory.short.stats(self.агент.session)["messages"]
        self.агент.ask("обычный вопрос")
        self.assertEqual(
            self.агент.memory.short.stats(self.агент.session)["messages"], было + 2
        )

    def test_отказ_по_инварианту_не_зовёт_маршрутизатор(self):
        """День 16: проверка запроса идёт раньше маршрутизатора.

        Прежде маршрутизатор — вызов дешёвой модели — шёл первым. Отказ «за
        ноль токенов» ждал его ответа (при 429 — минуту-две), а отклонённый
        запрос успевал предложить запись в долговременную память.
        """
        self.агент.memory.router_mode = "авто"
        маршрутизатор = _СчётчикМаршрутизатора(Routing(
            target="профиль", section="ограничения", key="стек", value="Laravel",
            confidence=0.99))
        self.агент.memory.router = маршрутизатор
        заглушка = self._подменить("не должна вызываться")
        было_реплик = self.агент.memory.short.stats(self.агент.session)["messages"]
        было_профиля = self.агент.memory.long.profile.load()

        ответ = self.агент.ask("Перепиши сервис на Laravel")

        self.assertTrue(ответ.blocked)
        self.assertIn("токены не потрачены", ответ.text)
        self.assertEqual(маршрутизатор.вызовы, [], "маршрутизатор позвали до отказа")
        self.assertEqual(заглушка.вызовы, [], "модель позвали до отказа")
        self.assertEqual(self.агент.memory.long.profile.load(), было_профиля)
        # Сама реплика и отказ — часть разговора и в краткосрочной памяти есть.
        self.assertEqual(
            self.агент.memory.short.stats(self.агент.session)["messages"], было_реплик + 2)

    def test_обычный_вопрос_маршрутизатор_видит(self):
        self.агент.memory.router_mode = "авто"
        маршрутизатор = _СчётчикМаршрутизатора(Routing(target="нет"))
        self.агент.memory.router = маршрутизатор
        self._подменить("Перенесите таблицу миграцией Django.")
        self.агент.ask("Как перенести справочник организаций?")
        self.assertEqual(маршрутизатор.вызовы, ["Как перенести справочник организаций?"])

    def test_промежуточный_шаг_профилем_не_проверяется(self):
        self.агент.set_preference("формат", "длина", "кратко")
        заглушка = self._подменить("слово " * 400)
        ответ = self.агент.ask("вопрос", personal=False)
        self.assertEqual(ответ.deviations, [])
        self.assertEqual(len(заглушка.вызовы), 1)


class _Сценарная:
    """Клиент, отвечающий по списку заготовленных ответов."""

    def __init__(self, ответы: list[str]) -> None:
        self.ответы = list(ответы)
        self.вызовы = 0
        self.spent = {"calls": 0, "tokens": 0, "cost": 0.0}

    def call(self, model_key, messages, **kwargs):
        from agent import prompts
        from agent.llm import Reply
        система = messages[0].get("content", "") if messages else ""
        # Ревизор задачи спрашивает не то, что шаг сценария: он ждёт вердикт
        # JSON. Отдать ему очередную реплику из списка значит получить «вердикт
        # не разобран» и лишнюю эскалацию — то есть мерить не то, что проверяем.
        if система.startswith(prompts.REVIEWER[:40]):
            self.вызовы += 1
            return Reply(text='{"сходится":true,"почему":"заглушка ревизора"}',
                         model_key=model_key)
        текст = self.ответы[min(self.вызовы, len(self.ответы) - 1)]
        self.вызовы += 1
        return Reply(text=текст, model_key=model_key)

    def close(self) -> None:
        pass


class ПаузаИПродолжение(unittest.TestCase):
    """Четыре точки останова и продолжение с того же шага.

    Сети тесты не трогают: проверяется машинерия состояния, а не ответы модели.
    """

    ИТОГ = '{"заголовок":"И","решение":"р","причина":"п"} Сделано.'

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    def _агент(self, ответы: list[str], **kwargs):
        from agent import MemoryAgent
        # Самоотчёт и суждение модели здесь выключены: проверяется механика
        # паузы, а не инварианты, и заглушка маркера не пишет.
        kwargs.setdefault("require_self_report", False)
        kwargs.setdefault("judge_semantic", False)
        агент = MemoryAgent(base_dir=self.каталог, router_mode=OFF, **kwargs)
        клиент = _Сценарная(ответы)
        агент.client = клиент
        агент.memory.client = клиент
        агент.memory.router.client = клиент
        агент.validator.client = клиент
        агент.заглушка = клиент
        return агент

    @staticmethod
    def _сценарий():
        return Scenario(имя="проба", триггеры=["проба"], шаги=[
            Step("аналитик", "собрать требования", роль="планирование",
                 стадия=PLANNING, вход=["запрос"], может_спросить=True),
            Step("backend", "написать код", роль="исполнение",
                 стадия=EXECUTION, вход=["аналитик"]),
        ])

    def test_шаг_спрашивает_и_сценарий_встаёт(self):
        агент = self._агент(["Часть ясна.\nНУЖНЫ СВЕДЕНИЯ: какой SRID?", self.ИТОГ])
        try:
            агент.add_scenario(self._сценарий())
            итог = агент.run_scenario("проба: слой", name="проба")
            self.assertTrue(итог.на_паузе)
            self.assertEqual(итог.причина_паузы, НЕТ_СВЕДЕНИЙ)
            self.assertEqual(итог.ожидание, ОТВЕТ)
            self.assertIn("SRID", итог.ожидание_текст)
            # Название спросившего шага должно быть в пояснении: указатель к
            # этому моменту уже стоит на следующем.
            self.assertIn("аналитик", итог.ожидание_текст)
        finally:
            агент.close()

    def test_шагу_без_разрешения_вопрос_не_засчитывается(self):
        # backend спрашивать не вправе: получив проект, он должен писать код.
        сценарий = self._сценарий()
        сценарий.шаги[0].может_спросить = False
        агент = self._агент(["Ответ.\nНУЖНЫ СВЕДЕНИЯ: а что именно?", self.ИТОГ])
        try:
            агент.add_scenario(сценарий)
            итог = агент.run_scenario("проба: слой", name="проба")
            self.assertFalse(итог.на_паузе)
        finally:
            агент.close()

    def test_продолжение_идёт_с_того_же_шага_в_новом_агенте(self):
        первый = self._агент(["Часть ясна.\nНУЖНЫ СВЕДЕНИЯ: какой SRID?"])
        try:
            первый.add_scenario(self._сценарий())
            итог = первый.run_scenario("проба: слой", name="проба")
            task_id = итог.task_id
            сделано_до = len(итог.шаги)
        finally:
            первый.close()

        # Новый агент — то же, что новый запуск процесса: всё берётся с диска.
        второй = self._агент([self.ИТОГ])
        try:
            состояние = второй.use_task(task_id)
            self.assertTrue(состояние.пауза)
            итог2 = второй.resume_scenario(task_id, ответ="SRID 3857")
            self.assertFalse(итог2.на_паузе)
            # Главное: пройденный шаг не переигрывается.
            self.assertEqual(сделано_до, 1)
            self.assertEqual(len(итог2.шаги), 1)
            # Шаг, ревизор перед завершением и свёртка задачи в решение.
            # Ревизор появился в этом дне: без отчёта проверки задача в done
            # не переходит.
            self.assertEqual(второй.заглушка.вызовы, 3)
        finally:
            второй.close()

    def test_ответ_человека_попадает_в_следующий_шаг(self):
        первый = self._агент(["Часть ясна.\nНУЖНЫ СВЕДЕНИЯ: какой SRID?"])
        try:
            первый.add_scenario(self._сценарий())
            task_id = первый.run_scenario("проба: слой", name="проба").task_id
        finally:
            первый.close()

        второй = self._агент([self.ИТОГ])
        перехвачено = []
        настоящий = второй.client.call

        def подглядеть(model_key, messages, **kwargs):
            перехвачено.append(messages[-1]["content"])
            return настоящий(model_key, messages, **kwargs)

        второй.client.call = подглядеть
        try:
            второй.use_task(task_id)
            второй.resume_scenario(task_id, ответ="SRID 3857, как у остальных слоёв")
            self.assertTrue(any("3857" in т for т in перехвачено),
                            "ответ человека не дошёл до шага")
        finally:
            второй.close()

    def test_режим_по_шагам_останавливает_на_переходе(self):
        агент = self._агент(["Требования собраны.", self.ИТОГ])
        try:
            агент.add_scenario(self._сценарий())
            итог = агент.run_scenario("проба: слой", name="проба", по_шагам=True)
            self.assertTrue(итог.на_паузе)
            self.assertEqual(итог.причина_паузы, НА_ПЕРЕХОДЕ)
            self.assertEqual(итог.ожидание, ПОДТВЕРДИТЬ)
            # Стадия при этом не сменилась: переход ещё не подтверждён.
            self.assertEqual(агент.task.stage, PLANNING)
            итог2 = агент.resume_scenario(итог.task_id)
            self.assertFalse(итог2.на_паузе)
            # Два шага, ревизор перед завершением и свёртка.
            self.assertEqual(агент.заглушка.вызовы, 4)
        finally:
            агент.close()

    def test_подтверждение_перехода_выполняет_переход(self):
        # Иначе цикл снова видит несменённую стадию и просит подтвердить тот же
        # переход — и так до бесконечности. Ровно это и было.
        агент = self._агент(["Требования собраны.", self.ИТОГ])
        try:
            агент.add_scenario(self._сценарий())
            итог = агент.run_scenario("проба: слой", name="проба", по_шагам=True)
            self.assertEqual(итог.причина_паузы, НА_ПЕРЕХОДЕ)
            итог2 = агент.resume_scenario(итог.task_id, по_шагам=True)
            self.assertEqual(агент.task.stage if агент.task else DONE, DONE,
                             "переход так и не состоялся")
            self.assertFalse(итог2.на_паузе, итог2.ожидание_текст)
        finally:
            агент.close()

    def test_согласие_действует_на_один_переход(self):
        # Три шага на трёх стадиях: подтвердили первый переход — второй должен
        # снова спросить.
        сценарий = self._сценарий()
        сценарий.шаги.append(Step("ревьюер", "проверить", роль="исполнение",
                                  стадия=VALIDATION, вход=["backend"]))
        агент = self._агент(["Раз.", "Два.", "Три.", self.ИТОГ])
        try:
            агент.add_scenario(сценарий)
            итог = агент.run_scenario("проба: слой", name="проба", по_шагам=True)
            self.assertEqual(итог.причина_паузы, НА_ПЕРЕХОДЕ)
            итог2 = агент.resume_scenario(итог.task_id, по_шагам=True)
            self.assertTrue(итог2.на_паузе, "второй переход прошёл без подтверждения")
            self.assertEqual(итог2.причина_паузы, НА_ПЕРЕХОДЕ)
        finally:
            агент.close()

    def test_нарушение_инварианта_ставит_на_паузу_а_не_роняет(self):
        агент = self._агент(["Возьмём Laravel, на нём быстрее."])
        try:
            агент.add_scenario(self._сценарий())
            итог = агент.run_scenario("проба: слой", name="проба")
            self.assertTrue(итог.на_паузе)
            self.assertEqual(итог.причина_паузы, НАРУШЕН_ИНВАРИАНТ)
            self.assertEqual(итог.ожидание, РЕШЕНИЕ)
        finally:
            агент.close()

    def test_пауза_по_команде_останавливает_перед_следующим_шагом(self):
        агент = self._агент(["Требования собраны.", self.ИТОГ])
        try:
            агент.add_scenario(self._сценарий())
            # Пауза ставится из обработчика «после шага» — так же, как её
            # ставит кнопка на странице во время прогона.
            def после(результат, номер, всего):
                if номер == 1:
                    агент.pause_task()

            итог = агент.run_scenario("проба: слой", name="проба", on_result=после)
            self.assertTrue(итог.на_паузе)
            self.assertEqual(итог.причина_паузы, ПО_КОМАНДЕ)
            self.assertEqual(len(итог.шаги), 1)
            self.assertEqual(агент.заглушка.вызовы, 1)
        finally:
            агент.close()

    def test_продолжать_нечего_если_задача_не_по_сценарию(self):
        агент = self._агент([self.ИТОГ])
        try:
            from agent import AgentError
            агент.start_task("ручная", "без сценария")
            with self.assertRaises(AgentError):
                агент.resume_scenario("ручная")
        finally:
            агент.close()


class ОтветСНарушениемНеВыходит(unittest.TestCase):
    """Главное требование дня: нарушающий ответ не доходит до пользователя."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    def _агент(self, текст: str, **kwargs):
        from agent import MemoryAgent
        kwargs.setdefault("judge_semantic", False)
        kwargs.setdefault("require_self_report", False)
        агент = MemoryAgent(base_dir=self.каталог, router_mode=OFF, **kwargs)
        клиент = _ЗаглушкаКлиента(текст)
        агент.client = клиент
        агент.memory.client = клиент
        агент.memory.router.client = клиент
        агент.validator.client = клиент
        агент.style.client = клиент
        агент.заглушка = клиент
        return агент

    def test_неисправленный_ответ_заменяется_отказом(self):
        # Прежде такой текст уходил пользователю с пометкой blocked. «Отказался
        # предлагать» и «предложил с пометкой» — разные вещи.
        агент = self._агент("Возьмём Laravel, на нём быстрее.")
        try:
            ответ = агент.ask("Опиши слой доступа к данным")
            self.assertTrue(ответ.blocked)
            self.assertNotIn("Laravel, на нём быстрее", ответ.text)
            self.assertIn("Не могу этого предложить", ответ.text)
            self.assertIsNotNone(ответ.refusal)
            # Сам ответ остаётся для разбора, но не как текст пользователю.
            self.assertTrue(ответ.violations)
        finally:
            агент.close()

    def test_в_диалог_попадает_отказ_а_не_нарушение(self):
        агент = self._агент("Возьмём Laravel, на нём быстрее.")
        try:
            агент.ask("Опиши слой доступа к данным")
            реплики = агент.memory.short.all(агент.session)
            последняя = реплики[-1]["content"]
            self.assertIn("Не могу этого предложить", последняя)
            self.assertNotIn("на нём быстрее", последняя)
        finally:
            агент.close()

    def test_отказ_до_вызова_не_тратит_токенов(self):
        агент = self._агент("Ответ.")
        try:
            ответ = агент.ask("Перепиши слой доступа на Laravel")
            self.assertEqual(агент.заглушка.вызовы, [], "модель звали напрасно")
            self.assertEqual(ответ.attempts, 0)
            self.assertIn("токены не потрачены", ответ.text)
        finally:
            агент.close()

    def test_чистый_ответ_проходит_как_прежде(self):
        агент = self._агент("Модель на GeoDjango, поля как в gissys.")
        try:
            ответ = агент.ask("Опиши модель организации")
            self.assertFalse(ответ.blocked)
            self.assertIsNone(ответ.refusal)
            self.assertIn("GeoDjango", ответ.text)
        finally:
            агент.close()

    def test_самоотчёт_вызывает_повтор(self):
        # Заглушка маркера не пишет, значит все попытки уйдут на напоминания.
        агент = self._агент("Ответ без самоотчёта.", require_self_report=True)
        try:
            ответ = агент.ask("Опиши модель организации на Django")
            self.assertGreater(len(агент.заглушка.вызовы), 1)
            self.assertEqual(ответ.самоотчёт, [])
        finally:
            агент.close()

    def test_обрезанный_ответ_не_требует_самоотчёта(self):
        # Самоотчёт стоит последней строкой, и обрезанный ответ не может его
        # содержать. Требовать его — значит трижды получить тот же обрубок.
        from agent.llm import Reply

        агент = self._агент("Ответ оборван на полусло", require_self_report=True)
        try:
            обычный = агент.client.call

            def обрезанный(model_key, messages, **kwargs):
                ответ = обычный(model_key, messages, **kwargs)
                return Reply(text=ответ.text, model_key=model_key,
                             finish_reason="length")

            агент.client.call = обрезанный
            ответ = агент.ask("Опиши модель организации на Django")
            self.assertEqual(len(агент.заглушка.вызовы), 1,
                             "обрезанный ответ ушёл в повторы")
            self.assertFalse(ответ.blocked)
        finally:
            агент.close()

    def test_возведение_решения_в_инвариант(self):
        агент = self._агент("Ответ.")
        try:
            было = len(агент.invariants())
            инвариант = агент.promote_decision(1)
            self.assertEqual(len(агент.invariants()), было + 1)
            self.assertEqual(инвариант.вид, "решение")
            # После возведения он попадает в промпт наравне с остальными.
            self.assertIn(инвариант.код, {и.код for и in агент.invariants()})
        finally:
            агент.close()

    def test_возведение_несуществующего_решения(self):
        from agent import AgentError
        агент = self._агент("Ответ.")
        try:
            with self.assertRaises(AgentError):
                агент.promote_decision(999)
        finally:
            агент.close()


class РазборВопросаШага(unittest.TestCase):
    """Маркер остановки разбирается кодом, а решение принимает модель."""

    def test_маркер_в_конце_ловится(self):
        from agent.scenarios import вопрос_шага
        self.assertEqual(
            вопрос_шага("Всё описано.\nНУЖНЫ СВЕДЕНИЯ: какой SRID у слоя?"),
            "какой SRID у слоя?")

    def test_маркер_в_разметке_ловится(self):
        from agent.scenarios import вопрос_шага
        self.assertIn("SRID", вопрос_шага("Текст.\n**НУЖНЫ СВЕДЕНИЯ:** какой SRID?"))

    def test_упоминание_в_середине_не_считается(self):
        # Иначе пересказ инструкции самой моделью останавливал бы сценарий.
        from agent.scenarios import вопрос_шага
        текст = ("Если бы не хватало данных, я бы написал НУЖНЫ СВЕДЕНИЯ: и перечислил.\n"
                 + "Но данных хватает.\n" * 6)
        self.assertEqual(вопрос_шага(текст), "")

    def test_без_маркера_пусто(self):
        from agent.scenarios import вопрос_шага
        self.assertEqual(вопрос_шага("Обычный ответ без вопросов."), "")


class ХранилищеИнвариантов(unittest.TestCase):
    """Инварианты проекта отдельно от профиля, два уровня, проверяемость."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp()
        self.склад = InvariantStore(os.path.join(self.каталог, "invariants.json"))

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    @staticmethod
    def _жёсткий(код="стек", значения=("laravel",)):
        return Invariant(код=код, правило="Только Django", вид=inv.СТЕК,
                         тип=inv.ЗАПРЕТ_СЛОВ, значения=list(значения),
                         почему="геометрия в ORM", вместо="GeoDjango")

    def test_жёсткий_без_значений_отклоняется(self):
        # Непроверяемый запрет хуже отсутствующего: создаёт ложное чувство защиты.
        with self.assertRaises(InvariantError):
            self.склад.add(Invariant(код="пустой", правило="нельзя",
                                     тип=inv.ЗАПРЕТ_СЛОВ))

    def test_негодная_регулярка_отклоняется(self):
        with self.assertRaises(InvariantError):
            self.склад.add(Invariant(код="битый", правило="нельзя",
                                     тип=inv.ЗАПРЕТ_РЕГУЛЯРОК,
                                     значения=["[незакрытая"]))

    def test_неизвестный_вид_отклоняется(self):
        with self.assertRaises(InvariantError):
            self.склад.add(Invariant(код="и", правило="п", вид="настроение"))

    def test_негодный_код_отклоняется(self):
        for плохой in ("", "код с пробелом", "../побег", "к" * 60):
            with self.assertRaises(InvariantError):
                self.склад.add(Invariant(код=плохой, правило="п"))

    def test_переживает_запись_и_чтение(self):
        self.склад.add(self._жёсткий())
        другой = InvariantStore(self.склад.path)
        поднят = другой.get("стек")
        self.assertIsNotNone(поднят)
        self.assertEqual(поднят.значения, ["laravel"])
        self.assertEqual(поднят.вид, inv.СТЕК)

    def test_добавление_по_коду_заменяет(self):
        self.склад.add(self._жёсткий())
        self.склад.add(self._жёсткий(значения=("laravel", "symfony")))
        self.assertEqual(len(self.склад.all()), 1)
        self.assertEqual(len(self.склад.get("стек").значения), 2)

    def test_личный_не_снимает_проектный(self):
        # Ослабить общее ограничение под себя нельзя — ровно тот случай, ради
        # которого инварианты и заводят.
        проектный = self._жёсткий()
        личный = Invariant(код="стек", правило="да можно всё", вид=inv.СТЕК,
                           тип=inv.СМЫСЛОВОЙ, уровень=inv.ЛИЧНЫЙ)
        общий = inv.merge([проектный], [личный])
        self.assertEqual(len(общий), 1)
        self.assertEqual(общий[0].правило, "Только Django")
        self.assertEqual(общий[0].уровень, inv.ПРОЕКТНЫЙ)

    def test_личный_добавляет_свой_запрет(self):
        личный = Invariant(код="мой", правило="не предлагать ночные выкатки",
                           вид=inv.БИЗНЕС_ПРАВИЛО, тип=inv.СМЫСЛОВОЙ)
        общий = inv.merge([self._жёсткий()], [личный])
        self.assertEqual({и.код for и in общий}, {"стек", "мой"})
        self.assertEqual(общий[1].уровень, inv.ЛИЧНЫЙ)

    def test_решение_возводится_в_инвариант(self):
        запись = {"id": 3, "заголовок": "Тайлы отдаёт Martin",
                  "решение": "векторные тайлы — Martin", "причина": "быстрее pg_tileserv",
                  "альтернативы": ["pg_tileserv отклонён"]}
        инвариант = inv.from_decision(запись)
        инвариант.validate()
        self.assertEqual(инвариант.вид, inv.РЕШЕНИЕ)
        # Обоснованием отказа служит причина, по которой решение приняли.
        self.assertIn("pg_tileserv", инвариант.почему)
        self.assertIn("pg_tileserv отклонён", инвариант.вместо)
        self.assertIn("№3", инвариант.источник)

    def test_решение_без_заголовка_не_возводится(self):
        with self.assertRaises(InvariantError):
            inv.from_decision({"id": 1, "решение": "что-то"})


class КонфликтЗапроса(ВременнаяПамять):
    """Первый рубеж: требование нарушить инвариант распознаётся до вызова."""

    def setUp(self) -> None:
        super().setUp()
        seed_module.seed(self.память)
        self.валидатор = StateValidator(self.память.all_invariants)

    def test_требование_ловится(self):
        нарушения = self.валидатор.check_request(
            "Перепиши слой доступа к данным на Laravel и дай код модели")
        self.assertTrue(нарушения)
        self.assertEqual(нарушения[0].код, "стек-бэкенд")

    def test_вопрос_об_инварианте_не_ловится(self):
        # Агент обязан уметь объяснить свои ограничения, иначе он вахтёр.
        for вопрос in ("А почему у нас нельзя Laravel?",
                       "Чем плох Laravel для этой задачи?",
                       "Можно ли было взять Laravel?",
                       "Сравни Laravel и Django для геоданных"):
            self.assertEqual(self.валидатор.check_request(вопрос), [], вопрос)

    def test_упоминание_без_повеления_не_ловится(self):
        self.assertEqual(
            self.валидатор.check_request("В соседнем проекте у нас Laravel"), [])

    def test_отказ_в_самом_запросе_не_ловится(self):
        self.assertEqual(
            self.валидатор.check_request("Сделай так, чтобы Laravel не использовался"), [])

    def test_чужая_субд_в_требовании_ловится(self):
        нарушения = self.валидатор.check_request("Давай возьмём MySQL, он привычнее")
        self.assertEqual(нарушения[0].код, "бд")

    def test_вместо_справа_означает_требование(self):
        # «возьмём MySQL вместо PostGIS» — MySQL и есть цель. На живом прогоне
        # проверка запроса приняла это за отказ от MySQL и пропустила.
        нарушения = self.валидатор.check_request(
            "Давай возьмём MySQL вместо PostGIS, команда его лучше знает")
        self.assertTrue(нарушения)
        self.assertEqual(нарушения[0].код, "бд")

    def test_вместо_слева_означает_отказ(self):
        self.assertEqual(
            self.валидатор.check_request("Вместо Laravel возьми Django"), [])

    def test_безобидный_запрос_проходит(self):
        self.assertEqual(
            self.валидатор.check_request("Опиши модель организации на GeoDjango"), [])


class ОтказПоИнварианту(ВременнаяПамять):
    """Как выглядит отказ и из чего он собран."""

    def setUp(self) -> None:
        super().setUp()
        seed_module.seed(self.память)
        self.валидатор = StateValidator(self.память.all_invariants)

    def _отказ(self) -> Refusal:
        нарушения = self.валидатор.check_request("Перепиши всё на Laravel")
        return self.валидатор.refusal(нарушения, "до вызова")

    def test_отказ_называет_инвариант_и_вид(self):
        текст = self._отказ().текст()
        self.assertIn("стек-бэкенд", текст)
        self.assertIn("стек", текст)

    def test_отказ_объясняет_почему(self):
        # «Так нельзя» — не объяснение. В отказ идёт обоснование инварианта.
        self.assertIn("Почему так решено", self._отказ().текст())

    def test_отказ_предлагает_замену(self):
        self.assertIn("Что можно вместо", self._отказ().текст())
        self.assertIn("GeoDjango", self._отказ().текст())

    def test_отказ_до_вызова_говорит_что_токены_не_потрачены(self):
        self.assertIn("токены не потрачены", self._отказ().текст())

    def test_пустой_отказ_пуст(self):
        отказ = self.валидатор.refusal([], "до вызова")
        self.assertFalse(отказ.есть)
        self.assertEqual(отказ.текст(), "")


class Самоотчёт(ВременнаяПамять):
    """Второй рубеж смысловых инвариантов: агент называет учтённое сам."""

    def setUp(self) -> None:
        super().setUp()
        seed_module.seed(self.память)
        self.валидатор = StateValidator(self.память.all_invariants)

    def test_разбор_самоотчёта(self):
        коды = self.валидатор.self_report(
            "Ответ.\nУЧТЕНЫ ИНВАРИАНТЫ: стек-бэкенд, бд (PostGIS)")
        self.assertEqual(коды, ["стек-бэкенд", "бд"])

    def test_разбор_в_разметке(self):
        коды = self.валидатор.self_report("Текст.\n**УЧТЕНЫ ИНВАРИАНТЫ:** стек-бэкенд")
        self.assertEqual(коды, ["стек-бэкенд"])

    def test_без_самоотчёта_пропущены_все_применимые(self):
        применимые = self.валидатор.applicable("напиши модель на Django")
        пропущено = self.валидатор.check_self_report("Просто ответ.", применимые)
        self.assertEqual(set(пропущено), {и.код for и in применимые})

    def test_полный_самоотчёт_проходит(self):
        применимые = self.валидатор.applicable("напиши модель")
        строка = "УЧТЕНЫ ИНВАРИАНТЫ: " + ", ".join(и.код for и in применимые)
        self.assertEqual(self.валидатор.check_self_report("Ответ.\n" + строка,
                                                          применимые), [])

    def test_жёсткие_применимы_всегда(self):
        применимые = {и.код for и in self.валидатор.applicable("любой текст")}
        self.assertIn("стек-бэкенд", применимые)

    def test_смысловой_применим_по_словам(self):
        # Требовать самоотчёт по бизнес-правилу про 1С в ответе про вёрстку
        # карты значило бы приучать агента писать «учтено» не глядя.
        про_1с = {и.код for и in self.валидатор.applicable(
            "как писать объекты сети через 1С")}
        про_вёрстку = {и.код for и in self.валидатор.applicable(
            "поменяй цвет подписи на карте")}
        self.assertIn("1С-источник-истины", про_1с)
        self.assertNotIn("1С-источник-истины", про_вёрстку)

    def test_самоотчёт_первой_строкой(self):
        # Длинный ответ упирается в предел токенов и обрывается: последней
        # строки тогда просто не существует. Первая от обрезки не страдает.
        коды = self.валидатор.self_report(
            "УЧТЕНЫ ИНВАРИАНТЫ: стек-бэкенд, бд\n\nДальше длинный ответ…")
        self.assertEqual(коды, ["стек-бэкенд", "бд"])

    def test_если_применимых_нет_самоотчёт_не_требуется(self):
        self.assertEqual(self.валидатор.check_self_report("Ответ.", []), [])


class ОбъяснениеИнварианта(ВременнаяПамять):
    """Агент обязан уметь объяснить свои ограничения, а не только их применять."""

    def setUp(self) -> None:
        super().setUp()
        seed_module.seed(self.память)
        self.валидатор = StateValidator(self.память.all_invariants)

    def test_вопрос_об_инварианте_распознаётся(self):
        self.assertTrue(self.валидатор.is_explanatory("А почему у нас нельзя Laravel?"))
        self.assertTrue(self.валидатор.is_explanatory("Чем PostGIS лучше MySQL?"))

    def test_обычный_вопрос_объяснением_не_считается(self):
        self.assertFalse(self.валидатор.is_explanatory("Почему индекс не используется?"))
        self.assertFalse(self.валидатор.is_explanatory("Перепиши на Laravel"))

    def test_в_объяснении_упоминание_не_нарушение(self):
        # Объясняя, почему проект не на Laravel, агент обязан назвать Laravel.
        ответ = ("Laravel — популярный PHP-фреймворк с большой экосистемой. "
                 "В Laravel есть Eloquent, миграции и очереди из коробки. "
                 "Laravel хорош там, где геометрия не нужна.")
        self.assertTrue(self.валидатор.check(ответ), "без пометки должно ловиться")
        self.assertEqual(self.валидатор.check(ответ, explanatory=True), [])

    def test_код_на_запрещённом_стеке_ловится_и_в_объяснении(self):
        # Объяснять можно, писать код на запрещённом стеке — нет.
        ответ = "Вот как это выглядело бы:\n```php\n<?php\n$this->load->model(\"x\");\n```"
        self.assertTrue(self.валидатор.check(ответ, explanatory=True))


class СуждениеМодели(ВременнаяПамять):
    """Третий рубеж: смысловые инварианты судит отдельная модель."""

    def setUp(self) -> None:
        super().setUp()
        seed_module.seed(self.память)

    def _валидатор(self, ответ_модели: str):
        class Судья:
            spent = {"calls": 0, "tokens": 0, "cost": 0.0}

            def __init__(self, текст): self.текст = текст; self.вызовы = 0

            def call(self, model_key, messages, **kwargs):
                from agent.llm import Reply
                self.вызовы += 1
                self.сообщения = messages
                return Reply(text=self.текст, model_key=model_key)

            def close(self): pass

        судья = Судья(ответ_модели)
        return StateValidator(self.память.all_invariants, судья), судья

    def test_вердикт_превращается_в_нарушение_с_обоснованием(self):
        валидатор, _ = self._валидатор(
            '{"нарушены":[{"код":"1С-источник-истины","почему":"пишет прямо в gisdata"}]}')
        применимые = валидатор.applicable("пишем объекты через 1С")
        вердикт = валидатор.judge("любой ответ", применимые)
        self.assertEqual(вердикт.нарушены, ["1С-источник-истины"])
        нарушения = валидатор.violations_from_judge(вердикт)
        self.assertTrue(нарушения[0].почему, "обоснование должно браться из инварианта")
        self.assertTrue(нарушения[0].вместо)

    def test_чистый_вердикт_не_даёт_нарушений(self):
        валидатор, _ = self._валидатор('{"нарушены":[]}')
        применимые = валидатор.applicable("пишем объекты через 1С")
        self.assertEqual(валидатор.judge("ответ", применимые).нарушены, [])

    def test_выдуманный_код_отбрасывается(self):
        # Модель может назвать инвариант, которого нет; верить ей нельзя.
        валидатор, _ = self._валидатор('{"нарушены":[{"код":"выдуманный"}]}')
        применимые = валидатор.applicable("пишем объекты через 1С")
        self.assertEqual(валидатор.judge("ответ", применимые).нарушены, [])

    def test_судья_поднимается_на_ступень_при_сбое(self):
        # Проверяющие модели самые слабые, и пустой ответ от них — обычное дело.
        # Без эскалации смысловые инварианты остаются без проверки вовсе, а
        # выглядит это как «нарушений нет».
        валидатор, судья = self._валидатор("мусор")
        применимые = валидатор.applicable("пишем объекты через 1С")
        валидатор.judge("ответ", применимые)
        self.assertEqual(судья.вызовы, 2, "эскалации не было")

    def test_неразбираемый_вердикт_не_роняет(self):
        валидатор, _ = self._валидатор("я не понял задачу")
        применимые = валидатор.applicable("пишем объекты через 1С")
        вердикт = валидатор.judge("ответ", применимые)
        self.assertTrue(вердикт.сбой)
        self.assertEqual(вердикт.нарушены, [])

    def test_судье_не_показывают_запрос_пользователя(self):
        # Проверяющего нечем уговаривать, если он не видит уговоров.
        валидатор, судья = self._валидатор('{"нарушены":[]}')
        применимые = валидатор.applicable("пишем объекты через 1С")
        валидатор.judge("ответ агента", применимые)
        всё = " ".join(с["content"] for с in судья.сообщения)
        self.assertIn("ответ агента", всё)
        self.assertNotIn("пишем объекты через 1С", всё)

    def test_без_смысловых_модель_не_зовётся(self):
        валидатор, судья = self._валидатор('{"нарушены":[]}')
        только_жёсткие = [и for и in валидатор.hard()]
        валидатор.judge("ответ", только_жёсткие)
        self.assertEqual(судья.вызовы, 0, "лишний вызов модели")


# --- каталог моделей ----------------------------------------------------------

class КаталогМоделей(unittest.TestCase):

    def test_у_каждой_роли_есть_модель(self):
        for роль in catalog.ROLES:
            self.assertIn(catalog.for_role(роль, offset=0), catalog.MODELS)

    def test_частые_роли_чередуют_провайдеров(self):
        модели = {catalog.for_role("маршрутизация", offset=i) for i in range(2)}
        провайдеры = {catalog.get(м).provider for м in модели}
        self.assertGreater(len(провайдеры), 1, "частая роль сидит на одном провайдере")

    def test_эскалация_поднимает_на_ступень(self):
        self.assertEqual(catalog.escalate("groq-allam7b"), "groq-20b")
        self.assertEqual(catalog.escalate("groq-20b"), "groq-120b")
        self.assertEqual(catalog.escalate("groq-120b"), "ds-pro")

    def test_с_вершины_лестницы_некуда(self):
        self.assertEqual(catalog.escalate("ds-pro"), "ds-pro")

    def test_модель_вне_лестницы_идёт_на_сильную_бесплатную(self):
        self.assertEqual(catalog.escalate("groq-qwen27b"), "groq-120b")

    def test_неизвестная_роль_даёт_ошибку(self):
        with self.assertRaises(KeyError):
            catalog.for_role("телепатия")


# --- начальное наполнение -----------------------------------------------------

class НачальноеНаполнение(ВременнаяПамять):

    def test_наполняет_пустую_память(self):
        сводка = seed_module.seed(self.память)
        self.assertGreater(сводка["знания"], 0)
        self.assertEqual(len(self.память.invariants.all()), len(seed_module.INVARIANTS))

    def test_не_перезаписывает_заполненную(self):
        seed_module.seed(self.память)
        self.память.long.profile.update("обращение", "тон", "дружелюбный")
        seed_module.seed(self.память)
        self.assertEqual(
            self.память.long.profile.load()["обращение"]["тон"], "дружелюбный"
        )

    def test_жёсткие_инварианты_проверяемы(self):
        seed_module.seed(self.память)
        for инвариант in self.память.invariants.hard():
            self.assertTrue(инвариант.значения,
                            f"инвариант «{инвариант.код}» нечем проверять")

    def test_омоглифы_в_самоотчёте_не_мешают(self):
        # Модель регулярно печатает «1c» латинской c вместо кириллической.
        from agent.validator import StateValidator as SV
        валидатор = SV(self.память.all_invariants)
        применимые = [и for и in валидатор.all() if и.код == "1С-источник-истины"]
        отчёт = "УЧТЕНЫ ИНВАРИАНТЫ: 1C-источник-истины"      # латинская C
        self.assertEqual(валидатор.check_self_report(отчёт, применимые), [])

    def test_у_каждого_инварианта_есть_обоснование(self):
        # Обоснование и альтернатива идут в текст отказа. Инвариант без них
        # даёт отказ «так нельзя», а это не объяснение.
        seed_module.seed(self.память)
        for инвариант in self.память.invariants.all():
            self.assertTrue(инвариант.почему, f"«{инвариант.код}» без обоснования")
            self.assertTrue(инвариант.вместо, f"«{инвариант.код}» без альтернативы")


class ЛимитыПровайдера(unittest.TestCase):
    """Минутный лимит проходит сам, суточный — нет, и путать их нельзя."""

    def test_минутный_лимит_не_считается_суточным(self):
        from agent.llm import _суточный_лимит
        self.assertFalse(_суточный_лимит(
            "Rate limit reached on tokens per minute (TPM): Limit 8000"))

    def test_суточный_лимит_узнаётся(self):
        from agent.llm import _суточный_лимит
        for сообщение in ("on tokens per day (TPD): Limit 200000",
                          "on requests per day (RPD)",
                          "daily quota exceeded"):
            self.assertTrue(_суточный_лимит(сообщение), сообщение)

    def test_суточный_лимит_не_уходит_в_повторы(self):
        # Ждать по минуте четыре раза, чтобы в конце получить ту же ошибку, —
        # это несколько минут, потраченных впустую.
        import httpx
        from agent.llm import Client, LLMError

        клиент = Client()
        попыток = {"счёт": 0}

        def ответ(запрос: httpx.Request) -> httpx.Response:
            попыток["счёт"] += 1
            return httpx.Response(429, json={"error": {
                "message": "Rate limit reached on tokens per day (TPD): Limit 200000"}})

        клиент._http = httpx.Client(transport=httpx.MockTransport(ответ))
        # Ключ нужен только для того, чтобы запрос дошёл до заглушки: без него
        # вызов обрывается раньше HTTP. Тест не должен зависеть от .env —
        # в свежем клоне его нет, и тест падал на «0 попыток».
        from unittest import mock
        модель = catalog.get("groq-120b")
        try:
            with mock.patch.dict(os.environ, {модель.env_var: "test-key"}), \
                    self.assertRaises(LLMError) as поймано:
                клиент.call("groq-120b", [{"role": "user", "content": "привет"}])
            self.assertEqual(попыток["счёт"], 1, "суточный лимит ушёл в повторы")
            self.assertIn("завтра", str(поймано.exception))
        finally:
            клиент.close()


# --- веб-интерфейс ------------------------------------------------------------

class ВебИнтерфейс(unittest.TestCase):
    """Всё, что можно сделать из консоли, должно быть доступно и со страницы.

    Тесты идут через тестовый клиент Flask и сети не трогают: проверяются ручки
    управления, а не ответы модели.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.каталог = tempfile.mkdtemp(prefix="web-test-")
        # web.py создаёт агента при импорте, поэтому каталог памяти задаётся до
        # него — иначе тест писал бы в рабочую память проекта.
        os.environ["MEMORY_DIR"] = cls.каталог
        import importlib
        import web as модуль
        cls.web = importlib.reload(модуль)
        cls.клиент = cls.web.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        os.environ.pop("MEMORY_DIR", None)
        shutil.rmtree(cls.каталог, ignore_errors=True)

    def состояние(self) -> dict:
        return self.клиент.get("/api/state").get_json()

    def test_страница_открывается(self):
        ответ = self.клиент.get("/")
        self.assertEqual(ответ.status_code, 200)

    def _подменить_клиента(self, клиент):
        """Ставит клиента во все места, где агент его держит, и возвращает прежнего.

        Мест четыре, и это выяснилось неприятным образом: первая версия
        подменяла только два, а маршрутизатор реплик держит свою ссылку — и
        «тесты без сети» тихо ходили в API, отчего набор шёл двадцать пять
        секунд вместо секунды.
        """
        прежний = self.web.agent.client
        self.web.agent.client = клиент
        self.web.agent.memory.client = клиент
        self.web.agent.memory.router.client = клиент
        self.web.agent.validator.client = клиент
        self.web.agent.style.client = клиент
        # Заглушка маркера самоотчёта не пишет, а проверяется здесь не он.
        self.web.agent.require_self_report = False
        self.web.agent.judge_semantic = False
        return прежний

    def _без_сети(self, текст: str = "Готово."):
        заглушка = _ЗаглушкаКлиента(текст)
        return заглушка, self._подменить_клиента(заглушка)

    def _вернуть(self, прежний) -> None:
        self._подменить_клиента(прежний)

    def _дождаться(self, предел: float = 20.0) -> dict:
        конец = time.monotonic() + предел
        while time.monotonic() < конец:
            прогон = self.клиент.get("/api/scenario/status").get_json()["run"]
            if прогон and прогон["готово"]:
                return прогон
            time.sleep(0.05)
        self.fail("сценарий не завершился за отведённое время")

    def test_состояние_задачи_отдаётся_страницей(self):
        self.клиент.post("/api/task", json={"action": "создать", "task_id": "сост",
                                            "title": "проверка"})
        состояние = self.состояние()["task_state"]
        self.assertTrue(состояние["есть"])
        for поле in ("этап", "шаг_словами", "ожидание", "ожидание_текст", "пауза"):
            self.assertIn(поле, состояние)
        self.клиент.post("/api/task", json={"action": "отпустить"})

    def test_пауза_и_продолжение_через_страницу(self):
        self.клиент.post("/api/task", json={"action": "создать", "task_id": "пауза-веб",
                                            "title": "проверка"})
        пауза = self.клиент.post("/api/task", json={"action": "пауза"})
        self.assertEqual(пауза.status_code, 200)
        self.assertTrue(пауза.get_json()["state"]["task_state"]["пауза"])
        дальше = self.клиент.post("/api/task", json={"action": "продолжить"})
        self.assertFalse(дальше.get_json()["state"]["task_state"]["пауза"])
        self.клиент.post("/api/task", json={"action": "отпустить"})

    def test_запрещённый_переход_задачи_отклоняется(self):
        self.клиент.post("/api/task", json={"action": "создать", "task_id": "проба-веб",
                                            "title": "проверка"})
        ответ = self.клиент.post("/api/task", json={"action": "стадия", "stage": "done"})
        self.assertEqual(ответ.status_code, 400)
        тело = ответ.get_json()
        # Отказ приходит не строкой, а разбором: страница рисует из него правило,
        # обоснование и то, чего не хватает.
        self.assertTrue(тело["refusal"]["текст"])
        self.assertEqual(тело["refusal"]["куда"], "done")
        self.assertEqual(тело["refusal"]["причина"], "в жизненном цикле нет такого перехода")
        # Состояние приходит вместе с отказом — в нём уже видна попытка.
        self.assertEqual(len(тело["state"]["task_state"]["отказы"]), 1)
        self.клиент.post("/api/task", json={"action": "отпустить"})

    def test_ворота_задачи_видны_в_состоянии(self):
        self.клиент.post("/api/task", json={"action": "создать", "task_id": "ворота-веб",
                                            "title": "ворота"})
        состояние = self.состояние()
        переходы = состояние["task_state"]["переходы"]
        self.assertEqual(len(переходы), len(состояние["stages"]))
        закрыт = next(п for п in переходы if п["стадия"] == "execution")
        self.assertFalse(закрыт["можно"])
        self.assertTrue(закрыт["чего_не_хватает"])
        # День 28: редактор условий ушёл из веба (spec § 9) — замки остались, а
        # сами условия страница больше не берёт.
        self.assertNotIn("conditions", состояние)
        self.клиент.post("/api/task", json={"action": "отпустить"})

    def test_утверждение_плана_и_проверка_со_страницы(self):
        self.клиент.post("/api/task", json={"action": "создать", "task_id": "цикл-веб",
                                            "title": "цикл"})
        # Плана нет — утверждать нечего, и страница получает внятный отказ.
        пусто = self.клиент.post("/api/task", json={"action": "утвердить-план"})
        self.assertEqual(пусто.status_code, 400)

        self.web.agent.task.set_plan(["разобрать схему", "написать модель"])
        self.web.agent.save_task()
        ответ = self.клиент.post("/api/task", json={"action": "утвердить-план"})
        self.assertEqual(ответ.status_code, 200)
        self.assertEqual(ответ.get_json()["approved"]["пунктов"], 2)

        стадия = self.клиент.post("/api/task", json={"action": "стадия",
                                                     "stage": "execution"})
        self.assertEqual(стадия.status_code, 200)
        проверка = self.клиент.post("/api/task", json={"action": "валидация",
                                                       "ревизор": False})
        self.assertEqual(проверка.status_code, 200)
        отчёт = проверка.get_json()["report"]
        self.assertIn(отчёт["вердикт"], ("прошла", "не прошла"))
        self.клиент.post("/api/task", json={"action": "отпустить"})

    def test_шаг_закрывается_со_страницы(self):
        self.клиент.post("/api/task", json={"action": "создать", "task_id": "шаги-веб",
                                            "title": "шаги"})
        self.web.agent.task.set_plan(["первый", "второй"])
        self.web.agent.save_task()
        self.клиент.post("/api/task", json={"action": "утвердить-план"})
        self.клиент.post("/api/task", json={"action": "стадия", "stage": "execution"})
        закрыт = self.клиент.post("/api/task", json={"action": "закрыть-шаг",
                                                     "value": "сделано"})
        self.assertEqual(закрыт.status_code, 200)
        состояние = закрыт.get_json()["state"]["task_state"]
        self.assertEqual(состояние["шаги"][0]["состояние"], "готов")
        self.assertEqual(состояние["шаг"], 2)
        self.клиент.post("/api/task", json={"action": "отпустить"})

# --- разметка страницы --------------------------------------------------------

class КонсольЗавершается(unittest.TestCase):
    """Команда, которая что-то сделала, не должна открывать диалог.

    Ловушка, стоившая зависшего прогона: ключи этого дня (--утвердить-план,
    --валидация, --закрыть-шаг, --условие) отрабатывали и проваливались в
    диалоговый режим, где cli.py молча ждёт ввода. В терминале это выглядит как
    зависание, в скрипте — как повисший процесс.
    """

    ДЕЙСТВИЯ = [
        ["--новая-задача", "проба"],
        ["--стадия", "execution"],
        ["--шаг", "ключ=значение"],
        ["--запомни", "знания", "текст"],
        ["--заготовка", "тимлид"],
        ["--настройка", "формат/длина=кратко"],
        ["--инвариант", "код=правило"],
        ["--снять-инвариант", "код"],
        ["--возвести", "1"],
        ["--утвердить-план"],
        ["--снять-утверждение"],
        ["--валидация"],
        ["--закрыть-шаг"],
        ["--условие", "код"],
        ["--снять-условие", "код"],
    ]

    def test_после_действия_диалог_не_открывается(self):
        import cli
        разбор = cli.build_parser()
        for ключи in self.ДЕЙСТВИЯ:
            аргументы = разбор.parse_args(ключи)
            self.assertTrue(cli.меняет_состояние(аргументы),
                            f"после «{' '.join(ключи)}» cli.py уйдёт в диалог и повиснет")

    def test_показывающие_команды_действиями_не_считаются(self):
        # Они и так возвращают результат сами, но список не должен разрастаться
        # до «любая команда завершает работу»: без вопроса и без действия
        # диалог открыться обязан.
        import cli
        разбор = cli.build_parser()
        self.assertFalse(cli.меняет_состояние(разбор.parse_args([])))
        self.assertFalse(cli.меняет_состояние(разбор.parse_args(["--трейс"])))


class СтраницаЦела(unittest.TestCase):
    """Структурные проверки скрипта страницы.

    Появились после поломки, которую не поймал ни один прежний тест: при
    рефакторинге был снят не тот заголовок функции, и «запуститьСценарий»
    оказался объявлен ВНУТРИ «нарисоватьТрейс». Синтаксис при этом остался
    корректным — `node --check` молчал, — а обработчик кнопки падал с
    ReferenceError, и сценарий не запускался вовсе. Проверки Python-кода такого
    не видят в принципе, поэтому нужна отдельная.
    """

    @classmethod
    def setUpClass(cls) -> None:
        import re
        разметка = pathlib.Path(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "templates", "index.html")
        ).read_text(encoding="utf-8")
        найдено = re.search(r"<script>(.*?)</script>", разметка, re.DOTALL)
        assert найдено, "в шаблоне нет блока <script>"
        cls.js = найдено.group(1)
        cls.разметка = разметка

    @staticmethod
    def _без_литералов(строка: str) -> str:
        import re
        return re.sub(r"'[^']*'|\"[^\"]*\"|`[^`]*`|//.*", "", строка)

    def _объявления(self):
        """Имена функций и глубина вложенности, на которой они объявлены."""
        import re
        глубина, итог = 0, []
        for строка in self.js.split("\n"):
            найдено = re.match(r"\s*(async\s+)?function\s+([А-Яа-яёA-Za-z_]+)", строка)
            if найдено:
                итог.append((найдено.group(2), глубина))
            без = self._без_литералов(строка)
            глубина += без.count("{") - без.count("}")
        return итог

    def test_скобки_сходятся(self):
        глубина = 0
        for строка in self.js.split("\n"):
            без = self._без_литералов(строка)
            глубина += без.count("{") - без.count("}")
        self.assertEqual(глубина, 0, "скобки в скрипте страницы не сходятся")

    def test_все_функции_объявлены_на_верхнем_уровне(self):
        вложенные = [(имя, г) for имя, г in self._объявления() if г != 0]
        self.assertEqual(вложенные, [],
                         f"функции объявлены внутри других: {вложенные}")

    def test_обработчики_видят_нужные_функции(self):
        """Всё, что зовут обработчики, должно быть объявлено на верхнем уровне."""
        объявлены = {имя for имя, г in self._объявления() if г == 0}
        # День 28: панели профиля, сценариев, инвариантов, слоёв, условий и
        # паузы ушли из веба (spec § 9) — их функций на странице больше нет.
        обязательные = {
            "нарисоватьСостояние", "нарисоватьЗадачу", "нарисоватьВорота",
            "нарисоватьТрейс", "применить", "спросить", "действиеЗадачи",
            "добавить", "запрос", "экранировать", "запросПотоком",
            # день 16 — панель MCP
            "загрузитьMCP", "осмотретьMCP", "нарисоватьMCP", "секундыMCP",
            "нарисоватьСерверMCP", "нарисоватьИнструментMCP",
        }
        self.assertEqual(обязательные - объявлены, set(),
                         "обработчики зовут функции, которых нет на верхнем уровне")

    def test_каждый_id_из_скрипта_есть_в_разметке(self):
        """$('имя') должно находить элемент, иначе обработчик молча не навесится."""
        import re
        имена = set(re.findall(r"\$\('([^']+)'\)", self.js))
        # Эти элементы рисуются самим скриптом, в статической разметке их нет.
        рисуемые = {"мастер-сохранить", "новый-сценарий", "сц-имя", "сц-описание",
                    "сц-триггеры", "сц-добавить", "сц-сохранить", "сц-отмена",
                    "mcp-все"}
        в_разметке = set(re.findall(r'id="([^"]+)"', self.разметка))
        пропавшие = имена - в_разметке - рисуемые
        self.assertEqual(пропавшие, set(), f"в разметке нет элементов: {пропавшие}")


# --- контролируемые переходы (день 15) ----------------------------------------

class УсловияПерехода(unittest.TestCase):
    """Сами условия: описание, проверка описания, хранение, слияние уровней."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    def test_базовые_покрывают_оба_примера_задания(self):
        коды = {у.код for у in БАЗОВЫЕ}
        self.assertIn("план-утверждён", коды)
        self.assertIn("валидация-пройдена", коды)
        план = next(у for у in БАЗОВЫЕ if у.код == "план-утверждён")
        self.assertEqual((план.откуда, план.куда), (PLANNING, EXECUTION))
        финал = next(у for у in БАЗОВЫЕ if у.код == "валидация-пройдена")
        self.assertEqual((финал.откуда, финал.куда), (VALIDATION, DONE))

    def test_у_каждого_базового_есть_обоснование_и_выход(self):
        # Отказ состоит из правила, обоснования и подсказки. Условие без них
        # даёт отказ «так нельзя», то есть бесполезный.
        for условие in БАЗОВЫЕ:
            self.assertTrue(условие.правило, условие.код)
            self.assertTrue(условие.почему, условие.код)
            self.assertTrue(условие.вместо, условие.код)

    def test_описание_проверяется(self):
        with self.assertRaises(TransitionConfigError):
            Условие(код="", правило="п").validate()
        with self.assertRaises(TransitionConfigError):
            Условие(код="к", правило="п", что="выдумка").validate()
        with self.assertRaises(TransitionConfigError):
            Условие(код="к", правило="п", откуда="нетакой").validate()
        with self.assertRaises(TransitionConfigError):
            # Проверка «есть в собранном» без ключа проверяет пустоту и всегда
            # проходит: это хуже отсутствующего условия.
            Условие(код="к", правило="п", что=tr.ЕСТЬ_В_СОБРАННОМ).validate()
        Условие(код="к", правило="п", что=tr.ЕСТЬ_В_СОБРАННОМ, значение="ссылка").validate()

    def test_звёздочка_подходит_всем_переходам_кроме_пустого(self):
        любое = Условие(код="л", правило="п", что=tr.НЕТ_ОТКРЫТЫХ_ВОПРОСОВ)
        self.assertTrue(любое.подходит(PLANNING, EXECUTION))
        self.assertTrue(любое.подходит(VALIDATION, DONE))
        # Переход в ту же стадию — не переход, и условие на нём не срабатывает.
        self.assertFalse(любое.подходит(EXECUTION, EXECUTION))

    def test_личное_условие_хранится_и_снимается(self):
        хранилище = ConditionStore(os.path.join(self.каталог, "conditions.json"))
        self.assertEqual(хранилище.all(), [])
        хранилище.add(Условие(код="ссылка", откуда=VALIDATION, куда=DONE,
                              что=tr.ЕСТЬ_В_СОБРАННОМ, значение="репозиторий",
                              правило="в готово — только со ссылкой"))
        self.assertEqual(len(хранилище.all()), 1)
        self.assertEqual(хранилище.get("ссылка").уровень, ЛИЧНЫЙ)
        self.assertTrue(хранилище.remove("ссылка"))
        self.assertFalse(хранилище.remove("ссылка"))

    def test_личное_не_подменяет_базовое(self):
        хранилище = ConditionStore(os.path.join(self.каталог, "conditions.json"))
        with self.assertRaises(TransitionConfigError):
            # Совпадение кода — единственный способ отменить базовое условие,
            # и поэтому он закрыт.
            хранилище.add(Условие(код="валидация-пройдена", правило="ничего не требую",
                                  что=tr.НЕТ_ОТКРЫТЫХ_ВОПРОСОВ))

    def test_слияние_отдаёт_приоритет_базовым(self):
        своё = Условие(код="план-утверждён", правило="подменить", что=tr.НЕТ_ОТКРЫТЫХ_ВОПРОСОВ)
        общие = tr.merge(list(БАЗОВЫЕ), [своё])
        план = [у for у in общие if у.код == "план-утверждён"]
        self.assertEqual(len(план), 1)
        self.assertEqual(план[0].уровень, tr.БАЗОВЫЙ)


class ВоротаПерехода(unittest.TestCase):
    """Проверка перехода: граф, условия, отказ и журнал попыток."""

    def _задача(self, **поля) -> TaskState:
        состояние = TaskState(task_id="проба", title="проба")
        for имя, значение in поля.items():
            setattr(состояние, имя, значение)
        return состояние

    def _готовая_к_проверке(self) -> TaskState:
        состояние = self._задача()
        состояние.set_plan(["разобрать схему", "написать модель"])
        состояние.утвердить_план("человек")
        состояние.transition(EXECUTION, "проба")
        for _ in состояние.шаги:
            состояние.начать_шаг()
            состояние.закончить_шаг("сделано")
        состояние.transition(VALIDATION, "проба")
        return состояние

    def test_маршрут_считается_по_графу(self):
        self.assertEqual(Ворота.маршрут(PLANNING, DONE),
                         [PLANNING, EXECUTION, VALIDATION, DONE])
        self.assertEqual(Ворота.маршрут(VALIDATION, PLANNING),
                         [VALIDATION, EXECUTION, PLANNING])
        # Из done не ведёт ни одна стрелка: задача завершена.
        self.assertEqual(Ворота.маршрут(DONE, PLANNING), [])

    def test_прыжок_через_этап_отклоняется_по_графу(self):
        вердикт = Ворота().проверить(self._задача(), DONE)
        self.assertFalse(вердикт.можно)
        self.assertEqual(вердикт.отказ.причина, tr.НЕТ_СТРЕЛКИ)
        # В отказе есть и законный маршрут, и вердикт по ближайшему шагу.
        self.assertEqual(вердикт.отказ.маршрут[1], EXECUTION)
        self.assertFalse(вердикт.отказ.следующий["можно"])

    def test_реализация_без_утверждённого_плана_закрыта(self):
        состояние = self._задача()
        состояние.set_plan(["шаг"])
        вердикт = Ворота().проверить(состояние, EXECUTION)
        self.assertFalse(вердикт.можно)
        self.assertEqual(вердикт.отказ.причина, tr.НЕ_ВЫПОЛНЕНО)
        self.assertEqual([п.код for п in вердикт.отказ.невыполненные], ["план-утверждён"])
        состояние.утвердить_план("человек")
        self.assertTrue(Ворота().проверить(состояние, EXECUTION).можно)

    def test_правка_плана_снимает_утверждение(self):
        состояние = self._задача()
        состояние.set_plan(["шаг"])
        состояние.утвердить_план("человек")
        self.assertTrue(состояние.утверждение_актуально)
        состояние.set_plan(["шаг", "ещё шаг"])
        # Подпись осталась, но относится к другому плану — значит, не действует.
        self.assertTrue(состояние.план_утверждён)
        self.assertFalse(состояние.утверждение_актуально)
        self.assertFalse(Ворота().проверить(состояние, EXECUTION).можно)

    def test_финал_без_проверки_закрыт(self):
        состояние = self._готовая_к_проверке()
        вердикт = Ворота().проверить(состояние, DONE)
        self.assertFalse(вердикт.можно)
        self.assertEqual([п.код for п in вердикт.отказ.невыполненные],
                         ["валидация-пройдена"])
        состояние.записать_валидацию({"вердикт": "прошла", "пункты": [
            {"пункт": "всё на месте", "итог": "прошло"}]})
        self.assertTrue(Ворота().проверить(состояние, DONE).можно)

    def test_красный_пункт_держит_задачу_незавершённой(self):
        состояние = self._готовая_к_проверке()
        состояние.записать_валидацию({"вердикт": "не прошла", "пункты": [
            {"пункт": "слой отдаётся", "итог": "не прошло", "пояснение": "нет тайлов"}]})
        self.assertFalse(Ворота().проверить(состояние, DONE).можно)

    def test_непроверенный_пункт_переход_не_запирает(self):
        # «Не проверено» — это сбой ревизора, а не дефект работы. Человек не
        # может починить чужой провайдер, и вечно незавершаемая задача хуже.
        состояние = self._готовая_к_проверке()
        состояние.записать_валидацию({"вердикт": "прошла", "пункты": [
            {"пункт": "по смыслу", "итог": "не проверено", "пояснение": "ревизор молчит"}]})
        self.assertTrue(Ворота().проверить(состояние, DONE).можно)

    def test_отчёт_устаревает_когда_работа_поменялась(self):
        состояние = self._готовая_к_проверке()
        состояние.записать_валидацию({"вердикт": "прошла", "пункты": []})
        self.assertTrue(Ворота().проверить(состояние, DONE).можно)
        состояние.remember("новый результат", "переделали слой")
        self.assertFalse(состояние.валидация_актуальна)
        self.assertFalse(Ворота().проверить(состояние, DONE).можно)

    def test_шаги_считаются_по_текущей_стадии(self):
        # Задача по сценарию держит в одном списке шаги всех стадий. Если
        # считать все подряд, стадию исполнения нельзя закрыть, пока не сделан
        # шаг ревьюера, который сам живёт на стадии проверки, — то есть условие
        # запирает сценарий на его же последнем шаге.
        состояние = self._задача()
        состояние.set_steps([
            TaskStep(номер=1, имя="backend", источник=ИЗ_СЦЕНАРИЯ, стадия=EXECUTION),
            TaskStep(номер=2, имя="ревьюер", источник=ИЗ_СЦЕНАРИЯ, стадия=VALIDATION),
        ], сценарий="проба")
        состояние.set_plan(["backend", "ревьюер"])
        состояние.утвердить_план("запуск сценария")
        состояние.transition(EXECUTION, "проба")
        состояние.начать_шаг()
        состояние.закончить_шаг("код готов")
        self.assertTrue(Ворота().проверить(состояние, VALIDATION).можно)

    def test_открытый_вопрос_запирает_любой_переход(self):
        состояние = self._задача()
        состояние.set_plan(["шаг"])
        состояние.утвердить_план("человек")
        состояние.остановить(НЕТ_СВЕДЕНИЙ, ОТВЕТ, "шаг «аналитик» спрашивает: какой SRID?")
        вердикт = Ворота().проверить(состояние, EXECUTION)
        self.assertFalse(вердикт.можно)
        self.assertIn("нет-открытых-вопросов", [п.код for п in вердикт.отказ.невыполненные])
        состояние.ответить("SRID 3857")
        self.assertTrue(Ворота().проверить(состояние, EXECUTION).можно)

    def test_личное_условие_ужесточает_переход(self):
        своё = Условие(код="ссылка", откуда=VALIDATION, куда=DONE,
                       что=tr.ЕСТЬ_В_СОБРАННОМ, значение="репозиторий",
                       правило="в готово — только со ссылкой на репозиторий")
        ворота = Ворота(lambda: tr.merge(list(БАЗОВЫЕ), [своё]))
        состояние = self._готовая_к_проверке()
        состояние.записать_валидацию({"вердикт": "прошла", "пункты": []})
        self.assertFalse(ворота.проверить(состояние, DONE).можно)
        состояние.remember("репозиторий проекта", "git@example")
        состояние.записать_валидацию({"вердикт": "прошла", "пункты": []})
        self.assertTrue(ворота.проверить(состояние, DONE).можно)

    def test_отклонённая_попытка_ложится_в_журнал(self):
        состояние = self._задача()
        ворота = Ворота()
        ворота.перевести(состояние, DONE, МОДЕЛЬ)
        self.assertEqual(len(состояние.отказы), 1)
        запись = состояние.отказы[0]
        self.assertEqual(запись["кто"], МОДЕЛЬ)
        self.assertEqual(запись["куда"], DONE)
        self.assertEqual(состояние.stage, PLANNING)

    def test_состоявшийся_переход_помнит_чем_заслужен(self):
        состояние = self._задача()
        состояние.set_plan(["шаг"])
        состояние.утвердить_план("человек")
        вердикт = Ворота().перевести(состояние, EXECUTION, ЧЕЛОВЕК)
        self.assertTrue(вердикт.выполнен)
        последний = состояние.transitions[-1]
        self.assertEqual(последний["кто"], ЧЕЛОВЕК)
        self.assertIn("план-утверждён", [у["код"] for у in последний["условия"]])

    def test_обзор_показывает_все_стадии(self):
        обзор = Ворота().обзор(self._задача())
        self.assertEqual([с["стадия"] for с in обзор], list(STAGES))
        текущая = [с for с in обзор if с["текущая"]]
        self.assertEqual(len(текущая), 1)
        self.assertEqual(текущая[0]["стадия"], PLANNING)

    def test_отказ_словами_содержит_всё_нужное(self):
        отказ = Ворота().проверить(self._задача(), DONE).отказ
        текст = отказ.текст()
        self.assertIn("планирование", текст)
        self.assertIn("маршрут", текст.lower())
        self.assertIn("Стадию задачи меняет код", текст)

    def test_маркер_просьбы_разбирается_и_убирается(self):
        ответ = "Всё сделано, слой отдаётся.\n\nПЕРЕХОД: done"
        self.assertEqual(tr.просьба(ответ), "done")
        self.assertEqual(tr.убрать_маркер(ответ), "Всё сделано, слой отдаётся.")
        self.assertEqual(tr.просьба("просто ответ без маркера"), "")
        # Несколько просьб — берётся последняя: она про итог работы.
        self.assertEqual(tr.просьба("ПЕРЕХОД: execution\nтекст\nПЕРЕХОД: validation"),
                         "validation")


class ПереходыАгента(unittest.TestCase):
    """Ворота внутри агента: отказ, утверждение, проверка, просьба модели."""

    ПЛАН = "1. Разобрать схему\n2. Написать модель\n3. Отдать слой"

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    def _агент(self, ответы: list[str], **kwargs):
        from agent import MemoryAgent
        kwargs.setdefault("require_self_report", False)
        kwargs.setdefault("judge_semantic", False)
        агент = MemoryAgent(base_dir=self.каталог, router_mode=OFF, **kwargs)
        клиент = _Сценарная(ответы)
        агент.client = клиент
        агент.memory.client = клиент
        агент.memory.router.client = клиент
        агент.validator.client = клиент
        агент.заглушка = клиент
        return агент

    def test_прыжок_отклоняется_и_объясняется(self):
        агент = self._агент(["ответ"])
        try:
            агент.start_task("проба", "слой")
            with self.assertRaises(ПереходОтклонён) as поймано:
                агент.transition(DONE)
            self.assertIn("нет такого перехода", str(поймано.exception))
            self.assertEqual(агент.task.stage, PLANNING)
            self.assertEqual(len(агент.task.отказы), 1)
        finally:
            агент.close()

    def test_вердикт_без_перехода_ничего_не_меняет(self):
        агент = self._агент(["ответ"])
        try:
            агент.start_task("проба", "слой")
            вердикт = агент.check_transition(EXECUTION)
            self.assertFalse(вердикт.можно)
            # Проверка — не попытка: журнал отказов остаётся пустым.
            self.assertEqual(агент.task.отказы, [])
        finally:
            агент.close()

    def test_полный_жизненный_цикл_проходится(self):
        агент = self._агент([self.ПЛАН, "код готов",
                             '{"заголовок":"И","решение":"р","причина":"п"}'])
        try:
            агент.start_task("проба", "слой")
            агент.plan()
            with self.assertRaises(ПереходОтклонён):
                агент.transition(EXECUTION)
            агент.approve_plan("Максим")
            агент.transition(EXECUTION)
            for _ in агент.task.шаги:
                агент.task.начать_шаг()
                агент.task.закончить_шаг("сделано")
            агент.transition(VALIDATION)
            агент.remember_step("итог", "слой отдаётся")
            with self.assertRaises(ПереходОтклонён):
                агент.finish_task()
            отчёт = агент.validate_task()
            self.assertEqual(отчёт["вердикт"], "прошла")
            запись = агент.finish_task()
            self.assertTrue(запись["id"])
        finally:
            агент.close()

    def test_просьба_модели_проходит_те_же_ворота(self):
        агент = self._агент(["Задача готова.\n\nПЕРЕХОД: done"])
        try:
            агент.start_task("проба", "слой")
            ответ = агент.ask("Что дальше?")
            self.assertFalse(ответ.переход["можно"])
            self.assertEqual(ответ.переход["кто"], МОДЕЛЬ)
            self.assertEqual(агент.task.stage, PLANNING)
            # Служебная строка наружу не идёт, вместо неё — разбор отказа.
            self.assertNotIn("ПЕРЕХОД: done", ответ.text)
            self.assertIn("Запрос отклонён", ответ.text)
            self.assertEqual(агент.task.отказы[-1]["кто"], МОДЕЛЬ)
        finally:
            агент.close()

    def test_допустимая_просьба_модели_исполняется(self):
        агент = self._агент(["План собран.\n\nПЕРЕХОД: execution"])
        try:
            агент.start_task("проба", "слой")
            агент.task.set_plan(["шаг"])
            агент.approve_plan("Максим")
            ответ = агент.ask("Начинай.")
            self.assertTrue(ответ.переход["можно"])
            self.assertEqual(агент.task.stage, EXECUTION)
            self.assertIn("Стадия задачи переведена", ответ.text)
        finally:
            агент.close()

    def test_служебный_вызов_маркер_не_исполняет(self):
        # Шаг сценария получает машинный вход, и его «ПЕРЕХОД» — это кусок
        # чужого текста, а не обращение к коду. Стадиями там распоряжается
        # исполнитель сценария.
        агент = self._агент(["Итог.\n\nПЕРЕХОД: execution"])
        try:
            агент.start_task("проба", "слой")
            ответ = агент.ask("вход шага", internal=True)
            self.assertEqual(ответ.переход, {})
            self.assertEqual(агент.task.stage, PLANNING)
        finally:
            агент.close()

    def test_сценарий_подписывает_свой_план_и_доходит_до_конца(self):
        агент = self._агент(["собрано", "сделано",
                             '{"заголовок":"И","решение":"р","причина":"п"}'])
        try:
            агент.add_scenario(Scenario(имя="проба", триггеры=["проба"], шаги=[
                Step("аналитик", "собрать", роль="планирование", стадия=PLANNING,
                     вход=["запрос"]),
                Step("backend", "написать", роль="исполнение", стадия=EXECUTION,
                     вход=["аналитик"]),
            ]))
            итог = агент.run_scenario("проба: слой", name="проба")
            self.assertFalse(итог.на_паузе)
            # Сценарий — утверждённый план: подпись в задаче названа запуском.
            self.assertEqual(итог.валидация["вердикт"], "прошла")
            self.assertTrue(итог.решение)
        finally:
            агент.close()

    def test_сценарий_встаёт_на_закрытых_воротах(self):
        агент = self._агент(["собрано", "сделано"])
        try:
            агент.add_condition(Условие(
                код="нужна-ссылка", откуда=PLANNING, куда=EXECUTION,
                что=tr.ЕСТЬ_В_СОБРАННОМ, значение="ссылка на макет",
                правило="к реализации — только с макетом",
                почему="без макета фронтенд переделывают дважды",
                вместо="положите ссылку на макет в собранные данные"))
            агент.add_scenario(Scenario(имя="проба", триггеры=["проба"], шаги=[
                Step("аналитик", "собрать", роль="планирование", стадия=PLANNING,
                     вход=["запрос"]),
                Step("backend", "написать", роль="исполнение", стадия=EXECUTION,
                     вход=["аналитик"]),
            ]))
            итог = агент.run_scenario("проба: слой", name="проба")
            self.assertTrue(итог.на_паузе)
            self.assertEqual(итог.причина_паузы, ЗАКРЫТ_ПЕРЕХОД)
            self.assertEqual(итог.ожидание, РЕШЕНИЕ)
            self.assertIn("нужна-ссылка", итог.отказ_перехода["невыполненные"])
            self.assertEqual(агент.task.stage, PLANNING)
        finally:
            агент.close()

    def test_после_снятия_препятствия_сценарий_продолжается(self):
        # Корректность продолжения после паузы — третья проверка задания дня.
        агент = self._агент(["собрано", "сделано",
                             '{"заголовок":"И","решение":"р","причина":"п"}'])
        try:
            агент.add_condition(Условие(
                код="нужна-ссылка", откуда=PLANNING, куда=EXECUTION,
                что=tr.ЕСТЬ_В_СОБРАННОМ, значение="ссылка на макет",
                правило="к реализации — только с макетом"))
            агент.add_scenario(Scenario(имя="проба", триггеры=["проба"], шаги=[
                Step("аналитик", "собрать", роль="планирование", стадия=PLANNING,
                     вход=["запрос"]),
                Step("backend", "написать", роль="исполнение", стадия=EXECUTION,
                     вход=["аналитик"]),
            ]))
            итог = агент.run_scenario("проба: слой", name="проба")
            self.assertTrue(итог.на_паузе)
            сделано_до = len(итог.шаги)
            агент.remember_step("ссылка на макет", "figma://макет")
            итог2 = агент.resume_scenario(итог.task_id)
            self.assertFalse(итог2.на_паузе)
            # Пройденный шаг не переигрывается: продолжили с того же места.
            self.assertEqual(сделано_до, 1)
            self.assertEqual(len(итог2.шаги), 1)
            self.assertEqual(агент.task, None)
        finally:
            агент.close()

    def test_шаг_задачи_закрывается_руками(self):
        # Задачу, заведённую руками, никто не ведёт по шагам: исполнитель
        # сценария тут не участвует. Без ручного закрытия условие
        # «шаги-доведены» из интерфейса не выполнить, и переход к проверке
        # остался бы закрытым навсегда.
        агент = self._агент(["ответ"])
        try:
            агент.start_task("проба", "слой")
            агент.task.set_plan(["разобрать схему", "написать модель"])
            агент.approve_plan("Максим")
            агент.transition(EXECUTION)
            self.assertFalse(агент.check_transition(VALIDATION).можно)
            агент.close_step("схема разобрана")
            агент.close_step("модель написана")
            self.assertTrue(агент.task.шаги_пройдены)
            self.assertTrue(агент.check_transition(VALIDATION).можно)
            from agent import AgentError
            with self.assertRaises(AgentError):
                агент.close_step()          # открытых шагов больше нет
        finally:
            агент.close()

    def test_условия_берутся_вызовом_а_не_снимком(self):
        агент = self._агент(["ответ"])
        try:
            агент.start_task("проба", "слой")
            агент.task.set_plan(["шаг"])
            агент.approve_plan("Максим")
            self.assertTrue(агент.check_transition(EXECUTION).можно)
            # Условие заводится посреди работы — следующий переход его видит.
            агент.add_condition(Условие(
                код="нужна-ссылка", откуда=PLANNING, куда=EXECUTION,
                что=tr.ЕСТЬ_В_СОБРАННОМ, значение="макет",
                правило="к реализации — только с макетом"))
            self.assertFalse(агент.check_transition(EXECUTION).можно)
        finally:
            агент.close()


# --- MCP (день 16) ---------------------------------------------------------------
#
# Все тесты этого раздела без сети. Настоящее соединение проверяется на своём
# сервере mcp_server.py — он поднимается и по stdio, и по HTTP на свободном
# порту, — а сбои изображают заглушки: команда, которой нет, процесс, который
# падает на старте, сервер, который молчит, и HTTP-сервер, отвечающий 401 и 404.

import json as _json
import socket as _socket
import types
import subprocess as _subprocess
import threading as _threading
from http.server import BaseHTTPRequestHandler as _Обработчик, ThreadingHTTPServer as _HTTPСервер

from unittest import mock

from datetime import datetime as _datetime, timedelta as _timedelta

import agent.mcp as amcp
import pipeline_server
from agent import AgentError, MemoryAgent
from agent.pipeline import runner as конвейеры_прогон, spec as конвейеры_описание
from agent.pipeline import store as конвейеры_хранилище, values as значения
from agent.scheduler import digest as scheduler_digest, schedule as расписание
from agent.scheduler import worker as scheduler_worker
from agent.scheduler.store import (ИСПОЛНЕНО, ОТМЕНЕНО, ScheduleStore, ScheduleStoreError,
                                   окно)
from agent.llm import Reply, _разобрать_вызовы as разобрать_вызовы
from agent.mcp import client as mcp_client
from agent.memory.calls import ВЫПОЛНЕНА, ОТКЛОНЕНА, CallStoreError

КОРЕНЬ_ДНЯ = os.path.dirname(os.path.abspath(__file__))
ИНСТРУМЕНТЫ_СВОЕГО = {"list_users", "list_tasks", "get_task", "list_invariants",
                      "list_transition_conditions", "list_decisions"}


def _файл_серверов(каталог: str, серверы: dict, env_файл: str = "") -> str:
    путь = os.path.join(каталог, "mcp-servers.json")
    with open(путь, "w", encoding="utf-8") as файл:
        _json.dump({"mcpServers": серверы}, файл, ensure_ascii=False)
    if env_файл:
        with open(os.path.join(каталог, ".env"), "w", encoding="utf-8") as файл:
            файл.write(env_файл)
    return путь


def _свой_сервер(память: str, **поля) -> dict:
    описание = {"command": "${PYTHON}", "args": [os.path.join(КОРЕНЬ_ДНЯ, "mcp_server.py")],
                "env": {"MEMORY_DIR": память}, "таймаут": 60}
    описание.update(поля)
    return описание


def _свободный_порт() -> int:
    with _socket.socket() as с:
        с.bind(("127.0.0.1", 0))
        return с.getsockname()[1]


def _наполнить_память(каталог: str) -> None:
    рабочая = WorkingMemory(os.path.join(каталог, "working"))
    рабочая.create("перенос-моделей", "схема gissys в GeoDjango",
                   plan=["описать модели", "перенести права"])
    InvariantStore(os.path.join(каталог, "invariants.json")).add(Invariant(
        код="только-django", правило="Бэкенд — только Django", вид=inv.СТЕК,
        тип=inv.ЗАПРЕТ_СЛОВ, значения=["Laravel"], почему="так решили", вместо="Django"))


class КонфигурацияMCP(unittest.TestCase):
    """mcp-servers.json: формат mcpServers, подстановка секретов, фильтр."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp(prefix="mcp-конфиг-")

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    def загрузить(self, серверы: dict, env_файл: str = "") -> list:
        return amcp.load(_файл_серверов(self.каталог, серверы, env_файл))

    def test_транспорт_по_полям_как_в_стандарте(self):
        серверы = self.загрузить({
            "a": {"command": "npx", "args": ["-y", "пакет"]},
            "b": {"url": "https://example.org/mcp"},
            "c": {"type": "streamable-http", "url": "https://example.org/mcp"},
            "d": {"type": "stdio", "command": "srv"},
        })
        self.assertEqual([с.транспорт for с in серверы], [amcp.STDIO, amcp.HTTP, amcp.HTTP, amcp.STDIO])
        self.assertEqual(серверы[0].аргументы, ["-y", "пакет"])

    def test_нет_файла_пустой_список(self):
        self.assertEqual(amcp.load(os.path.join(self.каталог, "нет.json")), [])

    def test_секрет_подставляется_но_наружу_не_уходит(self):
        сервер, = self.загрузить(
            {"gh": {"type": "http", "url": "https://example.org/mcp",
                    "headers": {"Authorization": "Bearer ${TEST_MCP_TOKEN}"}}},
            env_файл="TEST_MCP_TOKEN=сверхсекрет\n")
        self.assertEqual(сервер.заголовки["Authorization"], "Bearer сверхсекрет")
        self.assertTrue(сервер.готов)
        self.assertNotIn("сверхсекрет", _json.dumps(сервер.to_dict(), ensure_ascii=False))
        self.assertEqual(сервер.to_dict()["заголовки"], ["Authorization"])

    def test_правка_env_видна_без_перезапуска(self):
        """.env главнее окружения: агент сам загрузил его при старте, и правка
        токена при открытой странице иначе не действовала бы."""
        from unittest import mock
        описание = {"gh": {"url": "https://example.org/mcp",
                           "headers": {"Authorization": "Bearer ${TEST_MCP_TOKEN}"}}}
        with mock.patch.dict(os.environ, {"TEST_MCP_TOKEN": "прежний"}):
            сервер, = self.загрузить(описание, env_файл="TEST_MCP_TOKEN=новый\n")
            self.assertEqual(сервер.заголовки["Authorization"], "Bearer новый")
            # пустая строка в .env (как в .env.example) окружение не затирает
            сервер, = self.загрузить(описание, env_файл="TEST_MCP_TOKEN=\n")
            self.assertEqual(сервер.заголовки["Authorization"], "Bearer прежний")

    def test_нет_переменной_сервер_не_готов(self):
        сервер, = self.загрузить({"gh": {"url": "https://example.org/mcp",
                                          "headers": {"Authorization": "Bearer ${MISSING_MCP_TOKEN_XYZ}"}}})
        self.assertFalse(сервер.готов)
        self.assertEqual(сервер.не_хватает, ["MISSING_MCP_TOKEN_XYZ"])
        self.assertIn("MISSING_MCP_TOKEN_XYZ", сервер.почему_не_готов)

    def test_запасное_значение_переменной(self):
        сервер, = self.загрузить({"a": {"command": "srv", "args": ["${MISSING_MCP_TOKEN_XYZ:-запасное}"]}})
        self.assertEqual(сервер.аргументы, ["запасное"])
        self.assertTrue(сервер.готов)

    def test_встроенные_переменные_и_каталог(self):
        сервер, = self.загрузить({"a": {"command": "${PYTHON}", "args": ["${PROJECT_DIR}/s.py"]}})
        self.assertEqual(сервер.команда, sys.executable)
        self.assertEqual(сервер.аргументы, [os.path.join(self.каталог, "s.py")])
        self.assertEqual(сервер.каталог, self.каталог)
        # В показе — как написано: путь к интерпретатору на экране ни к чему.
        self.assertEqual(сервер.куда, "${PYTHON} ${PROJECT_DIR}/s.py")

    def test_относительная_команда_считается_от_файла(self):
        сервер, = self.загрузить({"a": {"command": "./bin/srv"}})
        self.assertEqual(сервер.команда, os.path.join(self.каталог, "bin", "srv"))

    def test_ошибки_описания_понятны(self):
        случаи = {
            "sse": ({"a": {"type": "sse", "url": "https://x/sse"}}, "Streamable HTTP"),
            "тип": ({"a": {"type": "websocket", "url": "https://x"}}, "неизвестный транспорт"),
            "команда": ({"a": {"type": "stdio"}}, "command"),
            "адрес": ({"a": {"type": "http", "url": "ftp://x"}}, "url"),
            "имя": ({"сервер": {"command": "x"}}, "кириллица"),
            "аргументы": ({"a": {"command": "x", "args": [1, 2]}}, "списком строк"),
            "таймаут": ({"a": {"command": "x", "таймаут": -1}}, "таймаут"),
            # кириллица в имени переменной ушла бы на сервер буквальным «${ТОКЕН}»
            "переменная": ({"a": {"url": "https://x/mcp",
                                  "headers": {"Authorization": "Bearer ${ТОКЕН}"}}}, "латиницей"),
        }
        for название, (серверы, фраза) in случаи.items():
            with self.subTest(название):
                with self.assertRaises(amcp.MCPConfigError) as ошибка:
                    self.загрузить(серверы)
                self.assertIn(фраза, str(ошибка.exception))

    def test_битый_файл_и_чужой_формат(self):
        путь = os.path.join(self.каталог, "битый.json")
        pathlib.Path(путь).write_text("{не json", encoding="utf-8")
        with self.assertRaises(amcp.MCPConfigError):
            amcp.load(путь)
        pathlib.Path(путь).write_text('{"servers": {}}', encoding="utf-8")
        with self.assertRaises(amcp.MCPConfigError) as ошибка:
            amcp.load(путь)
        self.assertIn("mcpServers", str(ошибка.exception))

    def test_фильтр_запрет_сильнее_разрешения(self):
        сервер, = self.загрузить({"fs": {"command": "x", "разрешить": ["read_*", "list_*"],
                                          "запретить": ["read_secret*"]}})
        self.assertEqual(сервер.доступ("read_file"), (True, ""))
        self.assertFalse(сервер.доступ("write_file")[0])
        self.assertIn("разрешить", сервер.доступ("write_file")[1])
        закрыт, почему = сервер.доступ("read_secret_key")
        self.assertFalse(закрыт)
        self.assertIn("read_secret*", почему)

    def test_без_фильтра_открыто_всё(self):
        сервер, = self.загрузить({"a": {"command": "x"}})
        self.assertTrue(сервер.доступ("что_угодно")[0])

    def test_отключённый_не_готов(self):
        сервер, = self.загрузить({"a": {"command": "x", "отключён": True}})
        self.assertFalse(сервер.готов)
        self.assertIn("отключён", сервер.почему_не_готов)

    def test_файл_проекта_читается(self):
        """Настоящий mcp-servers.json дня описан верно и секретов в себе не держит."""
        серверы = amcp.load(os.path.join(КОРЕНЬ_ДНЯ, "mcp-servers.json"))
        имена = [с.имя for с in серверы]
        # День 31: свой сервер git (только чтение) — после agent-state.
        self.assertEqual(имена, ["tracker", "tracker-real", "scheduler", "pipeline", "docs",
                                 "agent-state", "git", "filesystem", "everything", "deepwiki",
                                 "github"])
        планировщик = next(с for с in серверы if с.имя == "scheduler")
        # Поля дня 18: «планирует» закрывает обход подтверждения через
        # расписание, «своё» отличает память агента от чужой системы.
        self.assertEqual(планировщик.планирует, {"schedule_job": "tool"})
        self.assertTrue(планировщик.своё)
        # Поле дня 19: запуск цепочки спрашивается по её шагам, а не по пометке
        # самого run_pipeline.
        конвейеры = next(с for с in серверы if с.имя == "pipeline")
        self.assertEqual(конвейеры.конвейеры, {"run_pipeline": "pipeline"})
        self.assertTrue(конвейеры.своё)
        текст = pathlib.Path(КОРЕНЬ_ДНЯ, "mcp-servers.json").read_text(encoding="utf-8")
        self.assertIn("${GITHUB_TOKEN}", текст)
        self.assertIn("${TRACKER_TOKEN}", текст)
        # Ни ключа GitHub, ни OAuth-токена Яндекса в файле быть не может: в нём
        # только ссылки на переменные окружения.
        self.assertNotRegex(текст, r"gh[pous]_[A-Za-z0-9]{20,}|github_pat_|y0__[A-Za-z0-9]{10,}")


class ИнструментMCP(unittest.TestCase):
    """Запись tools/list глазами агента: параметры, доступ, имя и цена для модели."""

    @staticmethod
    def инструмент(**поля):
        import mcp_types
        поля.setdefault("name", "read_file")
        поля.setdefault("inputSchema", {"type": "object", "properties": {}})
        return mcp_types.Tool.model_validate(поля)

    def сервер(self, **поля):
        return amcp.Server(имя="fs", транспорт=amcp.STDIO, команда="x", **поля)

    def test_параметры_типы_и_обязательность(self):
        схема = {"type": "object", "required": ["path"], "properties": {
            "tail": {"type": "integer", "description": "последние строки"},
            "path": {"type": "string", "description": "путь к файлу"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "mode": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "kind": {"enum": ["a", "b"]},
        }}
        и = amcp.Tool.from_sdk(self.инструмент(inputSchema=схема), self.сервер())
        параметры = и.parameters()
        self.assertEqual(параметры[0].имя, "path")          # обязательные — вперёд
        self.assertTrue(параметры[0].обязательный)
        типы = {п.имя: п.тип for п in параметры}
        self.assertEqual(типы, {"path": "string", "tail": "integer", "tags": "array<string>",
                                "mode": "string", "kind": "enum"})
        self.assertEqual(next(п for п in параметры if п.имя == "kind").варианты, ["a", "b"])

    def test_доступ_по_пометкам_сервера(self):
        случаи = [
            (None, "не заявлено"),
            ({"readOnlyHint": True}, "только чтение"),
            ({"readOnlyHint": False, "destructiveHint": False}, "меняет данные"),
            ({"idempotentHint": True}, "может удалять"),   # destructiveHint по умолчанию истинно
        ]
        for пометки, ожидаем in случаи:
            with self.subTest(пометки):
                поля = {"annotations": пометки} if пометки else {}
                и = amcp.Tool.from_sdk(self.инструмент(**поля), self.сервер())
                self.assertEqual(и.доступ, ожидаем)

    def test_фильтр_сервера_применяется_к_инструменту(self):
        сервер = self.сервер(разрешить=["list_*"])
        открыт = amcp.Tool.from_sdk(self.инструмент(name="list_dir"), сервер)
        закрыт = amcp.Tool.from_sdk(self.инструмент(name="write_file"), сервер)
        self.assertTrue(открыт.разрешён)
        self.assertFalse(закрыт.разрешён)
        self.assertTrue(закрыт.почему_закрыт)

    def test_имя_для_модели_без_недопустимых_знаков(self):
        self.assertEqual(amcp.model_name("github", "get.file"), "github__get_file")
        self.assertLessEqual(len(amcp.model_name("s" * 40, "t" * 60)), 64)
        и = amcp.Tool.from_sdk(self.инструмент(name="get-annotated-message"), self.сервер())
        описание = и.for_model()
        self.assertEqual(описание["type"], "function")
        self.assertRegex(описание["function"]["name"], r"^[A-Za-z0-9_-]{1,64}$")

    def test_цена_растёт_вместе_с_описанием(self):
        коротко = amcp.Tool.from_sdk(self.инструмент(description="Читает файл."), self.сервер())
        длинно = amcp.Tool.from_sdk(self.инструмент(description="Читает файл. " * 40), self.сервер())
        self.assertGreater(коротко.tokens(), 0)
        # Обвязка схемы у обоих одна, разница — ровно в тридцати девяти повторах.
        self.assertGreater(длинно.tokens() - коротко.tokens(), 100)


class ПостраничныйСписок(unittest.TestCase):
    """tools/list может прийти частями — собрать нужно все страницы."""

    class _Страницы:
        def __init__(self, страниц: int, бесконечно: bool = False):
            self.страниц, self.бесконечно, self.курсоры = страниц, бесконечно, []

        async def list_tools(self, cursor=None):
            from types import SimpleNamespace
            self.курсоры.append(cursor)
            номер = len(self.курсоры)
            следующая = None if (номер >= self.страниц and not self.бесконечно) else f"с{номер}"
            return SimpleNamespace(tools=[f"инструмент-{номер}"], next_cursor=следующая)

    def test_все_страницы_по_курсору(self):
        import anyio
        клиент = self._Страницы(3)
        инструменты, страниц = anyio.run(mcp_client._все_инструменты, клиент)
        self.assertEqual(страниц, 3)
        self.assertEqual(инструменты, ["инструмент-1", "инструмент-2", "инструмент-3"])
        self.assertEqual(клиент.курсоры, [None, "с1", "с2"])

    def test_бесконечный_курсор_не_вешает(self):
        import anyio
        _, страниц = anyio.run(mcp_client._все_инструменты, self._Страницы(1, бесконечно=True))
        self.assertEqual(страниц, mcp_client.ПРЕДЕЛ_СТРАНИЦ)


class СвойСерверMCP(unittest.TestCase):
    """mcp_server.py: инструменты только читают и не пускают за пределы памяти."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.каталог = tempfile.mkdtemp(prefix="mcp-память-")
        _наполнить_память(cls.каталог)
        cls.прежняя = os.environ.get("MEMORY_DIR")
        os.environ["MEMORY_DIR"] = cls.каталог
        import mcp_server
        cls.модуль = mcp_server

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.прежняя is None:
            os.environ.pop("MEMORY_DIR", None)
        else:
            os.environ["MEMORY_DIR"] = cls.прежняя
        shutil.rmtree(cls.каталог, ignore_errors=True)

    def вызвать(self, имя: str, аргументы: dict | None = None):
        import anyio
        from mcp import Client

        async def вызов():
            async with Client(self.модуль.сервер) as клиент:
                return await клиент.call_tool(имя, аргументы or {})

        return anyio.run(вызов)

    def test_список_инструментов_и_пометки(self):
        import anyio
        from mcp import Client

        async def список():
            async with Client(self.модуль.сервер) as клиент:
                return (await клиент.list_tools()).tools

        инструменты = anyio.run(список)
        self.assertEqual({и.name for и in инструменты}, ИНСТРУМЕНТЫ_СВОЕГО)
        for и in инструменты:
            with self.subTest(и.name):
                self.assertTrue(и.annotations.read_only_hint)
                self.assertFalse(и.annotations.destructive_hint)
                self.assertTrue(и.description)

    def test_задачи_и_состояние_задачи(self):
        итог = self.вызвать("list_tasks")
        self.assertFalse(итог.is_error)
        self.assertEqual([з["task_id"] for з in итог.structured_content["задачи"]], ["перенос-моделей"])
        задача = self.вызвать("get_task", {"task_id": "перенос-моделей"}).structured_content
        self.assertEqual(задача["стадия"], PLANNING)
        закрытые = {п["стадия"]: п for п in задача["переходы"]}
        self.assertFalse(закрытые[EXECUTION]["можно"])     # план не подписан
        self.assertIn("план-утверждён", [у["код"] for у in закрытые[EXECUTION]["чего_не_хватает"]])

    def test_инварианты_проекта(self):
        итог = self.вызвать("list_invariants").structured_content
        self.assertIn("только-django", [и["код"] for и in итог["инварианты"]])

    def test_условия_переходов_включают_базовые(self):
        итог = self.вызвать("list_transition_conditions").structured_content
        self.assertTrue({у.код for у in БАЗОВЫЕ} <= {у["код"] for у in итог["условия"]})

    def test_имя_из_запроса_не_выводит_за_память(self):
        """«../../.env» вместо имени — та самая «логическая бомба» из лекции."""
        for имя in ("../../.env", "a/b", ""):
            with self.subTest(имя):
                итог = self.вызвать("list_decisions", {"user": имя})
                if имя:
                    self.assertTrue(итог.is_error)
                    self.assertIn("Недопустимое имя", итог.content[0].text)
                else:
                    self.assertFalse(итог.is_error)     # пусто — пользователь по умолчанию

    def test_чужая_задача_ошибка_а_не_падение(self):
        итог = self.вызвать("get_task", {"task_id": "нет-такой"})
        self.assertTrue(итог.is_error)

    def test_сервер_ничего_не_создаёт(self):
        пустой = tempfile.mkdtemp(prefix="mcp-пусто-")
        try:
            os.environ["MEMORY_DIR"] = пустой
            self.assertEqual(self.вызвать("list_tasks").structured_content, {"задачи": []})
            self.assertEqual(self.вызвать("list_users").structured_content["пользователи"], [])
            self.assertEqual(os.listdir(пустой), [])
        finally:
            os.environ["MEMORY_DIR"] = self.каталог
            shutil.rmtree(пустой, ignore_errors=True)


class СоединениеMCP(unittest.TestCase):
    """Настоящее соединение: процесс stdio и Streamable HTTP на своём сервере."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.каталог = tempfile.mkdtemp(prefix="mcp-соединение-")
        cls.память = os.path.join(cls.каталог, "memory")
        _наполнить_память(cls.память)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.каталог, ignore_errors=True)

    def сервер(self, **поля) -> "amcp.Server":
        сервер, = amcp.load(_файл_серверов(self.каталог, {"agent-state": _свой_сервер(self.память, **поля)}))
        return сервер

    def test_stdio_рукопожатие_и_список(self):
        осмотр = amcp.inspect(self.сервер())
        self.assertTrue(осмотр.ок, f"{осмотр.этап}: {осмотр.ошибка}\n{осмотр.журнал}")
        р = осмотр.рукопожатие
        self.assertEqual(р.имя, "agent-state")
        self.assertTrue(р.протокол)
        self.assertIn(р.способ, ("server/discover", "initialize"))
        self.assertIn("tools", р.возможности)
        self.assertEqual({и.имя for и in осмотр.инструменты}, ИНСТРУМЕНТЫ_СВОЕГО)
        self.assertTrue(all(и.доступ == "только чтение" for и in осмотр.инструменты))
        self.assertGreaterEqual(осмотр.страниц, 1)
        self.assertGreater(осмотр.токенов, 0)

    def test_список_по_stdio_совпадает_с_тем_что_сервер_объявил(self):
        """Через транспорт пришло ровно то, что сервер объявил у себя, со схемами."""
        import anyio
        from mcp import Client
        import mcp_server

        async def у_себя():
            async with Client(mcp_server.сервер) as клиент:
                return (await клиент.list_tools()).tools

        объявлено = {и.name: и.input_schema for и in anyio.run(у_себя)}
        пришло = {и.имя: и.входная_схема for и in amcp.inspect(self.сервер()).инструменты}
        self.assertEqual(пришло, объявлено)

    def test_http_транспорт(self):
        порт = _свободный_порт()
        процесс = _subprocess.Popen(
            [sys.executable, os.path.join(КОРЕНЬ_ДНЯ, "mcp_server.py"), "--http", str(порт),
             "--память-в", self.память],
            stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL, stdin=_subprocess.DEVNULL)
        try:
            предел = time.monotonic() + 30
            while time.monotonic() < предел:
                try:
                    _socket.create_connection(("127.0.0.1", порт), timeout=0.5).close()
                    break
                except OSError:
                    time.sleep(0.2)
            сервер, = amcp.load(_файл_серверов(self.каталог, {
                "agent-http": {"type": "http", "url": f"http://127.0.0.1:{порт}/mcp"}}))
            осмотр = amcp.inspect(сервер)
            self.assertTrue(осмотр.ок, осмотр.ошибка)
            self.assertEqual(сервер.транспорт, amcp.HTTP)
            self.assertEqual({и.имя for и in осмотр.инструменты}, ИНСТРУМЕНТЫ_СВОЕГО)
        finally:
            процесс.terminate()
            процесс.wait(timeout=10)

    def test_сеанс_переживает_несколько_запросов(self):
        with amcp.Session(self.сервер()) as сеанс:
            первый, _ = сеанс.tools()
            второй, _ = сеанс.tools()
        self.assertEqual([и.имя for и in первый], [и.имя for и in второй])

    def test_фильтр_закрывает_но_показывает(self):
        осмотр = amcp.inspect(self.сервер(разрешить=["list_*"]))
        закрытые = {и.имя for и in осмотр.инструменты if not и.разрешён}
        self.assertEqual(закрытые, {"get_task"})
        self.assertEqual(len(осмотр.инструменты), len(ИНСТРУМЕНТЫ_СВОЕГО))
        self.assertLess(осмотр.токенов, осмотр.токенов_всех)


class _Заглушка(_Обработчик):
    """HTTP-сервер, который отвечает отказом: 401 с WWW-Authenticate или 404."""

    статус = 401

    def do_POST(self):  # noqa: N802 — имя требует http.server
        self.send_response(self.статус)
        if self.статус == 401:
            self.send_header("WWW-Authenticate",
                             'Bearer error="invalid_token", error_description="Token expired"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"nope")

    do_GET = do_DELETE = do_POST

    def log_message(self, *args):
        pass


class СбоиMCP(unittest.TestCase):
    """Каждый сбой — понятная причина и подсказка, и ни один не вешает агента."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp(prefix="mcp-сбои-")

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    def осмотр(self, описание: dict):
        сервер, = amcp.load(_файл_серверов(self.каталог, {"s": описание}))
        return amcp.inspect(сервер)

    def заглушка(self, статус: int):
        класс = type("З", (_Заглушка,), {"статус": статус})
        сервер = _HTTPСервер(("127.0.0.1", 0), класс)
        _threading.Thread(target=сервер.serve_forever, daemon=True).start()
        # Cleanup выполняется в обратном порядке: остановить цикл, затем
        # закрыть listening socket, чтобы ResourceWarning не скрывал утечки.
        self.addCleanup(сервер.server_close)
        self.addCleanup(сервер.shutdown)
        return f"http://127.0.0.1:{сервер.server_address[1]}/mcp"

    def test_нет_команды(self):
        о = self.осмотр({"command": "нет-такой-команды-xyz"})
        self.assertFalse(о.ок)
        self.assertEqual(о.этап, mcp_client.ЗАПУСК)
        self.assertIn("не найдена команда", о.ошибка)

    def test_процесс_падает_на_старте_и_виден_его_журнал(self):
        о = self.осмотр({"command": sys.executable,
                         "args": ["-c", "import sys; sys.stderr.write('упал на старте\\n'); sys.exit(3)"]})
        self.assertFalse(о.ок)
        self.assertIn("закрыл соединение", о.ошибка)
        self.assertIn("упал на старте", о.журнал)

    def test_молчащий_сервер_упирается_в_таймаут(self):
        начало = time.monotonic()
        о = self.осмотр({"command": sys.executable, "args": ["-c", "import time; time.sleep(60)"],
                         "таймаут": 1.5})
        self.assertFalse(о.ок)
        self.assertIn("не ответил за 1.5 с", о.ошибка)
        self.assertLess(time.monotonic() - начало, 15, "таймаут не сработал — агент повис бы")

    def test_закрытый_порт(self):
        о = self.осмотр({"type": "http", "url": f"http://127.0.0.1:{_свободный_порт()}/mcp",
                         "таймаут": 5})
        self.assertFalse(о.ок)
        self.assertIn("нет соединения", о.ошибка)

    def test_отказ_авторизации_назван_прямо(self):
        о = self.осмотр({"type": "http", "url": self.заглушка(401), "таймаут": 5})
        self.assertFalse(о.ок)
        self.assertIn("отклонил авторизацию (HTTP 401: Token expired)", о.ошибка)
        self.assertIn("токен", о.подсказка)

    def test_ключи_агента_не_уходят_стороннему_серверу(self):
        """stdio-сервер получает только безопасный минимум окружения и своё env.

        Агент держит в окружении ключи провайдеров (load_dotenv), а в списке
        серверов бывают чужие пакеты из npm. У эталонного сервера есть даже
        инструмент get-env, который отдаёт окружение модели целиком.
        """
        from unittest import mock
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "проверка"}):
            о = self.осмотр({"command": sys.executable, "env": {"SERVER_OWN": "1"}, "args": [
                "-c", "import os, sys; sys.stderr.write(' '.join(sorted(os.environ))); sys.exit(1)"]})
        окружение = set(о.журнал.split())
        self.assertIn("SERVER_OWN", окружение)
        self.assertIn("PATH", окружение)
        self.assertNotIn("DEEPSEEK_API_KEY", окружение)
        self.assertFalse({и for и in окружение if и.endswith("_API_KEY")})

    def test_кириллица_в_токене_названа_прямо(self):
        о = self.осмотр({"type": "http", "url": self.заглушка(401), "таймаут": 5,
                         "headers": {"Authorization": "Bearer токен-по-русски"}})
        self.assertFalse(о.ок)
        self.assertIn("не-латинские символы", о.ошибка)
        self.assertIn(".env", о.подсказка)

    def test_не_тот_адрес(self):
        о = self.осмотр({"type": "http", "url": self.заглушка(404), "таймаут": 5})
        self.assertFalse(о.ок)
        self.assertIn("HTTP 404", о.ошибка)

    def test_не_настроенный_даже_не_подключается(self):
        о = self.осмотр({"url": "https://example.org/mcp",
                         "headers": {"Authorization": "Bearer ${MISSING_MCP_TOKEN_XYZ}"}})
        self.assertTrue(о.пропущен)
        self.assertLess(о.всего_с, 0.5)

    def test_реестр_не_обрывается_на_сломанном(self):
        память = os.path.join(self.каталог, "memory")
        путь = _файл_серверов(self.каталог, {
            "agent-state": _свой_сервер(память),
            "broken": {"command": "нет-такой-команды-xyz"},
            "no-token": {"url": "https://example.org/mcp", "headers": {"X": "${MISSING_MCP_TOKEN_XYZ}"}},
        })
        реестр = amcp.Registry(путь)
        осмотры = реестр.inspect()
        self.assertEqual([о.сервер.имя for о in осмотры], ["agent-state", "broken", "no-token"])
        итог = amcp.summary(осмотры)
        self.assertEqual((итог["подключено"], итог["сбоев"], итог["пропущено"]), (1, 1, 1))
        self.assertEqual(итог["инструментов"], len(ИНСТРУМЕНТЫ_СВОЕГО))
        with self.assertRaises(amcp.MCPConfigError):
            реестр.server("нет-такого")


class КонсольMCP(unittest.TestCase):
    """cli.py --mcp: настоящий процесс, закрытый stdin, диалог не открывается."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.каталог = tempfile.mkdtemp(prefix="mcp-консоль-")
        cls.файл = _файл_серверов(cls.каталог, {
            "agent-state": _свой_сервер(os.path.join(cls.каталог, "memory")),
            "broken": {"command": "нет-такой-команды-xyz"},
            # Токен есть, значит сервер готов и к нему подключатся. Адрес поэтому
            # локальный и закрытый: сети в этих тестах нет.
            "gh": {"url": f"http://127.0.0.1:{_свободный_порт()}/mcp", "таймаут": 5,
                   "headers": {"Authorization": "Bearer ${TEST_MCP_TOKEN}"}},
        }, env_файл="TEST_MCP_TOKEN=сверхсекрет\n")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.каталог, ignore_errors=True)

    def консоль(self, *ключи: str):
        итог = _subprocess.run(
            [sys.executable, os.path.join(КОРЕНЬ_ДНЯ, "cli.py"), "--mcp-файл", self.файл, *ключи],
            capture_output=True, text=True, stdin=_subprocess.DEVNULL, timeout=120,
            env={**os.environ, "MEMORY_DIR": os.path.join(self.каталог, "memory")})
        вывод = итог.stdout + итог.stderr
        self.assertNotIn("Вы:", вывод, "команда открыла диалог")
        return итог.returncode, вывод

    def test_ключи_разбираются(self):
        import cli
        разбор = cli.build_parser()
        self.assertEqual(разбор.parse_args(["--mcp"]).mcp, "*")
        self.assertEqual(разбор.parse_args(["--mcp", "deepwiki"]).mcp, "deepwiki")
        self.assertEqual(разбор.parse_args([]).mcp, "")

    def test_список_серверов_без_подключения_и_без_секрета(self):
        код, вывод = self.консоль("--mcp-серверы")
        self.assertEqual(код, 0, вывод)
        for имя in ("agent-state", "broken", "gh"):
            self.assertIn(имя, вывод)
        self.assertNotIn("сверхсекрет", вывод)

    def test_один_сервер_соединение_и_инструменты(self):
        код, вывод = self.консоль("--mcp", "agent-state")
        self.assertEqual(код, 0, вывод)
        self.assertIn("Соединение: установлено", вывод)
        for имя in ИНСТРУМЕНТЫ_СВОЕГО:
            self.assertIn(имя, вывод)
        self.assertIn("task_id*", вывод)
        self.assertIn("Подключено 1 из 1", вывод)

    def test_схема_по_ключу(self):
        код, вывод = self.консоль("--mcp", "agent-state", "--mcp-схема")
        self.assertEqual(код, 0, вывод)
        self.assertIn('"properties"', вывод)

    def test_сбой_виден_и_даёт_код_ошибки(self):
        код, вывод = self.консоль("--mcp")
        self.assertEqual(код, 1, вывод)
        self.assertIn("НЕ установлено", вывод)
        self.assertIn("не найдена команда", вывод)
        self.assertIn("Подключено 1 из 3", вывод)
        self.assertIn("сбоев 2", вывод)
        self.assertNotIn("сверхсекрет", вывод)

    def test_неизвестный_сервер(self):
        код, вывод = self.консоль("--mcp", "нет-такого")
        self.assertEqual(код, 1)
        self.assertIn("нет-такого", вывод)


class ВебMCP(unittest.TestCase):
    """Панель MCP на странице: те же возможности, что у --mcp в консоли."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.каталог = tempfile.mkdtemp(prefix="mcp-веб-")
        cls.файл = _файл_серверов(cls.каталог, {
            "agent-state": _свой_сервер(os.path.join(cls.каталог, "memory")),
            "broken": {"command": "нет-такой-команды-xyz"},
            # Токен есть, значит сервер готов и к нему подключатся. Адрес поэтому
            # локальный и закрытый: сети в этих тестах нет.
            "gh": {"url": f"http://127.0.0.1:{_свободный_порт()}/mcp", "таймаут": 5,
                   "headers": {"Authorization": "Bearer ${TEST_MCP_TOKEN}"}},
        }, env_файл="TEST_MCP_TOKEN=сверхсекрет\n")
        os.environ["MEMORY_DIR"] = os.path.join(cls.каталог, "memory")
        os.environ["MCP_CONFIG"] = cls.файл
        import importlib
        import web as модуль
        cls.web = importlib.reload(модуль)
        cls.клиент = cls.web.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        os.environ.pop("MEMORY_DIR", None)
        os.environ.pop("MCP_CONFIG", None)
        shutil.rmtree(cls.каталог, ignore_errors=True)

    def test_страница_помнит_панели_прошлых_дней(self):
        # Заголовок каждого дня свой (его проверяет тест дня), а панели —
        # наследство: пропажа любой из них означает, что новый день сломал
        # старый, и заметить это должен тест, а не человек в браузере.
        # День 28: на главной — то, что лекция недели велит применить к локальной
        # модели: инструменты MCP и заявки. Планировщик, конвейеры и оркестр
        # ушли из веба (spec § 9) — они в консоли и в своих MCP-серверах.
        html = self.клиент.get("/").get_data(as_text=True)
        self.assertIn("<title>Агент миграции ГИС —", html)
        self.assertIn('id="mcp-панель"', html)
        self.assertIn('id="заявки-панель"', html)
        self.assertIn('id="инструменты-вкл"', html)
        self.assertIn("mcp-servers.json", html)
        for убрано in ('id="планировщик-панель"', 'id="конвейеры-панель"', 'id="оркестр-панель"',
                       "scheduler.db", "pipelines.json", "flow-rules.json"):
            self.assertNotIn(убрано, html)

    def test_список_серверов(self):
        ответ = self.клиент.get("/api/mcp")
        self.assertEqual(ответ.status_code, 200)
        данные = ответ.get_json()
        self.assertEqual([с["имя"] for с in данные["servers"]], ["agent-state", "broken", "gh"])
        self.assertNotIn("сверхсекрет", ответ.get_data(as_text=True))

    def test_осмотр_одного_сервера(self):
        данные = self.клиент.post("/api/mcp/inspect", json={"server": "agent-state"}).get_json()
        осмотр, = данные["inspections"]
        self.assertTrue(осмотр["ок"], осмотр["ошибка"])
        self.assertEqual({и["имя"] for и in осмотр["инструменты"]}, ИНСТРУМЕНТЫ_СВОЕГО)
        self.assertEqual(осмотр["рукопожатие"]["имя"], "agent-state")
        self.assertGreater(данные["summary"]["токенов"], 0)

    def test_осмотр_всех_со_сбоем_не_ошибка_запроса(self):
        ответ = self.клиент.post("/api/mcp/inspect", json={})
        self.assertEqual(ответ.status_code, 200)
        итог = ответ.get_json()["summary"]
        self.assertEqual((итог["подключено"], итог["сбоев"], итог["пропущено"]), (1, 2, 0))
        self.assertNotIn("сверхсекрет", ответ.get_data(as_text=True))

    def test_неизвестный_сервер_и_битый_файл(self):
        self.assertEqual(self.клиент.post("/api/mcp/inspect", json={"server": "нет"}).status_code, 400)
        битый = os.path.join(self.каталог, "битый.json")
        pathlib.Path(битый).write_text("{", encoding="utf-8")
        os.environ["MCP_CONFIG"] = битый
        try:
            self.assertEqual(self.клиент.get("/api/mcp").status_code, 400)
        finally:
            os.environ["MCP_CONFIG"] = self.файл



# --- День 17: свой инструмент MCP вокруг API ----------------------------------

ИНСТРУМЕНТЫ_ТРЕКЕРА = {"list_queues", "list_issues", "get_issue"}
МЕНЯЮЩИЕ_ТРЕКЕРА = {"add_comment", "move_issue"}


def _трекер_клиент(каталог: str, **поля):
    """Мок-API трекера как WSGI-приложение: без порта и без сети."""
    import tracker_api

    поля.setdefault("данные", os.path.join(каталог, "tracker-data.json"))
    поля.setdefault("сброс", True)
    поля.setdefault("токен", "mock-token")
    поля.setdefault("орг", "mock-org")
    return tracker_api.создать(**поля)


def _трекер(приложение, **поля):
    """Клиент MCP-сервера к моку — через WSGI-транспорт httpx, в этом же процессе."""
    import httpx
    import tracker_server

    поля.setdefault("токен", "mock-token")
    поля.setdefault("организация", "mock-org")
    http = httpx.Client(transport=httpx.WSGITransport(app=приложение),
                        base_url="http://tracker.local")
    return tracker_server.Трекер(адрес="http://tracker.local", http=http, **поля)


def _вызвать(сервер, имя: str, аргументы: dict | None = None):
    """Вызов инструмента через настоящий клиент SDK, но без транспорта."""
    import anyio
    from mcp import Client

    async def вызов():
        async with Client(сервер) as клиент:
            return await клиент.call_tool(имя, аргументы or {})

    return anyio.run(вызов)


def _инструменты(сервер) -> list:
    import anyio
    from mcp import Client

    async def список():
        async with Client(сервер) as клиент:
            return (await клиент.list_tools()).tools

    return anyio.run(список)


def _текст(итог) -> str:
    return итог.content[0].text if итог.content else ""


class МокТрекера(unittest.TestCase):
    """tracker_api.py: чужой API, вокруг которого построен MCP-сервер дня.

    Проверяется именно то, чем API отличается от файла: авторизация, коды
    ответов, постраничность и жизненный цикл статусов. Если мок будет вести
    себя иначе, чем настоящий Трекер, обёртка окажется проверенной впустую.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.каталог = tempfile.mkdtemp(prefix="трекер-мок-")
        cls.приложение = _трекер_клиент(cls.каталог)
        cls.клиент = cls.приложение.test_client()
        cls.заголовки = {"Authorization": "OAuth mock-token", "X-Cloud-Org-ID": "mock-org"}

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.каталог, ignore_errors=True)

    def test_без_токена_401(self):
        ответ = self.клиент.get("/v3/myself")
        self.assertEqual(ответ.status_code, 401)
        self.assertIn("Unauthorized", ответ.get_json()["errorMessages"])

    def test_фикстура_не_зависит_от_tracker_env_и_сохраняет_401(self):
        каталог = tempfile.mkdtemp(prefix="трекер-окружение-")
        self.addCleanup(shutil.rmtree, каталог, True)
        with mock.patch.dict(os.environ, {
                "TRACKER_MOCK_TOKEN": "hostile-environment-token",
                "TRACKER_MOCK_ORG": "hostile-environment-org",
        }):
            клиент = _трекер_клиент(каталог).test_client()
        верный = клиент.get("/v3/myself", headers={
            "Authorization": "OAuth mock-token", "X-Cloud-Org-ID": "mock-org"})
        неверный = клиент.get("/v3/myself", headers={
            "Authorization": "OAuth hostile-environment-token", "X-Cloud-Org-ID": "mock-org"})
        self.assertEqual(верный.status_code, 200)
        self.assertEqual(неверный.status_code, 401)

    def test_чужая_организация_403(self):
        ответ = self.клиент.get("/v3/myself", headers={
            "Authorization": "OAuth mock-token", "X-Cloud-Org-ID": "чужая"})
        self.assertEqual(ответ.status_code, 403)

    def test_оба_заголовка_организации_годятся(self):
        """У Яндекс 360 организация приходит в X-Org-ID, у Облака — в X-Cloud-Org-ID."""
        for заголовок in ("X-Org-ID", "X-Cloud-Org-ID"):
            with self.subTest(заголовок):
                ответ = self.клиент.get("/v3/myself", headers={
                    "Authorization": "OAuth mock-token", заголовок: "mock-org"})
                self.assertEqual(ответ.status_code, 200)

    def test_поиск_по_фильтру_и_счётчик(self):
        ответ = self.клиент.post("/v3/issues/_search", headers=self.заголовки,
                                 json={"filter": {"queue": "MIG", "status": "open"}})
        ключи = [з["key"] for з in ответ.get_json()]
        self.assertTrue(ключи)
        self.assertTrue(all(к.startswith("MIG-") for к in ключи))
        self.assertEqual(int(ответ.headers["X-Total-Count"]), len(ключи))

    def test_поиск_по_тексту_ищет_и_в_описании(self):
        ответ = self.клиент.post("/v3/issues/_search", headers=self.заголовки,
                                 json={"query": "ST_AsMVT"})
        self.assertEqual([з["key"] for з in ответ.get_json()], ["MIG-3"])

    def test_постранично(self):
        первая = self.клиент.post("/v3/issues/_search", headers=self.заголовки,
                                  json={"filter": {}}, query_string={"perPage": 3, "page": 1})
        вторая = self.клиент.post("/v3/issues/_search", headers=self.заголовки,
                                  json={"filter": {}}, query_string={"perPage": 3, "page": 2})
        self.assertEqual(len(первая.get_json()), 3)
        self.assertTrue(вторая.get_json())
        self.assertNotEqual([з["key"] for з in первая.get_json()],
                            [з["key"] for з in вторая.get_json()])
        self.assertGreater(int(первая.headers["X-Total-Count"]), 3)

    def test_задачи_нет_404(self):
        ответ = self.клиент.get("/v3/issues/MIG-404", headers=self.заголовки)
        self.assertEqual(ответ.status_code, 404)
        self.assertIn("MIG-404", ответ.get_json()["errorMessages"][0])

    def test_комментарий_добавляется_и_виден(self):
        добавлен = self.клиент.post("/v3/issues/MIG-7/comments", headers=self.заголовки,
                                    json={"text": "проверка"})
        self.assertEqual(добавлен.status_code, 201)
        тексты = [к["text"] for к in
                  self.клиент.get("/v3/issues/MIG-7/comments", headers=self.заголовки).get_json()]
        self.assertIn("проверка", тексты)

    def test_пустой_комментарий_отклонён(self):
        ответ = self.клиент.post("/v3/issues/MIG-7/comments", headers=self.заголовки,
                                 json={"text": "   "})
        self.assertEqual(ответ.status_code, 422)

    def test_переход_меняет_статус_и_пишет_комментарий(self):
        ответ = self.клиент.post("/v3/issues/MIG-6/transitions/to_inProgress/_execute",
                                 headers=self.заголовки, json={"comment": "взяли"})
        self.assertEqual(ответ.status_code, 200)
        задача = self.клиент.get("/v3/issues/MIG-6", headers=self.заголовки).get_json()
        self.assertEqual(задача["status"]["key"], "inProgress")
        self.assertEqual(задача["statusType"]["key"], "inProgress")
        тексты = [к["text"] for к in
                  self.клиент.get("/v3/issues/MIG-6/comments", headers=self.заголовки).get_json()]
        self.assertIn("взяли", тексты)

    def test_недопустимый_переход_называет_доступные(self):
        ответ = self.клиент.post("/v3/issues/MIG-7/transitions/to_closed/_execute",
                                 headers=self.заголовки, json={})
        self.assertEqual(ответ.status_code, 422)
        self.assertIn("to_inProgress", ответ.get_json()["errorMessages"][0])

    def test_лимит_запросов(self):
        каталог = tempfile.mkdtemp(prefix="трекер-лимит-")
        try:
            клиент = _трекер_клиент(каталог, лимит=2).test_client()
            коды = [клиент.get("/v3/myself", headers=self.заголовки).status_code
                    for _ in range(4)]
            self.assertEqual(коды[:2], [200, 200])
            self.assertEqual(коды[2], 429)
            ответ = клиент.get("/v3/myself", headers=self.заголовки)
            self.assertEqual(ответ.headers.get("Retry-After"), "60")
        finally:
            shutil.rmtree(каталог, ignore_errors=True)


class СерверТрекера(unittest.TestCase):
    """tracker_server.py: регистрация инструментов, входные параметры, результат."""

    @classmethod
    def setUpClass(cls) -> None:
        import tracker_server

        cls.каталог = tempfile.mkdtemp(prefix="трекер-mcp-")
        cls.приложение = _трекер_клиент(cls.каталог)
        cls.модуль = tracker_server
        cls.сервер = tracker_server.создать_сервер(_трекер(cls.приложение), запись=True)
        cls.только_чтение = tracker_server.создать_сервер(_трекер(cls.приложение), запись=False)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.каталог, ignore_errors=True)

    # --- регистрация инструмента ---------------------------------------------

    def test_инструменты_зарегистрированы_с_пометками(self):
        инструменты = {и.name: и for и in _инструменты(self.сервер)}
        self.assertEqual(set(инструменты), ИНСТРУМЕНТЫ_ТРЕКЕРА | МЕНЯЮЩИЕ_ТРЕКЕРА)
        for имя in ИНСТРУМЕНТЫ_ТРЕКЕРА:
            self.assertTrue(инструменты[имя].annotations.read_only_hint, имя)
        for имя in МЕНЯЮЩИЕ_ТРЕКЕРА:
            self.assertFalse(инструменты[имя].annotations.read_only_hint, имя)
            # Инструмент ходит в чужую систему — это и есть «открытый мир».
            self.assertTrue(инструменты[имя].annotations.open_world_hint, имя)
        for инструмент in инструменты.values():
            self.assertTrue(инструмент.description, инструмент.name)
            self.assertTrue(инструмент.title, инструмент.name)

    def test_без_разрешения_меняющих_инструментов_нет(self):
        """Тот же сервер у настоящего Трекера отдаёт список короче — это и защита."""
        имена = {и.name for и in _инструменты(self.только_чтение)}
        self.assertEqual(имена, ИНСТРУМЕНТЫ_ТРЕКЕРА)

    # --- описание входных параметров -----------------------------------------

    def test_схема_входа_описывает_параметры(self):
        схемы = {и.name: и.input_schema for и in _инструменты(self.сервер)}
        задача = схемы["get_issue"]
        self.assertEqual(задача["required"], ["key"])
        self.assertIn("MIG-2", задача["properties"]["key"]["description"])
        комментарии = задача["properties"]["comments"]
        self.assertEqual((комментарии["minimum"], комментарии["maximum"]), (0, 20))
        список = схемы["list_issues"]["properties"]
        self.assertNotIn("required", схемы["list_issues"])   # все параметры необязательны
        self.assertIn("inProgress", список["status"]["description"])
        self.assertEqual((список["limit"]["minimum"], список["limit"]["maximum"]), (1, 50))
        for имя, поле in список.items():
            self.assertTrue(поле.get("description"), имя)

    def test_предел_параметра_проверяет_sdk(self):
        итог = _вызвать(self.сервер, "list_issues", {"limit": 99})
        self.assertTrue(итог.is_error)
        self.assertIn("less than or equal to 50", _текст(итог))

    # --- возврат результата ---------------------------------------------------

    def test_список_задач_фильтруется(self):
        итог = _вызвать(self.сервер, "list_issues", {"queue": "MIG", "status": "open"})
        self.assertFalse(итог.is_error)
        данные = итог.structured_content
        self.assertEqual(данные["показано"], len(данные["задачи"]))
        self.assertTrue(все := данные["задачи"])
        self.assertTrue(all(з["статус_код"] == "open" for з in все))
        self.assertTrue(all(з["очередь"] == "MIG" for з in все))

    def test_задача_целиком_с_комментариями_и_переходами(self):
        данные = _вызвать(self.сервер, "get_issue",
                          {"key": "MIG-3", "comments": 5}).structured_content
        self.assertEqual(данные["ключ"], "MIG-3")
        self.assertTrue(данные["описание"])
        self.assertTrue(данные["комментарии"])
        self.assertIn("статус", данные["можно_перевести_в"][0])

    def test_ключ_задачи_можно_писать_как_угодно(self):
        данные = _вызвать(self.сервер, "get_issue", {"key": " mig-3 "}).structured_content
        self.assertEqual(данные["ключ"], "MIG-3")

    def test_выжимка_короче_сырого_ответа_api(self):
        """Модели отдаётся выжимка, а не ответ API: лишнее — деньги в каждом запросе."""
        сырой = self.приложение.test_client().get(
            "/v3/issues/MIG-1",
            headers={"Authorization": "OAuth mock-token", "X-Cloud-Org-ID": "mock-org"})
        выжимка = _текст(_вызвать(self.сервер, "get_issue", {"key": "MIG-1", "comments": 0}))
        self.assertLess(len(выжимка), len(сырой.get_data(as_text=True)))
        self.assertNotIn("statusStartTime", выжимка)

    def test_длинное_описание_обрезается(self):
        import tracker_server

        длинное = "ф" * (tracker_server.ОПИСАНИЕ + 500)
        каталог = tempfile.mkdtemp(prefix="трекер-длина-")
        try:
            путь = os.path.join(каталог, "tracker-data.json")
            приложение = _трекер_клиент(каталог)
            данные = _json.loads(pathlib.Path(tracker_server.КОРЕНЬ, "tracker-seed.json")
                                 .read_text(encoding="utf-8"))
            данные["задачи"][0]["description"] = длинное
            pathlib.Path(путь).write_text(_json.dumps(данные, ensure_ascii=False),
                                          encoding="utf-8")
            сервер = tracker_server.создать_сервер(_трекер(приложение), запись=False)
            итог = _вызвать(сервер, "get_issue", {"key": "MIG-1", "comments": 0})
            описание = итог.structured_content["описание"]
            self.assertLess(len(описание), len(длинное))
            self.assertIn("всего", описание)
        finally:
            shutil.rmtree(каталог, ignore_errors=True)

    # --- изменяющие инструменты ----------------------------------------------

    def test_комментарий_доходит_до_api(self):
        итог = _вызвать(self.сервер, "add_comment", {"key": "MIG-4", "text": "из инструмента"})
        self.assertFalse(итог.is_error, _текст(итог))
        задача = _вызвать(self.сервер, "get_issue",
                          {"key": "MIG-4", "comments": 5}).structured_content
        self.assertIn("из инструмента", [к["текст"] for к in задача["комментарии"]])

    def test_перевод_статуса_и_недопустимый_переход(self):
        текущий = _вызвать(self.сервер, "get_issue", {"key": "MIG-8"}).structured_content
        self.assertEqual(текущий["статус_код"], "open")
        отказ = _вызвать(self.сервер, "move_issue", {"key": "MIG-8", "status": "closed"})
        self.assertTrue(отказ.is_error)
        self.assertIn("inProgress", _текст(отказ))
        итог = _вызвать(self.сервер, "move_issue",
                        {"key": "MIG-8", "status": "inProgress", "comment": "взяли в работу"})
        self.assertFalse(итог.is_error, _текст(итог))
        self.assertEqual(итог.structured_content["статус_код"], "inProgress")

    # --- ошибки чужого API становятся понятными -------------------------------

    def test_ключ_из_модели_проверяется_до_запроса(self):
        """«../../etc/passwd» вместо ключа — та самая логическая бомба из лекции."""
        for ключ in ("НЕТ-1", "../../etc/passwd", "MIG 2", ""):
            with self.subTest(ключ):
                итог = _вызвать(self.сервер, "get_issue", {"key": ключ})
                self.assertTrue(итог.is_error)
                self.assertIn("не похоже на ключ задачи", _текст(итог))

    def test_нет_задачи_объяснено(self):
        итог = _вызвать(self.сервер, "get_issue", {"key": "MIG-999"})
        self.assertTrue(итог.is_error)
        self.assertIn("404", _текст(итог))

    def test_токен_не_принят(self):
        import tracker_server

        сервер = tracker_server.создать_сервер(
            _трекер(self.приложение, токен="wrong-token"), запись=False)
        итог = _вызвать(сервер, "list_queues")
        self.assertTrue(итог.is_error)
        self.assertIn("TRACKER_TOKEN", _текст(итог))

    def test_чужая_организация_объяснена(self):
        import tracker_server

        сервер = tracker_server.создать_сервер(
            _трекер(self.приложение, организация="other-org"), запись=False)
        итог = _вызвать(сервер, "list_issues")
        self.assertTrue(итог.is_error)
        self.assertIn("X-Org-ID", _текст(итог))

    def test_кириллица_в_токене_объяснена(self):
        import tracker_server

        сервер = tracker_server.создать_сервер(
            _трекер(self.приложение, токен="токен"), запись=False)
        итог = _вызвать(сервер, "list_queues")
        self.assertTrue(итог.is_error)
        self.assertIn("не-латинские", _текст(итог))

    def test_трекер_не_отвечает(self):
        import tracker_server

        сервер = tracker_server.создать_сервер(
            tracker_server.Трекер(адрес=f"http://127.0.0.1:{_свободный_порт()}"), запись=False)
        итог = _вызвать(сервер, "list_queues")
        self.assertTrue(итог.is_error)
        self.assertIn("tracker_api.py", _текст(итог))

    def test_лимит_запросов_объяснён(self):
        import tracker_server

        каталог = tempfile.mkdtemp(prefix="трекер-429-")
        try:
            приложение = _трекер_клиент(каталог, лимит=1)
            сервер = tracker_server.создать_сервер(_трекер(приложение), запись=False)
            _вызвать(сервер, "list_queues")
            итог = _вызвать(сервер, "list_queues")
            self.assertTrue(итог.is_error)
            self.assertIn("429", _текст(итог))
        finally:
            shutil.rmtree(каталог, ignore_errors=True)


class ВызовЧерезСоединение(unittest.TestCase):
    """Session.call_tool: настоящий tools/call по stdio и разбор ответа."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.каталог = tempfile.mkdtemp(prefix="mcp-вызов-")
        cls.память = os.path.join(cls.каталог, "memory")
        _наполнить_память(cls.память)
        cls.описание, = amcp.load(_файл_серверов(
            cls.каталог, {"agent-state": _свой_сервер(cls.память)}))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.каталог, ignore_errors=True)

    def test_вызов_возвращает_данные_и_время(self):
        with amcp.Session(self.описание) as сеанс:
            итог = сеанс.call_tool("list_tasks", {})
        self.assertTrue(итог.ок)
        self.assertEqual(итог.полное_имя, "agent-state__list_tasks")
        self.assertEqual([з["task_id"] for з in итог.данные["задачи"]], ["перенос-моделей"])
        self.assertIn("перенос-моделей", итог.текст)
        self.assertGreater(итог.блоков, 0)
        self.assertGreaterEqual(итог.секунд, 0)

    def test_несколько_вызовов_в_одном_сеансе(self):
        """Сервер запускается один раз: второй вызов идёт по тому же соединению."""
        with amcp.Session(self.описание) as сеанс:
            первый = сеанс.call_tool("list_tasks", {})
            второй = сеанс.call_tool("get_task", {"task_id": "перенос-моделей"})
        self.assertTrue(первый.ок and второй.ок)
        self.assertEqual(второй.данные["стадия"], PLANNING)

    def test_ошибка_инструмента_это_результат_а_не_исключение(self):
        with amcp.Session(self.описание) as сеанс:
            итог = сеанс.call_tool("get_task", {"task_id": "нет-такой"})
        self.assertFalse(итог.ок)
        self.assertTrue(итог.текст)

    def test_вызов_без_соединения(self):
        сеанс = amcp.Session(self.описание)
        with self.assertRaises(amcp.MCPClientError):
            сеанс.call_tool("list_tasks", {})

    def test_длинный_ответ_обрезается(self):
        """Ответ чужого сервера может быть каким угодно; в промпт идёт не всё."""
        from agent.mcp import client as mcp_client

        class _Блок:
            type = "text"
            text = "я" * (mcp_client.ПРЕДЕЛ_ОТВЕТА + 1000)

        class _Итог:
            content = [_Блок()]
            structured_content = None
            is_error = False

        сеанс = amcp.Session(self.описание)
        сеанс._клиент = types.SimpleNamespace(call_tool=lambda *а, **к: None)
        сеанс._вызвать = lambda *а, **к: _Итог()
        итог = сеанс.call_tool("что-угодно", {})
        self.assertTrue(итог.обрезан)
        self.assertLess(len(итог.текст), mcp_client.ПРЕДЕЛ_ОТВЕТА + 200)
        self.assertIn("обрезан", итог.текст)


class ИнструментарийАгента(unittest.TestCase):
    """Toolbox: общий список инструментов, имена для модели и вызов по имени."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.каталог = tempfile.mkdtemp(prefix="mcp-ящик-")
        cls.память = os.path.join(cls.каталог, "memory")
        _наполнить_память(cls.память)
        cls.файл = _файл_серверов(cls.каталог, {
            "agent-state": _свой_сервер(cls.память),
            "broken": {"command": "нет-такой-команды-xyz"},
        })

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.каталог, ignore_errors=True)

    def test_имена_для_модели_и_цена(self):
        with amcp.Toolbox(["agent-state"], self.файл) as ящик:
            имена = {и["function"]["name"] for и in ящик.для_модели()}
            сводка = ящик.сводка()
        self.assertEqual(имена, {f"agent-state__{и}" for и in ИНСТРУМЕНТЫ_СВОЕГО})
        self.assertEqual(сводка["читающих"], len(ИНСТРУМЕНТЫ_СВОЕГО))
        self.assertEqual(сводка["меняющих"], 0)
        self.assertGreater(сводка["токенов"], 0)

    def test_вызов_по_полному_имени(self):
        with amcp.Toolbox(["agent-state"], self.файл) as ящик:
            итог = ящик.вызвать("agent-state__list_tasks", {})
        self.assertTrue(итог.ок)
        self.assertIn("перенос-моделей", итог.текст)

    def test_выдуманное_имя_названо_ошибкой(self):
        with amcp.Toolbox(["agent-state"], self.файл) as ящик:
            with self.assertRaises(amcp.ToolboxError) as ошибка:
                ящик.вызвать("agent-state__drop_database", {})
        self.assertIn("Доступны", str(ошибка.exception))

    def test_закрытый_фильтром_объясняет_причину(self):
        # Файл серверов пишется в отдельный каталог: общий на класс тест бы
        # переписал, и соседние проверки увидели бы чужую конфигурацию.
        каталог = tempfile.mkdtemp(prefix="mcp-фильтр-")
        self.addCleanup(shutil.rmtree, каталог, True)
        файл = _файл_серверов(каталог, {
            "agent-state": _свой_сервер(self.память, разрешить=["list_*"])})
        with amcp.Toolbox(["agent-state"], файл) as ящик:
            with self.assertRaises(amcp.ToolboxError) as ошибка:
                ящик.вызвать("agent-state__get_task", {"task_id": "перенос-моделей"})
        self.assertIn("закрыт настройками", str(ошибка.exception))

    def test_недоступный_сервер_не_отменяет_остальные(self):
        with amcp.Toolbox(["agent-state", "broken"], self.файл) as ящик:
            сводка = ящик.сводка()
            итог = ящик.вызвать("agent-state__list_tasks", {})
        self.assertTrue(итог.ок)
        self.assertEqual(сводка["подключено"], 1)
        self.assertEqual([н["сервер"] for н in сводка["недоступны"]], ["broken"])
        self.assertIn("не найдена команда", сводка["недоступны"][0]["причина"])

    def test_меняющим_считается_всё_кроме_явного_чтения(self):
        """Пометкам сервера верят только в сторону осторожности."""
        with amcp.Toolbox(["agent-state"], self.файл) as ящик:
            self.assertFalse(ящик.меняет("agent-state__list_tasks"))
            ящик.инструменты[0].пометки = {}          # сервер ничего не заявил
            self.assertTrue(ящик.меняет(ящик.инструменты[0].полное_имя))
            self.assertTrue(ящик.меняет("выдуманный__инструмент"))


class ЗаявкиНаИзменение(unittest.TestCase):
    """memory/tool-calls.json: заявка переживает процесс и решается один раз."""

    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="заявки-")
        self.память = MemoryManager(base_dir=self.каталог, user_id="инженер")

    def tearDown(self):
        shutil.rmtree(self.каталог, ignore_errors=True)

    def заявка(self, **поля):
        поля.setdefault("инструмент", "tracker__add_comment")
        поля.setdefault("аргументы", {"key": "MIG-2", "text": "готово"})
        поля.setdefault("сервер", "tracker")
        поля.setdefault("зачем", "отметь в трекере")
        return self.память.request_call(**поля)

    def test_заявка_заводится_и_ждёт(self):
        заявка = self.заявка()
        self.assertEqual(заявка.номер, 1)
        self.assertTrue(заявка.ждёт)
        self.assertEqual([з.номер for з in self.память.pending_calls()], [1])
        self.assertIn("MIG-2", заявка.словами())

    def test_заявка_видна_другому_процессу(self):
        """Ответ пришёл в одном процессе, подтверждение придёт в другом."""
        self.заявка()
        другая = MemoryManager(base_dir=self.каталог, user_id="инженер")
        self.assertEqual([з.инструмент for з in другая.pending_calls()],
                         ["tracker__add_comment"])

    def test_решается_один_раз(self):
        заявка = self.заявка()
        self.память.resolve_call(заявка.номер, ОТКЛОНЕНА, почему="не сейчас")
        self.assertEqual(self.память.pending_calls(), [])
        with self.assertRaises(CallStoreError):
            self.память.resolve_call(заявка.номер, ВЫПОЛНЕНА)

    def test_журнал_помнит_и_заявку_и_решение(self):
        заявка = self.заявка()
        self.память.resolve_call(заявка.номер, ВЫПОЛНЕНА, результат={"ок": True})
        записи = [з for з in self.память.journal(10) if з["правило"] == "вызов-инструмента"]
        self.assertEqual([з["применено"] for з in записи], [False, True])

    def test_нет_такой_заявки(self):
        with self.assertRaises(CallStoreError):
            self.память.resolve_call(42, ВЫПОЛНЕНА)

    def test_старые_заявки_не_копятся_без_предела(self):
        from agent.memory import calls as модуль_заявок

        предел = модуль_заявок.ПРЕДЕЛ
        for номер in range(предел + 5):
            self.заявка(аргументы={"key": f"MIG-{номер}"})
        self.assertEqual(len(self.память.calls.all()), предел)

    def test_вызов_попадает_в_журнал(self):
        self.память.log_tool_call(amcp.ToolResult(
            сервер="tracker", инструмент="list_issues", полное_имя="tracker__list_issues",
            аргументы={"status": "open"}, ок=True, текст="{}"), зачем="что в работе")
        запись = self.память.journal(1)[0]
        self.assertEqual(запись["правило"], "вызов-инструмента")
        self.assertTrue(запись["применено"])
        self.assertIn("list_issues", запись["текст"])


def _просьба(имя, аргументы, ид="c1", текст=""):
    """Ответ модели, который просит вызвать инструмент, — как его шлёт провайдер."""
    сырые = аргументы if isinstance(аргументы, str) else _json.dumps(аргументы)
    return Reply(
        text=текст, model_key="тест",
        tool_calls=разобрать_вызовы([
            {"id": ид, "function": {"name": имя, "arguments": сырые}}]),
        message={"role": "assistant", "content": текст, "tool_calls": [
            {"id": ид, "type": "function",
             "function": {"name": имя, "arguments": сырые}}]})


def не_планировщик(имя: str) -> bool:
    return not имя.endswith("schedule_job")


class _ЯщикДляАгента:
    """Инструментарий-заглушка: проверяем цикл агента, а не транспорт MCP."""

    def __init__(self, ответ=None, сбой: str = ""):
        self.вызовы: list[tuple[str, dict]] = []
        self.ответ = ответ or {"задачи": ["MIG-2"]}
        self.сбой = сбой
        self.имена = ["tracker"]
        # Планировщик ведёт собственную память агента: его записи заявки не
        # требуют — как и в настоящем Toolbox по полю «своё».
        self.свои = {"scheduler"}
        self.инструменты = [
            _Инструмент("tracker", "list_issues", "только чтение"),
            _Инструмент("tracker", "add_comment", "меняет данные"),
        ]
        self.закрыт = False

    def открыть(self):
        return []

    def сводка(self):
        return {"серверов": 1, "подключено": 1, "серверы": ["tracker"], "недоступны": [],
                "инструментов": 2, "читающих": 1, "меняющих": 1, "токенов": 100,
                "повторы_имён": []}

    def для_модели(self):
        return [и.for_model() for и in self.инструменты]

    def найти(self, имя):
        for инструмент in self.инструменты:
            if инструмент.полное_имя == имя:
                return инструмент
        return None

    def меняет(self, имя, аргументы=None):
        инструмент = self.найти(имя)
        if инструмент is None:
            return True
        if инструмент.доступ != "только чтение" and инструмент.сервер not in self.свои:
            return True
        return bool(self.планируемый(имя, аргументы)) and self.меняет(
            self.планируемый(имя, аргументы))

    def конвейер_вызова(self, имя, аргументы=None):
        # Конвейеров у этой заглушки нет: она про трекер и планировщик.
        return ""

    def меняющие_шаги(self, конвейер, глубина=3):
        return []

    def планируемый(self, имя, аргументы=None):
        # Заглушка планировщика: schedule_job ставит в расписание то, что
        # названо в аргументе «tool» — как настоящий Toolbox по полю
        # «планирует» из mcp-servers.json.
        if не_планировщик(имя) or not аргументы:
            return ""
        значение = аргументы.get("tool", "")
        return значение.strip() if isinstance(значение, str) else ""

    def вызвать(self, имя, аргументы=None):
        self.вызовы.append((имя, dict(аргументы or {})))
        if self.сбой:
            raise amcp.ToolboxError(self.сбой)
        return amcp.ToolResult(сервер="tracker", инструмент=имя.split("__")[-1],
                               полное_имя=имя, аргументы=dict(аргументы or {}),
                               ок=True, текст=_json.dumps(self.ответ, ensure_ascii=False),
                               данные=self.ответ, секунд=0.01, блоков=1)

    def close(self):
        self.закрыт = True


def _Инструмент(сервер: str, имя: str, доступ: str):
    пометки = {"readOnlyHint": True} if доступ == "только чтение" else {"destructiveHint": False}
    return amcp.Tool(сервер=сервер, имя=имя, описание=f"инструмент {имя}",
                     входная_схема={"type": "object", "properties": {}}, пометки=пометки)


class ЦиклИнструментов(unittest.TestCase):
    """Просьба модели → вызов агентом → результат → ответ. Модель — заглушка."""

    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="цикл-")
        self.агент = MemoryAgent(base_dir=self.каталог, judge_semantic=False,
                                 require_self_report=False, router_mode="выкл",
                                 seed_project=False)
        self.ящик = _ЯщикДляАгента()
        self.агент.toolbox = self.ящик
        self.запросы: list[dict] = []

    def tearDown(self):
        self.агент.close()
        shutil.rmtree(self.каталог, ignore_errors=True)

    def модель(self, *ответы):
        """Подменяет вызовы модели заранее заданными ответами."""
        очередь = list(ответы)

        def вызов(ключ, сообщения, **кв):
            self.запросы.append({"сообщения": сообщения, "tools": кв.get("tools")})
            return очередь.pop(0) if очередь else Reply(text="всё", model_key="тест")

        return mock.patch.object(self.агент.client, "call", side_effect=вызов)

    # Заготовка общая с проверками планировщика: просьба модели о вызове
    # выглядит одинаково, из какого бы дня она ни пришла.
    просьба = staticmethod(_просьба)

    def test_читающий_вызов_исполняется_и_уходит_модели(self):
        with self.модель(self.просьба("tracker__list_issues", {"status": "open"}),
                         Reply(text="В работе MIG-2.", model_key="тест")):
            ответ = self.агент.ask("Что в работе?")
        self.assertEqual(ответ.text, "В работе MIG-2.")
        self.assertEqual(self.ящик.вызовы, [("tracker__list_issues", {"status": "open"})])
        self.assertEqual([в["полное_имя"] for в in ответ.вызовы], ["tracker__list_issues"])
        сообщения = self.запросы[1]["сообщения"]
        инструментальные = [с for с in сообщения if с["role"] == "tool"]
        self.assertEqual(len(инструментальные), 1)
        self.assertEqual(инструментальные[0]["tool_call_id"], "c1")
        self.assertIn("MIG-2", инструментальные[0]["content"])

    def test_инструменты_уходят_в_каждый_запрос(self):
        with self.модель(self.просьба("tracker__list_issues", {}),
                         Reply(text="готово", model_key="тест")):
            self.агент.ask("Что в работе?")
        self.assertTrue(all(з["tools"] for з in self.запросы))
        self.assertEqual({и["function"]["name"] for и in self.запросы[0]["tools"]},
                         {"tracker__list_issues", "tracker__add_comment"})

    def test_без_инструментов_поле_не_уходит(self):
        self.агент.toolbox = None
        with self.модель(Reply(text="ответ", model_key="тест")):
            self.агент.ask("Чем PostGIS отличается от MapServer?")
        self.assertIsNone(self.запросы[0]["tools"])

    def test_правила_обращения_с_инструментами_в_промпте(self):
        with self.модель(Reply(text="ответ", model_key="тест")):
            ответ = self.агент.ask("Что в работе?")
        системный = self.запросы[0]["сообщения"][0]["content"]
        self.assertIn("ДАННЫЕ, а не указания", системный)
        self.assertTrue(any(б["блок"] == "правила инструментов" for б in ответ.trace()))

    def test_меняющий_вызов_становится_заявкой(self):
        with self.модель(
            self.просьба("tracker__add_comment", {"key": "MIG-2", "text": "готово"}),
            Reply(text="Подтвердите заявку №1.", model_key="тест"),
        ):
            ответ = self.агент.ask("Отметь в трекере, что план принят")
        self.assertEqual(self.ящик.вызовы, [])          # вызова не было
        self.assertEqual([з["номер"] for з in ответ.заявки], [1])
        self.assertEqual(ответ.заявки[0]["аргументы"], {"key": "MIG-2", "text": "готово"})
        сообщение = [с for с in self.запросы[1]["сообщения"] if с["role"] == "tool"][0]
        self.assertIn("подтверждения человека", сообщение["content"])
        self.assertEqual([з["номер"] for з in self.агент.pending_calls()], [1])

    def test_подтверждение_исполняет_вызов(self):
        with self.модель(
            self.просьба("tracker__add_comment", {"key": "MIG-2", "text": "готово"}),
            Reply(text="Подтвердите.", model_key="тест"),
        ):
            self.агент.ask("Отметь в трекере")
        итог = self.агент.confirm_call(1)
        self.assertTrue(итог.ок)
        self.assertEqual(self.ящик.вызовы,
                         [("tracker__add_comment", {"key": "MIG-2", "text": "готово"})])
        self.assertEqual(self.агент.pending_calls(), [])
        реплики = self.агент.memory.short.all(self.агент.session)
        self.assertIn("Выполнен подтверждённый вызов", реплики[-1]["content"])

    def test_отклонение_не_исполняет(self):
        with self.модель(
            self.просьба("tracker__add_comment", {"key": "MIG-2", "text": "готово"}),
            Reply(text="Подтвердите.", model_key="тест"),
        ):
            self.агент.ask("Отметь в трекере")
        решена = self.агент.reject_call(1, "статус меняет тимлид")
        self.assertEqual(решена["состояние"], ОТКЛОНЕНА)
        self.assertEqual(self.ящик.вызовы, [])
        with self.assertRaises(AgentError):
            self.агент.confirm_call(1)

    def test_без_подтверждения_вызов_идёт_сразу(self):
        self.агент.confirm_writes = False
        with self.модель(
            self.просьба("tracker__add_comment", {"key": "MIG-2", "text": "готово"}),
            Reply(text="Добавил.", model_key="тест"),
        ):
            ответ = self.агент.ask("Отметь в трекере")
        self.assertEqual(len(self.ящик.вызовы), 1)
        self.assertEqual(ответ.заявки, [])

    def test_кривые_аргументы_объяснены_модели(self):
        with self.модель(self.просьба("tracker__list_issues", "{сломано"),
                         Reply(text="Извини, перепишу.", model_key="тест")):
            ответ = self.агент.ask("Что в работе?")
        self.assertEqual(self.ящик.вызовы, [])
        сообщение = [с for с in self.запросы[1]["сообщения"] if с["role"] == "tool"][0]
        self.assertIn("не JSON", сообщение["content"])
        self.assertEqual(ответ.вызовы, [])

    def test_сбой_вызова_не_роняет_ответ(self):
        self.агент.toolbox = _ЯщикДляАгента(сбой="Сервер «tracker» не выполнил вызов")
        with self.модель(self.просьба("tracker__list_issues", {}),
                         Reply(text="Трекер недоступен.", model_key="тест")):
            ответ = self.агент.ask("Что в работе?")
        self.assertEqual(ответ.text, "Трекер недоступен.")
        self.assertEqual([в["ок"] for в in ответ.вызовы], [False])
        сообщение = [с for с in self.запросы[1]["сообщения"] if с["role"] == "tool"][0]
        self.assertIn("Вызов не выполнен", сообщение["content"])

    def test_предел_кругов_вызовов(self):
        """Модель, которая только и делает, что зовёт инструменты, не разорит."""
        просьбы = [self.просьба("tracker__list_issues", {}, ид=f"c{н}") for н in range(10)]
        with self.модель(*просьбы):
            ответ = self.агент.ask("Что в работе?")
        self.assertEqual(len(self.ящик.вызовы), self.агент.tool_rounds)
        self.assertIsNone(self.запросы[-1]["tools"])     # последний запрос — без инструментов
        # Модель и тут просит вызов, а не отвечает. Пустой текст наружу не выходит.
        self.assertIn("запрашивала инструменты", ответ.text)

    def test_вызовы_попадают_в_журнал_памяти(self):
        with self.модель(self.просьба("tracker__list_issues", {"status": "open"}),
                         Reply(text="готово", model_key="тест")):
            self.агент.ask("Что в работе?")
        записи = [з for з in self.агент.memory.journal(20) if з["правило"] == "вызов-инструмента"]
        self.assertTrue(записи)
        self.assertIn("list_issues", записи[-1]["текст"])

    def test_ручной_вызов_не_спрашивает_подтверждения(self):
        итог = self.агент.call_tool("tracker__add_comment", {"key": "MIG-2", "text": "вручную"})
        self.assertTrue(итог.ок)
        self.assertEqual(len(self.ящик.вызовы), 1)
        self.assertEqual(self.агент.pending_calls(), [])

    def test_закрытие_агента_закрывает_соединения(self):
        self.агент.close()
        self.assertTrue(self.ящик.закрыт)


class КонсольИнструментов(unittest.TestCase):
    """cli.py --инструменты/--вызвать/--заявки: настоящий процесс, stdin закрыт."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.каталог = tempfile.mkdtemp(prefix="инструменты-консоль-")
        cls.память = os.path.join(cls.каталог, "memory")
        _наполнить_память(cls.память)
        cls.файл = _файл_серверов(cls.каталог, {
            "agent-state": _свой_сервер(cls.память),
            "broken": {"command": "нет-такой-команды-xyz"},
        })

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.каталог, ignore_errors=True)

    def консоль(self, *ключи: str):
        итог = _subprocess.run(
            [sys.executable, os.path.join(КОРЕНЬ_ДНЯ, "cli.py"),
             "--mcp-файл", self.файл, "--память-в", self.память, *ключи],
            capture_output=True, text=True, stdin=_subprocess.DEVNULL, timeout=120,
            env={**os.environ, "MEMORY_DIR": self.память})
        вывод = итог.stdout + итог.stderr
        self.assertNotIn("Вы:", вывод, "команда открыла диалог")
        return итог.returncode, вывод

    def test_ключ_не_съедает_вопрос(self):
        """«--инструменты tracker "вопрос"» — вопрос должен остаться вопросом."""
        import cli

        разбор = cli.build_parser()
        аргументы = разбор.parse_args(["--инструменты", "agent-state", "что в работе?"])
        self.assertEqual(аргументы.tools, "agent-state")
        self.assertEqual(аргументы.вопрос, ["что в работе?"])
        self.assertIsNone(разбор.parse_args([]).tools)
        self.assertEqual(разбор.parse_args(["--инструменты"]).tools, "")
        self.assertEqual(cli.разобрать_серверы("a, b"), ["a", "b"])
        self.assertIsNone(cli.разобрать_серверы(None))

    def test_список_инструментов_без_модели(self):
        код, вывод = self.консоль("--инструменты", "agent-state")
        self.assertEqual(код, 0, вывод)
        self.assertIn("agent-state__list_tasks", вывод)
        self.assertIn("только чтение", вывод)
        self.assertIn("в каждом запросе", вывод)

    def test_недоступный_сервер_назван(self):
        код, вывод = self.консоль("--инструменты", "agent-state,broken")
        self.assertEqual(код, 0, вывод)
        self.assertIn("broken", вывод)
        self.assertIn("не найдена команда", вывод)

    def test_ручной_вызов(self):
        код, вывод = self.консоль("--вызвать", "agent-state__list_tasks")
        self.assertEqual(код, 0, вывод)
        self.assertIn("перенос-моделей", вывод)

    def test_ручной_вызов_с_аргументами(self):
        код, вывод = self.консоль("--вызвать", "agent-state__get_task",
                                  "--аргументы", '{"task_id": "перенос-моделей"}')
        self.assertEqual(код, 0, вывод)
        self.assertIn("planning", вывод)

    def test_кривые_аргументы_не_уходят_на_сервер(self):
        код, вывод = self.консоль("--вызвать", "agent-state__get_task", "--аргументы", "{нет")
        self.assertEqual(код, 1)
        self.assertIn("не JSON", вывод)

    def test_ошибка_инструмента_даёт_код_возврата(self):
        код, вывод = self.консоль("--вызвать", "agent-state__get_task",
                                  "--аргументы", '{"task_id": "нет-такой"}')
        self.assertEqual(код, 1, вывод)
        self.assertIn("ОШИБКА", вывод)

    def test_заявок_нет(self):
        код, вывод = self.консоль("--заявки")
        self.assertEqual(код, 0, вывод)
        self.assertIn("Ждущих заявок нет", вывод)

    def test_заявка_подтверждается_из_другого_процесса(self):
        """Заявку завёл один процесс, подтверждает другой — как паузу в Дне 13."""
        каталог = tempfile.mkdtemp(prefix="заявка-процесс-")
        try:
            память = MemoryManager(base_dir=каталог, user_id="инженер")
            память.request_call(инструмент="agent-state__list_tasks", аргументы={},
                                сервер="agent-state", зачем="проверка")
            итог = _subprocess.run(
                [sys.executable, os.path.join(КОРЕНЬ_ДНЯ, "cli.py"), "--mcp-файл", self.файл,
                 "--память-в", каталог, "--подтвердить", "1"],
                capture_output=True, text=True, stdin=_subprocess.DEVNULL, timeout=120,
                env={**os.environ, "MEMORY_DIR": каталог})
            вывод = итог.stdout + итог.stderr
            self.assertEqual(итог.returncode, 0, вывод)
            self.assertIn("исполнена", вывод)
            self.assertEqual(MemoryManager(base_dir=каталог).pending_calls(), [])
        finally:
            shutil.rmtree(каталог, ignore_errors=True)


class ВебИнструментов(unittest.TestCase):
    """Страница умеет то же, что консоль: включить, вызвать, решить заявку."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.каталог = tempfile.mkdtemp(prefix="инструменты-веб-")
        cls.память = os.path.join(cls.каталог, "memory")
        _наполнить_память(cls.память)
        cls.файл = _файл_серверов(cls.каталог, {"agent-state": _свой_сервер(cls.память)})
        cls.прежние = {к: os.environ.get(к) for к in ("MEMORY_DIR", "MCP_CONFIG")}
        os.environ["MEMORY_DIR"] = cls.память
        os.environ["MCP_CONFIG"] = cls.файл
        import importlib
        import web
        cls.web = importlib.reload(web)
        cls.клиент = cls.web.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.web.agent.close()
        for ключ, значение in cls.прежние.items():
            if значение is None:
                os.environ.pop(ключ, None)
            else:
                os.environ[ключ] = значение
        shutil.rmtree(cls.каталог, ignore_errors=True)

    def tearDown(self):
        self.клиент.post("/api/tools", json={"tools": []})

    def test_включение_и_выключение(self):
        данные = self.клиент.post("/api/tools", json={"tools": ["agent-state"]}).get_json()
        self.assertTrue(данные["enabled"])
        self.assertEqual({и["имя"] for и in данные["tools"]}, ИНСТРУМЕНТЫ_СВОЕГО)
        self.assertGreater(данные["summary"]["токенов"], 0)
        self.assertTrue(данные["confirm_writes"])
        выключено = self.клиент.post("/api/tools", json={"tools": []}).get_json()
        self.assertFalse(выключено["enabled"])
        self.assertIsNone(self.web.agent.toolbox)

    def test_ручной_вызов(self):
        ответ = self.клиент.post("/api/mcp/call",
                                 json={"tool": "agent-state__list_tasks", "args": {}})
        self.assertEqual(ответ.status_code, 200)
        результат = ответ.get_json()["result"]
        self.assertTrue(результат["ок"])
        self.assertIn("перенос-моделей", результат["текст"])

    def test_выдуманный_инструмент_это_ошибка_запроса(self):
        ответ = self.клиент.post("/api/mcp/call",
                                 json={"tool": "agent-state__drop", "args": {}})
        self.assertEqual(ответ.status_code, 400)
        self.assertIn("нет", ответ.get_json()["error"])

    def test_аргументы_должны_быть_объектом(self):
        ответ = self.клиент.post("/api/mcp/call",
                                 json={"tool": "agent-state__list_tasks", "args": "строка"})
        self.assertEqual(ответ.status_code, 400)

    def test_заявка_подтверждается(self):
        self.web.agent.memory.request_call(
            инструмент="agent-state__list_tasks", аргументы={}, сервер="agent-state",
            зачем="проверка")
        список = self.клиент.get("/api/calls").get_json()["calls"]
        номер = список[-1]["номер"]
        ответ = self.клиент.post("/api/calls", json={"номер": номер, "действие": "подтвердить"})
        self.assertEqual(ответ.status_code, 200)
        данные = ответ.get_json()
        self.assertTrue(данные["result"]["ок"])
        self.assertNotIn(номер, [з["номер"] for з in данные["calls"]])

    def test_заявка_отклоняется(self):
        заявка = self.web.agent.memory.request_call(
            инструмент="agent-state__list_tasks", аргументы={}, сервер="agent-state",
            зачем="проверка")
        ответ = self.клиент.post("/api/calls", json={
            "номер": заявка.номер, "действие": "отклонить", "почему": "не нужно"})
        self.assertEqual(ответ.status_code, 200)
        self.assertEqual(ответ.get_json()["rejected"]["состояние"], ОТКЛОНЕНА)

    def test_неизвестное_действие(self):
        ответ = self.клиент.post("/api/calls", json={"номер": 1, "действие": "стереть"})
        self.assertEqual(ответ.status_code, 400)

# --- День 18: планировщик и фоновые задачи -----------------------------------


def _планировщик_сервер(база: str = "", **поля) -> dict:
    """Описание сервера планировщика для временного mcp-servers.json.

    Пустая база — как в настоящем mcp-servers.json: путь сервер берёт из
    окружения. На этом и проверяется, что «--память-в» до него доходит.
    """
    окружение = ({"SCHEDULER_DB": база} if база else
                 {"MEMORY_DIR": "${MEMORY_DIR:-memory}", "SCHEDULER_DB": "${SCHEDULER_DB:-}"})
    описание = {"command": "${PYTHON}",
                "args": [os.path.join(КОРЕНЬ_ДНЯ, "scheduler_server.py")],
                "env": окружение, "таймаут": 60,
                "планирует": {"schedule_job": "tool"}, "своё": True}
    описание.update(поля)
    return описание


class РазборРасписания(unittest.TestCase):
    """Расписание словами: «через 15м», «каждые 30м», «ежедневно в 09:00»."""

    СЕЙЧАС = _datetime(2026, 9, 23, 10, 0, 0)

    def test_отложенное_один_раз(self):
        правило = расписание.разобрать("через 15м")
        self.assertEqual(правило.как, расписание.ОДНОКРАТНО)
        self.assertEqual(правило.секунд, 900)
        self.assertFalse(правило.повторяется)
        self.assertEqual(правило.первый(self.СЕЙЧАС), _datetime(2026, 9, 23, 10, 15))
        self.assertIsNone(правило.следующий(self.СЕЙЧАС))

    def test_периодическое(self):
        правило = расписание.разобрать("каждые 30м")
        self.assertEqual(правило.как, расписание.ИНТЕРВАЛ)
        self.assertTrue(правило.повторяется)
        self.assertEqual(правило.следующий(self.СЕЙЧАС), _datetime(2026, 9, 23, 10, 30))

    def test_ежедневное_переносится_на_завтра(self):
        правило = расписание.разобрать("ежедневно в 09:00")
        self.assertEqual(правило.как, расписание.ЕЖЕДНЕВНО)
        # 09:00 сегодня уже прошло — значит, завтра.
        self.assertEqual(правило.первый(self.СЕЙЧАС), _datetime(2026, 9, 24, 9, 0))
        self.assertEqual(правило.первый(_datetime(2026, 9, 23, 8, 0)),
                         _datetime(2026, 9, 23, 9, 0))

    def test_время_суток_один_раз(self):
        правило = расписание.разобрать("в 18:30")
        self.assertEqual(правило.как, расписание.ОДНОКРАТНО)
        self.assertEqual(правило.первый(self.СЕЙЧАС), _datetime(2026, 9, 23, 18, 30))
        self.assertIsNone(правило.следующий(self.СЕЙЧАС))

    def test_единицы_времени(self):
        for текст, секунд in (("через 30с", 30), ("через 2ч", 7200), ("через 1д", 86400),
                              ("через 45 минут", 2700), ("каждые 2 часа", 7200),
                              ("каждый час", 3600), ("каждые сутки", 86400)):
            with self.subTest(текст):
                self.assertEqual(расписание.разобрать(текст).секунд, секунд)

    def test_число_без_единицы_считается_минутами(self):
        self.assertEqual(расписание.разобрать("через 5").секунд, 300)

    def test_регистр_ё_и_лишние_пробелы(self):
        правило = расписание.разобрать("  КАЖДЫЕ   30   М  ")
        self.assertEqual(правило.секунд, 1800)
        self.assertEqual(расписание.разобрать("ЕЖЕДНЕВНО В 09:00").час, 9)

    def test_словами_читается_по_русски(self):
        self.assertEqual(расписание.разобрать("каждый час").словами(), "каждый час")
        self.assertEqual(расписание.разобрать("каждые 30м").словами(), "каждые 30 минут")
        self.assertEqual(расписание.разобрать("через 15м").словами(), "один раз через 15 минут")
        self.assertEqual(расписание.разобрать("ежедневно в 9:05").словами(),
                         "ежедневно в 09:05")

    def test_непонятное_расписание_объясняет_как_надо(self):
        with self.assertRaises(расписание.ОшибкаРасписания) as сбой:
            расписание.разобрать("когда-нибудь")
        self.assertIn("каждые 30м", str(сбой.exception))

    def test_непонятная_единица(self):
        with self.assertRaises(расписание.ОшибкаРасписания) as сбой:
            расписание.разобрать("каждые 30 попугаев")
        self.assertIn("попугаев", str(сбой.exception))

    def test_невозможное_время(self):
        with self.assertRaises(расписание.ОшибкаРасписания):
            расписание.разобрать("ежедневно в 25:00")
        with self.assertRaises(расписание.ОшибкаРасписания):
            расписание.разобрать("в 10:75")

    def test_пустое_расписание(self):
        with self.assertRaises(расписание.ОшибкаРасписания):
            расписание.разобрать("   ")

    def test_нулевой_и_слишком_большой_промежуток(self):
        with self.assertRaises(расписание.ОшибкаРасписания):
            расписание.разобрать("каждые 0м")
        with self.assertRaises(расписание.ОшибкаРасписания):
            расписание.разобрать("каждые 400д")

    def test_пропущенные_сроки_считаются(self):
        правило = расписание.разобрать("каждые 30м")
        срок = _datetime(2026, 9, 23, 1, 0)
        # Работника не было три часа: шесть получасовых сроков прошли мимо.
        self.assertEqual(правило.пропущено(срок, _datetime(2026, 9, 23, 4, 0)), 6)
        self.assertEqual(правило.пропущено(срок, _datetime(2026, 9, 23, 1, 10)), 0)
        self.assertEqual(расписание.разобрать("через 5м").пропущено(
            срок, _datetime(2026, 9, 23, 4, 0)), 0)

    def test_ежедневное_пропущено_считается_сутками(self):
        правило = расписание.разобрать("ежедневно в 09:00")
        self.assertEqual(правило.пропущено(_datetime(2026, 9, 20, 9, 0),
                                           _datetime(2026, 9, 23, 10, 0)), 3)

    def test_из_словаря_и_обратно(self):
        правило = расписание.разобрать("каждые 30м")
        снова = расписание.из_словаря(правило.to_dict())
        self.assertEqual(снова, правило)
        with self.assertRaises(расписание.ОшибкаРасписания):
            расписание.из_словаря({"как": "иногда"})


class ХранилищеПланировщика(unittest.TestCase):
    """Задания, запуски, сводки и напоминания в SQLite."""

    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="планировщик-")
        self.база = ScheduleStore(os.path.join(self.каталог, "memory", "scheduler.db"))

    def tearDown(self):
        self.база.close()
        shutil.rmtree(self.каталог, ignore_errors=True)

    def задание(self, расписание_="каждые 30м", **поля):
        поля.setdefault("инструмент", "tracker__list_issues")
        поля.setdefault("аргументы", {"queue": "MIG"})
        return self.база.добавить_задание(расписание=расписание_, **поля)

    def test_каталог_создаётся_сам(self):
        self.assertTrue(os.path.isfile(self.база.path))

    def test_задание_сохраняется_целиком(self):
        задание = self.задание(зачем="следить за миграцией")
        снова = self.база.задание(задание.номер)
        self.assertEqual(снова.инструмент, "tracker__list_issues")
        self.assertEqual(снова.аргументы, {"queue": "MIG"})
        self.assertEqual(снова.сервер, "tracker")     # выведен из имени инструмента
        self.assertEqual(снова.зачем, "следить за миграцией")
        self.assertTrue(снова.активно)
        self.assertTrue(снова.повторяется)

    def test_срок_первого_запуска_считается_сразу(self):
        сейчас = _datetime(2026, 9, 23, 10, 0)
        задание = self.база.добавить_задание("tracker__list_queues", "каждые 30м",
                                             сейчас=сейчас)
        self.assertEqual(задание.срок, "2026-09-23 10:30:00")

    def test_кривое_расписание_не_сохраняется(self):
        with self.assertRaises(расписание.ОшибкаРасписания):
            self.задание("когда получится")
        self.assertEqual(self.база.задания(), [])

    def test_пустой_инструмент_отклоняется(self):
        with self.assertRaises(ScheduleStoreError):
            self.база.добавить_задание("", "каждые 30м")

    def test_к_сроку_отдаёт_только_созревшие(self):
        сейчас = _datetime(2026, 9, 23, 10, 0)
        рано = self.база.добавить_задание("a__b", "каждые 30м", сейчас=сейчас)
        self.assertEqual(self.база.к_сроку(_datetime(2026, 9, 23, 10, 10)), [])
        созрело = self.база.к_сроку(_datetime(2026, 9, 23, 10, 31))
        self.assertEqual([з.номер for з in созрело], [рано.номер])

    def test_отменённое_не_срабатывает(self):
        задание = self.задание()
        self.база.отменить(задание.номер, почему="передумали")
        self.assertEqual(self.база.к_сроку(_datetime(2100, 1, 1)), [])
        self.assertEqual(self.база.задание(задание.номер).состояние, ОТМЕНЕНО)

    def test_отменить_дважды_нельзя(self):
        задание = self.задание()
        self.база.отменить(задание.номер)
        with self.assertRaises(ScheduleStoreError):
            self.база.отменить(задание.номер)
        with self.assertRaises(ScheduleStoreError):
            self.база.отменить(9999)

    def test_перенос_без_срока_завершает_задание(self):
        задание = self.задание("через 5м")
        self.база.перенести(задание.номер, None)
        снова = self.база.задание(задание.номер)
        self.assertEqual(снова.состояние, ИСПОЛНЕНО)
        self.assertEqual(снова.срок, "")

    def test_запуск_обновляет_счётчики(self):
        задание = self.задание()
        self.база.записать_запуск(задание.номер, ок=True, текст="8 задач",
                                  данные={"найдено": 8}, секунд=0.4)
        self.база.записать_запуск(задание.номер, ок=False, текст="трекер не ответил")
        снова = self.база.задание(задание.номер)
        self.assertEqual((снова.запусков, снова.сбоев), (2, 1))
        self.assertIn("трекер", снова.итог)
        запуски = self.база.запуски(задание=задание.номер)
        self.assertEqual([з.ок for з in запуски], [False, True])
        self.assertEqual(запуски[1].данные, {"найдено": 8})

    def test_пропуск_не_идёт_в_счётчик_запусков(self):
        задание = self.задание()
        self.база.записать_запуск(задание.номер, ок=True, текст="срок пропущен",
                                  пропуск=True, пропущено=4)
        снова = self.база.задание(задание.номер)
        self.assertEqual((снова.запусков, снова.сбоев), (0, 0))
        запуск, = self.база.запуски(задание=задание.номер)
        self.assertTrue(запуск.пропуск)
        self.assertEqual(запуск.пропущено, 4)
        self.assertEqual(self.база.счётчики()["пропусков"], 1)

    def test_длинный_результат_обрезается(self):
        задание = self.задание()
        self.база.записать_запуск(задание.номер, ок=True, текст="я" * 9000)
        запуск, = self.база.запуски(задание=задание.номер)
        self.assertLess(len(запуск.текст), 9000)
        self.assertIn("всего 9000", запуск.текст)

    def test_запуски_фильтруются_по_времени(self):
        задание = self.задание()
        self.база.записать_запуск(задание.номер, ок=True, сейчас=_datetime(2026, 9, 20, 10, 0))
        self.база.записать_запуск(задание.номер, ок=True, сейчас=_datetime(2026, 9, 23, 10, 0))
        поздние = self.база.запуски(с=_datetime(2026, 9, 22, 0, 0))
        self.assertEqual(len(поздние), 1)

    def test_сводка_сохраняется_и_озвучивается(self):
        сводка = self.база.добавить_сводку({"запусков": 3})
        self.assertEqual(self.база.последняя_сводка().номер, сводка.номер)
        self.assertEqual(сводка.текст, "")
        озвучена = self.база.озвучить(сводка.номер, "За сутки всё спокойно.", модель="ds-flash")
        self.assertEqual(озвучена.текст, "За сутки всё спокойно.")
        self.assertEqual(озвучена.модель, "ds-flash")
        with self.assertRaises(ScheduleStoreError):
            self.база.озвучить(999, "нет такой")

    def test_непрочитанная_сводка_отдаётся_один_раз(self):
        сводка = self.база.добавить_сводку({"запусков": 1})
        self.assertEqual(self.база.непрочитанная_сводка().номер, сводка.номер)
        self.база.прочитать_сводку(сводка.номер)
        self.assertIsNone(self.база.непрочитанная_сводка())
        # Прочитанная остаётся последней: её можно посмотреть руками.
        self.assertEqual(self.база.последняя_сводка().номер, сводка.номер)

    def test_напоминания(self):
        напоминание = self.база.напомнить("проверить бэкап PostGIS", задание=3)
        self.assertEqual(напоминание.задание, 3)
        self.assertFalse(напоминание.прочитано)
        self.assertEqual(len(self.база.напоминания(только_новые=True)), 1)
        self.assertEqual(self.база.прочитать_напоминания(), 1)
        self.assertEqual(self.база.напоминания(только_новые=True), [])
        with self.assertRaises(ScheduleStoreError):
            self.база.напомнить("   ")

    def test_пульс_работника(self):
        сейчас = _datetime(2026, 9, 23, 10, 0)
        self.assertFalse(self.база.работник(сейчас)["запущен"])
        self.база.пульс(тик=5.0, pid=4242, сейчас=сейчас)
        живой = self.база.работник(сейчас)
        self.assertTrue(живой["запущен"])
        self.assertEqual((живой["тик"], живой["pid"]), (5.0, 4242))
        # Полминуты без отметки — работник считается остановленным.
        self.assertFalse(self.база.работник(сейчас + _timedelta(minutes=5))["запущен"])

    def test_счётчики_для_панели(self):
        задание = self.задание()
        self.база.записать_запуск(задание.номер, ок=True)
        self.база.напомнить("позвонить")
        цифры = self.база.счётчики()
        self.assertEqual((цифры["заданий"], цифры["активных"], цифры["периодических"]), (1, 1, 1))
        self.assertEqual((цифры["запусков"], цифры["новых_напоминаний"]), (1, 1))

    def test_другое_соединение_видит_те_же_задания(self):
        задание = self.задание()
        соседнее = ScheduleStore(self.база.path)
        try:
            self.assertEqual(соседнее.задание(задание.номер).инструмент, задание.инструмент)
        finally:
            соседнее.close()

    def test_окно_сводки(self):
        сейчас = _datetime(2026, 9, 23, 10, 0)
        начало, конец = окно("сутки", сейчас)
        self.assertEqual(конец, сейчас)
        self.assertEqual(начало, _datetime(2026, 9, 22, 10, 0))
        self.assertEqual(окно("30м", сейчас)[0], _datetime(2026, 9, 23, 9, 30))
        self.assertEqual(окно("всё", сейчас)[0], _datetime(1970, 1, 1))
        with self.assertRaises(ScheduleStoreError):
            окно("когда-нибудь", сейчас)


class СводкаПланировщика(unittest.TestCase):
    """Агрегированный результат: счётчики и различия между сборами данных."""

    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="сводка-")
        self.база = ScheduleStore(os.path.join(self.каталог, "scheduler.db"))

    def tearDown(self):
        self.база.close()
        shutil.rmtree(self.каталог, ignore_errors=True)

    def test_числа_и_строки(self):
        строки = scheduler_digest.различия({"найдено": 8, "статус": "в работе"},
                                           {"найдено": 9, "статус": "готово"})
        self.assertIn("найдено: 8 → 9", строки)
        self.assertIn("статус: в работе → готово", строки)

    def test_списки_сравниваются_по_ключу(self):
        было = {"задачи": [{"ключ": "MIG-1", "статус": "в работе"},
                           {"ключ": "MIG-4", "статус": "открыта"}]}
        стало = {"задачи": [{"ключ": "MIG-1", "статус": "готово"},
                            {"ключ": "MIG-9", "статус": "открыта"}]}
        строки = scheduler_digest.различия(было, стало)
        self.assertTrue(any("MIG-1" in с and "готово" in с for с in строки), строки)
        self.assertTrue(any("новое: MIG-9" in с for с in строки), строки)
        self.assertTrue(any("пропало: MIG-4" in с for с in строки), строки)

    def test_появление_и_пропажа_полей(self):
        строки = scheduler_digest.различия({"а": 1}, {"а": 1, "б": 2})
        self.assertTrue(any("появилось «б»" in с for с in строки), строки)
        строки = scheduler_digest.различия({"а": 1, "б": 2}, {"а": 1})
        self.assertTrue(any("пропало «б»" in с for с in строки), строки)

    def test_шумные_поля_не_считаются_изменением(self):
        было = {"ключ": "MIG-1", "обновлена": "2026-09-23T10:00:00"}
        стало = {"ключ": "MIG-1", "обновлена": "2026-09-23T11:00:00"}
        self.assertEqual(scheduler_digest.различия(было, стало), [])

    def test_одинаковые_снимки_не_дают_строк(self):
        снимок = {"найдено": 8, "задачи": [{"ключ": "MIG-1"}]}
        self.assertEqual(scheduler_digest.различия(снимок, снимок), [])

    def test_списки_без_ключей(self):
        строки = scheduler_digest.различия({"метки": ["карта"]}, {"метки": ["карта", "право"]})
        self.assertTrue(any("было 1, стало 2" in с for с in строки), строки)

    def test_сводка_считает_запуски_и_изменения(self):
        задание = self.база.добавить_задание("tracker__list_issues", "каждые 30м")
        self.база.записать_запуск(задание.номер, ок=True, текст="8",
                                  данные={"найдено": 8, "задачи": [{"ключ": "MIG-1"}]})
        self.база.записать_запуск(задание.номер, ок=True, текст="9",
                                  данные={"найдено": 9,
                                          "задачи": [{"ключ": "MIG-1"}, {"ключ": "MIG-9"}]})
        цифры = scheduler_digest.собрать(self.база, за="час")
        self.assertEqual(цифры["запусков"], 2)
        self.assertEqual(цифры["сбоев"], 0)
        изменение, = цифры["изменения"]
        self.assertEqual(изменение["задание"], задание.номер)
        self.assertTrue(any("найдено: 8 → 9" in с for с in изменение["строки"]))

    def test_сравниваются_края_окна_а_не_последние_опросы(self):
        """Статус сменился утром, сводка вечерняя: изменение должно быть видно."""
        задание = self.база.добавить_задание("tracker__list_issues", "каждые 30м")
        сейчас = _datetime(2026, 9, 23, 20, 0)
        for час, найдено in ((8, 8), (9, 9), (19, 9), (19, 9)):
            self.база.записать_запуск(задание.номер, ок=True, данные={"найдено": найдено},
                                      сейчас=_datetime(2026, 9, 23, час, 0))
        цифры = scheduler_digest.собрать(self.база, за="сутки", сейчас=сейчас)
        изменение, = цифры["изменения"]
        self.assertTrue(any("найдено: 8 → 9" in с for с in изменение["строки"]),
                        изменение["строки"])
        # За последний час в окно попадает только вечерний сбор — сравнивать
        # его не с чем в окне, и прежним берётся последний до окна.
        час_назад = scheduler_digest.собрать(self.база, за="час", сейчас=сейчас)
        self.assertEqual(час_назад["изменения"], [])

    def test_единственный_сбор_в_окне_сравнивается_с_предыдущим(self):
        задание = self.база.добавить_задание("tracker__list_issues", "каждые 30м")
        self.база.записать_запуск(задание.номер, ок=True, данные={"найдено": 8},
                                  сейчас=_datetime(2026, 9, 23, 8, 0))
        self.база.записать_запуск(задание.номер, ок=True, данные={"найдено": 9},
                                  сейчас=_datetime(2026, 9, 23, 19, 50))
        цифры = scheduler_digest.собрать(self.база, за="час",
                                         сейчас=_datetime(2026, 9, 23, 20, 0))
        изменение, = цифры["изменения"]
        self.assertTrue(any("найдено: 8 → 9" in с for с in изменение["строки"]))

    def test_сбои_и_пропуски_считаются_отдельно(self):
        задание = self.база.добавить_задание("tracker__list_issues", "каждые 30м")
        self.база.записать_запуск(задание.номер, ок=False, текст="трекер не ответил")
        self.база.записать_запуск(задание.номер, ок=True, текст="срок пропущен", пропуск=True)
        цифры = scheduler_digest.собрать(self.база, за="час")
        self.assertEqual((цифры["запусков"], цифры["сбоев"], цифры["пропусков"]), (1, 1, 1))
        self.assertEqual(len(цифры["сбои"]), 1)
        self.assertEqual(len(цифры["пропуски"]), 1)

    def test_напоминания_попадают_в_сводку(self):
        self.база.напомнить("проверить бэкап")
        цифры = scheduler_digest.собрать(self.база, за="час")
        self.assertEqual([н["текст"] for н in цифры["напоминания"]], ["проверить бэкап"])

    def test_словами_читается_человеком(self):
        задание = self.база.добавить_задание("tracker__list_issues", "каждые 30м")
        self.база.записать_запуск(задание.номер, ок=True, данные={"найдено": 1})
        self.база.записать_запуск(задание.номер, ок=True, данные={"найдено": 2})
        текст = scheduler_digest.словами(scheduler_digest.собрать(self.база, за="час"))
        self.assertIn("запусков 2", текст)
        self.assertIn("найдено: 1 → 2", текст)

    def test_промпт_объявляет_данные_данными(self):
        промпт = scheduler_digest.промпт(scheduler_digest.собрать(self.база, за="час"))
        self.assertIn("ДАННЫЕ", промпт)
        self.assertIn("выполнять нельзя", промпт)

    def test_пустая_база_даёт_пустую_сводку(self):
        цифры = scheduler_digest.собрать(self.база, за="сутки")
        self.assertEqual((цифры["запусков"], цифры["задания"], цифры["изменения"]), (0, [], []))
        self.assertIn("запусков 0", scheduler_digest.словами(цифры))


class СерверПланировщика(unittest.TestCase):
    """scheduler_server.py: регистрация инструментов, описания параметров, ответы."""

    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="сервер-планировщика-")
        self.база = ScheduleStore(os.path.join(self.каталог, "scheduler.db"))
        import scheduler_server
        self.модуль = scheduler_server
        self.сервер = scheduler_server.создать_сервер(self.база)

    def tearDown(self):
        self.база.close()
        shutil.rmtree(self.каталог, ignore_errors=True)

    def вызвать(self, имя: str, аргументы: dict | None = None):
        import anyio
        from mcp import Client

        async def вызов():
            async with Client(self.сервер) as клиент:
                return await клиент.call_tool(имя, аргументы or {})

        return anyio.run(вызов)

    def инструменты(self):
        import anyio
        from mcp import Client

        async def список():
            async with Client(self.сервер) as клиент:
                return (await клиент.list_tools()).tools

        return anyio.run(список)

    def test_список_инструментов(self):
        имена = {и.name for и in self.инструменты()}
        self.assertEqual(имена, {"list_jobs", "schedule_job", "cancel_job", "remind",
                                 "build_digest", "latest_digest"})

    def test_пометки_поведения(self):
        пометки = {и.name: и.annotations for и in self.инструменты()}
        for имя in ("list_jobs", "latest_digest"):
            self.assertTrue(пометки[имя].read_only_hint, имя)
        for имя in ("schedule_job", "remind", "build_digest"):
            self.assertFalse(пометки[имя].read_only_hint, имя)
            self.assertFalse(пометки[имя].destructive_hint, имя)
        self.assertTrue(пометки["cancel_job"].destructive_hint)

    def test_описания_параметров_показывают_форму_записи(self):
        схемы = {и.name: и.input_schema for и in self.инструменты()}
        свойства = схемы["schedule_job"]["properties"]
        self.assertEqual(set(схемы["schedule_job"]["required"]), {"tool", "schedule"})
        for имя, поле in свойства.items():
            with self.subTest(имя):
                self.assertTrue(поле.get("description"))
        self.assertIn("каждые 30м", свойства["schedule"]["description"])

    def test_постановка_задания(self):
        итог = self.вызвать("schedule_job", {"tool": "tracker__list_issues",
                                             "schedule": "каждые 30м",
                                             "arguments": {"queue": "MIG"},
                                             "why": "следить"})
        self.assertFalse(итог.is_error)
        задание = итог.structured_content["задание"]
        self.assertEqual(задание["инструмент"], "tracker__list_issues")
        self.assertEqual(задание["когда"], "каждые 30 минут")
        self.assertEqual(self.база.задание(задание["номер"]).аргументы, {"queue": "MIG"})

    def test_предупреждение_когда_работник_не_запущен(self):
        итог = self.вызвать("schedule_job", {"tool": "a__b", "schedule": "через 5м"})
        self.assertFalse(итог.structured_content["работник_запущен"])
        self.assertIn("worker.py", итог.structured_content["предупреждение"])

    def test_кривое_расписание_объясняется_модели(self):
        итог = self.вызвать("schedule_job", {"tool": "a__b", "schedule": "иногда"})
        self.assertTrue(итог.is_error)
        текст = итог.content[0].text
        self.assertIn("каждые 30м", текст)

    def test_имя_инструмента_проверяется(self):
        итог = self.вызвать("schedule_job", {"tool": "../../etc/passwd",
                                             "schedule": "через 5м"})
        self.assertTrue(итог.is_error)
        self.assertIn("не похоже на имя инструмента", итог.content[0].text)
        self.assertEqual(self.база.задания(), [])

    def test_список_заданий_и_пульс(self):
        self.вызвать("schedule_job", {"tool": "a__b", "schedule": "каждые 30м"})
        итог = self.вызвать("list_jobs", {})
        self.assertEqual(итог.structured_content["заданий"], 1)
        self.assertIn("работник", итог.structured_content)

    def test_снятие_задания(self):
        номер = self.вызвать("schedule_job", {"tool": "a__b", "schedule": "каждые 30м"}
                             ).structured_content["задание"]["номер"]
        итог = self.вызвать("cancel_job", {"job": номер})
        self.assertTrue(итог.structured_content["снято"])
        self.assertEqual(self.база.задание(номер).состояние, ОТМЕНЕНО)
        снова = self.вызвать("cancel_job", {"job": номер})
        self.assertTrue(снова.is_error)

    def test_напоминание_сохраняется(self):
        итог = self.вызвать("remind", {"text": "проверить бэкап PostGIS"})
        self.assertTrue(итог.structured_content["сохранено"])
        self.assertEqual(self.база.напоминания()[0].текст, "проверить бэкап PostGIS")

    def test_сводка_собирается_и_читается(self):
        задание = self.база.добавить_задание("tracker__list_issues", "каждые 30м")
        self.база.записать_запуск(задание.номер, ок=True, данные={"найдено": 1})
        self.база.записать_запуск(задание.номер, ок=True, данные={"найдено": 2})
        собрана = self.вызвать("build_digest", {"period": "час"})
        номер = собрана.structured_content["сводка"]
        self.assertEqual(собрана.structured_content["цифры"]["запусков"], 2)
        self.assertIn("найдено: 1 → 2", собрана.structured_content["словами"])
        последняя = self.вызвать("latest_digest", {})
        self.assertTrue(последняя.structured_content["есть"])
        self.assertEqual(последняя.structured_content["сводка"]["номер"], номер)

    def test_сводки_ещё_нет(self):
        итог = self.вызвать("latest_digest", {})
        self.assertFalse(итог.structured_content["есть"])
        self.assertIn("build_digest", итог.structured_content["почему"])

    def test_путь_базы_из_окружения(self):
        прежние = {к: os.environ.get(к) for к in ("SCHEDULER_DB", "MEMORY_DIR")}
        try:
            os.environ["SCHEDULER_DB"] = os.path.join(self.каталог, "своя.db")
            self.assertTrue(self.модуль.путь_базы().endswith("своя.db"))
            os.environ.pop("SCHEDULER_DB")
            os.environ["MEMORY_DIR"] = self.каталог
            self.assertEqual(self.модуль.путь_базы(),
                             os.path.join(self.каталог, "scheduler.db"))
        finally:
            for ключ, значение in прежние.items():
                if значение is None:
                    os.environ.pop(ключ, None)
                else:
                    os.environ[ключ] = значение


class _ЯщикДляРаботника:
    """Инструментарий-заглушка: проверяем расписание, а не транспорт MCP."""

    def __init__(self, ответы=None, сбой: str = "", взрыв: bool = False):
        self.вызовы: list[tuple[str, dict]] = []
        self.ответы = list(ответы or [{"найдено": 8}])
        self.сбой = сбой
        self.взрыв = взрыв
        self.закрыт = False

    def открыть(self):
        return []

    def вызвать(self, имя, аргументы=None):
        self.вызовы.append((имя, dict(аргументы or {})))
        if self.взрыв:
            raise RuntimeError("сервер внезапно кончился")
        if self.сбой:
            raise amcp.ToolboxError(self.сбой)
        данные = self.ответы[min(len(self.вызовы), len(self.ответы)) - 1]
        return amcp.ToolResult(сервер=имя.split("__")[0], инструмент=имя.split("__")[-1],
                               полное_имя=имя, аргументы=dict(аргументы or {}),
                               ок=True, текст=_json.dumps(данные, ensure_ascii=False),
                               данные=данные, секунд=0.01)

    def close(self):
        self.закрыт = True


class РаботникПланировщика(unittest.TestCase):
    """Цикл работника: срок → вызов инструмента → запись результата."""

    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="работник-")
        self.база = ScheduleStore(os.path.join(self.каталог, "scheduler.db"))
        self.ящик = _ЯщикДляРаботника()

    def tearDown(self):
        self.база.close()
        shutil.rmtree(self.каталог, ignore_errors=True)

    def работник(self, **поля):
        поля.setdefault("ящик", self.ящик)
        поля.setdefault("озвучивать", False)
        return scheduler_worker.Работник(self.база, **поля)

    def test_созревшее_задание_выполняется(self):
        задание = self.база.добавить_задание("tracker__list_issues", "каждые 30м",
                                             {"queue": "MIG"},
                                             сейчас=_datetime(2026, 9, 23, 10, 0))
        запуски = self.работник().шаг(_datetime(2026, 9, 23, 10, 30))
        self.assertEqual(len(запуски), 1)
        self.assertEqual(self.ящик.вызовы, [("tracker__list_issues", {"queue": "MIG"})])
        self.assertTrue(запуски[0].ок)
        self.assertEqual(запуски[0].данные, {"найдено": 8})
        self.assertEqual(self.база.задание(задание.номер).запусков, 1)

    def test_несозревшее_не_трогается(self):
        self.база.добавить_задание("a__b", "каждые 30м", сейчас=_datetime(2026, 9, 23, 10, 0))
        self.assertEqual(self.работник().шаг(_datetime(2026, 9, 23, 10, 10)), [])
        self.assertEqual(self.ящик.вызовы, [])

    def test_срок_переносится_от_сейчас(self):
        задание = self.база.добавить_задание("a__b", "каждые 30м",
                                             сейчас=_datetime(2026, 9, 23, 10, 0))
        self.работник().шаг(_datetime(2026, 9, 23, 10, 30))
        self.assertEqual(self.база.задание(задание.номер).срок, "2026-09-23 11:00:00")

    def test_однократное_уходит_из_расписания(self):
        задание = self.база.добавить_задание("a__b", "через 5м",
                                             сейчас=_datetime(2026, 9, 23, 10, 0))
        self.работник().шаг(_datetime(2026, 9, 23, 10, 5))
        снова = self.база.задание(задание.номер)
        self.assertEqual((снова.состояние, снова.срок, снова.запусков), (ИСПОЛНЕНО, "", 1))

    def test_сбой_вызова_записывается_и_не_роняет_работника(self):
        ящик = _ЯщикДляРаботника(сбой="сервер не подключён")
        задание = self.база.добавить_задание("a__b", "каждые 30м",
                                             сейчас=_datetime(2026, 9, 23, 10, 0))
        запуски = self.работник(ящик=ящик).шаг(_datetime(2026, 9, 23, 10, 30))
        self.assertFalse(запуски[0].ок)
        self.assertIn("не подключён", запуски[0].текст)
        self.assertEqual(self.база.задание(задание.номер).сбоев, 1)

    def test_неожиданное_исключение_тоже_переживается(self):
        ящик = _ЯщикДляРаботника(взрыв=True)
        self.база.добавить_задание("a__b", "каждые 30м", сейчас=_datetime(2026, 9, 23, 10, 0))
        запуски = self.работник(ящик=ящик).шаг(_datetime(2026, 9, 23, 10, 30))
        self.assertFalse(запуски[0].ок)
        self.assertIn("RuntimeError", запуски[0].текст)

    def test_давно_просроченное_не_догоняется(self):
        задание = self.база.добавить_задание("a__b", "каждые 30м",
                                             сейчас=_datetime(2026, 9, 23, 1, 0))
        запуски = self.работник().шаг(_datetime(2026, 9, 23, 4, 0))
        self.assertEqual(self.ящик.вызовы, [], "вызова быть не должно")
        self.assertTrue(запуски[0].пропуск)
        self.assertGreaterEqual(запуски[0].пропущено, 6)
        self.assertIn("не догонялось", запуски[0].текст)
        снова = self.база.задание(задание.номер)
        self.assertEqual(снова.срок, "2026-09-23 04:30:00")
        self.assertEqual(снова.запусков, 0)

    def test_небольшое_опоздание_не_пропуск(self):
        self.база.добавить_задание("a__b", "каждые 30м", сейчас=_datetime(2026, 9, 23, 10, 0))
        запуски = self.работник().шаг(_datetime(2026, 9, 23, 10, 30, 20))
        self.assertFalse(запуски[0].пропуск)
        self.assertEqual(len(self.ящик.вызовы), 1)

    def test_пульс_отмечается_каждым_обходом(self):
        сейчас = _datetime(2026, 9, 23, 10, 0)
        self.работник(тик=2.0).шаг(сейчас)
        пульс = self.база.работник(сейчас)
        self.assertTrue(пульс["запущен"])
        self.assertEqual(пульс["тик"], 2.0)

    def test_работать_делает_заданное_число_обходов(self):
        работник = self.работник(тик=0.2)
        self.assertEqual(работник.работать(обходов=2), 2)

    def test_остановка_прекращает_цикл(self):
        работник = self.работник(тик=0.2)
        работник.остановить()
        self.assertEqual(работник.работать(обходов=5), 0)

    def test_сводка_озвучивается_моделью(self):
        class Клиент:
            def __init__(self):
                self.вопросы = []

            def call(self, ключ, сообщения, **прочее):
                self.вопросы.append(сообщения[0]["content"])
                # Поля — как у настоящего Reply: заглушка с чужими именами
                # однажды уже спрятала ошибку (см. test_клиент_модели_настоящий).
                return Reply(text="За час всё спокойно.", model_key="ds-flash")

        клиент = Клиент()
        сводка = self.база.добавить_сводку({"запусков": 2})
        работник = self.работник(клиент=клиент, модель="ds-flash", озвучивать=True)
        текст = работник.озвучить(сводка.номер)
        self.assertEqual(текст, "За час всё спокойно.")
        self.assertEqual(self.база.сводка(сводка.номер).текст, "За час всё спокойно.")
        self.assertIn("ДАННЫЕ", клиент.вопросы[0])

    def test_клиент_модели_настоящий(self):
        """Работник должен собирать НАСТОЯЩИЙ клиент, а не молча остаться без него.

        Проверка появилась после живого прогона: работник импортировал класс
        под несуществующим именем, ошибка гасилась, и сводки молча оставались
        без текста — заглушка в тестах этого не ловила.
        """
        from agent.llm import Client

        работник = scheduler_worker.Работник(self.база, озвучивать=True)
        клиент = работник._модель()
        try:
            self.assertIsInstance(клиент, Client)
            self.assertTrue(работник.модель, "модель для сводки не выбрана по роли")
        finally:
            if клиент is not None:
                клиент.close()

    def test_сбой_модели_не_ломает_сводку(self):
        class Падающий:
            def call(self, *аргументы, **прочее):
                raise RuntimeError("провайдер лёг")

        сводка = self.база.добавить_сводку({"запусков": 2})
        работник = self.работник(клиент=Падающий(), озвучивать=True)
        self.assertEqual(работник.озвучить(сводка.номер), "")
        self.assertEqual(self.база.сводка(сводка.номер).цифры, {"запусков": 2})

    def test_задание_сводки_озвучивается_автоматически(self):
        class Клиент:
            def call(self, ключ, сообщения, **прочее):
                return Reply(text="Итог.", model_key="ds-flash")

        сводка = self.база.добавить_сводку({"запусков": 1})
        ящик = _ЯщикДляРаботника(ответы=[{"сводка": сводка.номер}])
        self.база.добавить_задание("scheduler__build_digest", "каждые 30м",
                                   сейчас=_datetime(2026, 9, 23, 10, 0))
        работник = self.работник(ящик=ящик, клиент=Клиент(), озвучивать=True)
        работник.шаг(_datetime(2026, 9, 23, 10, 30))
        self.assertEqual(self.база.сводка(сводка.номер).текст, "Итог.")

    def test_сводка_сейчас_тем_же_кодом(self):
        задание = self.база.добавить_задание("a__b", "каждые 30м")
        self.база.записать_запуск(задание.номер, ок=True, данные={"найдено": 1})
        итог = scheduler_worker.сводка_сейчас(self.база, за="час")
        self.assertEqual(итог["цифры"]["запусков"], 1)
        self.assertEqual(self.база.последняя_сводка().номер, итог["номер"])


class ПланированиеИПодтверждение(unittest.TestCase):
    """Запланированное изменение — то же изменение, только позже."""

    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="планирование-")
        self.память = os.path.join(self.каталог, "memory")
        _наполнить_память(self.память)

    def tearDown(self):
        shutil.rmtree(self.каталог, ignore_errors=True)

    def агент(self, **поля):
        агент = MemoryAgent(base_dir=self.память, router_mode=OFF, seed_project=False,
                            judge_semantic=False, require_self_report=False, **поля)
        # Планировщик в заглушке помечен меняющим — как и настоящий schedule_job:
        # «своим» его делает поле сервера, а не сам инструмент.
        агент.toolbox = _ЯщикДляАгента()
        агент.toolbox.инструменты.append(
            _Инструмент("scheduler", "schedule_job", "меняет данные"))
        return агент

    def модель(self, агент, *ответы):
        очередь = list(ответы)

        def вызов(ключ, сообщения, **кв):
            return очередь.pop(0) if очередь else Reply(text="готово", model_key="тест")

        return mock.patch.object(агент.client, "call", side_effect=вызов)

    def test_планирование_читающего_исполняется_сразу(self):
        агент = self.агент()
        try:
            просьба = _просьба("scheduler__schedule_job",
                               {"tool": "tracker__list_issues", "schedule": "каждые 30м"})
            with self.модель(агент, просьба, Reply(text="Поставил.", model_key="тест")):
                ответ = агент.ask("Собирай задачи каждые полчаса")
            self.assertEqual(ответ.заявки, [])
            self.assertEqual([в["полное_имя"] for в in ответ.вызовы],
                             ["scheduler__schedule_job"])
        finally:
            агент.close()

    def test_планирование_меняющего_становится_заявкой(self):
        агент = self.агент()
        try:
            просьба = _просьба("scheduler__schedule_job",
                               {"tool": "tracker__add_comment", "schedule": "через 1м",
                                "arguments": {"key": "MIG-2", "text": "привет"}})
            with self.модель(агент, просьба, Reply(text="Завёл заявку.", model_key="тест")):
                ответ = агент.ask("Через минуту напиши комментарий в MIG-2")
            self.assertEqual(len(ответ.заявки), 1)
            self.assertEqual(ответ.вызовы, [])
            заявка = ответ.заявки[0]
            self.assertTrue(заявка["инструмент"].endswith("schedule_job"))
            self.assertEqual(агент.pending_calls()[0]["номер"], заявка["номер"])
            self.assertEqual(агент.toolbox.вызовы, [], "вызова быть не должно")
        finally:
            агент.close()

    def test_модели_объясняют_почему_нужна_заявка(self):
        агент = self.агент()
        try:
            просьба = _просьба("scheduler__schedule_job",
                               {"tool": "tracker__add_comment", "schedule": "через 1м"})
            сообщения: list = []

            def вызов(ключ, письма, **кв):
                сообщения.append(письма)
                return просьба if len(сообщения) == 1 else Reply(text="ок", model_key="тест")

            with mock.patch.object(агент.client, "call", side_effect=вызов):
                агент.ask("Через минуту прокомментируй MIG-2")
            ответ_инструмента = [с for с in сообщения[-1] if с.get("role") == "tool"][-1]
            self.assertIn("поставит в расписание", ответ_инструмента["content"])
            self.assertIn("tracker__add_comment", ответ_инструмента["content"])
        finally:
            агент.close()

    def test_без_подтверждения_планируется_сразу(self):
        агент = self.агент(confirm_writes=False)
        try:
            просьба = _просьба("scheduler__schedule_job",
                               {"tool": "tracker__add_comment", "schedule": "через 1м"})
            with self.модель(агент, просьба, Reply(text="Поставил.", model_key="тест")):
                ответ = агент.ask("Через минуту прокомментируй MIG-2")
            self.assertEqual(ответ.заявки, [])
            self.assertEqual(len(ответ.вызовы), 1)
        finally:
            агент.close()


class СводкаВОтвете(unittest.TestCase):
    """Свежая сводка и напоминания доходят до человека сами — и ровно один раз."""

    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="сводка-в-ответе-")
        self.память = os.path.join(self.каталог, "memory")
        _наполнить_память(self.память)

    def tearDown(self):
        shutil.rmtree(self.каталог, ignore_errors=True)

    def агент(self):
        агент = MemoryAgent(base_dir=self.память, router_mode=OFF, seed_project=False,
                            judge_semantic=False, require_self_report=False)
        # Модель здесь не важна: проверяется, что к ответу прикладывается
        # накопленное работником.
        агент.client.call = lambda ключ, сообщения, **кв: Reply(
            text="Ответ агента.", model_key="тест")
        return агент

    def test_сводка_показывается_один_раз(self):
        агент = self.агент()
        try:
            агент.планировщик.добавить_сводку({"запусков": 3}, текст="За ночь всё спокойно.")
            первый = агент.ask("Что нового?")
            self.assertIsNotNone(первый.сводка)
            self.assertEqual(первый.сводка["текст"], "За ночь всё спокойно.")
            второй = агент.ask("А ещё?")
            self.assertIsNone(второй.сводка)
        finally:
            агент.close()

    def test_напоминания_показываются_один_раз(self):
        агент = self.агент()
        try:
            агент.планировщик.напомнить("проверить бэкап PostGIS")
            первый = агент.ask("Привет")
            self.assertEqual([н["текст"] for н in первый.напоминания],
                             ["проверить бэкап PostGIS"])
            self.assertEqual(агент.ask("Ещё раз").напоминания, [])
        finally:
            агент.close()

    def test_служебный_вызов_ничего_не_получает(self):
        агент = self.агент()
        try:
            агент.планировщик.напомнить("проверить бэкап")
            ответ = агент.ask("машинный текст шага", internal=True)
            self.assertEqual(ответ.напоминания, [])
            self.assertIsNone(ответ.сводка)
            # Напоминание осталось непрочитанным: человек его ещё увидит.
            self.assertEqual(len(агент.планировщик.напоминания(только_новые=True)), 1)
        finally:
            агент.close()

    def test_без_базы_планировщика_ответ_обычный(self):
        агент = self.агент()
        try:
            self.assertFalse(os.path.exists(агент.scheduler_path))
            ответ = агент.ask("Привет")
            self.assertIsNone(ответ.сводка)
            self.assertEqual(ответ.напоминания, [])
        finally:
            агент.close()

    def test_ответ_в_словаре_несёт_фоновое(self):
        агент = self.агент()
        try:
            агент.планировщик.добавить_сводку({"запусков": 1}, текст="Тихо.")
            данные = агент.ask("Что нового?").to_dict()
            self.assertEqual(данные["сводка"]["текст"], "Тихо.")
            self.assertIn("напоминания", данные)
        finally:
            агент.close()


class КонсольПланировщика(unittest.TestCase):
    """cli.py: расписание, сводки и напоминания — настоящим процессом, stdin закрыт.

    Хранилище своё на каждый тест: «в расписании пусто» — такая же проверка,
    как и остальные, и зависеть от порядка запуска она не должна.
    """

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp(prefix="планировщик-консоль-")
        self.память = os.path.join(self.каталог, "memory")
        _наполнить_память(self.память)
        self.база = os.path.join(self.память, "scheduler.db")
        self.файл = _файл_серверов(self.каталог, {"scheduler": _планировщик_сервер(self.база)})

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    def консоль(self, *ключи: str):
        итог = _subprocess.run(
            [sys.executable, os.path.join(КОРЕНЬ_ДНЯ, "cli.py"),
             "--mcp-файл", self.файл, "--память-в", self.память, *ключи],
            capture_output=True, text=True, stdin=_subprocess.DEVNULL, timeout=120,
            env={**os.environ, "MEMORY_DIR": self.память})
        вывод = итог.stdout + итог.stderr
        self.assertNotIn("Вы:", вывод, "команда открыла диалог")
        return итог.returncode, вывод

    def test_ключи_действий_не_открывают_диалог(self):
        import cli

        разбор = cli.build_parser()
        для_проверки = (
            ["--запланировать", "a__b", "--когда", "каждые 30м"],
            ["--отменить-задание", "1"],
            ["--собрать-сводку", "час"],
        )
        for ключи in для_проверки:
            with self.subTest(ключи[0]):
                self.assertTrue(cli.меняет_состояние(разбор.parse_args(ключи)))

    def test_пустое_расписание_показывается_понятно(self):
        код, вывод = self.консоль("--задания")
        self.assertEqual(код, 0, вывод)
        self.assertIn("Работник: не запущен", вывод)
        self.assertIn("В расписании пусто", вывод)

    def test_постановка_и_снятие_задания(self):
        код, вывод = self.консоль("--запланировать", "tracker__list_issues",
                                  "--когда", "каждые 30м",
                                  "--аргументы", '{"queue": "MIG"}',
                                  "--зачем", "следить за миграцией")
        self.assertEqual(код, 0, вывод)
        self.assertIn("поставлено", вывод)
        self.assertIn("worker.py", вывод)       # работник не запущен — предупреждение

        код, вывод = self.консоль("--задания")
        self.assertIn("tracker__list_issues", вывод)
        self.assertIn("каждые 30 минут", вывод)
        self.assertIn("следить за миграцией", вывод)

        хранилище = ScheduleStore(self.база)
        try:
            номер = хранилище.задания(только_активные=True)[0].номер
        finally:
            хранилище.close()
        код, вывод = self.консоль("--отменить-задание", str(номер))
        self.assertEqual(код, 0, вывод)
        self.assertIn("снято с расписания", вывод)

    def test_память_в_доходит_до_сервера_планировщика(self):
        """Агент и его MCP-сервер должны смотреть в одну базу.

        Проверка появилась после разбора: «--память-в» задавал каталог только
        агенту, а сервер планировщика брал путь из MEMORY_DIR и писал в другой
        файл — задание ставилось и тут же «пропадало».
        """
        свой = tempfile.mkdtemp(prefix="память-в-")
        # Сервер описан как в проекте: путь к базе он берёт из окружения, а не
        # получает готовым — иначе проверять было бы нечего.
        файл = _файл_серверов(свой, {"scheduler": _планировщик_сервер()})
        окружение = {к: з for к, з in os.environ.items()
                     if к not in ("MEMORY_DIR", "SCHEDULER_DB")}
        try:
            итог = _subprocess.run(
                [sys.executable, os.path.join(КОРЕНЬ_ДНЯ, "cli.py"),
                 "--mcp-файл", файл, "--память-в", свой,
                 "--запланировать", "tracker__list_queues", "--когда", "каждые 30м"],
                capture_output=True, text=True, stdin=_subprocess.DEVNULL, timeout=120,
                env=окружение)
            self.assertEqual(итог.returncode, 0, итог.stdout + итог.stderr)
            хранилище = ScheduleStore(os.path.join(свой, "scheduler.db"))
            try:
                задания = хранилище.задания(только_активные=True)
                self.assertEqual([з.инструмент for з in задания], ["tracker__list_queues"])
            finally:
                хранилище.close()
        finally:
            shutil.rmtree(свой, ignore_errors=True)

    def test_расписание_без_когда_отклоняется(self):
        код, вывод = self.консоль("--запланировать", "a__b")
        self.assertEqual(код, 1)
        self.assertIn("--когда", вывод)

    def test_кривые_аргументы_отклоняются(self):
        код, вывод = self.консоль("--запланировать", "a__b", "--когда", "через 5м",
                                  "--аргументы", "{не json}")
        self.assertEqual(код, 1)
        self.assertIn("не JSON", вывод)

    def test_непонятное_расписание_объясняется(self):
        код, вывод = self.консоль("--запланировать", "a__b", "--когда", "иногда")
        self.assertEqual(код, 1)
        self.assertIn("каждые 30м", вывод)

    def test_сводка_собирается_и_показывается(self):
        код, вывод = self.консоль("--собрать-сводку", "час")
        self.assertEqual(код, 0, вывод)
        self.assertIn("собрана", вывод)
        код, вывод = self.консоль("--сводка")
        self.assertEqual(код, 0, вывод)
        self.assertIn("ПОСЛЕДНЯЯ СВОДКА", вывод)

    def test_запуски_и_напоминания(self):
        код, вывод = self.консоль("--запуски")
        self.assertEqual(код, 0, вывод)
        self.assertIn("ИСТОРИЯ ЗАПУСКОВ", вывод)
        код, вывод = self.консоль("--напоминания")
        self.assertEqual(код, 0, вывод)
        self.assertIn("НАПОМИНАНИЯ", вывод)

    def test_справка_рассказывает_про_планировщик(self):
        код, вывод = self.консоль("--help")
        self.assertEqual(код, 0)
        self.assertIn("--запланировать", вывод)
        self.assertIn("планировщик", вывод.lower())


class РаботникПроцессом(unittest.TestCase):
    """worker.py: отдельный процесс, который и делает работу 24/7."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.каталог = tempfile.mkdtemp(prefix="работник-процесс-")
        cls.память = os.path.join(cls.каталог, "memory")
        os.makedirs(cls.память, exist_ok=True)
        cls.база = os.path.join(cls.память, "scheduler.db")
        cls.файл = _файл_серверов(cls.каталог, {"scheduler": _планировщик_сервер(cls.база)})

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.каталог, ignore_errors=True)

    def работник(self, *ключи: str, таймаут: int = 180):
        итог = _subprocess.run(
            [sys.executable, os.path.join(КОРЕНЬ_ДНЯ, "worker.py"),
             "--база", self.база, "--mcp-файл", self.файл, "--без-модели", *ключи],
            capture_output=True, text=True, stdin=_subprocess.DEVNULL, timeout=таймаут)
        return итог.returncode, итог.stdout + итог.stderr

    def test_состояние_без_выполнения(self):
        код, вывод = self.работник("--состояние")
        self.assertEqual(код, 0, вывод)
        self.assertIn("Работник:", вывод)
        self.assertIn(self.база, вывод)

    def test_один_обход_выполняет_задание(self):
        хранилище = ScheduleStore(self.база)
        try:
            задание = хранилище.добавить_задание(
                "scheduler__remind", "через 1с", {"text": "проверить бэкап"},
                сейчас=_datetime.now() - _timedelta(seconds=2))
            код, вывод = self.работник("--раз", "--тик", "0.5")
            self.assertEqual(код, 0, вывод)
            self.assertIn("Обходов сделано: 1", вывод)
            self.assertEqual([н.текст for н in хранилище.напоминания()],
                             ["проверить бэкап"])
            self.assertEqual(хранилище.задание(задание.номер).состояние, ИСПОЛНЕНО)
            self.assertTrue(хранилище.работник()["pid"])
        finally:
            хранилище.close()

    def test_сводка_одной_командой(self):
        код, вывод = self.работник("--сводка", "час")
        self.assertEqual(код, 0, вывод)
        self.assertIn("собрана", вывод)
        хранилище = ScheduleStore(self.база)
        try:
            self.assertIsNotNone(хранилище.последняя_сводка())
        finally:
            хранилище.close()

    def test_справка(self):
        код, вывод = self.работник("--help")
        self.assertEqual(код, 0)
        self.assertIn("--опоздание", вывод)
        self.assertIn("--сколько", вывод)


# --- конвейеры инструментов (день 19) -----------------------------------------
#
# Ниже — всё, из чего состоит пайплайн: подстановка данных между шагами, разбор
# описания цепочки, хранилище прогонов, исполнитель, свой MCP-сервер, рубикон
# подтверждения, консоль и страница. Модель здесь не участвует нигде: выжимка
# проверяется и на заглушке клиента, и на запасном пути без модели.

def _конвейер_сервер(база: str = "", конвейеры: str = "", отчёты: str = "",
                     модель: str = "нет", **поля) -> dict:
    """Описание сервера конвейеров для временного mcp-servers.json."""
    окружение = {"PIPELINE_MODEL": модель}
    if база:
        окружение["SCHEDULER_DB"] = база
    else:
        окружение.update({"MEMORY_DIR": "${MEMORY_DIR:-memory}",
                          "SCHEDULER_DB": "${SCHEDULER_DB:-}"})
    if конвейеры:
        окружение["PIPELINES_FILE"] = конвейеры
    if отчёты:
        окружение["REPORTS_DIR"] = отчёты
    описание = {"command": "${PYTHON}",
                "args": [os.path.join(КОРЕНЬ_ДНЯ, "pipeline_server.py")],
                "env": окружение, "таймаут": 120,
                "конвейеры": {"run_pipeline": "pipeline"}, "своё": True}
    описание.update(поля)
    return описание


def _наполнить_для_поиска(память: str) -> None:
    """Знания и решения про миграцию — на них проверяется поиск по памяти."""
    долгая = LongTermMemory(os.path.join(память, "long"), "инженер")
    долгая.knowledge.add("f-postgis", "Геометрию газопроводов храним в EPSG:3857, "
                         "индекс GIST обязателен", topic="PostGIS",
                         tags=["postgis", "миграция"])
    долгая.knowledge.add("f-mapserver", "Старые mapfile заменяются слоями OpenLayers 10",
                         topic="MapServer", tags=["миграция"])
    долгая.decisions.add("Порядок миграции",
                         "Сначала справочники, потом геометрия газопроводов",
                         reason="Геометрия ссылается на справочники")


def _файл_конвейеров(каталог: str, конвейеры: dict) -> str:
    путь = os.path.join(каталог, "pipelines.json")
    with open(путь, "w", encoding="utf-8") as файл:
        _json.dump({"конвейеры": конвейеры}, файл, ensure_ascii=False)
    return путь


ПРОСТАЯ_ЦЕПОЧКА = {
    "проба": {
        "описание": "две ступени: найти и сохранить",
        "вход": {"запрос": {"по_умолчанию": "миграция"}},
        "шаги": [
            {"имя": "находки", "инструмент": "pipeline__search",
             "аргументы": {"query": "${вход.запрос}", "limit": 3}},
            {"имя": "выжимка", "инструмент": "pipeline__summarize",
             "аргументы": {"data": "${находки.данные.находки}", "question": "${вход.запрос}"}},
        ],
    },
}


class _ЯщикКонвейера:
    """Ящик-заглушка исполнителя: отвечает заранее заготовленным на каждый шаг."""

    def __init__(self, ответы: dict | None = None, сбой_на: str = "",
                 исключение_на: str = "") -> None:
        self.ответы = ответы or {}
        self.сбой_на = сбой_на
        self.исключение_на = исключение_на
        self.вызовы: list[tuple[str, dict]] = []

    def вызвать(self, имя, аргументы=None):
        self.вызовы.append((имя, dict(аргументы or {})))
        if имя == self.исключение_на:
            raise amcp.ToolboxError(f"сервер «{имя}» не отвечает")
        данные = self.ответы.get(имя, {"ок": True})
        ок = имя != self.сбой_на
        return amcp.ToolResult(
            сервер=имя.split("__")[0], инструмент=имя.split("__")[-1], полное_имя=имя,
            аргументы=dict(аргументы or {}), ок=ок,
            текст=("нет такой задачи" if not ок else
                   _json.dumps(данные, ensure_ascii=False)[:200]),
            данные=(None if not ок else данные), секунд=0.01)


class ПодстановкаЗначений(unittest.TestCase):
    """values.py: как данные переходят от шага к шагу."""

    ПОРТФЕЛЬ = {
        "вход": {"очередь": "MIG", "сколько": 3},
        "задачи": {"данные": {"задачи": [{"ключ": "MIG-1"}, {"ключ": "MIG-2"}],
                              "найдено": 2},
                   "текст": "две задачи", "ок": True},
    }

    def test_ссылка_целиком_отдаёт_значение_как_есть(self):
        значение, след = значения.подставить("${задачи.данные.задачи}", self.ПОРТФЕЛЬ)
        self.assertEqual(значение, [{"ключ": "MIG-1"}, {"ключ": "MIG-2"}])
        self.assertEqual(след, [("задачи.данные.задачи", "список из 2")])

    def test_число_остаётся_числом(self):
        значение, _ = значения.подставить("${вход.сколько}", self.ПОРТФЕЛЬ)
        self.assertEqual(значение, 3)
        self.assertIsInstance(значение, int)

    def test_ссылка_внутри_текста_становится_строкой(self):
        значение, _ = значения.подставить("очередь ${вход.очередь}, найдено "
                                          "${задачи.данные.найдено}", self.ПОРТФЕЛЬ)
        self.assertEqual(значение, "очередь MIG, найдено 2")

    def test_подстановка_проходит_в_глубину(self):
        значение, след = значения.подставить(
            {"а": ["${вход.очередь}", {"б": "${задачи.текст}"}]}, self.ПОРТФЕЛЬ)
        self.assertEqual(значение, {"а": ["MIG", {"б": "две задачи"}]})
        self.assertEqual(len(след), 2)

    def test_без_ссылок_значение_не_меняется(self):
        значение, след = значения.подставить({"limit": 5, "flag": True}, self.ПОРТФЕЛЬ)
        self.assertEqual(значение, {"limit": 5, "flag": True})
        self.assertEqual(след, [])

    def test_ссылки_собираются_без_повторов(self):
        пути = значения.ссылки({"а": "${вход.очередь}", "б": ["${вход.очередь}",
                                                             "${задачи.текст}"]})
        self.assertEqual(пути, ["вход.очередь", "задачи.текст"])

    def test_нет_шага_ошибка_называет_доступное(self):
        with self.assertRaises(значения.ОшибкаПодстановки) as сбой:
            значения.достать(self.ПОРТФЕЛЬ, "выжимка.текст")
        self.assertIn("«вход»", str(сбой.exception))
        self.assertIn("«задачи»", str(сбой.exception))

    def test_нет_поля_ошибка_называет_поля(self):
        with self.assertRaises(значения.ОшибкаПодстановки) as сбой:
            значения.достать(self.ПОРТФЕЛЬ, "задачи.данные.нетполя")
        self.assertIn("нет поля «нетполя»", str(сбой.exception))
        self.assertIn("«задачи»", str(сбой.exception))

    def test_номер_в_списке(self):
        self.assertEqual(значения.достать(self.ПОРТФЕЛЬ, "задачи.данные.задачи.1"),
                         {"ключ": "MIG-2"})
        self.assertEqual(значения.достать(self.ПОРТФЕЛЬ, "задачи.данные.задачи.-1.ключ"),
                         "MIG-2")

    def test_не_номер_в_списке_объясняется(self):
        with self.assertRaises(значения.ОшибкаПодстановки) as сбой:
            значения.достать(self.ПОРТФЕЛЬ, "задачи.данные.задачи.первая")
        self.assertIn("не номер", str(сбой.exception))

    def test_выход_за_границы_списка(self):
        with self.assertRaises(значения.ОшибкаПодстановки) as сбой:
            значения.достать(self.ПОРТФЕЛЬ, "задачи.данные.задачи.9")
        self.assertIn("2 шт.", str(сбой.exception))

    def test_внутрь_строки_идти_некуда(self):
        with self.assertRaises(значения.ОшибкаПодстановки) as сбой:
            значения.достать(self.ПОРТФЕЛЬ, "задачи.текст.длина")
        self.assertIn("не объект и не список", str(сбой.exception))

    def test_пустая_ссылка_запрещена(self):
        with self.assertRaises(значения.ОшибкаПодстановки):
            значения.достать(self.ПОРТФЕЛЬ, "   ")

    def test_коротко_описывает_значение_одной_строкой(self):
        self.assertEqual(значения.коротко([1, 2, 3]), "список из 3")
        self.assertEqual(значения.коротко({"а": 1, "б": 2}), "объект: а, б")
        self.assertEqual(значения.коротко("строка\nвторая"), "строка вторая")
        self.assertTrue(значения.коротко("я" * 200).endswith("…"))


class ОписаниеКонвейера(unittest.TestCase):
    """spec.py: описание цепочки и проверка связей до единого вызова."""

    def конвейер(self, **поля):
        описание = {"шаги": [{"имя": "первый", "инструмент": "a__one", "аргументы": {}}]}
        описание.update(поля)
        return конвейеры_описание.из_словаря("проба", описание)

    def test_шаги_и_вход_разбираются(self):
        к = self.конвейер(
            описание="проба",
            вход={"запрос": {"описание": "что искать", "по_умолчанию": "миграция"}},
            шаги=[{"имя": "первый", "инструмент": "a__one",
                   "аргументы": {"q": "${вход.запрос}"}}])
        self.assertEqual(к.словами(), "первый")
        self.assertEqual(к.инструменты(), ["a__one"])
        self.assertEqual(к.серверы(), ["a"])
        self.assertEqual(к.вход[0].по_умолчанию, "миграция")
        self.assertEqual(к.шаг("первый").ссылки(), ["вход.запрос"])

    def test_короткая_форма_входа_это_умолчание(self):
        к = self.конвейер(вход={"очередь": "MIG"},
                          шаги=[{"имя": "ш", "инструмент": "a__one",
                                 "аргументы": {"q": "${вход.очередь}"}}])
        self.assertEqual(к.собрать_вход(), {"очередь": "MIG"})

    def test_ссылка_вперёд_запрещена(self):
        with self.assertRaises(конвейеры_описание.ОшибкаКонвейера) as сбой:
            self.конвейер(шаги=[
                {"имя": "первый", "инструмент": "a__one",
                 "аргументы": {"x": "${второй.данные}"}},
                {"имя": "второй", "инструмент": "a__two", "аргументы": {}}])
        self.assertIn("идёт позже", str(сбой.exception))

    def test_ссылка_в_никуда_называет_доступное(self):
        with self.assertRaises(конвейеры_описание.ОшибкаКонвейера) as сбой:
            self.конвейер(шаги=[{"имя": "первый", "инструмент": "a__one",
                                 "аргументы": {"x": "${нетшага.данные}"}}])
        self.assertIn("ведёт в никуда", str(сбой.exception))

    def test_необъявленный_вход_запрещён(self):
        with self.assertRaises(конвейеры_описание.ОшибкаКонвейера) as сбой:
            self.конвейер(вход={"а": {"по_умолчанию": 1}},
                          шаги=[{"имя": "ш", "инструмент": "a__one",
                                 "аргументы": {"x": "${вход.б}"}}])
        self.assertIn("которого у конвейера нет", str(сбой.exception))

    def test_повтор_имени_шага(self):
        with self.assertRaises(конвейеры_описание.ОшибкаКонвейера) as сбой:
            self.конвейер(шаги=[{"имя": "ш", "инструмент": "a__one"},
                                {"имя": "ш", "инструмент": "a__two"}])
        self.assertIn("два шага с именем", str(сбой.exception))

    def test_шаг_нельзя_назвать_входом(self):
        with self.assertRaises(конвейеры_описание.ОшибкаКонвейера) as сбой:
            self.конвейер(шаги=[{"имя": "вход", "инструмент": "a__one"}])
        self.assertIn("нельзя назвать", str(сбой.exception))

    def test_имя_инструмента_только_латиницей(self):
        for плохое in ("инструмент", "../../etc/passwd", ""):
            with self.subTest(плохое):
                with self.assertRaises(конвейеры_описание.ОшибкаКонвейера):
                    self.конвейер(шаги=[{"имя": "ш", "инструмент": плохое}])

    def test_безымянный_шаг_получает_имя_по_номеру(self):
        к = конвейеры_описание.из_словаря(
            "проба", {"шаги": [{"инструмент": "a__one"}]})
        self.assertEqual(к.шаги[0].имя, "шаг1")

    def test_имя_шага_без_точек_и_скобок(self):
        for плохое in ("шаг.один", "${шаг}", " ", "а" * 60):
            with self.subTest(плохое):
                with self.assertRaises(конвейеры_описание.ОшибкаКонвейера):
                    конвейеры_описание.из_словаря(
                        "проба", {"шаги": [{"имя": плохое, "инструмент": "a__one"}]})

    def test_пустая_цепочка_и_слишком_длинная(self):
        with self.assertRaises(конвейеры_описание.ОшибкаКонвейера):
            self.конвейер(шаги=[])
        длинная = [{"имя": f"ш{н}", "инструмент": "a__one"}
                   for н in range(конвейеры_описание.ПРЕДЕЛ_ШАГОВ + 1)]
        with self.assertRaises(конвейеры_описание.ОшибкаКонвейера) as сбой:
            self.конвейер(шаги=длинная)
        self.assertIn("предел", str(сбой.exception))

    def test_обязательный_вход_требуется(self):
        к = self.конвейер(вход={"запрос": {"обязательное": True, "описание": "что искать"}},
                          шаги=[{"имя": "ш", "инструмент": "a__one",
                                 "аргументы": {"q": "${вход.запрос}"}}])
        with self.assertRaises(конвейеры_описание.ОшибкаКонвейера) as сбой:
            к.собрать_вход({})
        self.assertIn("нужен вход «запрос»", str(сбой.exception))
        self.assertEqual(к.собрать_вход({"запрос": "PostGIS"}), {"запрос": "PostGIS"})

    def test_лишний_вход_это_ошибка_а_не_молчание(self):
        к = self.конвейер(вход={"запрос": {"по_умолчанию": "а"}},
                          шаги=[{"имя": "ш", "инструмент": "a__one",
                                 "аргументы": {"q": "${вход.запрос}"}}])
        with self.assertRaises(конвейеры_описание.ОшибкаКонвейера) as сбой:
            к.собрать_вход({"запросс": "б"})
        self.assertIn("не знает вход", str(сбой.exception))

    def test_пустая_строка_во_входе_даёт_умолчание(self):
        к = self.конвейер(вход={"запрос": {"по_умолчанию": "а"}},
                          шаги=[{"имя": "ш", "инструмент": "a__one",
                                 "аргументы": {"q": "${вход.запрос}"}}])
        self.assertEqual(к.собрать_вход({"запрос": ""}), {"запрос": "а"})

    def test_каталог_без_файла_пуст(self):
        каталог = конвейеры_описание.Каталог(os.path.join(tempfile.mkdtemp(), "нет.json"))
        self.assertEqual(каталог.имена(), [])
        self.assertFalse(каталог.есть("проба"))

    def test_каталог_ругается_на_испорченный_файл(self):
        место = tempfile.mkdtemp()
        путь = os.path.join(место, "pipelines.json")
        with open(путь, "w", encoding="utf-8") as файл:
            файл.write("{это не json")
        with self.assertRaises(конвейеры_описание.ОшибкаКонвейера) as сбой:
            конвейеры_описание.Каталог(путь).имена()
        self.assertIn("испорчен", str(сбой.exception))
        shutil.rmtree(место, ignore_errors=True)

    def test_неизвестный_конвейер_называет_известные(self):
        место = tempfile.mkdtemp()
        каталог = конвейеры_описание.Каталог(_файл_конвейеров(место, ПРОСТАЯ_ЦЕПОЧКА))
        with self.assertRaises(конвейеры_описание.ОшибкаКонвейера) as сбой:
            каталог.конвейер("нетакого")
        self.assertIn("«проба»", str(сбой.exception))
        shutil.rmtree(место, ignore_errors=True)

    def test_настоящий_файл_проекта_проверяется(self):
        """pipelines.json из репозитория должен разбираться и быть связным."""
        каталог = конвейеры_описание.Каталог(os.path.join(КОРЕНЬ_ДНЯ, "pipelines.json"))
        имена = каталог.имена()
        self.assertIn("отчёт", имена)
        отчёт = каталог.конвейер("отчёт")
        self.assertEqual(len(отчёт.шаги), 4)
        self.assertEqual(отчёт.серверы(), ["tracker", "pipeline"])
        # Цепочка задания дня: получить данные → обработать → сохранить.
        self.assertEqual(отчёт.шаги[-1].инструмент, "pipeline__save_to_file")
        self.assertIn("выжимка.данные.текст", отчёт.шаги[-1].ссылки())


class ХранилищеПрогонов(unittest.TestCase):
    """store.py: прогоны и шаги в том же файле SQLite, что и расписание."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp(prefix="прогоны-")
        self.путь = os.path.join(self.каталог, "scheduler.db")
        self.хранилище = конвейеры_хранилище.PipelineStore(self.путь)

    def tearDown(self) -> None:
        self.хранилище.close()
        shutil.rmtree(self.каталог, ignore_errors=True)

    def прогон(self, конвейер="проба", шагов=2, **поля):
        return self.хранилище.начать(конвейер, шагов=шагов, **поля)

    def test_прогон_заводится_до_первого_шага(self):
        прогон = self.прогон(вход={"запрос": "миграция"}, кто="консоль", зачем="проба")
        self.assertEqual(прогон.состояние, конвейеры_хранилище.ИДЁТ)
        прочитан = self.хранилище.прогон(прогон.номер)
        self.assertEqual(прочитан.вход, {"запрос": "миграция"})
        self.assertEqual(прочитан.кто, "консоль")

    def test_шаги_пишутся_с_подстановками(self):
        прогон = self.прогон()
        self.хранилище.записать_шаг(
            прогон.номер, 1, "находки", "pipeline__search", True,
            вход={"query": "миграция"}, подстановки=[("вход.запрос", "миграция")],
            выход={"находки": [1, 2]}, текст="найдено 2", секунд=0.02)
        шаг = self.хранилище.шаги(прогон.номер)[0]
        self.assertEqual(шаг.вход, {"query": "миграция"})
        self.assertEqual(шаг.подстановки, [["вход.запрос", "миграция"]])
        self.assertEqual(шаг.выход, {"находки": [1, 2]})
        self.assertIn("вход.запрос → миграция", шаг.словами())

    def test_счётчик_сделанных_шагов_растёт(self):
        прогон = self.прогон()
        for место in (1, 2):
            self.хранилище.записать_шаг(прогон.номер, место, f"ш{место}", "a__b", True)
        self.assertEqual(self.хранилище.прогон(прогон.номер).сделано, 2)

    def test_завершение_и_чтение(self):
        прогон = self.прогон()
        self.хранилище.записать_шаг(прогон.номер, 1, "ш", "a__b", True)
        готов = self.хранилище.завершить(прогон.номер, конвейеры_хранилище.ГОТОВ,
                                         итог="reports/о.md", секунд=1.5)
        self.assertTrue(готов.ок)
        self.assertEqual(готов.итог, "reports/о.md")
        self.assertEqual(len(готов.шаги), 1)
        self.assertIn("готов", готов.словами())

    def test_сбой_виден_в_протоколе(self):
        прогон = self.прогон()
        self.хранилище.записать_шаг(прогон.номер, 1, "ш", "a__b", False,
                                    ошибка="нет такой задачи")
        сбой = self.хранилище.завершить(прогон.номер, конвейеры_хранилище.СБОЙ,
                                        ошибка="нет такой задачи")
        self.assertFalse(сбой.ок)
        self.assertIn("встали на «ш»", сбой.словами())

    def test_состояние_только_готов_или_сбой(self):
        прогон = self.прогон()
        with self.assertRaises(конвейеры_хранилище.PipelineStoreError):
            self.хранилище.завершить(прогон.номер, "почти")

    def test_пустое_имя_конвейера_запрещено(self):
        with self.assertRaises(конвейеры_хранилище.PipelineStoreError):
            self.хранилище.начать("  ", шагов=1)

    def test_список_прогонов_и_последний(self):
        первый = self.прогон("а", шагов=1)
        второй = self.прогон("б", шагов=1)
        self.assertEqual([п.номер for п in self.хранилище.прогоны()],
                         [второй.номер, первый.номер])
        self.assertEqual(self.хранилище.прогоны("а")[0].номер, первый.номер)
        self.assertEqual(self.хранилище.последний().номер, второй.номер)
        self.assertEqual(self.хранилище.последний("а").номер, первый.номер)

    def test_счётчики_за_окно(self):
        сейчас = _datetime(2026, 9, 23, 12, 0, 0)
        давний = self.хранилище.начать("а", шагов=1, сейчас=сейчас - _timedelta(days=2))
        self.хранилище.завершить(давний.номер, конвейеры_хранилище.ГОТОВ,
                                 сейчас=сейчас - _timedelta(days=2))
        свежий = self.хранилище.начать("а", шагов=1, сейчас=сейчас)
        self.хранилище.завершить(свежий.номер, конвейеры_хранилище.СБОЙ, ошибка="ой",
                                 сейчас=сейчас)
        за_сутки = self.хранилище.счётчики(сейчас - _timedelta(days=1), сейчас)
        self.assertEqual(за_сутки["прогонов"], 1)
        self.assertEqual(за_сутки["сбоев"], 1)
        self.assertEqual(self.хранилище.счётчики()["прогонов"], 2)

    def test_длинный_выход_обрезается(self):
        прогон = self.прогон()
        self.хранилище.записать_шаг(прогон.номер, 1, "ш", "a__b", True,
                                    текст="я" * 9000)
        шаг = self.хранилище.шаги(прогон.номер)[0]
        self.assertLess(len(шаг.текст), 9000)
        self.assertIn("всего 9000 симв.", шаг.текст)

    def test_файл_общий_с_планировщиком(self):
        """Расписание и прогоны живут в одном файле, но в своих таблицах."""
        расписание_хранилище = ScheduleStore(self.путь)
        задание = расписание_хранилище.добавить_задание(
            "pipeline__run_pipeline", "каждые 30м", {"pipeline": "отчёт"})
        прогон = self.прогон(задание=задание.номер)
        self.assertEqual(self.хранилище.прогон(прогон.номер).задание, задание.номер)
        self.assertEqual(расписание_хранилище.задание(задание.номер).инструмент,
                         "pipeline__run_pipeline")
        расписание_хранилище.close()


class ИсполнительКонвейера(unittest.TestCase):
    """runner.py: цепочка выполняется сама, данные едут по ссылкам."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp(prefix="исполнитель-")
        self.хранилище = конвейеры_хранилище.PipelineStore(
            os.path.join(self.каталог, "scheduler.db"))
        self.конвейер = конвейеры_описание.из_словаря("проба", {
            "вход": {"очередь": {"по_умолчанию": "MIG"}},
            "шаги": [
                {"имя": "задачи", "инструмент": "tracker__list_issues",
                 "аргументы": {"queue": "${вход.очередь}", "limit": 20}},
                {"имя": "выжимка", "инструмент": "pipeline__summarize",
                 "аргументы": {"data": "${задачи.данные.задачи}",
                               "question": "что в работе"}},
                {"имя": "файл", "инструмент": "pipeline__save_to_file",
                 "аргументы": {"name": "отчёт", "text": "${выжимка.данные.текст}"}},
            ]})
        self.ответы = {
            "tracker__list_issues": {"задачи": [{"ключ": "MIG-1"}, {"ключ": "MIG-2"}]},
            "pipeline__summarize": {"текст": "две задачи в работе", "цифры": {"задачи": 2}},
            "pipeline__save_to_file": {"путь": "reports/2026-09-24-отчёт.md", "байт": 42},
        }

    def tearDown(self) -> None:
        self.хранилище.close()
        shutil.rmtree(self.каталог, ignore_errors=True)

    def выполнить(self, ящик=None, вход=None, **поля):
        ящик = ящик or _ЯщикКонвейера(self.ответы)
        исполнитель = конвейеры_прогон.Исполнитель(ящик, self.хранилище, кто="тест")
        return исполнитель.выполнить(self.конвейер, вход=вход, **поля), ящик

    def test_цепочка_выполняется_целиком_и_по_порядку(self):
        прогон, ящик = self.выполнить()
        self.assertTrue(прогон.ок)
        self.assertEqual(прогон.сделано, 3)
        self.assertEqual([и for и, _ in ящик.вызовы],
                         ["tracker__list_issues", "pipeline__summarize",
                          "pipeline__save_to_file"])

    def test_данные_шага_доезжают_до_следующего(self):
        """Главная проверка дня: вход шага равен выходу предыдущего."""
        прогон, ящик = self.выполнить()
        _, аргументы_выжимки = ящик.вызовы[1]
        self.assertEqual(аргументы_выжимки["data"],
                         self.ответы["tracker__list_issues"]["задачи"])
        _, аргументы_файла = ящик.вызовы[2]
        self.assertEqual(аргументы_файла["text"],
                         self.ответы["pipeline__summarize"]["текст"])
        # и то же самое видно в протоколе, а не только в вызовах
        шаг = прогон.шаги[2]
        self.assertIn(["выжимка.данные.текст", "две задачи в работе"],
                      [list(п) for п in шаг.подстановки])
        self.assertEqual(шаг.вход["text"], "две задачи в работе")

    def test_вход_конвейера_подставляется_в_первый_шаг(self):
        _, ящик = self.выполнить(вход={"очередь": "GIS"})
        self.assertEqual(ящик.вызовы[0][1], {"queue": "GIS", "limit": 20})

    def test_итог_берётся_из_пути_последнего_шага(self):
        прогон, _ = self.выполнить()
        self.assertEqual(прогон.итог, "reports/2026-09-24-отчёт.md")

    def test_сбой_шага_останавливает_цепочку(self):
        ящик = _ЯщикКонвейера(self.ответы, сбой_на="pipeline__summarize")
        прогон, ящик = self.выполнить(ящик)
        self.assertEqual(прогон.состояние, конвейеры_хранилище.СБОЙ)
        self.assertEqual(len(ящик.вызовы), 2, "третий шаг не должен был выполняться")
        self.assertEqual(прогон.сделано, 1)
        self.assertIn("нет такой задачи", прогон.ошибка)

    def test_упавший_сервер_не_роняет_прогон(self):
        ящик = _ЯщикКонвейера(self.ответы, исключение_на="tracker__list_issues")
        прогон, _ = self.выполнить(ящик)
        self.assertEqual(прогон.состояние, конвейеры_хранилище.СБОЙ)
        self.assertIn("не отвечает", прогон.шаги[0].ошибка)

    def test_данные_не_доехали_ошибка_называет_путь(self):
        ответы = dict(self.ответы)
        ответы["pipeline__summarize"] = {"итог": "без поля «текст»"}
        прогон, ящик = self.выполнить(_ЯщикКонвейера(ответы))
        self.assertEqual(прогон.состояние, конвейеры_хранилище.СБОЙ)
        self.assertIn("выжимка.данные.текст", прогон.ошибка)
        self.assertEqual(len(ящик.вызовы), 2)

    def test_прогон_записан_даже_когда_цепочка_упала(self):
        прогон, _ = self.выполнить(_ЯщикКонвейера(self.ответы, сбой_на="tracker__list_issues"))
        прочитан = self.хранилище.прогон(прогон.номер)
        self.assertEqual(прочитан.состояние, конвейеры_хранилище.СБОЙ)
        self.assertEqual(len(прочитан.шаги), 1)

    def test_ошибка_описания_приходит_до_прогона(self):
        было = len(self.хранилище.прогоны(сколько=50))
        with self.assertRaises(конвейеры_описание.ОшибкаКонвейера):
            self.выполнить(вход={"неизвестный": 1})
        self.assertEqual(len(self.хранилище.прогоны(сколько=50)), было)

    def test_исполнитель_работает_и_без_хранилища(self):
        ящик = _ЯщикКонвейера(self.ответы)
        прогон = конвейеры_прогон.выполнить(self.конвейер, ящик, кто="скрипт")
        self.assertTrue(прогон.ок)
        self.assertEqual(прогон.номер, 0)
        self.assertEqual(len(прогон.шаги), 3)

    def test_кто_зачем_и_задание_записываются(self):
        прогон, _ = self.выполнить(зачем="утренний отчёт", задание=7)
        прочитан = self.хранилище.прогон(прогон.номер)
        self.assertEqual(прочитан.зачем, "утренний отчёт")
        self.assertEqual(прочитан.задание, 7)
        self.assertEqual(прочитан.кто, "тест")

    def test_текст_шага_тоже_можно_подставить(self):
        конвейер = конвейеры_описание.из_словаря("текстовый", {"шаги": [
            {"имя": "первый", "инструмент": "a__one", "аргументы": {}},
            {"имя": "второй", "инструмент": "a__two",
             "аргументы": {"t": "${первый.текст}"}}]})
        ящик = _ЯщикКонвейера({"a__one": {"что": "нибудь"}})
        прогон = конвейеры_прогон.Исполнитель(ящик, self.хранилище).выполнить(конвейер)
        self.assertTrue(прогон.ок)
        self.assertEqual(ящик.вызовы[1][1]["t"], прогон.шаги[0].текст)

    def test_протокол_словами_читается(self):
        прогон, _ = self.выполнить()
        строка = прогон.словами()
        self.assertIn("задачи → выжимка → файл", строка)
        self.assertIn("3 из 3", строка)


class СерверКонвейеров(unittest.TestCase):
    """pipeline_server.py: search, summarize, save_to_file, run_pipeline."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp(prefix="сервер-конвейеров-")
        self.память = os.path.join(self.каталог, "memory")
        _наполнить_память(self.память)
        _наполнить_для_поиска(self.память)
        self.отчёты = os.path.join(self.каталог, "reports")
        self.прежние = {к: os.environ.get(к)
                        for к in ("MEMORY_DIR", "REPORTS_DIR", "PIPELINE_MODEL")}
        os.environ["MEMORY_DIR"] = self.память
        os.environ["REPORTS_DIR"] = self.отчёты
        os.environ["PIPELINE_MODEL"] = "нет"
        self.хранилище = конвейеры_хранилище.PipelineStore(
            os.path.join(self.память, "scheduler.db"))
        self.каталог_цепочек = конвейеры_описание.Каталог(
            _файл_конвейеров(self.каталог, ПРОСТАЯ_ЦЕПОЧКА))
        self.сервер = pipeline_server.создать_сервер(
            каталог=self.каталог_цепочек, хранилище=self.хранилище)

    def tearDown(self) -> None:
        self.хранилище.close()
        for ключ, значение in self.прежние.items():
            if значение is None:
                os.environ.pop(ключ, None)
            else:
                os.environ[ключ] = значение
        shutil.rmtree(self.каталог, ignore_errors=True)

    # --- поиск ---------------------------------------------------------------

    def test_поиск_идёт_по_всем_слоям_памяти(self):
        итог = pipeline_server._поиск("миграция", сколько=10)
        self.assertGreater(итог["найдено"], 0)
        self.assertEqual(set(итог["слои"]), set(pipeline_server.СЛОИ))
        for находка in итог["находки"]:
            self.assertIn(находка["слой"], pipeline_server.СЛОИ)

    def test_поиск_можно_сузить_до_слоя(self):
        итог = pipeline_server._поиск("миграция", где="знания", сколько=10)
        self.assertEqual(итог["слои"], ["знания"])
        self.assertTrue(all(н["слой"] == "знания" for н in итог["находки"]))

    def test_поиск_ранжирует_по_совпадению(self):
        итог = pipeline_server._поиск("миграция", сколько=5)
        веса = [н["вес"] for н in итог["находки"]]
        self.assertEqual(веса, sorted(веса, reverse=True))

    def test_поиск_находит_другие_формы_слова(self):
        """«миграция» должна находить «миграции»: иначе поиск по-русски бесполезен."""
        итог = pipeline_server._поиск("миграция", где="решения", сколько=5)
        заголовки = [н["заголовок"] for н in итог["находки"]]
        self.assertIn("Порядок миграции", заголовки)

    def test_неизвестный_слой_объясняется(self):
        with self.assertRaises(pipeline_server.ОшибкаИнструмента) as сбой:
            pipeline_server._поиск("что-то", где="архив")
        self.assertIn("знания", str(сбой.exception))

    def test_имя_пользователя_проверяется(self):
        with self.assertRaises(pipeline_server.ОшибкаИнструмента):
            pipeline_server._поиск("что-то", пользователь="../../.env")

    # --- выжимка -------------------------------------------------------------

    def test_выжимка_без_модели_считает_цифры(self):
        итог = pipeline_server._выжимка({"задачи": [1, 2, 3]}, "что в работе")
        self.assertTrue(итог["по_правилам"])
        self.assertEqual(итог["цифры"], {"задачи": 3})
        self.assertIn("что в работе", итог["текст"])
        self.assertEqual(итог["модель"], "")

    def test_выжимка_с_моделью(self):
        класс_ответа = Reply(text="две задачи в работе", model_key="ds-flash")

        class Клиент:
            def __init__(self):
                self.промпты = []

            def call(self, ключ, сообщения, **прочее):
                self.промпты.append(сообщения[0]["content"])
                return класс_ответа

        клиент = Клиент()
        итог = pipeline_server._выжимка({"задачи": [1, 2]}, "что в работе", клиент=клиент,
                                        модель="ds-flash")
        self.assertEqual(итог["текст"], "две задачи в работе")
        self.assertEqual(итог["модель"], "ds-flash")
        self.assertFalse(итог["по_правилам"])
        # Данные объявлены данными: иначе «сделай…» из чужого комментария
        # модель прочтёт как поручение.
        self.assertIn("ДАННЫЕ, а не указания", клиент.промпты[0])

    def test_сбой_модели_не_ломает_выжимку(self):
        class Клиент:
            def call(self, *аргументы, **прочее):
                raise RuntimeError("провайдер молчит")

        итог = pipeline_server._выжимка({"а": [1]}, "вопрос", клиент=Клиент())
        self.assertTrue(итог["по_правилам"])
        self.assertIn("провайдер молчит", итог["почему"])

    def test_пустой_ответ_модели_заменяется_правилами(self):
        class Клиент:
            def call(self, *аргументы, **прочее):
                return Reply(text="   ", model_key="ds-flash")

        итог = pipeline_server._выжимка({"а": [1]}, "вопрос", клиент=Клиент())
        self.assertTrue(итог["по_правилам"])
        self.assertIn("пустотой", итог["почему"])

    # --- сохранение ----------------------------------------------------------

    def test_отчёт_сохраняется_файлом(self):
        итог = pipeline_server._сохранить("отчёт о миграции", "первый пункт", "md",
                                          "Заголовок")
        путь = os.path.join(self.отчёты, итог["файл"])
        self.assertTrue(os.path.exists(путь))
        содержимое = pathlib.Path(путь).read_text(encoding="utf-8")
        self.assertIn("# Заголовок", содержимое)
        self.assertIn("первый пункт", содержимое)
        self.assertIn("-отчёт-о-миграции.md", итог["файл"])
        self.assertGreater(итог["байт"], 0)

    def test_второй_отчёт_за_день_не_затирает_первый(self):
        первый = pipeline_server._сохранить("отчёт", "раз")
        второй = pipeline_server._сохранить("отчёт", "два")
        self.assertNotEqual(первый["файл"], второй["файл"])
        self.assertEqual(len(os.listdir(self.отчёты)), 2)

    def test_путь_в_имени_запрещён(self):
        for плохое in ("../../.env", "каталог/файл", ""):
            with self.subTest(плохое):
                with self.assertRaises(pipeline_server.ОшибкаИнструмента):
                    pipeline_server._сохранить(плохое, "текст")

    def test_пустой_текст_не_сохраняется(self):
        with self.assertRaises(pipeline_server.ОшибкаИнструмента):
            pipeline_server._сохранить("отчёт", "   ")

    def test_форматы(self):
        итог = pipeline_server._сохранить("данные", '{"а": 1}', "json")
        self.assertTrue(итог["файл"].endswith(".json"))
        обёрнут = pipeline_server._сохранить("текстом", "не json", "json")
        содержимое = _json.loads(
            pathlib.Path(os.path.join(self.отчёты, обёрнут["файл"])).read_text(
                encoding="utf-8"))
        self.assertEqual(содержимое["текст"], "не json")
        with self.assertRaises(pipeline_server.ОшибкаИнструмента):
            pipeline_server._сохранить("отчёт", "текст", "pdf")

    # --- инструменты и цепочка ------------------------------------------------

    def test_у_каждого_инструмента_есть_описание(self):
        инструменты = _инструменты(self.сервер)
        имена = {и.name for и in инструменты}
        self.assertEqual(имена, {"search", "summarize", "save_to_file", "run_pipeline",
                                 "list_pipelines", "last_run"})
        for инструмент in инструменты:
            self.assertTrue((инструмент.description or "").strip(), инструмент.name)
            self.assertTrue(инструмент.input_schema)

    def test_читающие_инструменты_помечены(self):
        пометки = {и.name: (и.annotations.read_only_hint if и.annotations else None)
                   for и in _инструменты(self.сервер)}
        self.assertTrue(пометки["search"])
        self.assertTrue(пометки["summarize"])
        self.assertTrue(пометки["list_pipelines"])
        self.assertFalse(пометки["save_to_file"])
        self.assertFalse(пометки["run_pipeline"])

    def test_список_конвейеров(self):
        итог = _вызвать(self.сервер, "list_pipelines").structured_content
        self.assertEqual(итог["конвейеров"], 1)
        self.assertEqual(итог["конвейеры"][0]["цепочка"], "находки → выжимка")

    def test_запуск_цепочки_одним_вызовом(self):
        итог = _вызвать(self.сервер, "run_pipeline",
                        {"pipeline": "проба", "why": "проба"}).structured_content
        self.assertEqual(итог["состояние"], "готов")
        self.assertEqual(итог["сделано"], 2)
        шаги = {ш["шаг"]: ш for ш in итог["шаги"]}
        self.assertIn("находки.данные.находки", " ".join(шаги["выжимка"]["получил"]))

    def test_неизвестный_конвейер_объясняется_модели(self):
        итог = _вызвать(self.сервер, "run_pipeline", {"pipeline": "нетакого"})
        self.assertTrue(итог.is_error)
        self.assertIn("Есть:", _текст(итог))

    def test_последний_прогон(self):
        пусто = _вызвать(self.сервер, "last_run").structured_content
        self.assertFalse(пусто["есть"])
        _вызвать(self.сервер, "run_pipeline", {"pipeline": "проба"})
        итог = _вызвать(self.сервер, "last_run", {"full": True}).structured_content
        self.assertTrue(итог["есть"])
        self.assertEqual(итог["конвейер"], "проба")
        self.assertIn("вход", итог["шаги"][0])

    def test_ящик_зовёт_свои_инструменты_напрямую(self):
        результат = self.сервер.ящик.вызвать("pipeline__search",
                                             {"query": "миграция", "limit": 2})
        self.assertTrue(результат.ок)
        self.assertIn("Найдено", результат.текст)
        self.assertIn("находки", результат.данные)

    def test_ящик_превращает_ошибку_инструмента_в_неудачный_результат(self):
        результат = self.сервер.ящик.вызвать("pipeline__search", {"where": "архив"})
        self.assertFalse(результат.ок)
        self.assertIn("Слоя памяти", результат.текст)

    def test_ящик_ругается_на_имя_без_сервера(self):
        with self.assertRaises(RuntimeError):
            self.сервер.ящик.вызвать("простоимя", {})


class КонвейерИПодтверждение(unittest.TestCase):
    """Рубикон Дней 17-18 для цепочки: заявка на весь конвейер до первого шага."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp(prefix="конвейер-рубикон-")
        конвейеры = {
            "читающий": {"шаги": [
                {"имя": "а", "инструмент": "pipeline__search", "аргументы": {}},
                {"имя": "б", "инструмент": "pipeline__save_to_file", "аргументы": {}}]},
            "меняющий": {"шаги": [
                {"имя": "а", "инструмент": "pipeline__search", "аргументы": {}},
                {"имя": "б", "инструмент": "tracker__add_comment", "аргументы": {}}]},
            "чужой-читающий": {"шаги": [
                {"имя": "а", "инструмент": "tracker__list_issues", "аргументы": {}}]},
            "круг": {"шаги": [
                {"имя": "а", "инструмент": "pipeline__run_pipeline",
                 "аргументы": {"pipeline": "круг"}}]},
        }
        self.файл_цепочек = _файл_конвейеров(self.каталог, конвейеры)
        self.файл = _файл_серверов(self.каталог, {
            "pipeline": _конвейер_сервер(конвейеры=self.файл_цепочек),
            "tracker": {"command": "${PYTHON}", "args": ["-c", "pass"]},
        })
        self.ящик = amcp.Toolbox(["pipeline"], self.файл)
        # Соединений не поднимаем: проверяется решение о подтверждении, а оно
        # принимается до вызова и по пометкам, а не по ответу сервера.
        self.ящик.инструменты = [
            _Инструмент("pipeline", "search", "только чтение"),
            _Инструмент("pipeline", "save_to_file", "меняет данные"),
            _Инструмент("pipeline", "run_pipeline", "меняет данные"),
        ]
        self.ящик.свои = {"pipeline"}
        self.ящик.запускающие = {"pipeline__run_pipeline": "pipeline"}
        self.ящик.открыт = True
        self.ящик._конвейеры = конвейеры_описание.Каталог(self.файл_цепочек)

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    def test_читающая_цепочка_идёт_без_заявки(self):
        self.assertFalse(self.ящик.меняет("pipeline__run_pipeline",
                                          {"pipeline": "читающий"}))
        self.assertEqual(self.ящик.меняющие_шаги("читающий"), [])

    def test_меняющий_шаг_требует_заявки_и_назван(self):
        self.assertTrue(self.ящик.меняет("pipeline__run_pipeline",
                                         {"pipeline": "меняющий"}))
        self.assertEqual(self.ящик.меняющие_шаги("меняющий"), ["tracker__add_comment"])

    def test_неизвестный_конвейер_считается_опасным(self):
        self.assertTrue(self.ящик.меняет("pipeline__run_pipeline",
                                         {"pipeline": "нетакого"}))
        self.assertIn("такого конвейера нет",
                      " ".join(self.ящик.меняющие_шаги("нетакого")))

    def test_своё_хозяйство_не_требует_заявки_даже_у_меняющего_шага(self):
        """save_to_file пишет в отчёты агента — это своё, как память."""
        self.assertFalse(self.ящик.меняет("pipeline__save_to_file"))

    def test_чужой_неподключённый_сервер_считается_меняющим(self):
        """Про tracker в этом ящике известно только имя: значит, спрашиваем."""
        self.assertEqual(self.ящик.меняющие_шаги("чужой-читающий"),
                         ["tracker__list_issues"])

    def test_цепочка_ссылающаяся_на_себя_не_зацикливается(self):
        шаги = self.ящик.меняющие_шаги("круг")
        self.assertTrue(шаги)

    def test_конвейер_вызова_берёт_имя_из_аргумента(self):
        self.assertEqual(
            self.ящик.конвейер_вызова("pipeline__run_pipeline", {"pipeline": "читающий"}),
            "читающий")
        self.assertEqual(self.ящик.конвейер_вызова("pipeline__search", {"query": "а"}), "")

    def test_поле_конвейеры_читается_из_файла_серверов(self):
        серверы = amcp.config.load(self.файл)
        описание = [с for с in серверы if с.имя == "pipeline"][0]
        self.assertEqual(описание.конвейеры, {"run_pipeline": "pipeline"})
        self.assertTrue(описание.своё)
        self.assertEqual(описание.to_dict()["конвейеры"], {"run_pipeline": "pipeline"})

    def test_словарь_конвейеров_проверяется(self):
        файл = _файл_серверов(self.каталог, {
            "pipeline": _конвейер_сервер(конвейеры={"run_pipeline": 5})})
        with self.assertRaises(amcp.MCPConfigError):
            amcp.config.load(файл)


class ПрогонВОтвете(unittest.TestCase):
    """Агент: конвейер в ответе, заявка на цепочку и состояние для интерфейсов."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp(prefix="агент-конвейер-")
        self.память = os.path.join(self.каталог, "memory")
        self.файл_цепочек = _файл_конвейеров(self.каталог, ПРОСТАЯ_ЦЕПОЧКА)
        self.прежний = os.environ.get("PIPELINES_FILE")
        os.environ["PIPELINES_FILE"] = self.файл_цепочек
        self.агент = MemoryAgent(base_dir=self.память, router_mode=OFF)

    def tearDown(self) -> None:
        self.агент.close()
        if self.прежний is None:
            os.environ.pop("PIPELINES_FILE", None)
        else:
            os.environ["PIPELINES_FILE"] = self.прежний
        shutil.rmtree(self.каталог, ignore_errors=True)

    def test_протокол_прогона_попадает_в_ответ(self):
        from agent.agent import _протокол_прогона

        протокол = {"прогон": 3, "конвейер": "проба", "состояние": "готов", "шаги": []}
        вызовы = [
            amcp.ToolResult(сервер="tracker", инструмент="list_issues",
                            полное_имя="tracker__list_issues", ок=True),
            amcp.ToolResult(сервер="pipeline", инструмент="run_pipeline",
                            полное_имя="pipeline__run_pipeline", ок=True, данные=протокол),
        ]
        self.assertEqual(_протокол_прогона(вызовы), протокол)

    def test_без_конвейера_поле_пустое(self):
        from agent.agent import _протокол_прогона

        self.assertIsNone(_протокол_прогона([]))
        неудачный = [amcp.ToolResult(сервер="pipeline", инструмент="run_pipeline",
                                     полное_имя="pipeline__run_pipeline", ок=False,
                                     данные={"шаги": []})]
        self.assertIsNone(_протокол_прогона(неудачный))

    def test_состояние_конвейеров_не_затирается_счётчиками(self):
        состояние = self.агент.состояние_конвейеров()
        self.assertEqual([к["имя"] for к in состояние["конвейеры"]], ["проба"])
        self.assertEqual(состояние["прогонов"], 0)
        self.assertIn("по_конвейерам", состояние)

    def test_описания_конвейеров_видны_агенту(self):
        конвейеры = self.агент.конвейеры()
        self.assertEqual(конвейеры[0]["цепочка"], "находки → выжимка")
        self.assertEqual(конвейеры[0]["инструменты"],
                         ["pipeline__search", "pipeline__summarize"])

    def test_прогоны_читаются_из_базы(self):
        хранилище = конвейеры_хранилище.PipelineStore(self.агент.scheduler_path)
        прогон = хранилище.начать("проба", шагов=1, кто="тест")
        хранилище.записать_шаг(прогон.номер, 1, "находки", "pipeline__search", True)
        хранилище.завершить(прогон.номер, конвейеры_хранилище.ГОТОВ, итог="готово")
        хранилище.close()
        self.assertEqual(self.агент.последний_прогон()["итог"], "готово")
        self.assertEqual(len(self.агент.прогоны()), 1)
        self.assertEqual(self.агент.прогон_конвейера(прогон.номер)["шаги"][0]["имя"],
                         "находки")
        self.assertIsNone(self.агент.прогон_конвейера(999))

    def test_заявка_называет_конвейер_и_меняющий_шаг(self):
        агент = MemoryAgent(base_dir=self.память, router_mode=OFF, tools=[])

        class Ящик(_ЯщикДляАгента):
            def __init__(self):
                super().__init__()
                self.свои = {"pipeline"}
                self.инструменты.append(_Инструмент("pipeline", "run_pipeline",
                                                    "меняет данные"))

            def конвейер_вызова(self, имя, аргументы=None):
                return (аргументы or {}).get("pipeline", "")

            def меняющие_шаги(self, конвейер, глубина=3):
                return ["tracker__add_comment"]

            def меняет(self, имя, аргументы=None):
                return имя.endswith("run_pipeline")

        агент.toolbox = Ящик()
        заявки: list = []
        просьба = _просьба("pipeline__run_pipeline",
                           {"pipeline": "в-трекер"}).tool_calls[0]
        текст = агент._исполнить_просьбу(просьба, "сделай отчёт", [], заявки)
        self.assertIn("выполнит конвейер «в-трекер»", текст)
        self.assertIn("tracker__add_comment", текст)
        self.assertEqual(len(заявки), 1)
        агент.close()


class СводкаСКонвейерами(unittest.TestCase):
    """Сводка планировщика знает про прогоны конвейеров (мост Дней 18 и 19)."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp(prefix="сводка-конвейеры-")
        self.путь = os.path.join(self.каталог, "scheduler.db")
        self.расписание = ScheduleStore(self.путь)
        self.прогоны = конвейеры_хранилище.PipelineStore(self.путь)

    def tearDown(self) -> None:
        self.расписание.close()
        self.прогоны.close()
        shutil.rmtree(self.каталог, ignore_errors=True)

    def test_прогоны_попадают_в_цифры_и_в_текст(self):
        прогон = self.прогоны.начать("отчёт", шагов=1)
        self.прогоны.записать_шаг(прогон.номер, 1, "ш", "a__b", True)
        self.прогоны.завершить(прогон.номер, конвейеры_хранилище.ГОТОВ, итог="reports/о.md")
        цифры = scheduler_digest.собрать(self.расписание, за="сутки")
        self.assertEqual(цифры["конвейеры"]["прогонов"], 1)
        self.assertIn("Конвейеров выполнено: 1", scheduler_digest.словами(цифры))

    def test_без_конвейеров_сводка_прежняя(self):
        цифры = scheduler_digest.собрать(self.расписание, за="сутки")
        self.assertEqual(цифры["конвейеры"]["прогонов"], 0)
        self.assertNotIn("Конвейеров", scheduler_digest.словами(цифры))


class КонсольКонвейеров(unittest.TestCase):
    """cli.py: конвейеры настоящим процессом, stdin закрыт."""

    def setUp(self) -> None:
        self.каталог = tempfile.mkdtemp(prefix="конвейеры-консоль-")
        self.память = os.path.join(self.каталог, "memory")
        _наполнить_память(self.память)
        self.отчёты = os.path.join(self.каталог, "reports")
        self.база = os.path.join(self.память, "scheduler.db")
        self.файл_цепочек = _файл_конвейеров(self.каталог, ПРОСТАЯ_ЦЕПОЧКА)
        self.файл = _файл_серверов(self.каталог, {
            "pipeline": _конвейер_сервер(self.база, self.файл_цепочек, self.отчёты)})

    def tearDown(self) -> None:
        shutil.rmtree(self.каталог, ignore_errors=True)

    def консоль(self, *ключи: str):
        итог = _subprocess.run(
            [sys.executable, os.path.join(КОРЕНЬ_ДНЯ, "cli.py"),
             "--mcp-файл", self.файл, "--память-в", self.память, *ключи],
            capture_output=True, text=True, stdin=_subprocess.DEVNULL, timeout=180,
            env={**os.environ, "MEMORY_DIR": self.память,
                 "PIPELINES_FILE": self.файл_цепочек, "REPORTS_DIR": self.отчёты,
                 "PIPELINE_MODEL": "нет"})
        вывод = итог.stdout + итог.stderr
        self.assertNotIn("Вы:", вывод, "команда открыла диалог")
        return итог.returncode, вывод

    def test_ключ_запуска_не_открывает_диалог(self):
        import cli

        разбор = cli.build_parser()
        self.assertTrue(cli.меняет_состояние(разбор.parse_args(["--конвейер", "проба"])))

    def test_список_конвейеров(self):
        код, вывод = self.консоль("--конвейеры")
        self.assertEqual(код, 0, вывод)
        self.assertIn("проба: находки → выжимка", вывод)
        self.assertIn("pipeline__search", вывод)

    def test_запуск_показывает_передачу_данных(self):
        код, вывод = self.консоль("--конвейер", "проба", "--вход", "запрос=миграция")
        self.assertEqual(код, 0, вывод)
        self.assertIn("готов", вывод)
        self.assertIn("получил вход.запрос → миграция", вывод)
        self.assertIn("получил находки.данные.находки", вывод)

    def test_протокол_читается_из_базы_другим_процессом(self):
        self.консоль("--конвейер", "проба")
        код, вывод = self.консоль("--прогон", "1")
        self.assertEqual(код, 0, вывод)
        self.assertIn("ПРОТОКОЛ ПРОГОНА", вывод)
        self.assertIn("находки → pipeline__search", вывод)
        код, вывод = self.консоль("--прогоны")
        self.assertIn("№1 проба", вывод)

    def test_неизвестный_конвейер_объясняется(self):
        код, вывод = self.консоль("--конвейер", "нетакого")
        self.assertNotEqual(код, 0)
        self.assertIn("Есть:", вывод)

    def test_нет_прогона_не_падение(self):
        код, вывод = self.консоль("--прогон", "7")
        self.assertEqual(код, 1)
        self.assertIn("Прогона №7 нет", вывод)

    def test_разбор_входа(self):
        import cli

        self.assertEqual(cli.разобрать_вход(["запрос=PostGIS индексы", "сколько=3",
                                             'фильтр={"а": 1}']),
                         {"запрос": "PostGIS индексы", "сколько": 3, "фильтр": {"а": 1}})
        with self.assertRaises(ValueError):
            cli.разобрать_вход(["простослово"])

    def test_протокол_сервера_переводится_для_печати(self):
        import cli

        печатный = cli.протокол_прогона({
            "прогон": 4, "конвейер": "проба", "состояние": "готов", "шагов": 1,
            "сделано": 1, "секунд": 0.2, "итог": "reports/о.md",
            "шаги": [{"шаг": "находки", "инструмент": "pipeline__search", "ок": True,
                      "получил": ["вход.запрос → миграция"], "вернул": "найдено 3"}]})
        self.assertEqual(печатный["номер"], 4)
        self.assertEqual(печатный["шаги"][0]["подстановки"],
                         [["вход.запрос", "миграция"]])
        self.assertEqual(печатный["шаги"][0]["место"], 1)


import contextlib as _contextlib
import io as _io

from agent.llm import LLMError
from agent.orchestra import flow as оркестр_флоу, order as порядок_вызовов
from agent.orchestra import plan as планы, routing as оркестр_маршрут
from agent.orchestra import store as флоу_хранилище

# --- оркестровка MCP (день 20) ------------------------------------------------
#
# Здесь проверяется всё, из чего состоит оркестровка: правила порядка, план,
# маршрутизатор серверов, хранилище флоу, исполнитель, агент, консоль и
# страница. Модель не зовётся нигде: и маршрут, и план — это разбор ответа,
# а ответы даёт заглушка по сценарию.

def _ответ_с_вызовами(*пары, текст=""):
    """Ответ модели с несколькими просьбами о вызовах в одном круге."""
    вызовы = [{"id": ид, "function": {"name": имя, "arguments": _json.dumps(аргументы)}}
              for ид, имя, аргументы in пары]
    return Reply(text=текст, model_key="тест", tool_calls=разобрать_вызовы(вызовы),
                 message={"role": "assistant", "content": текст,
                          "tool_calls": [{"id": в["id"], "type": "function",
                                          "function": в["function"]} for в in вызовы]})


class _КлиентПоСценарию:
    """Модель-заглушка: отдаёт заготовленные ответы по одному на вызов."""

    def __init__(self, *ответы):
        self.очередь = list(ответы)
        self.запросы: list[dict] = []

    def call(self, ключ, сообщения, **кв):
        self.запросы.append({"ключ": ключ, "сообщения": сообщения, "tools": кв.get("tools")})
        if not self.очередь:
            return Reply(text="итог", model_key="тест")
        return self.очередь.pop(0)

    def close(self):
        pass


class _ЯщикОркестра:
    """Инструментарий-заглушка для флоу: свой набор инструментов и ответов."""

    def __init__(self, ответы: dict | None = None, сбой_на: str = "",
                 исключение_на: str = "", имена: list | None = None):
        self.ответы = ответы or {}
        self.сбой_на = сбой_на
        self.исключение_на = исключение_на
        self.имена = ["tracker", "pipeline"] if имена is None else list(имена)
        self.вызовы: list[tuple[str, dict]] = []
        self.закрыт = False
        self.инструменты = [
            _Инструмент("tracker", "get_issue", "только чтение"),
            _Инструмент("tracker", "list_issues", "только чтение"),
            _Инструмент("tracker", "add_comment", "меняет данные"),
            _Инструмент("pipeline", "search", "только чтение"),
            _Инструмент("pipeline", "summarize", "только чтение"),
            _Инструмент("pipeline", "save_to_file", "меняет данные"),
        ]

    def открыть(self):
        return []

    def сводка(self):
        return {"серверов": 2, "подключено": 2, "серверы": self.имена, "недоступны": [],
                "инструментов": len(self.инструменты), "читающих": 4, "меняющих": 2,
                "токенов": 300, "повторы_имён": []}

    def для_модели(self):
        return [и.for_model() for и in self.инструменты]

    def найти(self, имя):
        return next((и for и in self.инструменты if и.полное_имя == имя), None)

    def меняет(self, имя, аргументы=None):
        инструмент = self.найти(имя)
        return True if инструмент is None else инструмент.доступ != "только чтение"

    def вызвать(self, имя, аргументы=None):
        self.вызовы.append((имя, dict(аргументы or {})))
        if имя == self.исключение_на:
            raise amcp.ToolboxError(f"сервер «{имя}» не отвечает")
        данные = self.ответы.get(имя, {"ок": True})
        ок = имя != self.сбой_на
        return amcp.ToolResult(
            сервер=имя.split("__")[0], инструмент=имя.split("__")[-1], полное_имя=имя,
            аргументы=dict(аргументы or {}), ок=ок,
            текст=("нет такой задачи" if not ок else _json.dumps(данные, ensure_ascii=False)),
            данные=(None if not ок else данные), секунд=0.01)

    def close(self):
        self.закрыт = True


def _план_ответ(цель, *инструменты):
    """Ответ круга планирования: JSON с шагами."""
    шаги = [{"инструмент": и, "зачем": f"нужно для {и}"} for и in инструменты]
    return Reply(text=_json.dumps({"цель": цель, "шаги": шаги}, ensure_ascii=False),
                 model_key="тест")


class ПравилаПорядка(unittest.TestCase):
    """«Что после чего»: заслон, который стоит до вызова, а не после."""

    def setUp(self):
        self.порядок = порядок_вызовов.загрузить()

    def test_комментарий_без_чтения_задачи_не_проходит(self):
        вердикт = self.порядок.проверить("tracker__add_comment", [])
        self.assertFalse(вердикт.можно)
        self.assertIn("tracker__get_issue", вердикт.почему)
        self.assertIn("догадке", вердикт.почему)

    def test_после_чтения_задачи_комментарий_проходит(self):
        вердикт = self.порядок.проверить("tracker__add_comment", ["tracker__get_issue"])
        self.assertTrue(вердикт.можно)
        self.assertEqual(вердикт.закрыто, "tracker__get_issue")

    def test_любой_из_перечисленных_открывает_дорогу(self):
        self.assertTrue(self.порядок.проверить(
            "tracker__add_comment", ["tracker__list_issues"]).можно)

    def test_шаблон_в_после_срабатывает(self):
        вердикт = self.порядок.проверить("pipeline__summarize",
                                         ["deepwiki__ask_wiki_question"])
        self.assertTrue(вердикт.можно)
        self.assertEqual(вердикт.закрыто, "deepwiki__ask_wiki_question")

    def test_инструмент_без_правил_проходит_всегда(self):
        self.assertTrue(self.порядок.проверить("scheduler__list_jobs", []).можно)

    def test_план_с_нарушением_получает_замечание(self):
        # В этом плане нарушены оба шага: сохранять нечего (выжимки ещё нет) и
        # обрабатывать нечего (данных не получали). Замечание на каждый — это
        # и нужно: человек должен видеть все слабые места плана сразу.
        замечания = self.порядок.проверить_план(
            ["pipeline__save_to_file", "pipeline__summarize"])
        self.assertEqual(len(замечания), 2)
        self.assertIn("шаг 1 «pipeline__save_to_file»", замечания[0])
        self.assertIn("шаг 2 «pipeline__summarize»", замечания[1])

    def test_верный_план_замечаний_не_даёт(self):
        self.assertEqual(self.порядок.проверить_план(
            ["tracker__get_issue", "pipeline__search", "pipeline__summarize",
             "pipeline__save_to_file", "tracker__add_comment"]), [])

    def test_файл_читается_и_совпадает_со_встроенными(self):
        из_файла = порядок_вызовов.загрузить(os.path.join(КОРЕНЬ_ДНЯ, "flow-rules.json"))
        встроенные = порядок_вызовов.разобрать(порядок_вызовов.ПО_УМОЛЧАНИЮ)
        self.assertEqual(из_файла.to_dict()["правила"], встроенные.to_dict()["правила"])
        self.assertEqual(из_файла.путь, os.path.join(КОРЕНЬ_ДНЯ, "flow-rules.json"))

    def test_нет_файла_значит_встроенные_правила(self):
        порядок = порядок_вызовов.загрузить("/нет/такого/файла.json")
        self.assertEqual(порядок.путь, "встроенные")
        self.assertEqual(len(порядок), len(порядок_вызовов.ПО_УМОЛЧАНИЮ))

    def test_испорченный_файл_объясняет_причину(self):
        каталог = tempfile.mkdtemp(prefix="правила-")
        try:
            путь = os.path.join(каталог, "flow-rules.json")
            with open(путь, "w", encoding="utf-8") as файл:
                файл.write("{не json")
            with self.assertRaises(порядок_вызовов.ОшибкаПорядка) as ошибка:
                порядок_вызовов.загрузить(путь)
            self.assertIn("не читается", str(ошибка.exception))
        finally:
            shutil.rmtree(каталог, ignore_errors=True)

    def test_правило_без_имени_инструмента_отвергается(self):
        with self.assertRaises(порядок_вызовов.ОшибкаПорядка):
            порядок_вызовов.разобрать([{"после": ["a"]}])

    def test_правило_без_после_отвергается(self):
        with self.assertRaises(порядок_вызовов.ОшибкаПорядка) as ошибка:
            порядок_вызовов.разобрать([{"инструмент": "a", "после": []}])
        self.assertIn("непустым списком", str(ошибка.exception))

    def test_после_строкой_читается_как_список(self):
        порядок = порядок_вызовов.разобрать([{"инструмент": "a", "после": "b"}])
        self.assertEqual(порядок.правила[0].после, ["b"])

    def test_словами_перечисляет_правила(self):
        строка = self.порядок.словами()
        self.assertIn("tracker__add_comment", строка)
        self.assertIn("после", строка)
        self.assertEqual(порядок_вызовов.Порядок().словами(), "Правил порядка нет.")


class ПланФлоу(unittest.TestCase):
    """План — намерение агента, с которым потом сверяют факт."""

    ДОСТУПНЫЕ = ["tracker__get_issue", "pipeline__search", "pipeline__summarize"]

    def test_обычный_план_разбирается(self):
        текст = _json.dumps({"цель": "разобрать MIG-2", "шаги": [
            {"инструмент": "tracker__get_issue", "зачем": "прочитать"},
            {"инструмент": "pipeline__search", "зачем": "память"}]}, ensure_ascii=False)
        план = планы.разобрать(текст, self.ДОСТУПНЫЕ)
        self.assertEqual(план.цель, "разобрать MIG-2")
        self.assertEqual(план.инструменты(), ["tracker__get_issue", "pipeline__search"])
        self.assertEqual(план.шаги[0].место, 1)
        self.assertEqual(план.шаги[0].зачем, "прочитать")
        self.assertEqual(план.серверы(), ["tracker", "pipeline"])

    def test_json_в_обёртке_из_текста_находится(self):
        план = планы.разобрать(
            'Вот план:\n```json\n{"цель": "ц", "шаги": [{"инструмент": "pipeline__search"}]}\n```',
            self.ДОСТУПНЫЕ)
        self.assertEqual(план.инструменты(), ["pipeline__search"])

    def test_мусор_вместо_json_не_рушит_флоу(self):
        план = планы.разобрать("я подумаю об этом завтра", self.ДОСТУПНЫЕ)
        self.assertTrue(план.пуст)
        self.assertIn("не разобран", план.замечания[0])

    def test_неизвестный_инструмент_помечается(self):
        план = планы.разобрать(
            '{"шаги": [{"инструмент": "нет__такого"}]}', self.ДОСТУПНЫЕ)
        self.assertFalse(план.шаги[0].известен)
        self.assertIn("нет среди доступных", план.замечания[0])

    def test_шаг_строкой_тоже_читается(self):
        план = планы.разобрать('{"шаги": ["pipeline__search"]}', self.ДОСТУПНЫЕ)
        self.assertEqual(план.инструменты(), ["pipeline__search"])

    def test_слишком_длинный_план_обрезается(self):
        шаги = [{"инструмент": "pipeline__search"} for _ in range(20)]
        план = планы.разобрать(_json.dumps({"шаги": шаги}), self.ДОСТУПНЫЕ)
        self.assertEqual(len(план.шаги), планы.ПРЕДЕЛ_ШАГОВ)
        self.assertIn("оставлены первые", план.замечания[-1])

    def test_замечания_порядка_попадают_в_план(self):
        план = планы.разобрать(
            '{"шаги": [{"инструмент": "pipeline__summarize"}]}',
            ["pipeline__summarize"], порядок_вызовов.загрузить())
        self.assertTrue(any("Обрабатывать нечего" in з for з in план.замечания))

    def test_пустой_список_шагов_это_ответ(self):
        план = планы.разобрать('{"цель": "ответить словами", "шаги": []}', self.ДОСТУПНЫЕ)
        self.assertTrue(план.пуст)
        self.assertEqual(план.замечания, [])
        self.assertIn("План пуст", план.словами())

    def test_промпт_перечисляет_инструменты_и_вопрос(self):
        сообщения = планы.промпт("Что в работе?", [
            {"имя": "tracker__list_issues", "описание": "Список задач"}])
        self.assertEqual(сообщения[0]["role"], "system")
        self.assertIn("Ничего не вызывай", сообщения[0]["content"])
        self.assertIn("tracker__list_issues — Список задач", сообщения[1]["content"])
        self.assertIn("Что в работе?", сообщения[1]["content"])

    def test_план_переживает_сохранение_в_базу(self):
        план = планы.разобрать(
            '{"цель": "ц", "шаги": [{"инструмент": "pipeline__search", "зачем": "з"}]}',
            self.ДОСТУПНЫЕ)
        снова = планы.План.from_dict(план.to_dict())
        self.assertEqual(снова.цель, "ц")
        self.assertEqual(снова.шаги[0].зачем, "з")
        self.assertEqual(снова.инструменты(), план.инструменты())

    def test_для_модели_печатает_шаги(self):
        план = планы.разобрать(
            '{"цель": "ц", "шаги": [{"инструмент": "pipeline__search", "зачем": "з"}]}',
            self.ДОСТУПНЫЕ)
        строка = план.для_модели()
        self.assertIn("Цель: ц", строка)
        self.assertIn("1. pipeline__search", строка)

    def test_сервер_шага_виден_из_имени(self):
        план = планы.разобрать('{"шаги": [{"инструмент": "deepwiki__ask"}]}', [])
        self.assertEqual(план.шаги[0].сервер, "deepwiki")


class БезопасныйЗапускТестов(unittest.TestCase):
    """run_tests.py не наследует секреты и не переносит локальный .env."""

    def setUp(self):
        self.корень = pathlib.Path(tempfile.mkdtemp(prefix="runner-fixture-"))
        self.addCleanup(shutil.rmtree, self.корень, True)

    def git(self, *аргументы):
        import run_tests
        return _subprocess.run(
            ["git", *аргументы], cwd=self.корень, check=True,
            env=run_tests._окружение_git(),
            stdout=_subprocess.PIPE, stderr=_subprocess.PIPE,
        )

    def под_guard(self, код: str, таймаут: int = 10):
        import run_tests
        guard = self.корень / "guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(run_tests._ПРЕДОХРАНИТЕЛЬ, encoding="utf-8")
        return _subprocess.run(
            [sys.executable, "-B", "-c", код],
            env=run_tests._окружение(self.корень / "state", guard),
            capture_output=True, text=True, timeout=таймаут,
        )

    def test_окружение_только_разрешённое_и_синтетическое(self):
        import run_tests
        временный = self.корень / "state"
        with mock.patch.dict(os.environ, {
                "UNLISTED_SENTINEL": "must-not-inherit",
                "ALTERNATE_PROVIDER_CREDENTIAL": "must-not-inherit",
                "EXAMPLE_API_KEY": "must-not-inherit",
        }):
            окружение = run_tests._окружение(временный, self.корень / "guard")
        for имя in ("UNLISTED_SENTINEL", "ALTERNATE_PROVIDER_CREDENTIAL", "EXAMPLE_API_KEY"):
            self.assertNotIn(имя, окружение)
        self.assertEqual(окружение["HF_HUB_OFFLINE"], "1")
        self.assertEqual(окружение["TMPDIR"], str(временный / "tmp"))
        self.assertEqual(окружение["TRACKER_MOCK_TOKEN"], "synthetic-mock-token")

    def test_снимок_исключает_env_и_дочерний_процесс_его_не_читает(self):
        import run_tests
        self.git("init", "-q")
        probe = self.корень / "probe.py"
        probe.write_text(
            "import os\nfrom dotenv import load_dotenv\nload_dotenv()\n"
            "print(os.environ.get('DAY30_SECRET_SENTINEL', 'missing'))\n",
            encoding="utf-8")
        (self.корень / ".env").write_text(
            "DAY30_SECRET_SENTINEL=must-not-copy\n", encoding="utf-8")
        (self.корень / ".env.example").write_text(
            "DAY30_EXAMPLE=synthetic\n", encoding="utf-8")
        self.git("add", "probe.py", ".env.example")

        снимок = self.корень / "snapshot"
        снимок.mkdir()
        run_tests._копировать(self.корень, снимок)
        self.assertNotIn("must-not-copy", (снимок / ".env").read_text(encoding="utf-8"))
        self.assertTrue((снимок / ".env.example").is_file())

        состояние = self.корень / "state"
        запуск = _subprocess.run(
            [sys.executable, "-B", "probe.py"], cwd=снимок,
            env=run_tests._окружение(состояние, self.корень / "guard"),
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(запуск.stdout.strip(), "missing")

    def test_сетевой_guard_передаётся_потомку_с_явным_env(self):
        import run_tests
        guard = self.корень / "guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(run_tests._ПРЕДОХРАНИТЕЛЬ, encoding="utf-8")
        parent = self.корень / "parent.py"
        parent.write_text(
            "import os, subprocess, sys\n"
            "code = '''import os, socket\n"
            "print(os.environ.get(\"DAY30_TEST_GUARD\", \"missing\"))\n"
            "try:\n    socket.getaddrinfo(\"198.51.100.1\", 443)\n"
            "except OSError:\n    print(\"blocked\")\n"
            "else:\n    print(\"unblocked\")\n"
            "for address in ((\"127.0.0.1\", 11434), (\"127.0.0.1\", 54321)):\n"
            "    try:\n        socket.socket().connect(address)\n"
            "    except OSError:\n        print(\"blocked-local\")\n"
            "    else:\n        print(\"unblocked-local\")\n"
            "listener = socket.socket()\n"
            "listener.bind((\"127.0.0.1\", 0)); listener.listen()\n"
            "client = socket.create_connection(listener.getsockname())\n"
            "accepted, _ = listener.accept()\n"
            "client.close(); accepted.close(); listener.close()\n"
            "print(\"allowed-test-listener\")\n'''\n"
            "env = {\"PATH\": os.environ[\"PATH\"], \"PYTHONIOENCODING\": \"utf-8\"}\n"
            "done = subprocess.run([sys.executable, \"-B\", \"-c\", code], env=env, "
            "capture_output=True, text=True, check=True)\n"
            "print(done.stdout, end=\"\")\n",
            encoding="utf-8")
        запуск = _subprocess.run(
            [sys.executable, "-B", str(parent)],
            env=run_tests._окружение(self.корень / "state", guard),
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(запуск.stdout.splitlines(), [
            "1", "blocked", "blocked-local", "blocked-local", "allowed-test-listener",
        ])

    def test_сетевой_guard_не_ломает_socketpair_и_testclient(self):
        import run_tests
        guard = self.корень / "guard"
        guard.mkdir()
        (guard / "sitecustomize.py").write_text(run_tests._ПРЕДОХРАНИТЕЛЬ, encoding="utf-8")
        probe = self.корень / "testclient_probe.py"
        probe.write_text(
            "import socket\n"
            "from fastapi import FastAPI\n"
            "from fastapi.testclient import TestClient\n"
            "a, b = socket.socketpair()\n"
            "a.sendall(b'x')\n"
            "assert b.recv(1) == b'x'\n"
            "a.close(); b.close()\n"
            "app = FastAPI()\n"
            "@app.get('/')\n"
            "async def root(): return {'ok': True}\n"
            "response = TestClient(app).get('/')\n"
            "assert response.status_code == 200 and response.json() == {'ok': True}\n",
            encoding="utf-8")
        запуск = _subprocess.run(
            [sys.executable, "-B", str(probe)],
            env=run_tests._окружение(self.корень / "state", guard),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual((запуск.returncode, запуск.stdout, запуск.stderr), (0, "", ""))

    def test_сетевой_guard_разделяет_family_type_protocol_и_адрес(self):
        запуск = self.под_guard(r'''
import socket

def запрещён(sock, address):
    try:
        sock.connect(address)
    except OSError as exc:
        assert "запрещ" in str(exc).lower(), exc
    else:
        raise AssertionError(f"соединение неожиданно разрешено: {address}")
    finally:
        sock.close()

udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.bind(("127.0.0.1", 0))
udp_port = udp.getsockname()[1]
запрещён(socket.socket(socket.AF_INET, socket.SOCK_STREAM), ("127.0.0.1", udp_port))
udp.close()

alias = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
alias.bind(("127.0.0.2", 0))
alias.listen()
alias_port = alias.getsockname()[1]
запрещён(socket.socket(socket.AF_INET, socket.SOCK_STREAM), ("127.0.0.1", alias_port))
запрещён(socket.socket(socket.AF_INET6, socket.SOCK_STREAM), ("::1", alias_port))
alias.close()
print("endpoint-isolation-ok")
''')
        self.assertEqual((запуск.returncode, запуск.stdout, запуск.stderr),
                         (0, "endpoint-isolation-ok\n", ""))

    def test_сетевой_guard_проверяет_bind_и_жизненный_цикл_listener(self):
        запуск = self.под_guard(r'''
import socket
import sitecustomize
import subprocess
import sys

def запрещён(sock, address):
    try:
        sock.connect(address)
    except OSError as exc:
        assert "запрещ" in str(exc).lower(), exc
    else:
        raise AssertionError(f"соединение неожиданно разрешено: {address}")
    finally:
        sock.close()

настоящий_bind = sitecustomize._настоящий_bind
вызовы = []
def отмеченный_bind(*args, **kwargs):
    вызовы.append((args, kwargs))
    return настоящий_bind(*args, **kwargs)
sitecustomize._настоящий_bind = отмеченный_bind
try:
    for address in (("0.0.0.0", 0), ("192.0.2.10", 0)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(address)
        except OSError:
            pass
        else:
            raise AssertionError(f"bind неожиданно разрешён: {address}")
        finally:
            sock.close()
    sock6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        sock6.bind(("::", 0))
    except OSError:
        pass
    else:
        raise AssertionError("IPv6 wildcard bind неожиданно разрешён")
    finally:
        sock6.close()
finally:
    sitecustomize._настоящий_bind = настоящий_bind
assert вызовы == [], вызовы

listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen()
endpoint = listener.getsockname()
client = socket.create_connection(endpoint, timeout=1)
accepted, _ = listener.accept()
client.close()
accepted.close()
listener.close()
запрещён(socket.socket(socket.AF_INET, socket.SOCK_STREAM), endpoint)

server_code = r"""
import socket
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen()
print(listener.getsockname()[1], flush=True)
connection, _ = listener.accept()
connection.close()
listener.close()
"""
child = subprocess.Popen(
    [sys.executable, "-B", "-c", server_code],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
child_endpoint = ("127.0.0.1", int(child.stdout.readline().strip()))
child_client = socket.create_connection(child_endpoint, timeout=1)
child_client.close()
_, child_stderr = child.communicate(timeout=3)
assert child.returncode == 0, child_stderr
запрещён(socket.socket(socket.AF_INET, socket.SOCK_STREAM), child_endpoint)

failed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
def сбой_bind(*args, **kwargs):
    raise OSError("synthetic bind failure")
sitecustomize._настоящий_bind = сбой_bind
try:
    try:
        failed.bind(("127.0.0.1", 49152))
    except OSError as exc:
        assert "synthetic bind failure" in str(exc)
    else:
        raise AssertionError("неуспешный bind неожиданно удался")
finally:
    sitecustomize._настоящий_bind = настоящий_bind
assert failed not in sitecustomize._связанные
failed.close()
запрещён(socket.socket(socket.AF_INET, socket.SOCK_STREAM), ("127.0.0.1", 49152))

for port in (8443, 11434, 11435, 11436):
    запрещён(socket.socket(socket.AF_INET, socket.SOCK_STREAM), ("127.0.0.1", port))
запрещён(socket.socket(socket.AF_INET, socket.SOCK_STREAM), ("198.51.100.10", 443))
try:
    socket.getaddrinfo("example.invalid", 443)
except OSError as exc:
    assert "dns" in str(exc).lower(), exc
else:
    raise AssertionError("внешний DNS неожиданно разрешён")
print("bind-lifecycle-ok")
''')
        self.assertEqual((запуск.returncode, запуск.stdout, запуск.stderr),
                         (0, "bind-lifecycle-ok\n", ""))

    def test_снимок_копирует_разрешённый_неотслеживаемый_security_тест(self):
        import run_tests
        self.git("init", "-q")
        новый = self.корень / "test_service_journal_security.py"
        новый.write_text("SYNTHETIC_TEST = True\n", encoding="utf-8")
        снимок = self.корень / "snapshot"
        снимок.mkdir()
        run_tests._копировать(self.корень, снимок)
        self.assertEqual(
            (снимок / "test_service_journal_security.py").read_text(encoding="utf-8"),
            "SYNTHETIC_TEST = True\n",
        )

    def test_ссылка_на_неотслеживаемый_env_отклонена_до_копирования(self):
        import run_tests
        self.git("init", "-q")
        (self.корень / ".env").write_text("SYNTHETIC_ONLY=value\n", encoding="utf-8")
        (self.корень / "settings.txt").symlink_to(".env")
        self.git("add", "settings.txt")
        снимок = self.корень / "snapshot"
        снимок.mkdir()
        with self.assertRaisesRegex(RuntimeError, "ссылки запрещены"):
            run_tests._копировать(self.корень, снимок)
        self.assertEqual(list(снимок.iterdir()), [])

    def test_ровно_шесть_модулей_а_живой_режим_отклонён(self):
        import contextlib
        import io
        import run_tests
        self.assertEqual(run_tests.МОДУЛИ, (
            "tests", "test_gateway_validation", "test_service_journal_security", "test_telegram_offset",
            "test_telegram_reliability", "test_telegram_security", "test_devassist", "test_review",
        ))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ошибка:
            run_tests._аргументы(["--живые"])
        self.assertEqual(ошибка.exception.code, 2)


class МаршрутСерверов(unittest.TestCase):
    """Какие серверы поднимать под запрос: модель, правила и фильтры."""

    def серверы(self):
        # Готовность серверов должна определяться только фикстурой. Настоящий
        # .env намеренно не читается и не копируется: github выключен пустым
        # значением, а tracker-real получает синтетические реквизиты.
        каталог = tempfile.mkdtemp(prefix="маршрут-серверы-")
        shutil.copy(os.path.join(КОРЕНЬ_ДНЯ, "mcp-servers.json"), каталог)
        pathlib.Path(os.path.join(каталог, ".env")).write_text(
            "TRACKER_TOKEN=synthetic-test-token\n"
            "TRACKER_ORG=synthetic-test-org\n",
            encoding="utf-8")
        try:
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
                return amcp.load(os.path.join(каталог, "mcp-servers.json"))
        finally:
            shutil.rmtree(каталог, ignore_errors=True)

    def маршрутизатор(self, *ответы):
        клиент = _КлиентПоСценарию(*ответы) if ответы else None
        return оркестр_маршрут.Маршрутизатор(self.серверы(), клиент=клиент), клиент

    def test_правила_по_темам_без_модели(self):
        м, _ = self.маршрутизатор()
        маршрут = м.выбрать("Что в работе по задачам трекера?")
        self.assertEqual(маршрут.способ, оркестр_маршрут.ПРАВИЛА)
        self.assertIn("tracker", маршрут.серверы)
        self.assertIn("в запросе есть", маршрут.почему["tracker"])

    def test_модель_выбирает_серверы(self):
        м, клиент = self.маршрутизатор(Reply(
            text='{"серверы": ["tracker", "pipeline"], "почему": {"tracker": "задачи"}}',
            model_key="тест"))
        маршрут = м.выбрать("Собери отчёт по задачам")
        self.assertEqual(маршрут.способ, оркестр_маршрут.МОДЕЛЬ)
        self.assertEqual(маршрут.серверы, ["tracker", "pipeline"])
        self.assertEqual(маршрут.почему["tracker"], "задачи")
        self.assertIn("Серверы:", клиент.запросы[0]["сообщения"][1]["content"])

    def test_правила_добирают_то_что_модель_пропустила(self):
        # Живой замер: на запросе из двух частей дешёвая модель называет один
        # сервер из двух. Недостающий сервер дороже лишнего — флоу без памяти
        # начинает выдумывать вместо того, чтобы искать.
        м, _ = self.маршрутизатор(Reply(text='{"серверы": ["tracker"]}', model_key="тест"))
        маршрут = м.выбрать("Прочитай задачу в трекере и подними, что знает наша память")
        self.assertEqual(маршрут.способ, оркестр_маршрут.ОБА)
        self.assertEqual(set(маршрут.серверы), {"tracker", "pipeline"})
        self.assertIn("в запросе есть", маршрут.почему["pipeline"])

    def test_правила_не_спорят_с_решением_обойтись_без_инструментов(self):
        # В запросе есть слово «postgis» — тема deepwiki. Но модель сказала
        # «инструменты не нужны», и это ответ, а не пропуск.
        м, _ = self.маршрутизатор(Reply(text='{"серверы": []}', model_key="тест"))
        маршрут = м.выбрать("Чем PostGIS отличается от MapServer?")
        self.assertTrue(маршрут.пуст)
        self.assertEqual(маршрут.способ, оркестр_маршрут.ПУСТО)

    def test_сбой_модели_включает_правила(self):
        класс = LLMError

        class Падающий:
            def call(self, *а, **кв):
                raise класс("429 лимит")

        м = оркестр_маршрут.Маршрутизатор(self.серверы(), клиент=Падающий())
        маршрут = м.выбрать("Что в работе по задачам трекера?")
        self.assertEqual(маршрут.способ, оркестр_маршрут.ПРАВИЛА)
        self.assertIn("tracker", маршрут.серверы)
        self.assertIn("429", маршрут.ошибка)

    def test_ответ_не_json_включает_правила(self):
        м, _ = self.маршрутизатор(Reply(text="думаю, нужен трекер", model_key="тест"))
        маршрут = м.выбрать("Что в работе по задачам трекера?")
        self.assertEqual(маршрут.способ, оркестр_маршрут.ПРАВИЛА)
        self.assertIn("не разобран", маршрут.ошибка)

    def test_пустой_выбор_модели_означает_без_инструментов(self):
        м, _ = self.маршрутизатор(Reply(text='{"серверы": []}', model_key="тест"))
        маршрут = м.выбрать("Чем PostGIS отличается от MapServer?")
        self.assertEqual(маршрут.способ, оркестр_маршрут.ПУСТО)
        self.assertTrue(маршрут.пуст)

    def test_сервер_по_просьбе_сам_не_берётся(self):
        м, _ = self.маршрутизатор(Reply(
            text='{"серверы": ["tracker-real"]}', model_key="тест"))
        маршрут = м.выбрать("Что в работе?")
        self.assertNotIn("tracker-real", маршрут.серверы)
        причины = {о["сервер"]: о["почему"] for о in маршрут.отсеяно}
        self.assertIn("по явной просьбе", причины["tracker-real"])

    def test_тема_из_двух_слов_ловится_в_любом_падеже(self):
        # «настоящий трекер» в запросе выглядит как «в настоящем трекере»:
        # целой строкой тема не найдётся никогда, поэтому сравниваются основы.
        self.assertTrue(оркестр_маршрут.совпало("настоящий трекер",
                                                "посмотри в настоящем трекере задачи"))
        self.assertFalse(оркестр_маршрут.совпало("настоящий трекер", "что в трекере?"))
        self.assertTrue(оркестр_маршрут.совпало("задач", "что по задачам?"))

    def test_сервер_по_просьбе_берётся_когда_назван_словами(self):
        м, _ = self.маршрутизатор(Reply(
            text='{"серверы": ["tracker-real"]}', model_key="тест"))
        маршрут = м.выбрать("Посмотри в настоящем трекере задачи")
        self.assertEqual(маршрут.серверы, ["tracker-real"])

    def test_замещающий_сервер_вытесняет_замещаемый(self):
        м, _ = self.маршрутизатор(Reply(
            text='{"серверы": ["tracker", "tracker-real"]}', model_key="тест"))
        маршрут = м.выбрать("Сравни с настоящим трекером")
        self.assertEqual(маршрут.серверы, ["tracker-real"])
        причины = {о["сервер"]: о["почему"] for о in маршрут.отсеяно}
        self.assertIn("заменён сервером", причины["tracker"])

    def test_неготовый_сервер_отсеивается_с_причиной(self):
        м, _ = self.маршрутизатор(Reply(text='{"серверы": ["github"]}', model_key="тест"))
        маршрут = м.выбрать("Что в репозитории?")
        self.assertNotIn("github", маршрут.серверы)
        причины = {о["сервер"]: о["почему"] for о in маршрут.отсеяно}
        self.assertIn("не хватает переменных", причины["github"])

    def test_человек_называет_серверы_сам(self):
        м, клиент = self.маршрутизатор(Reply(text='{"серверы": ["pipeline"]}',
                                             model_key="тест"))
        маршрут = м.выбрать("что угодно", названные=["tracker"])
        self.assertEqual(маршрут.способ, оркестр_маршрут.ЧЕЛОВЕК)
        self.assertEqual(маршрут.серверы, ["tracker"])
        self.assertEqual(клиент.запросы, [])     # модель не звали вовсе

    def test_неизвестное_имя_в_названных_объясняется(self):
        м, _ = self.маршрутизатор()
        маршрут = м.выбрать("что угодно", названные=["выдумка"])
        причины = {о["сервер"]: о["почему"] for о in маршрут.отсеяно}
        self.assertIn("нет в mcp-servers.json", причины["выдумка"])

    def test_описания_серверов_без_секретов(self):
        м, _ = self.маршрутизатор()
        описания = {о["имя"]: о for о in м.описания()}
        self.assertEqual(len(описания), 11)   # День 31: + git
        self.assertTrue(описания["tracker-real"]["по_просьбе"])
        self.assertTrue(описания["pipeline"]["своё"])
        self.assertFalse(описания["github"]["готов"])
        строкой = _json.dumps(описания, ensure_ascii=False)
        self.assertNotIn("Bearer", строкой)

    def test_маршрут_переживает_запись_в_базу(self):
        м, _ = self.маршрутизатор()
        маршрут = м.выбрать("Что в работе по задачам трекера?")
        снова = оркестр_маршрут.Маршрут.from_dict(маршрут.to_dict())
        self.assertEqual(снова.серверы, маршрут.серверы)
        self.assertEqual(снова.способ, маршрут.способ)
        self.assertEqual(снова.почему, маршрут.почему)

    def test_словами_называет_способ_и_серверы(self):
        м, _ = self.маршрутизатор()
        строка = м.выбрать("Что в работе по задачам трекера?").словами()
        self.assertIn("правила", строка)
        self.assertIn("tracker", строка)


class ХранилищеФлоу(unittest.TestCase):
    """Протокол флоу: шаги, пауза на заявке и возвращение к ней."""

    def setUp(self):
        self.хранилище = флоу_хранилище.FlowStore(":memory:")

    def tearDown(self):
        self.хранилище.close()

    def test_флоу_заводится_до_первого_вызова(self):
        запись = self.хранилище.начать("Разбери MIG-2", серверы=["tracker"],
                                       план={"цель": "разбор"})
        self.assertEqual(запись.состояние, флоу_хранилище.ИДЁТ)
        self.assertEqual(запись.цель, "разбор")
        self.assertEqual(self.хранилище.флоу(запись.номер).серверы, ["tracker"])

    def test_пустой_запрос_не_принимается(self):
        with self.assertRaises(флоу_хранилище.FlowStoreError):
            self.хранилище.начать("   ")

    def test_шаг_считается_вызовом_даже_когда_отклонён(self):
        запись = self.хранилище.начать("в")
        self.хранилище.записать_шаг(запись.номер, 1, "tracker__add_comment",
                                    состояние=флоу_хранилище.ОТКЛОНЁН, ок=False,
                                    причина="нет чтения")
        свежая = self.хранилище.флоу(запись.номер)
        self.assertEqual(свежая.вызовов, 1)
        self.assertEqual(свежая.выполненные(), [])
        self.assertEqual(свежая.шаги[0].причина, "нет чтения")

    def test_выполненные_шаги_видны_в_порядке_вызова(self):
        запись = self.хранилище.начать("в")
        for место, имя in enumerate(["tracker__get_issue", "pipeline__search"], 1):
            self.хранилище.записать_шаг(запись.номер, место, имя)
        self.assertEqual(self.хранилище.флоу(запись.номер).выполненные(),
                         ["tracker__get_issue", "pipeline__search"])

    def test_неизвестное_состояние_шага_отвергается(self):
        запись = self.хранилище.начать("в")
        with self.assertRaises(флоу_хранилище.FlowStoreError):
            self.хранилище.записать_шаг(запись.номер, 1, "и", состояние="как-нибудь")

    def test_пауза_хранит_заявку_ожидание_и_переписку(self):
        запись = self.хранилище.начать("в")
        self.хранилище.приостановить(запись.номер, 5,
                                     {"инструмент": "tracker__add_comment", "ид": "c2"},
                                     [{"role": "assistant"}, {"role": "tool"}], секунд=1.5)
        свежая = self.хранилище.флоу(запись.номер)
        self.assertTrue(свежая.ждёт)
        self.assertEqual(свежая.заявка, 5)
        self.assertEqual(свежая.ожидание["ид"], "c2")
        self.assertEqual(len(свежая.цепочка), 2)
        self.assertEqual(свежая.секунд, 1.5)
        self.assertIn("ждёт подтверждения заявки №5", свежая.словами())

    def test_возобновление_стирает_ожидание(self):
        запись = self.хранилище.начать("в")
        self.хранилище.приостановить(запись.номер, 5, {"инструмент": "и"}, [{"role": "tool"}])
        self.хранилище.возобновить(запись.номер)
        свежая = self.хранилище.флоу(запись.номер)
        self.assertEqual(свежая.состояние, флоу_хранилище.ИДЁТ)
        self.assertEqual(свежая.ожидание, {})
        self.assertEqual(свежая.цепочка, [])

    def test_завершение_пишет_итог_и_сверку(self):
        запись = self.хранилище.начать("в")
        готовая = self.хранилище.завершить(запись.номер, флоу_хранилище.ГОТОВ,
                                           итог="сделано", сверка={"по_плану": 2})
        self.assertTrue(готовая.ок)
        self.assertTrue(готовая.закрыт)
        self.assertEqual(готовая.сверка["по_плану"], 2)
        self.assertTrue(готовая.кончен)

    def test_закрыть_флоу_состоянием_ждёт_нельзя(self):
        запись = self.хранилище.начать("в")
        with self.assertRaises(флоу_хранилище.FlowStoreError):
            self.хранилище.завершить(запись.номер, флоу_хранилище.ЖДЁТ)

    def test_ждущие_показываются_отдельно(self):
        первый = self.хранилище.начать("раз")
        второй = self.хранилище.начать("два")
        self.хранилище.приостановить(второй.номер, 1, {"инструмент": "и"}, [])
        self.хранилище.завершить(первый.номер, флоу_хранилище.ГОТОВ)
        self.assertEqual([ф.номер for ф in self.хранилище.ждущие()], [второй.номер])

    def test_счётчики_считают_состояния_и_вызовы(self):
        готовый = self.хранилище.начать("раз")
        self.хранилище.записать_шаг(готовый.номер, 1, "pipeline__search")
        self.хранилище.завершить(готовый.номер, флоу_хранилище.ГОТОВ)
        оборванный = self.хранилище.начать("два")
        self.хранилище.завершить(оборванный.номер, флоу_хранилище.ОБОРВАН)
        цифры = self.хранилище.счётчики()
        self.assertEqual(цифры["флоу"], 2)
        self.assertEqual(цифры["готовых"], 1)
        self.assertEqual(цифры["сбоев"], 1)
        self.assertEqual(цифры["вызовов"], 1)

    def test_список_отдаёт_свежие_первыми(self):
        for текст in ("раз", "два", "три"):
            self.хранилище.начать(текст)
        self.assertEqual([ф.запрос for ф in self.хранилище.список(сколько=2)],
                         ["три", "два"])
        self.assertEqual(self.хранилище.последний().запрос, "три")

    def test_файл_общий_с_планировщиком_и_конвейерами(self):
        каталог = tempfile.mkdtemp(prefix="общая-база-")
        try:
            путь = os.path.join(каталог, "scheduler.db")
            расписание_хранилище = ScheduleStore(путь)
            прогоны = конвейеры_хранилище.PipelineStore(путь)
            флоу = флоу_хранилище.FlowStore(путь)
            флоу.начать("в")
            self.assertEqual(len(флоу.список()), 1)
            self.assertEqual(прогоны.счётчики()["прогонов"], 0)
            расписание_хранилище.close()
            прогоны.close()
            флоу.close()
        finally:
            shutil.rmtree(каталог, ignore_errors=True)


class ИсполнительФлоу(unittest.TestCase):
    """Длинный флоу целиком: план, порядок, пауза, бюджет, сверка."""

    def setUp(self):
        self.хранилище = флоу_хранилище.FlowStore(":memory:")
        self.ящик = _ЯщикОркестра()
        self.заявки: dict[int, dict] = {}

    def tearDown(self):
        self.хранилище.close()

    def оркестр(self, клиент, **поля):
        поля.setdefault("модель", "тест")
        return оркестр_флоу.Оркестр(
            клиент, self.ящик, self.хранилище, порядок_вызовов.загрузить(),
            заявка=self.завести, читать_заявку=self.заявки.get, **поля)

    def завести(self, инструмент, аргументы, зачем):
        номер = len(self.заявки) + 1
        self.заявки[номер] = {"состояние": "ждёт", "результат": {}, "почему": "",
                              "инструмент": инструмент, "аргументы": аргументы}
        return номер

    ОСНОВА = [{"role": "user", "content": "вопрос"}]

    def test_план_составляется_отдельным_кругом_без_инструментов(self):
        клиент = _КлиентПоСценарию(
            _план_ответ("разбор", "tracker__get_issue", "pipeline__search"),
            Reply(text="готово", model_key="тест"))
        флоу = self.оркестр(клиент).запустить("Разбери MIG-2", self.ОСНОВА)
        self.assertIsNone(клиент.запросы[0]["tools"])
        self.assertEqual(флоу.план["цель"], "разбор")
        self.assertEqual([ш["инструмент"] for ш in флоу.план["шаги"]],
                         ["tracker__get_issue", "pipeline__search"])

    def test_план_уходит_в_системное_сообщение_круга_исполнения(self):
        клиент = _КлиентПоСценарию(_план_ответ("ц", "pipeline__search"),
                                   Reply(text="готово", model_key="тест"))
        self.оркестр(клиент).запустить("в", self.ОСНОВА)
        системные = [с for с in клиент.запросы[1]["сообщения"] if с["role"] == "system"]
        self.assertTrue(any("План, который ты сам составил" in с["content"]
                            for с in системные))

    def test_вызовы_идут_по_порядку_и_попадают_в_протокол(self):
        клиент = _КлиентПоСценарию(
            _план_ответ("ц", "tracker__get_issue", "pipeline__search"),
            _ответ_с_вызовами(("c1", "tracker__get_issue", {"key": "MIG-2"})),
            _ответ_с_вызовами(("c2", "pipeline__search", {"query": "миграция"})),
            Reply(text="Готово: задача и память собраны.", model_key="тест"))
        флоу = self.оркестр(клиент).запустить("Разбери MIG-2", self.ОСНОВА)
        self.assertEqual(флоу.состояние, флоу_хранилище.ГОТОВ)
        self.assertEqual([и for и, _ in self.ящик.вызовы],
                         ["tracker__get_issue", "pipeline__search"])
        self.assertEqual(флоу.итог, "Готово: задача и память собраны.")
        self.assertEqual(флоу.сверка["по_плану"], 2)
        self.assertTrue(флоу.сверка["порядок_ок"])

    def test_нарушение_порядка_останавливает_вызов(self):
        клиент = _КлиентПоСценарию(
            _план_ответ("ц", "tracker__get_issue", "tracker__add_comment"),
            _ответ_с_вызовами(("c1", "tracker__add_comment", {"key": "MIG-2", "text": "x"})),
            Reply(text="Сначала прочитаю задачу.", model_key="тест"))
        флоу = self.оркестр(клиент).запустить("Прокомментируй MIG-2", self.ОСНОВА)
        self.assertEqual(self.ящик.вызовы, [])
        шаг = флоу.шаги[0]
        self.assertEqual(шаг.состояние, флоу_хранилище.ОТКЛОНЁН)
        self.assertIn("Сначала нужен вызов", шаг.причина)
        ответ_модели = [с for с in клиент.запросы[-1]["сообщения"] if с["role"] == "tool"][0]
        self.assertIn("нарушен порядок", ответ_модели["content"])
        self.assertIn("tracker__add_comment", флоу.сверка["отклонено"])
        self.assertFalse(флоу.сверка["порядок_ок"])

    def test_меняющий_вызов_останавливает_флоу_на_заявке(self):
        клиент = _КлиентПоСценарию(
            _план_ответ("ц", "tracker__get_issue", "tracker__add_comment"),
            _ответ_с_вызовами(("c1", "tracker__get_issue", {"key": "MIG-2"})),
            _ответ_с_вызовами(("c2", "tracker__add_comment", {"key": "MIG-2", "text": "x"})))
        флоу = self.оркестр(клиент).запустить("Прокомментируй MIG-2", self.ОСНОВА)
        self.assertTrue(флоу.ждёт)
        self.assertEqual(флоу.заявка, 1)
        self.assertEqual(флоу.ожидание["инструмент"], "tracker__add_comment")
        self.assertEqual([и for и, _ in self.ящик.вызовы], ["tracker__get_issue"])
        self.assertEqual(флоу.шаги[-1].состояние, флоу_хранилище.ОЖИДАНИЕ)

    def test_остальные_вызовы_круга_получают_ответ_и_откладываются(self):
        клиент = _КлиентПоСценарию(
            _план_ответ("ц", "tracker__add_comment"),
            _ответ_с_вызовами(("c1", "tracker__list_issues", {}),
                              ("c2", "tracker__add_comment", {"key": "M", "text": "t"}),
                              ("c3", "pipeline__search", {"query": "x"})))
        флоу = self.оркестр(клиент).запустить("в", self.ОСНОВА)
        self.assertTrue(флоу.ждёт)
        отложенные = [ш for ш in флоу.шаги if ш.состояние == флоу_хранилище.ОТЛОЖЕН]
        self.assertEqual([ш.инструмент for ш in отложенные], ["pipeline__search"])
        # На каждую просьбу модели в переписке должен быть ответ роли tool,
        # иначе следующий запрос провалится по формату function calling.
        цепочка = self.хранилище.флоу(флоу.номер).цепочка
        ответы = {с.get("tool_call_id") for с in цепочка if с.get("role") == "tool"}
        self.assertEqual(ответы, {"c1", "c3"})

    def test_подтверждение_продолжает_флоу_с_того_же_места(self):
        клиент = _КлиентПоСценарию(
            _план_ответ("ц", "tracker__get_issue", "tracker__add_comment"),
            _ответ_с_вызовами(("c1", "tracker__get_issue", {"key": "MIG-2"})),
            _ответ_с_вызовами(("c2", "tracker__add_comment", {"key": "MIG-2", "text": "x"})),
            Reply(text="Комментарий добавлен.", model_key="тест"))
        оркестр = self.оркестр(клиент)
        флоу = оркестр.запустить("Прокомментируй MIG-2", self.ОСНОВА)
        self.заявки[1] = {"состояние": "выполнена", "почему": "",
                          "результат": {"текст": "комментарий №6"}}
        продолженный = оркестр.продолжить(флоу.номер, self.ОСНОВА)
        self.assertEqual(продолженный.состояние, флоу_хранилище.ГОТОВ)
        self.assertEqual(продолженный.итог, "Комментарий добавлен.")
        подтверждённый = [ш for ш in продолженный.шаги
                          if ш.состояние == флоу_хранилище.ВЫПОЛНЕН
                          and ш.инструмент == "tracker__add_comment"]
        self.assertEqual(len(подтверждённый), 1)
        self.assertIn("подтверждена заявка", подтверждённый[0].причина)
        self.assertEqual(продолженный.сверка["по_плану"], 2)

    def test_нерешённая_заявка_продолжать_не_даёт(self):
        клиент = _КлиентПоСценарию(
            _план_ответ("ц", "tracker__add_comment"),
            _ответ_с_вызовами(("c1", "tracker__list_issues", {})),
            _ответ_с_вызовами(("c2", "tracker__add_comment", {"key": "M", "text": "t"})))
        оркестр = self.оркестр(клиент)
        флоу = оркестр.запустить("в", self.ОСНОВА)
        with self.assertRaises(оркестр_флоу.ОшибкаФлоу) as ошибка:
            оркестр.продолжить(флоу.номер, self.ОСНОВА)
        self.assertIn("ещё не решена", str(ошибка.exception))

    def test_отклонённая_заявка_даёт_флоу_доиграть(self):
        клиент = _КлиентПоСценарию(
            _план_ответ("ц", "tracker__add_comment"),
            _ответ_с_вызовами(("c1", "tracker__list_issues", {})),
            _ответ_с_вызовами(("c2", "tracker__add_comment", {"key": "M", "text": "t"})),
            Reply(text="Комментарий не добавлен: человек отказал.", model_key="тест"))
        оркестр = self.оркестр(клиент)
        флоу = оркестр.запустить("в", self.ОСНОВА)
        self.заявки[1] = {"состояние": "отклонена", "почему": "статус меняет тимлид",
                          "результат": {}}
        продолженный = оркестр.продолжить(флоу.номер, self.ОСНОВА)
        self.assertEqual(продолженный.состояние, флоу_хранилище.ГОТОВ)
        отказ = [ш for ш in продолженный.шаги if ш.состояние == флоу_хранилище.ОТКАЗАНО]
        self.assertEqual(len(отказ), 1)
        self.assertIn("тимлид", отказ[0].причина)
        последнее = [с for с in клиент.запросы[-1]["сообщения"] if с["role"] == "tool"][-1]
        self.assertIn("тимлид", последнее["content"])

    def test_продолжать_можно_только_ждущий_флоу(self):
        клиент = _КлиентПоСценарию(_план_ответ("ц"), Reply(text="готово", model_key="т"))
        оркестр = self.оркестр(клиент)
        флоу = оркестр.запустить("в", self.ОСНОВА)
        with self.assertRaises(оркестр_флоу.ОшибкаФлоу):
            оркестр.продолжить(флоу.номер, self.ОСНОВА)
        with self.assertRaises(оркестр_флоу.ОшибкаФлоу):
            оркестр.продолжить(999, self.ОСНОВА)

    def test_бюджет_вызовов_закрывает_флоу_честно(self):
        круги = [_план_ответ("ц")]
        круги += [_ответ_с_вызовами((f"c{н}", "pipeline__search", {"query": "x"}))
                  for н in range(5)]
        круги.append(Reply(text="Успел собрать часть.", model_key="тест"))
        флоу = self.оркестр(_КлиентПоСценарию(*круги), вызовов=2).запустить("в", self.ОСНОВА)
        self.assertEqual(флоу.состояние, флоу_хранилище.ОБОРВАН)
        self.assertEqual(флоу.вызовов, 2)
        self.assertIn("бюджет", флоу.ошибка)

    def test_бюджет_кругов_закрывает_флоу(self):
        круги = [_план_ответ("ц")]
        круги += [_ответ_с_вызовами((f"c{н}", "pipeline__search", {})) for н in range(6)]
        круги.append(Reply(text="итог по добытому", model_key="тест"))
        флоу = self.оркестр(_КлиентПоСценарию(*круги), кругов=2, вызовов=99).запустить(
            "в", self.ОСНОВА)
        self.assertEqual(флоу.состояние, флоу_хранилище.ОБОРВАН)
        self.assertIn("круги", флоу.ошибка)

    def test_модель_напоминают_о_несделанных_пунктах_плана(self):
        клиент = _КлиентПоСценарию(
            _план_ответ("ц", "tracker__get_issue", "pipeline__search"),
            Reply(text="Пожалуй, хватит.", model_key="тест"),
            _ответ_с_вызовами(("c1", "tracker__get_issue", {}),
                              ("c2", "pipeline__search", {})),
            Reply(text="Теперь всё.", model_key="тест"))
        флоу = self.оркестр(клиент).запустить("в", self.ОСНОВА)
        напоминание = [с for з in клиент.запросы for с in з["сообщения"]
                       if с["role"] == "system" and "Не сделаны пункты плана" in с["content"]]
        self.assertTrue(напоминание)
        self.assertEqual(флоу.итог, "Теперь всё.")
        self.assertEqual(флоу.сверка["по_плану"], 2)

    def test_напоминание_даётся_только_один_раз(self):
        клиент = _КлиентПоСценарию(
            _план_ответ("ц", "tracker__get_issue"),
            Reply(text="хватит", model_key="тест"),
            Reply(text="правда хватит", model_key="тест"))
        флоу = self.оркестр(клиент).запустить("в", self.ОСНОВА)
        self.assertEqual(флоу.состояние, флоу_хранилище.ГОТОВ)
        self.assertEqual(флоу.итог, "правда хватит")
        self.assertEqual(флоу.сверка["пропущено"], ["tracker__get_issue"])

    def test_неизвестный_инструмент_объясняется_модели(self):
        клиент = _КлиентПоСценарию(
            _план_ответ("ц"),
            _ответ_с_вызовами(("c1", "нет__такого", {})),
            Reply(text="понял", model_key="тест"))
        флоу = self.оркестр(клиент).запустить("в", self.ОСНОВА)
        self.assertEqual(флоу.шаги[0].состояние, флоу_хранилище.ОШИБКА)
        ответ = [с for с in клиент.запросы[-1]["сообщения"] if с["role"] == "tool"][0]
        self.assertIn("Доступны:", ответ["content"])

    def test_сбой_инструмента_не_валит_флоу(self):
        self.ящик.исключение_на = "pipeline__search"
        клиент = _КлиентПоСценарию(
            _план_ответ("ц", "pipeline__search"),
            _ответ_с_вызовами(("c1", "pipeline__search", {})),
            Reply(text="Память недоступна, отвечаю по общему.", model_key="тест"))
        флоу = self.оркестр(клиент).запустить("в", self.ОСНОВА)
        self.assertEqual(флоу.состояние, флоу_хранилище.ГОТОВ)
        self.assertEqual(флоу.шаги[0].состояние, флоу_хранилище.ОШИБКА)
        self.assertIn("не отвечает", флоу.шаги[0].причина)

    def test_флоу_без_инструментов_не_запускается(self):
        self.ящик.инструменты = []
        with self.assertRaises(оркестр_флоу.ОшибкаФлоу) as ошибка:
            self.оркестр(_КлиентПоСценарию()).запустить("в", self.ОСНОВА)
        self.assertIn("без них", str(ошибка.exception))

    def test_пустой_вопрос_не_запускается(self):
        with self.assertRaises(оркестр_флоу.ОшибкаФлоу):
            self.оркестр(_КлиентПоСценарию()).запустить("  ", self.ОСНОВА)

    def test_вызов_сверх_плана_виден_в_сверке(self):
        клиент = _КлиентПоСценарию(
            _план_ответ("ц", "pipeline__search"),
            _ответ_с_вызовами(("c1", "tracker__list_issues", {})),
            _ответ_с_вызовами(("c2", "pipeline__search", {})),
            Reply(text="готово", model_key="тест"))
        флоу = self.оркестр(клиент).запустить("в", self.ОСНОВА)
        self.assertEqual(флоу.сверка["сверх_плана"], ["tracker__list_issues"])
        self.assertEqual(флоу.сверка["по_плану"], 1)
        self.assertTrue(флоу.сверка["порядок_ок"])


class СверкаПланаИФакта(unittest.TestCase):
    """Отдельно — арифметика сверки: она и есть ответ на «корректность порядка»."""

    def сверить(self, план_инструменты, факт, отклонённые=()):
        план = планы.разобрать(
            _json.dumps({"шаги": [{"инструмент": и} for и in план_инструменты]}), [])
        шаги = [флоу_хранилище.ШагФлоу(место=м, инструмент=и,
                                       состояние=флоу_хранилище.ВЫПОЛНЕН, ок=True)
                for м, и in enumerate(факт, 1)]
        шаги += [флоу_хранилище.ШагФлоу(место=99, инструмент=и,
                                        состояние=флоу_хранилище.ОТКЛОНЁН, ок=False)
                 for и in отклонённые]
        return оркестр_флоу.сверить(план, шаги)

    def test_полное_совпадение(self):
        сверка = self.сверить(["a", "b"], ["a", "b"])
        self.assertTrue(сверка.по_плану_целиком)
        self.assertTrue(сверка.порядок_ок)
        self.assertIn("по плану 2 из 2", сверка.словами())

    def test_лишний_вызов_между_шагами_порядок_не_ломает(self):
        сверка = self.сверить(["a", "b"], ["a", "x", "b"])
        self.assertTrue(сверка.порядок_ок)
        self.assertEqual(сверка.сверх_плана, ["x"])

    def test_переставленные_шаги_ломают_порядок(self):
        сверка = self.сверить(["a", "b"], ["b", "a"])
        self.assertFalse(сверка.порядок_ок)

    def test_пропущенный_шаг_виден(self):
        сверка = self.сверить(["a", "b"], ["a"])
        self.assertEqual(сверка.пропущено, ["b"])
        self.assertFalse(сверка.по_плану_целиком)

    def test_отклонённый_по_порядку_вызов_портит_вердикт(self):
        сверка = self.сверить(["a"], ["a"], отклонённые=["c"])
        self.assertFalse(сверка.порядок_ок)
        self.assertEqual(сверка.отклонено, ["c"])

    def test_без_плана_сверка_просто_перечисляет(self):
        сверка = self.сверить([], ["a", "b"])
        self.assertEqual(сверка.в_плане, 0)
        self.assertIn("Плана не было", сверка.словами())


class АгентИОркестр(unittest.TestCase):
    """Агент: маршрут в обычном ответе, флоу, продолжение, состояние."""

    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="оркестр-агент-")
        self.агент = MemoryAgent(base_dir=self.каталог, judge_semantic=False,
                                 require_self_report=False, router_mode="выкл",
                                 seed_project=False,
                                 mcp_config=os.path.join(КОРЕНЬ_ДНЯ, "mcp-servers.json"))
        self.ящик = _ЯщикОркестра()
        self.агент.toolbox = self.ящик

    def tearDown(self):
        self.агент.close()
        shutil.rmtree(self.каталог, ignore_errors=True)

    def модель(self, *ответы):
        клиент = _КлиентПоСценарию(*ответы)
        self.клиент = клиент
        return mock.patch.object(self.агент.client, "call", side_effect=клиент.call)

    def test_названный_человеком_набор_не_пересматривается(self):
        self.агент._серверы_ключом = ["tracker"]
        with self.модель(Reply(text="ответ", model_key="тест")):
            ответ = self.агент.ask("Что в работе?")
        self.assertEqual(ответ.маршрут["способ"], "человек")
        self.assertEqual(ответ.маршрут["серверы"], ["tracker"])
        self.assertIs(self.агент.toolbox, self.ящик)      # ящик не подменён
        self.assertEqual(len(self.клиент.запросы), 1)     # лишнего круга не было

    def test_выключенная_маршрутизация_молчит(self):
        self.агент.маршрутизация = False
        with self.модель(Reply(text="ответ", model_key="тест")):
            ответ = self.агент.ask("Что в работе?")
        self.assertIsNone(ответ.маршрут)

    def test_флоу_проходит_целиком_и_попадает_в_протокол(self):
        with self.модель(
            _план_ответ("разбор", "tracker__get_issue", "pipeline__search"),
            _ответ_с_вызовами(("c1", "tracker__get_issue", {"key": "MIG-2"})),
            _ответ_с_вызовами(("c2", "pipeline__search", {"query": "миграция"})),
            Reply(text="Разобрал.", model_key="тест"),
        ):
            флоу = self.агент.флоу("Разбери MIG-2")
        self.assertEqual(флоу["состояние"], "готов")
        self.assertEqual(флоу["итог"], "Разобрал.")
        self.assertEqual(флоу["сверка"]["по_плану"], 2)
        self.assertEqual([и for и, _ in self.ящик.вызовы],
                         ["tracker__get_issue", "pipeline__search"])
        # Разговор помнит и вопрос, и итог: флоу — это разговор, а не служебный вызов.
        реплики = [з["content"] for з in self.агент.memory.short.all(self.агент.session)]
        self.assertIn("Разбери MIG-2", реплики)
        self.assertIn("Разобрал.", реплики)

    def test_флоу_без_инструментов_объясняет_как_их_включить(self):
        self.агент.toolbox = None
        with self.assertRaises(AgentError) as ошибка:
            self.агент.флоу("Разбери MIG-2")
        self.assertIn("--инструменты", str(ошибка.exception))

    def test_меняющий_шаг_заводит_заявку_агента(self):
        with self.модель(
            _план_ответ("ц", "tracker__get_issue", "tracker__add_comment"),
            _ответ_с_вызовами(("c1", "tracker__get_issue", {"key": "MIG-2"})),
            _ответ_с_вызовами(("c2", "tracker__add_comment", {"key": "MIG-2", "text": "x"})),
        ):
            флоу = self.агент.флоу("Прокомментируй MIG-2")
        self.assertEqual(флоу["состояние"], "ждёт")
        заявки = self.агент.pending_calls()
        self.assertEqual([з["инструмент"] for з in заявки], ["tracker__add_comment"])
        self.assertEqual(флоу["заявка"], заявки[0]["номер"])
        self.assertEqual(self.агент.флоу_по_заявке(заявки[0]["номер"])["номер"],
                         флоу["номер"])

    def test_подтверждение_и_продолжение_доводят_флоу(self):
        with self.модель(
            _план_ответ("ц", "tracker__get_issue", "tracker__add_comment"),
            _ответ_с_вызовами(("c1", "tracker__get_issue", {"key": "MIG-2"})),
            _ответ_с_вызовами(("c2", "tracker__add_comment", {"key": "MIG-2", "text": "x"})),
        ):
            флоу = self.агент.флоу("Прокомментируй MIG-2")
        self.агент.confirm_call(флоу["заявка"])
        self.assertIn("tracker__add_comment", [и for и, _ in self.ящик.вызовы])
        with self.модель(Reply(text="Комментарий добавлен.", model_key="тест")):
            продолженный = self.агент.продолжить_флоу(флоу["номер"])
        self.assertEqual(продолженный["состояние"], "готов")
        self.assertEqual(продолженный["сверка"]["по_плану"], 2)

    def test_продолжить_можно_только_ждущий(self):
        with self.модель(_план_ответ("ц"), Reply(text="всё", model_key="тест")):
            флоу = self.агент.флоу("Просто спроси")
        with self.assertRaises(AgentError) as ошибка:
            self.агент.продолжить_флоу(флоу["номер"])
        self.assertIn("не ждёт", str(ошибка.exception))

    def test_состояние_флоу_собирает_правила_серверы_и_счётчики(self):
        with self.модель(_план_ответ("ц"), Reply(text="всё", model_key="тест")):
            self.агент.флоу("Просто спроси")
        состояние = self.агент.состояние_флоу()
        self.assertEqual(len(состояние["серверы"]), 11)   # День 31: + git
        self.assertEqual(len(состояние["правила"]["правила"]), 4)
        self.assertEqual(состояние["счётчики"]["флоу"], 1)
        self.assertEqual(состояние["бюджет"]["кругов"], оркестр_флоу.ПРЕДЕЛ_КРУГОВ)
        # Ключи не затирают друг друга: у счётчиков и у списка разные имена.
        self.assertIn("флоу", состояние)
        self.assertIsInstance(состояние["флоу"], list)

    def test_маршрут_запроса_показывает_выбор_не_вызывая_инструментов(self):
        with self.модель(Reply(text='{"серверы": ["tracker"], "почему": {"tracker": "задачи"}}',
                               model_key="тест")):
            маршрут = self.агент.маршрут_запроса("Что в работе?")
        self.assertEqual(маршрут["серверы"], ["tracker"])
        self.assertEqual(len(маршрут["все_серверы"]), 11)   # День 31: + git
        self.assertEqual(self.ящик.вызовы, [])

    def test_включение_инструментов_переиспользует_ящик(self):
        self.агент.включить_инструменты(["tracker"])
        первый = self.агент.toolbox
        self.агент.включить_инструменты(["tracker"])
        self.assertIs(self.агент.toolbox, первый)
        self.агент.выключить_инструменты()
        self.assertIsNone(self.агент.toolbox)

    def test_правила_порядка_берутся_рядом_с_файлом_серверов(self):
        self.assertEqual(self.агент.правила_порядка.путь,
                         os.path.join(КОРЕНЬ_ДНЯ, "flow-rules.json"))


class НастройкиОркестровки(unittest.TestCase):
    """Поля Дня 20 в mcp-servers.json и доверие «только_чтение»."""

    def test_поля_маршрутизации_читаются(self):
        серверы = {с.имя: с for с in amcp.load(
            os.path.join(КОРЕНЬ_ДНЯ, "mcp-servers.json"))}
        self.assertTrue(серверы["tracker-real"].по_просьбе)
        self.assertEqual(серверы["tracker-real"].замещает, "tracker")
        self.assertTrue(серверы["tracker"].темы)
        self.assertTrue(серверы["deepwiki"].только_чтение)
        self.assertFalse(серверы["tracker"].только_чтение)

    def test_темы_приводятся_к_нижнему_регистру(self):
        каталог = tempfile.mkdtemp(prefix="серверы-")
        try:
            # Имя сервера — только латиницей: из него складывается имя функции
            # для модели, а там кириллица запрещена.
            путь = _файл_серверов(каталог, {"probe": {
                "command": "${PYTHON}", "args": ["-c", "pass"],
                "темы": ["Трекер", " ЗАДАЧИ "], "по_просьбе": True}})
            сервер = amcp.load(путь)[0]
            self.assertEqual(сервер.темы, ["трекер", "задачи"])
            self.assertTrue(сервер.по_просьбе)
        finally:
            shutil.rmtree(каталог, ignore_errors=True)

    def test_описание_сервера_отдаёт_новые_поля(self):
        сервер = [с for с in amcp.load(os.path.join(КОРЕНЬ_ДНЯ, "mcp-servers.json"))
                  if с.имя == "tracker-real"][0]
        словарь = сервер.to_dict()
        self.assertTrue(словарь["по_просьбе"])
        self.assertEqual(словарь["замещает"], "tracker")
        self.assertIn("темы", словарь)

    def test_одновременное_подключение_не_дублирует_инструменты(self):
        # Страница присылает два изменения настроек подряд (галочка и поле
        # серверов), Flask отвечает в разных потоках — и «открыть» звалось
        # дважды разом. Список инструментов тогда собирался двумя половинами,
        # а провайдер отвечал «Tool names must be unique» на весь запрос.
        ящик = amcp.Toolbox(["agent-state"], os.path.join(КОРЕНЬ_ДНЯ, "mcp-servers.json"))
        try:
            потоки = [_threading.Thread(target=ящик.открыть) for _ in range(4)]
            for поток in потоки:
                поток.start()
            for поток in потоки:
                поток.join()
            имена = [и["function"]["name"] for и in ящик.для_модели()]
            self.assertEqual(len(имена), len(set(имена)), имена)
            self.assertEqual(set(имена), {f"agent-state__{и}" for и in ИНСТРУМЕНТЫ_СВОЕГО})
        finally:
            ящик.close()

    def test_доверенный_сервер_не_требует_заявки(self):
        ящик = amcp.Toolbox([], os.path.join(КОРЕНЬ_ДНЯ, "mcp-servers.json"))
        ящик.доверенные = {"deepwiki"}
        ящик.инструменты = [_Инструмент("deepwiki", "ask_wiki_question", "не заявлено")]
        self.assertFalse(ящик.меняет("deepwiki__ask_wiki_question", {}))
        ящик.доверенные = set()
        self.assertTrue(ящик.меняет("deepwiki__ask_wiki_question", {}))

    def test_файл_проекта_описывает_одиннадцать_серверов(self):
        серверы = amcp.load(os.path.join(КОРЕНЬ_ДНЯ, "mcp-servers.json"))
        self.assertEqual(len(серверы), 11)   # День 31: + git
        self.assertTrue(all(с.описание for с in серверы))
        # У каждого готового сервера есть темы: без них не работает запасной
        # путь маршрутизатора, а он и нужен как раз тогда, когда модель молчит.
        без_тем = [с.имя for с in серверы if not с.темы]
        self.assertEqual(без_тем, [])


class СводкаСФлоу(unittest.TestCase):
    """Сводка Дня 18 считает и длинные флоу — это тоже работа без человека."""

    def test_цифры_и_текст_сводки_знают_про_флоу(self):
        каталог = tempfile.mkdtemp(prefix="сводка-флоу-")
        try:
            путь = os.path.join(каталог, "scheduler.db")
            хранилище = ScheduleStore(путь)
            флоу = флоу_хранилище.FlowStore(путь)
            готовый = флоу.начать("раз")
            флоу.записать_шаг(готовый.номер, 1, "pipeline__search")
            флоу.завершить(готовый.номер, флоу_хранилище.ГОТОВ)
            ждущий = флоу.начать("два")
            флоу.приостановить(ждущий.номер, 3, {"инструмент": "tracker__add_comment"}, [])
            цифры = scheduler_digest.собрать(хранилище, "сутки")
            текст = scheduler_digest.словами(цифры)
            self.assertEqual(цифры["флоу"]["флоу"], 2)
            self.assertEqual(цифры["флоу"]["ждут"], 1)
            self.assertIn("Длинных флоу: 2", текст)
            self.assertIn("Ждут вашего подтверждения флоу: 1", текст)
            флоу.close()
            хранилище.close()
        finally:
            shutil.rmtree(каталог, ignore_errors=True)

    def test_без_таблиц_флоу_сводка_не_падает(self):
        хранилище = ScheduleStore(":memory:")
        цифры = scheduler_digest.собрать(хранилище, "сутки")
        self.assertEqual(цифры["флоу"]["флоу"], 0)
        self.assertNotIn("Длинных флоу", scheduler_digest.словами(цифры))
        хранилище.close()


class КонсольОркестра(unittest.TestCase):
    """Ключи и печать: маршрут, флоу, протокол, продолжение."""

    def test_ключи_дня_разбираются(self):
        import cli

        аргументы = cli.build_parser().parse_args(
            ["--флоу", "Разбери MIG-2", "--кругов-флоу", "5", "--вызовов-флоу", "7"])
        self.assertEqual(аргументы.flow, "Разбери MIG-2")
        self.assertEqual(аргументы.flow_rounds, 5)
        self.assertEqual(аргументы.flow_calls, 7)
        прочие = cli.build_parser().parse_args(
            ["--флоу-продолжить", "2", "--без-маршрутизации"])
        self.assertEqual(прочие.flow_resume, 2)
        self.assertTrue(прочие.no_routing)

    def test_запуск_флоу_считается_действием(self):
        import cli

        разбор = cli.build_parser()
        self.assertTrue(cli.меняет_состояние(разбор.parse_args(["--флоу", "в"])))
        self.assertTrue(cli.меняет_состояние(разбор.parse_args(["--флоу-продолжить", "1"])))
        # Показывающие ключи диалог не открывают, но и действием не считаются.
        self.assertFalse(cli.меняет_состояние(разбор.parse_args(["--флоу-список"])))

    def test_протокол_флоу_печатает_план_шаги_и_сверку(self):
        import cli

        флоу = {"номер": 3, "состояние": "готов", "секунд": 2.5, "кругов": 3, "вызовов": 2,
                "запрос": "Разбери MIG-2",
                "маршрут": {"способ": "модель", "серверы": ["tracker"],
                            "почему": {"tracker": "задачи"}, "отсеяно": []},
                "план": {"цель": "разбор", "шаги": [{"инструмент": "tracker__get_issue"}],
                         "замечания": []},
                "шаги": [{"место": 1, "инструмент": "tracker__get_issue", "состояние": "выполнен",
                          "по_плану": 1, "зачем": "прочитать", "причина": "",
                          "текст": '{"ключ":\n "MIG-2"}'}],
                "сверка": {"словами": "по плану 1 из 1; порядок соблюдён"},
                "итог": "готово"}
        печать = _io.StringIO()
        with _contextlib.redirect_stdout(печать):
            cli.напечатать_флоу(флоу)
        текст = печать.getvalue()
        self.assertIn("Флоу №3 — готов", текст)
        self.assertIn("Маршрут (модель): tracker", текст)
        self.assertIn("план: разбор", текст)
        self.assertIn("по плану 1", текст)
        self.assertIn("сверка: по плану 1 из 1", текст)
        # Ответ инструмента — в одну строку, иначе протокол нечитаем.
        self.assertIn('{"ключ": "MIG-2"}', текст)

    def test_ждущий_флоу_подсказывает_команды(self):
        import cli

        печать = _io.StringIO()
        with _contextlib.redirect_stdout(печать):
            cli.напечатать_флоу({"номер": 4, "состояние": "ждёт", "секунд": 1, "кругов": 2,
                                 "вызовов": 1, "запрос": "в", "заявка": 7,
                                 "ожидание": {"инструмент": "tracker__add_comment"},
                                 "план": {}, "шаги": [], "сверка": {}, "итог": ""})
        текст = печать.getvalue()
        self.assertIn("--подтвердить 7", текст)
        self.assertIn("--флоу-продолжить 4", текст)

    def test_маршрут_виден_под_обычным_ответом(self):
        import cli

        from agent.agent import Answer

        ответ = Answer(text="готово", маршрут={"способ": "модель", "серверы": ["tracker"],
                                               "почему": {}, "отсеяно": []})
        печать = _io.StringIO()
        with _contextlib.redirect_stdout(печать):
            cli.показать_вызовы(ответ)
        self.assertIn("↳ маршрут (модель): tracker", печать.getvalue())

    def test_маршрут_печатает_отсеянные_серверы(self):
        import cli

        печать = _io.StringIO()
        with _contextlib.redirect_stdout(печать):
            cli.напечатать_маршрут({"способ": "модель", "серверы": ["tracker"],
                                    "почему": {"tracker": "задачи"},
                                    "отсеяно": [{"сервер": "github", "почему": "нет токена"}]})
        текст = печать.getvalue()
        self.assertIn("(не взят) github — нет токена", текст)

    def test_пустая_история_флоу_подсказывает_запуск(self):
        import cli

        class Пустой:
            def флоу_список(self, сколько=10):
                return []

        печать = _io.StringIO()
        with _contextlib.redirect_stdout(печать):
            код = cli.показать_флоу_список(Пустой())
        self.assertEqual(код, 0)
        self.assertIn("--флоу", печать.getvalue())

    def test_нет_такого_флоу_это_ошибка_команды(self):
        import cli

        class Пустой:
            def показать_флоу(self, номер):
                return None

        печать = _io.StringIO()
        with _contextlib.redirect_stdout(печать):
            код = cli.показать_один_флоу(Пустой(), 9)
        self.assertEqual(код, 1)
        self.assertIn("Флоу №9 нет", печать.getvalue())

    def test_справка_рассказывает_про_оркестровку(self):
        import cli

        разбор = cli.build_parser()
        текст = разбор.format_help()
        self.assertIn("--флоу", текст)
        self.assertIn("--маршрут", текст)
        self.assertIn("flow-rules.json", разбор.epilog)
        self.assertIn("оркестровка нескольких серверов", разбор.epilog)


class ПоказВБраузере(unittest.TestCase):
    """Набор для показа дня: сценарий расширению, стенд, запись и режим «показ».

    Проверяется не поведение модели, а состоятельность набора: что сцены водителя
    опираются на элементы, которые на странице действительно есть, что скрипты
    запускаются и ссылаются друг на друга теми же ключами, которыми описаны, и
    что запись по умолчанию берёт окно браузера, а не весь экран. Ошибка здесь
    обнаруживается иначе только живым прогоном на пять минут.
    """

    @classmethod
    def setUpClass(cls) -> None:
        читать = lambda *части: pathlib.Path(
            os.path.join(КОРЕНЬ_ДНЯ, *части)).read_text(encoding="utf-8")
        cls.водитель = читать("browser.mjs")
        cls.показ = cls.водитель.split("if (РЕЖИМ === 'показ')")[1].split(
            "if (РЕЖИМ === 'переходы')")[0]
        cls.общее = (cls.водитель.split("// --- День 21: страница индекса документов")[1].split(
            "if (РЕЖИМ === 'индекс')")[0]
            + cls.водитель.split("// --- День 26: локальная модель")[1].split("if (РЕЖИМ === 'офлайн')")[0]
            + cls.водитель.split("// --- День 28: локальный RAG рядом с облаком")[1].split(
                "if (РЕЖИМ === 'локальный-rag')")[0]
            + cls.водитель.split("// --- День 29: оптимизация под задачу")[1].split(
                "if (РЕЖИМ === 'оптимизация')")[0]
            + cls.водитель.split("// --- День 30: приватный AI-сервис")[1].split(
                "if (РЕЖИМ === 'сервис')")[0])
        cls.страница = читать("templates", "index.html")
        cls.страница_индекса = читать("templates", "rag.html")
        cls.страница_чата = читать("templates", "chat.html")
        cls.страница_локальной = читать("templates", "local.html")
        cls.сервис_админ = читать("templates", "service_admin.html")
        cls.сервис_чат = читать("templates", "service_chat.html")
        cls.сценарий = читать("DEMO-CHROME.md")
        cls.стенд = читать("demo_stand.sh")
        cls.запись = читать("record_demo.sh")
        cls.показать = читать("demo_show.sh")

    def test_восемь_сцен_с_подписями(self):
        for номер in range(1, 9):
            self.assertIn(f"Сцена {номер}", self.показ)
            self.assertIn(f"Сцена {номер}", self.сценарий)
        self.assertIn("подпись-показа", self.показ)
        self.assertIn("process.env.PAUSE", self.показ)      # темп задаётся снаружи
        self.assertIn("process.exit(0)", self.показ)        # показ не проверка

    def test_сцены_опираются_на_настоящие_элементы_страницы(self):
        # Самая дорогая ошибка показа — опечатка в идентификаторе: водитель
        # молча ничего не нажимает, а видно это только на пятой минуте прогона.
        сцены = self.показ + self.общее
        # Полоса офлайна — общая для страниц (День 27).
        полоса = pathlib.Path(os.path.join(КОРЕНЬ_ДНЯ, "templates", "_offline.html")).read_text(encoding="utf-8")
        for узел in ("офлайн-полоса", "офлайн-тумблер", "сеть-внешних", "сеть-локальных"):
            self.assertIn(узел, сцены, f"сцены не пользуются «{узел}»")
            self.assertIn(узел, полоса, f"в полосе офлайна нет «{узел}»")
        # День 30: показ идёт по страницам сервиса /admin и /chat.
        for узел in ("служба", "лимиты", "клиенты-карта", "нагрузка-карта", "нагрузка", "нагрузка-8"):
            self.assertIn(f"'#{узел}'" if узел != "нагрузка-8" else "'нагрузка-8'", self.показ, f"сцены не пользуются «{узел}»")
            self.assertIn(f'id="{узел}"', self.сервис_админ, f"на /admin нет «{узел}»")
        for узел in ("ключ", "войти"):
            self.assertIn(f"'{узел}'", сцены, f"помощники Дня 30 не пользуются «{узел}»")
            self.assertIn(f'id="{узел}"', self.сервис_админ, f"на /admin нет «{узел}»")
        for узел in ("ключ", "вопрос", "отправить", "статус", "режим-чат", "режим-документы"):
            self.assertIn(f'id="{узел}"', self.сервис_чат, f"на /chat сервиса нет «{узел}»")
        self.assertIn("'режим-' + режим", сцены, "режим чата выбирается по id «режим-…»")
        for узел in ("последний-ответ", "последний-отказ"):
            self.assertIn(узел, сцены)
            self.assertIn(f"id = '{узел}'", self.сервис_чат, f"страница чата не размечает «{узел}»")
        for функция in ("function реплика(", "function отказ("):
            self.assertIn(функция, self.сервис_чат, "лимит на странице показывает ответ функциями страницы")
        # Итог нагрузки в подписи — по данным: «все прошли при повторе» только при ошибках 0.
        self.assertIn("п7.ошибок ? `не дождались ответа", self.показ)
        self.assertNotIn("все прошли при повторе; задержка", self.показ)
        for класс in ("'подпись'", "'источник'", "'цитата'"):
            self.assertIn(класс.strip("'"), self.сервис_чат)
        self.assertIn("SERVICE_KEYS", self.показ + сцены, "ключи показа — из файла, не из кода")
        # День 29: карточка «Оптимизация под задачу» на /local — проверка режима «оптимизация».
        for узел in ("опт-конфигурации", "опт-переменные", "опт-сервер-стоп", "опт-сервер", "опт-свой",
                     "опт-вопрос", "опт-а", "опт-б"):
            self.assertIn(узел, сцены, f"помощники Дня 29 не пользуются «{узел}»")
            self.assertIn(f'id="{узел}"', self.страница_локальной, f"на /local нет «{узел}»")
        # Эти узлы рисует сам скрипт карточки — по ним водитель ждёт итога.
        self.assertIn("ход-конф", сцены, "сцены не пользуются «ход-конф»")
        self.assertIn("ход-конф", self.страница_локальной, "скрипт /local не рисует «ход-конф»")
        self.assertIn("dataset.конф", сцены, "помощники не читают data-конф колонки")
        self.assertIn("data-конф", self.страница_локальной, "скрипт /local не размечает колонки data-конф")
        # День 28: страница /rag осталась — её проверяют режимы «локальный-rag» и «сравнение».
        for узел in ("неделя-6", "локальный-rag-карточка", "лрг-вопрос", "лрг-спросить", "лрг-рядом",
                     "сравнение-карточка"):
            self.assertIn(f'id="{узел}"', self.страница_индекса, f"на /rag нет «{узел}»")
        for узел in ("лрг-набор", "лрг-ход", "лрг-ответ", "лрг-спросить"):
            self.assertIn(узел, сцены, f"общие помощники не пользуются «{узел}»")
        self.assertIn("process.env.MARKS", self.показ, "метки ожиданий — для ускоренной копии")
        # День 26 остаётся проверкой своей страницы (режим «локально»).
        for узел in ("лок-спросить", "лок-вопрос", "лок-режим", "лок-коротко", "лок-ответы",
                     "модели-ollama", "доступ-итог", "доступ-вывод"):
            self.assertIn(узел, self.общее, f"проверка /local не пользуется «{узел}»")
            self.assertIn(f'id="{узел}"', self.страница_локальной, f"на /local нет «{узел}»")
        # Чат Дня 25 остаётся проверкой своей страницы.
        for узел in ("чат-лента", "чат-реплика", "память-задачи", "сценарии-запустить"):
            self.assertIn(f'id="{узел}"', self.страница_чата, f"на /chat нет «{узел}»")

    def test_стенд_показывает_копию_индекса(self):
        # Показ пересобирает индекс: делать это на рабочем index/ незачем.
        self.assertIn('RAG_DIR="$DEMO_INDEX"', self.стенд)
        self.assertIn('cp -r "$INDEX" "$DEMO_INDEX"', self.стенд)
        # Настройки второго этапа — копия рядом с индексом (День 23).
        self.assertIn('RAG_SETTINGS="$DEMO_INDEX/rag-settings.json"', self.стенд)

    def test_скрипты_запускаются_и_согласованы(self):
        for имя in ("demo_stand.sh", "record_demo.sh", "demo_show.sh"):
            путь = os.path.join(КОРЕНЬ_ДНЯ, имя)
            self.assertTrue(os.access(путь, os.X_OK), f"{имя} не исполняемый")
            готово = _subprocess.run(["bash", "-n", путь], capture_output=True, text=True)
            self.assertEqual(готово.returncode, 0, готово.stderr)
            справка = _subprocess.run(["bash", путь, "--help"],
                                      capture_output=True, text=True, timeout=60)
            self.assertEqual(справка.returncode, 0, справка.stderr)
            self.assertIn("Запуск:", справка.stdout)
            self.assertNotIn("set -u", справка.stdout, f"справка {имя} задевает код")
        self.assertIn("PORT=5001", self.стенд)              # 5000 — сервер человека
        self.assertIn("DEMO_KEY", self.стенд)
        # День 31: показ пишется кадрами самого Chrome (CDP screencast) — чужое окно не попадёт.
        self.assertIn('RECORD_CDP="$FILE"', self.показать)
        # Ключи вводятся в поля-пароли: окно «Сохранить пароль?» закрывало /admin в кадре.
        self.assertIn('"credentials_enable_service": false', self.показать)

    def test_запись_берёт_окно_а_не_экран(self):
        # В кадре должно быть только окно браузера: рабочий стол и окно редактора
        # к работе агента не относятся.
        self.assertIn("_NET_CLIENT_LIST", self.запись)      # ищем окно у X
        # Вокруг окна Chrome лежит прозрачная тень: в геометрии X она есть, на
        # экране её нет. Без поправки по краям кадра видна полоска рабочего
        # стола — нашлось разбором первого кадра записи Дня 19.
        self.assertIn("_GTK_FRAME_EXTENTS", self.запись)
        self.assertIn("--pid", self.запись)
        self.assertIn("--screen", self.запись)              # весь экран — только явно
        self.assertIn("% 2", self.запись)                   # чётные размеры для yuv420p
        self.assertIn('WINDOW="Google Chrome"', self.запись)
        # Незакрытый mp4 уже случался: ffmpeg должен получать именно INT.
        self.assertIn('kill -INT "$FF"', self.запись)

    def test_записи_не_попадают_в_репозиторий(self):
        игнор = pathlib.Path(os.path.join(КОРЕНЬ_ДНЯ, ".gitignore")).read_text(
            encoding="utf-8")
        for правило in ("/demo/", "/demo-memory/", "/demo-reports/", "/demo-index/", "/index/"):
            self.assertIn(правило, игнор)


# --- живые проверки -----------------------------------------------------------

# --- День 21: индекс документов -----------------------------------------------

from agent import rag  # noqa: E402
from agent.rag import chunking as rag_chunking, embed as rag_embed  # noqa: E402
from agent.rag import evaluate as rag_evaluate, extract as rag_extract  # noqa: E402
from agent.rag import store as rag_store  # noqa: E402
import rag_server  # noqa: E402

ДОКУМЕНТЫ_ОБРАЗЦА = os.path.join(КОРЕНЬ_ДНЯ, "DocsSample")
ДОГОВОР_VPN = os.path.join(ДОКУМЕНТЫ_ОБРАЗЦА, "Ростелеком VPN ВОГ396658 от 23.12.2024.pdf")

_ДЛИННЫЙ_АБЗАЦ = ("Сборка проекта идёт в конвейере непрерывной интеграции, и каждый шаг "
                  "пишет журнал, который хранится тридцать дней. ") * 12


def _набор_документов(каталог: str) -> str:
    """Маленький набор всех поддержанных видов, кроме PDF: Markdown, текст, код, таблица."""
    os.makedirs(каталог, exist_ok=True)
    with open(os.path.join(каталог, "политика.md"), "w", encoding="utf-8") as f:
        f.write("# Политика код-ревью\n\nВводный абзац о том, зачем нужно ревью.\n\n"
                "## Сроки\n\nРевью занимает не более 24 часов с момента запроса.\n\n"
                "## Апрувы\n\nДо мержа нужно минимум 2 апрува от владельцев кода.\n\n"
                "## Сборка\n\n" + "\n\n".join([_ДЛИННЫЙ_АБЗАЦ] * 4) + "\n")
    with open(os.path.join(каталог, "заметки.txt"), "w", encoding="utf-8") as f:
        f.write("Первый абзац заметок про отпуск.\n\nВторой абзац: отпуск согласуют за две недели.\n")
    with open(os.path.join(каталог, "модуль.py"), "w", encoding="utf-8") as f:
        f.write('"""Модуль."""\nimport os\n\n\ndef посчитать(x):\n    return x * 2\n\n\n'
                'class Склад:\n    def положить(self):\n        pass\n')
    from openpyxl import Workbook
    книга = Workbook()
    лист = книга.active
    лист.title = "Лицензии"
    лист.append(["Лицензии на ПО 2026", None, None, None, None, None, None])
    лист.append(["N", "Организация", "Кол-во", "Сумма", "Январь", "Февраль", "Март"])
    лист.append([1, "Автотранс", 30, 26731.5, 100, 100, 100])
    лист.append([2, "Аннарайгаз", 10, 8910.5, 50, 60, 70])
    второй = книга.create_sheet("Итог")
    второй.append(["Статья", "Сумма"])
    второй.append(["Сопровождение КонсультантПлюс", 676.36])
    книга.save(os.path.join(каталог, "бюджет.xlsx"))
    # Мусор, который должен остаться незамеченным.
    open(os.path.join(каталог, "~$бюджет.xlsx"), "wb").close()
    open(os.path.join(каталог, "картинка.png"), "wb").close()
    return каталог


class ИзвлечениеДокументов(unittest.TestCase):
    """agent/rag/extract.py: документ → блоки с разделом и страницей."""

    @classmethod
    def setUpClass(cls):
        cls.каталог = _набор_документов(tempfile.mkdtemp(prefix="rag-документы-"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.каталог, ignore_errors=True)

    def извлечь(self, имя: str):
        return rag_extract.извлечь(os.path.join(self.каталог, имя), self.каталог)

    def test_находит_поддержанные_файлы_и_пропускает_замки_офиса(self):
        имена = [os.path.basename(п) for п in rag_extract.найти_документы(self.каталог)]
        self.assertEqual(sorted(имена), ["бюджет.xlsx", "заметки.txt", "модуль.py", "политика.md"])

    def test_нет_каталога_внятная_ошибка(self):
        with self.assertRaisesRegex(rag_extract.ОшибкаИзвлечения, "не найден"):
            rag_extract.найти_документы(os.path.join(self.каталог, "нет-такого"))

    def test_markdown_заголовки_задают_путь_раздела_и_название(self):
        д = self.извлечь("политика.md")
        self.assertEqual(д.название, "Политика код-ревью")
        разделы = {б.раздел for б in д.блоки}
        self.assertIn(("Политика код-ревью", "Сроки"), разделы)
        self.assertIn(("Политика код-ревью", "Апрувы"), разделы)
        сроки = next(б for б in д.блоки if "24 часов" in б.текст)
        self.assertEqual(сроки.раздел, ("Политика код-ревью", "Сроки"))
        self.assertEqual(д.источник, "политика.md")

    def test_код_делится_по_функциям_и_классам(self):
        д = self.извлечь("модуль.py")
        self.assertEqual([б.раздел[0] for б in д.блоки], ["модуль", "def посчитать", "class Склад"])

    def test_таблица_строка_становится_парами_шапка_значение(self):
        д = self.извлечь("бюджет.xlsx")
        self.assertEqual(д.страниц, 2)
        заголовок = д.блоки[0]
        self.assertEqual(заголовок.вид, "заголовок")
        self.assertIn("Лицензии на ПО 2026", заголовок.текст)
        строка = next(б for б in д.блоки if "Автотранс" in б.текст)
        self.assertEqual(строка.раздел, ("Лист «Лицензии»",))
        self.assertIn("Организация: Автотранс", строка.текст)
        self.assertIn("Сумма: 26731.5", строка.текст)
        # Три одинаковых месяца подряд — одной парой, а не тремя.
        self.assertIn("Январь…Март (×3): 100", строка.текст)
        разные = next(б for б in д.блоки if "Аннарайгаз" in б.текст)
        self.assertIn("Январь: 50; Февраль: 60; Март: 70", разные.текст)
        итог = next(б for б in д.блоки if "Консультант" in б.текст)
        self.assertEqual(итог.страница, 2)

    def test_разметка_договора_разделы_пункты_и_приложения(self):
        строки = [
            "Договор об оказании услуг связи № 1",
            "1. Предмет Договора",
            "1.1. Оператор обязуется оказывать услуги связи",
            "в соответствии с лицензией.",
            "1.2. Абонент оплачивает услуги.",
            "3. Порядок расчетов",
            "3.5. Срок оплаты: не позднее 20 числа",
            "- первый пункт перечня",
            "Приложение № 2",
            "1. Оператор начал предоставление Услуги, а Абонент начал пользование",
        ]
        блоки = rag_extract._Разметка().разметить(строки, 4, False)
        пункт = next(б for б in блоки if б.пункт == "1.1")
        self.assertEqual(пункт.текст, "1.1. Оператор обязуется оказывать услуги связи "
                                      "в соответствии с лицензией.")
        self.assertEqual(пункт.раздел, ("1. Предмет Договора",))
        self.assertEqual(пункт.страница, 4)
        срок = next(б for б in блоки if "20 числа" in б.текст)
        self.assertEqual((срок.раздел, срок.пункт), (("3. Порядок расчетов",), "3.5"))
        перечень = next(б for б in блоки if б.текст.startswith("- первый"))
        self.assertEqual(перечень.раздел, ("3. Порядок расчетов",))
        # Приложение сбрасывает раздел, а длинная фраза с запятой — не заголовок.
        последний = блоки[-1]
        self.assertEqual(последний.раздел, ("Приложение № 2",))
        self.assertEqual(последний.вид, "текст")

    def test_скан_номер_пункта_отдельной_строкой_приклеивается_к_тексту(self):
        блоки = rag_extract._Разметка().разметить(
            ["4.13.", "", "Также Заказчик обязуется", "в срок.", "", "4.14. Другое"], 3, True)
        self.assertEqual([(б.пункт, б.текст) for б in блоки],
                         [("4.13", "4.13. Также Заказчик обязуется в срок."),
                          ("4.14", "4.14. Другое")])
        self.assertTrue(all(б.распознан for б in блоки))

    def test_колонтитулы_с_половины_страниц_вычищаются(self):
        страницы = [["Электронный документ подписан ЭП", f"Текст страницы {i} про договор"]
                    for i in range(6)]
        шум = rag_extract._колонтитулы(страницы)
        self.assertEqual(шум, {"электронный документ подписан эп"})
        self.assertEqual(rag_extract._колонтитулы(страницы[:2]), set())

    def test_без_tesseract_ocr_недоступен_с_объяснением(self):
        with mock.patch("shutil.which", return_value=None):
            можно, почему = rag_extract.ocr_доступен()
        self.assertFalse(можно)
        self.assertIn("tesseract", почему)

    def test_скан_без_ocr_помечается_замечанием_а_не_молчит(self):
        pdf = os.path.join(self.каталог, "скан.pdf")
        open(pdf, "wb").close()
        try:
            with mock.patch.object(rag_extract, "_страниц_pdf", return_value=2), \
                 mock.patch.object(rag_extract, "_текст_страницы", return_value="  3 "):
                д = rag_extract.извлечь(pdf, self.каталог, ocr=False)
        finally:
            os.remove(pdf)
        self.assertEqual(д.блоки, [])
        self.assertTrue(any("без текстового слоя страниц: 2 из 2" in з for з in д.замечания),
                        д.замечания)

    def test_распознанная_страница_берётся_из_кэша(self):
        кэш = tempfile.mkdtemp(prefix="ocr-кэш-")
        try:
            sha1 = "ab" * 20
            with open(os.path.join(кэш, f"{sha1[:16]}-003.txt"), "w", encoding="utf-8") as f:
                f.write("распознано раньше")
            with mock.patch("subprocess.run", side_effect=AssertionError("OCR не нужен")):
                текст = rag_extract._распознать("/нет/файла.pdf", 3, sha1, кэш)
            self.assertEqual(текст, "распознано раньше")
        finally:
            shutil.rmtree(кэш, ignore_errors=True)

    @unittest.skipUnless(os.path.exists(ДОГОВОР_VPN) and shutil.which("pdftotext"),
                         "нужен договор из DocsSample и poppler")
    def test_настоящий_договор_разобран_на_разделы(self):
        д = rag_extract.извлечь(ДОГОВОР_VPN, ДОКУМЕНТЫ_ОБРАЗЦА, ocr=False)
        self.assertTrue(д.название.startswith("Договор об оказании телематических услуг связи"))
        разделы = {б.раздел for б in д.блоки}
        self.assertIn(("3. Порядок расчетов",), разделы)
        self.assertIn(("Приложение № 4",), разделы)
        self.assertFalse(any("Электронный документ подписан" in б.текст for б in д.блоки))
        срок = next(б for б in д.блоки if "не позднее 20 числа" in б.текст)
        self.assertEqual((срок.пункт, срок.страница), ("3.5", 4))


class СтратегииРазбиения(unittest.TestCase):
    """agent/rag/chunking.py: «фикс» по токенам с перекрытием, «структура» по разделам."""

    @classmethod
    def setUpClass(cls):
        cls.каталог = _набор_документов(tempfile.mkdtemp(prefix="rag-чанки-"))
        cls.политика = rag_extract.извлечь(os.path.join(cls.каталог, "политика.md"), cls.каталог)
        cls.бюджет = rag_extract.извлечь(os.path.join(cls.каталог, "бюджет.xlsx"), cls.каталог)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.каталог, ignore_errors=True)

    def test_фикс_держит_размер_и_перекрывается(self):
        чанки = rag.нарезать(self.политика, "фикс", размер=60, перекрытие=15)
        self.assertGreater(len(чанки), 3)
        for ч in чанки:
            self.assertLessEqual(ч.токенов, 60 + 5, ч.текст)
        for до, после in zip(чанки, чанки[1:]):
            начало = " ".join(после.текст.split()[:4])
            self.assertIn(начало, до.текст, "следующий чанк должен начинаться внутри предыдущего")
            self.assertFalse(до.текст.startswith(начало), "и всё же сдвигаться вперёд")
        # Ни одно слово документа не потеряно.
        все = set(" ".join(ч.текст for ч in чанки).split())
        self.assertEqual(все, set(self.политика.текст.split()))

    def test_фикс_не_знает_структуры_и_режет_поперёк_разделов(self):
        чанки = rag.нарезать(self.политика, "фикс", размер=60, перекрытие=10)
        self.assertTrue(any(len(ч.разделы) > 1 for ч in чанки))
        self.assertTrue(all(ч.контекст == "" for ч in чанки))

    def test_фикс_отказывает_при_перекрытии_не_меньше_размера(self):
        with self.assertRaisesRegex(ValueError, "Перекрытие"):
            rag.нарезать(self.политика, "фикс", размер=50, перекрытие=50)

    def test_структура_не_смешивает_разделы_и_делит_длинный_по_абзацам(self):
        чанки = rag.нарезать(self.политика, "структура", макс=120, мин=20)
        for ч in чанки:
            self.assertLessEqual(ч.токенов, 120, ч.текст)
            верх = {р.split(" › ")[0] for р in ч.разделы}
            self.assertEqual(len(верх), 1)
        сборка = [ч for ч in чанки if ч.раздел.endswith("Сборка")]
        self.assertGreater(len(сборка), 1, "длинный раздел должен разделиться")
        for ч in сборка:
            # Граница — конец предложения, а не середина слова.
            self.assertTrue(ч.текст.startswith("Сборка"), ч.текст[:40])
            self.assertTrue(ч.текст.rstrip().endswith("дней."), ч.текст[-40:])
            self.assertEqual(ч.контекст, "Политика код-ревью › Политика код-ревью › Сборка")
            self.assertTrue(ч.текст_для_вектора.startswith(ч.контекст))

    def test_структура_склеивает_крошечные_разделы(self):
        чанки = rag.нарезать(self.политика, "структура", макс=700, мин=120)
        первый = чанки[0]
        self.assertIn("24 часов", первый.текст)
        self.assertIn("2 апрува", первый.текст)
        self.assertGreater(len(первый.разделы), 1)

    def test_таблица_структура_держит_лист_целиком(self):
        чанки = rag.нарезать(self.бюджет, "структура")
        self.assertEqual([ч.раздел for ч in чанки], ["Лист «Лицензии»", "Лист «Итог»"])
        self.assertEqual([ч.страницы for ч in чанки], ["1", "2"])

    def test_chunk_id_латиницей_стабилен_и_уникален(self):
        первый = rag.нарезать(self.политика, "структура")
        второй = rag.нарезать(self.политика, "структура")
        self.assertEqual([ч.chunk_id for ч in первый], [ч.chunk_id for ч in второй])
        фикс = rag.нарезать(self.политика, "фикс")
        все = [ч.chunk_id for ч in первый + фикс]
        self.assertEqual(len(все), len(set(все)))
        for ид in все:
            self.assertRegex(ид, r"^(fixed|struct)-[0-9a-f]{8}-\d{4}$")

    def test_паспорт_несёт_всё_для_ссылки(self):
        ч = rag.нарезать(self.политика, "структура")[0]
        паспорт = ч.паспорт()
        for поле in ("chunk_id", "source", "title", "section", "pages", "strategy", "tokens"):
            self.assertIn(поле, паспорт)
        self.assertEqual(паспорт["source"], "политика.md")
        self.assertEqual(паспорт["title"], "Политика код-ревью")

    def test_неизвестная_стратегия(self):
        with self.assertRaisesRegex(ValueError, "Нет стратегии"):
            rag.нарезать(self.политика, "по-смыслу")


class _ОтветOllama:
    def __init__(self, код: int, данные: dict):
        self.status_code = код
        self._данные = данные
        self.text = _json.dumps(данные)

    def json(self):
        return self._данные

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("ошибка", request=None, response=None)


class Эмбеддеры(unittest.TestCase):
    """agent/rag/embed.py: хэш без модели и Ollama по HTTP."""

    def test_хэш_детерминирован_нормирован_и_различает_темы(self):
        э = rag_embed.ЭмбеддерХэш()
        а, б, в = э.векторы(["срок оплаты услуг связи", "срок оплаты услуг", "лицензии на антивирус"])
        self.assertEqual(а, э.векторы(["срок оплаты услуг связи"])[0])
        self.assertAlmostEqual(sum(x * x for x in а), 1.0, places=5)
        близко = sum(x * y for x, y in zip(а, б))
        далеко = sum(x * y for x, y in zip(а, в))
        self.assertGreater(близко, далеко)

    def test_имена_эмбеддеров(self):
        self.assertIsInstance(rag.создать_эмбеддер("хэш"), rag_embed.ЭмбеддерХэш)
        оллама = rag.создать_эмбеддер("ollama:nomic-embed-text")
        self.assertEqual(оллама.имя, "ollama:nomic-embed-text")
        self.assertEqual(rag.создать_эмбеддер("ollama").имя, "ollama:bge-m3")
        with self.assertRaisesRegex(rag.ОшибкаЭмбеддера, "Нет эмбеддера"):
            rag.создать_эмбеддер("word2vec")

    def test_ollama_пачками_и_с_нормировкой(self):
        пачки = []

        def post(адрес, json, timeout):
            пачки.append(len(json["input"]))
            self.assertTrue(адрес.endswith("/api/embed"))
            self.assertEqual(json["model"], "bge-m3")
            return _ОтветOllama(200, {"embeddings": [[3.0, 4.0]] * len(json["input"])})

        э = rag_embed.ЭмбеддерOllama(адрес="http://ollama.local")
        with mock.patch("httpx.post", side_effect=post):
            векторы = э.векторы([f"текст {i}" for i in range(20)])
        self.assertEqual(пачки, [16, 4])
        self.assertEqual(векторы[0], [0.6, 0.8])
        self.assertEqual(э.размерность, 2)

    def test_ollama_не_запущена_подсказка_как_запустить(self):
        import httpx
        э = rag_embed.ЭмбеддерOllama(адрес="http://127.0.0.1:9")
        with mock.patch("httpx.get", side_effect=httpx.ConnectError("отказ")):
            self.assertIn("ollama serve", э.проверить())

    def test_ollama_без_модели_подсказка_pull(self):
        э = rag_embed.ЭмбеддерOllama(адрес="http://ollama.local")
        with mock.patch("httpx.get", return_value=_ОтветOllama(
                200, {"models": [{"name": "llama3:latest"}]})):
            self.assertIn("ollama pull bge-m3", э.проверить())
        with mock.patch("httpx.get", return_value=_ОтветOllama(
                200, {"models": [{"name": "bge-m3:latest"}]})):
            self.assertEqual(э.проверить(), "")


class ХранилищеИИндексатор(unittest.TestCase):
    """store.py и indexer.py: запись, поиск по косинусу, кэш векторов, пересборка."""

    def setUp(self):
        self.корень = tempfile.mkdtemp(prefix="rag-индекс-")
        self.документы = _набор_документов(os.path.join(self.корень, "docs"))
        self.индексатор = rag.Индексатор(документы=self.документы,
                                         индекс=os.path.join(self.корень, "index"),
                                         эмбеддер="хэш", ocr=False)

    def tearDown(self):
        shutil.rmtree(self.корень, ignore_errors=True)

    def test_сборка_пишет_обе_стратегии_с_паспортами(self):
        отчёт = self.индексатор.построить()
        self.assertEqual(set(отчёт["стратегии"]), {"фикс", "структура"})
        self.assertEqual(отчёт["документов"], 4)
        индексы = {и["стратегия"]: и for и in self.индексатор.хранилище.индексы()}
        self.assertEqual(индексы["фикс"]["параметры"], {"размер": 500, "перекрытие": 75})
        self.assertEqual(индексы["структура"]["эмбеддер"], "хэш")
        self.assertEqual(индексы["структура"]["размерность"], 512)
        документы = self.индексатор.хранилище.документы("структура")
        self.assertEqual(len(документы), 4)
        self.assertTrue(all(д["чанков"] > 0 for д in документы))
        self.assertTrue(os.path.exists(os.path.join(self.корень, "index", "docs.db")))

    def test_повторная_сборка_берёт_векторы_из_кэша(self):
        self.индексатор.построить(["структура"])
        второй = self.индексатор.построить(["структура"])
        с = второй["стратегии"]["структура"]
        self.assertEqual(с["из_кэша"], с["чанков"])

    def test_пересборка_одной_стратегии_не_трогает_другую(self):
        self.индексатор.построить()
        фикс_до = len(self.индексатор.хранилище.чанки("фикс"))
        self.индексатор.построить(["структура"], макс=100, мин=20)
        self.assertEqual(len(self.индексатор.хранилище.чанки("фикс")), фикс_до)
        self.assertEqual(self.индексатор.хранилище.индекс("структура")["параметры"],
                         {"макс": 100, "мин": 20})

    def test_поиск_ставит_нужный_фрагмент_первым(self):
        self.индексатор.построить()
        находки = self.индексатор.найти("сколько апрувов нужно до мержа", "структура", 3)
        self.assertIn("2 апрува", находки[0].чанк.текст)
        self.assertEqual([н.место for н in находки], [1, 2, 3])
        self.assertGreaterEqual(находки[0].оценка, находки[1].оценка)
        только = self.индексатор.найти("апрувов", "фикс", 5, источник="бюджет")
        self.assertTrue(all("бюджет" in н.чанк.источник for н in только))

    def test_обе_стратегии_одним_вектором(self):
        self.индексатор.построить()
        итог = self.индексатор.найти_везде("лицензии Автотранс", 2)
        self.assertEqual(set(итог), {"фикс", "структура"})

    def test_чанк_и_соседи(self):
        self.индексатор.построить(["фикс"], размер=60, перекрытие=10)
        чанки = self.индексатор.хранилище.чанки("фикс", "политика.md")
        средний = self.индексатор.хранилище.чанк(чанки[1].chunk_id)
        до, после = self.индексатор.хранилище.соседи(средний)
        self.assertEqual((до.номер, после.номер), (0, 2))
        self.assertIsNone(self.индексатор.хранилище.чанк("нет-такого"))

    def test_индекс_другим_эмбеддером_не_ищется_молча(self):
        self.индексатор.построить(["структура"])
        чужой = rag.Индексатор(документы=self.документы, индекс=os.path.join(self.корень, "index"),
                               эмбеддер="ollama", ocr=False)
        with self.assertRaisesRegex(rag.ОшибкаИндекса, "собран эмбеддером хэш"):
            чужой.найти("апрув", "структура")

    def test_пустой_индекс_и_пустой_запрос(self):
        with self.assertRaisesRegex(rag.ОшибкаИндекса, "нет"):
            self.индексатор.найти("апрув", "структура")
        with self.assertRaisesRegex(rag.ОшибкаИндекса, "Пустой запрос"):
            self.индексатор.найти("  ", "структура")

    def test_размерность_запроса_сверяется(self):
        self.индексатор.построить(["структура"])
        with self.assertRaisesRegex(rag.ОшибкаИндекса, "Размерность"):
            self.индексатор.хранилище.искать("структура", [1.0, 0.0], 3)

    def test_недоступный_эмбеддер_останавливает_сборку_до_работы(self):
        with mock.patch.object(rag_embed.ЭмбеддерХэш, "проверить", return_value="сломан"):
            with self.assertRaisesRegex(rag.ОшибкаИндекса, "сломан"):
                self.индексатор.построить()
        self.assertEqual(self.индексатор.хранилище.индексы(), [])


class СравнениеСтратегий(unittest.TestCase):
    """evaluate.py: статистика нарезки и эталонные вопросы."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-сравнение-")
        cls.документы = _набор_документов(os.path.join(cls.корень, "docs"))
        cls.индексатор = rag.Индексатор(документы=cls.документы,
                                        индекс=os.path.join(cls.корень, "index"),
                                        эмбеддер="хэш", ocr=False)
        # «Структура» с пределом больше абзаца: длинный раздел делится по целым
        # абзацам, и ни один не рвётся; «фикс» по 60 токенов рвёт их обязательно.
        cls.индексатор.построить(размер=60, перекрытие=15, макс=700, мин=20)
        cls.вопросы = os.path.join(cls.корень, "вопросы.json")
        with open(cls.вопросы, "w", encoding="utf-8") as f:
            _json.dump({"вопросы": [
                {"id": "a", "вопрос": "сколько апрувов нужно до мержа", "источник": "политика",
                 "маркеры": ["минимум 2 апрува"]},
                {"id": "b", "вопрос": "лицензии Автотранс количество", "источник": "бюджет",
                 "маркеры": ["Организация: Автотранс"]},
                {"id": "c", "вопрос": "про космос", "источник": "", "маркеры": ["марсоход"]},
            ]}, f, ensure_ascii=False)
        cls.отчёт = rag_evaluate.сравнить(cls.индексатор, cls.вопросы)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def test_статистика_нарезки(self):
        н = self.отчёт["нарезка"]
        self.assertEqual(н["структура"]["разорвано_блоков"], 0)
        self.assertGreater(н["фикс"]["разорвано_блоков"], 0)
        self.assertGreater(н["фикс"]["избыточность"], 1.05, "перекрытие дублирует текст")
        self.assertAlmostEqual(н["структура"]["избыточность"], 1.0, delta=0.05)
        for с in ("фикс", "структура"):
            self.assertLessEqual(н[с]["мин"], н[с]["медиана"])
            self.assertLessEqual(н[с]["медиана"], н[с]["макс"])

    def test_метрики_поиска(self):
        п = self.отчёт["поиск"]
        self.assertEqual(п["вопросов"], 3)
        for с, м in п["стратегии"].items():
            self.assertEqual(set(м), {"hit@1", "hit@3", "hit@5", "mrr", "документ@1"})
            self.assertTrue(all(0.0 <= з <= 1.0 for з in м.values()), м)
            self.assertLessEqual(м["hit@1"], м["hit@3"])
            self.assertLessEqual(м["hit@3"], м["hit@5"])
        по_id = {в["id"]: в for в in п["вопросы"]}
        self.assertEqual(по_id["a"]["места"]["структура"]["место"], 1)
        self.assertEqual(по_id["c"]["места"]["структура"]["место"], 0)

    def test_попадание_требует_и_документ_и_фразу(self):
        ч = rag.нарезать(rag_extract.извлечь(os.path.join(self.документы, "политика.md"),
                                             self.документы), "структура")[0]
        self.assertTrue(rag_evaluate.попадание(ч, {"источник": "политика", "маркеры": ["2 апрува"]}))
        self.assertFalse(rag_evaluate.попадание(ч, {"источник": "бюджет", "маркеры": ["2 апрува"]}))
        self.assertFalse(rag_evaluate.попадание(ч, {"источник": "", "маркеры": ["3 апрува"]}))
        # Переносы строк и регистр не мешают.
        self.assertTrue(rag_evaluate.попадание(ч, {"маркеры": ["МИНИМУМ   2\nапрува"]}))

    def test_markdown_и_запись_рядом_со_своим_индексом(self):
        текст = rag_evaluate.в_markdown(self.отчёт)
        self.assertIn("## Как нарезано", текст)
        self.assertIn("## Как находится", текст)
        self.assertIn("| hit@1 |", текст)
        путь = rag_evaluate.записать(self.отчёт, os.path.join(self.корень, "index"))
        self.assertEqual(путь, os.path.join(self.корень, "index", "RAG-COMPARISON.md"))
        self.assertEqual(rag_evaluate.прочитать(os.path.join(self.корень, "index"))["документов"], 4)

    def test_эталонные_вопросы_проекта_корректны(self):
        вопросы = rag_evaluate.загрузить_вопросы(os.path.join(КОРЕНЬ_ДНЯ, "rag-questions.json"))
        self.assertGreaterEqual(len(вопросы), 15)
        self.assertEqual(len({в["id"] for в in вопросы}), len(вопросы))
        for в in rag_evaluate.с_ответом(вопросы):
            self.assertTrue(в["маркеры"], в)
        # День 23: ловушки для подбора порога — без маркеров, но с пометкой.
        ловушки = [в for в in вопросы if в.get("ловушка")]
        self.assertGreaterEqual(len(ловушки), 5)
        self.assertTrue(all(not в["маркеры"] for в in ловушки))


class СерверДокументов(unittest.TestCase):
    """rag_server.py: пять инструментов только для чтения поверх индекса."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-сервер-")
        документы = _набор_документов(os.path.join(cls.корень, "docs"))
        cls.индексатор = rag.Индексатор(документы=документы,
                                        индекс=os.path.join(cls.корень, "index"),
                                        эмбеддер="хэш", ocr=False)
        cls.индексатор.построить()
        cls.сервер = rag_server.создать_сервер(cls.индексатор)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def test_инструменты_только_читают(self):
        инструменты = _инструменты(self.сервер)
        self.assertEqual({и.name for и in инструменты},
                         {"search_docs", "compare_strategies", "get_chunk", "list_documents",
                          "index_stats"})
        for и in инструменты:
            self.assertTrue(и.annotations.read_only_hint, и.name)

    def test_поиск_возвращает_паспорт_и_обрезанный_текст(self):
        итог = _вызвать(self.сервер, "search_docs", {"query": "минимум апрувов до мержа", "limit": 2})
        self.assertFalse(итог.is_error, _текст(итог))
        данные = итог.structured_content
        self.assertEqual(данные["strategy"], "структура")
        первая = данные["hits"][0]
        for поле in ("chunk_id", "source", "section", "pages", "score", "rank", "text"):
            self.assertIn(поле, первая)
        self.assertEqual(первая["source"], "политика.md")

    def test_ошибки_доходят_до_модели_текстом(self):
        итог = _вызвать(self.сервер, "search_docs", {"query": "апрув", "strategy": "смысл"})
        self.assertTrue(итог.is_error)
        self.assertIn("Нет стратегии", _текст(итог))
        итог = _вызвать(self.сервер, "get_chunk", {"chunk_id": "struct-00000000-0000"})
        self.assertTrue(итог.is_error)
        self.assertIn("нет в индексе", _текст(итог))

    def test_чанк_с_соседями_документы_и_состояние(self):
        ид = _вызвать(self.сервер, "search_docs", {"query": "сборка проекта журнал",
                                                   "strategy": "фикс"}).structured_content["hits"][0]["chunk_id"]
        чанк = _вызвать(self.сервер, "get_chunk", {"chunk_id": ид, "neighbors": True}).structured_content
        self.assertEqual(чанк["chunk_id"], ид)
        self.assertIn("previous", чанк)
        документы = _вызвать(self.сервер, "list_documents", {}).structured_content["documents"]
        self.assertEqual(len(документы), 4)
        индексы = _вызвать(self.сервер, "index_stats", {}).structured_content["indexes"]
        self.assertEqual({и["strategy"] for и in индексы}, {"фикс", "структура"})
        рядом = _вызвать(self.сервер, "compare_strategies", {"query": "лицензии"}).structured_content
        self.assertEqual(set(рядом["strategies"]), {"фикс", "структура"})

    def test_сервер_описан_в_файле_проекта(self):
        серверы = {с.имя: с for с in amcp.load(os.path.join(КОРЕНЬ_ДНЯ, "mcp-servers.json"))}
        docs = серверы["docs"]
        self.assertIn("rag_server.py", " ".join(docs.аргументы))
        self.assertIn("договор", docs.темы)
        self.assertIn("RAG_DIR", docs.окружение)


class КонсольИндекса(unittest.TestCase):
    """cli.py: ключи индекса работают без агента и не открывают диалог."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-консоль-")
        cls.документы = _набор_документов(os.path.join(cls.корень, "docs"))
        cls.индекс = os.path.join(cls.корень, "index")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def cli(self, *ключи: str) -> _subprocess.CompletedProcess:
        return _subprocess.run(
            [sys.executable, os.path.join(КОРЕНЬ_ДНЯ, "cli.py"), "--документы-из", self.документы,
             "--индекс-в", self.индекс, "--эмбеддер", "хэш", "--без-ocr", *ключи],
            capture_output=True, text=True, stdin=_subprocess.DEVNULL, timeout=120,
            env={**os.environ, "MEMORY_DIR": os.path.join(self.корень, "memory")})

    def test_1_индексация_поиск_чанк_и_сравнение(self):
        сборка = self.cli("--индексировать")
        self.assertEqual(сборка.returncode, 0, сборка.stderr)
        self.assertIn("«фикс» (размер 500, перекрытие 75): чанков", сборка.stdout)
        self.assertIn("«структура» (макс 700, мин 120): чанков", сборка.stdout)
        self.assertNotIn("Вы:", сборка.stdout)

        показ = self.cli("--индекс")
        self.assertEqual(показ.returncode, 0, показ.stderr)
        self.assertIn("политика.md", показ.stdout)

        поиск = self.cli("--найти-в-документах", "минимум апрувов до мержа", "--сколько", "1")
        self.assertEqual(поиск.returncode, 0, поиск.stderr)
        self.assertIn("Стратегия «структура»", поиск.stdout)
        self.assertIn("Стратегия «фикс»", поиск.stdout)
        ид = next(с.split(",")[0].strip() for с in поиск.stdout.splitlines()
                  if с.strip().startswith("struct-"))

        чанк = self.cli("--чанк", ид)
        self.assertEqual(чанк.returncode, 0, чанк.stderr)
        self.assertIn("chunk_id", чанк.stdout)

        сравнение = self.cli("--сравнить-стратегии", "--вопросы",
                             os.path.join(КОРЕНЬ_ДНЯ, "rag-questions.json"))
        self.assertEqual(сравнение.returncode, 0, сравнение.stderr)
        self.assertIn("## Как нарезано", сравнение.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.индекс, "RAG-COMPARISON.md")))

    def test_2_извлечение_показывает_документы(self):
        итог = self.cli("--извлечь")
        self.assertEqual(итог.returncode, 0, итог.stderr)
        self.assertIn("бюджет.xlsx", итог.stdout)
        self.assertIn("Документов 4", итог.stdout)

    def test_3_ошибки_коротко_и_кодом_1(self):
        итог = self.cli("--индексировать", "по-смыслу")
        self.assertEqual(итог.returncode, 1)
        self.assertIn("Нет стратегии", итог.stderr)
        итог = self.cli("--чанк", "struct-00000000-0000")
        self.assertEqual(итог.returncode, 1)
        self.assertNotIn("Traceback", итог.stderr)

    def test_ключ_индекса_виден_серверу_docs_через_окружение(self):
        import io
        import cli
        прежние = {к: os.environ.get(к) for к in ("RAG_DIR", "RAG_EMBEDDER", "DOCS_DIR")}
        try:
            with mock.patch.object(sys, "argv", ["cli.py", "--индекс-в", self.индекс,
                                                 "--эмбеддер", "хэш", "--индекс"]), \
                 mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(cli.main(), 0)
            self.assertEqual(os.environ["RAG_DIR"], os.path.abspath(self.индекс))
            self.assertEqual(os.environ["RAG_EMBEDDER"], "хэш")
        finally:
            for к, з in прежние.items():
                if з is None:
                    os.environ.pop(к, None)
                else:
                    os.environ[к] = з

    def test_без_ключей_индекса_команда_не_перехватывается(self):
        import cli
        аргументы = cli.build_parser().parse_args(["вопрос"])
        self.assertIsNone(cli.команда_индекса(аргументы))

    def test_справка_рассказывает_об_индексе(self):
        import cli
        справка = cli.build_parser().format_help()
        for ключ in ("--индексировать", "--найти-в-документах", "--сравнить-стратегии",
                     "--размер-чанка", "--перекрытие", "--эмбеддер", "--чанк"):
            self.assertIn(ключ, справка)
        self.assertIn("индекс документов (День 21, RAG)", справка)


class ВебИндекса(unittest.TestCase):
    """Страница /rag и её ручки: состояние, сборка в фоне, поиск, чанк, сравнение."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-веб-")
        cls.прежние = {к: os.environ.get(к)
                       for к in ("MEMORY_DIR", "RAG_DIR", "DOCS_DIR", "RAG_EMBEDDER")}
        os.environ["MEMORY_DIR"] = os.path.join(cls.корень, "memory")
        os.environ["RAG_DIR"] = os.path.join(cls.корень, "index")
        os.environ["DOCS_DIR"] = _набор_документов(os.path.join(cls.корень, "docs"))
        os.environ["RAG_EMBEDDER"] = "хэш"
        import importlib
        import web as веб_модуль
        cls.веб = importlib.reload(веб_модуль)
        cls.клиент = cls.веб.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.веб.agent.close()
        for к, з in cls.прежние.items():
            if з is None:
                os.environ.pop(к, None)
            else:
                os.environ[к] = з
        shutil.rmtree(cls.корень, ignore_errors=True)

    def дождаться(self) -> dict:
        for _ in range(200):
            работа = self.клиент.get("/api/rag").get_json()["работа"]
            if работа and работа["готово"]:
                return работа
            time.sleep(0.05)
        self.fail("работа с индексом не закончилась")

    def test_1_пустое_состояние(self):
        с = self.клиент.get("/api/rag").get_json()
        self.assertEqual(с["индексы"], [])
        self.assertEqual(с["эмбеддер"]["имя"], "хэш")
        self.assertTrue(с["эмбеддер"]["готов"])
        self.assertIn("готов", с["ocr"])
        ответ = self.клиент.post("/api/rag/compare", json={})
        self.assertEqual(ответ.status_code, 400)

    def test_2_сборка_в_фоне_и_второй_запуск_ждёт(self):
        ответ = self.клиент.post("/api/rag/build", json={"embedder": "хэш", "ocr": False})
        self.assertEqual(ответ.status_code, 202)
        работа = self.дождаться()
        self.assertEqual(работа["ошибка"], "")
        self.assertEqual(set(работа["итог"]["стратегии"]), {"фикс", "структура"})
        с = self.клиент.get("/api/rag").get_json()
        self.assertEqual(len(с["индексы"]), 2)
        self.assertEqual(len(с["документы"]), 4)
        self.assertEqual(set(с["документы"][0]["чанков"]), {"фикс", "структура"})
        with self.веб._ЗАМОК_ИНДЕКСА:
            занято = self.клиент.post("/api/rag/build", json={"embedder": "хэш"})
        self.assertEqual(занято.status_code, 409)

    def test_3_поиск_и_чанк(self):
        о = self.клиент.post("/api/rag/search", json={"query": "минимум апрувов", "limit": 2}).get_json()
        self.assertEqual(set(о["results"]), {"фикс", "структура"})
        ид = о["results"]["структура"][0]["chunk_id"]
        ч = self.клиент.get(f"/api/rag/chunk/{ид}").get_json()
        self.assertEqual(ч["chunk"]["chunk_id"], ид)
        self.assertIn("next", ч)
        self.assertEqual(self.клиент.get("/api/rag/chunk/нет").status_code, 404)
        одна = self.клиент.post("/api/rag/search", json={"query": "апрув", "strategy": "фикс"}).get_json()
        self.assertEqual(list(одна["results"]), ["фикс"])

    def test_4_сравнение_в_фоне(self):
        ответ = self.клиент.post("/api/rag/compare", json={"embedder": "хэш"})
        self.assertEqual(ответ.status_code, 202)
        работа = self.дождаться()
        self.assertEqual(работа["ошибка"], "")
        с = self.клиент.get("/api/rag").get_json()
        self.assertIn("поиск", с["сравнение"])
        self.assertTrue(os.path.exists(os.path.join(os.environ["RAG_DIR"], "RAG-COMPARISON.md")))

    def test_проверка_параметров(self):
        плохо = self.клиент.post("/api/rag/build", json={"size": 100, "overlap": 100})
        self.assertEqual(плохо.status_code, 400)
        self.assertIn("Перекрытие", плохо.get_json()["error"])
        self.assertEqual(self.клиент.post("/api/rag/build", json={"strategies": ["смысл"]}).status_code, 400)
        self.assertEqual(self.клиент.post("/api/rag/search", json={"query": ""}).status_code, 400)
        self.assertEqual(self.клиент.post("/api/rag/build", json={"embedder": "word2vec"}).status_code, 400)

    def test_страницы(self):
        страница = self.клиент.get("/rag").get_data(as_text=True)
        # С Дня 22 страница называется по RAG (в Дне 23 — «RAG: реранкинг…»),
        # но индекс Дня 21 на месте.
        self.assertIn("<title>RAG", страница)
        self.assertIn("Собрать индекс", страница)
        for ид in ('id="собрать"', 'id="запрос"', 'id="искать"', 'id="сравнить"', 'id="находки"',
                   'id="документы"', "❓ Справка по форме"):
            self.assertIn(ид, страница)
        главная = self.клиент.get("/").get_data(as_text=True)
        self.assertIn('href="/rag"', главная)
        self.assertIn("Локальный RAG", главная)
        self.assertIn("индексу документов", главная.lower())


# --- День 22: первый RAG-запрос -----------------------------------------------

from agent.rag import answer as rag_answer, qa as rag_qa  # noqa: E402


class _КлиентRAG:
    """Фальшивая модель на настоящем Reply: отвечает по режиму, судит по правилу.

    Режим узнаётся по тексту запроса, как его видит настоящая модель: с RAG в
    сообщении пользователя лежат «Фрагменты документов», без RAG — голый вопрос.
    """

    def __init__(self, без_rag: str = "Обычно такие договоры требуют трёх апрувов.",
                 судья: str = '{"оценка": "верно", "выдумка": false, "пояснение": "ок"}'):
        self.без_rag = без_rag
        self.судья = судья
        self.вызовы: list[dict] = []
        # Агент читает расход у клиента и закрывает его — как у настоящего Client.
        self.spent = {"calls": 0, "tokens": 0, "cost": 0.0}

    def close(self) -> None:
        pass

    def reset_spent(self) -> None:
        self.spent = {"calls": 0, "tokens": 0, "cost": 0.0}

    def call(self, model_key, messages, max_tokens=0, temperature=None, low_effort=False,
             tools=None):
        from agent.llm import Reply
        self.вызовы.append({"model": model_key, "messages": messages, "low_effort": low_effort})
        система, пользователь = messages[0]["content"], messages[-1]["content"]
        if система.startswith("Ты проверяешь"):
            текст = self.судья
        elif система.startswith("Пользователь задал вопрос по документам"):
            # День 24: уточняющий вопрос для «не знаю».
            текст = '{"уточнение": "Какой документ вы имеете в виду?"}'
        elif '"цитаты": [{"n": 1, "текст": "…"}]' in система:
            # День 24: ответ с цитатами — как настоящая модель, JSON с дословной цитатой.
            пользователь = messages[1]["content"]
            if "Фрагменты документов" in пользователь and "апрув" in пользователь:
                текст = ('{"ответ": "До мержа нужно минимум 2 апрува [1].", "источники": [1], '
                         '"цитаты": [{"n": 1, "текст": "До мержа нужно минимум 2 апрува от владельцев кода"}]}')
            else:
                текст = ('{"ответ": "Не знаю", "не_знаю": true, '
                         '"уточнение": "Какой документ вы имеете в виду?", "источники": [], "цитаты": []}')
        elif "Фрагменты документов" in пользователь:
            текст = "До мержа нужно минимум 2 апрува [1]." if "апрув" in пользователь else \
                rag_answer.НЕТ_В_ДОКУМЕНТАХ + "."
        elif "не нашлось" in пользователь:
            текст = rag_answer.НЕТ_В_ДОКУМЕНТАХ + "."
        else:
            текст = self.без_rag
        return Reply(text=текст, model_key=model_key, prompt_tokens=len(пользователь) // 4,
                     completion_tokens=12, cost=0.0001)


class _Сломанный:
    spent = {"calls": 0, "tokens": 0, "cost": 0.0}

    def close(self) -> None:
        pass

    def call(self, *a, **k):
        from agent.llm import LLMError
        raise LLMError("провайдер недоступен")


def _индекс_для_rag(корень: str) -> "rag.Индексатор":
    документы = _набор_документов(os.path.join(корень, "docs"))
    индексатор = rag.Индексатор(документы=документы, индекс=os.path.join(корень, "index"),
                                эмбеддер="хэш", ocr=False)
    индексатор.построить()
    return индексатор


_ВОПРОСЫ_RAG = [
    {"id": "a", "вопрос": "Сколько апрувов нужно до мержа?",
     "ожидание": "Минимум 2 апрува.", "факты": [["2 апрува", "два апрува"]],
     "источники": [{"файл": "политика", "раздел": "Апрувы"}]},
    {"id": "b", "ловушка": True, "вопрос": "Сколько стоит Office 365?",
     "ожидание": "В документах этого нет.", "факты": [], "источники": []},
]


class RAGЗапрос(unittest.TestCase):
    """agent/rag/answer.py: вопрос → поиск → промпт с фрагментами → модель."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-запрос-")
        cls.индексатор = _индекс_для_rag(cls.корень)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def отвечатель(self, клиент=None, **поля):
        поля.setdefault("порог", 0.0)
        # Тесты Дня 22 проверяют его формат: текст со ссылками [n]. Формат
        # «цитаты» (по умолчанию с Дня 24) — в классах Дня 24.
        поля.setdefault("формат", rag_answer.ТЕКСТ)
        return rag_answer.Отвечатель(self.индексатор, клиент or _КлиентRAG(), **поля)

    def test_с_rag_фрагменты_с_паспортом_и_ссылка_на_источник(self):
        клиент = _КлиентRAG()
        ответ = self.отвечатель(клиент).ответить("Сколько апрувов нужно до мержа?", "rag", "ds-flash")
        сообщения = клиент.вызовы[0]["messages"]
        self.assertIn(rag_answer.ПРАВИЛА_RAG, сообщения[0]["content"])
        self.assertIn("[1] политика.md", сообщения[1]["content"])
        self.assertTrue(сообщения[1]["content"].rstrip().endswith("Вопрос: Сколько апрувов нужно до мержа?"))
        self.assertEqual(ответ.цитаты, [1])
        self.assertEqual(ответ.источники[0]["source"], "политика.md")
        self.assertIn("2 апрува", ответ.фрагменты[0].чанк.текст)
        self.assertFalse(ответ.отказ)
        слои = [б.layer for б in ответ.блоки]
        self.assertEqual(слои.count("документы"), len(ответ.фрагменты))
        self.assertEqual(ответ.модель, "ds-flash")

    def test_без_rag_тот_же_вопрос_та_же_роль_без_поиска(self):
        клиент = _КлиентRAG()
        with mock.patch.object(self.индексатор, "найти",
                               side_effect=AssertionError("без RAG поиска нет")):
            ответ = self.отвечатель(клиент).ответить("Сколько апрувов нужно до мержа?", "без", "ds-flash")
        сообщения = клиент.вызовы[0]["messages"]
        self.assertEqual(сообщения[0]["content"], rag_answer.РОЛЬ)
        self.assertEqual(сообщения[1]["content"], "Сколько апрувов нужно до мержа?")
        self.assertEqual((ответ.фрагменты, ответ.цитаты), ([], []))
        self.assertEqual(ответ.источники, [])

    def test_режимы_различаются_только_контекстом(self):
        клиент = _КлиентRAG()
        итог = self.отвечатель(клиент).сравнить("Сколько апрувов нужно до мержа?", "ds-flash")
        self.assertEqual(set(итог), {"без", "rag"})
        без, с = клиент.вызовы
        self.assertEqual(без["model"], с["model"])
        self.assertTrue(с["messages"][0]["content"].startswith(без["messages"][0]["content"]))

    def test_порог_отсеивает_и_модель_слышит_что_фрагментов_нет(self):
        клиент = _КлиентRAG()
        ответ = self.отвечатель(клиент, порог=0.999).ответить("Сколько апрувов?", "rag", "ds-flash")
        self.assertEqual(ответ.фрагменты, [])
        self.assertTrue(ответ.отсеяно)
        self.assertIn("не нашлось", клиент.вызовы[0]["messages"][1]["content"])
        self.assertTrue(ответ.отказ)

    def test_сбой_модели_становится_ошибкой_ответа(self):
        ответ = self.отвечатель(_Сломанный()).ответить("вопрос", "rag", "ds-flash")
        self.assertIn("провайдер недоступен", ответ.ошибка)

    def test_ссылки_и_выдуманные_номера(self):
        self.assertEqual(rag_answer.разобрать_ссылки("A [2]. B [1, 3]. C [2] и [7].", 5),
                         ([2, 1, 3], [7]))
        self.assertEqual(rag_answer.разобрать_ссылки("без ссылок", 5), ([], []))

    def test_отказ_распознаётся_только_явный(self):
        self.assertTrue(rag_answer.ОТКАЗ.search("В документах этого нет."))
        self.assertTrue(rag_answer.ОТКАЗ.search("У меня нет доступа к вашему договору"))
        # Так модель без RAG честно отказывает чаще всего — это тоже отказ.
        self.assertTrue(rag_answer.ОТКАЗ.search(
            "В предоставленном контексте нет данных о стоимости подписки"))
        self.assertTrue(rag_answer.ОТКАЗ.search(
            "В предоставленных данных нет информации о закупке лицензий"))
        # Первая версия шаблона принимала такой содержательный ответ за отказ.
        self.assertFalse(rag_answer.ОТКАЗ.search(
            "Срок — до 20-го числа, если в договоре не указан иной период."))

    def test_неверный_режим_и_пустой_вопрос(self):
        with self.assertRaisesRegex(ValueError, "Нет режима"):
            self.отвечатель().ответить("вопрос", "смешанный")
        with self.assertRaisesRegex(ValueError, "Пустой"):
            self.отвечатель().ответить("  ", "rag")

    def test_ответ_сериализуется_для_страницы(self):
        данные = self.отвечатель().ответить("Сколько апрувов нужно до мержа?", "rag", "ds-flash").to_dict()
        for поле in ("text", "mode", "fragments", "sources", "citations", "blocks", "tokens_in"):
            self.assertIn(поле, данные)
        _json.dumps(данные, ensure_ascii=False)

    def test_роли_моделей_для_ответа_и_судьи(self):
        self.assertIn("ответ-по-документам", catalog.ROLES)
        # С Дня 23 у судьи запасная модель: квота groq-20b кончается посреди дня.
        self.assertEqual(catalog.ROLES["судья-ответов"]["models"], ["groq-20b", "groq-120b"])


class КонтрольныеВопросы(unittest.TestCase):
    """agent/rag/qa.py и rag-control.json: правила, судья, итог по режимам."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-контроль-")
        cls.индексатор = _индекс_для_rag(cls.корень)
        cls.вопросы = _ВОПРОСЫ_RAG

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def test_факты_без_разделителей_и_с_регуляркой(self):
        self.assertTrue(rag_qa.есть_факт("составляет 24 441 696,00 рублей", ["24441696"]))
        self.assertTrue(rag_qa.есть_факт("26 731,5 руб.", ["26731.5"]))
        self.assertTrue(rag_qa.есть_факт("неустойка 0,2 % в день", ["re:0\\.2\\s*%"]))
        self.assertFalse(rag_qa.есть_факт("0,1 % в день", ["re:0\\.2\\s*%"]))

    def test_ловушка_верна_только_отказом(self):
        вопрос = self.вопросы[1]
        отказ = rag_answer.ОтветRAG(вопрос="?", режим="rag", текст="В документах этого нет.", отказ=True)
        выдумка = rag_answer.ОтветRAG(вопрос="?", режим="без", текст="Около 12 000 руб. в год.")
        self.assertTrue(rag_qa.по_правилам(вопрос, отказ).верно_по_правилам)
        self.assertFalse(rag_qa.по_правилам(вопрос, выдумка).верно_по_правилам)

    def test_прогон_в_обоих_режимах_с_судьёй(self):
        клиент = _КлиентRAG(судья='{"оценка": "неверно", "выдумка": true, "пояснение": "x"}')
        отвечатель = rag_answer.Отвечатель(self.индексатор, клиент, порог=0.0)
        прогон = rag_qa.прогнать(отвечатель, self.вопросы, True, "ds-flash")
        без, с = прогон.итог("без"), прогон.итог("rag")
        self.assertEqual(с["верно_по_правилам"], 2)          # факт найден, ловушка — отказ
        self.assertEqual(без["верно_по_правилам"], 0)        # выдумал «трёх апрувов»
        self.assertEqual((с["источник_найден"], с["источник_процитирован"], с["с_источником"]),
                         (1, 1, 1))
        self.assertEqual(без["выдумок"], 2)
        судьи = [в for в in клиент.вызовы if в["messages"][0]["content"].startswith("Ты проверяешь")]
        self.assertEqual(len(судьи), 4)
        self.assertTrue(all(в["low_effort"] for в in судьи))
        self.assertTrue(all(в["model"] == "groq-20b" for в in судьи))

    def test_отказ_без_цифр_не_выдумка_даже_если_судья_сказал_иначе(self):
        клиент = _КлиентRAG(судья='{"оценка": "неверно", "выдумка": true, "пояснение": "нет"}')
        отказ = rag_answer.ОтветRAG(вопрос="?", режим="rag", текст="В документах этого нет.", отказ=True)
        оценка = rag_qa.по_правилам(self.вопросы[0], отказ)
        rag_qa.судить(клиент, self.вопросы[0], оценка)
        self.assertEqual((оценка.судья, оценка.выдумка), ("неверно", False))
        self.assertIn("не выдумка", оценка.пояснение)

    def test_числа_из_фрагментов_это_опора_а_не_выдумка(self):
        отвечатель = rag_answer.Отвечатель(self.индексатор, _КлиентRAG(), порог=0.0)
        ответ = отвечатель.ответить("Сколько апрувов нужно до мержа?", "rag", "ds-flash")
        ответ.текст = "Нужно 2 апрува [1], ревью — не более 24 часов [2]."
        оценка = rag_qa.по_правилам(self.вопросы[0], ответ)
        self.assertEqual((оценка.чисел, оценка.чисел_в_контексте), (2, 2))
        rag_qa.судить(_КлиентRAG(судья='{"оценка": "верно", "выдумка": true, "пояснение": "лишнее"}'),
                      self.вопросы[0], оценка)
        self.assertFalse(оценка.выдумка)
        self.assertIn("опора на контекст", оценка.пояснение)
        # Число, которого во фрагментах нет, — не опора, и вердикт судьи остаётся.
        ответ.текст = "Нужно 2 апрува [1] и 7 согласований [2]."
        оценка = rag_qa.по_правилам(self.вопросы[0], ответ)
        self.assertEqual((оценка.чисел, оценка.чисел_в_контексте), (2, 1))
        rag_qa.судить(_КлиентRAG(судья='{"оценка": "частично", "выдумка": true, "пояснение": "7"}'),
                      self.вопросы[0], оценка)
        self.assertTrue(оценка.выдумка)
        self.assertEqual(rag_qa.числа_ответа("24 441 696,00 руб. [1], [2, 3]"), ["24441696.00"])
        self.assertEqual(rag_qa.числа_ответа("с 01.01.2025 по 31.12.2027, НДС 20%"),
                         ["01.01.2025", "31.12.2027", "20"])

    def test_неразобранный_вердикт_не_выдаётся_за_оценку(self):
        клиент = _КлиентRAG(судья="Думаю, всё хорошо")
        оценка = rag_qa.по_правилам(self.вопросы[0], rag_answer.ОтветRAG(
            вопрос="?", режим="без", текст="2 апрува"))
        rag_qa.судить(клиент, self.вопросы[0], оценка)
        self.assertEqual(оценка.судья, "")
        self.assertIn("не разобран", оценка.пояснение)
        оценка2 = rag_qa.по_правилам(self.вопросы[0], оценка.ответ)
        with mock.patch("time.sleep"):
            rag_qa.судить(_Сломанный(), self.вопросы[0], оценка2)
        self.assertIn("судья недоступен", оценка2.пояснение)

    def test_отчёт_и_запись_рядом_со_своим_индексом(self):
        отвечатель = rag_answer.Отвечатель(self.индексатор, _КлиентRAG(), порог=0.0)
        прогон = rag_qa.прогнать(отвечатель, self.вопросы, False, "ds-flash")
        текст = rag_qa.в_markdown(прогон)
        for кусок in ("## Итог", "| верно по правилам", "## По вопросам", "## Ответы целиком",
                      "(ловушка)", "← процитирован"):
            self.assertIn(кусок, текст)
        путь = rag_qa.записать(прогон, os.path.join(self.корень, "index"))
        self.assertEqual(путь, os.path.join(self.корень, "index", "RAG-QA.md"))
        self.assertEqual(rag_qa.прочитать(os.path.join(self.корень, "index"))["model"], "ds-flash")

    def test_контрольный_набор_проекта(self):
        вопросы = rag_qa.загрузить(os.path.join(КОРЕНЬ_ДНЯ, "rag-control.json"))
        self.assertEqual(len(вопросы), 10)
        self.assertEqual(sum(bool(в.get("ловушка")) for в in вопросы), 2)
        self.assertEqual(len({в["id"] for в in вопросы}), 10)
        for в in вопросы:
            self.assertTrue(в["ожидание"])
            if not в.get("ловушка"):
                self.assertTrue(в["факты"] and в["источники"], в["id"])

    def test_плохой_набор_отвергается(self):
        путь = os.path.join(self.корень, "плохо.json")
        with open(путь, "w", encoding="utf-8") as f:
            _json.dump({"вопросы": [{"id": "x", "вопрос": "?", "ожидание": "!"}]}, f)
        with self.assertRaisesRegex(ValueError, "нет фактов"):
            rag_qa.загрузить(путь)


class АгентДвеРежима(unittest.TestCase):
    """MemoryAgent.спросить_документы / сравнить_режимы, консоль и веб Дня 22."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-агент-")
        cls.индексатор = _индекс_для_rag(cls.корень)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def агент(self):
        from agent import MemoryAgent
        агент = MemoryAgent(base_dir=os.path.join(self.корень, "memory"), router_mode=OFF)
        агент.client = _КлиентRAG()
        агент._rag_индексатор = self.индексатор
        return агент

    def test_два_режима_и_запись_в_диалог(self):
        агент = self.агент()
        try:
            with mock.patch.object(rag_answer, "ПОРОГ", 0.0):
                итог = агент.сравнить_режимы("Сколько апрувов нужно до мержа?")
            self.assertEqual(итог["rag"].цитаты, [1])
            self.assertEqual(итог["без"].фрагменты, [])
            тексты = " ".join(р["content"] for р in агент.memory.short.all(агент.session))
            self.assertIn("[документы, с RAG] Сколько апрувов", тексты)
            self.assertIn("[документы, без RAG] Сколько апрувов", тексты)
            self.assertIn("2 апрува [1]", тексты)
            with self.assertRaisesRegex(AgentError, "Нет режима"):
                агент.спросить_документы("вопрос", "смешанный")
        finally:
            агент.close()

    def test_сбой_модели_и_индекса_это_ошибка_агента(self):
        агент = self.агент()
        try:
            агент.client = _Сломанный()
            with self.assertRaisesRegex(AgentError, "провайдер недоступен"):
                агент.спросить_документы("вопрос", "без")
        finally:
            агент.close()

    def test_ключи_консоли_не_открывают_диалог(self):
        import cli
        справка = cli.build_parser().format_help()
        for ключ in ("--rag", "--без-rag", "--сравнить-rag", "--контрольные", "--контроль",
                     "--без-судьи-ответов", "--фрагменты"):
            self.assertIn(ключ, справка)
        self.assertIn("RAG-запрос: ответ с документами и без (День 22)", справка)
        for ключи in (["--rag", "?"], ["--без-rag", "?"], ["--сравнить-rag", "?"], ["--контрольные"]):
            self.assertTrue(cli.меняет_состояние(cli.build_parser().parse_args(ключи)), ключи)

    def test_печать_ответа_с_источниками(self):
        import io
        import cli
        with mock.patch.object(rag_answer, "ПОРОГ", 0.0):
            ответ = rag_answer.Отвечатель(self.индексатор, _КлиентRAG(), порог=0.0).ответить(
                "Сколько апрувов нужно до мержа?", "rag", "ds-flash")
        вывод = io.StringIO()
        with mock.patch("sys.stdout", вывод):
            cli.напечатать_ответ_rag(ответ)
        текст = вывод.getvalue()
        # С Дня 23 шапка называет и режим поиска; без него — поиск Дня 22, «база».
        self.assertIn("С RAG · поиск «база» · модель ds-flash", текст)
        self.assertIn("Источники:", текст)
        self.assertIn("[1] политика.md", текст)
        self.assertIn("← процитирован", текст)


class ВебRAGЗапроса(unittest.TestCase):
    """Ручки /api/rag/ask и /api/rag/qa и карточки страницы."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-веб22-")
        cls.прежние = {к: os.environ.get(к) for к in
                       ("MEMORY_DIR", "RAG_DIR", "DOCS_DIR", "RAG_EMBEDDER", "RAG_CONTROL",
                        "RAG_SETTINGS")}
        # Настройки второго этапа (День 23) — не из корня проекта: там лежит
        # настоящий подбор, и тест зависел бы от него.
        os.environ["RAG_SETTINGS"] = os.path.join(cls.корень, "rag-settings.json")
        os.environ["MEMORY_DIR"] = os.path.join(cls.корень, "memory")
        os.environ["RAG_DIR"] = os.path.join(cls.корень, "index")
        os.environ["DOCS_DIR"] = _набор_документов(os.path.join(cls.корень, "docs"))
        os.environ["RAG_EMBEDDER"] = "хэш"
        контроль = os.path.join(cls.корень, "контроль.json")
        with open(контроль, "w", encoding="utf-8") as f:
            _json.dump({"вопросы": _ВОПРОСЫ_RAG}, f, ensure_ascii=False)
        os.environ["RAG_CONTROL"] = контроль
        rag.Индексатор(эмбеддер="хэш", ocr=False).построить()
        import importlib
        import web as веб_модуль
        cls.веб = importlib.reload(веб_модуль)
        cls.веб.agent.client = _КлиентRAG()
        cls.клиент = cls.веб.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.веб.agent.close()
        for к, з in cls.прежние.items():
            if з is None:
                os.environ.pop(к, None)
            else:
                os.environ[к] = з
        shutil.rmtree(cls.корень, ignore_errors=True)

    def test_вопрос_в_обоих_режимах(self):
        with mock.patch.object(rag_answer, "ПОРОГ", 0.0):
            о = self.клиент.post("/api/rag/ask", json={
                "question": "Сколько апрувов нужно до мержа?", "mode": "оба", "model": "ds-flash"}).get_json()
        self.assertEqual(set(о["answers"]), {"без", "rag"})
        self.assertEqual(о["answers"]["rag"]["citations"], [1])
        self.assertEqual(о["answers"]["без"]["fragments"], [])
        self.assertEqual(self.веб.agent.model_key, "", "модель страницы не должна залипать в агенте")

    def test_один_режим_и_ошибки_ввода(self):
        о = self.клиент.post("/api/rag/ask", json={"question": "?", "mode": "без"}).get_json()
        self.assertEqual(list(о["answers"]), ["без"])
        self.assertEqual(self.клиент.post("/api/rag/ask", json={"question": ""}).status_code, 400)
        self.assertEqual(self.клиент.post("/api/rag/ask", json={"question": "?", "mode": "x"}).status_code, 400)

    def test_контрольные_в_фоне(self):
        # С Дня 23 по умолчанию идут пять режимов; режимы Дня 22 просятся явно.
        ответ = self.клиент.post("/api/rag/qa", json={"model": "ds-flash", "judge": False,
                                                      "modes": ["без", "rag"]})
        self.assertEqual(ответ.status_code, 202)
        for _ in range(200):
            работа = self.клиент.get("/api/rag").get_json()["работа"]
            if работа and работа["готово"]:
                break
            time.sleep(0.05)
        self.assertEqual(работа["ошибка"], "")
        контроль = self.клиент.get("/api/rag").get_json()["контроль"]
        self.assertEqual(len(контроль["questions"]), 2)
        self.assertIn("rag", контроль["totals"])

    @unittest.skipUnless(shutil.which("node"), "нужен node")
    def test_скрипты_страниц_синтаксически_целы(self):
        # Повторное объявление переменной в скрипте страницы убивает его целиком:
        # кнопки мертвы, а тесты ручек зелёные — они зовут сервер напрямую.
        # Так и случилось в Дне 22: показ простоял десять минут без единого запроса.
        for имя in ("rag.html", "index.html"):
            текст = pathlib.Path(os.path.join(КОРЕНЬ_ДНЯ, "templates", имя)).read_text(encoding="utf-8")
            скрипт = текст[текст.rindex("<script>") + 8:текст.rindex("</script>")]
            скрипт = re.sub(r"\{%.*?%\}|\{\{.*?\}\}", "0", скрипт, flags=re.S)
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
                f.write(скрипт)
            try:
                готово = _subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
            finally:
                os.remove(f.name)
            self.assertEqual(готово.returncode, 0, f"{имя}: {готово.stderr[-400:]}")

    def test_страница_дня_22(self):
        страница = self.клиент.get("/rag").get_data(as_text=True)
        for узел in ('id="rag-вопрос"', 'id="rag-режим"', 'id="rag-модель"', 'id="rag-спросить"',
                     'id="rag-ответы"', 'id="прогнать-контроль"', 'id="контроль"', 'id="контроль-ход"',
                     "<title>RAG"):
            self.assertIn(узел, страница)
        главная = self.клиент.get("/").get_data(as_text=True)
        self.assertIn('id="rag-ссылка-28"', главная)


# --- День 23: реранкинг, фильтр и rewrite ---------------------------------------

from agent.rag import calibrate as rag_calibrate  # noqa: E402
from agent.rag import rerank as rag_rerank, retrieval as rag_retrieval  # noqa: E402
from agent.rag import rewrite as rag_rewrite  # noqa: E402


class _КлиентДня23(_КлиентRAG):
    """Модель, которая умеет ещё переписывать запросы и оценивать фрагменты.

    Переписывание отвечает словами из документа («минимум 2 апрува»), LLM-реранкер
    ставит 9 фрагменту, где есть «апрув», и 1 остальным — как сделала бы модель.
    """

    def __init__(self, *а, запросы=None, **к):
        super().__init__(*а, **к)
        self.запросы = запросы or ["минимум 2 апрува до мержа", "владельцы кода апрув"]

    def call(self, model_key, messages, max_tokens=0, temperature=None, low_effort=False,
             tools=None):
        from agent.llm import Reply
        система = messages[0]["content"]
        if система.startswith("Ты готовишь поисковые запросы"):
            self.вызовы.append({"model": model_key, "messages": messages, "low_effort": low_effort})
            return Reply(text=_json.dumps({"запросы": self.запросы}, ensure_ascii=False),
                         model_key=model_key, prompt_tokens=50, completion_tokens=20)
        if система.startswith("Ты оцениваешь"):
            self.вызовы.append({"model": model_key, "messages": messages, "low_effort": low_effort})
            фрагменты = messages[1]["content"].split("\n\nВопрос:")[0].split("\n\n[")[1:]
            оценки = {str(i): 9 if "апрув" in кусок else 1 for i, кусок in enumerate(фрагменты, 1)}
            return Reply(text=_json.dumps({"оценки": оценки}), model_key=model_key,
                         prompt_tokens=300, completion_tokens=40)
        return super().call(model_key, messages, max_tokens, temperature, low_effort, tools)


def _поисковик_для_rag(корень: str, клиент=None, настройки: dict | None = None):
    """Поисковик над маленьким индексом. У эмбеддера-заглушки косинусы ниже 0,45,
    поэтому порог первого этапа в файле настроек — ноль."""
    индексатор = _индекс_для_rag(корень)
    путь = os.path.join(корень, "rag-settings.json")
    with open(путь, "w", encoding="utf-8") as f:
        _json.dump({"когда": "тест", "выбрано": {"порог_косинуса": 0.0, **(настройки or {})}},
                   f, ensure_ascii=False)
    return rag_retrieval.Поисковик(индексатор, клиент or _КлиентДня23(), путь_настроек_=путь)


class Реранкеры(unittest.TestCase):
    """rerank.py: три реранкера за одним интерфейсом и кэш оценок."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-реранк-")
        cls.индексатор = _индекс_для_rag(cls.корень)
        cls.находки = cls.индексатор.найти("Сколько апрувов нужно до мержа?", "структура", 5)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def test_эвристика_ставит_выше_фрагмент_со_словами_вопроса(self):
        э = rag_rerank.Эвристика(self.индексатор.хранилище)
        оценки = э.оценить("Сколько апрувов нужно до мержа?", self.находки)
        self.assertTrue(all(0.0 <= о <= 1.0 for о in оценки))
        лучший = self.находки[оценки.index(max(оценки))]
        self.assertIn("апрув", лучший.чанк.текст)

    def test_основы_без_вопросительных_слов(self):
        self.assertEqual(rag_rerank.основы("Сколько стоит OpenScape?"), ["стоит", "opens"])

    def test_кросс_энкодер_сигмоида_пачками_и_кэш(self):
        import numpy as np

        class Сессия:
            def __init__(self):
                self.пачки = []

            def get_inputs(self):
                return [types.SimpleNamespace(name="input_ids"), types.SimpleNamespace(name="attention_mask")]

            def run(self, _, данные):
                self.пачки.append(данные["input_ids"].shape[0])
                return [np.zeros((данные["input_ids"].shape[0], 1), dtype=np.float32)]

        class Токенизатор:
            def encode_batch(self, пары):
                return [types.SimpleNamespace(ids=[0, 1, 2], attention_mask=[1, 1, 1]) for _ in пары]

            def encode(self, текст, add_special_tokens=True):
                return types.SimpleNamespace(ids=[0] * 3, offsets=[(0, 1)] * 3)

        кэш = rag_rerank.КэшОценок(self.индексатор.база)
        к = rag_rerank.КроссЭнкодер(кэш)
        к._сессия, к._токенизатор, к._разметка = Сессия(), Токенизатор(), Токенизатор()
        находки = self.индексатор.найти("мерж", "фикс", 10)
        оценки = к.оценить("мерж?", находки)
        self.assertEqual(оценки, [0.5] * len(находки))       # sigmoid(0)
        self.assertEqual(к._сессия.пачки, [8, len(находки) - 8] if len(находки) > 8 else [len(находки)])
        к.оценить("мерж?", находки)
        self.assertEqual(к.из_кэша, len(находки))

    def test_кросс_энкодер_оценивает_длинный_чанк_окнами(self):
        # Пара «вопрос + чанк» обрезается на длине модели, и ответ в конце
        # длинного чанка кросс-энкодер не видел (c03: 0,005). Окна с
        # перекрытием и лучшее окно как оценка чанка это чинят.
        import numpy as np
        from agent.rag.chunking import Чанк
        from agent.rag.store import Находка

        class Посимвольно:
            """Токен — символ: границы окон легко проверить."""

            def encode(self, текст, add_special_tokens=True):
                return types.SimpleNamespace(ids=list(range(len(текст))),
                                             offsets=[(i, i + 1) for i in range(len(текст))])

            def encode_batch(self, пары):
                # «Модель» узнаёт ответ, только если он целиком в окне.
                return [types.SimpleNamespace(ids=[1 if "ОТВЕТ" in окно else 0], attention_mask=[1])
                        for _, окно in пары]

        class Сессия:
            def get_inputs(self):
                return [types.SimpleNamespace(name="input_ids")]

            def run(self, _, данные):
                return [np.where(данные["input_ids"] == 1, 5.0, -5.0).astype(np.float32)]

        текст = "начало договора " * 5 + "ОТВЕТ"
        чанк = Чанк(chunk_id="c", стратегия="структура", источник="f.pdf", название="f",
                    номер=0, текст=текст, раздел="2", контекст="f › 2")
        к = rag_rerank.КроссЭнкодер(None, длина=40, перекрытие=10)
        к._сессия, к._токенизатор, к._разметка = Сессия(), Посимвольно(), Посимвольно()
        окна = к.окна("вопрос", чанк)
        self.assertGreater(len(окна), 1)
        self.assertTrue(all(о.startswith("f › 2\n") for о in окна))    # паспорт в каждом окне
        self.assertIn("ОТВЕТ", окна[-1])
        [оценка] = к.оценить("вопрос", [Находка(чанк, 0.5, 1)])
        self.assertGreater(оценка, 0.99)                                # лучшее окно
        self.assertEqual(к.пар, len(окна))
        # Без окон (длина с запасом) — одно окно, как раньше.
        широкий = rag_rerank.КроссЭнкодер(None, длина=1000)
        широкий._разметка = Посимвольно()
        self.assertEqual(широкий.окна("вопрос", чанк), ["f › 2\n" + текст])
        # Оценки обрезанных пар и окон не смешиваются в кэше.
        self.assertNotEqual(к.ключ_кэша, rag_rerank.КРОСС)
        # Окна у энкодера, чьи оценки пришли из кэша: разметка грузится сама.
        свежий = rag_rerank.КроссЭнкодер(None, длина=40, перекрытие=10)

        def загрузить():
            свежий._разметка = Посимвольно()

        with mock.patch.object(свежий, "загрузить", side_effect=загрузить) as загрузка:
            self.assertEqual(свежий.окна("вопрос", чанк), окна)
        загрузка.assert_called_once()

    def test_кросс_энкодер_без_модели_внятная_ошибка(self):
        к = rag_rerank.КроссЭнкодер(None, модель="нет/такой-модели")
        with mock.patch("huggingface_hub.hf_hub_download", side_effect=OSError("сеть")):
            with self.assertRaisesRegex(rag_rerank.ОшибкаРеранкера, "не скачалась"):
                к.оценить("вопрос", self.находки[:1])

    def test_llm_реранкер_json_и_отказ_на_мусор(self):
        р = rag_rerank.LLMРеранкер(_КлиентДня23(), None, "ds-flash")
        оценки = р.оценить("Сколько апрувов нужно до мержа?", self.находки)
        self.assertTrue(any(о == 0.9 for о in оценки))
        self.assertTrue(all(о in (0.9, 0.1) for о in оценки))
        сломанный = rag_rerank.LLMРеранкер(_КлиентRAG(), None, "ds-flash")
        with self.assertRaisesRegex(rag_rerank.ОшибкаРеранкера, "не оценки"):
            сломанный.оценить("вопрос", self.находки[:2])

    def test_создать_по_имени(self):
        self.assertIsInstance(rag_rerank.создать("эвристика", хранилище=self.индексатор.хранилище),
                              rag_rerank.Эвристика)
        with self.assertRaisesRegex(rag_rerank.ОшибкаРеранкера, "Нет реранкера"):
            rag_rerank.создать("bert")
        with self.assertRaisesRegex(rag_rerank.ОшибкаРеранкера, "клиент"):
            rag_rerank.создать("llm")


class ПереписываниеЗапроса(unittest.TestCase):
    """rewrite.py: запросы на языке документов, кэш и слияние RRF."""

    def setUp(self):
        self.корень = tempfile.mkdtemp(prefix="rag-rewrite-")
        self.база = os.path.join(self.корень, "db.sqlite")

    def tearDown(self):
        shutil.rmtree(self.корень, ignore_errors=True)

    def test_запросы_кэш_и_исходный_первым(self):
        клиент = _КлиентДня23(запросы=["срок оплаты", "Сколько апрувов?", "неустойка"])
        п = rag_rewrite.Переписчик(клиент, self.база, ["Договор VPN", "Лист «Касперский»"], "groq-20b")
        итог = п.переписать("Сколько апрувов?")
        self.assertEqual(итог.все, ["Сколько апрувов?", "срок оплаты", "неустойка"])
        self.assertIn("Лист «Касперский»", клиент.вызовы[0]["messages"][0]["content"])
        self.assertTrue(клиент.вызовы[0]["low_effort"])
        второй = п.переписать("Сколько апрувов?")
        self.assertTrue(второй.из_кэша)
        self.assertEqual(len(клиент.вызовы), 1)

    def test_сбой_модели_оставляет_исходный_вопрос(self):
        итог = rag_rewrite.Переписчик(_Сломанный(), self.база).переписать("вопрос")
        self.assertEqual(итог.все, ["вопрос"])
        self.assertIn("не удалось", итог.ошибка)
        мусор = rag_rewrite.Переписчик(_КлиентRAG(без_rag="нет json"), "").переписать("вопрос")
        self.assertIn("не запросы", мусор.ошибка)

    def test_rrf_поднимает_найденное_несколькими_запросами(self):
        from agent.rag.store import Находка
        from agent.rag.chunking import Чанк

        def н(ид, место, cos=0.6):
            return Находка(Чанк(chunk_id=ид, стратегия="структура", источник="f", название="f",
                                номер=0, текст=ид, раздел=""), cos, место)

        слито = rag_rewrite.слить([[н("a", 1), н("b", 2)], [н("b", 1), н("c", 2)]])
        self.assertEqual([х.чанк.chunk_id for х, _, _ in слито], ["b", "a", "c"])


class ПоискВДваЭтапа(unittest.TestCase):
    """retrieval.py: четыре режима, судьба кандидатов, поправки и сбой реранкера."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-два-этапа-")
        cls.поисковик = _поисковик_для_rag(cls.корень, настройки={
            "реранкер": "эвристика", "k_до": 10, "k_после": 2, "порог": 0.3})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def test_режимы_из_файла_настроек(self):
        р = self.поисковик.режимы()
        self.assertEqual(list(р), ["база", "фильтр", "rewrite", "полный"])
        self.assertEqual((р["база"].реранкер, р["база"].k_после, р["база"].rewrite), ("", 5, False))
        self.assertEqual((р["фильтр"].реранкер, р["фильтр"].k_до, р["фильтр"].k_после, р["фильтр"].порог),
                         ("эвристика", 10, 2, 0.3))
        self.assertTrue(р["полный"].rewrite and р["полный"].реранкер == "эвристика")
        self.assertTrue(р["rewrite"].rewrite and not р["rewrite"].реранкер)
        self.assertTrue(all(н.порог_косинуса == 0.0 for н in р.values()))
        self.assertIn("rag-settings.json", rag_retrieval.прочитать_настройки(self.поисковик.путь_настроек)["источник"])

    def test_без_файла_значения_по_умолчанию(self):
        п = rag_retrieval.прочитать_настройки(os.path.join(self.корень, "нет.json"))
        self.assertIn("по умолчанию", п["источник"])
        self.assertEqual((п["реранкер"], п["порог_косинуса"]), ("кросс", 0.45))

    def test_база_это_поиск_дня_22(self):
        отбор = self.поисковик.найти("Сколько апрувов нужно до мержа?", "база")
        прямой = self.поисковик.индексатор.найти("Сколько апрувов нужно до мержа?", "структура", 5)
        self.assertEqual([н.чанк.chunk_id for н in отбор.фрагменты],
                         [н.чанк.chunk_id for н in прямой if н.оценка >= 0.0])
        self.assertTrue(all(н.реранк is None for н in отбор.фрагменты))
        self.assertEqual([э["этап"] for э in отбор.этапы], ["поиск", "порог косинуса", "фильтр"])

    def test_фильтр_реранжирует_режет_порогом_и_K(self):
        отбор = self.поисковик.найти("Сколько апрувов нужно до мержа?",
                                     rag_retrieval.Настройки(имя="т", реранкер="эвристика", k_до=10,
                                                             k_после=2, порог=0.3, порог_косинуса=-1))
        self.assertLessEqual(len(отбор.фрагменты), 2)
        оценки = [н.реранк for н in отбор.фрагменты]
        self.assertEqual(оценки, sorted(оценки, reverse=True))
        self.assertTrue(all(о >= 0.3 for о in оценки))
        судьбы = {к.судьба for к in отбор.кандидаты}
        self.assertTrue(судьбы <= {"в промпт", "ниже порога", "за пределами K", "порог косинуса"})
        self.assertEqual(sum(к.судьба == "в промпт" for к in отбор.кандидаты), len(отбор.фрагменты))
        self.assertIn("2 апрува", отбор.фрагменты[0].чанк.текст)
        self.assertTrue(all(н.место_до >= 1 for н in отбор.фрагменты))
        данные = отбор.to_dict()
        self.assertEqual(данные["kept"], len(отбор.фрагменты))
        _json.dumps(данные, ensure_ascii=False)

    def test_полный_переписывает_и_сливает(self):
        отбор = self.поисковик.найти("Сколько апрувов нужно до мержа?", "полный")
        self.assertEqual(отбор.этапы[0]["этап"], "запросы")
        self.assertEqual(отбор.запросы[0], "Сколько апрувов нужно до мержа?")
        self.assertEqual(len(отбор.запросы), 3)
        self.assertEqual(отбор.этапы[1]["запросов"], 3)

    def test_высокий_порог_оставляет_пустой_контекст(self):
        отбор = self.поисковик.найти("Сколько стоит AutoCAD?", rag_retrieval.Настройки(
            имя="т", реранкер="эвристика", k_до=10, k_после=5, порог=0.99, порог_косинуса=0.1))
        self.assertIn("порог косинуса", {к.судьба for к in отбор.кандидаты})
        self.assertEqual(отбор.фрагменты, [])
        self.assertTrue(all(к.судьба in ("ниже порога", "порог косинуса") for к in отбор.кандидаты))

    def test_сбой_реранкера_честно_записан(self):
        with mock.patch.object(rag_rerank.Эвристика, "_оценить",
                               side_effect=rag_rerank.ОшибкаРеранкера("сломался")):
            отбор = self.поисковик.найти("Сколько апрувов?", rag_retrieval.Настройки(
                имя="т", реранкер="эвристика", k_до=10, k_после=3, порог_косинуса=-1))
        self.assertIn("сломался", отбор.ошибки[0])
        self.assertEqual(len(отбор.фрагменты), 3)
        реранк = next(э for э in отбор.этапы if э["этап"] == "реранк")
        self.assertIn("сломался", реранк["ошибка"])

    def test_неизвестный_режим(self):
        with self.assertRaisesRegex(ValueError, "Нет режима поиска"):
            self.поисковик.найти("вопрос", "смешанный")


class ПодборПорога(unittest.TestCase):
    """calibrate.py: сетка, лексикографический выбор, запись настроек."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-подбор-")
        cls.поисковик = _поисковик_для_rag(cls.корень)
        cls.вопросы = os.path.join(cls.корень, "вопросы.json")
        with open(cls.вопросы, "w", encoding="utf-8") as f:
            _json.dump({"вопросы": [
                {"id": "a", "вопрос": "сколько апрувов нужно до мержа", "источник": "политика",
                 "маркеры": ["минимум 2 апрува"]},
                {"id": "b", "вопрос": "лицензии Автотранс количество", "источник": "бюджет",
                 "маркеры": ["Организация: Автотранс"]},
                {"id": "t", "ловушка": True, "вопрос": "стоимость AutoCAD", "маркеры": []},
            ]}, f, ensure_ascii=False)
        cls.итог = rag_calibrate.подобрать(cls.поисковик, cls.вопросы,
                                           реранкеры=("эвристика", "llm"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def test_выбор_и_метрики(self):
        в = self.итог["выбрано"]
        self.assertIn(в["реранкер"], ("эвристика", "llm"))
        self.assertIn(в["k_до"], rag_calibrate.K_ДО)
        self.assertIn(в["k_после"], rag_calibrate.K_ПОСЛЕ)
        self.assertIn(в["порог"], rag_calibrate.ПОРОГИ)
        for ключ in ("hit@1", "полнота", "mrr", "фрагментов", "ловушки_пусто"):
            self.assertIn(ключ, в["среднее"])
        # Допуск по полноте — один вопрос стенда: здесь их два.
        self.assertEqual(self.итог["допуск"], 0.5)
        self.assertEqual(set(self.итог["лучшие"]["без rewrite"]), {"без реранкера", "эвристика", "llm"})
        self.assertEqual(set(self.итог["задержка"]), {"эвристика", "llm"})

    def test_полнота_важнее_всего(self):
        строки = [
            {"реранкер": "а", "k_до": 10, "k_после": 3, "порог": 0.5, "hit@1": 1, "полнота": 0.5,
             "mrr": 0.9, "фрагментов": 1, "ловушки_пусто": 1},
            {"реранкер": "б", "k_до": 10, "k_после": 5, "порог": 0.0, "hit@1": 0.5, "полнота": 1.0,
             "mrr": 0.6, "фрагментов": 5, "ловушки_пусто": 0},
        ]
        self.assertEqual(rag_calibrate.выбрать(строки)["реранкер"], "б")

    def test_разница_в_один_вопрос_не_решает(self):
        # Без допуска выбор уходил к реранкеру, который нашёл на один вопрос
        # больше, но почти не фильтровал: так было на настоящем подборе.
        строки = [
            {"реранкер": "мягкий", "k_до": 20, "k_после": 8, "порог": 0.2, "hit@1": 0.52,
             "полнота": 0.96, "mrr": 0.65, "фрагментов": 7.2, "ловушки_пусто": 0.1},
            {"реранкер": "строгий", "k_до": 20, "k_после": 5, "порог": 0.5, "hit@1": 0.77,
             "полнота": 0.94, "mrr": 0.83, "фрагментов": 2.2, "ловушки_пусто": 1.0},
            {"реранкер": "режущий", "k_до": 20, "k_после": 3, "порог": 0.7, "hit@1": 0.9,
             "полнота": 0.5, "mrr": 0.95, "фрагментов": 1.0, "ловушки_пусто": 1.0},
        ]
        self.assertEqual(rag_calibrate.выбрать(строки)["реранкер"], "мягкий")
        self.assertEqual(rag_calibrate.выбрать(строки, 1 / 24)["реранкер"], "строгий")

    def test_отчёты_пишутся_атомарно(self):
        # Страница читает qa.json, пока фоновая работа его пишет: с прямой
        # записью «w» она ловила пустой JSON и отвечала 500 (так упал тест
        # контрольных в фоне на полном прогоне).
        путь = os.path.join(self.корень, "атомарно.json")
        with open(путь, "w", encoding="utf-8") as f:
            f.write('{"старое": 1}')
        увидено = []
        настоящий = open

        def подсмотреть(имя, *а, **к):
            if str(имя) == путь:
                with настоящий(путь, encoding="utf-8") as ф:
                    увидено.append(ф.read())
            return настоящий(имя, *а, **к)

        with mock.patch("builtins.open", подсмотреть):
            rag_evaluate.записать_атомарно(путь, '{"новое": 2}')
        self.assertEqual(увидено, [])        # сам файл на запись не открывался
        with open(путь, encoding="utf-8") as f:
            self.assertEqual(_json.load(f), {"новое": 2})
        self.assertFalse([и for и in os.listdir(self.корень) if и.endswith(".tmp")])

    def test_запись_настроек_и_отчёта(self):
        каталог = os.path.join(self.корень, "index")
        настройки = os.path.join(self.корень, "rag-settings.json")
        путь = rag_calibrate.записать(self.итог, каталог, настройки)
        self.assertEqual(путь, os.path.join(каталог, "RAG-RERANK.md"))
        текст = pathlib.Path(путь).read_text(encoding="utf-8")
        for кусок in ("**Выбрано:**", "## Реранкеры", "## Цена второго этапа", "## Порог отсечения",
                      "## top-K до и после", "## Переписанные запросы"):
            self.assertIn(кусок, текст)
        режимы = rag_retrieval.режимы(настройки)
        self.assertEqual(режимы["фильтр"].реранкер, self.итог["выбрано"]["реранкер"])
        прочитано = rag_calibrate.прочитать(каталог)
        self.assertNotIn("сетка", прочитано)


class ОтветСФильтром(unittest.TestCase):
    """answer.py, qa.py и агент: ответ в режимах поиска и контроль по пяти режимам."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-ответ-фильтр-")
        cls.поисковик = _поисковик_для_rag(cls.корень, настройки={
            "реранкер": "эвристика", "k_до": 10, "k_после": 3, "порог": 0.0})
        cls.клиент = _КлиентДня23()
        # Формат Дня 23: эти тесты — о поиске, а не о цитатах и «не знаю».
        cls.отвечатель = rag_answer.Отвечатель(cls.поисковик.индексатор, cls.клиент, порог=0.0,
                                               формат=rag_answer.ТЕКСТ)
        cls.отвечатель._поисковик = cls.поисковик

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def test_ответ_в_режиме_фильтр_несёт_отбор(self):
        ответ = self.отвечатель.ответить("Сколько апрувов нужно до мержа?", "rag", "ds-flash", поиск="фильтр")
        self.assertEqual(ответ.поиск, "фильтр")
        self.assertIsNotNone(ответ.отбор)
        self.assertLessEqual(len(ответ.фрагменты), 3)
        self.assertIsNotNone(ответ.фрагменты[0].реранк)
        данные = ответ.to_dict()
        self.assertEqual(данные["search"], "фильтр")
        self.assertIn("candidates", данные["selection"])
        self.assertIn("rerank", данные["fragments"][0])

    def test_база_как_день_22(self):
        ответ = self.отвечатель.ответить("Сколько апрувов нужно до мержа?", "rag", "ds-flash")
        self.assertEqual((ответ.поиск, ответ.отбор), ("база", None))

    def test_сравнить_поиск_четыре_режима(self):
        итог = self.отвечатель.сравнить_поиск("Сколько апрувов нужно до мержа?", модель="ds-flash")
        self.assertEqual(list(итог), ["база", "фильтр", "rewrite", "полный"])

    def test_контроль_по_пяти_режимам(self):
        прогон = rag_qa.прогнать(self.отвечатель, _ВОПРОСЫ_RAG, False, "ds-flash",
                                 режимы=("без", "база", "фильтр", "rewrite", "полный"))
        данные = прогон.to_dict()
        self.assertEqual(данные["modes"], ["без", "база", "фильтр", "rewrite", "полный"])
        self.assertEqual(set(данные["totals"]), set(данные["modes"]))
        self.assertIn("фрагментов", данные["totals"]["фильтр"])
        текст = rag_qa.в_markdown(прогон)
        self.assertIn("| показатель | без RAG | база | + фильтр | + rewrite | + rewrite и фильтр |", текст)
        self.assertIn("Запросы: «", текст)
        self.assertIn("реранк", текст)

    def test_судья_переходит_на_запасную_модель(self):
        # У groq-20b кончилась суточная квота посреди дня прогонов, и судья
        # молчал на весь контроль. Теперь вердикт выносит запасная модель роли,
        # и прогон называет её.
        from agent.llm import LLMError

        class Квота(_КлиентRAG):
            def call(self, model_key, messages, *а, **к):
                if model_key == "groq-20b" and messages[0]["content"].startswith("Ты проверяешь"):
                    raise LLMError("У Groq закончилась суточная квота: tokens per day (TPD)")
                return super().call(model_key, messages, *а, **к)

        клиент = Квота()
        отвечатель = rag_answer.Отвечатель(self.поисковик.индексатор, клиент, порог=0.0)
        with mock.patch("time.sleep", side_effect=AssertionError("квоту не ждут — сразу к запасной")):
            прогон = rag_qa.прогнать(отвечатель, _ВОПРОСЫ_RAG[:1], True, "ds-flash")
        оценка = прогон.оценки["a"]["rag"]
        self.assertEqual((оценка.судья, оценка.модель_судьи), ("верно", "groq-120b"))
        self.assertEqual(прогон.модель_судьи, "groq-120b")
        self.assertEqual(прогон.to_dict()["questions"][0]["modes"]["rag"]["judge_model"], "groq-120b")

    def test_день_22_по_умолчанию_два_режима(self):
        прогон = rag_qa.прогнать(self.отвечатель, _ВОПРОСЫ_RAG[:1], False, "ds-flash")
        self.assertEqual(прогон.режимы, ("без", "rag"))

    def test_агент_отбор_и_сравнение(self):
        from agent import MemoryAgent
        агент = MemoryAgent(base_dir=os.path.join(self.корень, "memory"), router_mode=OFF)
        try:
            агент.client = _КлиентДня23()
            агент._rag_индексатор = self.поисковик.индексатор
            отвечатель = агент._отвечатель()
            отвечатель._поисковик = rag_retrieval.Поисковик(self.поисковик.индексатор, агент.client,
                                                            путь_настроек_=self.поисковик.путь_настроек)
            self.assertIs(агент._отвечатель(), отвечатель, "отвечатель должен жить с агентом")
            отбор = агент.отбор("Сколько апрувов?", "фильтр", настройки={"k_после": 1, "порог": 0.0})
            self.assertEqual(отбор.настройки.имя, "фильтр*")
            self.assertEqual(len(отбор.фрагменты), 1)
            итог = агент.сравнить_поиск("Сколько апрувов нужно до мержа?", ("база", "фильтр"))
            self.assertEqual(set(итог), {"база", "фильтр"})
            with self.assertRaisesRegex(AgentError, "Нет режима поиска"):
                агент.отбор("вопрос", "смешанный")
            with self.assertRaisesRegex(AgentError, "Пустой"):
                агент.отбор(" ")
        finally:
            агент.close()


class КонсольИВебДня23(unittest.TestCase):
    """Ключи консоли и ручки страницы второго этапа."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-веб23-")
        cls.прежние = {к: os.environ.get(к) for к in
                       ("MEMORY_DIR", "RAG_DIR", "DOCS_DIR", "RAG_EMBEDDER", "RAG_CONTROL", "RAG_SETTINGS",
                        "RAG_QUESTIONS")}
        os.environ["MEMORY_DIR"] = os.path.join(cls.корень, "memory")
        os.environ["RAG_DIR"] = os.path.join(cls.корень, "index")
        os.environ["DOCS_DIR"] = _набор_документов(os.path.join(cls.корень, "docs"))
        os.environ["RAG_EMBEDDER"] = "хэш"
        контроль = os.path.join(cls.корень, "контроль.json")
        with open(контроль, "w", encoding="utf-8") as f:
            _json.dump({"вопросы": _ВОПРОСЫ_RAG}, f, ensure_ascii=False)
        os.environ["RAG_CONTROL"] = контроль
        настройки = os.path.join(cls.корень, "rag-settings.json")
        with open(настройки, "w", encoding="utf-8") as f:
            _json.dump({"когда": "тест", "выбрано": {"реранкер": "эвристика", "k_до": 10,
                                                     "k_после": 3, "порог": 0.0,
                                                     "порог_косинуса": 0.0}}, f)
        os.environ["RAG_SETTINGS"] = настройки
        вопросы = os.path.join(cls.корень, "вопросы.json")
        with open(вопросы, "w", encoding="utf-8") as f:
            _json.dump({"вопросы": [{"id": "a", "вопрос": "сколько апрувов", "источник": "политика",
                                     "маркеры": ["2 апрува"]}]}, f, ensure_ascii=False)
        os.environ["RAG_QUESTIONS"] = вопросы
        rag.Индексатор(эмбеддер="хэш", ocr=False).построить()
        import importlib
        import web as веб_модуль
        cls.веб = importlib.reload(веб_модуль)
        cls.веб.agent.client = _КлиентДня23()
        cls.клиент = cls.веб.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.веб.agent.close()
        for к, з in cls.прежние.items():
            if з is None:
                os.environ.pop(к, None)
            else:
                os.environ[к] = з
        shutil.rmtree(cls.корень, ignore_errors=True)

    def test_ключи_консоли(self):
        import cli
        справка = cli.build_parser().format_help()
        for ключ in ("--поиск", "--отбор", "--сравнить-поиск", "--реранкер", "--k-до", "--k-после",
                     "--порог-отсечения", "--rewrite", "--подобрать", "--настройки-поиска", "--режимы"):
            self.assertIn(ключ, справка)
        for ключи in (["--отбор", "?"], ["--сравнить-поиск", "?"], ["--подобрать"], ["--настройки-поиска"]):
            self.assertTrue(cli.меняет_состояние(cli.build_parser().parse_args(ключи)), ключи)
        аргументы = cli.build_parser().parse_args(["--реранкер", "нет", "--k-после", "2",
                                                   "--порог-отсечения", "0.3", "--rewrite", "да"])
        self.assertEqual(cli._поправки(аргументы),
                         {"реранкер": "", "k_после": 2, "порог": 0.3, "rewrite": True})

    def test_печать_отбора(self):
        import io
        import cli
        отбор = self.веб.agent.отбор("Сколько апрувов нужно до мержа?", "полный")
        вывод = io.StringIO()
        with mock.patch("sys.stdout", вывод):
            cli.напечатать_отбор(отбор)
        текст = вывод.getvalue()
        for кусок in ("1) запросы", "2) первый этап", "4) реранкер «эвристика»", "5) фильтр",
                      "в промпт", "место  было   cos    реранк"):
            self.assertIn(кусок, текст)

    def test_отбор_и_поправки(self):
        о = self.клиент.post("/api/rag/select", json={
            "question": "Сколько апрувов нужно до мержа?", "search": "фильтр", "k_after": 1,
            "cutoff": 0.0, "reranker": "эвристика", "rewrite": False}).get_json()["selection"]
        self.assertEqual(о["kept"], 1)
        self.assertEqual(о["settings"]["имя"], "фильтр*")
        for плохо in ({"question": ""}, {"question": "x", "search": "чушь"},
                      {"question": "x", "reranker": "bert"}, {"question": "x", "cutoff": 2}):
            self.assertEqual(self.клиент.post("/api/rag/select", json=плохо).status_code, 400)

    def test_ответ_во_всех_режимах_поиска(self):
        о = self.клиент.post("/api/rag/ask", json={"question": "Сколько апрувов нужно до мержа?",
                                                   "mode": "поиск"}).get_json()
        self.assertEqual(set(о["answers"]), {"база", "фильтр", "rewrite", "полный"})
        self.assertEqual(о["answers"]["фильтр"]["search"], "фильтр")
        одна = self.клиент.post("/api/rag/ask", json={"question": "?", "mode": "rag", "search": "фильтр"}).get_json()
        self.assertEqual(одна["answers"]["rag"]["search"], "фильтр")
        self.assertEqual(self.клиент.post("/api/rag/ask", json={"question": "?", "search": "x"}).status_code, 400)

    def test_состояние_контроль_и_подбор_в_фоне(self):
        с = self.клиент.get("/api/rag").get_json()
        self.assertEqual(с["режимы_поиска"]["фильтр"]["реранкер"], "эвристика")
        self.assertIn("rag-settings.json", с["настройки_поиска"])
        self.assertEqual(self.клиент.post("/api/rag/qa", json={"modes": ["x"]}).status_code, 400)
        ответ = self.клиент.post("/api/rag/qa", json={"judge": False, "modes": ["без", "фильтр"]})
        self.assertEqual(ответ.status_code, 202)
        работа = self._дождаться()
        self.assertEqual(работа["ошибка"], "")
        self.assertEqual(set(self.клиент.get("/api/rag").get_json()["контроль"]["modes"]), {"без", "фильтр"})
        # Кросс-энкодер в тестах — это 570 МБ модели и минуты CPU: подбор идёт
        # по эвристике и LLM, а сама ручка и запись настроек — настоящие.
        with mock.patch.object(rag_calibrate, "подобрать", _подбор_без_кросса):
            ответ = self.клиент.post("/api/rag/calibrate", json={})
            self.assertEqual(ответ.status_code, 202)
            работа = self._дождаться()
        self.assertEqual(работа["ошибка"], "")
        подбор = self.клиент.get("/api/rag").get_json()["подбор"]
        self.assertIn(подбор["выбрано"]["реранкер"], ("эвристика", "llm"))
        with open(os.environ["RAG_SETTINGS"], encoding="utf-8") as f:
            self.assertEqual(_json.load(f)["выбрано"]["порог_косинуса"], 0.0)

    def _дождаться(self) -> dict:
        for _ in range(400):
            работа = self.клиент.get("/api/rag").get_json()["работа"]
            if работа and работа["готово"]:
                return работа
            time.sleep(0.05)
        self.fail("фоновая работа не закончилась")

    def test_страница_дня_23(self):
        страница = self.клиент.get("/rag").get_data(as_text=True)
        for узел in ('id="отбор-вопрос"', 'id="отбор-режим"', 'id="отбор-реранкер"', 'id="отбор-kдо"',
                     'id="отбор-kпосле"', 'id="отбор-порог"', 'id="отбор-rewrite"', 'id="показать-отбор"',
                     'id="ответить-все"', 'id="отбор"', 'id="ответы-поиск"', 'id="подобрать"',
                     'id="подбор"', 'id="подбор-ход"', 'id="режимы-поиска"', "<title>RAG"):
            self.assertIn(узел, страница)
        главная = self.клиент.get("/").get_data(as_text=True)
        self.assertIn('id="rag-ссылка-28"', главная)
        self.assertIn("<title>Агент миграции ГИС —", главная)


_ОРИГИНАЛЬНЫЙ_ПОДБОР = rag_calibrate.подобрать


def _подбор_без_кросса(поисковик, путь, прогресс=None):
    return _ОРИГИНАЛЬНЫЙ_ПОДБОР(поисковик, путь, прогресс, реранкеры=("эвристика", "llm"))


# --- День 24: цитаты, источники и «не знаю» ---------------------------------------

from agent.rag import abstain as rag_abstain, cite as rag_cite  # noqa: E402
from agent.rag import citecheck as rag_citecheck  # noqa: E402

_ЦИТАТА_АПРУВЫ = "До мержа нужно минимум 2 апрува от владельцев кода"


def _json_ответ(ответ: str, источники: list, цитаты: list, **ещё) -> str:
    return _json.dumps({"ответ": ответ, "источники": источники,
                        "цитаты": [{"n": n, "текст": т} for n, т in цитаты], **ещё}, ensure_ascii=False)


class _КлиентДня24(_КлиентДня23):
    """Модель для ответов с цитатами: отвечает по сценарию, судит по правилу.

    `ответы` — что модель вернёт на запросы с цитатами по порядку (первый
    ответ, ответ на повторный запрос…); когда сценарий кончился — верный JSON.
    """

    def __init__(self, *ответы, судья_цитат: str = '{"смысл": "да", "лишнее": "", "пояснение": "ок"}'):
        super().__init__()
        self.ответы = list(ответы)
        self.судья_цитат = судья_цитат

    def call(self, model_key, messages, max_tokens=0, temperature=None, low_effort=False, tools=None):
        from agent.llm import Reply
        система = messages[0]["content"]
        if '"цитаты": [{"n": 1, "текст": "…"}]' in система:
            self.вызовы.append({"model": model_key, "messages": messages})
            текст = self.ответы.pop(0) if self.ответы else _json_ответ(
                "До мержа нужно минимум 2 апрува [1].", [1], [(1, _ЦИТАТА_АПРУВЫ)])
            return Reply(text=текст, model_key=model_key, prompt_tokens=400, completion_tokens=60,
                         cost=0.0002)
        if система.startswith("Ты проверяешь, подтверждают ли цитаты"):
            self.вызовы.append({"model": model_key, "messages": messages})
            return Reply(text=self.судья_цитат, model_key=model_key, prompt_tokens=200,
                         completion_tokens=20)
        return super().call(model_key, messages, max_tokens, temperature, low_effort, tools)


class ЦитатыИПроверкаКодом(unittest.TestCase):
    """cite.py: разбор JSON, нормализация, поиск цитаты в фрагментах, нарушения формата."""

    ДОГОВОР = ("2.2.7. извещать Абонента об изменении тарифов на Услуги и (или) тарифных планов, "
               "условий предоставления Услуги не менее чем за 10 дней до введения изменений;")
    СКАН = ("Исполнитель вправе потребовать оплаты штрафной неустойки в размере 0.2% от просроченной "
            "суммы за каждый календарный день просрочки. Общая стоимость — 24 441 696,00 рублей.")

    def test_разбор_json_в_обёртке_и_без(self):
        р = rag_cite.разобрать('```json\n' + _json_ответ("10 дней [1].", ["1"], [(1, "не менее чем за 10 дней")]) + '\n```')
        self.assertEqual((р.ответ, р.источники, р.цитаты[0].n), ("10 дней [1].", [1], 1))
        self.assertEqual(rag_cite.разобрать("просто текст").ошибка_формата, "ответ не в формате JSON")
        строкой = rag_cite.разобрать('{"ответ": "x", "цитаты": ["[2] извещать абонента заранее"]}')
        self.assertEqual((строкой.цитаты[0].n, строкой.цитаты[0].текст), (2, "извещать абонента заранее"))
        не_знаю = rag_cite.разобрать('{"не_знаю": true, "неоднозначно": true, "уточнение": "Какой договор?"}')
        self.assertTrue(не_знаю.не_знаю and не_знаю.неоднозначно)

    def test_нормализация_ocr_и_числа(self):
        self.assertEqual(rag_cite.нормализовать("Предоставле- ния  Услуги «Ёлка»"),
                         rag_cite.нормализовать('предоставления услуги "елка"'))
        self.assertEqual(rag_cite.числа("Стоимость 24 441 696,00 руб. [1], 0,20 %, п. 2.2.7, до 31.12.2027"),
                         ["24441696", "0.2", "2.2.7", "31.12.2027"])
        # Цифры в названиях — не факты ответа: живой прогон c07 требовал цитату
        # на «5» из листа «УИС-5».
        self.assertEqual(rag_cite.числа("в листе «УИС-5» по договору ВОГ396658, Лист_1, V2.1 — 676,36 на 2026г., п.3.5"),
                         ["676.36", "2026", "3.5"])

    def test_цитата_дословно_почти_в_другом_и_выдумка(self):
        р = rag_cite.разобрать(_json_ответ("x", [1], [
            (1, "извещать Абонента об изменении тарифов на Услуги"),
            (1, "Исполнитель вправе потребовать оплаты штрафной нсустойки"),    # OCR: «с» вместо «е»
            (1, "Оператор обязан уведомлять за тридцать дней до изменений"),
            (1, "10 дней")]))
        rag_cite.проверить_цитаты(р, [self.ДОГОВОР, self.СКАН])
        проверки = [(ц.проверка, ц.найдена_в) for ц in р.цитаты]
        self.assertEqual(проверки, [(rag_cite.ДОСЛОВНО, 1), (rag_cite.В_ДРУГОМ, 2),
                                    (rag_cite.НЕ_НАЙДЕНА, 0), (rag_cite.КОРОТКАЯ, 0)])
        self.assertGreater(р.цитаты[1].сходство, rag_cite.ПОЧТИ_ПОРОГ)
        self.assertEqual([ц.подтверждена for ц in р.цитаты], [True, True, False, False])

    def test_нарушения_формата(self):
        пустой = rag_cite.разобрать('{"ответ": "", "источники": [], "цитаты": []}')
        нарушено = " | ".join(rag_cite.нарушения(пустой, 2))
        for кусок in ("пустое поле «ответ»", "пустой список «источники»", "пустой список «цитаты»"):
            self.assertIn(кусок, нарушено)
        р = rag_cite.разобрать(_json_ответ("Срок — 10 дней [3], а неустойка 0,5 % [1].", [1, 3],
                                           [(1, "не менее чем за 10 дней до введения изменений")]))
        rag_cite.проверить_цитаты(р, [self.ДОГОВОР, self.СКАН])
        нарушено = " | ".join(rag_cite.нарушения(р, 2, "Какой срок?"))
        self.assertIn("номера, которых нет среди фрагментов: [3]", нарушено)
        self.assertIn("ссылки на несуществующие фрагменты: [3]", нарушено)
        self.assertIn("числа, которых нет ни в одной цитате: 0.5", нарушено)
        self.assertNotIn(" 10", нарушено.split("цитате:")[1])
        без_уточнения = rag_cite.разобрать('{"не_знаю": true}')
        self.assertIn("нет уточняющего вопроса", rag_cite.нарушения(без_уточнения, 2)[0])

    def test_числа_из_вопроса_не_требуют_цитаты(self):
        ц = [rag_cite.Цитата(1, "общая стоимость 24441696.00 рублей", rag_cite.ДОСЛОВНО, 1)]
        self.assertEqual(rag_cite.числа_без_опоры("24 441 696 руб. на 2026 год, НДС 20% [1]", ц,
                                                  "Сколько стоит в 2026 году?"), ["20"])


class РежимНеЗнаю(unittest.TestCase):
    """abstain.py: уверенность, «не знаю», уточнение, подбор порога."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-не-знаю-")
        cls.поисковик = _поисковик_для_rag(cls.корень, _КлиентДня24(), настройки={
            "реранкер": "эвристика", "k_до": 10, "k_после": 3, "порог": 0.0})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def test_уверенность_лучшая_оценка_реранкера(self):
        р = rag_abstain.решить(self.поисковик.найти("Сколько апрувов нужно до мержа?", "фильтр"), 0.3)
        self.assertGreater(р.уверенность, 0.3)
        self.assertFalse(р.не_знаю)
        ловушка = rag_abstain.решить(self.поисковик.найти("Сколько стоит AutoCAD?", "фильтр"), 0.3)
        self.assertTrue(ловушка.не_знаю)
        разделы = [(н.чанк.источник, н.чанк.раздел) for н in ловушка.ближайшие]
        self.assertEqual(len(разделы), len(set(разделы)), "ближайшие разделы без повторов")

    def test_без_оценок_реранкера_уверенность_ноль_а_без_реранкера_неизвестна(self):
        отсечено = self.поисковик.найти("Что с ним делать дальше?", rag_retrieval.Настройки(
            имя="т", реранкер="эвристика", k_до=10, k_после=3, порог_косинуса=0.99))
        self.assertEqual(rag_abstain.решить(отсечено, 0.3).уверенность, 0.0)
        база = self.поисковик.найти("Что с ним делать дальше?", "база")
        self.assertIsNone(rag_abstain.решить(база, 0.3).уверенность)
        self.assertFalse(rag_abstain.решить(база, 0.3).не_знаю)

    def test_текст_не_знаю_и_уточнение(self):
        self.assertEqual(rag_abstain.очистить_уточнение("Уточните, пожалуйста: по договору VPN [1] или ВымпелКом [2]"),
                         "По договору VPN или ВымпелКом?")
        self.assertEqual(rag_abstain.текст_не_знаю("причина", "Какой договор?"),
                         "Не знаю: причина. Уточните, пожалуйста: какой договор?")
        уточнение, reply = rag_abstain.уточнить(_КлиентRAG(), "ds-flash", "вопрос", [])
        self.assertEqual(уточнение, "Какой документ вы имеете в виду?")
        запасное, ничего = rag_abstain.уточнить(_Сломанный(), "ds-flash", "вопрос",
                                                 self.поисковик.найти("лицензии", "фильтр").фрагменты[:2])
        self.assertIsNone(ничего)
        self.assertIn("какой документ вы имеете в виду", запасное)

    def test_подбор_порога_и_запись_без_потери_настроек(self):
        вопросы = os.path.join(self.корень, "стенд.json")
        with open(вопросы, "w", encoding="utf-8") as f:
            _json.dump({"вопросы": [
                {"id": "a", "вопрос": "сколько апрувов нужно до мержа", "источник": "политика",
                 "маркеры": ["2 апрува"]},
                {"id": "t", "ловушка": True, "вопрос": "стоимость AutoCAD", "маркеры": []}]}, f,
                ensure_ascii=False)
        итог = rag_abstain.подобрать(self.поисковик, вопросы)
        в = итог["выбрано"]
        self.assertEqual((в["ловушки_не_знаю"], в["лишние_не_знаю"]), (1, 0))
        self.assertIn(итог["порог"], rag_abstain.ПОРОГИ)
        rag_abstain.записать(итог, self.поисковик.путь_настроек)
        настройки = rag_retrieval.прочитать_настройки(self.поисковик.путь_настроек)
        self.assertEqual((настройки["порог_уверенности"], настройки["реранкер"]), (итог["порог"], "эвристика"))
        # Новый подбор второго этапа (День 23) порог «не знаю» не теряет.
        with open(self.поисковик.путь_настроек, encoding="utf-8") as f:
            self.assertIn("уверенность", _json.load(f))


class ОтветСЦитатами(unittest.TestCase):
    """answer.py в формате «цитаты»: источники, проверенные цитаты, повтор, «не знаю»."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-цитаты-")
        cls.поисковик = _поисковик_для_rag(cls.корень, _КлиентДня24(), настройки={
            "реранкер": "эвристика", "k_до": 10, "k_после": 3, "порог": 0.0,
            "порог_уверенности": 0.3})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def ответ(self, вопрос="Сколько апрувов нужно до мержа?", *ответы, поиск="фильтр", **поля):
        клиент = _КлиентДня24(*ответы)
        о = rag_answer.Отвечатель(self.поисковик.индексатор, клиент, порог=0.0, **поля)
        о._поисковик = rag_retrieval.Поисковик(self.поисковик.индексатор, клиент,
                                               путь_настроек_=self.поисковик.путь_настроек)
        return о.ответить(вопрос, "rag", "ds-flash", поиск=поиск), клиент

    def test_ответ_источники_и_дословная_цитата(self):
        а, клиент = self.ответ()
        self.assertEqual(а.формат, rag_answer.ЦИТАТЫ)
        self.assertIn(rag_cite.ПРАВИЛА_ЦИТАТ, клиент.вызовы[0]["messages"][0]["content"])
        self.assertEqual(а.текст, "До мержа нужно минимум 2 апрува [1].")
        self.assertEqual(а.источники[0]["source"], "политика.md")
        self.assertTrue(а.источники[0]["chunk_id"])
        self.assertEqual([(ц.проверка, ц.найдена_в) for ц in а.выдержки], [(rag_cite.ДОСЛОВНО, 1)])
        self.assertEqual((а.вызовов, а.починка, а.нарушения, а.не_знаю), (1, False, [], False))
        данные = а.to_dict()
        self.assertEqual(данные["quotes"][0]["chunk_id"], а.источники[0]["chunk_id"])
        self.assertIn(_ЦИТАТА_АПРУВЫ, данные["fragments"][0]["text"])     # фрагмент целиком
        self.assertGreater(данные["confidence"]["confidence"], 0.3)

    def test_слабый_контекст_не_знаю_по_порогу_без_ответа_по_документам(self):
        а, клиент = self.ответ("Сколько стоит AutoCAD?")
        self.assertTrue(а.не_знаю)
        self.assertEqual(а.не_знаю_кто, "порог")
        self.assertEqual((а.фрагменты, а.источники, а.выдержки), ([], [], []))
        self.assertTrue(а.текст.startswith("Не знаю: в документах не нашлось достаточно близкого фрагмента"))
        self.assertTrue(а.текст.endswith("?"))
        self.assertEqual(а.уточнение, "Какой документ вы имеете в виду?")
        # Модель спрашивали только об уточнении — фрагментов она не видела.
        self.assertFalse(any("Фрагменты документов" in в["messages"][-1]["content"] for в in клиент.вызовы))
        self.assertTrue(а.отказ)

    def test_повтор_при_нарушении_формата(self):
        а, клиент = self.ответ("Сколько апрувов нужно до мержа?", "Минимум два апрува.")
        self.assertTrue(а.починка)
        self.assertEqual(а.вызовов, 2)
        починка = клиент.вызовы[1]["messages"][-1]["content"]
        self.assertIn("ответ не в формате JSON", починка)
        self.assertEqual(клиент.вызовы[1]["messages"][-2], {"role": "assistant", "content": "Минимум два апрува."})
        self.assertFalse(а.не_знаю)
        self.assertEqual(а.нарушения, [])

    def test_число_без_цитаты_просит_повтор(self):
        лишнее = _json_ответ("Нужно 2 апрува за 24 часа [1].", [1], [(1, _ЦИТАТА_АПРУВЫ)])
        а, клиент = self.ответ("Сколько апрувов нужно до мержа?", лишнее)
        self.assertTrue(а.починка)
        self.assertIn("числа, которых нет ни в одной цитате: 24", клиент.вызовы[1]["messages"][-1]["content"])

    def test_нет_подтверждённой_цитаты_нет_ответа(self):
        выдумка = _json_ответ("Нужно 3 апрува [1].", [1], [(1, "Нужно три апрува от любого разработчика")])
        а, _ = self.ответ("Сколько апрувов нужно до мержа?", выдумка, выдумка)
        self.assertTrue(а.не_знаю)
        self.assertEqual(а.не_знаю_кто, "проверка цитат")
        self.assertIn("ответ не подтверждается цитатами", а.текст)
        self.assertEqual(а.выдержки[0].проверка, rag_cite.НЕ_НАЙДЕНА)
        self.assertTrue(а.нарушения)

    def test_неоднозначный_вопрос_модель_просит_уточнить(self):
        неясно = '{"не_знаю": true, "неоднозначно": true, "уточнение": "Какой договор [1] имеется в виду?"}'
        а, _ = self.ответ("Сколько апрувов нужно до мержа?", неясно)
        self.assertTrue(а.не_знаю)
        self.assertEqual(а.не_знаю_кто, "модель")
        self.assertIn("вопрос можно понять по-разному", а.текст)
        self.assertEqual(а.уточнение, "Какой договор имеется в виду?")

    def test_формат_дня_23_и_база_без_измерения_уверенности(self):
        а, _ = self.ответ(формат=rag_answer.ТЕКСТ)
        self.assertEqual((а.формат, а.выдержки, а.решение), (rag_answer.ТЕКСТ, [], None))
        база, _ = self.ответ("Сколько апрувов нужно до мержа?", поиск="")
        self.assertEqual(база.поиск, "база")
        self.assertIsNone(база.решение)      # без реранкера уверенность не меряется
        self.assertFalse(база.не_знаю)

    def test_порог_в_вызове_поправляет_настройки(self):
        а, _ = self.ответ("Сколько апрувов нужно до мержа?", порог_уверенности=0.99)
        self.assertTrue(а.не_знаю)
        self.assertEqual(а.решение.порог, 0.99)


class ПроверкаЦитатНаВопросах(unittest.TestCase):
    """citecheck.py: четыре требования задания на вопросах, отчёт и сравнение с Днём 23."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-проверка-цитат-")
        cls.поисковик = _поисковик_для_rag(cls.корень, _КлиентДня24(), настройки={
            "реранкер": "эвристика", "k_до": 10, "k_после": 3, "порог": 0.0,
            "порог_уверенности": 0.3})
        cls.контроль = os.path.join(cls.корень, "контроль.json")
        with open(cls.контроль, "w", encoding="utf-8") as f:
            _json.dump({"вопросы": _ВОПРОСЫ_RAG}, f, ensure_ascii=False)
        cls.расплывчатые = os.path.join(cls.корень, "расплывчатые.json")
        with open(cls.расплывчатые, "w", encoding="utf-8") as f:
            _json.dump({"вопросы": [{"id": "v1", "вопрос": "Что с ним делать дальше?",
                                     "ожидание": "не знаю + уточнение"}]}, f, ensure_ascii=False)
        клиент = _КлиентДня24()
        cls.отвечатель = rag_answer.Отвечатель(cls.поисковик.индексатор, клиент, порог=0.0)
        cls.отвечатель._поисковик = rag_retrieval.Поисковик(
            cls.поисковик.индексатор, клиент, путь_настроек_=cls.поисковик.путь_настроек)
        cls.вопросы = rag_citecheck.загрузить_вопросы(cls.контроль, cls.расплывчатые)
        cls.прогон = rag_citecheck.прогнать(cls.отвечатель, cls.вопросы, True, "ds-flash")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def test_вопросы_трёх_видов(self):
        self.assertEqual([(в["id"], в["вид"]) for в in self.вопросы],
                         [("a", "с ответом"), ("b", "ловушка"), ("v1", "расплывчатый")])

    def test_четыре_требования_в_итоге(self):
        т = self.прогон.итог("цитаты")
        self.assertEqual((т["отвечено"], т["с_источниками"], т["источники_полные"], т["с_цитатами"]), (1, 1, 1, 1))
        self.assertEqual((т["подтверждено"], т["дословно"], т["смысл_код"], т["смысл_да"]), (1, 1, 1, 1))
        self.assertEqual((т["ловушки_не_знаю"], т["расплывчатые_не_знаю"], т["лишние_не_знаю"]), (1, 1, 0))
        self.assertEqual(т["верно"], 3)
        текст = self.прогон.итог("текст")
        self.assertEqual((текст["с_цитатами"], текст["расплывчатые_не_знаю"]), (0, 0))

    def test_судья_видит_ответ_и_цитаты(self):
        п = self.прогон.проверки["цитаты"][0]
        self.assertEqual((п.смысл_судья, п.модель_судьи), ("да", "groq-20b"))
        запрос = next(в for в in self.отвечатель.клиент.вызовы
                      if в["messages"][0]["content"].startswith("Ты проверяешь, подтверждают ли цитаты"))
        self.assertIn(_ЦИТАТА_АПРУВЫ, запрос["messages"][1]["content"])

    def test_отчёт_и_чтение(self):
        путь = rag_citecheck.записать(self.прогон, os.path.join(self.корень, "index"))
        текст = pathlib.Path(путь).read_text(encoding="utf-8")
        for кусок in ("источники в каждом ответе", "цитаты в каждом ответе", "смысл совпадает с цитатами — код",
                      "ловушки: «не знаю» с уточнением", "расплывчатые: «не знаю» с уточнением",
                      "| с цитатами (День 24) | без цитат (День 23) |", "цитата [1] (дословно)"):
            self.assertIn(кусок, текст)
        данные = rag_citecheck.прочитать(os.path.join(self.корень, "index"))
        self.assertEqual(данные["formats"], ["цитаты", "текст"])
        self.assertEqual(данные["questions"][2]["checks"]["цитаты"]["abstain_by"], "порог")


class КонсольИВебДня24(unittest.TestCase):
    """Ключи консоли, печать цитат и «не знаю», ручки страницы."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="rag-веб24-")
        cls.прежние = {к: os.environ.get(к) for к in
                       ("MEMORY_DIR", "RAG_DIR", "DOCS_DIR", "RAG_EMBEDDER", "RAG_CONTROL", "RAG_SETTINGS",
                        "RAG_QUESTIONS", "RAG_VAGUE")}
        os.environ["MEMORY_DIR"] = os.path.join(cls.корень, "memory")
        os.environ["RAG_DIR"] = os.path.join(cls.корень, "index")
        os.environ["DOCS_DIR"] = _набор_документов(os.path.join(cls.корень, "docs"))
        os.environ["RAG_EMBEDDER"] = "хэш"
        for имя, переменная, данные in (
                ("контроль.json", "RAG_CONTROL", {"вопросы": _ВОПРОСЫ_RAG}),
                ("rag-settings.json", "RAG_SETTINGS", {"когда": "тест", "выбрано": {
                    "реранкер": "эвристика", "k_до": 10, "k_после": 3, "порог": 0.0,
                    "порог_косинуса": 0.0, "порог_уверенности": 0.3}}),
                ("вопросы.json", "RAG_QUESTIONS", {"вопросы": [
                    {"id": "a", "вопрос": "сколько апрувов", "источник": "политика", "маркеры": ["2 апрува"]},
                    {"id": "t", "ловушка": True, "вопрос": "AutoCAD", "маркеры": []}]}),
                ("расплывчатые.json", "RAG_VAGUE", {"вопросы": [
                    {"id": "v1", "вопрос": "Что с ним делать дальше?", "ожидание": "не знаю"}]})):
            путь = os.path.join(cls.корень, имя)
            with open(путь, "w", encoding="utf-8") as f:
                _json.dump(данные, f, ensure_ascii=False)
            os.environ[переменная] = путь
        rag.Индексатор(эмбеддер="хэш", ocr=False).построить()
        import importlib
        import web as веб_модуль
        cls.веб = importlib.reload(веб_модуль)
        cls.веб.agent.client = _КлиентДня24()
        cls.клиент = cls.веб.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.веб.agent.close()
        for к, з in cls.прежние.items():
            if з is None:
                os.environ.pop(к, None)
            else:
                os.environ[к] = з
        shutil.rmtree(cls.корень, ignore_errors=True)

    def _дождаться(self) -> dict:
        for _ in range(400):
            работа = self.клиент.get("/api/rag").get_json()["работа"]
            if работа and работа["готово"]:
                return работа
            time.sleep(0.05)
        self.fail("фоновая работа не закончилась")

    def test_ключи_консоли(self):
        import cli
        справка = cli.build_parser().format_help()
        for ключ in ("--с-цитатами", "--проверить-цитаты", "--без-сравнения", "--без-цитат",
                     "--порог-уверенности", "--подобрать-уверенность"):
            self.assertIn(ключ, справка)
        for ключи in (["--с-цитатами", "?"], ["--проверить-цитаты"], ["--подобрать-уверенность"]):
            self.assertTrue(cli.меняет_состояние(cli.build_parser().parse_args(ключи)), ключи)
        # «--ответ» с Дня 13 — ответ на вопрос задачи; ключ Дня 24 его не перекрыл.
        self.assertEqual(cli.build_parser().parse_args(["--ответ", "да"]).answer, "да")

    def test_печать_цитат_и_не_знаю(self):
        import io
        import cli
        ответ = self.веб.agent.ответ_по_документам("Сколько апрувов нужно до мержа?")
        вывод = io.StringIO()
        with mock.patch("sys.stdout", вывод):
            cli.напечатать_ответ_rag(ответ)
        self.assertIn(f"[1] ✓ дословно «{_ЦИТАТА_АПРУВЫ}»", вывод.getvalue())
        self.assertIn("Источники:\n  [1] политика.md", вывод.getvalue())
        ответ = self.веб.agent.ответ_по_документам("Что с ним делать дальше?")
        вывод = io.StringIO()
        with mock.patch("sys.stdout", вывод):
            cli.напечатать_ответ_rag(ответ)
        for кусок in ("Не знаю:", "«Не знаю» — решил: порог уверенности (код).",
                      "Уверенность: лучшая оценка реранкера 0.00, порог 0.30.",
                      "Уточняющий вопрос: Какой документ вы имеете в виду?", "Ближайшие разделы:"):
            self.assertIn(кусок, вывод.getvalue())

    def test_ручка_ответа_с_цитатами(self):
        о = self.клиент.post("/api/rag/cited", json={"question": "Сколько апрувов нужно до мержа?"}).get_json()["answer"]
        self.assertEqual((о["format"], о["search"], о["abstain"]), ("цитаты", "фильтр", False))
        self.assertEqual(о["quotes"][0]["check"], "дословно")
        нет = self.клиент.post("/api/rag/cited", json={"question": "Что с ним делать дальше?"}).get_json()["answer"]
        self.assertEqual((нет["abstain"], нет["abstain_by"]), (True, "порог"))
        self.assertEqual(нет["clarification"], "Какой документ вы имеете в виду?")
        текст = self.клиент.post("/api/rag/cited", json={"question": "Сколько апрувов?", "format": "текст"}).get_json()
        self.assertEqual(текст["answer"]["format"], "текст")
        for плохо in ({"question": ""}, {"question": "x", "search": "чушь"}, {"question": "x", "format": "xml"},
                      {"question": "x", "confidence": 2}, {"question": "x", "confidence": "абв"}):
            self.assertEqual(self.клиент.post("/api/rag/cited", json=плохо).status_code, 400, плохо)

    def test_проверка_и_подбор_порога_в_фоне(self):
        self.assertEqual(self.клиент.post("/api/rag/citecheck", json={"compare": False}).status_code, 202)
        self.assertEqual(self._дождаться()["ошибка"], "")
        с = self.клиент.get("/api/rag").get_json()
        self.assertEqual(с["цитаты"]["formats"], ["цитаты"])
        self.assertEqual([в["kind"] for в in с["цитаты"]["questions"]], ["с ответом", "ловушка", "расплывчатый"])
        self.assertEqual(self.клиент.post("/api/rag/confidence", json={}).status_code, 202)
        работа = self._дождаться()
        self.assertEqual(работа["ошибка"], "")
        с = self.клиент.get("/api/rag").get_json()
        self.assertEqual(с["порог_уверенности"], работа["итог"]["порог"])
        self.assertIn("сетка", с["уверенность"])

    def test_страница_дня_24(self):
        страница = self.клиент.get("/rag").get_data(as_text=True)
        for узел in ('id="цит-вопрос"', 'id="цит-поиск"', 'id="цит-формат"', 'id="цит-порог"', 'id="цит-спросить"',
                     'id="цит-ответ"', 'id="проверить-цитаты"', 'id="проверка-судья"', 'id="проверка-сравнение"',
                     'id="проверка"', 'id="проверка-ход"', 'id="подобрать-уверенность"', 'id="уверенность"',
                     "<title>RAG: локальный конвейер рядом с облаком"):
            self.assertIn(узел, страница)
        главная = self.клиент.get("/").get_data(as_text=True)
        self.assertIn('id="rag-ссылка-28"', главная)
        self.assertIn("<title>Агент миграции ГИС —", главная)


# --- День 25: мини-чат с RAG и памятью задачи -----------------------------------

from agent.chat import chat as chat_mod
from agent.chat import scenarios as chat_scenarios
from agent.chat import state as chat_state

_СУДЬЯ_ЧАТА_ДА = ('{"цель_удержана": "да", "ограничения_соблюдены": "да", "ответы_по_делу": "да", '
                  '"ходы_с_проблемами": [], "пояснение": "ок"}')
_ЦИТАТА_СРОК = "Ревью занимает не более 24 часов с момента запроса"


class _КлиентДня25(_КлиентДня24):
    """Модель чата: разбирает реплики по сценарию, отвечает по памяти, судит разговор.

    `разборы` — подстрока реплики → JSON разбора (dict) или строка как есть
    (для неразбираемого ответа); без совпадения — «документы» с репликой как
    вопросом для поиска. `по_памяти` — ответы по памяти задачи по порядку.
    """

    def __init__(self, *ответы, разборы=None, по_памяти=None, судья_чата=_СУДЬЯ_ЧАТА_ДА):
        super().__init__(*ответы)
        self.разборы = dict(разборы or {})
        self.по_памяти = list(по_памяти or [])
        self.судья_чата = судья_чата

    def call(self, model_key, messages, max_tokens=0, temperature=None, low_effort=False, tools=None):
        from agent.llm import Reply
        система, пользователь = messages[0]["content"], messages[-1]["content"]
        if система.startswith("Ты ведёшь «память задачи»"):
            self.вызовы.append({"model": model_key, "messages": messages, "вид": "разбор"})
            реплика = messages[1]["content"].split("):\n")[-1]
            разбор = next((р for ключ, р in self.разборы.items() if ключ in реплика),
                          {"вид": "документы", "вопрос_для_поиска": реплика})
            текст = разбор if isinstance(разбор, str) else _json.dumps(разбор, ensure_ascii=False)
            return Reply(text=текст, model_key=model_key, prompt_tokens=300, completion_tokens=50, cost=0.0001)
        if система.startswith("Ты — помощник в чате по документам"):
            self.вызовы.append({"model": model_key, "messages": messages, "вид": "по памяти"})
            текст = (self.по_памяти.pop(0) if self.по_памяти
                     else '{"ответ": "Цель прежняя.", "опора": ["Ц"]}')
            return Reply(text=текст, model_key=model_key, prompt_tokens=500, completion_tokens=40, cost=0.0002)
        if система.startswith("Ты проверяешь разговор"):
            self.вызовы.append({"model": model_key, "messages": messages, "вид": "судья чата"})
            return Reply(text=self.судья_чата, model_key=model_key, prompt_tokens=900, completion_tokens=40)
        return super().call(model_key, messages, max_tokens, temperature, low_effort, tools)


_ЦЕЛЬ_РЕВЬЮ = {"вид": "документы", "вопрос_для_поиска": "Сколько апрувов нужно до мержа?",
               "цель": "разобраться с правилами код-ревью"}


class ПамятьЗадачиЧата(unittest.TestCase):
    """state.py: слияние памяти задачи кодом и её хранение."""

    def test_цель_ставится_один_раз_и_меняется_только_явно(self):
        п = chat_state.ПамятьЗадачи("тест")
        self.assertEqual(п.применить({"цель": "сравнить договоры связи"}, 1), {"цель": "сравнить договоры связи"})
        итог = п.применить({"цель": "собрать бюджет"}, 2)
        self.assertEqual((п.цель.текст, итог), ("сравнить договоры связи", {"цель_оставлена": "собрать бюджет"}))
        итог = п.применить({"цель": "собрать бюджет на ИТ", "смена_цели": True}, 3)
        self.assertEqual((п.цель.текст, п.цель.реплика, итог.get("смена_цели")), ("собрать бюджет на ИТ", 3, True))
        self.assertEqual([з.текст for з in п.прежние_цели], ["сравнить договоры связи"])
        self.assertEqual(п.реплик, 3)

    def test_повторы_не_дублируются_термин_переопределяется(self):
        п = chat_state.ПамятьЗадачи("тест")
        п.применить({"ограничения": ["суммы как в документе, с НДС или без"]}, 1)
        итог = п.применить({"ограничения": ["Суммы как в документе — с НДС или без."],
                            "уточнения": ["Ростелеком — только договор VPN"]}, 2)
        self.assertEqual((len(п.ограничения), итог), (1, {"уточнения": ["Ростелеком — только договор VPN"]}))
        п.применить({"термины": [{"термин": "срок платежа", "значение": "дней на оплату счёта"}]}, 3)
        итог = п.применить({"термины": [{"термин": "Срок платежа", "значение": "до какого числа платить"}]}, 4)
        т = п.термины[0]
        self.assertEqual((len(п.термины), т.текст, т.было, т.реплика),
                         (1, "до какого числа платить", "дней на оплату счёта", 4))
        self.assertIn("было", итог["термины"][0])
        self.assertEqual(п.применить({"термины": [{"термин": "", "значение": "x"}, "чушь"]}, 5), {})

    def test_пересказ_цели_в_уточнения_не_идёт(self):
        п = chat_state.ПамятьЗадачи("тест")
        п.применить({"цель": "сравнить сроки оплаты по двум договорам связи — Ростелеком VPN и ВымпелКом"}, 1)
        итог = п.применить({"уточнения": ["сравниваются два договора: Ростелеком VPN и ВымпелКом",
                                          "только договор VPN, стационарный не берём"]}, 1)
        self.assertEqual([з.текст for з in п.уточнения], ["только договор VPN, стационарный не берём"])
        self.assertEqual(итог["пропущено"], ["сравниваются два договора: Ростелеком VPN и ВымпелКом"])
        self.assertFalse(chat_state.пересказ("", "что угодно"))

    def test_выяснено_коды_и_текст(self):
        п = chat_state.ПамятьЗадачи("тест")
        п.применить({"цель": "сравнить", "ограничения": ["без пересчёта"],
                     "термины": [{"термин": "срок", "значение": "дни"}]}, 1)
        п.записать_выяснено("Сколько апрувов?", "2 апрува [1].", [{"source": "политика.md", "chunk_id": "c1"}],
                            [_ЦИТАТА_АПРУВЫ], 2)
        п.записать_выяснено("сколько апрувов", "Два апрува [1].", [{"source": "политика.md", "chunk_id": "c1"}],
                            [_ЦИТАТА_АПРУВЫ], 3)
        self.assertEqual(len(п.выяснено), 1, "тот же вопрос обновляет запись, а не плодит дубль")
        self.assertEqual(list(п.коды()), ["Ц", "О1", "Т1", "В1"])
        self.assertEqual([п.вид_кода(к) for к in ("Ц", "О1", "Т1", "В1", "Х9")],
                         ["цель", "ограничение", "термин", "выяснено", ""])
        текст = п.текст(с_кодами=True)
        for кусок in ("Ц. Цель разговора (реплика 1): сравнить", "О1. без пересчёта (реплика 1)",
                      "Т1. «срок» — дни", "В1. (реплика 3) Вопрос: сколько апрувов",
                      "Источник: политика.md · c1", f"Цитата: «{_ЦИТАТА_АПРУВЫ}»"):
            self.assertIn(кусок, текст)
        self.assertNotIn("В1", п.текст(с_выясненным=False, с_кодами=True))
        self.assertIn("без пересчёта", п.от_пользователя())
        self.assertNotIn("апрув", п.от_пользователя(), "факты из документов — не слова пользователя")

    def test_хранилище_латиницей_и_чужой_файл(self):
        self.assertEqual(chat_state.имя_файла("Договоры связи"), "chat-dogovory-svyazi.json")
        self.assertEqual(chat_state.имя_файла("!!!"), "chat-main.json")
        каталог = tempfile.mkdtemp(prefix="чат-память-")
        try:
            х = chat_state.ХранилищеЧатов(каталог)
            п = х.загрузить("договоры")
            self.assertTrue(п.пуста)
            п.применить({"цель": "сравнить"}, 1)
            п.журнал.append({"turn": 1})
            х.сохранить(п)
            снова = х.загрузить("договоры")
            self.assertEqual((снова.цель.текст, снова.реплик, снова.журнал), ("сравнить", 1, [{"turn": 1}]))
            self.assertEqual([с["сессия"] for с in х.список()], ["договоры"])
            with self.assertRaises(ValueError):
                х.загрузить("Договоры!")            # та же латиница, чужая сессия
            self.assertTrue(х.удалить("договоры"))
            self.assertFalse(х.удалить("договоры"))
        finally:
            shutil.rmtree(каталог, ignore_errors=True)


class ХодЧата(unittest.TestCase):
    """chat.py: разбор реплики, поиск на каждом ходе, ответ с источниками, память."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="чат-ход-")
        cls.поисковик = _поисковик_для_rag(cls.корень, _КлиентДня25(), настройки={
            "реранкер": "эвристика", "k_до": 10, "k_после": 3, "порог": 0.0, "порог_уверенности": 0.3})
        cls.номер = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def чат(self, клиент, сессия="тест"):
        type(self).номер += 1
        о = rag_answer.Отвечатель(self.поисковик.индексатор, клиент, порог=0.0)
        о._поисковик = rag_retrieval.Поисковик(self.поисковик.индексатор, клиент,
                                               путь_настроек_=self.поисковик.путь_настроек)
        короткая = ShortTermMemory(os.path.join(self.корень, f"dialog-{self.номер}.db"))
        хранилище = chat_state.ХранилищеЧатов(os.path.join(self.корень, f"chat-{self.номер}"))
        return chat_mod.Чат(о, клиент, короткая, хранилище, сессия, "ds-flash")

    def test_разбор_реплики_из_json(self):
        р = chat_mod.разобрать_разбор('Вот: {"вид": "Разговор", "вопрос_для_поиска": " цель ", "уточнения": "только VPN", '
                                      '"термины": [{"термин": "срок", "значение": "дни"}, {"термин": "x"}], '
                                      '"подвопросы": ["a", "b", "c", "d", "e"]}')
        self.assertEqual((р.вид, р.вопрос_для_поиска, р.уточнения), ("разговор", "цель", ["только VPN"]))
        self.assertEqual(р.термины, [{"термин": "срок", "значение": "дни"}])
        self.assertEqual(len(р.подвопросы), chat_mod.ПОДВОПРОСОВ)
        self.assertEqual(chat_mod.разобрать_разбор('{"вид": "чушь"}').вид, chat_mod.ДОКУМЕНТЫ)
        self.assertIsNone(chat_mod.разобрать_разбор("не JSON"))

    def test_ход_по_документам_источники_память_история(self):
        клиент = _КлиентДня25(разборы={"апрувов": _ЦЕЛЬ_РЕВЬЮ, "на само ревью": {
            "вид": "документы", "вопрос_для_поиска": "Сколько времени занимает ревью?"}})
        ч = self.чат(клиент)
        х = ч.ход("Мне нужно разобраться с правилами код-ревью. Сколько апрувов нужно до мержа?")
        self.assertEqual((х.номер, х.вид, х.не_знаю), (1, "документы", False))
        self.assertEqual(х.изменения, {"цель": "разобраться с правилами код-ревью"})
        документ = х.источники[0]
        self.assertEqual((документ["вид"], документ["source"]), ("документ", "политика.md"))
        self.assertTrue(документ["chunk_id"])
        self.assertEqual(документ["цитаты"], [{"текст": _ЦИТАТА_АПРУВЫ, "проверка": "дословно"}])
        self.assertEqual((х.выяснено, х.цель, х.сводка["выяснено"]), ("В1", "разобраться с правилами код-ревью", 1))
        self.assertGreater(х.кандидатов, 0, "поиск по базе шёл")
        # Ответ — в историю вместе с источником: следующий разбор видит, о чём шла речь.
        история = ч.история()
        self.assertEqual([с["role"] for с in история], ["user", "assistant"])
        self.assertIn("[Источники: политика.md", история[1]["content"])
        клиент.ответы.append(_json_ответ("Не более 24 часов [1].", [1], [(1, _ЦИТАТА_СРОК)]))
        х2 = ч.ход("А сколько времени на само ревью?")
        self.assertEqual((х2.номер, х2.разбор.вопрос_для_поиска), (2, "Сколько времени занимает ревью?"))
        self.assertEqual(х2.цель, "разобраться с правилами код-ревью", "цель держится")
        разбор = [в for в in клиент.вызовы if в.get("вид") == "разбор"][-1]["messages"][1]["content"]
        self.assertIn("Цель разговора (реплика 1): разобраться с правилами код-ревью", разбор)
        self.assertIn("Ассистент: До мержа нужно минимум 2 апрува", разбор)
        ответ = [в for в in клиент.вызовы if '"цитаты": [{"n": 1' in в["messages"][0]["content"]][-1]["messages"]
        self.assertIn(rag_answer.ПРАВИЛА_ЧАТА, ответ[0]["content"])
        self.assertIn("Контекст разговора (не источник фактов):", ответ[1]["content"])
        self.assertIn("Реплика пользователя дословно: А сколько времени на само ревью?", ответ[1]["content"])
        self.assertTrue(ответ[1]["content"].rstrip().endswith("Вопрос: Сколько времени занимает ревью?"))
        self.assertEqual(len(ч.память.выяснено), 2)

    def test_ход_по_памяти_опора_и_числа(self):
        клиент = _КлиентДня25(разборы={"апрувов": _ЦЕЛЬ_РЕВЬЮ, "цель": {
            "вид": "разговор", "вопрос_для_поиска": "правила код-ревью апрувы"}},
            по_памяти=['{"ответ": "Нужно 5 апрувов.", "опора": ["Ц"]}',
                       '{"ответ": "Цель — разобраться с ревью; до мержа 2 апрува.", "опора": ["Ц", "B1", "Х7"]}'])
        ч = self.чат(клиент)
        ч.ход("Мне нужно разобраться с правилами код-ревью. Сколько апрувов нужно до мержа?")
        х = ч.ход("Какая у нас цель и что выяснили?")
        self.assertEqual((х.вид, х.починка), ("разговор", True))
        self.assertEqual(х.опора, ["Ц", "В1"], "латинская B — та же «В», чужой код отброшен")
        self.assertIn("в «опора» коды, которых нет в памяти: Х7", х.нарушения)
        self.assertNotIn("числа", " ".join(х.нарушения), "после повтора чисел без опоры нет")
        память = [и for и in х.источники if и["вид"] == "память"]
        self.assertEqual([и["код"] for и in память], ["Ц", "В1"])
        self.assertEqual(память[1]["документы"][0]["source"], "политика.md")
        self.assertTrue(х.найдено, "поиск по базе идёт и на реплике о разговоре")
        self.assertGreater(х.кандидатов, 0)
        починка = [в for в in клиент.вызовы if в.get("вид") == "по памяти"][-1]["messages"][-1]["content"]
        self.assertIn("числа, которых нет в записях из «опора»: 5", починка)
        self.assertIn("В1. (реплика 1) Вопрос: Сколько апрувов нужно до мержа?",
                      [в for в in клиент.вызовы if в.get("вид") == "по памяти"][0]["messages"][1]["content"])

    def test_слабый_контекст_не_знаю_с_проверенными_разделами(self):
        ч = self.чат(_КлиентДня25(разборы={"апрувов": _ЦЕЛЬ_РЕВЬЮ}))
        ч.ход("Мне нужно разобраться с правилами код-ревью. Сколько апрувов нужно до мержа?")
        х = ч.ход("Что с ним делать дальше?")
        self.assertEqual((х.не_знаю, х.не_знаю_кто), (True, "порог"))
        self.assertTrue(х.уточнение)
        self.assertTrue(х.источники and all(и["вид"] == "проверено" for и in х.источники))
        self.assertEqual(х.цель, "разобраться с правилами код-ревью", "ловушка цель не сбивает")
        self.assertEqual(len(ч.память.выяснено), 1, "«не знаю» в выяснено не попадает")

    def test_сбой_разбора_запасной_путь_кодом(self):
        ч = self.чат(_КлиентДня25(разборы={"апрувов": _ЦЕЛЬ_РЕВЬЮ, "ну а": "не JSON вовсе"}))
        ч.ход("Мне нужно разобраться с правилами код-ревью. Сколько апрувов нужно до мержа?")
        х = ч.ход("ну а сроки?")
        self.assertTrue(х.разбор.ошибка)
        self.assertEqual(х.разбор.вопрос_для_поиска, "ну а сроки? (разобраться с правилами код-ревью)")
        self.assertEqual((х.изменения, х.вид), ({}, "документы"))
        self.assertTrue(х.есть_источники)

    def test_подвопросы_ищутся_по_отдельности_и_сливаются(self):
        разбор = {"вид": "документы", "вопрос_для_поиска": "Сколько апрувов и сколько времени на ревью?",
                  "подвопросы": ["Сколько апрувов нужно до мержа?", "Сколько времени занимает ревью?"]}
        клиент = _КлиентДня25(разборы={"и сколько": разбор})
        ч = self.чат(клиент)
        х = ч.ход("Сколько апрувов и сколько времени на ревью?")
        отбор = х.ответ_rag.отбор
        self.assertEqual([э["вопрос"] for э in отбор.этапы if э.get("этап") == "подвопрос"], разбор["подвопросы"])
        ид = [н.чанк.chunk_id for н in отбор.фрагменты]
        self.assertEqual(len(ид), len(set(ид)), "фрагменты без повторов")
        ответ = [в for в in клиент.вызовы if '"цитаты": [{"n": 1' in в["messages"][0]["content"]][-1]["messages"]
        self.assertIn("Вопрос: Сколько апрувов нужно до мержа?\nСколько времени занимает ревью?", ответ[1]["content"])
        # Слияние напрямую: лучшие места каждого подвопроса идут первыми, по очереди.
        п = ч.отвечатель.поисковик
        а, б = п.найти("Сколько апрувов нужно до мержа?", "фильтр"), п.найти("Отпуск согласуют?", "фильтр")
        слитый = chat_mod.слить_отборы("оба", [а, б])
        self.assertEqual(слитый.фрагменты[0].чанк.chunk_id, а.фрагменты[0].чанк.chunk_id)
        self.assertEqual(слитый.фрагменты[1].чанк.chunk_id,
                         next(н.чанк.chunk_id for н in б.фрагменты if н.чанк.chunk_id != а.фрагменты[0].чанк.chunk_id))
        self.assertTrue(all(к.судьба == "в промпт" for к in слитый.кандидаты
                            if к.находка.чанк.chunk_id in {н.чанк.chunk_id for н in слитый.фрагменты}))

    def test_новый_разговор_стирает_историю_и_память(self):
        ч = self.чат(_КлиентДня25(разборы={"апрувов": _ЦЕЛЬ_РЕВЬЮ}))
        ч.ход("Мне нужно разобраться с правилами код-ревью. Сколько апрувов нужно до мержа?")
        итог = ч.новый()
        self.assertEqual((итог["сообщений_стёрто"], итог["память_стёрта"]), (2, True))
        self.assertEqual((ч.история(), ч.память.пуста), ([], True))
        with self.assertRaises(ValueError):
            ч.ход("   ")


class СценарииЧата(unittest.TestCase):
    """scenarios.py: проверки кодом по ходам, судья, отчёт."""

    СЦЕНАРИЙ = {"id": "t1", "сессия": "тест-сценарий", "название": "Код-ревью", "цель_ключи": ["ревью"],
                "ходы": [
                    {"реплика": "Мне нужно разобраться с правилами код-ревью. Сколько апрувов нужно до мержа?",
                     "вид": "документы", "факты": ["2 апрува"], "источник": "политика", "цель": True},
                    {"реплика": "Ограничение: отвечай одной фразой.", "вид": "разговор", "память": ["одной фразой"]},
                    {"реплика": "Что с ним делать дальше?", "вид": "документы", "не_знаю": True}]}

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="чат-сценарии-")
        cls.поисковик = _поисковик_для_rag(cls.корень, _КлиентДня25(), настройки={
            "реранкер": "эвристика", "k_до": 10, "k_после": 3, "порог": 0.0, "порог_уверенности": 0.3})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def агент(self, клиент):
        корень, поисковик = self.корень, self.поисковик

        class Агент:
            model_key = "ds-flash"
            client = клиент

            def чат(self, сессия):
                о = rag_answer.Отвечатель(поисковик.индексатор, клиент, порог=0.0)
                о._поисковик = rag_retrieval.Поисковик(поисковик.индексатор, клиент,
                                                       путь_настроек_=поисковик.путь_настроек)
                return chat_mod.Чат(о, клиент, ShortTermMemory(os.path.join(корень, "dialog.db")),
                                    chat_state.ХранилищеЧатов(os.path.join(корень, "memory", "chat")), сессия)
        return Агент()

    def клиент(self):
        return _КлиентДня25(разборы={
            "апрувов": _ЦЕЛЬ_РЕВЬЮ,
            "Ограничение": {"вид": "разговор", "вопрос_для_поиска": "правила код-ревью",
                            "ограничения": ["отвечать одной фразой"]}})

    def test_сценарий_проходит_и_отчёт_пишется(self):
        ход = []
        прогон = chat_scenarios.прогнать(self.агент(self.клиент()), [self.СЦЕНАРИЙ], True,
                                         lambda этап, сделано, всего, что: ход.append((этап, сделано, всего)))
        с = прогон.сценарии[0]
        итог = с.итог()
        self.assertEqual((итог["ходов"], итог["с_источниками"], итог["провалов"]), (3, 3, 0),
                         [(п.имя, п.пояснение) for пп in с.пункты for п in пп if not п.ок])
        self.assertEqual((итог["факты"], итог["не_знаю"], итог["память"], итог["цель_на_месте"]),
                         ((1, 1), (1, 1), (1, 1), (2, 2)))
        self.assertEqual((итог["цель_ключи"], итог["судья"]), (True, "да"))
        self.assertEqual(ход[0], ("сценарий t1", 0, 3))
        self.assertEqual(ход[-1], ("сценарий t1", 3, 3))
        путь = chat_scenarios.записать(прогон, os.path.join(self.корень, "memory"))
        with open(путь, encoding="utf-8") as f:
            отчёт = f.read()
        self.assertEqual(итог["с_поиском"], 3)
        for кусок in ("# Мини-чат с RAG и памятью задачи", "| ответов с источниками | 3 из 3 |",
                      "| поиск по базе на реплике | 3 из 3 |", "✓ поиск по базе",
                      "| судья: цель удержана | да |", "**Цель** (реплика 1): разобраться с правилами код-ревью",
                      "✓ источники", "проверено: политика.md"):
            self.assertIn(кусок, отчёт)
        данные = chat_scenarios.прочитать(os.path.join(self.корень, "memory"))
        self.assertEqual(данные["scenarios"][0]["totals"]["провалов"], 0)
        self.assertNotIn("журнал", данные["scenarios"][0]["memory"])

    def test_провал_ожидания_виден(self):
        сценарий = _json.loads(_json.dumps(self.СЦЕНАРИЙ))
        сценарий["ходы"][0]["факты"] = ["3 апрува"]
        сценарий["ходы"][1]["вид"] = "документы"
        прогон = chat_scenarios.прогнать(self.агент(self.клиент()), [сценарий], False)
        провалы = [(п.имя, п.пояснение) for пп in прогон.сценарии[0].пункты for п in пп if not п.ок]
        self.assertIn(("факты", "нет: 3 апрува"), провалы)
        self.assertIn(("вид", "понято «разговор», ожидалось «документы»"), провалы)
        self.assertEqual(прогон.сценарии[0].судья, {})

    def test_судья_разбирается_и_молчит_честно(self):
        клиент = _КлиентДня25(судья_чата="не JSON")
        память = chat_state.ПамятьЗадачи("x")
        вердикт = chat_scenarios.судить(клиент, self.СЦЕНАРИЙ, [], память)
        self.assertEqual(вердикт["цель_удержана"], "")
        self.assertIn("вердикт не разобран", вердикт["пояснение"])
        вердикт = chat_scenarios.судить(_КлиентДня25(судья_чата=_СУДЬЯ_ЧАТА_ДА.replace("[]", '[2, "x"]')),
                                        self.СЦЕНАРИЙ, [], память)
        self.assertEqual((вердикт["цель_удержана"], вердикт["ходы_с_проблемами"]), ("да", [2]))

    def test_файл_сценариев_дня(self):
        сценарии = chat_scenarios.загрузить()
        self.assertEqual([с["id"] for с in сценарии], ["s1", "s2"])
        for с in сценарии:
            self.assertTrue(10 <= len(с["ходы"]) <= 15, с["id"])
            self.assertTrue(с["ходы"][0].get("цель"))
            self.assertTrue(any(х.get("не_знаю") for х in с["ходы"]), "в каждом сценарии ловушка")
            self.assertTrue(any(х.get("память") for х in с["ходы"]), "в каждом сценарии ограничения/термины")
            self.assertTrue(с["ходы"][-1].get("выяснено"), "итог в конце держится на выясненном")


class КонсольИВебДня25(unittest.TestCase):
    """Ключи консоли, печать хода, ручки и страница мини-чата."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="чат-веб25-")
        cls.прежние = {к: os.environ.get(к) for к in
                       ("MEMORY_DIR", "RAG_DIR", "DOCS_DIR", "RAG_EMBEDDER", "RAG_SETTINGS", "CHAT_SCENARIOS")}
        os.environ["MEMORY_DIR"] = os.path.join(cls.корень, "memory")
        os.environ["RAG_DIR"] = os.path.join(cls.корень, "index")
        os.environ["DOCS_DIR"] = _набор_документов(os.path.join(cls.корень, "docs"))
        os.environ["RAG_EMBEDDER"] = "хэш"
        for имя, переменная, данные in (
                ("rag-settings.json", "RAG_SETTINGS", {"когда": "тест", "выбрано": {
                    "реранкер": "эвристика", "k_до": 10, "k_после": 3, "порог": 0.0,
                    "порог_косинуса": 0.0, "порог_уверенности": 0.3}}),
                ("сценарии.json", "CHAT_SCENARIOS", {"сценарии": [СценарииЧата.СЦЕНАРИЙ]})):
            путь = os.path.join(cls.корень, имя)
            with open(путь, "w", encoding="utf-8") as f:
                _json.dump(данные, f, ensure_ascii=False)
            os.environ[переменная] = путь
        rag.Индексатор(эмбеддер="хэш", ocr=False).построить()
        import importlib
        import web as веб_модуль
        cls.веб = importlib.reload(веб_модуль)
        cls.веб.agent.client = _КлиентДня25(разборы={
            "апрувов": _ЦЕЛЬ_РЕВЬЮ,
            "Ограничение": {"вид": "разговор", "вопрос_для_поиска": "правила код-ревью",
                            "ограничения": ["отвечать одной фразой"]}})
        cls.клиент = cls.веб.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.веб.agent.close()
        for к, з in cls.прежние.items():
            if з is None:
                os.environ.pop(к, None)
            else:
                os.environ[к] = з
        shutil.rmtree(cls.корень, ignore_errors=True)

    def _дождаться(self) -> dict:
        for _ in range(400):
            работа = self.клиент.get("/api/chat?session=x").get_json()["работа"]
            if работа and работа["готово"]:
                return работа
            time.sleep(0.05)
        self.fail("сценарии не закончились")

    def test_ключи_консоли(self):
        import cli
        справка = cli.build_parser().format_help()
        for ключ in ("--чат", "--реплика", "--память-чата", "--история-чата", "--новый-чат", "--чаты",
                     "--сценарии-чата", "--файл-сценариев"):
            self.assertIn(ключ, справка)
        разбор = cli.build_parser().parse_args(["--чат", "договоры", "--реплика", "а", "--реплика", "б"])
        self.assertEqual((разбор.chat, разбор.chat_say), ("договоры", ["а", "б"]))
        self.assertEqual(cli.build_parser().parse_args(["--чат"]).chat, "основная")
        for ключи in (["--чат"], ["--память-чата"], ["--история-чата", "x"], ["--новый-чат"], ["--чаты"],
                      ["--сценарии-чата"], ["--сценарии-чата", "s1"]):
            self.assertTrue(cli.меняет_состояние(cli.build_parser().parse_args(ключи)), ключи)
        self.assertIsNone(cli.build_parser().parse_args([]).chat)

    def test_консоль_ведёт_чат_без_диалога_и_печатает_источники(self):
        import io
        import cli
        аргументы = cli.build_parser().parse_args(
            ["--чат", "кли", "--реплика", "Мне нужно разобраться с правилами код-ревью. Сколько апрувов нужно до мержа?",
             "--реплика", "Ограничение: отвечай одной фразой."])
        вывод = io.StringIO()
        with mock.patch("sys.stdout", вывод), mock.patch("builtins.input", side_effect=AssertionError("диалог")):
            self.assertEqual(cli.выполнить(self.веб.agent, аргументы), 0)
        текст = вывод.getvalue()
        for кусок in ("Реплика 1:", "↳ понято: документы · искали: «Сколько апрувов нужно до мержа?»",
                      "↳ память задачи: + цель «разобраться с правилами код-ревью»",
                      f"✓ дословно «{_ЦИТАТА_АПРУВЫ}»", "Источники:\n  [1] политика.md",
                      "Реплика 2:", "↳ память задачи: + ограничение «отвечать одной фразой»",
                      "Источники — память задачи:", "Цель: разобраться с правилами код-ревью"):
            self.assertIn(кусок, текст)
        вывод = io.StringIO()
        with mock.patch("sys.stdout", вывод):
            cli.выполнить(self.веб.agent, cli.build_parser().parse_args(["--память-чата", "кли"]))
            cli.выполнить(self.веб.agent, cli.build_parser().parse_args(["--чаты"]))
            cli.выполнить(self.веб.agent, cli.build_parser().parse_args(["--история-чата", "кли"]))
            cli.выполнить(self.веб.agent, cli.build_parser().parse_args(["--новый-чат", "кли"]))
        текст = вывод.getvalue()
        for кусок in ("Ц. Цель разговора (реплика 1): разобраться с правилами код-ревью",
                      "О1. отвечать одной фразой (реплика 2)", "В1. (реплика 1) Сколько апрувов нужно до мержа?",
                      "кли", "История чата «кли»: реплик 4", "Чат «кли» начат заново: стёрто реплик 4",
                      "Пользователь: Мне нужно разобраться"):
            self.assertIn(кусок, текст)
        self.assertNotIn("Вы:", текст, "«Вы:» — приглашение диалога, в выводе истории его быть не должно")

    def test_ручки_чата(self):
        с = "веб"
        ответ = self.клиент.post("/api/chat/say", json={
            "session": с, "text": "Мне нужно разобраться с правилами код-ревью. Сколько апрувов нужно до мержа?"})
        self.assertEqual(ответ.status_code, 200)
        данные = ответ.get_json()
        ход = данные["turn"]
        self.assertEqual((ход["turn"], ход["parse"]["kind"], ход["sources"][0]["вид"]), (1, "документы", "документ"))
        self.assertNotIn("rag", ход)
        self.assertEqual(данные["state"]["память"]["цель"]["текст"], "разобраться с правилами код-ревью")
        состояние = self.клиент.get("/api/chat?session=" + с).get_json()
        self.assertEqual((len(состояние["ходы"]), len(состояние["история"])), (1, 2))
        self.assertEqual(состояние["ходы"][0]["sources"][0]["source"], "политика.md")
        self.assertIn(с, [ч["сессия"] for ч in состояние["сессии"]])
        self.assertEqual(self.клиент.post("/api/chat/say", json={"session": с, "text": " "}).status_code, 400)
        сброс = self.клиент.post("/api/chat/new", json={"session": с}).get_json()
        self.assertEqual((сброс["reset"]["сообщений_стёрто"], сброс["state"]["ходы"]), (2, []))

    def test_сценарии_в_фоне(self):
        self.assertEqual(self.клиент.post("/api/chat/scenarios", json={"ids": ["нет"]}).status_code, 400)
        self.assertEqual(self.клиент.post("/api/chat/scenarios", json={"judge": True}).status_code, 202)
        работа = self._дождаться()
        self.assertEqual((работа["ошибка"], работа["вид"]), ("", "сценарии"))
        прогон = self.клиент.get("/api/chat?session=x").get_json()["сценарии"]
        self.assertEqual([с["id"] for с in прогон["scenarios"]], ["t1"])
        self.assertEqual(прогон["scenarios"][0]["totals"]["провалов"], 0)
        self.assertTrue(os.path.exists(os.path.join(os.environ["MEMORY_DIR"], "chat", "CHAT-SCENARIOS.md")),
                        "отчёт чужой памяти — рядом с ней, а не в корне репозитория")

    def test_страница_дня_25(self):
        страница = self.клиент.get("/chat").get_data(as_text=True)
        for узел in ('id="чат-сессия"', 'id="чат-новый"', 'id="чат-модель"', 'id="чат-лента"', 'id="чат-реплика"',
                     'id="чат-отправить"', 'id="память-задачи"', 'id="сценарии-запустить"', 'id="сценарии-судья"',
                     'id="сценарии-выбор"', 'id="сценарии-ход"', 'id="сценарии"', "❓ Справка по форме",
                     "<title>Мини-чат: RAG и память задачи"):
            self.assertIn(узел, страница)
        главная = self.клиент.get("/").get_data(as_text=True)
        self.assertIn('id="чат-ссылка-25"', главная)
        self.assertIn("<title>Агент миграции ГИС —", главная)
        self.assertIn('id="чат-ссылка-rag"', self.клиент.get("/rag").get_data(as_text=True))


# --- День 26: локальная модель (Ollama) ----------------------------------------

import httpx as _httpx  # noqa: E402

from agent import llm as llm_mod  # noqa: E402
from agent.local import bench as local_bench, ollama as local_ollama, stream as local_stream  # noqa: E402


def _ollama_ответ(текст="Париж", **ещё):
    """Последний (или единственный) объект ответа /api/chat с замерами в наносекундах."""
    данные = {"model": "qwen3:4b", "message": {"role": "assistant", "content": текст},
              "done": True, "done_reason": "stop", "total_duration": 3_000_000_000,
              "load_duration": 1_000_000_000, "prompt_eval_count": 20,
              "prompt_eval_duration": 500_000_000, "eval_count": 10, "eval_duration": 1_000_000_000}
    данные.update(ещё)
    return данные


class _Ollama:
    """Поддельная Ollama за httpx.MockTransport: запоминает запросы, отвечает по сценарию."""

    def __init__(self, ответ=None, код=200, поток=None):
        self.запросы: list[_httpx.Request] = []
        self.ответ = ответ if ответ is not None else _ollama_ответ()
        self.код = код
        self.поток = поток

    def __call__(self, запрос: _httpx.Request) -> _httpx.Response:
        self.запросы.append(запрос)
        if self.поток is not None:
            return _httpx.Response(self.код, content="".join(self.поток).encode("utf-8"))
        return _httpx.Response(self.код, json=self.ответ)

    def клиент(self) -> _httpx.Client:
        return _httpx.Client(transport=_httpx.MockTransport(self))

    def тело(self, n: int = -1) -> dict:
        return _json.loads(self.запросы[n].content)


class ЛокальныйКаталог(unittest.TestCase):
    """catalog.py: локальные модели без ключа, «скачана ли» — у Ollama."""

    def setUp(self):
        catalog.forget_installed()

    def tearDown(self):
        catalog.forget_installed()

    def test_локальная_модель_без_ключа_и_бесплатно(self):
        м = catalog.get("loc-qwen3")
        self.assertTrue(м.local and м.has_key and м.free)
        self.assertEqual((м.api_key, м.price_in, м.price_out, м.price_text), ("", 0.0, 0.0, "локально, бесплатно"))
        # Адрес — из OLLAMA_HOST (по умолчанию 127.0.0.1:11434), а не зашит в тест:
        # у человека Ollama может слушать другой адрес.
        self.assertEqual(м.base_url, catalog._OLLAMA)
        self.assertTrue(м.base_url.startswith(("http://", "https://")))
        self.assertEqual(м.context_window, catalog.LOCAL_CTX)
        # День 29: + loc-rag-docs — своя модель, оптимизированная под ответ по договорам.
        self.assertEqual(set(catalog.local_models()), {"loc-qwen35", "loc-qwen3", "loc-qwen17", "loc-gemma3", "loc-phi4",
                                                       "loc-rag-docs"})
        self.assertFalse(catalog.get("ds-flash").local)

    def test_скачана_ли_спрашивает_ollama_с_кэшем(self):
        вызовы = []

        def tags(url, timeout):
            вызовы.append(url)
            return _httpx.Response(200, json={"models": [{"name": "qwen3:4b"}, {"name": "phi4-mini:latest"}]},
                                   request=_httpx.Request("GET", url))
        with mock.patch("httpx.get", side_effect=tags):
            self.assertTrue(catalog.installed("loc-qwen3"))
            self.assertTrue(catalog.installed("loc-phi4"), "имя без тега = :latest")
            self.assertFalse(catalog.installed("loc-gemma3"))
            self.assertTrue(catalog.installed("ds-flash"), "облачная — всегда «скачана»")
            описание = {м["key"]: м for м in catalog.describe()}
        self.assertEqual(len(вызовы), 1, "один запрос к Ollama на весь каталог")
        self.assertTrue(вызовы[0].endswith("/api/tags"))
        self.assertTrue(описание["loc-qwen3"]["available"] and описание["loc-qwen3"]["local"])
        self.assertFalse(описание["loc-gemma3"]["available"])

    def test_ollama_молчит_локальные_недоступны(self):
        with mock.patch("httpx.get", side_effect=_httpx.ConnectError("нет")) as запрос:
            self.assertFalse(catalog.installed("loc-qwen3"))
            self.assertNotIn("loc-qwen3", catalog.available())
            catalog.describe()
        self.assertEqual(запрос.call_count, 1, "отказ Ollama тоже кэшируется: страница не ждёт по 2 с на модель")


class КлиентOllama(unittest.TestCase):
    """llm.py: локальная модель — родной /api/chat, окно и think на запрос, замеры Ollama."""

    def вызвать(self, ollama: _Ollama, **параметры):
        клиент = llm_mod.Client()
        клиент._http = ollama.клиент()
        try:
            return клиент.call("loc-qwen3", параметры.pop("сообщения", [{"role": "user", "content": "Столица Франции?"}]),
                               **параметры)
        finally:
            клиент.close()

    def test_запрос_в_родной_api_без_ключа(self):
        ollama = _Ollama()
        ответ = self.вызвать(ollama, max_tokens=300, temperature=0.0)
        запрос = ollama.запросы[0]
        self.assertEqual(запрос.url.path, "/api/chat")
        self.assertNotIn("authorization", {к.lower() for к in запрос.headers})
        тело = ollama.тело()
        self.assertEqual((тело["model"], тело["stream"], тело["think"]), ("qwen3:4b", False, False))
        self.assertEqual(тело["options"], {"num_ctx": catalog.LOCAL_CTX, "temperature": 0.0, "num_predict": 300})
        self.assertEqual(тело["keep_alive"], llm_mod.LOCAL_KEEP_ALIVE)
        self.assertEqual((ответ.text, ответ.prompt_tokens, ответ.completion_tokens, ответ.cost), ("Париж", 20, 10, 0.0))
        self.assertEqual(ответ.timings, {"load_s": 1.0, "prompt_s": 0.5, "gen_s": 1.0, "total_s": 3.0,
                                         "ttft_s": 1.5, "tok_s": 10.0, "prompt_tok_s": 40.0})

    def test_ответ_не_длиннее_половины_окна(self):
        ollama = _Ollama()
        self.вызвать(ollama, max_tokens=100_000)
        self.assertEqual(ollama.тело()["options"]["num_predict"], catalog.LOCAL_CTX // 2)

    def test_размышления_отдельно_от_ответа(self):
        ollama = _Ollama(_ollama_ответ("<think>считаю 17*23</think>\n391", message={
            "role": "assistant", "content": "<think>считаю 17*23</think>\n391", "thinking": ""}))
        ответ = self.вызвать(ollama)
        self.assertEqual((ответ.text, ответ.thinking), ("391", "считаю 17*23"))
        ollama = _Ollama(_ollama_ответ(message={"role": "assistant", "content": "391", "thinking": "17*23=391"}))
        self.assertEqual(self.вызвать(ollama).thinking, "17*23=391")

    def test_инструменты_туда_и_обратно(self):
        ollama = _Ollama(_ollama_ответ(message={"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "tracker__get_issue", "arguments": {"key": "MIG-1"}}}]}))
        сообщения = [{"role": "user", "content": "задача?"},
                     {"role": "assistant", "content": "", "tool_calls": [
                         {"id": "c7", "type": "function", "function": {"name": "tracker__list_issues", "arguments": '{"limit": 2}'}}]},
                     {"role": "tool", "tool_call_id": "c7", "content": "MIG-1"}]
        ответ = self.вызвать(ollama, сообщения=сообщения, tools=[{"type": "function", "function": {"name": "x"}}])
        self.assertEqual((ответ.tool_calls[0].имя, ответ.tool_calls[0].аргументы), ("tracker__get_issue", {"key": "MIG-1"}))
        self.assertEqual(_json.loads(ответ.message["tool_calls"][0]["function"]["arguments"]), {"key": "MIG-1"})
        тело = ollama.тело()
        self.assertEqual(тело["messages"][1]["tool_calls"][0]["function"]["arguments"], {"limit": 2},
                         "аргументы в родном API — объект, а не строка")
        self.assertEqual(тело["messages"][2]["tool_name"], "tracker__list_issues")
        self.assertIn("tools", тело)

    def test_понятные_ошибки(self):
        with self.assertRaisesRegex(llm_mod.LLMError, "не скачана в Ollama. Скачайте: ollama pull qwen3:4b"):
            self.вызвать(_Ollama({"error": "model 'qwen3:4b' not found"}, код=404))
        with self.assertRaisesRegex(llm_mod.LLMError, "не умеет вызывать инструменты"):
            self.вызвать(_Ollama({"error": "registry.ollama.ai/library/gemma3:4b does not support tools"}, код=400))
        with self.assertRaisesRegex(llm_mod.LLMError, "пустой ответ"):
            self.вызвать(_Ollama(_ollama_ответ("")))

        def нет_связи(запрос):
            raise _httpx.ConnectError("Connection refused")
        клиент = llm_mod.Client()
        клиент._http = _httpx.Client(transport=_httpx.MockTransport(нет_связи))
        with self.assertRaisesRegex(llm_mod.LLMError, "Ollama не отвечает .*systemctl start ollama"):
            клиент.call("loc-qwen3", [{"role": "user", "content": "x"}])


class ПотокЛокальнойИОблачной(unittest.TestCase):
    """stream.py: одинаковый замер для Ollama (NDJSON) и облака (SSE)."""

    def test_поток_ollama(self):
        строки = [_json.dumps({"message": {"thinking": "думаю"}}) + "\n",
                  _json.dumps({"message": {"content": "Па"}}) + "\n", "\n",
                  _json.dumps({"message": {"content": "риж"}}) + "\n",
                  _json.dumps(_ollama_ответ("", message={"content": ""})) + "\n"]
        ollama = _Ollama(поток=строки)
        события = list(local_stream.поток("loc-qwen3", [{"role": "user", "content": "?"}], думать=True,
                                          http=ollama.клиент()))
        self.assertEqual([с["тип"] for с in события], ["думает", "текст", "текст", "итог"])
        итог = события[-1]
        self.assertEqual((итог["text"], итог["thinking"], итог["local"]), ("Париж", "думаю", True))
        self.assertEqual((итог["tokens_in"], итог["tokens_out"], итог["cost"]), (20, 10, 0.0))
        self.assertEqual(итог["server"]["load_s"], 1.0)
        self.assertTrue(ollama.тело()["stream"] and ollama.тело()["think"])

    def test_поток_облака(self):
        куски = ['data: {"choices":[{"delta":{"content":"При"}}]}\n\n', ": keep-alive\n\n",
                 'data: {"choices":[{"delta":{"content":"вет"},"finish_reason":"stop"}]}\n\n',
                 'data: {"choices":[],"usage":{"prompt_tokens":1000000,"completion_tokens":0}}\n\n', "data: [DONE]\n\n"]
        облако = _Ollama(поток=куски)
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}):
            итог = local_stream.спросить("ds-flash", [{"role": "user", "content": "?"}], http=облако.клиент())
        self.assertEqual((итог["text"], итог["local"], итог["finish"], итог["tokens_in"]), ("Привет", False, "stop", 1_000_000))
        self.assertAlmostEqual(итог["cost"], catalog.get("ds-flash").price_in)
        тело = облако.тело()
        self.assertEqual((тело["stream"], тело["stream_options"]), (True, {"include_usage": True}))
        self.assertEqual(облако.запросы[0].headers["authorization"], "Bearer test-key")

    def test_скорость_по_одному_слову_не_считается(self):
        строки = [_json.dumps({"message": {"content": "36"}}) + "\n",
                  _json.dumps(_ollama_ответ("", message={"content": ""}, eval_count=2)) + "\n"]
        итог = local_stream.спросить("loc-qwen3", [{"role": "user", "content": "?"}], http=_Ollama(поток=строки).клиент())
        self.assertEqual((итог["text"], итог["tok_s"]), ("36", 0.0))
        self.assertGreater(итог["server"]["tok_s"], 0, "у Ollama своя скорость генерации — она остаётся")

    def test_пустой_ответ_ошибка(self):
        ollama = _Ollama(поток=[_json.dumps(_ollama_ответ("", message={"content": ""}, done_reason="length")) + "\n"])
        with self.assertRaisesRegex(llm_mod.LLMError, "пустой ответ \\(length\\)"):
            local_stream.спросить("loc-qwen3", [{"role": "user", "content": "?"}], http=ollama.клиент())


class СлужбаOllama(unittest.TestCase):
    """ollama.py: доказательства «работает здесь» из самой Ollama, CLI и сырой HTTP."""

    def _get(self, url, timeout):
        ответы = {"/api/version": {"version": "0.34.4"},
                  "/api/tags": {"models": [{"name": "qwen3:4b", "size": 2_500_000_000, "digest": "359d7dd4bcda99",
                                            "details": {"family": "qwen3", "parameter_size": "4.0B",
                                                        "quantization_level": "Q4_K_M", "format": "gguf"}},
                                           {"name": "bge-m3:latest", "size": 1_160_000_000, "details": {}}]},
                  "/api/ps": {"models": [{"name": "qwen3:4b", "size": 3_400_000_000, "size_vram": 0,
                                          "context_length": 8192, "expires_at": "2026-10-05T16:07:41.1+03:00"}]}}
        путь = "/" + url.split("/", 3)[3]
        return _httpx.Response(200, json=ответы[путь], request=_httpx.Request("GET", url))

    def test_состояние(self):
        with mock.patch("agent.local.ollama.httpx.get", side_effect=self._get), \
                mock.patch("agent.local.ollama.служба", return_value="active"):
            с = local_ollama.состояние()
        self.assertEqual((с["version"], с["service"], с["loopback"], с["error"]), ("0.34.4", "active", True, ""))
        м = с["models"][0]
        self.assertEqual((м["name"], м["size_gb"], м["params"], м["quant"], м["key"]), ("qwen3:4b", 2.5, "4.0B", "Q4_K_M", "loc-qwen3"))
        self.assertEqual(с["models"][1]["key"], "", "эмбеддер в каталоге агента не модель ответа")
        з = с["loaded"][0]
        self.assertEqual((з["processor"], з["context"], з["until"]), ("100% CPU", 8192, "2026-10-05T16:07:41"))
        self.assertEqual({к["key"]: к["installed"] for к in с["catalog"]}["loc-qwen3"], True)
        self.assertEqual({к["key"]: к["installed"] for к in с["catalog"]}["loc-gemma3"], False)

    def test_ollama_молчит(self):
        with mock.patch("agent.local.ollama.httpx.get", side_effect=_httpx.ConnectError("refused")), \
                mock.patch("agent.local.ollama.служба", return_value="inactive"):
            с = local_ollama.состояние()
        self.assertIn("systemctl start ollama", с["error"])
        self.assertEqual((с["models"], с["loaded"]), ([], []))

    def test_доля_gpu(self):
        def ps(url, timeout):
            return _httpx.Response(200, json={"models": [{"name": "a", "size": 100, "size_vram": 100},
                                                         {"name": "b", "size": 100, "size_vram": 25}]},
                                   request=_httpx.Request("GET", url))
        with mock.patch("agent.local.ollama.httpx.get", side_effect=ps):
            self.assertEqual([м["processor"] for м in local_ollama.загружены()], ["100% GPU", "75%/25% CPU/GPU"])

    def test_сведения_окно_и_умения(self):
        ответ = _httpx.Response(200, json={"capabilities": ["completion", "tools", "thinking"],
                                           "details": {"family": "qwen3", "parameter_size": "4.0B", "quantization_level": "Q4_K_M"},
                                           "model_info": {"qwen3.context_length": 262144, "general.parameter_count": 4022468096},
                                           "parameters": "temperature 0.6"},
                                request=_httpx.Request("POST", "http://x/api/show"))
        with mock.patch("agent.local.ollama.httpx.post", return_value=ответ):
            с = local_ollama.сведения("qwen3:4b")
        self.assertEqual((с["context_length"], с["capabilities"][1], с["parameter_count"]), (262144, "tools", 4022468096))

    def test_через_cli(self):
        готово = _subprocess.CompletedProcess(
            ["ollama"], 0, stdout="391\n\n",
            stderr="\x1b[?25l⠙ \x1b[?25h\ntotal duration:       2.1s\nload duration:        10ms\n"
                   "prompt eval count:    30 token(s)\neval rate:            4.40 tokens/s\n")
        with mock.patch("agent.local.ollama.shutil.which", return_value="/usr/local/bin/ollama"), \
                mock.patch("agent.local.ollama.subprocess.run", return_value=готово) as запуск:
            р = local_ollama.через_cli("qwen3:4b", "17*23?")
        self.assertEqual(запуск.call_args[0][0], ["ollama", "run", "qwen3:4b", "17*23?", "--think=false",
                                                  "--verbose", "--nowordwrap"])
        self.assertEqual((р["code"], р["stdout"]), (0, "391"))
        self.assertEqual(р["timings"], {"total duration": "2.1s", "load duration": "10ms",
                                        "prompt eval count": "30 token(s)", "eval rate": "4.40 tokens/s"})
        self.assertIn('ollama run qwen3:4b "17*23?"', р["command"])

    def test_через_http_родной_и_openai(self):
        запросы = []

        def post(url, json, timeout):
            запросы.append((url, json))
            if url.endswith("/api/chat"):
                данные = _ollama_ответ("391")
            else:
                данные = {"choices": [{"message": {"content": "391"}}], "usage": {"prompt_tokens": 9}}
            return _httpx.Response(200, json=данные, request=_httpx.Request("POST", url))
        with mock.patch("agent.local.ollama.httpx.post", side_effect=post):
            родной = local_ollama.через_http("native", "qwen3:4b", "17*23?")
            openai = local_ollama.через_http("openai", "qwen3:4b", "17*23?")
        self.assertTrue(запросы[0][0].endswith("/api/chat") and запросы[1][0].endswith("/v1/chat/completions"))
        self.assertEqual(запросы[0][1]["think"], False)
        self.assertNotIn("options", запросы[0][1], "окно Ollama по умолчанию — как у CLI, без перезагрузки модели")
        self.assertEqual((родной["text"], openai["text"], родной["status"]), ("391", "391", 200))
        self.assertTrue(родной["curl"].startswith("curl http://") and "/api/chat" in родной["curl"])
        with self.assertRaises(ValueError):
            local_ollama.через_http("grpc", "qwen3:4b", "?")


class ЗамерЛокальных(unittest.TestCase):
    """bench.py: задачи трёх уровней, проверка кодом, выбор основной модели, отметки."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="лок-замер-")
        cls.задачи = local_bench.загрузить_задачи()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.корень, ignore_errors=True)

    def задача(self, ид):
        return next(з for з in self.задачи if з["id"] == ид)

    def test_файл_задач_дня(self):
        self.assertEqual([з["id"] for з in self.задачи], ["p1", "p2", "s1", "s2", "c1", "c2"])
        for уровень in local_bench.УРОВНИ:
            self.assertEqual(sum(з["уровень"] == уровень for з in self.задачи), 2, уровень)
        путь = os.path.join(self.корень, "плохие.json")
        with open(путь, "w", encoding="utf-8") as f:
            _json.dump({"задачи": [{"id": "x", "уровень": "лёгкий", "вопрос": "?", "проверка": {}}]}, f)
        with self.assertRaisesRegex(ValueError, "нужны уровень"):
            local_bench.загрузить_задачи(путь)

    def test_проверка_кодом(self):
        self.assertEqual(local_bench.проверить(self.задача("p1"), "Париж."), (True, "всё на месте"))
        self.assertFalse(local_bench.проверить(self.задача("p1"), "Лондон")[0])
        self.assertTrue(local_bench.проверить(self.задача("p2"), "Telecommunication services contract")[0])
        self.assertEqual(local_bench.проверить(self.задача("s1"), "С 2025 по 2027 — 36 платежей."),
                         (True, "ответ 36, ожидалось 36"))
        self.assertFalse(local_bench.проверить(self.задача("s1"), "36 месяцев, то есть 3 года")[0],
                         "засчитывается последнее число ответа")
        self.assertTrue(local_bench.проверить(self.задача("s2"), "Итого 1 112,21 тыс. руб.")[0])
        self.assertFalse(local_bench.проверить(self.задача("s2"), "1112,2")[0])
        хорошо = ('```json\n{"кто_платит": "Заказчик", "срок_оплаты_дней": 30, "дни": "календарные", '
                  '"отсчёт_от": "даты выставления счёта", "неустойка_процент_в_день": "0,2%", "валюта": "рубли"}\n```')
        self.assertEqual(local_bench.проверить(self.задача("c1"), хорошо), (True, "полей верно 6 из 6"))
        ок, пояснение = local_bench.проверить(self.задача("c1"), хорошо.replace("календарные", "рабочие"))
        self.assertFalse(ок)
        self.assertIn("дни='рабочие'", пояснение)
        self.assertEqual(local_bench.проверить(self.задача("c1"), "не JSON"), (False, "ответ не разбирается как объект JSON"))

    def test_проверка_ответа_по_документам(self):
        контекст = {"тексты": ["2.2.7. извещать Абонента об изменении тарифов не менее чем за 10 дней до введения изменений;",
                               "Раздел о ВымпелКоме: за 30 дней"],
                    "источники": ["Ростелеком VPN ВОГ396658.pdf", "Вымпелком.pdf"]}
        хорошо = _json.dumps({"ответ": "Не менее чем за 10 дней [1].", "источники": [1], "цитаты": [
            {"n": 1, "текст": "извещать Абонента об изменении тарифов не менее чем за 10 дней"}]}, ensure_ascii=False)
        self.assertEqual(local_bench.проверить(self.задача("c2"), хорошо, контекст),
                         (True, "цитат подтверждено 1 из 1"))
        выдумка = хорошо.replace("извещать Абонента", "оператор обязан сообщать абоненту")
        self.assertFalse(local_bench.проверить(self.задача("c2"), выдумка, контекст)[0])
        чужой = _json.dumps({"ответ": "За 30 дней [2].", "источники": [2], "цитаты": [
            {"n": 2, "текст": "Раздел о ВымпелКоме: за 30 дней"}]}, ensure_ascii=False)
        ок, пояснение = local_bench.проверить(self.задача("c2"), чужой, контекст)
        self.assertFalse(ок)
        self.assertIn("цитата не из нужного документа", пояснение)
        self.assertEqual(local_bench.проверить(self.задача("c2"), '{"не_знаю": true, "уточнение": "?"}', контекст),
                         (False, "модель ответила «не знаю»"))

    def _прогон(self):
        """Прогон на поддельном потоке: локальная qwen3 верна везде, gemma — в половине, облако — везде."""
        задачи = [з for з in self.задачи if з["проверка"]["вид"] != "rag"]
        ответы = {"p1": "Париж", "p2": "telecommunication services contract", "s1": "36", "s2": "1112.21",
                  "c1": '{"кто_платит": "Заказчик", "срок_оплаты_дней": 30, "дни": "календарные", '
                        '"отсчёт_от": "счёта", "неустойка_процент_в_день": 0.2, "валюта": "руб"}'}

        def спросить(ключ, сообщения, предел=0, температура=0.2, **_):
            вопрос = сообщения[-1]["content"]
            if вопрос == local_bench.ПРОГРЕВ:
                return {"text": "да", "server": {"load_s": 12.5}, "total_s": 13.0}
            ид = next(з["id"] for з in задачи if з["вопрос"] == вопрос)
            текст = ответы[ид] if ключ != "loc-gemma3" or ид in ("p1", "s1") else "не знаю"
            return {"тип": "итог", "model": ключ, "text": текст, "ttft_s": 0.5 if ключ == "ds-flash" else 4.0,
                    "total_s": 6.0, "tok_s": {"loc-qwen3": 6.0, "loc-gemma3": 9.0, "ds-flash": 60.0}[ключ],
                    "tokens_in": 30, "tokens_out": 5, "cost": 0.0001 if ключ == "ds-flash" else 0.0, "server": {}}
        ход = []
        with mock.patch("agent.local.bench.stream.спросить", side_effect=спросить), \
                mock.patch("agent.local.bench.ollama.выгрузить") as выгрузить:
            прогон = local_bench.прогнать(["loc-qwen3", "loc-gemma3", "ds-flash"], задачи,
                                          lambda *а: ход.append(а))
        self.assertEqual([в.args[0] for в in выгрузить.call_args_list], ["qwen3:4b", "gemma3:4b"],
                         "после своих задач локальная модель выгружается; облако — нет")
        return прогон, ход

    def test_прогон_и_выбор_основной(self):
        прогон, ход = self._прогон()
        и = прогон.итог("loc-qwen3")
        self.assertEqual((и["верно"], и["задач"], и["загрузка_s"], и["tok_s"]), (5, 5, 12.5, 6.0))
        self.assertEqual(и["по_уровням"], {"простой": (2, 2), "средний": (2, 2), "сложный": (1, 1)})
        self.assertEqual(прогон.итог("loc-gemma3")["верно"], 2)
        self.assertEqual(прогон.итог("ds-flash")["загрузка_s"], 0.0, "облако не прогревается")
        выбор = local_bench.выбрать(прогон)
        self.assertEqual(выбор["model"], "loc-qwen3", "больше верных важнее скорости")
        self.assertEqual(ход[-1], ("замер моделей", 15, 15, "готово"))

    def test_запись_выбора_и_основная(self):
        прогон, _ = self._прогон()
        путь = os.path.join(self.корень, "local-settings.json")
        with mock.patch.dict(os.environ, {"LOCAL_SETTINGS": путь}):
            данные = local_bench.записать_выбор(прогон)
            self.assertEqual((данные["основная"], local_bench.основная()), ("loc-qwen3", "loc-qwen3"))
        with mock.patch.dict(os.environ, {"LOCAL_SETTINGS": os.path.join(self.корень, "нет.json")}), \
                mock.patch("agent.catalog.installed", side_effect=lambda к: к == "loc-gemma3"):
            self.assertEqual(local_bench.основная(), "loc-gemma3", "без настроек — первая скачанная")

    def test_отчёт_и_отметки(self):
        прогон, _ = self._прогон()
        self.assertEqual(set(прогон.машина), {"до", "после"})
        память = os.path.join(self.корень, "memory")
        путь = local_bench.записать(прогон, память)
        self.assertEqual(os.path.dirname(путь), os.path.join(память, "local"), "отчёт чужой памяти — рядом с ней")
        with open(путь, encoding="utf-8") as f:
            отчёт = f.read()
        for кусок in ("| Qwen 3 4B (локально) | локально | 5 из 5 |", "| DeepSeek V4 Flash | облако | 5 из 5 |",
                      "**Основная локальная модель: Qwen 3 4B (локально)**", "### c1. Извлечение по схеме в JSON",
                      "Машина до прогона:", "Системная подсказка всем задачам"):
            self.assertIn(кусок, отчёт)
        local_bench.отметить(память, "p1", "loc-gemma3", "👎", "ответил не по-русски")
        local_bench.отметить(память, "p1", "loc-gemma3", "👍")
        данные = local_bench.прочитать(память)
        self.assertEqual(данные["marks"]["p1|loc-gemma3"]["mark"], "👍", "последняя отметка побеждает")
        self.assertEqual(данные["chosen"]["model"], "loc-qwen3")
        for плохо in (("", "loc-qwen3", "👍"), ("p1", "нет-модели", "👍"), ("p1", "loc-qwen3", "5")):
            with self.assertRaises(ValueError):
                local_bench.отметить(память, *плохо)

    def test_системная_подсказка_и_машина(self):
        сообщения, контекст = local_bench.сообщения_задачи(self.задача("s1"))
        self.assertEqual([с["role"] for с in сообщения], ["system", "user"])
        self.assertIn("только числом", сообщения[0]["content"])
        self.assertEqual((контекст, self.задача("s1")["предел"]), ({}, 300))
        м = local_bench.машина()
        for ключ in ("threads", "cores", "load", "temp_c"):
            self.assertIn(ключ, м)
        self.assertIn("ядра", local_bench.машина_строкой(м))
        self.assertEqual(local_bench.машина_строкой({}), "—")

    def test_задача_по_документам_без_индекса(self):
        with self.assertRaisesRegex(ValueError, "нужен индекс"):
            local_bench.сообщения_задачи(self.задача("c2"))


def _поддельный_поток(ключ, сообщения, **_):
    """Поток без Ollama и облака: два куска текста и итог."""
    локальная = catalog.get(ключ).local
    yield {"тип": "текст", "текст": "Па"}
    yield {"тип": "текст", "текст": "риж"}
    yield {"тип": "итог", "model": ключ, "label": catalog.get(ключ).label, "local": локальная, "text": "Париж",
           "thinking": "", "ttft_s": 4.0 if локальная else 0.4, "total_s": 5.0, "tok_s": 6.0 if локальная else 60.0,
           "tokens_in": 20, "tokens_out": 3, "cost": 0.0 if локальная else 0.0001,
           "server": {"load_s": 0.0, "prompt_s": 3.9, "gen_s": 1.0, "prompt_tok_s": 5.0} if локальная else {}}


class КонсольИВебДня26(unittest.TestCase):
    """Ключи консоли, печать, ручки и страница /local — без Ollama."""

    @classmethod
    def setUpClass(cls):
        cls.корень = tempfile.mkdtemp(prefix="лок-веб26-")
        cls.прежние = {к: os.environ.get(к) for к in ("MEMORY_DIR", "LOCAL_SETTINGS", "LOCAL_TASKS")}
        os.environ["MEMORY_DIR"] = os.path.join(cls.корень, "memory")
        os.environ["LOCAL_SETTINGS"] = os.path.join(cls.корень, "local-settings.json")
        задачи = [з for з in local_bench.загрузить_задачи() if з["id"] in ("p1", "s1")]
        os.environ["LOCAL_TASKS"] = os.path.join(cls.корень, "задачи.json")
        with open(os.environ["LOCAL_TASKS"], "w", encoding="utf-8") as f:
            _json.dump({"система": "Отвечай коротко.", "задачи": задачи}, f, ensure_ascii=False)
        import importlib
        import web as веб_модуль
        cls.веб = importlib.reload(веб_модуль)
        cls.клиент = cls.веб.app.test_client()
        cls.состояние = {"address": "http://127.0.0.1:11434", "loopback": True, "service": "active", "num_ctx": 8192,
                         "cli": "/usr/local/bin/ollama", "version": "0.34.4", "error": "",
                         "models": [{"name": "qwen3:4b", "size_gb": 2.5, "params": "4.0B", "quant": "Q4_K_M",
                                     "family": "qwen3", "key": "loc-qwen3"}],
                         "loaded": [{"name": "qwen3:4b", "size_gb": 3.4, "processor": "100% CPU", "context": 8192,
                                     "until": "2026-10-05T16:00:00", "key": "loc-qwen3"}],
                         "disk": {"path": "/usr/share/ollama/.ollama/models", "size_gb": 7.0},
                         "catalog": [{"key": "loc-qwen3", "api_id": "qwen3:4b", "label": "Qwen 3 4B (локально)", "installed": True}]}

    @classmethod
    def tearDownClass(cls):
        cls.веб.agent.close()
        for к, з in cls.прежние.items():
            if з is None:
                os.environ.pop(к, None)
            else:
                os.environ[к] = з
        shutil.rmtree(cls.корень, ignore_errors=True)

    def setUp(self):
        self.заплатки = [mock.patch("agent.local.ollama.состояние", return_value=self.состояние),
                         mock.patch("agent.catalog.installed", side_effect=lambda к: к in ("loc-qwen3", "ds-flash") or not catalog.get(к).local),
                         mock.patch("agent.local.stream.поток", side_effect=_поддельный_поток)]
        for з in self.заплатки:
            з.start()

    def tearDown(self):
        for з in self.заплатки:
            з.stop()

    def _дождаться(self) -> dict:
        for _ in range(400):
            работа = self.клиент.get("/api/local").get_json()["работа"]
            if работа and работа["готово"]:
                return работа
            time.sleep(0.05)
        self.fail("замер не закончился")

    def test_ключи_консоли(self):
        import cli
        справка = cli.build_parser().format_help()
        for ключ in ("--ollama", "--локально", "--рядом", "--думать", "--коротко", "--cli-ollama", "--http-ollama",
                     "--бенчмарк-локально", "--модели", "--без-облака", "--файл-задач", "--отметить"):
            self.assertIn(ключ, справка)
        for ключи in (["--ollama"], ["--локально", "?"], ["--cli-ollama", "?"], ["--http-ollama", "?"],
                      ["--бенчмарк-локально"], ["--отметить", "p1:loc-qwen3=да"]):
            self.assertTrue(cli.меняет_состояние(cli.build_parser().parse_args(ключи)), ключи)
        self.assertIn("loc-qwen3", cli.build_parser().parse_args(["--модель", "loc-qwen3"]).model)

    def test_консоль_статус_и_вопрос_рядом(self):
        import io
        import cli
        вывод = io.StringIO()
        with mock.patch("sys.stdout", вывод):
            self.assertEqual(cli.ollama_cli(cli.build_parser().parse_args(["--ollama"])), 0)
        for кусок in ("Ollama 0.34.4 · служба: active", "петлевой интерфейс", "qwen3:4b", "Q4_K_M", "100% CPU",
                      "loc-qwen3"):
            self.assertIn(кусок, вывод.getvalue())
        вывод = io.StringIO()
        with mock.patch("sys.stdout", вывод):
            код = cli.выполнить(self.веб.agent, cli.build_parser().parse_args(
                ["--локально", "Столица Франции?", "--рядом", "--модель", "loc-qwen3"]))
        self.assertEqual(код, 0)
        текст = вывод.getvalue()
        for кусок in (f"Qwen 3 4B (локально) · локально, {catalog._OLLAMA}", "Париж",
                      "до первого токена 4.0 с", "DeepSeek V4 Flash · облако", "Рядом: до первого токена 4.0 с против 0.4 с",
                      "цена $0 против $0.0001"):
            self.assertIn(кусок, текст)

    def test_неизмеримая_скорость_не_ноль(self):
        import io
        import cli
        итог = list(_поддельный_поток("ds-flash", []))[-1]
        итог["tok_s"] = 0.0          # ответ пришёл одним куском — скорость не измерить
        вывод = io.StringIO()
        with mock.patch("sys.stdout", вывод):
            cli._печать_итога(итог)
        self.assertNotIn("0.0 ток/с", вывод.getvalue())
        self.assertIn("всего 5.0 с · вход 20", вывод.getvalue())

    def test_консоль_отметка(self):
        import io
        import cli
        вывод = io.StringIO()
        with mock.patch("sys.stdout", вывод):
            self.assertEqual(cli.выполнить(self.веб.agent, cli.build_parser().parse_args(
                ["--отметить", "p1:loc-qwen3=да", "--почему", "верно и коротко"])), 0)
        self.assertIn("Отметка 👍 — задача p1, модель loc-qwen3", вывод.getvalue())
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(cli.выполнить(self.веб.agent, cli.build_parser().parse_args(["--отметить", "p1:loc-qwen3=5"])), 1)

    def test_ручки_состояния_и_вопроса(self):
        с = self.клиент.get("/api/local").get_json()
        self.assertEqual((с["ollama"]["version"], с["cloud"]), ("0.34.4", catalog.for_role("ответ-по-документам", 0)))
        self.assertEqual([з["id"] for з in с["tasks"]], ["p1", "s1"])
        ответ = self.клиент.post("/api/local/ask", json={"question": "Столица?", "mode": "both", "model": "loc-qwen3",
                                                         "short": True})
        self.assertEqual(ответ.mimetype, "application/x-ndjson")
        события = [_json.loads(с) for с in ответ.get_data(as_text=True).splitlines() if с.strip()]
        ответ.close()
        переданные = [вызов.args[1] for вызов in local_stream.поток.call_args_list]
        self.assertEqual([с["role"] for с in переданные[-1]], ["system", "user"], "«как в замере» — с подсказкой замера")
        self.assertEqual(переданные[-1][0]["content"], "Отвечай коротко.")
        итоги = {с["model"]: с for с in события if с["тип"] == "итог"}
        self.assertEqual(set(итоги), {"loc-qwen3", catalog.for_role("ответ-по-документам", 0)})
        self.assertEqual(sum(1 for с in события if с["тип"] == "текст"), 4)
        for плохо in ({"question": ""}, {"question": "x", "mode": "оба"}, {"question": "x", "model": "ds-flash"},
                      {"question": "x", "cloud": "loc-qwen3"}):
            self.assertEqual(self.клиент.post("/api/local/ask", json=плохо).status_code, 400, плохо)

    def test_ручки_cli_и_http(self):
        with mock.patch("agent.local.ollama.через_cli", return_value={"command": "ollama run qwen3:4b", "code": 0,
                                                                      "stdout": "391", "stderr": "", "timings": {}, "seconds": 2.0}) as cli_:
            р = self.клиент.post("/api/local/cli", json={"question": "17*23?", "model": "loc-qwen3"}).get_json()
        self.assertEqual((р["stdout"], cli_.call_args[0][0]), ("391", "qwen3:4b"))
        with mock.patch("agent.local.ollama.через_http", return_value={"kind": "openai", "text": "391"}) as http_:
            р = self.клиент.post("/api/local/http", json={"question": "17*23?", "kind": "openai"}).get_json()
        self.assertEqual((р["text"], http_.call_args[0][0]), ("391", "openai"))
        self.assertEqual(self.клиент.post("/api/local/http", json={"question": "x", "kind": "grpc"}).status_code, 400)
        with mock.patch("agent.local.ollama.через_cli", side_effect=local_ollama.ОшибкаOllama("нет ollama")):
            self.assertEqual(self.клиент.post("/api/local/cli", json={"question": "x"}).status_code, 502)

    def test_замер_в_фоне_и_отметка(self):
        def спросить(ключ, сообщения, **_):
            итог = list(_поддельный_поток(ключ, сообщения))[-1]
            итог["text"] = "Париж" if "Франц" in сообщения[-1]["content"] else "36"
            return итог
        with mock.patch("agent.local.bench.stream.спросить", side_effect=спросить), \
                mock.patch("agent.local.bench.ollama.выгрузить"):
            self.assertEqual(self.клиент.post("/api/local/bench", json={"cloud": True}).status_code, 202)
            работа = self._дождаться()
        self.assertEqual((работа["ошибка"], работа["вид"], работа["итог"]["выбор"]), ("", "бенчмарк", "loc-qwen3"))
        с = self.клиент.get("/api/local").get_json()
        self.assertEqual(с["bench"]["totals"]["loc-qwen3"]["верно"], 2)
        self.assertEqual(с["main"], "loc-qwen3", "выбор записан в local-settings.json")
        р = self.клиент.post("/api/local/mark", json={"task": "p1", "model": "loc-qwen3", "mark": "👍"}).get_json()
        self.assertEqual(р["marks"]["p1|loc-qwen3"]["mark"], "👍")
        self.assertEqual(self.клиент.post("/api/local/mark", json={"task": "", "model": "loc-qwen3", "mark": "👍"}).status_code, 400)

    def test_страница_дня_26(self):
        страница = self.клиент.get("/local").get_data(as_text=True)
        for узел in ('id="ollama-карточка"', 'id="ollama"', 'id="обновить"', 'id="спросить-карточка"', 'id="лок-вопрос"',
                     'id="лок-режим"', 'id="лок-модель"', 'id="облако-модель"', 'id="лок-думать"', 'id="лок-спросить"',
                     'id="лок-ответы"', 'id="примеры"', 'id="лок-коротко"', 'id="доступ-карточка"', 'id="доступ-вопрос"', 'id="через-cli"',
                     'id="через-native"', 'id="через-openai"', 'id="доступ"', 'id="бенч-карточка"', 'id="бенч-облако"',
                     'id="бенч-запустить"', 'id="бенч-ход"', 'id="бенч"', "❓ Справка по форме",
                     "<title>Локальная модель: Ollama на этой машине", '<option value="loc-qwen3"'):
            self.assertIn(узел, страница)
        self.assertIn('<option value="loc-gemma3"  disabled>', страница.replace('selected ', ''),
                      "нескачанная модель недоступна")
        главная = self.клиент.get("/").get_data(as_text=True)
        self.assertIn('id="лок-ссылка-26"', главная)
        self.assertIn("<title>Агент миграции ГИС — локальный RAG", главная)


# --- День 27: локальная модель в приложении — офлайн-режим ----------------------------

from agent.local import offline as local_offline, progress as local_progress, schemas as local_schemas  # noqa: E402
from agent.mcp import config as mcp_config_mod  # noqa: E402
from agent import prompts as prompts_mod  # noqa: E402


def _карта_офлайна(корень: str, роли: dict | None = None, поиск: dict | None = None) -> str:
    """Карта ролей и выбор основной модели во временном каталоге; путь карты."""
    путь = os.path.join(корень, "local-roles.json")
    with open(путь, "w", encoding="utf-8") as f:
        _json.dump({"роли": роли if роли is not None else {
                        "разбор-реплики": {"модель": "loc-qwen17", "схема": "разбор", "температура": 0, "предел": 500},
                        "ответ-по-документам": {"модель": "основная", "схема": "цитаты", "температура": 0}},
                    "поиск": поиск if поиск is not None else {
                        "реранкер": "эвристика", "k_до": 10, "k_после": 2, "порог": 0.0,
                        "порог_уверенности": 0.3, "подвопросов": 2},
                    "история": {"окно": 2, "символов": 50}}, f, ensure_ascii=False)
    with open(os.path.join(корень, "local-settings.json"), "w", encoding="utf-8") as f:
        _json.dump({"основная": "loc-qwen35"}, f)
    return путь


class _ОфлайнОснова(unittest.TestCase):
    """Своя карта ролей, режим выключен до и после каждого теста, журнал пуст."""

    def setUp(self):
        self.корень = tempfile.mkdtemp(prefix="офлайн-")
        self.прежние = {к: os.environ.get(к) for к in ("LOCAL_ROLES", "LOCAL_SETTINGS", "HF_HUB_OFFLINE")}
        os.environ["LOCAL_ROLES"] = _карта_офлайна(self.корень)
        os.environ["LOCAL_SETTINGS"] = os.path.join(self.корень, "local-settings.json")
        local_offline.включить(False)
        local_offline.сбросить_журнал()
        self.скачана = mock.patch("agent.catalog.installed", return_value=True)
        self.скачана.start()

    def tearDown(self):
        self.скачана.stop()
        local_offline.включить(False)
        local_offline.сбросить_журнал()
        for к, з in self.прежние.items():
            if з is None:
                os.environ.pop(к, None)
            else:
                os.environ[к] = з
        shutil.rmtree(self.корень, ignore_errors=True)


class ОфлайнРежим(_ОфлайнОснова):
    """offline.py: карта ролей, предохранитель до сокета, журнал сети."""

    def test_выключен_роли_как_раньше(self):
        self.assertFalse(local_offline.включён())
        self.assertEqual(catalog.for_role("разбор-реплики", 0), catalog.ROLES["разбор-реплики"]["models"][0])
        self.assertFalse(catalog.get(catalog.for_role("исполнение", 0)).local)

    def test_включён_все_роли_локальные(self):
        local_offline.включить(True, "тест")
        for роль in catalog.ROLES:
            for сдвиг in range(3):
                self.assertTrue(catalog.get(catalog.for_role(роль, сдвиг)).local, роль)
        self.assertEqual(catalog.for_role("разбор-реплики"), "loc-qwen17")
        # Роли нет в карте — основная модель, а не облако.
        self.assertEqual(catalog.for_role("планирование"), "loc-qwen35")
        self.assertEqual(local_offline.параметры_роли("разбор-реплики").схема, "разбор")
        self.assertNotIn("ds-flash", catalog.available())
        описание = {м["key"]: м for м in catalog.describe()}
        self.assertTrue(описание["ds-flash"]["offline_blocked"])
        self.assertFalse(описание["ds-flash"]["available"])

    def test_в_карте_только_локальные_модели(self):
        _карта_офлайна(self.корень, роли={"разбор-реплики": {"модель": "ds-flash"}})
        with self.assertRaisesRegex(ValueError, "облачная модель «ds-flash»"):
            local_offline.карта()
        _карта_офлайна(self.корень, роли={"разбор-реплики": {"модель": "нет-такой"}})
        with self.assertRaisesRegex(ValueError, "такой нет в каталоге"):
            local_offline.карта()

    def test_облачная_модель_отказ_до_сокета(self):
        запросы = []
        клиент = llm_mod.Client()
        клиент._http = _httpx.Client(transport=_httpx.MockTransport(lambda з: запросы.append(з) or _httpx.Response(200, json={})))
        local_offline.включить(True, "тест")
        try:
            with self.assertRaisesRegex(llm_mod.LLMError, "Офлайн-режим: облачная модель «ds-flash»"):
                клиент.call("ds-flash", [{"role": "user", "content": "привет"}])
        finally:
            клиент.close()
        self.assertEqual(запросы, [], "до сокета: запрос не ушёл даже в транспорт")
        ж = local_offline.журнал()
        self.assertEqual((ж["заблокировано"], ж["внешних"]), (1, 0))
        self.assertEqual((ж["recent"][-1]["result"], ж["recent"][-1]["model"]), ("заблокирован до сокета", "ds-flash"))

    def test_перехватчик_блокирует_внешний_адрес_и_пропускает_локальный(self):
        local_offline.включить(True, "тест")
        with _httpx.Client(timeout=2) as http:
            with self.assertRaises(local_offline.ОшибкаОфлайна):
                http.get("https://api.deepseek.com/models")
            # Локальный адрес перехватчик пропускает: дальше — обычная ошибка
            # закрытого порта, а не блокировка.
            with self.assertRaises(_httpx.ConnectError) as ошибка:
                http.get("http://127.0.0.1:9/ничего")
            self.assertNotIsInstance(ошибка.exception, local_offline.ОшибкаОфлайна)
        ж = local_offline.журнал()
        self.assertEqual((ж["локальных"], ж["внешних"], ж["заблокировано"]), (1, 0, 1))
        self.assertTrue(issubclass(local_offline.ОшибкаОфлайна, _httpx.ConnectError))

    def test_в_облачном_режиме_журнал_считает_внешние(self):
        local_offline._проверить_запрос(_httpx.Request("GET", "https://example.org/путь?token=секрет"))
        local_offline._проверить_запрос(_httpx.Request("POST", "http://localhost:11434/api/chat"))
        ж = local_offline.журнал()
        self.assertEqual((ж["локальных"], ж["внешних"], ж["заблокировано"]), (1, 1, 0))
        self.assertNotIn("секрет", _json.dumps(ж, ensure_ascii=False), "строка запроса в журнал не попадает")

    def test_локальный_адрес(self):
        for хост in ("127.0.0.1", "127.3.4.5", "localhost", "::1", "[::1]", "",
                     _httpx.URL(catalog._OLLAMA).host):
            self.assertTrue(local_offline.локальный(хост), хост)
        for хост in ("api.deepseek.com", "8.8.8.8", "192.168.1.10", "mcp.deepwiki.com"):
            self.assertFalse(local_offline.локальный(хост), хост)
        with mock.patch.dict(os.environ, {"OFFLINE_ALLOW": "192.168.1.10, nas.local"}):
            self.assertTrue(local_offline.локальный("192.168.1.10"))
            self.assertTrue(local_offline.локальный("nas.local"))

    def test_окружение_дочерних_и_hugging_face(self):
        self.assertEqual(local_offline.окружение_для_дочерних(), {})
        local_offline.включить(True, "тест")
        окр = local_offline.окружение_для_дочерних()
        self.assertEqual((окр["AGENT_OFFLINE"], окр["HF_HUB_OFFLINE"]), ("1", "1"))
        self.assertEqual(окр["LOCAL_ROLES"], os.environ["LOCAL_ROLES"])
        self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), "1")
        local_offline.включить(False)
        self.assertIsNone(os.environ.get("HF_HUB_OFFLINE"))

    def test_схема_ответа_только_локальной_модели(self):
        self.assertIsNone(local_schemas.формат("разбор-реплики", "ds-flash", "разбор"))
        self.assertIs(local_schemas.формат("разбор-реплики", "loc-qwen17"), local_schemas.РАЗБОР)
        self.assertIs(local_schemas.формат("ответ-по-документам", "loc-qwen35", "по_памяти"), local_schemas.ПО_ПАМЯТИ)
        self.assertIsNone(local_schemas.формат("сжатие", "loc-qwen17"), "у роли нет схемы — нет format")
        тело = llm_mod.ollama_payload(catalog.get("loc-qwen17"), [{"role": "user", "content": "x"}], 500, 0.0,
                                      fmt=local_schemas.РАЗБОР)
        self.assertEqual(тело["format"]["required"][0], "вид")

    def test_запись_подбора_поиска_в_карту(self):
        итог = {"когда": "2026-10-06T12:00:00", "стенд": "rag-questions.json", "вопросов": 29,
                "выбор": {"k_до": 8, "порог_уверенности": 0.015, "найдено": 22, "с_ответом": 24},
                "варианты": [{"k_до": 6, "найдено": 19, "с_ответом": 24, "порог": 0.015, "лишнее": 1},
                             {"k_до": 8, "найдено": 22, "с_ответом": 24, "порог": 0.015}]}
        local_offline.записать_поиск(итог)
        with open(os.environ["LOCAL_ROLES"], encoding="utf-8") as f:
            данные = _json.load(f)
        п = данные["поиск"]
        self.assertEqual((п["k_до"], п["порог_уверенности"], п["реранкер"], п["k_после"]), (8, 0.015, "эвристика", 2))
        self.assertEqual([в["k_до"] for в in п["подбор"]["варианты"]], [6, 8])
        self.assertNotIn("лишнее", п["подбор"]["варианты"][0])
        self.assertIn("разбор-реплики", данные["роли"], "роли не тронуты")
        local_offline.включить(True, "тест")
        self.assertEqual((local_offline.поиск().k_до, local_offline.порог_уверенности()), (8, 0.015))


class ОблегчённыйАгент(_ОфлайнОснова):
    """Агент с инструментами в офлайне: сжатый промпт, проверки — по карте ролей."""

    def setUp(self):
        super().setUp()
        self.агент = MemoryAgent(base_dir=os.path.join(self.корень, "memory"))

    def tearDown(self):
        self.агент.close()
        super().tearDown()

    def промпт(self):
        return self.агент.builder.build("Какой статус у задачи MIG-2 в трекере?", self.агент.task,
                                        set(self.агент.layers), with_tools=True)

    def test_сжатый_промпт_в_офлайне(self):
        полный = self.промпт()
        local_offline.включить(True, "тест")
        сжатый = self.промпт()
        п, с = полный.messages[0]["content"], сжатый.messages[0]["content"]
        self.assertIn("ИНВАРИАНТЫ ПРОЕКТА", с)
        self.assertIn(prompts_mod.ИНСТРУМЕНТЫ_КРАТКО, с)
        self.assertNotIn(prompts_mod.ИНСТРУМЕНТЫ, с)
        self.assertNotIn("Почему", с.split("ИНВАРИАНТЫ ПРОЕКТА")[1].split("\n\n")[0])
        if "РАМКИ ПРОЕКТА" in п:
            self.assertNotIn("РАМКИ ПРОЕКТА", с)
        self.assertNotIn("УЖЕ ПРИНЯТЫЕ РЕШЕНИЯ", с)
        self.assertLess(len(с), len(п) * 0.7, "офлайн-промпт заметно короче")
        причины = {б.name: б.why for б in сжатый.blocks if not б.included}
        self.assertIn("офлайн", причины.get("решения", ""))

    def test_короткая_история_в_офлайне(self):
        for i in range(6):
            self.агент.memory.short.append(self.агент.memory.session, "user", f"вопрос {i} " + "х" * 900)
            self.агент.memory.short.append(self.агент.memory.session, "assistant", f"ответ {i} " + "у" * 900)
        облако = self.промпт().messages
        local_offline.включить(True, "тест")
        офлайн = self.промпт().messages
        история = [м for м in офлайн[1:-1]]
        # Тестовая карта: «история» — окно 2 и 50 символов (у проекта — 4 по 400).
        self.assertEqual(len(история), 2, "последние сообщения по окну карты, каждое обрезано")
        self.assertTrue(all(len(м["content"]) <= 51 for м in история))
        self.assertTrue(история[-1]["content"].startswith("ответ 5"), "берутся последние")
        self.assertGreater(len(облако), len(офлайн))

    def test_полный_промпт_если_в_карте_выключено(self):
        local_offline.задать_агента({"сжатый_промпт": False})
        local_offline.включить(True, "тест")
        self.assertIn(prompts_mod.ИНСТРУМЕНТЫ, self.промпт().messages[0]["content"])

    def test_проверки_агента_по_карте(self):
        self.assertFalse(local_offline.выключено("самоотчёт"), "вне офлайна карта не действует")
        local_offline.включить(True, "тест")
        self.assertTrue(local_offline.выключено("самоотчёт") and local_offline.выключено("судья_по_смыслу"))
        итог = local_offline.задать_агента({"самоотчёт": True, "знаний": 3})
        self.assertEqual((итог["самоотчёт"], итог["знаний"], итог["сжатый_промпт"]), (True, 3, True))
        self.assertFalse(local_offline.выключено("самоотчёт"))
        with self.assertRaisesRegex(ValueError, "Нет таких настроек"):
            local_offline.задать_агента({"скорость": True})
        with self.assertRaisesRegex(ValueError, "да или нет"):
            local_offline.задать_агента({"самоотчёт": "да"})
        with self.assertRaisesRegex(ValueError, "от 0 до 10"):
            local_offline.задать_агента({"знаний": 50})

    def test_эскалация_в_офлайне_не_уходит_в_облако(self):
        self.assertFalse(catalog.get(catalog.escalate("glm-flash")).local)
        local_offline.включить(True, "тест")
        self.assertEqual(catalog.escalate("loc-qwen17"), "loc-qwen35")
        self.assertEqual(catalog.escalate("groq-120b"), "loc-qwen35")


class ПоискИОтветОфлайна(_ОфлайнОснова):
    """retrieval.py и answer.py в офлайне: реранкер и K — из карты, порог «не знаю» — свой."""

    def setUp(self):
        super().setUp()
        self.поисковик = _поисковик_для_rag(self.корень, _КлиентДня23(), настройки={
            "реранкер": "llm", "k_до": 20, "k_после": 8, "порог": 0.3, "порог_уверенности": 0.5})

    def test_режимы_с_реранкером_берут_второй_этап_из_карты(self):
        self.assertEqual(self.поисковик.настройки("фильтр").реранкер, "llm")
        local_offline.включить(True, "тест")
        фильтр = self.поисковик.настройки("фильтр")
        self.assertEqual((фильтр.реранкер, фильтр.k_до, фильтр.k_после, фильтр.rewrite, фильтр.имя),
                         ("эвристика", 10, 2, False, "фильтр · офлайн"))
        полный = self.поисковик.настройки("полный")
        self.assertEqual((полный.реранкер, полный.rewrite, полный.имя), ("эвристика", True, "полный · офлайн"))
        self.assertEqual(self.поисковик.настройки("rewrite").реранкер, "", "режим без реранкера офлайн не трогает")
        self.assertEqual(self.поисковик.настройки("база").реранкер, "", "у базы второго этапа нет и в офлайне")

    def test_порог_не_знаю_под_шкалу_реранкера_офлайна(self):
        отвечатель = rag_answer.Отвечатель(self.поисковик.индексатор, _КлиентДня23())
        отвечатель._поисковик = self.поисковик
        self.assertEqual(отвечатель._порог_уверенности(), 0.5)
        local_offline.включить(True, "тест")
        self.assertEqual(отвечатель._порог_уверенности(), 0.3)
        self.assertEqual(отвечатель._порог_уверенности(0.7), 0.7, "явная поправка главнее")


class _КлиентОфлайна(_КлиентДня25):
    """Модель чата, которая запоминает схему ответа и температуру каждого вызова."""

    def call(self, model_key, messages, max_tokens=0, temperature=None, low_effort=False, tools=None, fmt=None):
        self.параметры = getattr(self, "параметры", [])
        self.параметры.append({"model": model_key, "fmt": fmt, "temperature": temperature,
                               "max_tokens": max_tokens, "система": messages[0]["content"][:40]})
        from agent.local import progress as п
        п.токены(model_key, 3, 0.5, первый=0.2)
        return super().call(model_key, messages, max_tokens, temperature, low_effort, tools)


class ЧатОфлайн(_ОфлайнОснова):
    """chat.py в офлайне: каждая роль — локальная модель, схемы, этапы хода."""

    def чат(self, клиент, модель=""):
        поисковик = _поисковик_для_rag(self.корень, клиент, настройки={
            "реранкер": "llm", "k_до": 10, "k_после": 8, "порог": 0.0, "порог_уверенности": 0.0})
        о = rag_answer.Отвечатель(поисковик.индексатор, клиент, порог=0.0)
        о._поисковик = rag_retrieval.Поисковик(поисковик.индексатор, клиент, путь_настроек_=поисковик.путь_настроек)
        return chat_mod.Чат(о, клиент, ShortTermMemory(os.path.join(self.корень, "dialog.db")),
                            chat_state.ХранилищеЧатов(os.path.join(self.корень, "chat")), "тест", модель)

    def test_ход_целиком_на_локальных_моделях_со_схемами(self):
        _карта_офлайна(self.корень, поиск={"реранкер": "эвристика", "k_до": 10, "k_после": 2, "порог": 0.0,
                                           "порог_уверенности": 0.0, "подвопросов": 2})
        local_offline.включить(True, "тест")
        клиент = _КлиентОфлайна(разборы={"апрувов": _ЦЕЛЬ_РЕВЬЮ})
        события = []
        метка = local_progress.наблюдать(события.append)
        try:
            х = self.чат(клиент, "ds-flash").ход("Мне нужно разобраться с правилами код-ревью. Сколько апрувов нужно до мержа?")
        finally:
            local_progress.снять(метка)
        разбор, ответ = клиент.параметры[0], клиент.параметры[-1]
        self.assertEqual((разбор["model"], разбор["fmt"], разбор["temperature"], разбор["max_tokens"]),
                         ("loc-qwen17", local_schemas.РАЗБОР, 0, 500))
        self.assertEqual((ответ["model"], ответ["fmt"]), ("loc-qwen35", local_schemas.ЦИТАТЫ),
                         "облачная модель, выбранная в чате, в офлайне пропускается")
        self.assertTrue(all(catalog.get(п["model"]).local for п in клиент.параметры))
        self.assertEqual([э["этап"] for э in х.этапы], ["разбор-реплики", "поиск", "ответ-по-документам"])
        self.assertTrue(all("секунд" in э and "_старт" not in э for э in х.этапы))
        поиск = х.этапы[1]
        self.assertEqual((поиск["реранкер"], поиск["k_после"]), ("эвристика", 2))
        self.assertLessEqual(поиск["в_промпт"], 2)
        self.assertTrue(х.этапы[0]["локально"] and х.этапы[2]["локально"])
        self.assertEqual(х.этапы[0]["вход"], 300)
        self.assertEqual(х.to_dict()["stages"], х.этапы)
        self.assertIn("этап", [с["событие"] for с in события])
        self.assertIn("токены", [с["событие"] for с in события])
        self.assertTrue(х.источники and х.источники[0]["вид"] == "документ")

    def test_вопрос_списанный_из_инструкции_заменяется_репликой(self):
        local_offline.включить(True, "тест")
        клиент = _КлиентОфлайна(разборы={"апрувов": {"вид": "документы",
                                                     "вопрос_для_поиска": "Назови договор, оператора, статью бюджета"}})
        х = self.чат(клиент).ход("Сколько апрувов нужно до мержа?")
        self.assertEqual(х.разбор.вопрос_для_поиска, "Сколько апрувов нужно до мержа?")
        self.assertIn("списан из инструкции", х.разбор.ошибка)
        self.assertTrue(chat_mod.из_инструкции("Назови договор, оператора, статью бюджета", chat_mod.РАЗБОР))
        self.assertFalse(chat_mod.из_инструкции("договор", chat_mod.РАЗБОР), "короткое совпадение — не списано")

    def test_подвопросов_в_офлайне_не_больше_двух_и_история_короче(self):
        local_offline.включить(True, "тест")
        клиент = _КлиентОфлайна(разборы={"всех": {"вид": "документы", "вопрос_для_поиска": "ревью",
                                                  "подвопросы": ["апрувы?", "срок ревью?", "кто мержит?", "тесты?"]}})
        х = self.чат(клиент).ход("Расскажи про всех: апрувы, срок, мерж, тесты")
        self.assertEqual(х.этапы[1]["подвопросов"], 2)
        self.assertEqual(chat_mod.окно_истории(), (2, 50))
        local_offline.включить(False)
        self.assertEqual(chat_mod.окно_истории(), (chat_mod.ОКНО, chat_mod.СИМВОЛОВ))


class ПотокСПрогрессом(_ОфлайнОснова):
    """llm.py: при наблюдателе локальная модель отвечает потоком, счётчик токенов — наружу."""

    def test_поток_ollama_и_счётчик_токенов(self):
        куски = [_json.dumps({"message": {"content": "Па"}, "done": False}) + "\n",
                 _json.dumps({"message": {"content": "риж"}, "done": False}) + "\n",
                 _json.dumps({**_ollama_ответ(""), "message": {"content": ""}, "eval_count": 2}) + "\n"]
        ollama = _Ollama(поток=куски)
        клиент = llm_mod.Client()
        клиент._http = ollama.клиент()
        события = []
        метка = local_progress.наблюдать(события.append)
        try:
            ответ = клиент.call("loc-qwen17", [{"role": "user", "content": "Столица?"}], max_tokens=50,
                                fmt=local_schemas.ПО_ПАМЯТИ)
        finally:
            local_progress.снять(метка)
            клиент.close()
        self.assertEqual((ответ.text, ответ.completion_tokens, ответ.model_key), ("Париж", 2, "loc-qwen17"))
        тело = ollama.тело()
        self.assertTrue(тело["stream"])
        self.assertEqual(тело["format"], local_schemas.ПО_ПАМЯТИ)
        токены = [с for с in события if с["событие"] == "токены"]
        self.assertIn("первый_токен", токены[0])
        self.assertTrue(токены[-1].get("готово"))
        self.assertEqual(токены[-1]["выход"], 2)

    def test_без_наблюдателя_как_раньше(self):
        ollama = _Ollama()
        клиент = llm_mod.Client()
        клиент._http = ollama.клиент()
        try:
            self.assertEqual(клиент.call("loc-qwen17", [{"role": "user", "content": "?"}]).text, "Париж")
        finally:
            клиент.close()
        self.assertFalse(ollama.тело()["stream"])
        self.assertNotIn("format", ollama.тело())


class MCPОфлайн(_ОфлайнОснова):
    """mcp/config.py и client.py: серверы с выходом в интернет в офлайне не подключаются."""

    def серверы(self):
        путь = os.path.join(self.корень, "mcp-servers.json")
        with open(путь, "w", encoding="utf-8") as f:
            _json.dump({"mcpServers": {
                "own": {"command": "python3", "args": ["сервер.py"], "env": {"ИМЯ": "1"}},
                "npx-server": {"command": "npx", "args": ["-y", "пакет"]},
                "remote": {"type": "http", "url": "https://mcp.example.com/mcp"},
                "on-machine": {"type": "http", "url": "http://127.0.0.1:8765/mcp"},
                "prod": {"command": "python3", "args": ["сервер.py"], "сеть": True}}}, f)
        return {с.имя: с for с in mcp_config_mod.load(путь)}

    def test_наружу_и_готовность_в_офлайне(self):
        с = self.серверы()
        self.assertEqual({и: с[и].наружу for и in с},
                         {"own": False, "npx-server": True, "remote": True, "on-machine": False, "prod": True})
        self.assertTrue(all(х.готов for х in с.values()), "в облачном режиме готовы все")
        local_offline.включить(True, "тест")
        self.assertEqual({и for и in с if с[и].готов}, {"own", "on-machine"})
        self.assertIn("офлайн-режим: сервер ходит в интернет", с["remote"].почему_не_готов)
        self.assertIn("npx скачивает пакет", с["npx-server"].почему_не_готов)
        self.assertIn("«сеть»: true", с["prod"].почему_не_готов)
        self.assertTrue(с["prod"].to_dict()["наружу"])

    def test_дочерний_сервер_получает_офлайн(self):
        сессия = mcp_client.Session(self.серверы()["own"])
        self.assertEqual(сессия.окружение_процесса(), {"ИМЯ": "1"})
        local_offline.включить(True, "тест")
        окр = сессия.окружение_процесса()
        self.assertEqual((окр["ИМЯ"], окр["AGENT_OFFLINE"], окр["LOCAL_ROLES"]), ("1", "1", os.environ["LOCAL_ROLES"]))

    def test_свой_mcp_файл_дня_в_офлайне(self):
        local_offline.включить(True, "тест")
        с = {х.имя: х for х in mcp_config_mod.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp-servers.json"))}
        self.assertTrue(с["tracker"].готов and с["docs"].готов and с["pipeline"].готов)
        self.assertFalse(с["tracker-real"].готов or с["github"].готов or с["deepwiki"].готов or с["filesystem"].готов)


class КонсольИВебДня27(_ОфлайнОснова):
    """Ключи --офлайн, отказ облачной модели, ручки /api/offline, ход чата потоком, страницы."""

    @classmethod
    def setUpClass(cls):
        cls.общий = tempfile.mkdtemp(prefix="офлайн-веб-")
        cls.прежний_каталог = os.environ.get("MEMORY_DIR")
        os.environ["MEMORY_DIR"] = os.path.join(cls.общий, "memory")
        import importlib
        import web as веб_модуль
        cls.веб = importlib.reload(веб_модуль)
        cls.клиент = cls.веб.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.веб.agent.close()
        if cls.прежний_каталог is None:
            os.environ.pop("MEMORY_DIR", None)
        else:
            os.environ["MEMORY_DIR"] = cls.прежний_каталог
        shutil.rmtree(cls.общий, ignore_errors=True)

    def test_ключи_консоли(self):
        import cli
        разбор = cli.build_parser()
        а = разбор.parse_args(["--офлайн", "--сеть", "--офлайн-статус"])
        self.assertTrue(а.offline and а.net_journal and а.offline_status)
        self.assertTrue(cli.меняет_состояние(разбор.parse_args(["--офлайн-подбор"])))
        self.assertIn("поиск · эвристика 10→2 · 3.5 с", cli.этапы_строкой([
            {"этап": "поиск", "реранкер": "эвристика", "k_до": 10, "k_после": 2, "секунд": 3.5}]))
        self.assertIn("разбор-реплики · loc-qwen17 (локально) · 48.0 с, вход 900 / выход 120 ток.", cli.этапы_строкой([
            {"этап": "разбор-реплики", "модель": "loc-qwen17", "локально": True, "секунд": 48.0, "вход": 900, "выход": 120}]))

    def _консоль(self, *ключи) -> _subprocess.CompletedProcess:
        окр = {**os.environ, "OLLAMA_HOST": "127.0.0.1:9", "PYTHONIOENCODING": "utf-8"}
        return _subprocess.run([sys.executable, "cli.py", *ключи], capture_output=True, text=True, timeout=120,
                               cwd=os.path.dirname(os.path.abspath(__file__)), env=окр, stdin=_subprocess.DEVNULL)

    def test_офлайн_и_облачная_модель_несовместимы(self):
        готово = self._консоль("--офлайн", "--модель", "ds-flash", "--чаты")
        self.assertEqual(готово.returncode, 2)
        self.assertIn("--офлайн и облачная модель «ds-flash» несовместимы", готово.stderr)

    def test_консоль_настройки_агента(self):
        готово = self._консоль("--офлайн-агент", "самоотчёт=да,знаний=3")
        self.assertEqual(готово.returncode, 0, готово.stderr)
        self.assertIn("самоотчёт=да", готово.stdout)
        self.assertTrue(local_offline.агент()["самоотчёт"])
        готово = self._консоль("--офлайн-агент", "самоотчёт=может")
        self.assertEqual(готово.returncode, 2)
        self.assertIn("КЛЮЧ=да или КЛЮЧ=нет", готово.stderr)
        ответ = self.клиент.post("/api/offline/agent", json={"самоотчёт": False, "судья_по_смыслу": True})
        self.assertEqual(ответ.get_json()["map"]["agent"]["судья_по_смыслу"], True)
        self.assertEqual(self.клиент.post("/api/offline/agent", json={"нет": 1}).status_code, 400)

    def test_офлайн_статус_в_консоли(self):
        готово = self._консоль("--офлайн-статус")
        self.assertEqual(готово.returncode, 0, готово.stderr)
        for кусок in ("Офлайн-режим: выключен", "разбор-реплики", "loc-qwen17", "Основная локальная модель: loc-qwen35",
                      "Поиск: реранкер эвристика, K_до 10 → в промпт 2",
                      "github         нет — офлайн-режим: сервер ходит в интернет",
                      "Агент с инструментами: сжатый промпт"):
            self.assertIn(кусок, готово.stdout)

    def test_ручки_офлайна(self):
        с = self.клиент.get("/api/offline").get_json()
        self.assertFalse(с["on"])
        self.assertEqual(len(с["map"]["roles"]), len(catalog.ROLES))
        с = self.клиент.post("/api/offline", json={"on": True, "reset_net": True}).get_json()
        self.assertTrue(с["on"] and local_offline.включён())
        self.assertEqual((с["source"], с["net"]["внешних"]), ("страница", 0))
        роли = {р["role"]: р["model"] for р in с["map"]["roles"]}
        self.assertEqual(роли["разбор-реплики"], "loc-qwen17")
        self.assertTrue(all(catalog.get(м).local for м in роли.values()))
        self.assertEqual(с["search"]["реранкер"], "эвристика")
        self.assertIn("github", [х["name"] for х in с["servers"] if not х["ready"]])
        ответ = self.клиент.post("/api/chat/say", json={"text": "привет", "model": "ds-flash"})
        self.assertEqual(ответ.status_code, 400)
        self.assertIn("облачная модель «ds-flash» недоступна", ответ.get_json()["error"])
        self.assertEqual(self.клиент.post("/api/offline", json={}).status_code, 400)
        self.assertFalse(self.клиент.post("/api/offline", json={"on": False}).get_json()["on"])

    def test_ход_чата_потоком(self):
        class _Ход:
            def to_dict(self):
                return {"turn": 1, "text": "ок", "sources": [], "stages": [{"этап": "разбор-реплики"}], "rag": {"x": 1}}

        class _Чат:
            def ход(self, текст):
                local_progress.этап("разбор-реплики", модель="loc-qwen17", локально=True)
                local_progress.токены("loc-qwen17", 5, 1.0, первый=0.4)
                return _Ход()

        with mock.patch.object(self.веб.agent, "чат", return_value=_Чат()), \
                mock.patch.object(self.веб, "_состояние_чата", return_value={"сессия": "тест"}):
            ответ = self.клиент.post("/api/chat/say", json={"text": "привет", "stream": True})
            строки = [_json.loads(с) for с in ответ.get_data(as_text=True).splitlines() if с.strip()]
        self.assertEqual(ответ.mimetype, "application/x-ndjson")
        self.assertEqual([с["событие"] for с in строки], ["этап", "токены", "готово"])
        self.assertEqual(строки[0]["модель"], "loc-qwen17")
        self.assertEqual(строки[-1]["turn"]["stages"], [{"этап": "разбор-реплики"}])
        self.assertNotIn("rag", строки[-1]["turn"])
        self.assertIn("внешних", строки[-1]["net"])

    def test_страницы_дня_27(self):
        for путь in ("/", "/chat", "/rag", "/local"):
            html = self.клиент.get(путь).get_data(as_text=True)
            self.assertIn('id="офлайн-полоса"', html, путь)
            self.assertIn('id="офлайн-тумблер"', html, путь)
        главная = self.клиент.get("/").get_data(as_text=True)
        self.assertIn('data-local="0"', главная)
        self.assertIn('data-local="1"', главная)
        local = self.клиент.get("/local").get_data(as_text=True)
        self.assertIn('id="офлайн-карточка"', local)
        self.assertIn('data-облако="1"', local)
        чат = self.клиент.get("/chat").get_data(as_text=True)
        self.assertIn("stream: true", чат)
        self.assertIn("function этапыHTML", чат)


# --- День 28: локальная LLM + RAG ---------------------------------------------------

from agent.rag import profile as rag_profile  # noqa: E402


class _КлиентЛокальногоRAG(_КлиентДня24):
    """Модель ответа с цитатами, которая принимает схему (format) и, если она
    локальная, отдаёт замеры Ollama. чтение — токенов промпта в секунду:
    десятки — промпт читается заново, сотни — взят из кэша Ollama."""

    def __init__(self, *ответы, чтение: float = 40.0, **к):
        super().__init__(*ответы, **к)
        self.чтение = чтение
        self.схемы: list = []

    def call(self, model_key, messages, max_tokens=0, temperature=None, low_effort=False, tools=None,
             fmt=None):
        self.схемы.append((model_key, fmt))
        reply = super().call(model_key, messages, max_tokens, temperature, low_effort, tools)
        if catalog.get(model_key).local:
            чтение_с = round(reply.prompt_tokens / self.чтение, 2)
            reply.timings = {"load_s": 0.0, "prompt_s": чтение_с, "gen_s": 5.0, "ttft_s": чтение_с,
                             "total_s": round(чтение_с + 5.0, 2),
                             "tok_s": round(reply.completion_tokens / 5.0, 1), "prompt_tok_s": self.чтение}
        return reply


def _отвечатель_дня28(корень: str, клиент, **поля) -> "rag_answer.Отвечатель":
    """Отвечатель над маленьким индексом: LLM-реранкер в rag-settings (как облако
    Дня 24), карта офлайна — эвристика 10 → 2 (как локальный профиль)."""
    поисковик = _поисковик_для_rag(корень, клиент, настройки={
        "реранкер": "llm", "k_до": 20, "k_после": 8, "порог": 0.3, "порог_уверенности": 0.5})
    поля.setdefault("порог", 0.0)
    о = rag_answer.Отвечатель(поисковик.индексатор, клиент, **поля)
    о._поисковик = поисковик
    return о


class ПрофильRAGДня28(_ОфлайнОснова):
    """profile.py: параметры локального конвейера — явно, а не глобальным флагом."""

    def setUp(self):
        super().setUp()
        self.отвечатель = _отвечатель_дня28(self.корень, _КлиентЛокальногоRAG())

    def test_локальный_профиль_из_карты_и_без_LLM_в_поиске(self):
        п = rag_profile.локальный(self.отвечатель.поисковик)
        self.assertFalse(local_offline.включён(), "профиль не зависит от флага офлайна")
        self.assertEqual((п.поиск.реранкер, п.поиск.k_до, п.поиск.k_после, п.поиск.rewrite),
                         ("эвристика", 10, 2, False))
        self.assertEqual(п.порог_уверенности, 0.3)
        self.assertEqual(п.поиск.порог_косинуса, 0.0, "порог косинуса — из режима, карта его не задала")
        self.assertTrue(п.имя.startswith("локальный"))
        self.assertEqual(п.to_dict()["поиск"]["k_после"], 2)

    def test_текущий_профиль_локальный_только_в_офлайне(self):
        self.assertIsNone(rag_profile.текущий(self.отвечатель.поисковик))
        local_offline.включить(True, "тест")
        self.assertTrue(rag_profile.текущий(self.отвечатель.поисковик).имя.startswith("локальный"))

    def test_облачный_профиль_дня24(self):
        п = rag_profile.облачный_дня24()
        self.assertEqual((п.поиск, п.порог_уверенности, п.окно_фрагмента), ("фильтр", None, False))

    def test_явный_профиль_сильнее_флага(self):
        о = self.отвечатель
        лок, обл = rag_profile.локальный(о.поисковик), rag_profile.облачный_дня24()
        self.assertEqual(о._порог_уверенности(профиль=лок), 0.3, "вне офлайна, но профиль локальный")
        self.assertEqual(о._порог_уверенности(), 0.5, "без профиля — rag-settings.json, как в Дне 24")
        local_offline.включить(True, "тест")
        self.assertEqual(о._порог_уверенности(), 0.3, "в офлайне текущий профиль — локальный")
        self.assertEqual(о._порог_уверенности(профиль=обл), 0.5, "явный облачный профиль сильнее флага")
        self.assertEqual(о._порог_уверенности(0.7, профиль=лок), 0.7, "явная поправка главнее профиля")

    def test_ответ_с_локальным_профилем_вне_офлайна(self):
        о = _отвечатель_дня28(self.корень, _КлиентЛокальногоRAG(), порог_уверенности=0.0)
        ответ = о.ответить("Сколько апрувов нужно до мержа?", модель="loc-qwen35",
                           профиль=rag_profile.локальный(о.поисковик))
        self.assertEqual(ответ.отбор.настройки.реранкер, "эвристика")
        self.assertLessEqual(len(ответ.фрагменты), 2)
        self.assertTrue(ответ.профиль.startswith("локальный"))
        self.assertEqual(ответ.to_dict()["profile"], ответ.профиль)
        self.assertEqual(о.клиент.схемы[-1], ("loc-qwen35", local_schemas.ЦИТАТЫ))

    def test_без_профиля_вне_офлайна_поведение_дня24(self):
        ответ = self.отвечатель.ответить("Сколько апрувов нужно до мержа?", модель="ds-flash", поиск="фильтр")
        self.assertEqual(ответ.отбор.настройки.реранкер, "llm")
        self.assertEqual((ответ.профиль, ответ.окна), ("", []))
        self.assertEqual(self.отвечатель.клиент.схемы[-1], ("ds-flash", None), "облаку схему не шлём")


class ПоискБезКэшаИРазбивка(_ОфлайнОснова):
    """retrieval.py, rerank.py, indexer.py: «новый вопрос» не читает кэш оценок;
    секунды поиска разложены по шагам."""

    def setUp(self):
        super().setUp()
        self.клиент = _КлиентДня23()
        self.поисковик = _поисковик_для_rag(self.корень, self.клиент, настройки={
            "реранкер": "llm", "k_до": 6, "k_после": 2, "порог": 0.0})

    def оценок(self):
        return sum(1 for в in self.клиент.вызовы if в["messages"][0]["content"].startswith("Ты оцениваешь"))

    def этап(self, отбор, имя):
        return next(э for э in отбор.этапы if э["этап"] == имя)

    def test_без_кэша_не_читает_но_пишет(self):
        вопрос = "Сколько апрувов нужно до мержа?"
        первый = self.поисковик.найти(вопрос, "фильтр")
        self.assertEqual(self.оценок(), 1)
        второй = self.поисковик.найти(вопрос, "фильтр")
        self.assertEqual(self.оценок(), 1, "повтор — оценки из кэша")
        self.assertGreater(self.этап(второй, "реранк")["из_кэша"], 0)
        новый = self.поисковик.найти(вопрос, "фильтр", без_кэша=True)
        self.assertEqual(self.оценок(), 2, "новый вопрос — оценки считаются заново")
        реранк = self.этап(новый, "реранк")
        self.assertEqual((реранк["из_кэша"], реранк["без_кэша"]), (0, True))
        self.assertGreater(реранк["посчитано"], 0)
        self.assertEqual([н.чанк.chunk_id for н in первый.фрагменты], [н.чанк.chunk_id for н in новый.фрагменты])
        self.поисковик.найти(вопрос, "фильтр")
        self.assertEqual(self.оценок(), 2, "после нового вопроса кэш по-прежнему пишется и читается")

    def test_разбивка_секунд_поиска(self):
        отбор = self.поисковик.найти("Сколько апрувов нужно до мержа?", "фильтр")
        поиск = self.этап(отбор, "поиск")
        self.assertIn("вектор_с", поиск)
        self.assertIn("косинус_с", поиск)
        self.assertGreaterEqual(поиск["вектор_с"], 0.0)
        находки, замер = self.поисковик.индексатор.найти_замер("апрувы", "структура", 3)
        self.assertEqual(len(находки), 3)
        self.assertEqual(set(замер), {"вектор", "косинус"})


class ЗамерыОтветаRAG(_ОфлайнОснова):
    """answer.py: замеры каждого ответа, кэш промпта по скорости чтения, этапы по просьбе."""

    ВОПРОС = "Сколько апрувов нужно до мержа?"

    def ответ(self, клиент, **к):
        # Порог «не знаю» — ноль: здесь проверяются замеры ответа, а не порог.
        о = _отвечатель_дня28(self.корень, клиент, порог_уверенности=0.0)
        return о.ответить(self.ВОПРОС, профиль=rag_profile.локальный(о.поисковик), **к)

    def test_замеры_локального_ответа(self):
        ответ = self.ответ(_КлиентЛокальногоRAG(чтение=40.0), модель="loc-qwen35")
        з = ответ.to_dict()["timings"]
        self.assertEqual((з["вход"], з["выход"], з["вызовов"]), (400, 60, 1))
        self.assertEqual(з["чтение_с"], 10.0)
        self.assertEqual(з["до_первого_с"], 10.0)
        self.assertEqual(з["генерация_с"], 5.0)
        self.assertEqual(з["ток_с"], 12.0)
        self.assertIs(з["кэш_промпта"], False)
        self.assertEqual(з["скорость_чтения"], 40.0)
        self.assertEqual(з["ток_с_по"], "генерации")
        for ключ in ("поиск_с", "вектор_с", "косинус_с", "кросс_с", "окна_с", "модель_с", "всего_с"):
            self.assertIn(ключ, з)
        self.assertGreaterEqual(з["всего_с"], з["модель_с"])

    def test_промпт_из_кэша_по_скорости_чтения(self):
        з = self.ответ(_КлиентЛокальногоRAG(чтение=450.0), модель="loc-qwen35").to_dict()["timings"]
        self.assertIs(з["кэш_промпта"], True)
        self.assertGreaterEqual(rag_answer.ПОРОГ_КЭША_ПРОМПТА, 100.0)

    def test_облачный_ответ_без_замеров_ollama(self):
        з = self.ответ(_КлиентЛокальногоRAG(), модель="ds-flash").to_dict()["timings"]
        self.assertIsNone(з["кэш_промпта"])
        self.assertIsNone(з["до_первого_с"])
        self.assertEqual(з["ток_с_по"], "общему времени")

    def test_повторный_запрос_складывает_замеры(self):
        плохой = _json_ответ("Нужно 3 апрува [1].", [1], [(1, "этого текста нет в документах")])
        ответ = self.ответ(_КлиентЛокальногоRAG(плохой), модель="loc-qwen35")
        з = ответ.to_dict()["timings"]
        self.assertTrue(ответ.починка)
        self.assertEqual(з["вызовов"], 2)
        self.assertEqual(з["генерация_с"], 10.0)
        self.assertEqual(з["до_первого_с"], 10.0, "первый токен — по первому вызову")

    def test_этапы_наблюдателю_только_по_просьбе(self):
        события: list = []
        метка = local_progress.наблюдать(события.append)
        try:
            self.ответ(_КлиентЛокальногоRAG(), модель="loc-qwen35")
            без_хода = [с.get("этап") for с in события if с["событие"] == "этап"]
            события.clear()
            self.ответ(_КлиентЛокальногоRAG(), модель="loc-qwen35", ход=True)
        finally:
            local_progress.снять(метка)
        self.assertEqual(без_хода, [], "без просьбы ответ этапы не объявляет — у чата свои")
        этапы = [с for с in события if с["событие"] == "этап"]
        self.assertEqual([э["этап"] for э in этапы], ["поиск", "ответ"])
        поиск, ответ = этапы
        self.assertEqual((поиск["реранкер"], поиск["k_после"]), ("эвристика", 2))
        for ключ in ("вектор_с", "косинус_с", "кросс_с", "в_промпт", "кандидатов"):
            self.assertIn(ключ, поиск)
        self.assertEqual((ответ["модель"], ответ["локально"]), ("loc-qwen35", True))


class ТемператураОтветаДня28(_ОфлайнОснова):
    """Ответ RAG локальной моделью берёт температуру роли из карты (найдено в прогоне
    Дня 28: карта объявляла 0, а ответ шёл при 0,2 клиента по умолчанию)."""

    class _Клиент(_КлиентЛокальногоRAG):
        def call(self, model_key, messages, max_tokens=0, temperature=None, low_effort=False, tools=None,
                 fmt=None):
            self.температуры = getattr(self, "температуры", [])
            self.температуры.append((model_key, messages[0]["content"][:20], temperature))
            return super().call(model_key, messages, max_tokens, temperature, low_effort, tools, fmt)

    def test_локальному_ответу_температура_из_карты_облаку_нет(self):
        клиент = self._Клиент()
        о = _отвечатель_дня28(self.корень, клиент, порог_уверенности=0.0)
        о.ответить("Сколько апрувов нужно до мержа?", модель="loc-qwen35", профиль=rag_profile.локальный(о.поисковик))
        self.assertEqual(клиент.температуры[-1][::2], ("loc-qwen35", 0), "в карте у ответа — 0")
        о.ответить("Сколько апрувов нужно до мержа?", модель="ds-flash", профиль=rag_profile.локальный(о.поисковик))
        self.assertEqual(клиент.температуры[-1][::2], ("ds-flash", None), "облаку — температура клиента")
        self.assertEqual(local_schemas.температура("ответ-по-документам", "loc-qwen17"), 0)
        self.assertIsNone(local_schemas.температура("ответ-по-документам", "ds-flash"))

    def test_уточнение_не_знаю_тоже_по_карте(self):
        клиент = self._Клиент()
        о = _отвечатель_дня28(self.корень, клиент, порог_уверенности=0.99)
        ответ = о.ответить("Сколько стоит Office 365?", модель="loc-qwen35", профиль=rag_profile.локальный(о.поисковик))
        self.assertTrue(ответ.не_знаю)
        self.assertEqual(клиент.температуры[-1][::2], ("loc-qwen35", 0), "уточнение пишет та же роль")


class _КлиентТемператур:
    """Запоминает (модель, temperature) каждого вызова; модели из «сбой» отвечают
    ошибкой квоты — так судья переходит к следующей модели без паузы."""

    spent = {"calls": 0, "tokens": 0, "cost": 0.0}

    def __init__(self, текст="{}", сбой=()):
        self.вызовы, self.текст, self.сбой = [], текст, set(сбой)

    def call(self, model_key, messages, max_tokens=0, temperature=None, low_effort=False, tools=None,
             fmt=None):
        from agent.llm import LLMError
        self.вызовы.append((model_key, temperature))
        if model_key in self.сбой:
            raise LLMError("суточная квота исчерпана")
        return Reply(text=self.текст, model_key=model_key, prompt_tokens=10, completion_tokens=5)

    def close(self):
        pass


class ТемператураРолейИзКарты(_ОфлайнОснова):
    """Четыре роли, у которых карта объявляет температуру 0, а вызов шёл с другой
    (28.1, по решению пользователя): переписывание запроса, LLM-реранкер, судья
    ответов, сжатие. Локальной модели — 0 из карты; облачной — как раньше:
    температура клиента (None), у свёртки задачи — 0,2."""

    def setUp(self):
        super().setUp()
        _карта_офлайна(self.корень, роли={
            "переписывание-запроса": {"модель": "loc-qwen17", "температура": 0},
            "реранкер": {"модель": "loc-qwen17", "температура": 0},
            "судья-ответов": {"модель": "основная", "температура": 0},
            "сжатие": {"модель": "loc-qwen17", "температура": 0}})

    def test_помощник_вызова(self):
        self.assertEqual(local_schemas.температура_вызова("сжатие", "loc-qwen17"), {"temperature": 0})
        self.assertEqual(local_schemas.температура_вызова("сжатие", "ds-flash"), {})
        self.assertEqual(local_schemas.температура_вызова("сжатие", ""), {})

    def test_переписывание_запроса(self):
        for модель, ждём in (("loc-qwen17", 0), ("ds-flash", None)):
            клиент = _КлиентТемператур('{"запросы": ["а", "б"]}')
            rag_rewrite.Переписчик(клиент, "", модель=модель).переписать("Сколько апрувов?")
            self.assertEqual(клиент.вызовы, [(модель, ждём)])

    def test_llm_реранкер(self):
        import contextlib
        находки = [types.SimpleNamespace(чанк=types.SimpleNamespace(
            текст_для_вектора="Минимум два апрува.", источник="политика.md", раздел="Апрувы"))]
        for модель, ждём in (("loc-qwen17", 0), ("ds-flash", None)):
            клиент = _КлиентТемператур('{"оценки": {"1": 0.9}}')
            with contextlib.suppress(rag_rerank.ОшибкаРеранкера):
                rag_rerank.LLMРеранкер(клиент, None, модель)._оценить("Сколько апрувов?", находки)
            self.assertEqual(клиент.вызовы, [(модель, ждём)])

    def test_судья_ответов_температура_своя_у_каждой_модели(self):
        # Облачная первая отказала квотой — запасная локальная получает 0 из карты.
        клиент = _КлиентТемператур('{"смысл": "да"}', сбой={"ds-flash"})
        rag_qa._спросить_судью(клиент, ["ds-flash", "loc-qwen35"],
                               [{"role": "user", "content": "в"}], rag_qa.Оценка(режим="с RAG"))
        self.assertEqual(клиент.вызовы, [("ds-flash", None), ("loc-qwen35", 0)])

    def test_судья_цитат_и_судья_чата(self):
        ответ = types.SimpleNamespace(вопрос="Сколько апрувов?", текст="Два [1].", выдержки=[
            types.SimpleNamespace(подтверждена=True, найдена_в=1, n=1, текст="Минимум два апрува")])
        with mock.patch.object(rag_qa, "модели_судьи", return_value=["ds-flash", "loc-qwen35"]):
            клиент = _КлиентТемператур('{"смысл": "да"}', сбой={"ds-flash"})
            rag_citecheck.судить(клиент, rag_citecheck.Проверка(вопрос={}, ответ=ответ, подтверждено=1))
            self.assertEqual(клиент.вызовы, [("ds-flash", None), ("loc-qwen35", 0)])
            клиент = _КлиентТемператур('{"цель_удержана": "да"}', сбой={"ds-flash"})
            chat_scenarios.судить(клиент, {"id": "s"}, [], chat_state.ПамятьЗадачи("тест"))
            self.assertEqual(клиент.вызовы, [("ds-flash", None), ("loc-qwen35", 0)])

    def test_судья_сравнения_дня28(self):
        з = {"вопрос": "Сколько апрувов?", "ответ": {"abstain": False, "text": "Два [1].",
             "quotes": [{"confirmed": True, "n": 1, "text": "Минимум два апрува"}]}}
        for модель, ждём in (("loc-qwen35", 0), ("ds-pro", None)):
            клиент = _КлиентТемператур('{"смысл": "да"}')
            rag_localbench.судить(клиент, з, модель=модель)
            self.assertEqual(клиент.вызовы, [(модель, ждём)])
            self.assertEqual(з["судья"]["смысл"], "да")

    def test_свёртка_задачи(self):
        каталог = tempfile.mkdtemp(prefix="свёртка-", dir=self.корень)
        for модель, ждём in (("loc-qwen17", 0), ("ds-flash", 0.2)):
            память = MemoryManager(base_dir=os.path.join(каталог, модель), router_mode=OFF)
            задача = память.working.create("з", "перенос")
            for стадия in (EXECUTION, VALIDATION):
                задача.transition(стадия)
            память.working.save(задача)
            клиент = _КлиентТемператур('{"заголовок": "и", "решение": "р", "причина": "п"}')
            память.client = клиент
            память.finish_task(задача, model_key=модель)
            self.assertEqual(клиент.вызовы, [(модель, ждём)], "облаку — прежние 0,2")

    def test_сводка_работника_и_выжимка_конвейера(self):
        for модель, ждём in (("loc-qwen17", 0), ("ds-flash", None)):
            база = ScheduleStore(os.path.join(self.корень, f"scheduler-{модель}.db"))
            try:
                клиент = _КлиентТемператур("За час всё спокойно.")
                сводка = база.добавить_сводку({"запусков": 2})
                scheduler_worker.Работник(база, ящик=_ЯщикДляРаботника(), озвучивать=True,
                                          клиент=клиент, модель=модель).озвучить(сводка.номер)
                self.assertEqual(клиент.вызовы, [(модель, ждём)])
            finally:
                база.close()
            клиент = _КлиентТемператур("две задачи в работе")
            pipeline_server._выжимка({"задачи": [1, 2]}, "что в работе", клиент=клиент, модель=модель)
            self.assertEqual(клиент.вызовы, [(модель, ждём)])


class ДельтаСетиДня28(_ОфлайнОснова):
    """offline.py: снимок счётчиков журнала и разница — сеть одного ответа."""

    def test_снимок_и_разница(self):
        до = local_offline.снимок_журнала()
        local_offline._записать("POST", "127.0.0.1", "/api/chat", "пропущен", "loc-qwen35")
        local_offline._записать("POST", "api.deepseek.com", "/chat/completions", "пропущен", "ds-flash")
        local_offline._записать("GET", "huggingface.co", "/x", "заблокирован")
        разница = local_offline.разница(до, local_offline.снимок_журнала())
        self.assertEqual(разница, {"локальных": 1, "внешних": 1, "заблокировано": 1,
                                   "хосты": ["api.deepseek.com", "huggingface.co"]})
        # Хосты — только новые записи после снимка: прежние в разницу не попадают.
        ещё = local_offline.снимок_журнала()
        local_offline._записать("POST", "127.0.0.1", "/api/embed", "пропущен")
        self.assertEqual(local_offline.разница(ещё, local_offline.снимок_журнала())["хосты"], [])


from agent.rag import localbench as rag_localbench  # noqa: E402

_ВОПРОСЫ_28 = [
    {"id": "a", "вид": "с ответом", "вопрос": "Сколько апрувов нужно до мержа?",
     "ожидание": "Минимум 2 апрува.", "факты": [["2 апрува", "два апрува"]],
     "источники": [{"файл": "политика", "раздел": "Апрувы"}]},
    {"id": "b", "вид": "ловушка", "вопрос": "Сколько стоит Office 365?",
     "ожидание": "В документах этого нет.", "факты": [], "источники": []},
]


class _ОсноваСравнения(_ОфлайнОснова):
    """Отвечатель над маленьким индексом; порог «не знаю» локального профиля — ноль."""

    def setUp(self):
        super().setUp()
        _карта_офлайна(self.корень, поиск={"реранкер": "эвристика", "k_до": 10, "k_после": 2, "порог": 0.0,
                                           "порог_уверенности": 0.0})
        self.клиент = _КлиентЛокальногоRAG()
        self.отвечатель = _отвечатель_дня28(self.корень, self.клиент)
        self.каталог = os.path.join(self.корень, "index", "local-rag")

    def вызовы_ответа(self):
        return [м for м, _ in self.клиент.схемы]


class ОдинПоискТриУчастника(_ОсноваСравнения):
    """localbench.рядом: у трёх участников один поиск и одни окна, у четвёртого — свой."""

    def test_четыре_участника_и_общие_фрагменты(self):
        записи = rag_localbench.рядом(self.отвечатель, _ВОПРОСЫ_28[0], судья="")
        self.assertEqual([з["участник"] for з in записи], ["лок-4b", "лок-1.7b", "облако-тот-же", "облако-лучшее"])
        фрагменты = [з["ответ"]["fragments"] for з in записи]
        self.assertTrue(фрагменты[0])
        self.assertEqual(фрагменты[0], фрагменты[1])
        self.assertEqual(фрагменты[0], фрагменты[2], "облако на том же поиске видит те же chunk_id")
        self.assertEqual([з["поиск_общий"] for з in записи], [True, True, True, False])
        self.assertEqual(записи[3]["ответ"]["selection_reranker"], "llm", "облако-лучшее — «фильтр» Дня 24")
        self.assertEqual(записи[0]["ответ"]["selection_reranker"], "эвристика")
        self.assertEqual([(з["модель"]) for з in записи], ["loc-qwen35", "loc-qwen17", "ds-flash", "ds-flash"])
        self.assertIn(("loc-qwen17", local_schemas.ЦИТАТЫ), self.клиент.схемы)
        self.assertIn(("ds-flash", None), self.клиент.схемы)
        for з in записи:
            self.assertIn("timings", з["ответ"])
            self.assertEqual(set(з["сеть"]), {"локальных", "внешних", "заблокировано", "хосты"})
        self.assertEqual(записи[0]["сеть_поиска"], записи[2]["сеть_поиска"], "поиск общий — и сеть поиска одна")

    def test_окна_считаются_один_раз(self):
        _карта_офлайна(self.корень, поиск={"реранкер": "эвристика", "k_до": 10, "k_после": 2, "порог": 0.0,
                                           "порог_уверенности": 0.0, "окно_фрагмента": True})
        счёт = []

        def окна(ответ, фрагменты):
            # Настоящий выбор окна грузит кросс-энкодер ONNX — тесту нужен счёт.
            счёт.append(1)
            ответ.окна.append({"chunk_id": фрагменты[0].чанк.chunk_id, "было": 100, "стало": 50})
            return фрагменты

        self.отвечатель._лучшие_окна = окна
        записи = rag_localbench.рядом(self.отвечатель, _ВОПРОСЫ_28[0], судья="",
                                      участники=rag_localbench.участники(["лок-4b", "лок-1.7b", "облако-тот-же"]))
        self.assertEqual(len(счёт), 1, "окна выбираются один раз на общий поиск")
        self.assertTrue(all(з["ответ"]["windows"] for з in записи))


class ПрогонСравнения(_ОсноваСравнения):
    """localbench.Прогон: строка JSONL на ответ, продолжение после обрыва, «новый вопрос»."""

    def прогон(self, **к):
        к.setdefault("участники", rag_localbench.участники(["лок-4b", "облако-тот-же"]))
        к.setdefault("повторов", 2)
        к.setdefault("судья", "")
        return rag_localbench.Прогон(self.каталог, "тест", _ВОПРОСЫ_28, **к)

    def test_полный_прогон_и_порядок(self):
        п = self.прогон()
        п.выполнить(self.отвечатель)
        записи = п.записи()
        self.assertEqual(len(записи), 2 * 2 * 2)
        self.assertEqual([з["ключ"] for з in записи[:4]],
                         ["a|лок-4b|1", "a|облако-тот-же|1", "b|лок-4b|1", "b|облако-тот-же|1"])
        self.assertTrue(all(з["новый"] == (з["повтор"] == 1) for з in записи))
        реранк_нового = записи[0]["ответ"]["selection_rerank"]
        self.assertTrue(реранк_нового["без_кэша"], "повтор 1 — оценки реранкера не из кэша")
        self.assertFalse(записи[4]["ответ"]["selection_rerank"]["без_кэша"])
        состояние = п.состояние()
        self.assertEqual((состояние["готово"], состояние["всего"], состояние["завершён"]), (8, 8, True))
        with open(п.путь_журнала, encoding="utf-8") as f:
            self.assertEqual(sum(1 for _ in f), 8)

    def test_продолжение_после_обрыва(self):
        п = self.прогон()
        вызовов = {"n": 0}
        прежний = self.клиент.call

        def обрыв(*а, **к):
            вызовов["n"] += 1
            if вызовов["n"] == 4:
                raise KeyboardInterrupt
            return прежний(*а, **к)

        self.клиент.call = обрыв
        with self.assertRaises(KeyboardInterrupt):
            п.выполнить(self.отвечатель)
        self.клиент.call = прежний
        частично = п.состояние()
        self.assertFalse(частично["завершён"])
        self.assertLess(частично["готово"], 8)
        с = rag_localbench.сводка(п.записи(), _ВОПРОСЫ_28, п.участники, 2)
        self.assertFalse(с["завершён"])
        снова = self.прогон()
        снова.выполнить(self.отвечатель)
        ключи = [з["ключ"] for з in снова.записи()]
        self.assertEqual(len(ключи), 8)
        self.assertEqual(len(set(ключи)), 8, "готовые тройки не пересчитываются")

    def test_продолжение_берёт_вопросы_из_состояния(self):
        п = self.прогон(повторов=1)
        п.выполнить(self.отвечатель)
        состояние = rag_localbench.прочитать_состояние(self.каталог)
        self.assertEqual(состояние["вопросы_ids"], ["a", "b"])
        self.assertEqual(состояние["участники"], ["лок-4b", "облако-тот-же"])

    def test_ошибка_модели_пишется_а_прогон_идёт_дальше(self):
        прежний = self.клиент.call

        def сбой(model_key, *а, **к):
            if model_key == "ds-flash":
                raise LLMError("облако недоступно")
            return прежний(model_key, *а, **к)

        self.клиент.call = сбой
        п = self.прогон(повторов=1)
        п.выполнить(self.отвечатель)
        записи = п.записи()
        self.assertEqual(len(записи), 4)
        облачные = [з for з in записи if з["участник"] == "облако-тот-же"]
        self.assertTrue(all("облако недоступно" in з["ошибка"] for з in облачные))
        self.assertTrue(all(not з["верно"] for з in облачные))


def _запись(вопрос, участник, повтор, верно=True, текст="Минимум 2 апрува [1].", всего=10.0, починка=False,
            не_знаю=False, кто="", факты=(True,), кэш=False, ошибка="", чтение=6.0, вид="с ответом"):
    return {"ключ": f"{вопрос}|{участник}|{повтор}", "вопрос_id": вопрос, "вид": вид, "участник": участник,
            "повтор": повтор, "новый": повтор == 1, "верно": верно, "ошибка": ошибка,
            "ответ": {"text": текст, "abstain": не_знаю, "abstain_by": кто, "repaired": починка,
                      "violations": [], "timings": {"поиск_с": 1.0, "чтение_с": чтение, "до_первого_с": чтение,
                                                    "генерация_с": 3.0, "ток_с": 10.0, "всего_с": всего,
                                                    "кэш_промпта": кэш, "окна_с": 0.0, "модель_с": всего - 1.0}},
            "проверка": {"facts_found": list(факты), "sources_complete": True, "right_source": True,
                         "confirmed": 1, "verbatim": 1, "numbers": 1, "numbers_in_quotes": 1,
                         "abstain": не_знаю, "clarification_ok": не_знаю},
            "судья": {"смысл": "да"}, "сеть": {"локальных": 1, "внешних": 0, "заблокировано": 0}}


class МетрикиДня28(unittest.TestCase):
    """localbench.сводка: формулы spec § 6.2 на синтетических записях."""

    УЧАСТНИКИ = rag_localbench.участники(["лок-4b"])

    def сводка(self, записи):
        return rag_localbench.сводка(записи, _ВОПРОСЫ_28, self.УЧАСТНИКИ, 3)["участники"]["лок-4b"]

    def test_стабилен_только_при_совпадении_трёх_признаков(self):
        записи = [_запись("a", "лок-4b", п) for п in (1, 2, 3)]
        записи += [_запись("b", "лок-4b", 1, не_знаю=True, кто="порог", вид="ловушка", текст="Не знаю."),
                   _запись("b", "лок-4b", 2, не_знаю=True, кто="модель", вид="ловушка", текст="Не знаю."),
                   _запись("b", "лок-4b", 3, не_знаю=True, кто="порог", вид="ловушка", текст="Не знаю.")]
        с = self.сводка(записи)
        self.assertEqual(с["стабильность"]["стабильных"], 1, "у b «не знаю» решали то порог, то модель")
        self.assertEqual(с["стабильность"]["вопросов"], 2)

    def test_дословная_устойчивость_и_сбои(self):
        записи = [_запись("a", "лок-4b", 1), _запись("a", "лок-4b", 2, текст="Минимум  2 апрува [1]. "),
                  _запись("a", "лок-4b", 3, починка=True)]
        записи += [_запись("b", "лок-4b", п, текст=т, вид="ловушка")
                   for п, т in ((1, "x"), (2, "y"), (3, "x"))]
        записи[-1]["ошибка"] = "таймаут"
        с = self.сводка(записи)["стабильность"]
        self.assertEqual(с["дословно"], 1, "пробелы не в счёт, разный текст — в счёт")
        self.assertEqual((с["починок"], с["ответов"], с["ошибок"]), (1, 6, 1))

    def test_качество_по_повторам(self):
        записи = [_запись("a", "лок-4b", 1), _запись("a", "лок-4b", 2, верно=False), _запись("a", "лок-4b", 3)]
        к = self.сводка(записи)["качество"]
        self.assertEqual(к["верно_по_повторам"], [1, 0, 1])
        self.assertAlmostEqual(к["верно_в_среднем"], 2 / 3, places=2)
        self.assertEqual(к["смысл_да"], 3)

    def test_скорость_новый_вопрос_и_повтор(self):
        записи = [_запись("a", "лок-4b", 1, всего=100.0, чтение=60.0),
                  _запись("a", "лок-4b", 2, всего=20.0, чтение=0.5, кэш=True),
                  _запись("a", "лок-4b", 3, всего=30.0, чтение=0.5, кэш=True)]
        ск = self.сводка(записи)["скорость"]
        self.assertEqual(ск["новый"]["всего_с"]["медиана"], 100.0)
        self.assertEqual(ск["повтор"]["всего_с"]["медиана"], 25.0)
        self.assertEqual((ск["повтор"]["всего_с"]["мин"], ск["повтор"]["всего_с"]["макс"]), (20.0, 30.0))
        self.assertEqual((ск["новый"]["из_кэша"], ск["повтор"]["из_кэша"]), (0, 2))
        разброс = self.сводка(записи)["стабильность"]["разброс"]
        self.assertGreater(разброс["кв_медиана"], 0.5)


class ПредупреждениеОКэшеДня28(unittest.TestCase):
    """Если «новый вопрос» взял промпт из кэша Ollama, отчёт говорит об этом прямо:
    так случилось при перемере Дня 28 — промпты совпали с прошлым прогоном."""

    def test_предупреждение(self):
        уч = rag_localbench.участники(["лок-4b"])
        записи = [_запись("a", "лок-4b", п, кэш=п != 1) for п in (1, 2, 3)]
        чисто = rag_localbench.в_markdown(rag_localbench.сводка(записи, _ВОПРОСЫ_28[:1], уч, 3), записи, {})
        self.assertNotIn("⚠", чисто)
        записи[0]["ответ"]["timings"]["кэш_промпта"] = True
        md = rag_localbench.в_markdown(rag_localbench.сводка(записи, _ВОПРОСЫ_28[:1], уч, 3), записи, {})
        self.assertIn("⚠ «Новый вопрос» у участника «локально · Qwen 3.5 4B»: промпт взят из кэша Ollama в 1 из 1", md)


class ОтчётLocalRag(_ОсноваСравнения):
    """localbench.в_markdown и записать: таблицы, условия, без путей и DocsSample."""

    def test_отчёт(self):
        п = rag_localbench.Прогон(self.каталог, "тест", _ВОПРОСЫ_28, повторов=1, судья="",
                                  участники=rag_localbench.участники(["лок-4b", "облако-тот-же"]))
        п.выполнить(self.отвечатель)
        с = rag_localbench.сводка(п.записи(), _ВОПРОСЫ_28, п.участники, 1)
        условия = {"машина": {"нагрузка": "1,0"}, "профиль": {"имя": "локальный"}, "ollama": "0.0-тест"}
        md = rag_localbench.в_markdown(с, п.записи(), условия)
        for раздел in ("## Качество", "## Скорость", "## Стабильность", "## Условия прогона", "## Ответы целиком"):
            self.assertIn(раздел, md)
        self.assertNotIn(self.корень, md)
        self.assertNotIn("DocsSample", md)
        путь_md, путь_json = rag_localbench.записать(os.path.join(self.корень, "index"), с, п.записи(), условия,
                                                     в_корень=False)
        self.assertTrue(os.path.exists(путь_md) and os.path.exists(путь_json))
        with open(путь_json, encoding="utf-8") as f:
            self.assertEqual(len(_json.load(f)["записи"]), 4)


class СудьяИОтметкиДня28(_ОсноваСравнения):
    """Судья — одна модель на всех; пересуживание не трогает ответы; отметки 👍/👎."""

    def test_судья_одна_модель_и_пересуживание(self):
        п = rag_localbench.Прогон(self.каталог, "тест", _ВОПРОСЫ_28[:1], повторов=1, судья="ds-pro",
                                  участники=rag_localbench.участники(["лок-4b", "облако-тот-же"]))
        п.выполнить(self.отвечатель, клиент=self.клиент)
        судьи = [в["model"] for в in self.клиент.вызовы
                 if в["messages"][0]["content"].startswith("Ты проверяешь, подтверждают ли цитаты")]
        self.assertEqual(судьи, ["ds-pro", "ds-pro"])
        self.assertTrue(all(з["судья"]["смысл"] == "да" for з in п.записи()))
        def ответов():
            return sum('"цитаты": [{"n": 1' in в["messages"][0]["content"] for в in self.клиент.вызовы)

        ответов_до = ответов()
        self.клиент.судья_цитат = '{"смысл": "частично", "лишнее": "x", "пояснение": "п"}'
        rag_localbench.пересудить(п, self.клиент, "ds-pro")
        self.assertEqual(ответов(), ответов_до, "пересуживание без новых ответов")
        self.assertTrue(all(з["судья"]["смысл"] == "частично" for з in п.записи()))

    def test_отметки(self):
        память = os.path.join(self.корень, "memory")
        rag_localbench.отметить(память, "a", "лок-4b", "👍", "коротко и верно")
        rag_localbench.отметить(память, "a", "облако-лучшее", "👎")
        отметки = rag_localbench.отметки(память)
        self.assertEqual(отметки[("a", "лок-4b")]["mark"], "👍")
        self.assertEqual(отметки[("a", "облако-лучшее")]["model"], "ds-flash")
        with self.assertRaises(ValueError):
            rag_localbench.отметить(память, "a", "нет-такого", "👍")


class КонсольИВебДня28(_ОсноваСравнения):
    """Ключи Дня 28, локальный ответ потоком, «рядом», фоновое сравнение, отметки."""

    @classmethod
    def setUpClass(cls):
        cls.общий = tempfile.mkdtemp(prefix="локальный-rag-веб-")
        cls.прежний_каталог = os.environ.get("MEMORY_DIR")
        os.environ["MEMORY_DIR"] = os.path.join(cls.общий, "memory")
        import importlib
        import web as веб_модуль
        cls.веб = importlib.reload(веб_модуль)
        cls.клиент_веба = cls.веб.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.веб.agent.close()
        if cls.прежний_каталог is None:
            os.environ.pop("MEMORY_DIR", None)
        else:
            os.environ["MEMORY_DIR"] = cls.прежний_каталог
        shutil.rmtree(cls.общий, ignore_errors=True)

    def setUp(self):
        super().setUp()
        # Веб берёт отвечатель у агента — подменяем его тестовым над маленьким индексом.
        self.веб.agent._отвечатель = lambda *а, **к: self.отвечатель

    def tearDown(self):
        self.веб.agent.__dict__.pop("_отвечатель", None)
        super().tearDown()

    @staticmethod
    def события(ответ) -> list[dict]:
        return [_json.loads(с) for с in ответ.get_data(as_text=True).splitlines() if с.strip()]

    def test_ключи_консоли(self):
        import cli
        разбор = cli.build_parser()
        а = разбор.parse_args(["--сравнить-локально", "--повторов", "2", "--участники", "лок-4b,облако-тот-же",
                               "--только-вопросы", "c01,c02", "--метка", "м1", "--без-судьи"])
        self.assertEqual((а.local_rag_bench, а.lr_repeats, а.lr_participants, а.lr_questions, а.lr_label, а.no_judge),
                         (True, 2, "лок-4b,облако-тот-же", "c01,c02", "м1", True))
        for ключи in (["--rag-локально", "x"], ["--rag-рядом", "c01"], ["--сравнить-локально"],
                      ["--отметить-rag", "c01:лок-4b=👍"]):
            self.assertTrue(cli.меняет_состояние(разбор.parse_args(ключи)), ключи)
        строка = cli.замеры_строкой({"поиск_с": 62.0, "вектор_с": 4.9, "косинус_с": 0.5, "кросс_с": 55.8,
                                     "оценок_из_кэша": 0, "окна_с": 13.5, "чтение_с": 142.4, "вход": 2300,
                                     "скорость_чтения": 16.0, "кэш_промпта": False, "до_первого_с": 150.3,
                                     "генерация_с": 60.5, "ток_с": 2.5, "всего_с": 286.3})
        for кусок in ("поиск 62,0 с (вектор 4,9 · косинус 0,5 · кросс-энкодер 55,8, оценок из кэша 0)",
                      "окна 13,5 с", "чтение промпта 142,4 с (2300 ток., 16,0 ток/с, заново)",
                      "первый токен 150,3 с", "генерация 60,5 с, 2,5 ток/с", "всего 286,3 с"):
            self.assertIn(кусок, строка)
        облако = cli.замеры_строкой({"поиск_с": 1.0, "модель_с": 3.2, "вход": 2000, "выход": 80, "ток_с": 25.0,
                                     "всего_с": 4.2})
        self.assertIn("ответ модели 3,2 с (2000 → 80 ток., 25,0 ток/с по общему времени)", облако)

    def _консоль(self, *ключи) -> _subprocess.CompletedProcess:
        окр = {**os.environ, "OLLAMA_HOST": "127.0.0.1:9", "PYTHONIOENCODING": "utf-8"}
        return _subprocess.run([sys.executable, "cli.py", *ключи], capture_output=True, text=True, timeout=120,
                               cwd=os.path.dirname(os.path.abspath(__file__)), env=окр, stdin=_subprocess.DEVNULL)

    def test_консоль_локальный_ответ_только_локальной_моделью(self):
        готово = self._консоль("--rag-локально", "До какого числа платить?", "--модель", "ds-flash")
        self.assertEqual(готово.returncode, 2)
        self.assertIn("--rag-локально отвечает только локальной моделью", готово.stderr)
        готово = self._консоль("--отметить-rag", "c01:нет-такого=👍")
        self.assertEqual(готово.returncode, 1)
        self.assertIn("Нет участника «нет-такого»", готово.stderr)

    def test_локальный_ответ_потоком(self):
        ответ = self.клиент_веба.post("/api/rag/cited", json={"question": "Сколько апрувов нужно до мержа?",
                                                              "profile": "локальный", "stream": True})
        события = self.события(ответ)
        этапы = [с["этап"] for с in события if с["событие"] == "этап"]
        self.assertEqual(этапы[:2], ["поиск", "ответ"])
        итог = события[-1]
        self.assertEqual(итог["событие"], "готово")
        self.assertEqual(итог["answer"]["model"], "loc-qwen35")
        self.assertTrue(итог["answer"]["profile"].startswith("локальный"))
        self.assertIn("чтение_с", итог["answer"]["timings"])
        self.assertEqual(set(итог["net"]), {"локальных", "внешних", "заблокировано", "хосты"})
        плохо = self.клиент_веба.post("/api/rag/cited", json={"question": "x", "profile": "локальный",
                                                              "model": "ds-flash"})
        self.assertEqual(плохо.status_code, 400)

    def test_рядом(self):
        данные = self.клиент_веба.post("/api/rag/side", json={
            "question": "Сколько апрувов нужно до мержа?", "participants": ["лок-4b", "облако-тот-же"],
            "judge": False}).get_json()
        self.assertEqual([з["участник"] for з in данные["records"]], ["лок-4b", "облако-тот-же"])
        self.assertEqual(данные["records"][0]["ответ"]["fragments"], данные["records"][1]["ответ"]["fragments"])
        поток = self.события(self.клиент_веба.post("/api/rag/side", json={
            "question": "Сколько апрувов нужно до мержа?", "participants": ["лок-4b"], "judge": False,
            "stream": True}))
        self.assertIn("участник", [с.get("этап") for с in поток if с["событие"] == "этап"])
        self.assertEqual(поток[-1]["событие"], "готово")
        local_offline.включить(True, "тест")
        запрет = self.клиент_веба.post("/api/rag/side", json={"question": "x", "participants": ["облако-тот-же"]})
        self.assertEqual(запрет.status_code, 400)
        self.assertIn("выключенным офлайном", запрет.get_json()["error"])
        self.assertEqual(self.клиент_веба.post("/api/rag/side", json={"question": " "}).status_code, 400)

    def test_веб_без_дней_до_20_и_без_убранных_ручек(self):
        """SC-07: на страницах нет описаний Дней ≤ 20, ручки убранных панелей — 404."""
        for путь in ("/", "/rag", "/chat", "/local"):
            html = self.клиент_веба.get(путь).get_data(as_text=True)
            дни = re.findall(r"(?:День|Дня|Дне|Дней|Дням|дня|дне)\s+(\d+)", html)
            self.assertEqual([д for д in дни if int(д) <= 20], [], путь)
            self.assertIn('id="неделя-6"' if путь != "/" else 'id="rag-ссылка-28"', html, путь)
        for метод, путь in (("post", "/api/scenario"), ("post", "/api/scenario/resume"), ("get", "/api/scenario/status"),
                            ("post", "/api/profile"), ("post", "/api/user"), ("post", "/api/match"),
                            ("post", "/api/settings"), ("post", "/api/invariant"), ("post", "/api/condition"),
                            ("post", "/api/remember"), ("get", "/api/scheduler"), ("get", "/api/pipelines"),
                            ("get", "/api/flows"), ("post", "/api/route")):
            self.assertEqual(getattr(self.клиент_веба, метод)(путь, json={}).status_code, 404, путь)
        состояние = self.клиент_веба.get("/api/state").get_json()
        self.assertEqual(set(состояние), {"info", "task_state", "tasks", "stages", "stage_labels", "dialog"})

    def test_фоновое_сравнение_и_отметки(self):
        ответ = self.клиент_веба.post("/api/rag/localbench", json={
            "action": "start", "participants": ["лок-4b"], "repeats": 1, "questions": ["c01", "c09"],
            "judge": False, "label": "веб-тест"}).get_json()
        self.assertTrue(ответ["started"])
        self.assertEqual(ответ["total"], 2)
        for _ in range(200):
            с = self.клиент_веба.get("/api/rag/localbench").get_json()
            if not с["running"]:
                break
            time.sleep(0.05)
        self.assertFalse(с["running"], "прогон не закончился")
        self.assertEqual(с["error"], "")
        self.assertTrue(с["state"]["завершён"])
        self.assertEqual(с["summary"]["готово"], 2)
        self.assertIn("лок-4b", с["summary"]["участники"])
        from agent.rag.indexer import КОРЕНЬ
        self.assertFalse(rag_localbench.отчёт_в_корень(os.path.dirname(self.каталог)))
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(self.каталог), "LOCAL-RAG.md")),
                        "отчёт тестового индекса — рядом с ним, а не в корне репозитория")
        self.assertNotEqual(os.path.dirname(self.каталог), os.path.join(КОРЕНЬ, "index"))
        отметка = self.клиент_веба.post("/api/rag/mark", json={"id": "c01", "participant": "лок-4b",
                                                               "mark": "👍", "note": "верно"}).get_json()
        self.assertEqual(отметка["marks"]["c01|лок-4b"], "👍")
        self.assertEqual(self.клиент_веба.get("/api/rag/localbench").get_json()["marks"]["c01|лок-4b"], "👍")
        self.assertEqual(self.клиент_веба.post("/api/rag/mark", json={"id": "c01", "participant": "нет",
                                                                      "mark": "👍"}).status_code, 400)
        self.assertEqual(self.клиент_веба.post("/api/rag/localbench", json={"action": "что"}).status_code, 400)


from agent.local import resources as local_resources, server2 as local_server2, tuning as local_tuning  # noqa: E402

_ЛОКАЛЬНАЯ_ОПТ = {
    "до": "до", "после": "после",
    "конфигурации": {
        "до": {"модель": "loc-qwen35", "шаблон": "общий", "описание": "как в Дне 28"},
        "р-q8": {"модель": "loc-qwen35", "тег": "qwen3.5:4b-q8_0", "шаблон": "общий", "рычаг": "Р-Q8"},
        "р-потоки": {"модель": "loc-qwen35", "опции": {"num_thread": 4}, "шаблон": "общий", "рычаг": "Р-потоки"},
        "р-kv": {"модель": "loc-qwen35", "сервер": "второй", "переменные": {"OLLAMA_KV_CACHE_TYPE": "q8_0"},
                 "шаблон": "общий", "рычаг": "Р-KV"},
        "после": {"модель": "loc-rag-docs", "опции": {"num_predict": 600, "presence_penalty": 0, "num_ctx": 4096},
                  "шаблон": "договоры"},
    },
}


class _ОсноваДня29(_ОфлайнОснова):
    """Своя карта ролей и свой local-opt.json во временном каталоге."""

    def setUp(self):
        super().setUp()
        self.прежний_опт = os.environ.get("LOCAL_OPT")
        os.environ["LOCAL_OPT"] = os.path.join(self.корень, "local-opt.json")
        self.записать_опт(_ЛОКАЛЬНАЯ_ОПТ)

    def tearDown(self):
        if self.прежний_опт is None:
            os.environ.pop("LOCAL_OPT", None)
        else:
            os.environ["LOCAL_OPT"] = self.прежний_опт
        super().tearDown()

    def записать_опт(self, данные):
        with open(os.environ["LOCAL_OPT"], "w", encoding="utf-8") as f:
            _json.dump(данные, f, ensure_ascii=False)
        local_tuning._кэш = None


class КонфигурацияМодели(_ОсноваДня29):
    """tuning.py и llm.py: параметры вызова — конфигурацией, без неё — как в Дне 28."""

    СООБЩЕНИЯ = [{"role": "user", "content": "x"}]

    def test_без_конфигурации_тело_как_в_дне_28(self):
        тело = llm_mod.ollama_payload(catalog.get("loc-qwen35"), self.СООБЩЕНИЯ, 1500, 0.0, fmt={"type": "object"})
        self.assertEqual(тело["model"], "qwen3.5:4b")
        self.assertEqual(тело["options"], {"num_ctx": catalog.LOCAL_CTX, "temperature": 0.0,
                                           "num_predict": min(1500, catalog.LOCAL_CTX // 2)})
        self.assertEqual(set(тело), {"model", "messages", "stream", "think", "keep_alive", "options", "format"})

    def test_опции_и_веса_поверх_базы_только_своей_модели(self):
        к = local_tuning.Конфигурация("x", тег="qwen3.5:4b-q8_0",
                                      опции={"num_thread": 4, "num_predict": 600, "presence_penalty": 0})
        with local_tuning.применить(к):
            тело = llm_mod.ollama_payload(catalog.get("loc-qwen35"), self.СООБЩЕНИЯ, 1500, 0.0)
            чужое = llm_mod.ollama_payload(catalog.get("loc-qwen17"), self.СООБЩЕНИЯ, 1500, 0.0)
        self.assertEqual(тело["model"], "qwen3.5:4b-q8_0")
        self.assertEqual(тело["options"], {"num_ctx": catalog.LOCAL_CTX, "temperature": 0.0, "num_predict": 600,
                                           "num_thread": 4, "presence_penalty": 0})
        self.assertEqual((чужое["model"], чужое["options"]["num_predict"]), ("qwen3:1.7b", 1500),
                         "разбор реплики на 1.7B конфигурацию ответа не получает")
        self.assertNotIn("num_thread", llm_mod.ollama_payload(catalog.get("loc-qwen35"), self.СООБЩЕНИЯ, 1500, 0.0)["options"],
                         "вне блока with — снова как в Дне 28")

    def test_сервер_конфигурации_адрес_вызова(self):
        ollama = _Ollama()
        клиент = llm_mod.Client()
        клиент._http = ollama.клиент()
        with local_tuning.применить(local_tuning.получить("р-kv")):
            клиент.call("loc-qwen35", self.СООБЩЕНИЯ)
        клиент.call("loc-qwen35", self.СООБЩЕНИЯ)
        self.assertEqual(str(ollama.запросы[0].url), "http://127.0.0.1:11435/api/chat")
        self.assertEqual(str(ollama.запросы[1].url), f"{catalog._OLLAMA}/api/chat")

    def test_файл_и_ошибки(self):
        все = local_tuning.загрузить()
        self.assertEqual(list(все), ["до", "р-q8", "р-потоки", "р-kv", "после"])
        self.assertTrue(все["р-kv"].серверная and not все["р-потоки"].серверная)
        self.assertEqual(все["р-потоки"].опции_загрузки(), {"num_thread": 4})
        for плохо, слово in ((
                {"модель": "ds-flash"}, "облачная"), ({"опции": {"num_thred": 4}}, "неизвестные опции"),
                ({"шаблон": "сказки"}, "нет шаблона"), ({"сервер": "соседний"}, "нет сервера"),
                ({"переменные": {"OLLAMA_KV_CACHE_TYPE": "q8_0"}}, "только у второго сервера")):
            self.записать_опт({"конфигурации": {"плохая": плохо}})
            with self.assertRaisesRegex(local_tuning.ОшибкаКонфигурации, слово):
                local_tuning.загрузить()
        with self.assertRaisesRegex(local_tuning.ОшибкаКонфигурации, "Нет конфигурации"):
            self.записать_опт(_ЛОКАЛЬНАЯ_ОПТ)
            local_tuning.получить("нет-такой")

    def test_модель_со_своей_конфигурацией_без_with(self):
        тело = llm_mod.ollama_payload(catalog.get("loc-rag-docs"), self.СООБЩЕНИЯ, 1500, 0.0)
        self.assertEqual(тело["model"], "loc-rag-docs")
        self.assertEqual(тело["options"], {"num_ctx": 4096, "temperature": 0.0, "num_predict": 600,
                                           "presence_penalty": 0})
        self.assertEqual(local_tuning.шаблон_для("loc-rag-docs"), "договоры")


class ШаблонДоговоры(_ОсноваДня29):
    """cite.py, answer.py: шаблон «договоры» — короче, схема и проверки Дня 24 те же."""

    def test_короче_общего_и_схема_та_же(self):
        общий = len(rag_answer.РОЛЬ) + 2 + len(rag_cite.ПРАВИЛА_ЦИТАТ)
        self.assertLess(len(rag_cite.ШАБЛОН_ДОГОВОРЫ), общий * 0.5)
        for поле in ('"ответ"', '"источники"', '"цитаты"', '"не_знаю"', '"уточнение"', '"неоднозначно"'):
            self.assertIn(поле, rag_cite.ШАБЛОН_ДОГОВОРЫ)
        сообщения, блоки = rag_answer.собрать_сообщения("в?", rag_answer.С_RAG, [], rag_answer.ЦИТАТЫ,
                                                        шаблон="договоры")
        self.assertEqual(сообщения[0]["content"], rag_cite.ШАБЛОН_ДОГОВОРЫ)
        self.assertEqual(len([б for б in блоки if б.layer == "ядро"]), 1)

    def test_выбор_шаблона_конфигурацией_картой_и_облако(self):
        self.assertEqual(local_tuning.шаблон_для("loc-qwen35"), "общий")
        with local_tuning.применить(local_tuning.Конфигурация("x", шаблон="договоры")):
            self.assertEqual(local_tuning.шаблон_для("loc-qwen35"), "договоры")
            self.assertEqual(local_tuning.шаблон_для("ds-flash"), "общий")
        _карта_офлайна(self.корень, роли={"ответ-по-документам": {"модель": "основная", "схема": "цитаты",
                                                                 "температура": 0, "промпт": "договоры"}})
        # Карта перезаписана в тот же такт часов — время изменения то же, и кэш карты
        # отдал бы прежнюю: сбрасываем его явно.
        local_offline._кэш_карты = None
        self.assertEqual(local_tuning.шаблон_для("loc-qwen35"), "договоры", "поле «промпт» роли в карте")
        self.assertEqual(local_tuning.шаблон_для("ds-flash"), "общий", "облаку шаблон локальной не нужен")

    def test_ответ_с_цитатой_проходит_проверки_дня_24(self):
        клиент = _КлиентЛокальногоRAG()
        о = _отвечатель_дня28(self.корень, клиент, порог_уверенности=0.0)
        with local_tuning.применить(local_tuning.Конфигурация("x", шаблон="договоры")):
            ответ = о.ответить("Сколько апрувов нужно до мержа?", модель="loc-qwen35",
                               профиль=rag_profile.локальный(о.поисковик))
        self.assertEqual((ответ.шаблон, ответ.конфигурация), ("договоры", "x"))
        self.assertEqual(клиент.вызовы[-1]["messages"][0]["content"], rag_cite.ШАБЛОН_ДОГОВОРЫ)
        self.assertFalse(ответ.не_знаю)
        self.assertTrue(any(ц.подтверждена for ц in ответ.выдержки))
        self.assertEqual(ответ.to_dict()["template"], "договоры")


class РесурсыРаннера(unittest.TestCase):
    """resources.py: раннер по блобу и проектору, ОЗУ и секунды процессора из /proc."""

    def setUp(self):
        self.корень = tempfile.mkdtemp(prefix="proc-")

    def tearDown(self):
        shutil.rmtree(self.корень, ignore_errors=True)

    def процесс(self, pid, аргументы, rss_кб=2_000_000, hwm_кб=2_100_000, utime=300, stime=50):
        п = os.path.join(self.корень, str(pid))
        os.makedirs(п)
        with open(os.path.join(п, "cmdline"), "wb") as f:
            f.write(b"\0".join(а.encode() for а in аргументы) + b"\0")
        with open(os.path.join(п, "status"), "w", encoding="utf-8") as f:
            f.write(f"Name:\tllama-server\nVmHWM:\t{hwm_кб} kB\nVmRSS:\t{rss_кб} kB\n")
        поля = ["S"] + ["0"] * 10 + [str(utime), str(stime)] + ["0"] * 20
        with open(os.path.join(п, "stat"), "w", encoding="utf-8") as f:
            f.write(f"{pid} (llama server) " + " ".join(поля))

    def test_раннер_по_блобу_и_проектору(self):
        self.процесс(10, ["/usr/local/lib/ollama/llama-server", "--model", "/x/blobs/sha256-aaa", "--mmproj", "/x/p"])
        self.процесс(11, ["/usr/local/lib/ollama/llama-server", "--model", "/x/blobs/sha256-aaa"])
        self.процесс(12, ["/usr/bin/python3", "sha256-aaa"])
        self.assertEqual(local_resources.раннер("sha256-aaa", True, self.корень), 10)
        self.assertEqual(local_resources.раннер("sha256-aaa", False, self.корень), 11)
        self.assertIsNone(local_resources.раннер("sha256-bbb", None, self.корень))

    def test_показания_и_наблюдатель(self):
        self.процесс(20, ["/x/llama-server", "--model", "sha256-ccc"], rss_кб=3_000_000, utime=200, stime=0)
        такт = os.sysconf("SC_CLK_TCK")
        п = local_resources.показания(20, self.корень)
        self.assertEqual(п["rss_мб"], round(3_000_000 / 1024, 1))
        self.assertEqual(п["cpu_с"], round(200 / такт, 2))
        with local_resources.Наблюдатель("sha256-ccc", None, self.корень, шаг=0.05) as н:
            shutil.rmtree(os.path.join(self.корень, "20"))
            self.процесс(20, ["/x/llama-server", "--model", "sha256-ccc"], rss_кб=3_500_000, utime=200 + 5 * такт,
                         stime=0)
            time.sleep(0.2)
        self.assertEqual(н.итог["pid"], 20)
        self.assertEqual(н.итог["пик_мб"], round(3_500_000 / 1024, 1))
        self.assertEqual(н.итог["cpu_с"], 5.0)
        self.assertIn("нагрузка_до", н.итог)
        self.assertIsNone(local_resources.показания(999, self.корень))

    def test_блобы_из_модельфайла(self):
        ollama = _Ollama(ответ={"modelfile": "FROM /m/blobs/sha256-веса\nFROM /m/blobs/sha256-проектор\nTEMPLATE x"})
        self.assertEqual(local_resources.блобы("http://o", "qwen3.5:4b", ollama.клиент()),
                         ["sha256-веса", "sha256-проектор"])


class ВторойСервер(unittest.TestCase):
    """server2.py: свой порт, каталог моделей службы, переменные рычага; стоп — только своего."""

    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="сервер2-")
        self.прежний = os.environ.get("MEMORY_DIR")
        os.environ["MEMORY_DIR"] = self.каталог

    def tearDown(self):
        if self.прежний is None:
            os.environ.pop("MEMORY_DIR", None)
        else:
            os.environ["MEMORY_DIR"] = self.прежний
        shutil.rmtree(self.каталог, ignore_errors=True)

    def test_окружение(self):
        with mock.patch.dict(os.environ, {"OLLAMA_HOST": "0.0.0.0:1", "OLLAMA_KEEP_ALIVE": "30m"}):
            окр = local_server2.окружение({"OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_FLASH_ATTENTION": "1"})
        self.assertEqual(окр["OLLAMA_HOST"], "127.0.0.1:11435")
        self.assertEqual(окр["OLLAMA_MODELS"], "/usr/share/ollama/.ollama/models")
        self.assertEqual((окр["OLLAMA_NOPRUNE"], окр["OLLAMA_KV_CACHE_TYPE"], окр["OLLAMA_FLASH_ATTENTION"]),
                         ("1", "q8_0", "1"))
        self.assertEqual(окр["OLLAMA_KEEP_ALIVE"], "5m", "переменные службы второму серверу не достаются")
        with self.assertRaisesRegex(local_server2.ОшибкаСервера, "нельзя задать"):
            local_server2.окружение({"OLLAMA_MODELS": "/tmp"})

    def test_старт_пишет_состояние_стоп_только_своего(self):
        процесс = mock.Mock(pid=4242, returncode=None)
        процесс.poll.return_value = None
        with mock.patch.object(local_server2.subprocess, "Popen", return_value=процесс) as popen, \
                mock.patch.object(local_server2, "_отвечает", return_value="0.34.4"), \
                mock.patch.object(local_server2, "_свой", side_effect=lambda pid: False), \
                mock.patch.object(local_server2, "бинарник", return_value="/usr/local/bin/ollama"):
            local_server2.старт({"OLLAMA_KV_CACHE_TYPE": "q8_0"})
        аргументы, ключи = popen.call_args
        self.assertEqual(аргументы[0], ["/usr/local/bin/ollama", "serve"])
        self.assertTrue(ключи["start_new_session"])
        self.assertEqual(ключи["env"]["OLLAMA_KV_CACHE_TYPE"], "q8_0")
        with open(os.path.join(self.каталог, "local", "server2.json"), encoding="utf-8") as f:
            self.assertEqual(_json.load(f)["pid"], 4242)
        with mock.patch.object(local_server2, "_свой", return_value=False), \
                mock.patch.object(local_server2.os, "kill") as kill:
            local_server2.стоп()
        kill.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.каталог, "local", "server2.json")))


from agent.rag import optbench as rag_optbench  # noqa: E402


class _ОсноваСтенда(_ОсноваДня29):
    """Отвечатель над маленьким индексом (как в Дне 28) и local-opt.json дня."""

    def setUp(self):
        super().setUp()
        _карта_офлайна(self.корень, поиск={"реранкер": "эвристика", "k_до": 10, "k_после": 2, "порог": 0.0,
                                           "порог_уверенности": 0.0})
        self.клиент = _КлиентЛокальногоRAG()
        self.отвечатель = _отвечатель_дня28(self.корень, self.клиент)
        self.каталог = os.path.join(self.корень, "index", "local-opt")

    @staticmethod
    def ответить(отвечатель, конф, вопрос, подготовка, кэш=False):
        """Ответ в конфигурации без Ollama и /proc: путь тот же, ресурсы — подставные."""
        профиль = rag_optbench.replace(подготовка.профиль, окно_фрагмента=False)
        with local_tuning.применить(конф):
            ответ = отвечатель.ответить(вопрос, rag_answer.С_RAG, конф.модель, поиск=профиль.поиск,
                                        формат=rag_answer.ЦИТАТЫ, отбор=подготовка.отбор, профиль=профиль)
        if кэш:
            for в in ответ.замеры_вызовов:
                в["prompt_tok_s"] = 900.0
        замер = rag_optbench.Замер(загрузка_с=12.0, ресурсы={"pid": 1, "rss_мб": 3000.0, "пик_мб": 3100.0,
                                                               "cpu_с": 40.0, "нагрузка_до": 1.5})
        return ответ, {"локальных": 1, "внешних": 0, "заблокировано": 0, "хосты": []}, 30.0, замер


class СкринингИКачество(_ОсноваСтенда):
    """optbench.py: задания, латинский квадрат, запись с ресурсами, продолжение, пометка кэша."""

    def test_латинский_квадрат_и_задания_качества(self):
        вв = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        з = rag_optbench.задания_скрининга(вв, ["до", "р1", "р2"])
        self.assertEqual([x.конфигурация for x in з[:3]], ["до", "р1", "р2"])
        self.assertEqual([x.конфигурация for x in з[3:6]], ["р1", "р2", "до"])
        self.assertEqual([x.конфигурация for x in з[6:9]], ["р2", "до", "р1"])
        к = rag_optbench.задания_качества(вв, ["до", "после", "после-q8"])
        self.assertEqual(len(к), 3 * 3 + 3)
        self.assertEqual({x.ключ for x in к if x.повтор == 2}, {"a|после|2", "b|после|2", "c|после|2"})

    def test_прогон_пишет_записи_с_ресурсами_и_продолжается(self):
        задания = rag_optbench.задания_скрининга(_ВОПРОСЫ_28, ["до", "р-потоки"])
        прогон = rag_optbench.Прогон(self.каталог, "т", задания, _ВОПРОСЫ_28)
        счёт = []
        прогон.выполнить(self.отвечатель, ответить=self.ответить, стоп=lambda: len(счёт) >= 2,
                         прогресс=lambda с, з: счёт.append(з["ключ"]))
        self.assertEqual(len(прогон.записи()), 2)
        self.assertFalse(rag_optbench.прочитать_состояние(self.каталог)["завершён"])
        прогон.выполнить(self.отвечатель, ответить=self.ответить)
        записи = прогон.записи()
        self.assertEqual(len(записи), 4)
        з = next(x for x in записи if x["ключ"] == "a|р-потоки|1")
        self.assertEqual((з["конфигурация"], з["рычаг"], з["тег"], з["сервер"]), ("р-потоки", "Р-потоки", "qwen3.5:4b",
                                                                               "служба"))
        self.assertEqual(з["опции"], {"num_thread": 4})
        self.assertEqual((з["ресурсы"]["пик_мб"], з["загрузка_с"]), (3100.0, 12.0))
        self.assertTrue(з["новый"] and з["верно"])
        self.assertEqual(з["судья"]["смысл"], "да", "судья ds-pro оценил ответ")
        self.assertTrue(rag_optbench.прочитать_состояние(self.каталог)["завершён"])

    def test_ответ_из_кэша_помечен_и_не_идёт_в_скорость(self):
        задания = [rag_optbench.Задание("a", "до")]
        прогон = rag_optbench.Прогон(self.каталог, "к", задания, _ВОПРОСЫ_28)
        прогон.выполнить(self.отвечатель, ответить=lambda *а: self.ответить(*а, кэш=True))
        з = прогон.записи()[0]
        self.assertFalse(з["новый"])
        св = rag_optbench.сводка(прогон.записи())["до"]
        self.assertEqual((св["из_кэша"], св["чтение_с"]), (1, None))

    def test_gpu_память_с_машины_и_без_пометки_кэша(self):
        """Арендованный GPU: локальный /proc не читается (там нашёлся бы раннер ноутбука с теми же
        весами), память — VRAM модели из /api/ps машины; быстрое чтение промпта — не кэш."""
        конф = local_tuning.Конфигурация("до-gpu", сервер=local_tuning.GPU)
        прогон = rag_optbench.Прогон(self.каталог, "gpu-т", [rag_optbench.Задание("a", "до-gpu")], _ВОПРОСЫ_28,
                                     конфигурации={"до-gpu": конф})
        адреса = []

        def ответить(*а):
            ответ, сеть, секунд, замер = rag_optbench.ответить_в_конфигурации(*а)
            for в in ответ.замеры_вызовов:
                в["prompt_tok_s"] = 2500.0
            return ответ, сеть, секунд, замер

        with mock.patch.object(local_resources, "блобы", side_effect=lambda адрес, тег: адреса.append(адрес) or ["sha256-x"]), \
                mock.patch.object(rag_optbench, "загрузить_модель", return_value=1.5), \
                mock.patch.object(local_resources, "Наблюдатель", side_effect=AssertionError("локальный /proc")), \
                mock.patch.object(local_resources, "в_памяти", return_value=[{"name": "qwen3.5:4b", "size_мб": 5000,
                                                                               "vram_мб": 4800, "context": 8192}]), \
                mock.patch.object(local_resources, "выгрузить"):
            прогон.выполнить(self.отвечатель, ответить=ответить)
        з = прогон.записи()[0]
        self.assertEqual(адреса, ["http://127.0.0.1:11436"], "веса ищутся на машине через туннель")
        self.assertEqual(з["ресурсы"], {"удалённо": True, "vram_мб": 4800})
        self.assertTrue(з["ответ"]["timings"]["кэш_промпта"], "по скорости чтения это выглядит как кэш")
        self.assertTrue(з["новый"], "но модель грузится заново на каждый ответ — ответ новый")
        св = rag_optbench.сводка(прогон.записи())["до-gpu"]
        self.assertEqual((св["vram_мб"], св["пик_мб"], св["из_кэша"]), (4800, None, 0))
        отчёт = rag_optbench.в_markdown(None, {"сводка": {"до": {**св, "конфигурация": "до"}}}, None, {},
                                        {"описание": "GPU", "метка": "gpu-т", "сводка": {"до-gpu": св}})
        self.assertIn("| до-gpu | qwen3.5:4b |", отчёт)
        self.assertIn("VRAM, МБ", отчёт)

    def test_неизвестная_конфигурация(self):
        with self.assertRaisesRegex(ValueError, "Нет конфигураций"):
            rag_optbench.Прогон(self.каталог, "х", [rag_optbench.Задание("a", "нет-такой")], _ВОПРОСЫ_28)


def _св(имя, рычаг="", верно=3, время=100.0, пик=3000.0, починок=0, обрывов=0, годных=3, вход=1800, выход=100,
        хуже_на=()):
    вопросы = {в: {"модель_с": время, "чтение_с": время * 0.8, "вход": вход, "выход": выход, "пик_мб": пик,
                   "cpu_с": время, "загрузка_с": 10.0, "верно": в not in хуже_на, "починка": False, "обрыв": False}
               for в in ("c01", "c02", "v1")}
    return {"конфигурация": имя, "рычаг": рычаг, "годных": годных, "верно": верно, "починок": починок,
            "обрывов": обрывов, "ошибок": 0, "пропусков": 0, "модель_с": время, "пик_мб": пик, "rss_мб": пик,
            "чтение_с": время * 0.8, "генерация_с": время * 0.2, "cpu_с": время, "загрузка_с": 10.0, "вход": вход,
            "выход": выход, "судья": {"да": верно, "частично": 0, "нет": 0}, "по_вопросам": вопросы}


def _м(имя, чтение=0.0, побед=0, генерация=0.0, побед_г=None, раундов=5):
    return {"конфигурация": имя, "раундов": раундов, "пар": раундов, "чтение_доля": чтение, "чтение_побед": побед,
            "генерация_доля": генерация, "генерация_побед": побед if побед_г is None else побед_г}


class ПравилоВыбора(_ОсноваДня29):
    """optbench.выбрать (D-05, версия 3) и собрать_после: что войдёт в «после»."""

    def test_правило_d05_версии_3(self):
        К = local_tuning.Конфигурация
        конфигурации = {
            "до": К("до", шаблон="общий"),
            "р-потоки": К("р-потоки", опции={"num_thread": 4}, рычаг="Р-потоки"),
            "р-пакет": К("р-пакет", опции={"num_batch": 2048}, рычаг="Р-пакет"),
            "р-шаблон": К("р-шаблон", шаблон="договоры", рычаг="Р-шаблон"),
            "р-окно": К("р-окно", опции={"num_ctx": 4096}, рычаг="Р-окно"),
            "р-предел": К("р-предел", опции={"num_predict": 600}, рычаг="Р-предел"),
            "р-штраф": К("р-штраф", опции={"presence_penalty": 0}, рычаг="Р-штраф"),
            "р-ничего": К("р-ничего", опции={"top_k": 20}, рычаг="Р-ничего"),
            "р-сервер": К("р-сервер", сервер="второй", рычаг="Р-сервер"),
            "р-сервер2": К("р-сервер2", сервер="второй", рычаг="Р-сервер2"),
            "р-q8": К("р-q8", тег="qwen3.5:4b-q8_0", рычаг="Р-Q8"),
            "р-без-проектора": К("р-без-проектора", тег="qwen3.5-4b-text", рычаг="Р-без-проектора"),
        }
        св = {"до": _св("до"), "р-потоки": _св("р-потоки", "Р", время=180.0), "р-пакет": _св("р-пакет", "Р", время=60.0),
              "р-шаблон": _св("р-шаблон", "Р", вход=1450, время=130.0), "р-окно": _св("р-окно", "Р", пик=2800.0),
              "р-предел": _св("р-предел", "Р"), "р-штраф": _св("р-штраф", "Р", верно=2, хуже_на=("v1",)),
              "р-ничего": _св("р-ничего", "Р", время=50.0), "р-сервер": _св("р-сервер", "Р"),
              "р-q8": _св("р-q8", "Р", пик=4350.0), "р-без-проектора": _св("р-без-проектора", "Р", пик=2500.0),
              "р-сервер2": _св("р-сервер2", "Р", время=70.0)}
        микро = {"до": _м("до"), "р-потоки": _м("р-потоки", 0.12, 5), "р-пакет": _м("р-пакет", -0.10, 0),
                 "р-сервер": _м("р-сервер", 0.12, 4), "р-ничего": _м("р-ничего", 0.07, 5),
                 "р-сервер2": _м("р-сервер2", 0.05, 4)}
        р = rag_optbench.выбрать(св, конфигурации, микро=микро)
        self.assertEqual(set(р["взяты"]), {"р-потоки", "р-шаблон", "р-окно", "р-предел", "р-без-проектора"})
        self.assertEqual(р["серверные"], ["р-сервер"])
        self.assertEqual(set(р["отклонены"]), {"р-пакет", "р-штраф", "р-ничего", "р-q8"})
        self.assertIn("хуже на v1", р["причины"]["р-штраф"])
        self.assertIn("скорость чтения", р["причины"]["р-потоки"], "время скрининга (+80 %) не решает — решает микрозамер")
        self.assertIn("выигрыша нет", р["причины"]["р-ничего"], "время −50 % на скрининге — шум; +7 % в микрозамере — ниже порога")
        self.assertIn("контроль", р["причины"]["р-сервер2"])
        self.assertNotIn("р-сервер2", р["взяты"] + р["серверные"] + р["отклонены"], "контроль не выбирается")
        self.assertIn("проигрыш: пик ОЗУ", р["причины"]["р-q8"])
        self.assertIn("худший случай", р["причины"]["р-предел"])
        после = rag_optbench.собрать_после(р, конфигурации)
        self.assertEqual((после["модель"], после["шаблон"], после["веса"], после["без_проектора"]),
                         ("loc-rag-docs", "договоры", "qwen3.5:4b", True))
        self.assertEqual(после["опции"], {"num_thread": 4, "num_ctx": 4096, "num_predict": 600})
        # Версия 4: вето — рычаг прошёл скрининг, но провалил проверку вне набора.
        вето = {"р-шаблон": {"причина": "на вопросе о двух договорах сразу — «не знаю»"}}
        р4 = rag_optbench.выбрать(св, конфигурации, микро=микро, вето_=вето)
        self.assertNotIn("р-шаблон", р4["взяты"])
        self.assertIn("р-шаблон", р4["отклонены"])
        self.assertIn("проверкой вне набора: на вопросе о двух договорах", р4["причины"]["р-шаблон"])
        self.assertEqual(set(р4["взяты"]), set(р["взяты"]) - {"р-шаблон"}, "остальные решения те же")
        self.assertEqual(rag_optbench.собрать_после(р4, конфигурации)["шаблон"], "общий")

    def test_парное_сравнение_против_медиан_наборов(self):
        б = _св("до")
        с = _св("р")
        for в, (было, стало) in {"c01": (100, 210), "c02": (200, 190), "v1": (300, 290)}.items():
            б["по_вопросам"][в]["модель_с"], с["по_вопросам"][в]["модель_с"] = было, стало
        б["модель_с"], с["модель_с"] = 200, 210          # медианы наборов: +5 %
        д = rag_optbench.сравнить(б, с)
        self.assertLess(д["модель_с"], 0, "парно на тех же вопросах — быстрее на двух из трёх")
        self.assertEqual(д["вопросов"], 3)

    def test_сводка_микрозамера(self):
        записи = []
        for раунд, (до, р) in enumerate(((10.0, 11.5), (12.0, 13.0), (9.0, 8.5)), 1):
            записи += [{"ключ": f"{раунд}|до", "раунд": раунд, "конфигурация": "до", "рычаг": "",
                        "чтение_ток_с": до, "генерация_ток_с": 3.0, "загрузка_с": 10, "ресурсы": {"пик_мб": 3900}},
                       {"ключ": f"{раунд}|р", "раунд": раунд, "конфигурация": "р", "рычаг": "Р",
                        "чтение_ток_с": р, "генерация_ток_с": 3.3, "загрузка_с": 9, "ресурсы": {"пик_мб": 3200}}]
        записи.append({"ключ": "4|р", "раунд": 4, "конфигурация": "р", "пропуск": "нет памяти"})
        св = rag_optbench.сводка_микро(записи)
        self.assertEqual((св["р"]["раундов"], св["р"]["пар"], св["р"]["чтение_побед"], св["р"]["генерация_побед"]),
                         (3, 3, 2, 3))
        self.assertEqual(св["р"]["чтение_доля"], round(13.0 / 12.0 - 1, 2), "медиана долей: +15 %, +8 %, −6 %")
        self.assertEqual((св["р"]["чтение_ток_с_мин"], св["р"]["чтение_ток_с_макс"]), (8.5, 13.0))
        self.assertEqual(св["до"]["чтение_доля"], 0.0)
        имена = ["до", "р", "х"]
        к = rag_optbench.МикроЗамер.__new__(rag_optbench.МикроЗамер)
        к.имена, к.раундов = имена, 3
        self.assertEqual(к.ключи()[:6], ["1|до", "1|р", "1|х", "2|р", "2|х", "2|до"], "карусель: сдвиг в каждом раунде")


_МОДЕЛЬФАЙЛ_БАЗЫ = """# Modelfile generated by "ollama show"
FROM /m/blobs/sha256-веса
FROM /m/blobs/sha256-проектор
TEMPLATE {{ .Prompt }}
RENDERER qwen3.5
PARSER qwen3.5
PARAMETER presence_penalty 1.5
PARAMETER temperature 1
PARAMETER top_k 20
LICENSE \"\"\"Apache License
Version 2.0\"\"\"
"""


class МодельДляЗадачи(_ОсноваДня29):
    """Modelfile без проектора с параметрами «после»; тело запроса к loc-rag-docs — те же опции."""

    def test_модельфайл_без_проектора_с_параметрами(self):
        текст = rag_optbench.модельфайл(_МОДЕЛЬФАЙЛ_БАЗЫ, {"presence_penalty": 0, "num_ctx": 4096, "temperature": 0},
                                        шапка="Своя модель Дня 29")
        строки = текст.splitlines()
        self.assertEqual([с for с in строки if с.startswith("FROM")], ["FROM /m/blobs/sha256-веса"])
        self.assertIn("PARAMETER presence_penalty 0", строки)
        self.assertNotIn("PARAMETER presence_penalty 1.5", строки)
        self.assertIn("PARAMETER top_k 20", строки, "чего конфигурация не задаёт — как у базы")
        self.assertLess(строки.index("PARAMETER num_ctx 4096"), строки.index('LICENSE """Apache License'))
        self.assertTrue(строки[0].startswith("# Своя модель"))
        с_проектором = rag_optbench.модельфайл(_МОДЕЛЬФАЙЛ_БАЗЫ, {}, без_проектора=False)
        self.assertEqual(len([с for с in с_проектором.splitlines() if с.startswith("FROM")]), 2)

    def test_тело_запроса_равно_опциям_после_и_модельфайлу(self):
        после = local_tuning.получить("после")
        тело = llm_mod.ollama_payload(catalog.get("loc-rag-docs"), [{"role": "user", "content": "x"}], 1500, 0.0)
        for к, з in после.опции.items():
            self.assertEqual(тело["options"][к], з)
        текст = rag_optbench.модельфайл(_МОДЕЛЬФАЙЛ_БАЗЫ, после.опции)
        for к, з in после.опции.items():
            self.assertIn(f"PARAMETER {к} {з}", текст)


class ОтборБезПД(_ОсноваСтенда):
    """optbench.без_пд и отбор_для_gpu: на GPU — только фрагменты без ПД (D-12)."""

    def test_шаблоны(self):
        # Список фамилий — локальный файл вне репозитория; в тесте — вымышленная фамилия.
        файл = os.path.join(self.корень, "pd-names.txt")
        with open(файл, "w", encoding="utf-8") as f:
            f.write("# проверка\nВымыслов\n")
        прежний = os.environ.get("PD_NAMES")
        os.environ["PD_NAMES"] = файл
        self.addCleanup(lambda: os.environ.pop("PD_NAMES", None) if прежний is None
                        else os.environ.__setitem__("PD_NAMES", прежний))
        for текст, вид in (("подписал Иванов И. И.", "фамилия и инициалы"), ("И.И. Иванов", "инициалы и фамилия"),
                           ("Сергей Петрович", "отчество"), ("пишите на ab.cd@mail.ru", "e-mail"),
                           ("тел. +7 (473) 123-45-67", "телефон"), ("ответственный Вымыслов", "фамилия из списка")):
            self.assertIn(вид, rag_optbench.без_пд(текст), текст)
        self.assertEqual(rag_optbench.без_пд("Срок ежемесячной оплаты Услуг: не позднее 20 числа. Россия, ООО «Ромашка»"),
                         [])
        os.environ["PD_NAMES"] = os.path.join(self.корень, "нет-такого.txt")
        self.assertNotIn("фамилия из списка", rag_optbench.без_пд("ответственный Вымыслов"), "без файла — без списка")
        self.assertIn("отчество", rag_optbench.без_пд("Сергей Петрович"), "общие признаки остаются")

    def test_отбор_для_gpu(self):
        итог = rag_optbench.отбор_для_gpu(self.отвечатель, _ВОПРОСЫ_28[:1])
        self.assertEqual(итог[0]["id"], "a")
        self.assertTrue(итог[0]["фрагменты"])
        self.assertTrue(итог[0]["годен"])


class ОтчётОптимизации(_ОсноваСтенда):
    """optbench.в_markdown и записать: разделы отчёта, без путей и ПД."""

    def test_разделы_и_запись(self):
        задания = rag_optbench.задания_скрининга(_ВОПРОСЫ_28, ["до", "р-потоки"])
        прогон = rag_optbench.Прогон(self.каталог, "о", задания, _ВОПРОСЫ_28)
        прогон.выполнить(self.отвечатель, ответить=self.ответить)
        св = rag_optbench.сводка(прогон.записи())
        решение = rag_optbench.выбрать(св, local_tuning.загрузить())
        текст = rag_optbench.в_markdown({"сводка": св}, {"сводка": св}, решение,
                                        {"когда": "2026-10-09", "Ollama": "0.34.4"},
                                        vps={"дата": "2026-10-08", "провайдеры": [
                                            {"провайдер": "immers.cloud", "gpu": "A10 24 ГБ", "цена": "36,55 ₽/ч",
                                             "тарификация": "посекундно", "где": "Москва"}],
                                            "смета": ["прогон дня ≈ 55 ₽"]})
        for раздел in ("## Условия", "## «До» и «после» на 14 вопросах", "## Скрининг рычагов",
                       "## Что вошло в «после»", "## Температура", "## GPU-VPS: подбор и смета"):
            self.assertIn(раздел, текст)
        self.assertIn("| до | — |", текст)
        self.assertNotIn("DocsSample", текст)
        self.assertNotIn("/home/", текст)
        md, js = rag_optbench.записать(os.path.join(self.корень, "index"), текст, {"сводка": св})
        self.assertTrue(md.startswith(self.корень), "отчёт тестового индекса — рядом с ним")
        self.assertTrue(os.path.exists(js))


class КонсольИВебДня29(_ОсноваСтенда):
    """Ключи Дня 29, карточка «Оптимизация под задачу», ручки /api/opt/*."""

    @classmethod
    def setUpClass(cls):
        cls.общий = tempfile.mkdtemp(prefix="оптимизация-веб-")
        cls.прежний_каталог = os.environ.get("MEMORY_DIR")
        os.environ["MEMORY_DIR"] = os.path.join(cls.общий, "memory")
        import importlib
        import web as веб_модуль
        cls.веб = importlib.reload(веб_модуль)
        cls.клиент_веба = cls.веб.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.веб.agent.close()
        if cls.прежний_каталог is None:
            os.environ.pop("MEMORY_DIR", None)
        else:
            os.environ["MEMORY_DIR"] = cls.прежний_каталог
        shutil.rmtree(cls.общий, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.веб.agent._отвечатель = lambda *а, **к: self.отвечатель
        self.без_ollama = mock.patch.object(rag_optbench, "недостающие", return_value=[])
        self.без_ollama.start()

    def tearDown(self):
        self.без_ollama.stop()
        self.веб.agent.__dict__.pop("_отвечатель", None)
        super().tearDown()

    def test_ключи_консоли(self):
        import cli
        разбор = cli.build_parser()
        а = разбор.parse_args(["--скрининг", "--конфигурации", "до,р-шаблон", "--только-вопросы", "c01",
                               "--метка", "м1", "--без-судьи"])
        self.assertEqual((а.opt_screen, а.opt_configs, а.lr_questions, а.lr_label, а.no_judge),
                         (True, "до,р-шаблон", "c01", "м1", True))
        а = разбор.parse_args(["--rag-локально", "в?", "--конфигурация", "после"])
        self.assertEqual((а.rag_local, а.opt_config), ("в?", "после"))
        а = разбор.parse_args(["--второй-сервер", "старт", "--переменные", "OLLAMA_KV_CACHE_TYPE=q8_0"])
        self.assertEqual((а.opt_server2, а.opt_vars), ("старт", "OLLAMA_KV_CACHE_TYPE=q8_0"))
        for ключи in (["--оптимизация"], ["--сравнить-конфигурации"], ["--отчёт-оптимизации"], ["--создать-модель"],
                      ["--второй-сервер", "статус"]):
            self.assertTrue(cli.меняет_состояние(разбор.parse_args(ключи)), ключи)

    def test_задания_консоли_и_продолжение(self):
        import cli
        разбор = cli.build_parser()
        а = разбор.parse_args(["--скрининг"])
        задания, вопросы = cli._задания_оптимизации(а, "скрининг", {}, "м")
        self.assertEqual(len(задания), len(rag_optbench.СКРИНИНГ) * 3)
        self.assertEqual([в["id"] for в in вопросы], list(rag_optbench.ВОПРОСЫ_СКРИНИНГА))
        прежнее = {"метка": "м", "задания": ["c01|до|1", "c01|после|2"]}
        задания, вопросы = cli._задания_оптимизации(а, "качество", прежнее, "м")
        self.assertEqual([з.ключ for з in задания], ["c01|до|1", "c01|после|2"], "продолжение — те же задания")
        а = разбор.parse_args(["--сравнить-конфигурации"])
        задания, вопросы = cli._задания_оптимизации(а, "качество", {}, "н")
        self.assertEqual(len(задания), 14 * 3 + 14)

    def test_страница_и_ручки(self):
        страница = self.клиент_веба.get("/local").get_data(as_text=True)
        for кусок in ("опт-карточка", "Оптимизация под задачу", "опт-рядом", "опт-скрининг", "опт-сервер-старт",
                      "день-29"):
            self.assertIn(кусок, страница)
        # Отчёт прогонов рядом с индексом: ветка чтения его файла тоже должна работать (на стенде она падала).
        индекс = os.path.dirname(self.отвечатель.индексатор.база)
        with open(os.path.join(индекс, rag_optbench.ФАЙЛ_ИТОГА), "w", encoding="utf-8") as f:
            _json.dump({"скрининг": {"метка": "м", "сводка": {}}, "решение": {"взяты": ["р-шаблон"]}}, f)
        с = self.клиент_веба.get("/api/opt").get_json()
        self.assertEqual(с["report"]["решение"]["взяты"], ["р-шаблон"])
        self.assertEqual([к["имя"] for к in с["tuning"]["configs"]], list(_ЛОКАЛЬНАЯ_ОПТ["конфигурации"]))
        self.assertEqual((с["missing"], с["running"], с["busy"]), ([], False, False))
        self.assertEqual(len(с["questions"]), 14)
        self.assertEqual(self.клиент_веба.post("/api/opt/side", json={"question": "c01", "configs": ["до", "нет"]}).status_code, 400)
        self.assertEqual(self.клиент_веба.post("/api/opt/side", json={"question": "", "configs": ["до"]}).status_code, 400)
        self.assertEqual(self.клиент_веба.post("/api/opt/run", json={"kind": "что"}).status_code, 400)
        self.assertEqual(self.клиент_веба.post("/api/opt/run", json={"action": "что"}).status_code, 400)
        self.assertEqual(self.клиент_веба.post("/api/opt/server2", json={"action": "что"}).status_code, 400)
        ответ = self.клиент_веба.post("/api/opt/model", json={})
        self.assertEqual(ответ.status_code, 400)
        self.assertIn("скрининга ещё не было", ответ.get_json()["error"])

    def test_рядом_в_двух_конфигурациях_потоком(self):
        # Поток читается лениво: get_data — внутри подмены, иначе ответ пойдёт в настоящую Ollama.
        with mock.patch.object(rag_optbench, "ответить_в_конфигурации",
                               side_effect=lambda о, к, в, п, ход=False: self.ответить(о, к, в, п)):
            ответ = self.клиент_веба.post("/api/opt/side", json={"question": "Сколько апрувов нужно до мержа?",
                                                                 "configs": ["до", "р-потоки"], "stream": True})
            события = [_json.loads(с) for с in ответ.get_data(as_text=True).splitlines() if с.strip()]
        итог = события[-1]
        self.assertEqual(итог["событие"], "готово")
        self.assertEqual([з["конфигурация"] for з in итог["records"]], ["до", "р-потоки"])
        self.assertTrue(all(з["ресурсы"]["пик_мб"] for з in итог["records"]))
        self.assertIn("поиск", [с.get("этап") for с in события if с["событие"] == "этап"])
        self.assertFalse(self.веб._ЗАМОК_ОПТИМИЗАЦИИ.locked(), "замок отпущен после ответа")

    def test_прогон_из_другого_процесса_виден(self):
        self.assertFalse(rag_optbench.идёт_в_другом_процессе(None))
        self.assertFalse(rag_optbench.идёт_в_другом_процессе({"pid": os.getpid(), "завершён": False}))
        self.assertFalse(rag_optbench.идёт_в_другом_процессе({"pid": 2 ** 22 + 7, "завершён": False}))
        self.assertFalse(rag_optbench.идёт_в_другом_процессе({"pid": os.getppid(), "завершён": True}))
        # Живой процесс Python с незавершённым прогоном — «идёт снаружи»: страница это видит,
        # а новый прогон и ответ «рядом» отклоняются, пока он не кончится.
        import subprocess
        # Ждём строку от процесса: сразу после запуска /proc/<pid>/cmdline бывает пуст (идёт exec).
        чужой = subprocess.Popen([sys.executable, "-c", "import time; print('готов', flush=True); time.sleep(60)"],
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(чужой.stdout.readline().strip(), "готов")
            состояние = {"pid": чужой.pid, "завершён": False, "вид": "скрининг"}
            self.assertTrue(rag_optbench.идёт_в_другом_процессе(состояние))
            self.assertFalse(rag_optbench.идёт_в_другом_процессе({**состояние, "остановлен": True}))
            with mock.patch.object(rag_optbench, "прочитать_состояние", return_value=состояние):
                с = self.клиент_веба.get("/api/opt").get_json()
                self.assertEqual((с["outside"], с["running"], с["busy"]), (True, True, True))
                ответ = self.клиент_веба.post("/api/opt/run", json={"kind": "скрининг"})
                self.assertEqual(ответ.status_code, 409)
                self.assertIn("из консоли", ответ.get_json()["error"])
                ответ = self.клиент_веба.post("/api/opt/side", json={"question": "c01", "configs": ["до", "р-потоки"]})
                self.assertEqual(ответ.status_code, 409)
                self.assertFalse(self.веб._ЗАМОК_ОПТИМИЗАЦИИ.locked(), "отказ не держит замок")
        finally:
            чужой.kill()
            чужой.wait()
            чужой.stdout.close()
        # Свой прогон или ответ «рядом» держит замок — отказ с текстом (а не с пустым error состояния).
        with self.веб._ЗАМОК_ОПТИМИЗАЦИИ:
            ответ = self.клиент_веба.post("/api/opt/run", json={"kind": "скрининг"})
        self.assertEqual(ответ.status_code, 409)
        self.assertIn("уже идёт", ответ.get_json()["error"])


# --- День 30: приватный AI-сервис -----------------------------------------------------

import asyncio  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402

import httpx  # noqa: E402

from agent.service import config as svc_config, gateway as svc_gateway, journal as svc_journal  # noqa: E402
from agent.service import keys as svc_keys, limits as svc_limits, load as svc_load, tokens as svc_tokens  # noqa: E402
from agent.service import certs as svc_certs, unit as svc_unit  # noqa: E402


class КлючиСервиса(unittest.TestCase):
    """keys.Ключи: ключ показывается один раз, на диске — только хэш; права; отзыв."""

    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="ключи-сервиса-")
        self.addCleanup(shutil.rmtree, self.каталог, True)
        self.путь = os.path.join(self.каталог, "keys.json")
        self.ключи = svc_keys.Ключи(self.путь)

    def test_создание_хэш_и_права(self):
        ключ = self.ключи.создать("чат-1", ["чат"])
        self.assertTrue(ключ.startswith("sk-day30-") and len(ключ) > 40)
        текст = pathlib.Path(self.путь).read_text(encoding="utf-8")
        self.assertNotIn(ключ, текст, "на диске нет самого ключа")
        self.assertIn(svc_keys.хэш(ключ), текст)
        self.assertEqual(oct(os.stat(self.путь).st_mode & 0o777), "0o600")
        запись = self.ключи.проверить(ключ)
        self.assertEqual((запись["имя"], запись["права"]), ("чат-1", ["чат"]))
        self.assertNotIn("хэш", запись)
        self.assertIsNone(self.ключи.проверить(ключ + "x"))
        self.assertIsNone(self.ключи.проверить("Bearer чужое"))
        self.assertTrue(all("хэш" not in з for з in self.ключи.список()))

    def test_ошибки_и_отзыв(self):
        with self.assertRaisesRegex(svc_keys.ОшибкаКлюча, "права"):
            self.ключи.создать("x", ["всё"])
        with self.assertRaisesRegex(svc_keys.ОшибкаКлюча, "имя"):
            self.ключи.создать("  ", ["чат"])
        ключ = self.ключи.создать("док", ["документы", "чат"])
        self.assertEqual(self.ключи.проверить(ключ)["права"], ["чат", "документы"], "порядок прав — как в ПРАВА")
        with self.assertRaisesRegex(svc_keys.ОшибкаКлюча, "уже есть"):
            self.ключи.создать("док", ["чат"])
        self.assertTrue(self.ключи.отозвать("док"))
        self.assertIsNone(self.ключи.проверить(ключ), "отозванный ключ не пускает")
        self.assertFalse(self.ключи.отозвать("док"))
        self.assertTrue(self.ключи.создать("док", ["чат"]).startswith("sk-day30-"), "имя свободно после отзыва")


class ПодсетиСервиса(unittest.TestCase):
    def test_разрешённые_и_чужие(self):
        п = svc_limits.Подсети(svc_config.Настройки().подсети)
        for адрес in ("127.0.0.1", "192.168.1.31", "192.168.1.43", "172.17.0.2", "::1", "::ffff:192.168.1.5"):
            self.assertTrue(п.разрешён(адрес), адрес)
        for адрес in ("10.99.0.2", "172.18.0.3", "172.19.0.2", "10.8.1.4", "8.8.8.8", "testclient", ""):
            self.assertFalse(п.разрешён(адрес), f"{адрес}: сети CRM, VPN и интернет — не пускаем")


class ЛимитыСервиса(unittest.TestCase):
    def test_ведро_на_ключ(self):
        сейчас = [100.0]
        в = svc_limits.Ведро(6, часы=lambda: сейчас[0])
        self.assertEqual([в.взять("а")[0] for _ in range(6)], [True] * 6)
        ок, ждать = в.взять("а")
        self.assertFalse(ок)
        self.assertAlmostEqual(ждать, 10.0, places=3)
        self.assertTrue(в.взять("б")[0], "у другого ключа своё ведро")
        сейчас[0] += 10.0
        self.assertTrue(в.взять("а")[0], "через Retry-After — снова можно")
        self.assertFalse(в.взять("а")[0])

    def test_очередь_fifo_предел_и_отмена(self):
        async def сценарий():
            о = svc_limits.Очередь(2, средняя_с=30.0)
            порядок = []
            self.assertEqual(await о.войти(), 0.0)

            async def ждущий(имя):
                await о.войти()
                порядок.append(имя)

            а = asyncio.ensure_future(ждущий("а"))
            б = asyncio.ensure_future(ждущий("б"))
            await asyncio.sleep(0)
            self.assertEqual(о.состояние()["ждут"], 2)
            self.assertFalse(о.свободно())
            with self.assertRaises(svc_limits.ОчередьПолна) as сбой:
                await о.войти()
            self.assertEqual(сбой.exception.повтор_с, 90, "средняя 30 с × (идёт 1 + ждут 2)")
            о.выйти(20.0)
            await asyncio.sleep(0)
            self.assertEqual(порядок, ["а"], "FIFO: первым входит первый ждущий")
            # Ждущий ушёл — его место не держится.
            в = asyncio.ensure_future(ждущий("в"))
            await asyncio.sleep(0)
            в.cancel()
            await asyncio.sleep(0)
            self.assertEqual(о.состояние()["ждут"], 1)
            о.выйти(20.0)
            await asyncio.sleep(0)
            self.assertEqual(порядок, ["а", "б"])
            о.выйти(20.0)
            self.assertEqual(о.состояние()["идёт"], 0)
            # Отмена после передачи места — место уходит следующему, а не теряется.
            await о.войти()
            г = asyncio.ensure_future(о.войти())
            д = asyncio.ensure_future(ждущий("д"))
            await asyncio.sleep(0)
            о.выйти(10.0)                       # место передано «г»
            г.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(порядок, ["а", "б", "д"])
            self.assertEqual((о.состояние()["отказано"], о.состояние()["обслужено"]), (1, 4))
            await asyncio.gather(а, б, д, return_exceptions=True)
            о.выйти(10.0)                       # «д» закончил
            self.assertEqual(о.состояние()["идёт"], 0)
            # Запись — без await: всплеск синхронных записей не обгоняет очередь.
            з = [о.записаться() for _ in range(3)]
            with self.assertRaises(svc_limits.ОчередьПолна):
                о.записаться()
            self.assertEqual((з[0].done(), з[1].done(), о.состояние()["ждут"]), (True, False, 2))
            о.отменить(з[0])                    # место первого — второму
            self.assertTrue(з[1].done())
            о.отменить(з[2])                    # ждущий ушёл
            self.assertEqual(о.состояние()["ждут"], 0)
            о.отменить(з[1])
            self.assertEqual(о.состояние()["идёт"], 0)

        asyncio.run(сценарий())


class ТокеныСервиса(unittest.TestCase):
    def test_шаблон_чата_как_на_воротах_0(self):
        с = [{"role": "user", "content": "Привет"}]
        self.assertEqual(svc_tokens.шаблон(с),
                         "<|im_start|>user\nПривет<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n")
        т = svc_tokens.Токенизатор(счётчик=len)
        self.assertEqual(т.вход(с), len(svc_tokens.шаблон(с)))

    def test_путь_весов_из_модельфайла(self):
        def ответ(request):
            return httpx.Response(200, json={"modelfile": "# x\nFROM /usr/share/ollama/blobs/sha256-abc\nPARAMETER t 0"})
        к = httpx.Client(transport=httpx.MockTransport(ответ))
        self.assertEqual(svc_tokens.путь_весов("http://o", "loc-rag-docs", к), "/usr/share/ollama/blobs/sha256-abc")


def _ollama_подставная(вызовы, ответ_текст="Голубой", причина="stop"):
    async def обработчик(request):
        тело = json.loads(request.content)
        вызовы.append(тело)
        if тело.get("stream"):
            строки = [json.dumps({"message": {"role": "assistant", "content": ч}, "done": False}, ensure_ascii=False)
                      for ч in ("Го", "лу", "бой")]
            строки.append(json.dumps({"done": True, "done_reason": причина, "prompt_eval_count": 26, "eval_count": 3}))
            return httpx.Response(200, content=("\n".join(строки) + "\n").encode())
        return httpx.Response(200, json={"message": {"role": "assistant", "content": ответ_текст}, "done": True,
                                         "done_reason": причина, "prompt_eval_count": 26, "eval_count": 5})
    return httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(обработчик))


class _ОсноваШлюза(unittest.TestCase):
    """Шлюз на подставной Ollama и подставном токенизаторе (символы / 4)."""

    def setUp(self):
        from fastapi.testclient import TestClient
        self.каталог = tempfile.mkdtemp(prefix="шлюз-")
        self.addCleanup(shutil.rmtree, self.каталог, True)
        self.ключи = svc_keys.Ключи(os.path.join(self.каталог, "keys.json"))
        self.к_чат = self.ключи.создать("чат", ["чат"])
        self.к_док = self.ключи.создать("док", ["документы"])
        self.к_админ = self.ключи.создать("админ", ["админ"])
        self.журнал_путь = os.path.join(self.каталог, "requests.jsonl")
        self.вызовы = []
        self.документы = []
        self.сервис = svc_gateway.Сервис(
            svc_config.Настройки(), self.ключи, svc_journal.Журнал(self.журнал_путь),
            svc_tokens.Токенизатор(счётчик=lambda т: len(т) // 4), _ollama_подставная(self.вызовы),
            отвечатель=self._ответ_документы, состояние=lambda: {"network": {"внешних": 0}})
        self.app = svc_gateway.создать_приложение(self.сервис)
        self.клиент = TestClient(self.app, base_url="https://testserver", client=("192.168.1.31", 50000))

    def _ответ_документы(self, вопрос):
        self.документы.append(вопрос)
        return {"answer": "Не позднее 20 числа.", "abstain": False, "clarification": "",
                "sources": [{"n": 1, "source": "договор.pdf", "section": "3", "page": 4, "chunk_id": "struct-1"}],
                "quotes": [{"n": 1, "text": "не позднее 20 числа", "check": "дословно", "confirmed": True}],
                "usage": {"prompt_tokens": 1800, "completion_tokens": 70}, "timings": {}}

    def h(self, ключ):
        return {"Authorization": f"Bearer {ключ}"}


class ШлюзOpenAI(_ОсноваШлюза):
    def test_здоровье_модели_и_доступ(self):
        self.assertEqual(self.клиент.get("/health").json()["model"], "loc-rag-docs")
        self.assertEqual(self.клиент.get("/v1/models").status_code, 401)
        r = self.клиент.get("/v1/models", headers=self.h("sk-day30-unknownkey"))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (401, "invalid_api_key"))
        self.assertEqual([м["id"] for м in self.клиент.get("/v1/models", headers=self.h(self.к_чат)).json()["data"]],
                         ["loc-rag-docs"])
        from fastapi.testclient import TestClient
        чужой = TestClient(self.app, base_url="https://testserver", client=("10.99.0.2", 1))
        r = чужой.get("/health")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "forbidden_network"),
                         "чужая подсеть — 403 до ключа, даже /health")

    def test_чат_без_потока(self):
        r = self.клиент.post("/v1/chat/completions", headers=self.h(self.к_чат),
                             json={"messages": [{"role": "user", "content": "Цвет неба?"}], "max_tokens": 2000,
                                   "temperature": 0.3})
        self.assertEqual(r.status_code, 200, r.text)
        д = r.json()
        self.assertEqual((д["object"], д["choices"][0]["message"]["content"], д["choices"][0]["finish_reason"]),
                         ("chat.completion", "Голубой", "stop"))
        self.assertEqual(д["usage"], {"prompt_tokens": 26, "completion_tokens": 5, "total_tokens": 31})
        self.assertEqual(r.headers["x-max-tokens-clamped"], "2000->600")
        т = self.вызовы[-1]
        self.assertEqual((т["model"], т["think"], т["keep_alive"], т["stream"]), ("loc-rag-docs", False, "30m", False))
        self.assertEqual(т["options"], {"num_ctx": 4096, "num_predict": 600, "temperature": 0.3})

    def test_поток_в_формате_openai(self):
        with self.клиент.stream("POST", "/v1/chat/completions", headers=self.h(self.к_чат),
                                json={"stream": True, "messages": [{"role": "user", "content": "Цвет?"}]}) as r:
            текст = "".join(r.iter_text())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(текст.startswith(": очередь 0\n\n"))
        куски = [json.loads(с[6:]) for с in текст.split("\n\n") if с.startswith("data: {")]
        self.assertEqual("".join(к["choices"][0]["delta"].get("content", "") for к in куски), "Голубой")
        self.assertEqual(куски[0]["choices"][0]["delta"]["role"], "assistant")
        self.assertEqual(куски[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(куски[-1]["usage"]["prompt_tokens"], 26)
        self.assertTrue(текст.rstrip().endswith("data: [DONE]"))
        self.assertEqual(self.сервис.очередь.состояние()["идёт"], 0, "место освобождено")

    def test_отказы_до_модели(self):
        большой = {"messages": [{"role": "user", "content": "слово " * 3000}]}
        r = self.клиент.post("/v1/chat/completions", headers=self.h(self.к_чат), json=большой)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (413, "context_length_exceeded"))
        self.assertIn("предела 3400", r.json()["error"]["message"])
        r = self.клиент.post("/v1/chat/completions", headers=self.h(self.к_чат),
                             json={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (404, "model_not_found"))
        r = self.клиент.post("/v1/chat/completions", headers=self.h(self.к_чат), json={"messages": [{"role": "кто", "content": 1}]})
        self.assertEqual(r.status_code, 400)
        r = self.клиент.post("/v1/chat/completions", headers={**self.h(self.к_чат), "Content-Length": "999999"},
                             content=b"{}")
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.вызовы, [], "ни один отказ не дошёл до модели")

    def test_лимит_ключа_и_очередь(self):
        коды = [self.клиент.get("/v1/models", headers=self.h(self.к_админ)).status_code for _ in range(7)]
        self.assertEqual(коды, [200] * 6 + [429])
        r = self.клиент.get("/v1/models", headers=self.h(self.к_админ))
        self.assertEqual(r.json()["error"]["code"], "rate_limited")
        self.assertTrue(int(r.headers["retry-after"]) >= 1)
        о = self.сервис.очередь
        о._занято = True
        о._ждут.extend(object() for _ in range(о.ждущих_макс))       # очередь полна
        r = self.клиент.post("/v1/chat/completions", headers=self.h(self.к_чат),
                             json={"messages": [{"role": "user", "content": "x"}]})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (429, "queue_full"))
        self.assertIn("retry-after", r.headers)
        о._ждут.clear()
        о._занято = False

    def test_всплеск_потоков_без_гонки(self):
        """Нагрузка, ступень 8: проверка «место есть» и запись в очередь были разнесены, и при всплеске
        лишний поток получал HTTP 200 с queue_full внутри. Теперь место занимается сразу."""
        async def медленная(request):
            await asyncio.sleep(0.3)
            строки = [json.dumps({"message": {"role": "assistant", "content": "да"}, "done": False}),
                      json.dumps({"done": True, "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 1})]
            return httpx.Response(200, content=("\n".join(строки) + "\n").encode())
        self.сервис.ollama = httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(медленная))
        ключи = [self.ключи.создать(f"всплеск-{i}", ["чат"]) for i in range(7)]

        async def всплеск():
            транспорт = httpx.ASGITransport(app=self.app, client=("192.168.1.31", 1))
            async with httpx.AsyncClient(transport=транспорт, base_url="https://testserver", timeout=60) as к:
                async def один(ключ):
                    r = await к.post("/v1/chat/completions", headers=self.h(ключ),
                                     json={"stream": True, "messages": [{"role": "user", "content": "x"}]})
                    return r.status_code, r.text, r.headers.get("retry-after")
                return await asyncio.gather(*(один(к_) for к_ in ключи))

        итоги = asyncio.run(всплеск())
        коды = sorted(к for к, _, _ in итоги)
        self.assertEqual(коды, [200] * 5 + [429] * 2, "1 генерация + 4 ждут; остальным — честный 429")
        self.assertTrue(all("queue_full" in т and п for к, т, п in итоги if к == 429))
        self.assertTrue(all("queue_full" not in т and "[DONE]" in т for к, т, _ in итоги if к == 200),
                        "у принятых нет ошибки внутри потока")
        self.assertEqual(self.сервис.очередь.состояние()["идёт"], 0)

    def test_журнал_пишет_коды_отказов(self):
        self.клиент.get("/v1/models")
        self.клиент.get("/v1/models", headers=self.h("sk-day30-unknownkey"))
        self.клиент.post("/v1/documents/answer", headers=self.h(self.к_чат), json={"question": "x"})
        for _ in range(7):
            self.клиент.get("/v1/models", headers=self.h(self.к_админ))
        записи = [json.loads(с) for с in pathlib.Path(self.журнал_путь).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(з["код"], з.get("ошибка")) for з in записи[:3]],
                         [(401, "missing_api_key"), (401, "invalid_api_key"), (403, "permission_denied")])
        self.assertEqual(записи[2]["ключ"], "чат", "у отказа по праву ключ известен")
        self.assertEqual((записи[-1]["код"], записи[-1].get("ошибка")), (429, "rate_limited"))

    def test_журнал_без_текстов(self):
        секрет = "Секретный вопрос про договор номер сорок два"
        self.клиент.post("/v1/chat/completions", headers=self.h(self.к_чат),
                         json={"messages": [{"role": "user", "content": секрет}]})
        текст = pathlib.Path(self.журнал_путь).read_text(encoding="utf-8")
        self.assertNotIn(секрет, текст)
        self.assertNotIn("Голубой", текст)
        запись = json.loads(текст.strip().splitlines()[-1])
        self.assertEqual((запись["адрес"], запись["ключ"], запись["код"], запись["вход"]),
                         ("192.168.1.31", "чат", 200, len(svc_tokens.шаблон([{"role": "user", "content": секрет}])) // 4))


class ДокументыСервиса(_ОсноваШлюза):
    def test_права_формат_и_пределы(self):
        вопрос = {"question": "До какого числа платить?"}
        self.assertEqual(self.клиент.post("/v1/documents/answer", headers=self.h(self.к_чат), json=вопрос).status_code, 403)
        r = self.клиент.post("/v1/documents/answer", headers=self.h(self.к_док), json=вопрос)
        self.assertEqual(r.status_code, 200, r.text)
        д = r.json()
        self.assertEqual((д["object"], д["answer"], д["quotes"][0]["check"], д["sources"][0]["chunk_id"]),
                         ("documents.answer", "Не позднее 20 числа.", "дословно", "struct-1"))
        r = self.клиент.post("/v1/documents/answer", headers=self.h(self.к_док), json={"question": "я" * 1001})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (413, "question_too_long"))
        self.assertEqual(self.документы, ["До какого числа платить?"])
        self.assertEqual(self.сервис.очередь.состояние()["идёт"], 0)

    def test_ошибка_отвечателя_освобождает_очередь(self):
        def сломанный(_):
            raise RuntimeError("индекс недоступен")
        self.сервис.отвечатель = сломанный
        r = self.клиент.post("/v1/documents/answer", headers=self.h(self.к_док), json={"question": "x"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (502, "documents_error"))
        self.assertEqual(self.сервис.очередь.состояние()["идёт"], 0)


class СлужбаИСертификаты(unittest.TestCase):
    def test_unit_файл(self):
        т = svc_unit.текст("/venv/bin/python", "/корень", "/корень/memory")
        for нужно in ("ExecStart=/venv/bin/python /корень/service.py", "Restart=on-failure",
                      "Environment=MEMORY_DIR=/корень/memory", "WorkingDirectory=/корень"):
            self.assertIn(нужно, т)

    @unittest.skipUnless(shutil.which("openssl"), "нужен openssl")
    def test_сертификат_своего_центра(self):
        каталог = tempfile.mkdtemp(prefix="tls-")
        self.addCleanup(shutil.rmtree, каталог, True)
        итог = svc_certs.выпустить(каталог, ["127.0.0.1", "192.168.1.43", ""], ["localhost"], 30)
        self.assertEqual(set(итог["san"]), {"127.0.0.1", "192.168.1.43", "localhost"})
        центр = pathlib.Path(итог["ca"]).read_text()
        итог2 = svc_certs.выпустить(каталог, ["127.0.0.1", "192.168.1.50"], ["localhost"], 30)
        self.assertEqual(pathlib.Path(итог2["ca"]).read_text(), центр, "новый адрес — тот же центр: клиентам ca.crt не менять")
        self.assertIn("192.168.1.50", итог2["san"])
        self.assertEqual(oct(os.stat(итог2["key"]).st_mode & 0o777), "0o600")
        проверка = subprocess.run(["openssl", "verify", "-CAfile", итог["ca"], итог2["crt"]], capture_output=True, text=True)
        self.assertIn("OK", проверка.stdout)


class НагрузкаСервиса(unittest.TestCase):
    def test_сводка_ступеней(self):
        def з(ступень, длит, код=200, отказов=0, ожидание=0.0, ошибка=""):
            return {"вид": "чат", "ступень": ступень, "код": код, "длительность_с": длит, "отказов_429": отказов,
                    "ожидание_с": ожидание, "первый_токен_с": 1.0, "начало": 0.0, "конец": 60.0, "ошибка": ошибка,
                    "ответ": 50, "причина": "stop"}
        прогон = {"метка": "т", "адрес": "https://x", "ресурсы": {"2": {"пик_мб": 3000, "cpu_с": 90}},
                  "записи": [з(1, 30), з(2, 30), з(2, 60, отказов=2, ожидание=30), з(2, 0, код=502, ошибка="ollama_error"),
                             {"вид": "документы", "ступень": 0, "код": 200, "длительность_с": 120, "отказов_429": 0,
                              "начало": 0.0, "конец": 120.0, "ошибка": "", "ожидание_с": 0}]}
        св = svc_load.сводка(прогон)
        с1, с2 = св["ступени"]
        self.assertEqual((с1["клиентов"], с1["успешно"], с1["ошибок"]), (1, 1, 0))
        self.assertEqual((с2["запросов"], с2["успешно"], с2["ошибок"], с2["отказов_429"], с2["повторов"]), (3, 2, 1, 2, 1))
        self.assertEqual((с2["задержка_p50_с"], с2["задержка_p95_с"], с2["ожидание_p50_с"]), (45.0, 58.5, 15.0))
        self.assertEqual(с2["ресурсы"]["пик_мб"], 3000)
        self.assertEqual(св["документы"]["успешно"], 1)
        self.assertEqual(св["всего_ошибок"], 1, "429 — работа лимитов, не ошибка")

    def test_полный_прогон_не_заслоняется_коротким(self):
        каталог = tempfile.mkdtemp(prefix="прогоны-")
        self.addCleanup(shutil.rmtree, каталог, True)
        def прогон(имя, ступени, когда):
            путь = os.path.join(каталог, f"run-{имя}.json")
            записи = [{"вид": "чат", "ступень": с, "код": 200} for с in ступени]
            pathlib.Path(путь).write_text(_json.dumps({"метка": имя, "записи": записи}), encoding="utf-8")
            os.utime(путь, (когда, когда))
        прогон("старый", [1, 2, 4, 8], 1000)
        self.assertEqual(svc_load.полный(каталог)["файл"], "run-старый.json")
        прогон("полный", [1, 1, 2, 2, 4, 8], 2000)
        прогон("показ", [8] * 8, 3000)
        прогон("консоль", [2, 2], 4000)
        self.assertEqual(svc_load.последний(каталог)["файл"], "run-консоль.json")
        self.assertEqual(svc_load.полный(каталог)["файл"], "run-полный.json")
        пусто = tempfile.mkdtemp(prefix="прогоны-")
        self.addCleanup(shutil.rmtree, пусто, True)
        прогон_пусто = os.path.join(пусто, "run-x.json")
        pathlib.Path(прогон_пусто).write_text(_json.dumps({"записи": [{"вид": "чат", "ступень": 8}]}), encoding="utf-8")
        self.assertIsNone(svc_load.полный(пусто))

    def _клиент_нагрузки(self, отказов: int, повтор: str):
        import asyncio
        import service_load
        вызовов = []
        def ответ(запрос):
            вызовов.append(1)
            if len(вызовов) <= отказов:
                return httpx.Response(429, headers={"retry-after": повтор},
                                      json={"error": {"code": "queue_full"}})
            тело = ('data: {"choices": [{"delta": {"content": "Да"}}]}\n\n'
                    'data: {"choices": [{"delta": {}, "finish_reason": "stop"}], "x_queue_wait": 0,'
                    ' "usage": {"prompt_tokens": 5, "completion_tokens": 1}}\n\ndata: [DONE]\n\n')
            return httpx.Response(200, text=тело, headers={"content-type": "text/event-stream"})
        async def прогон():
            async with httpx.AsyncClient(transport=httpx.MockTransport(ответ), base_url="https://x") as к:
                return await service_load._чат(к, "sk-test", "вопрос", 10)
        return asyncio.run(прогон()), вызовов, service_load

    def test_клиент_нагрузки_повторяет_до_срока_а_не_по_числу_попыток(self):
        # Retry-After — подсказка сервиса по медиане недавних генераций; после коротких
        # проверочных запросов она мала, и клиент с пределом 12 попыток сдавался (показ, дубль 2).
        запись, вызовов, _ = self._клиент_нагрузки(30, "0")
        self.assertEqual((запись["код"], запись["ошибка"], запись["отказов_429"]), (200, "", 30))
        self.assertEqual((запись["ответ"], запись["причина"]), (1, "stop"))
        self.assertEqual(len(вызовов), 31)

    def test_клиент_нагрузки_сдаётся_по_сроку(self):
        import service_load
        with mock.patch.object(service_load, "СРОК_С", 5):
            запись, вызовов, _ = self._клиент_нагрузки(100, "60")
        self.assertEqual(len(вызовов), 1, "пауза дольше срока — не ждать её зря")
        self.assertIn("429", запись["ошибка"])
        self.assertIsNone(запись["длительность_с"])


class КонсольИВебДня30(_ОсноваШлюза):
    def test_отчёт_консольной_проверки_без_ключей(self):
        import check_cli
        текст = "Ключ:\n" + "sk-day30-" + "x9_-" * 11 + "\nи sk-day30-…"   # выдуманный ключ
        скрыто = check_cli.скрыть_ключи(текст)
        self.assertNotRegex(скрыто, r"sk-day30-[A-Za-z0-9_-]{8,}")
        self.assertIn("sk-day30-…(скрыт в отчёте)", скрыто)
        self.assertIn("и sk-day30-…", скрыто, "многоточие из документации не трогается")
        токен = "123456789:" + "AAbbCC_dd-" * 4                     # выдуманный токен Telegram
        self.assertNotIn(токен, check_cli.скрыть_ключи(f"токен {токен} и путь /bot{токен}/getMe"))

    def test_ключи_из_консоли(self):
        память = tempfile.mkdtemp(prefix="консоль-сервиса-")
        self.addCleanup(shutil.rmtree, память, True)
        окр = {**os.environ, "MEMORY_DIR": память, "PYTHONIOENCODING": "utf-8"}
        запуск = lambda *а: subprocess.run([sys.executable, "cli.py", *а], capture_output=True, text=True, env=окр,
                                           cwd=os.path.dirname(os.path.abspath(__file__)), timeout=120)
        r = запуск("--сервис-ключ", "телефон", "--права", "чат,документы")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"sk-day30-\S{30,}")
        self.assertIn("только хэш", r.stdout)
        r = запуск("--сервис-ключи")
        self.assertIn("телефон", r.stdout)
        self.assertNotIn("sk-day30-", r.stdout, "список ключей не показывает ключи")
        self.assertIn("отозван.", запуск("--сервис-отозвать", "телефон").stdout)
        r = запуск("--сервис-ключ", "x", "--права", "всё")
        self.assertEqual(r.returncode, 1)
        self.assertIn("права", r.stderr)

    def test_страницы_и_служебные_ручки(self):
        for путь, нужно in (("/chat", 'id="ключ"'), ("/chat", "/v1/chat/completions"), ("/admin", "/admin/api/state")):
            r = self.клиент.get(путь)
            self.assertEqual(r.status_code, 200)
            self.assertIn(нужно, r.text)
            self.assertNotIn("cdn.", r.text, "страницы без внешних скриптов")
        self.assertEqual(self.клиент.get("/admin/api/state", headers=self.h(self.к_чат)).status_code, 403)
        с = self.клиент.get("/admin/api/state", headers=self.h(self.к_админ)).json()
        self.assertEqual(с["service"]["лимиты"]["вход_токенов"], 3400)
        self.assertEqual(с["network"], {"внешних": 0})
        self.assertEqual({к["имя"] for к in с["keys"]}, {"чат", "док", "админ"})
        self.assertTrue(all("хэш" not in к for к in с["keys"]))


@unittest.skipUnless(ЖИВЫЕ, "нужен ключ API; запускать с --живые")
class ЖивыеПроверки(unittest.TestCase):

    def test_маршрутизатор_отличает_вопрос_от_факта(self):
        from agent.llm import Client
        from agent.memory.router import Router
        клиент = Client()
        try:
            маршрутизатор = Router(клиент)
            вопрос = маршрутизатор.classify("А как в GeoDjango сделать индекс по геометрии?")
            факт = маршрутизатор.classify("У нас в схеме gisdata 37 таблиц")
            self.assertFalse(вопрос.wants_write, f"вопрос принят за факт: {вопрос.to_dict()}")
            self.assertTrue(факт.wants_write or факт.failed, факт.to_dict())
        finally:
            клиент.close()

    def test_агент_отвечает_и_не_нарушает_инвариантов(self):
        from agent import MemoryAgent
        каталог = tempfile.mkdtemp()
        агент = MemoryAgent(base_dir=каталог, router_mode=OFF, temperature=0.0)
        try:
            ответ = агент.ask("Какой ORM использовать для геометрии в новой системе?")
            self.assertTrue(ответ.text)
            self.assertFalse(ответ.blocked, [str(н) for н in ответ.violations])
            self.assertIn(LONG, ответ.layers())
        finally:
            агент.close()
            shutil.rmtree(каталог, ignore_errors=True)



# --- День 30, дополнение: Telegram-бот ------------------------------------------------

from agent.service import pd as svc_pd  # noqa: E402


class МаскаПД(unittest.TestCase):
    """agent/service/pd.py: те же шаблоны, что у Дня 29, и маска «[скрыто]» перед отправкой в Telegram."""

    def setUp(self):
        self.корень = tempfile.mkdtemp(prefix="пд-")
        self.addCleanup(shutil.rmtree, self.корень, True)
        файл = os.path.join(self.корень, "pd-names.txt")
        pathlib.Path(файл).write_text("# основы фамилий\nВымыслов\nПридумк\n", encoding="utf-8")
        прежний = os.environ.get("PD_NAMES")
        os.environ["PD_NAMES"] = файл
        self.addCleanup(lambda: os.environ.pop("PD_NAMES", None) if прежний is None
                        else os.environ.__setitem__("PD_NAMES", прежний))

    def test_каждый_вид_скрыт_а_остальной_текст_цел(self):
        текст = ("Контактное лицо: Вымыслов А. Б., тел. +7 (912) 345-67-89, e-mail ab.cd@mail.ru. "
                 "Подписал И.И. Иванов; ответственный — Сергей Петрович Придумкина. "
                 "Срок оплаты: не позднее 20 числа текущего месяца.")
        self.assertTrue(svc_pd.найти(текст))
        скрыт, виды = svc_pd.скрыть(текст)
        self.assertEqual(svc_pd.найти(скрыт), [], скрыт)
        for кусок in ("Вымыслов", "912", "ab.cd", "Иванов", "Петрович", "Сергей", "Придумкина"):
            self.assertNotIn(кусок, скрыт)
        self.assertIn("Срок оплаты: не позднее 20 числа текущего месяца.", скрыт)
        self.assertIn(svc_pd.МАСКА, скрыт)
        self.assertEqual(set(виды), {"фамилия и инициалы", "инициалы и фамилия", "отчество",
                                     "фамилия из списка", "e-mail", "телефон"}, виды)
        self.assertTrue(all(isinstance(n, int) and n > 0 for n in виды.values()))

    def test_фамилия_от_основы_целым_словом(self):
        скрыт, виды = svc_pd.скрыть("Согласовано: Вымысловский и Придумкиной.")
        self.assertEqual(скрыт, f"Согласовано: {svc_pd.МАСКА} и {svc_pd.МАСКА}.")
        self.assertEqual(виды, {"фамилия из списка": 2})

    def test_инициалы_без_второй_точки_и_остатки(self):
        # Живой ответ /doc (показ, сцена 5): «… клиентам М.В Фамилия — … инженер А В.[скрыто]» — фамилии нет
        # в списке, а у инициалов нет второй точки: шаблон Дня 29 их не узнавал.
        for текст in ("Начальник отдела М.В Выдуманный подписал", "Подписал: Выдуманный М.В", "инженер А В. Выдуманный",
                      "инженер А.В.Выдуманный", "согласовано: А. В. Выдуманный"):
            self.assertTrue(svc_pd.найти(текст), текст)
            скрыт, виды = svc_pd.скрыть(текст)
            self.assertNotIn("Выдуманный", скрыт, текст)
            self.assertEqual(svc_pd.найти(скрыт), [], скрыт)
        скрыт, _ = svc_pd.скрыть("главный инженер А В.Придумкин (действующий на основании)")
        self.assertEqual(скрыт, f"главный инженер {svc_pd.МАСКА} (действующий на основании)", "инициалы не остаются рядом с маской")
        for чисто in ("Договор № 337 от 09.09.2024", "п. 3.5 Порядок расчетов", "ПАО «Ростелеком», г. Москва"):
            self.assertEqual(svc_pd.скрыть(чисто), (чисто, {}), чисто)

    def test_латинские_двойники_букв(self):
        # Живой ответ /doc: в распознанном договоре инициалы — латиницей («M.B», буквы LATIN), фамилия —
        # кириллицей; шаблоны с [А-ЯЁ] их не видели — фамилия ушла в Telegram (показ, сцена 5).
        латиница = "Начальник отдела M.B Выдуманный, тел."
        self.assertIn("инициалы и фамилия", svc_pd.найти(латиница))
        скрыт, виды = svc_pd.скрыть(латиница)
        self.assertEqual(скрыт, f"Начальник отдела {svc_pd.МАСКА}, тел.")
        смесь = "Ответственный: B" + "ымыслов"          # фамилия из списка с латинской «B»
        self.assertIn("фамилия из списка", svc_pd.найти(смесь))
        self.assertNotIn("ымыслов", svc_pd.скрыть(смесь)[0])
        for чисто in ("Telegram Bot API, 4818 MB, HTTP 200", "ООО «Ромашка», ИНН указан в разделе 9"):
            self.assertEqual(svc_pd.скрыть(чисто), (чисто, {}), чисто)

    def test_без_пд_ничего_не_меняется(self):
        текст = "Срок ежемесячной оплаты Услуг: не позднее 20 числа текущего месяца оказания Услуг."
        self.assertEqual(svc_pd.скрыть(текст), (текст, {}))

    def test_без_списка_фамилий_общие_шаблоны_и_признак(self):
        self.assertTrue(svc_pd.список_есть())
        os.environ["PD_NAMES"] = os.path.join(self.корень, "нет-такого.txt")
        self.assertFalse(svc_pd.список_есть())
        скрыт, виды = svc_pd.скрыть("Вымыслов, тел. 8 912 345 67 89")
        self.assertIn("Вымыслов", скрыт, "без списка фамилия не узнаётся — об этом бот предупреждает")
        self.assertEqual(виды, {"телефон": 1})

    def test_день_29_тот_же(self):
        # optbench.без_пд теперь берёт шаблоны отсюда — находки те же.
        for текст in ("подписал Иванов И. И.", "Сергей Петрович", "пишите на ab.cd@mail.ru", "Вымыслов"):
            self.assertEqual(rag_optbench.без_пд(текст), svc_pd.найти(текст), текст)
        self.assertIs(rag_optbench.фамилии, svc_pd.фамилии)


class СетьБота(_ОфлайнОснова):
    """offline.py: разрешённый ВНЕШНИЙ хост (OFFLINE_ALLOW_EXTERNAL) и токен Bot API в журнале."""

    ТОКЕН = "123456789:AAH-проба_x9ZZZZZZZZZZZZZZZZZZZZZZZZZZZ".replace("проба", "probe")

    def test_разрешённый_внешний_пропущен_и_посчитан_внешним(self):
        with mock.patch.dict(os.environ, {"OFFLINE_ALLOW_EXTERNAL": "api.telegram.org"}):
            local_offline.включить(True, "тест")
            local_offline._проверить_запрос(_httpx.Request("POST", f"https://api.telegram.org/bot{self.ТОКЕН}/getUpdates"))
            local_offline._проверить_запрос(_httpx.Request("POST", "https://127.0.0.1:8443/v1/chat/completions"))
            with self.assertRaises(local_offline.ОшибкаОфлайна):
                local_offline._проверить_запрос(_httpx.Request("GET", "https://api.deepseek.com/models"))
            self.assertFalse(local_offline.локальный("api.telegram.org"), "внешний остаётся внешним")
        ж = local_offline.журнал()
        self.assertEqual((ж["локальных"], ж["внешних"], ж["заблокировано"]), (1, 1, 1))
        записи = {з["host"]: з for з in ж["recent"]}
        self.assertEqual(записи["api.telegram.org"]["result"], "пропущен")
        self.assertEqual(записи["api.telegram.org"]["path"], "/bot…/getUpdates")

    def test_без_переменной_telegram_заблокирован_как_раньше(self):
        local_offline.включить(True, "тест")
        with self.assertRaises(local_offline.ОшибкаОфлайна):
            local_offline._проверить_запрос(_httpx.Request("GET", f"https://api.telegram.org/bot{self.ТОКЕН}/getMe"))

    def test_токен_не_попадает_в_журнал_и_разницу(self):
        до = local_offline.снимок_журнала()
        local_offline._проверить_запрос(_httpx.Request("POST", f"https://api.telegram.org/bot{self.ТОКЕН}/sendMessage"))
        local_offline._проверить_запрос(_httpx.Request("POST", f"http://127.0.0.1:8081/bot{self.ТОКЕН}/editMessageText"))
        дамп = _json.dumps(local_offline.журнал(), ensure_ascii=False)
        self.assertNotIn(self.ТОКЕН.split(":")[1], дамп)
        self.assertNotIn(self.ТОКЕН.split(":")[0], дамп)
        self.assertIn("/bot…/sendMessage", дамп)
        self.assertEqual(local_offline.разница(до, local_offline.снимок_журнала())["хосты"], ["api.telegram.org"])



from agent.service import telegram as svc_tg  # noqa: E402


class _ФальшивыйTelegram:
    """Bot API в памяти: записывает вызовы (метод, тело), выдаёт message_id; отказы задаются заранее."""

    def __init__(self):
        self.вызовы: list[tuple[str, dict]] = []
        self.обновления: list[list[dict]] = []
        self.отказы: dict[str, list[tuple[int, dict]]] = {}
        self.номер = 100
        self.не_разбирать = False             # как Telegram, не разобравший HTML: 400 на parse_mode

    def __call__(self, запрос):
        метод = запрос.url.path.rsplit("/", 1)[-1]
        тело = _json.loads(запрос.content or b"{}")
        self.вызовы.append((метод, тело))
        if self.не_разбирать and тело.get("parse_mode"):
            return httpx.Response(400, json={"ok": False, "error_code": 400,
                                             "description": "Bad Request: can't parse entities"})
        if self.отказы.get(метод):
            код, данные = self.отказы[метод].pop(0)
            return httpx.Response(код, json=данные)
        if метод == "sendMessage":
            self.номер += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.номер, "text": тело.get("text")}})
        if метод == "editMessageText":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": тело.get("message_id")}})
        if метод == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "TestBot", "can_join_groups": False}})
        if метод == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": self.обновления.pop(0) if self.обновления else []})
        return httpx.Response(200, json={"ok": True, "result": True})

    def тексты(self, метод=None):
        return [т.get("text") for м, т in self.вызовы if метод in (None, м) and "text" in т]

    def методы(self):
        return [м for м, _ in self.вызовы]


class _ФальшивыйСервис:
    """Сервис Дня 30 в памяти: ответы по очереди из списка, запросы записываются."""

    def __init__(self):
        self.чат: list = []
        self.документы: list = []
        self.запросы: list[tuple[str, dict]] = []
        self.очереди: list[dict] = []         # что отдаёт /health по очереди; пусто — «ждут 2 из 4»

    def __call__(self, запрос):
        путь = запрос.url.path
        тело = _json.loads(запрос.content) if запрос.content else {}
        self.запросы.append((путь, тело))
        if путь == "/health":
            очередь = self.очереди.pop(0) if self.очереди else {"идёт": 1, "ждут": 2}
            return httpx.Response(200, json={"status": "ok", "model": "loc-rag-docs", "queue": {
                "мест_в_очереди": 4, "средняя_с": 26.2, "отказано": 0, "обслужено": 9, **очередь}})
        очередь = self.чат if путь == "/v1/chat/completions" else self.документы
        ответ = очередь.pop(0)
        if isinstance(ответ, Exception):
            raise ответ
        код, заголовки, тело_ответа = ответ
        if isinstance(тело_ответа, str):
            return httpx.Response(код, headers=заголовки, text=тело_ответа)
        return httpx.Response(код, headers=заголовки, json=тело_ответа)


def _sse(*куски, вход=5, ответ=2, причина="stop", впереди=0, ошибка=None):
    строки = [f": очередь {впереди}\n\n"]
    for кусок in куски:
        строки.append("data: " + _json.dumps({"choices": [{"index": 0, "delta": {"content": кусок},
                                                          "finish_reason": None}]}, ensure_ascii=False) + "\n\n")
    if ошибка:
        строки.append("data: " + _json.dumps({"error": {"message": "сбой", "code": ошибка}}) + "\n\n")
    else:
        строки.append("data: " + _json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": причина}],
                                              "usage": {"prompt_tokens": вход, "completion_tokens": ответ},
                                              "x_queue_wait": 0.0}) + "\n\n")
    строки.append("data: [DONE]\n\n")
    return (200, {"content-type": "text/event-stream"}, "".join(строки))


def _отказ(код, code, повтор=None):
    заголовки = {"retry-after": str(повтор)} if повтор is not None else {}
    return (код, заголовки, {"error": {"message": f"отказ {code}", "type": "x", "code": code}})


class БотTelegram(unittest.IsolatedAsyncioTestCase):
    """agent/service/telegram.py: бот — клиент сервиса; Telegram и сервис подменены (MockTransport)."""

    ВЛАДЕЛЕЦ = 918273645    # девять цифр: проверка «ID не в состоянии» не спутает его с PID или временем

    def setUp(self):
        self.каталог = tempfile.mkdtemp(prefix="бот-")
        self.addCleanup(shutil.rmtree, self.каталог, True)
        self.tg, self.сервис, self.паузы = _ФальшивыйTelegram(), _ФальшивыйСервис(), []
        self.номер_обновления = 0

    def бот(self, владелец=ВЛАДЕЛЕЦ, правка_с=0.0):
        api = svc_tg.BotAPI("123:TESTTOKEN", база="http://127.0.0.1:18080", transport=httpx.MockTransport(self.tg))
        сервис = svc_tg.КлиентСервиса("https://svc.test", "sk-day30-test", transport=httpx.MockTransport(self.сервис))

        async def пауза(секунд):
            self.паузы.append(секунд)
        return svc_tg.Бот(api, сервис, владелец=владелец, каталог=self.каталог, пауза=пауза, правка_с=правка_с)

    def обн(self, текст, от=ВЛАДЕЛЕЦ, тип="private", чат=None):
        self.номер_обновления += 1
        return {"update_id": self.номер_обновления, "message": {
            "message_id": self.номер_обновления, "from": {"id": от, "is_bot": False, "first_name": "Т"},
            "chat": {"id": чат if чат is not None else от, "type": тип}, "date": int(time.time()), "text": текст}}

    async def прогон(self, бот, *тексты, **ещё):
        # Как человек: следующее сообщение — после ответа на предыдущее (одновременные — отдельный тест).
        for т in тексты:
            await бот.обработать(self.обн(т, **ещё))
            await бот.дождаться()

    def журнал(self):
        путь = os.path.join(self.каталог, "telegram.jsonl")
        return [_json.loads(с) for с in pathlib.Path(путь).read_text(encoding="utf-8").splitlines()] if os.path.exists(путь) else []

    async def test_владелец_получает_ответ_потоком_с_подписью(self):
        self.сервис.чат.append(_sse("При", "вет", "!", вход=24, ответ=3))
        бот = self.бот()
        await self.прогон(бот, "Скажи привет")
        методы = self.tg.методы()
        self.assertEqual(методы[0], "sendMessage", "сначала — заготовка")
        self.assertTrue(self.tg.тексты("sendMessage")[0].startswith("⏳"))
        правки = self.tg.тексты("editMessageText")
        self.assertGreaterEqual(len(правки), 2, правки)
        self.assertTrue(all(а != б for а, б in zip(правки, правки[1:])), "одинаковых правок подряд нет")
        self.assertTrue(правки[-1].startswith("Привет!"))
        self.assertIn("loc-rag-docs · вход 24 · ответ 3 токенов", правки[-1])
        self.assertIn("typing", [т.get("action") for м, т in self.tg.вызовы if м == "sendChatAction"])
        путь, тело = self.сервис.запросы[0]
        self.assertEqual((путь, тело["stream"], тело["messages"][-1]["content"]), ("/v1/chat/completions", True, "Скажи привет"))
        з = self.журнал()[-1]
        self.assertEqual((з["кто"], з["вид"], з["код"], з["ответ"]), ("владелец", "чат", 200, 3))
        await бот.закрыть()

    async def test_правки_не_чаще_порога(self):
        self.сервис.чат.append(_sse(*["слово "] * 30))
        бот = self.бот(правка_с=3600)
        await self.прогон(бот, "много слов")
        self.assertEqual(len(self.tg.тексты("editMessageText")), 1, "только итоговая правка: порог не пройден")
        await бот.закрыть()

    async def test_история_и_new(self):
        self.сервис.чат += [_sse("Париж"), _sse("Около 2 млн"), _sse("Да")]
        бот = self.бот()
        await self.прогон(бот, "Столица Франции?", "А сколько там жителей?")
        второй = self.сервис.запросы[1][1]["messages"]
        self.assertEqual([с["role"] for с in второй], ["user", "assistant", "user"])
        self.assertEqual(второй[1]["content"], "Париж")
        await self.прогон(бот, "/new", "Ты здесь?")
        self.assertEqual(len(self.сервис.запросы[2][1]["messages"]), 1, "после /new история пуста")
        self.assertIn("заново", self.tg.тексты("sendMessage")[-2])
        await бот.закрыть()

    async def test_история_не_длиннее_предела(self):
        self.сервис.чат += [_sse(f"ответ {i}") for i in range(10)]
        бот = self.бот()
        await self.прогон(бот, *[f"вопрос {i}" for i in range(10)])
        self.assertLessEqual(len(self.сервис.запросы[-1][1]["messages"]), svc_tg.ИСТОРИЯ + 1)
        await бот.закрыть()

    async def test_413_укорачивает_историю_сам(self):
        self.сервис.чат += [_sse("раз"), _sse("два"), _отказ(413, "context_length_exceeded"), _sse("три")]
        бот = self.бот()
        await self.прогон(бот, "первый", "второй", "третий")
        до, после = self.сервис.запросы[2][1]["messages"], self.сервис.запросы[3][1]["messages"]
        self.assertEqual(len(после), len(до) - 2, "убрана самая старая пара")
        self.assertTrue(self.tg.тексты("editMessageText")[-1].startswith("три"))
        self.assertEqual(self.журнал()[-1]["укорочено"], 1)
        await бот.закрыть()

    async def test_413_один_вопрос_длиннее_окна(self):
        self.сервис.чат.append(_отказ(413, "context_length_exceeded"))
        бот = self.бот()
        await self.прогон(бот, "очень длинный вопрос")
        self.assertIn("длиннее окна модели", self.tg.тексты()[-1])
        await бот.закрыть()

    async def test_429_оба_кода_повторяемые(self):
        # queue_full: место ждём по /health (раз в 5 с), не дольше Retry-After; rate_limited — пауза по ведру.
        self.сервис.чат += [_отказ(429, "queue_full", 7), _отказ(429, "rate_limited", 10), _sse("готово")]
        бот = self.бот()
        await self.прогон(бот, "вопрос")
        self.assertEqual(self.паузы, [5.0, 10.0])
        тексты = self.tg.тексты()
        self.assertTrue(any("как только освободится место" in т for т in тексты), тексты)
        self.assertTrue(any("повторю через 10 с" in т for т in тексты), тексты)
        self.assertTrue(тексты[-1].startswith("готово"))
        self.assertEqual(self.журнал()[-1]["повторов_429"], 2)
        await бот.закрыть()

    async def test_queue_full_не_ждёт_весь_retry_after(self):
        # Сервис оценил Retry-After в 741 с (медиана длинных генераций × очередь), а место освободилось
        # через 15 с: бот повторяет, как только /health показывает место (находка check_telegram.py).
        self.сервис.чат += [_отказ(429, "queue_full", 741), _sse("Москва")]
        self.сервис.очереди = [{"идёт": 1, "ждут": 4}, {"идёт": 1, "ждут": 4}, {"идёт": 1, "ждут": 3}]
        бот = self.бот()
        await self.прогон(бот, "столица России?")
        self.assertEqual(self.паузы, [5.0, 5.0, 5.0], "три опроса /health по 5 с, а не 741 с")
        self.assertEqual([п for п, _ in self.сервис.запросы].count("/health"), 3)
        self.assertTrue(self.tg.тексты()[-1].startswith("Москва"))
        await бот.закрыть()

    async def test_queue_full_медленный_health_не_растягивает_ожидание(self):
        # check_cli (шаг 30.10): под нагрузкой /health отвечает секундами, а предел считался только
        # по сумме пауз — повтор уходил позже Retry-After. Теперь предел — и по реальным часам.
        часы = [0.0]
        self.сервис.чат += [_отказ(429, "queue_full", 10), _sse("ок")]
        self.сервис.очереди = [{"идёт": 1, "ждут": 4}] * 5
        api = svc_tg.BotAPI("123:TESTTOKEN", база="http://127.0.0.1:18080", transport=httpx.MockTransport(self.tg))
        сервис = svc_tg.КлиентСервиса("https://svc.test", "sk-day30-test", transport=httpx.MockTransport(self.сервис))
        здоровье = сервис.здоровье

        async def медленное_здоровье():
            часы[0] += 4.0
            return await здоровье()
        сервис.здоровье = медленное_здоровье

        async def пауза(секунд):
            self.паузы.append(секунд)
            часы[0] += секунд
        бот = svc_tg.Бот(api, сервис, владелец=self.ВЛАДЕЛЕЦ, каталог=self.каталог, пауза=пауза, правка_с=0.0,
                         часы=lambda: часы[0])
        await self.прогон(бот, "вопрос")
        self.assertEqual(self.паузы, [5.0, 1.0], "5 с, /health (+4 с), остаток до 10 с — 1 с, повтор")
        self.assertTrue(any("попытка 2" in т for т in self.tg.тексты()), "номер попытки виден")
        await бот.закрыть()

    async def test_queue_full_не_дольше_retry_after(self):
        self.сервис.чат += [_отказ(429, "queue_full", 12), _sse("ок")]
        self.сервис.очереди = [{"идёт": 1, "ждут": 4}] * 10
        бот = self.бот()
        await self.прогон(бот, "вопрос")
        self.assertEqual(sum(self.паузы), 12.0, "мест нет — повтор не позже Retry-After")
        await бот.закрыть()

    async def test_429_не_дольше_срока(self):
        self.сервис.чат.append(_отказ(429, "rate_limited", 5000))
        бот = self.бот()
        await self.прогон(бот, "вопрос")
        self.assertEqual(self.паузы, [], "пауза дольше срока — не ждать её зря")
        self.assertIn("не освободилась", self.tg.тексты()[-1])
        await бот.закрыть()

    async def test_отказы_сервиса_понятным_текстом(self):
        self.сервис.чат += [_отказ(401, "invalid_api_key"), _отказ(403, "permission_denied"),
                            httpx.ConnectError("нет связи"), _отказ(502, "ollama_error"), _sse("нача", ошибка="timeout")]
        бот = self.бот()
        await self.прогон(бот, "1", "2", "3", "4", "5")
        итоги = [т for т in self.tg.тексты("editMessageText")]
        весь = "\n".join(итоги)
        self.assertIn("--телеграм-ключ", весь)
        self.assertIn("нет права", весь)
        self.assertIn("Сервис не отвечает", весь)
        self.assertIn("ollama_error", весь)
        self.assertIn("поток прервался: timeout", итоги[-1])
        self.assertNotIn("нача", итоги[-1], "частичный ответ ошибочного потока не выдаётся за результат")
        await бот.закрыть()

    async def test_чужому_один_отказ_в_сутки_и_ни_одного_запроса_в_сервис(self):
        бот = self.бот()
        await self.прогон(бот, "привет", "ещё раз", от=918273699)
        self.assertEqual(self.tg.тексты(), ["Это частный бот."])
        self.assertEqual(self.сервис.запросы, [])
        записи = self.журнал()
        self.assertEqual([з["кто"] for з in записи], ["чужой", "чужой"])
        self.assertNotIn("918273699", _json.dumps(записи), "ID чужого — только хэш")
        self.assertEqual(len(записи[0]["чей"]), 8)
        await бот.закрыть()

    async def test_группа_покидается(self):
        бот = self.бот()
        await self.прогон(бот, "/start", чат=-100, тип="group")
        self.assertEqual(self.tg.методы(), ["leaveChat"])
        self.assertEqual(self.сервис.запросы, [])
        await бот.закрыть()

    async def test_без_владельца_никому_не_отвечает(self):
        бот = self.бот(владелец=None)
        await self.прогон(бот, "привет")
        self.assertEqual((self.tg.вызовы, self.сервис.запросы), ([], []))
        self.assertEqual(self.журнал()[-1]["вид"], "владелец не задан")
        await бот.закрыть()

    async def test_doc_маскирует_пд_и_проверяет_итог(self):
        файл = os.path.join(self.каталог, "pd-names.txt")
        pathlib.Path(файл).write_text("Вымыслов\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"PD_NAMES": файл}):
            self.сервис.документы.append((200, {"x-queue-wait": "1.5"}, {
                "answer": "Контактное лицо — Вымыслов А. Б., тел. +7 (912) 345-67-89.", "abstain": False,
                "sources": [{"n": 1, "source": "Договор связи.pdf", "section": "18. Контактные данные"}],
                "quotes": [{"n": 1, "text": "Ответственный: Вымыслов, ab.cd@mail.ru", "check": "дословно", "confirmed": True},
                           {"n": 2, "text": "Срок оплаты — до 20 числа", "check": "дословно", "confirmed": True}],
                "usage": {"prompt_tokens": 1800, "completion_tokens": 40}}))
            бот = self.бот()
            await self.прогон(бот, "/doc Кто контактное лицо?")
            итог = self.tg.тексты("editMessageText")[-1]
        self.assertEqual(svc_pd.найти(итог), [], итог)
        for кусок in ("Вымыслов", "912", "ab.cd"):
            self.assertNotIn(кусок, итог)
        self.assertIn(svc_pd.МАСКА, итог)
        self.assertIn("[1] Договор связи.pdf · 18. Контактные данные", итог)
        self.assertIn("✓ дословно", итог)
        self.assertIn("сокращена", итог)
        self.assertIn("Скрыто фильтром ПД: 4", итог)
        self.assertIn("не ловит", итог)
        self.assertEqual(self.сервис.запросы[0], ("/v1/documents/answer", {"question": "Кто контактное лицо?"}))
        з = self.журнал()[-1]
        self.assertEqual((з["вид"], з["скрыто"]), ("документы", 4))
        self.assertNotIn("Вымыслов", _json.dumps(self.журнал(), ensure_ascii=False))
        await бот.закрыть()

    async def test_doc_без_вопроса_и_слишком_длинный(self):
        self.сервис.документы.append(_отказ(413, "question_too_long"))
        бот = self.бот()
        await self.прогон(бот, "/doc", "/doc " + "а" * 1200)
        тексты = self.tg.тексты()
        self.assertIn("/doc До какого числа", тексты[0])
        self.assertIn("длиннее 1000 знаков", тексты[-1])
        await бот.закрыть()

    async def test_doc_не_знаю(self):
        self.сервис.документы.append((200, {}, {"answer": "", "abstain": True, "sources": [], "quotes": [],
                                                "clarification": "Уточните договор.", "usage": {}}))
        бот = self.бот()
        await self.прогон(бот, "/doc что-то")
        итог = self.tg.тексты("editMessageText")[-1]
        self.assertIn("Не знаю", итог)
        self.assertIn("Уточните договор.", итог)
        await бот.закрыть()

    async def test_status_и_help(self):
        бот = self.бот()
        await self.прогон(бот, "/status", "/help")
        статус, помощь = self.tg.тексты()
        self.assertIn("идёт 1, ждут 2 из 4", статус)
        self.assertEqual(self.сервис.запросы[0][0], "/health", "статус — не /v1/*: вне лимита ключа")
        for кусок in ("/doc", "/new", "/status", "не ловит", "только владельцу"):
            self.assertIn(кусок, помощь)
        await бот.закрыть()

    async def test_не_текст(self):
        бот = self.бот()
        обн = self.обн("")
        del обн["message"]["text"]
        обн["message"]["photo"] = [{"file_id": "x"}]
        await бот.обработать(обн)
        await бот.дождаться()
        self.assertIn("только текст", self.tg.тексты()[-1])
        await бот.закрыть()

    async def test_длинный_ответ_двумя_сообщениями(self):
        self.сервис.чат.append(_sse("а" * 2500, "б" * 2500))
        бот = self.бот()
        await self.прогон(бот, "длинно")
        отправлено = self.tg.тексты("sendMessage")
        self.assertEqual(len(отправлено), 2, "заготовка и продолжение")
        self.assertLessEqual(max(len(т) for т in self.tg.тексты()), 4096)
        self.assertTrue(отправлено[-1].rstrip().endswith("токенов · очередь 0.0 с · всего " +
                                                         отправлено[-1].rsplit("всего ", 1)[1]))
        await бот.закрыть()

    async def test_429_telegram_на_правку(self):
        self.tg.отказы["editMessageText"] = [(429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                                                     "parameters": {"retry_after": 3}})]
        self.сервис.чат.append(_sse("один", " два"))
        бот = self.бот()
        await self.прогон(бот, "вопрос")
        self.assertIn(3, self.паузы)
        self.assertTrue(self.tg.тексты("editMessageText")[-1].startswith("один два"))
        self.assertEqual(self.журнал()[-1]["отказов_telegram"], 1)
        await бот.закрыть()

    async def test_одно_дело_за_раз(self):
        self.сервис.чат += [_sse("первый"), _sse("второй")]
        бот = self.бот()
        await бот.обработать(self.обн("вопрос 1"))
        await бот.обработать(self.обн("вопрос 2"))
        await бот.дождаться()
        self.assertTrue(any("отвечу после текущего" in т for т in self.tg.тексты()), self.tg.тексты())
        self.assertEqual([т["messages"][-1]["content"] for _, т in self.сервис.запросы], ["вопрос 1", "вопрос 2"])
        await бот.закрыть()

    async def test_опрос_сохраняет_offset_и_читает_его_после_перезапуска(self):
        self.сервис.чат.append(_sse("ок"))
        self.tg.обновления = [[self.обн("вопрос")]]
        бот = self.бот()
        await бот.опросить_раз()
        await бот.дождаться()
        await бот.закрыть()
        self.assertEqual(pathlib.Path(self.каталог, "telegram-offset").read_text().strip(), "2")
        бот2 = self.бот()
        await бот2.опросить_раз()
        запросы = [т for м, т in self.tg.вызовы if м == "getUpdates"]
        self.assertEqual(запросы[-1]["offset"], 2)
        состояние = _json.loads(pathlib.Path(self.каталог, "telegram-state.json").read_text(encoding="utf-8"))
        self.assertTrue(состояние["владелец_задан"])
        self.assertNotIn(str(self.ВЛАДЕЛЕЦ), _json.dumps(состояние), "ID владельца в состояние не пишется")
        await бот2.закрыть()

    async def test_журнал_без_текстов(self):
        self.сервис.чат.append(_sse("ОТВЕТ-МАРКЕР"))
        бот = self.бот()
        await self.прогон(бот, "ВОПРОС-МАРКЕР")
        дамп = pathlib.Path(self.каталог, "telegram.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("МАРКЕР", дамп)
        self.assertNotIn(str(self.ВЛАДЕЛЕЦ), дамп)
        await бот.закрыть()

    async def test_status_сразу_пока_идёт_ответ(self):
        # Ручная проверка владельца: /status ждал 7 минут за длинным /doc в очереди бота.
        отпустить = asyncio.Event()
        прежний = self.сервис

        async def медленный(запрос):
            if запрос.url.path == "/v1/chat/completions":
                await отпустить.wait()
            return прежний(запрос)
        self.сервис.чат.append(_sse("долгий ответ"))
        api = svc_tg.BotAPI("123:TESTTOKEN", база="http://127.0.0.1:18080", transport=httpx.MockTransport(self.tg))
        сервис = svc_tg.КлиентСервиса("https://svc.test", "sk-day30-test", transport=httpx.MockTransport(медленный))

        async def пауза(секунд):
            self.паузы.append(секунд)
        бот = svc_tg.Бот(api, сервис, владелец=self.ВЛАДЕЛЕЦ, каталог=self.каталог, пауза=пауза, правка_с=0.0)
        await бот.обработать(self.обн("длинный вопрос"))
        for _ in range(20):
            await asyncio.sleep(0)
        await бот.обработать(self.обн("/status"))
        тексты = self.tg.тексты()
        self.assertTrue(any(т.startswith("Сервис: ok") for т in тексты), тексты)
        self.assertFalse(any("отвечу после текущего" in т for т in тексты), "мгновенная команда — без очереди")
        self.assertTrue(any("бот отвечает" in т for т in тексты if т.startswith("Сервис")), "статус говорит, что бот занят")
        отпустить.set()
        await бот.дождаться()
        self.assertTrue(self.tg.тексты("editMessageText")[-1].startswith("долгий ответ"))
        await бот.закрыть()

    async def test_new_с_вопросом_отвечает_на_вопрос(self):
        # «/new Сколько стоит …» — история очищена, вопрос не потерян.
        self.сервис.чат += [_sse("раз"), _sse("ответ на вопрос")]
        бот = self.бот()
        await self.прогон(бот, "первый", "/new Сколько стоит VPN?")
        последний = self.сервис.запросы[-1][1]["messages"]
        self.assertEqual([с["content"] for с in последний], ["Сколько стоит VPN?"], "история пуста, вопрос ушёл")
        self.assertIn("заново", " ".join(self.tg.тексты("sendMessage")))
        self.assertTrue(self.tg.тексты("editMessageText")[-1].startswith("ответ на вопрос"))
        await бот.закрыть()

    def test_склонение_реплик(self):
        self.assertEqual([svc_tg.реплик(n) for n in (0, 1, 2, 4, 5, 11, 12, 21, 22, 25)],
                         ["0 реплик", "1 реплика", "2 реплики", "4 реплики", "5 реплик", "11 реплик", "12 реплик",
                          "21 реплика", "22 реплики", "25 реплик"])

    def test_markdown_в_html(self):
        html_ = svc_tg.в_html("**Итог:** цена <б> & `код`\n### Раздел\n*   пункт\n- ещё")
        self.assertEqual(html_, "<b>Итог:</b> цена &lt;б&gt; &amp; <code>код</code>\n<b>Раздел</b>\n• пункт\n• ещё")
        self.assertEqual(svc_tg.в_html("2 * 3 = 6, а ** не пара"), "2 * 3 = 6, а ** не пара")

    async def test_итог_с_разметкой_и_откат_на_простой_текст(self):
        self.сервис.чат += [_sse("**Жирно** и `код`"), _sse("**снова**")]
        бот = self.бот()
        await self.прогон(бот, "вопрос")
        итог = [т for м, т in self.tg.вызовы if м == "editMessageText"][-1]
        self.assertEqual(итог.get("parse_mode"), "HTML")
        self.assertTrue(итог["text"].startswith("<b>Жирно</b> и <code>код</code>"))
        self.tg.не_разбирать = True
        await self.прогон(бот, "ещё")
        итог = [т for м, т in self.tg.вызовы if м == "editMessageText"][-1]
        self.assertNotIn("parse_mode", итог, "Telegram не разобрал разметку — простой текст")
        self.assertTrue(итог["text"].startswith("**снова**"))
        await бот.закрыть()

    def test_токен_скрыт_в_тексте_ошибок(self):
        self.assertEqual(svc_tg.без_токена("ошибка https://api.telegram.org/bot123:AAbb-c_d/getMe"),
                         "ошибка https://api.telegram.org/bot…/getMe")



class СлужбаБота(_ОсноваШлюза):
    """Служба day31-tg, ключ бота, состояние для /admin, консоль и ручка /admin/api/telegram."""

    def test_unit_файл_бота(self):
        т = svc_unit.текст_бота("/py", "/корень", "/память")
        for кусок in ("ExecStart=/py /корень/telegram_bot.py", "Environment=MEMORY_DIR=/память",
                      "After=network-online.target day31-llm.service", "Restart=on-failure",
                      "RestartPreventExitStatus=2 3"):
            self.assertIn(кусок, т)
        self.assertNotIn("TELEGRAM", т, "секреты — в .env, не в unit-файле")
        self.assertTrue(svc_unit.путь_unit(svc_unit.ИМЯ_БОТА).endswith("systemd/user/day31-tg.service"))
        self.assertIn("service.py", svc_unit.текст("/py", "/корень", "/память"), "служба сервиса — как была")

    def test_ключ_бота_файл_0600_и_старый_отозван(self):
        from agent.service import manage
        with mock.patch.dict(os.environ, {"MEMORY_DIR": self.каталог}):
            первый = manage.бот_ключ()
            путь = os.path.join(self.каталог, "service", "telegram.key")
            self.assertEqual(oct(os.stat(путь).st_mode & 0o777), "0o600")
            ключ1 = pathlib.Path(путь).read_text().strip()
            второй = manage.бот_ключ()
            ключ2 = pathlib.Path(путь).read_text().strip()
            хранилище = manage.хранилище_ключей()
            self.assertIsNone(хранилище.проверить(ключ1), "старый ключ бота отозван")
            self.assertEqual(хранилище.проверить(ключ2)["права"], ["чат", "документы", "проект"])   # День 31: + «проект»
        self.assertEqual((первый["имя"], второй["файл"]), ("телеграм", путь))
        self.assertNotIn("sk-day30-", _json.dumps(второй), "сам ключ наружу не отдаётся")

    def test_состояние_бота_без_секретов(self):
        from agent.service import manage
        with mock.patch.dict(os.environ, {"MEMORY_DIR": self.каталог}):
            пусто = manage.состояние_бота()
            self.assertIsNone(пусто["состояние"])
            каталог = os.path.join(self.каталог, "service")
            pathlib.Path(каталог, "telegram-state.json").write_text(_json.dumps(
                {"бот": "@TestBot", "владелец_задан": True, "последний_опрос": "2026-10-10T16:00:00"}), encoding="utf-8")
            журнал = svc_journal.Журнал(os.path.join(каталог, "telegram.jsonl"), поля=svc_tg.ПОЛЯ_ЖУРНАЛА)
            журнал.записать(кто="владелец", вид="чат", код=200, текст="ТЕКСТ-МАРКЕР")
            журнал.записать(кто="чужой", чей="abcd1234", вид="отказ")
            с = manage.состояние_бота()
        self.assertEqual(с["состояние"]["бот"], "@TestBot")
        self.assertEqual([з["кто"] for з in с["журнал"]], ["чужой", "владелец"], "новые первыми")
        self.assertEqual(с["чужих"], 1)
        self.assertNotIn("МАРКЕР", _json.dumps(с, ensure_ascii=False))
        self.assertIn("состояние", с["служба"])

    def test_ручка_telegram_только_для_админа(self):
        вызовы = []
        self.сервис.телеграм = lambda действие: вызовы.append(действие) or {"состояние": "active"}
        тело = {"действие": "старт"}
        self.assertEqual(self.клиент.post("/admin/api/telegram", json=тело, headers=self.h(self.к_чат)).status_code, 403)
        r = self.клиент.post("/admin/api/telegram", json=тело, headers=self.h(self.к_админ))
        self.assertEqual((r.status_code, r.json()["состояние"], вызовы), (200, "active", ["старт"]))
        r = self.клиент.post("/admin/api/telegram", json={"действие": "удалить"}, headers=self.h(self.к_админ))
        self.assertEqual(r.status_code, 400)
        страница = self.клиент.get("/admin").text
        for узел in ('id="телеграм-карта"', 'id="телеграм-старт"', 'id="телеграм-стоп"', "/admin/api/telegram"):
            self.assertIn(узел, страница)

    def test_консоль_бота_без_секретов(self):
        память = tempfile.mkdtemp(prefix="консоль-бота-")
        self.addCleanup(shutil.rmtree, память, True)
        окр = {**os.environ, "MEMORY_DIR": память, "PYTHONIOENCODING": "utf-8",
               "TELEGRAM_BOT_TOKEN": "123456:ПРОБНЫЙ-токен".replace("ПРОБНЫЙ", "PROBE"), "TELEGRAM_OWNER_ID": "424242"}
        запуск = lambda *а: subprocess.run([sys.executable, "cli.py", *а], capture_output=True, text=True, env=окр,
                                           cwd=os.path.dirname(os.path.abspath(__file__)), timeout=120)
        r = запуск("--телеграм-ключ")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("telegram.key", r.stdout)
        self.assertNotIn("sk-day30-", r.stdout + r.stderr)
        r = запуск("--телеграм", "статус")
        вывод = r.stdout + r.stderr
        self.assertIn("Служба day31-tg", вывод)
        self.assertIn("владелец: задан", вывод)
        for секрет in ("PROBE", "424242"):
            self.assertNotIn(секрет, вывод)



class ПоказTelegram(unittest.TestCase):
    """Показ бота: режим «телеграм» в browser.mjs, demo_telegram.sh, раздел DEMO-CHROME.md."""

    @classmethod
    def setUpClass(cls):
        корень = os.path.dirname(os.path.abspath(__file__))
        читать = lambda *ч: pathlib.Path(корень, *ч).read_text(encoding="utf-8")
        водитель = читать("browser.mjs")
        # День 31: тот же блок водит и показ «телеграм31» (сцены /help).
        cls.блок = водитель.split("if (РЕЖИМ === 'телеграм'")[1].split("if (РЕЖИМ === 'показ")[0]
        cls.скрипт = читать("demo_telegram.sh")
        cls.сценарий = читать("DEMO-CHROME.md").split("## Дополнение: показ Telegram-бота")[1]
        cls.админ = читать("templates", "service_admin.html")
        cls.путь_скрипта = os.path.join(корень, "demo_telegram.sh")

    def test_шесть_сцен_и_только_чат_бота(self):
        for номер in range(1, 7):
            self.assertIn(f"'Сцена {номер}'", self.блок)
            self.assertIn(f"| {номер} |", self.сценарий)
        self.assertNotIn("'Сцена 7'", self.блок)
        self.assertIn("открыт не чат с ботом", self.блок, "перед отправкой — сверка заголовка чата")
        self.assertIn("await отправить('/status')", self.блок, "сцена 4 показывает мгновенный /status (версия 7)")
        # Список чатов владельца в кадр не попадает: колонка скрыта до записи и перед каждой отправкой.
        self.assertIn("#column-left { display: none !important; }", self.блок)
        self.assertIn("process.env.READY", self.блок)
        for селектор in (".chat-info .peer-title", ".input-message-input[contenteditable]", ".bubble.is-in"):
            self.assertIn(селектор, self.блок)
        self.assertIn("process.exit(0)", self.блок)
        self.assertIn("process.env.MARKS", self.блок)
        for узел in ("телеграм-служба", "телеграм-сеть", "телеграм-журнал", "телеграм-карта"):
            self.assertIn(узел, self.блок)
            self.assertIn(f'id="{узел}"', self.админ)

    def test_скрипт_показа_бота(self):
        self.assertTrue(os.access(self.путь_скрипта, os.X_OK))
        self.assertEqual(_subprocess.run(["bash", "-n", self.путь_скрипта], capture_output=True).returncode, 0)
        справка = _subprocess.run(["bash", self.путь_скрипта, "--help"], capture_output=True, text=True, timeout=60)
        self.assertIn("Запуск:", справка.stdout)
        # День 31: запись — кадрами самого Chrome (RECORD_CDP), а не областью экрана.
        for кусок in ('"credentials_enable_service"', "--window-size=880,980", 'RECORD_CDP="$FILE"',
                      "--сервис-отозвать", "user-data-dir=\"$PROFILE\""):
            self.assertIn(кусок, self.скрипт)
        self.assertNotIn('rm -rf "$PROFILE"', self.скрипт, "профиль с сеансом удаляет владелец, не скрипт")
        self.assertIn("CDP_TIMEOUT=60000", self.скрипт, "подвисшая страница не вешает показ")
        self.assertIn('["exit_type"] = "Normal"', self.скрипт, "нет окна «Восстановить страницы?» поверх чата")
        self.assertLess(self.блок.index("await списокСкрыт()"), self.блок.index("await начатьЗапись()"),
                        "запись — только после того, как чат открыт и список чатов скрыт")
        водитель = pathlib.Path(os.path.dirname(os.path.abspath(__file__)), "browser.mjs").read_text(encoding="utf-8")
        self.assertIn("process.env.CDP_TIMEOUT", водитель)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
