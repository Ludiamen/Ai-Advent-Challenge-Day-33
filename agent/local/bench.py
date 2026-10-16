"""Запросы разной сложности к локальным моделям и к облаку — замер и выбор (День 26).

Лекция недели советует не верить чужим впечатлениям о моделях, а сравнить
несколько на одинаковых запросах. Здесь так и сделано: задачи трёх уровней
(local-tasks.json) уходят каждой модели одним и тем же промптом, ответ берётся
потоком (stream.py), а верность проверяет код:

  простой    факт или перевод — нужные слова в ответе;
  средний    рассуждение с числом — последнее число ответа равно ожиданию;
  сложный    извлечение по схеме — JSON разбирается, поля сверяются;
             ответ по документам — формат Дня 24: JSON с цитатами, цитата
             найдена в тексте фрагмента, факт в ответе, источник тот.

У каждой задачи свой предел длины ответа: на CPU токен стоит секунды, и
ответ, оборванный пределом без нужного числа, неверен — модель не выполнила
просьбу ответить коротко. После своих задач модель выгружается из памяти.

Перед замером каждая локальная модель прогревается одним коротким запросом:
загрузка весов в память (десятки секунд на CPU) — цена первого запроса, а не
скорость модели, и в таблице она стоит отдельным столбцом.

Основная локальная модель выбирается правилом: больше верных ответов → выше
скорость генерации → меньше время до первого токена. Выбор пишется в
local-settings.json, его читают консоль и страница (основная()).

Человек может отметить любой ответ «👍» или «👎» — лекция предлагает так
копить статистику качества. Отметки лежат в memory/local/marks.jsonl и
подмешиваются к прогону при чтении.

Публичное API:
  путь_задач(), загрузить_задачи(путь)
  проверить(задача, текст, контекст) -> (ок, пояснение)
  прогнать(модели, задачи, прогресс, отвечатель) -> Прогон
  выбрать(прогон), основная(), записать_выбор(прогон, путь)
  записать(прогон, каталог_памяти), прочитать(каталог_памяти), в_markdown(данные)
  отметить(каталог_памяти, задача, модель, оценка, заметка), отметки(каталог_памяти)
"""

from __future__ import annotations

import json
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from agent import catalog
from agent.llm import LLMError
from agent.local import ollama, stream
from agent.rag.evaluate import записать_атомарно

УРОВНИ = ("простой", "средний", "сложный")
ФАЙЛ_ПРОГОНА = "bench.json"
ФАЙЛ_ОТЧЁТА = "LOCAL-BENCH.md"
ФАЙЛ_ОТМЕТОК = "marks.jsonl"
ОЦЕНКИ = ("👍", "👎")
ПРОГРЕВ = "Ответь одним словом: да."
ПРЕДЕЛ_ОТВЕТА = 700


def _корень() -> str:
    from agent.rag.indexer import КОРЕНЬ
    return КОРЕНЬ


def путь_задач() -> str:
    return os.environ.get("LOCAL_TASKS") or os.path.join(_корень(), "local-tasks.json")


def путь_настроек() -> str:
    return os.environ.get("LOCAL_SETTINGS") or os.path.join(_корень(), "local-settings.json")


def загрузить_задачи(путь: str = "") -> list[dict[str, Any]]:
    """Задачи; общая системная подсказка файла («система») — в каждую задачу."""
    with open(путь or путь_задач(), encoding="utf-8") as f:
        данные = json.load(f)
    задачи = данные.get("задачи") if isinstance(данные, dict) else данные
    система = данные.get("система", "") if isinstance(данные, dict) else ""
    if система:
        for з in задачи or []:
            з.setdefault("система", система)
    if not задачи:
        raise ValueError("В файле задач нет ни одной задачи")
    for з in задачи:
        if з.get("уровень") not in УРОВНИ or not з.get("вопрос") or not з.get("проверка"):
            raise ValueError(f"Задача {з.get('id', '?')}: нужны уровень ({', '.join(УРОВНИ)}), вопрос и проверка")
    return задачи


def системная_подсказка(путь: str = "") -> str:
    """Общая системная подсказка задач замера («система» в файле задач)."""
    try:
        with open(путь or путь_задач(), encoding="utf-8") as f:
            данные = json.load(f)
    except (OSError, ValueError):
        return ""
    return данные.get("система", "") if isinstance(данные, dict) else ""


# --- проверка кодом ------------------------------------------------------------

def _числа(текст: str) -> list[float]:
    т = re.sub(r"(?<=\d)[\s ](?=\d{3}\b)", "", текст or "")
    итог = []
    for м in re.findall(r"-?\d+(?:[.,]\d+)?", т):
        try:
            итог.append(float(м.replace(",", ".")))
        except ValueError:
            pass
    return итог


def _json(текст: str) -> dict[str, Any] | None:
    т = re.sub(r"^```(?:json)?\s*|\s*```$", "", (текст or "").strip())
    найдено = re.search(r"\{.*\}", т, re.S)
    if not найдено:
        return None
    try:
        данные = json.loads(найдено.group(0))
    except json.JSONDecodeError:
        return None
    return данные if isinstance(данные, dict) else None


def проверить(задача: dict[str, Any], текст: str, контекст: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Верен ли ответ — кодом, без модели-судьи."""
    п = задача["проверка"]
    вид = п.get("вид")
    if вид == "содержит":
        нет = [ш for ш in п["все"] if not re.search(ш, текст or "", re.I)]
        return (not нет, "всё на месте" if not нет else f"нет: {', '.join(нет)}")
    if вид == "число":
        числа = _числа(текст)
        if not числа:
            return False, "в ответе нет числа"
        ок = abs(числа[-1] - float(п["ожидание"])) <= float(п.get("допуск", 1e-9))
        return ок, f"ответ {числа[-1]:g}, ожидалось {п['ожидание']:g}"
    if вид == "json":
        данные = _json(текст)
        if данные is None:
            return False, "ответ не разбирается как объект JSON"
        плохие = []
        for поле, ожидание in п["поля"].items():
            значение = данные.get(поле)
            if "число" in ожидание:
                числа = _числа(str(значение)) if значение is not None else []
                if not числа or abs(числа[0] - float(ожидание["число"])) > 1e-6:
                    плохие.append(f"{поле}={значение!r}")
            elif not re.search(ожидание["шаблон"], str(значение or ""), re.I):
                плохие.append(f"{поле}={значение!r}")
        return (not плохие, f"полей верно {len(п['поля']) - len(плохие)} из {len(п['поля'])}"
                + (f"; неверно: {', '.join(плохие)}" if плохие else ""))
    if вид == "rag":
        return _проверить_rag(п, текст, контекст or {})
    raise ValueError(f"Неизвестный вид проверки «{вид}»")


def _проверить_rag(п: dict[str, Any], текст: str, контекст: dict[str, Any]) -> tuple[bool, str]:
    from agent.rag import cite
    разбор = cite.разобрать(текст)
    if разбор.ошибка_формата:
        return False, f"формат: {разбор.ошибка_формата}"
    if разбор.не_знаю:
        return False, "модель ответила «не знаю»"
    тексты = контекст.get("тексты") or []
    cite.проверить_цитаты(разбор, тексты)
    подтверждено = [ц for ц in разбор.цитаты if ц.подтверждена]
    факты = [ш for ш in п.get("факты", []) if not re.search(ш, разбор.ответ, re.I)]
    источники = контекст.get("источники") or []
    номера = {ц.найдена_в or ц.n for ц in подтверждено}
    нужный = any(re.search(п.get("источник", ""), источники[n - 1], re.I)
                 for n in номера if 1 <= n <= len(источники))
    ок = bool(подтверждено) and not факты and нужный
    return ок, (f"цитат подтверждено {len(подтверждено)} из {len(разбор.цитаты)}"
                + (f"; нет факта: {', '.join(факты)}" if факты else "")
                + ("" if нужный else "; цитата не из нужного документа"))


# --- прогон --------------------------------------------------------------------

def сообщения_задачи(задача: dict[str, Any], отвечатель=None) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Промпт задачи. У RAG-задачи — промпт Дня 24 с найденными фрагментами:
    поиск один раз, фрагменты одни и те же для всех моделей."""
    if задача["проверка"].get("вид") != "rag":
        система = [{"role": "system", "content": задача["система"]}] if задача.get("система") else []
        return система + [{"role": "user", "content": задача["вопрос"]}], {}
    if отвечатель is None:
        raise ValueError("Для задачи по документам нужен индекс (Отвечатель)")
    from agent.rag.answer import С_RAG, ЦИТАТЫ, собрать_сообщения
    отбор = отвечатель.поисковик.найти(задача["вопрос"], "фильтр")
    # Сколько лучших фрагментов дать модели: агент Дня 24 даёт до восьми, но на
    # CPU каждый фрагмент — минуты чтения промпта.
    фрагменты = отбор.фрагменты[:int(задача["проверка"].get("фрагментов") or len(отбор.фрагменты))]
    сообщения, _ = собрать_сообщения(задача["вопрос"], С_RAG, фрагменты, ЦИТАТЫ)
    return сообщения, {"тексты": [н.чанк.текст for н in фрагменты],
                       "источники": [н.чанк.источник for н in фрагменты],
                       "фрагментов": len(фрагменты)}


@dataclass
class Прогон:
    модели: list[str]
    задачи: list[dict[str, Any]]
    ответы: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)   # модель → задача → ответ
    прогрев: dict[str, dict[str, Any]] = field(default_factory=dict)              # модель → загрузка
    машина: dict[str, Any] = field(default_factory=dict)                           # до и после прогона
    когда: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    секунд: float = 0.0

    def итог(self, ключ: str) -> dict[str, Any]:
        ответы = [о for о in self.ответы.get(ключ, {}).values()]
        скорости = [о["tok_s"] for о in ответы if о.get("tok_s")]
        до_первого = [о["ttft_s"] for о in ответы if о.get("ok") is not None and not о.get("error")]
        по_уровням = {у: (sum(1 for з in self.задачи if з["уровень"] == у and self.ответы.get(ключ, {}).get(з["id"], {}).get("ok")),
                          sum(1 for з in self.задачи if з["уровень"] == у)) for у in УРОВНИ}
        модель = catalog.get(ключ)
        return {"model": ключ, "label": модель.label, "local": модель.local,
                "верно": sum(1 for о in ответы if о.get("ok")), "задач": len(self.задачи),
                "по_уровням": по_уровням,
                "tok_s": round(statistics.median(скорости), 1) if скорости else 0.0,
                "ttft_s": round(statistics.median(до_первого), 2) if до_первого else 0.0,
                "секунд": round(sum(о.get("total_s", 0) for о in ответы), 1),
                "стоимость": round(sum(о.get("cost", 0) for о in ответы), 5),
                "загрузка_s": (self.прогрев.get(ключ) or {}).get("load_s", 0.0),
                "ошибок": sum(1 for о in ответы if о.get("error"))}

    def to_dict(self) -> dict[str, Any]:
        return {"when": self.когда, "seconds": round(self.секунд, 1), "models": self.модели,
                "machine": self.машина,
                "tasks": self.задачи, "answers": self.ответы, "warmup": self.прогрев,
                "totals": {к: self.итог(к) for к in self.модели}, "chosen": выбрать(self)}


def прогнать(модели: list[str], задачи: list[dict[str, Any]],
             прогресс: Callable[[str, int, int, str], None] | None = None,
             отвечатель=None) -> Прогон:
    прогон = Прогон(list(модели), list(задачи))
    прогон.машина = {"до": машина()}
    старт = time.monotonic()
    подготовлено = {з["id"]: сообщения_задачи(з, отвечатель) for з in задачи}
    всего = len(модели) * len(задачи)
    сделано = 0
    for ключ in модели:
        модель = catalog.get(ключ)
        if модель.local:
            if прогресс:
                прогресс("замер моделей", сделано, всего, f"{ключ}: загрузка в память")
            try:
                прогрев = stream.спросить(ключ, [{"role": "user", "content": ПРОГРЕВ}], предел=10)
                прогон.прогрев[ключ] = {"load_s": прогрев["server"].get("load_s", 0.0),
                                        "total_s": прогрев["total_s"]}
            except LLMError as exc:
                прогон.прогрев[ключ] = {"error": str(exc)[:300]}
        for задача in задачи:
            if прогресс:
                прогресс("замер моделей", сделано, всего, f"{ключ}: {задача['id']} {задача['название']}")
            сообщения, контекст = подготовлено[задача["id"]]
            try:
                ответ = stream.спросить(ключ, сообщения, предел=int(задача.get("предел") or ПРЕДЕЛ_ОТВЕТА),
                                        температура=0.0)
                ок, пояснение = проверить(задача, ответ["text"], контекст)
                ответ.update({"ok": ок, "note": пояснение})
            except LLMError as exc:
                ответ = {"ok": False, "note": "сбой", "error": str(exc)[:300], "text": "",
                         "ttft_s": 0.0, "total_s": 0.0, "tok_s": 0.0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0}
            ответ.pop("тип", None)
            ответ["level"] = задача["уровень"]
            прогон.ответы.setdefault(ключ, {})[задача["id"]] = ответ
            сделано += 1
        if модель.local:
            # Освободить память под следующую модель: две модели 4B разом на
            # этой машине — это подкачка и замеры чужой медлительности.
            ollama.выгрузить(модель.api_id)
    if прогресс:
        прогресс("замер моделей", всего, всего, "готово")
    прогон.секунд = time.monotonic() - старт
    прогон.машина["после"] = машина()
    return прогон


# --- машина, на которой шёл замер ------------------------------------------------

def машина() -> dict[str, Any]:
    """Процессор, ядра, память, нагрузка и температура — без них числа замера
    нельзя сравнить ни с другой машиной, ни с другим днём на этой."""
    итог: dict[str, Any] = {"threads": os.cpu_count() or 0}
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            текст = f.read()
        модель = re.search(r"model name\s*:\s*(.+)", текст)
        ядра = {м for м in re.findall(r"core id\s*:\s*(\d+)", текст)}
        итог.update({"cpu": модель.group(1).strip() if модель else "", "cores": len(ядра) or итог["threads"]})
    except OSError:
        итог.update({"cpu": "", "cores": итог["threads"]})
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            память = {к: int(з.split()[0]) for к, _, з in (с.partition(":") for с in f) if з.strip()}
        итог.update({"ram_gb": round(память.get("MemTotal", 0) / 1e6, 1),
                     "ram_free_gb": round(память.get("MemAvailable", 0) / 1e6, 1),
                     "swap_used_gb": round((память.get("SwapTotal", 0) - память.get("SwapFree", 0)) / 1e6, 1)})
    except (OSError, ValueError):
        pass
    try:
        итог["load"] = [round(н, 1) for н in os.getloadavg()]
    except OSError:
        итог["load"] = []
    итог["temp_c"] = _температура()
    return итог


def _температура() -> float:
    """Температура пакета CPU (coretemp), если система её отдаёт."""
    import glob
    for путь in glob.glob("/sys/class/hwmon/hwmon*/temp1_input"):
        try:
            with open(os.path.join(os.path.dirname(путь), "name"), encoding="utf-8") as f:
                if f.read().strip() not in ("coretemp", "k10temp", "zenpower"):
                    continue
            with open(путь, encoding="utf-8") as f:
                return round(int(f.read().strip()) / 1000, 1)
        except (OSError, ValueError):
            continue
    return 0.0


def машина_строкой(м: dict[str, Any]) -> str:
    if not м:
        return "—"
    return (f"{м.get('cpu') or 'CPU'} — {м.get('cores')} ядра / {м.get('threads')} потока, ОЗУ "
            f"{м.get('ram_gb')} ГБ (свободно {м.get('ram_free_gb')}), подкачка занята {м.get('swap_used_gb')} ГБ, "
            f"нагрузка {' / '.join(str(н) for н in м.get('load') or [])}, температура {м.get('temp_c') or '—'} °C")


# --- выбор основной локальной модели ---------------------------------------------

def выбрать(прогон: Прогон) -> dict[str, Any]:
    """Больше верных → быстрее генерация → меньше ожидание первого токена."""
    локальные = [прогон.итог(к) for к in прогон.модели if catalog.get(к).local
                 and not (прогон.прогрев.get(к) or {}).get("error")]
    if not локальные:
        return {}
    лучший = max(локальные, key=lambda и: (и["верно"], и["tok_s"], -и["ttft_s"]))
    return {"model": лучший["model"], "label": лучший["label"],
            "rule": "больше верных ответов → выше скорость генерации → меньше время до первого токена",
            "верно": лучший["верно"], "задач": лучший["задач"], "tok_s": лучший["tok_s"],
            "ttft_s": лучший["ttft_s"]}


def записать_выбор(прогон: Прогон, путь: str = "") -> dict[str, Any]:
    выбор = выбрать(прогон)
    if not выбор:
        raise ValueError("Ни одна локальная модель не ответила — выбирать не из чего")
    данные = {"основная": выбор["model"], "когда": прогон.когда, "выбор": выбор,
              "итоги": {к: прогон.итог(к) for к in прогон.модели}}
    записать_атомарно(путь or путь_настроек(), json.dumps(данные, ensure_ascii=False, indent=1))
    return данные


def основная() -> str:
    """Основная локальная модель: из local-settings.json, иначе первая скачанная."""
    путь = путь_настроек()
    if os.path.exists(путь):
        try:
            with open(путь, encoding="utf-8") as f:
                ключ = json.load(f).get("основная", "")
            if ключ in catalog.MODELS and catalog.get(ключ).local:
                return ключ
        except (OSError, ValueError):
            pass
    скачанные = [к for к in catalog.local_models() if catalog.installed(к)]
    return скачанные[0] if скачанные else catalog.local_models()[0]


# --- отметки человека ------------------------------------------------------------

def _каталог(каталог_памяти: str) -> str:
    путь = os.path.join(os.path.abspath(каталог_памяти), "local")
    os.makedirs(путь, exist_ok=True)
    return путь


def отметить(каталог_памяти: str, задача: str, модель: str, оценка: str, заметка: str = "") -> dict[str, Any]:
    if not задача:
        raise ValueError("Нет задачи")
    if оценка not in ОЦЕНКИ:
        raise ValueError(f"Оценка — {' или '.join(ОЦЕНКИ)}")
    if модель not in catalog.MODELS:
        raise ValueError(f"Нет модели «{модель}»")
    запись = {"when": datetime.now().isoformat(timespec="seconds"), "task": задача, "model": модель,
              "mark": оценка, "note": заметка[:300]}
    with open(os.path.join(_каталог(каталог_памяти), ФАЙЛ_ОТМЕТОК), "a", encoding="utf-8") as f:
        f.write(json.dumps(запись, ensure_ascii=False) + "\n")
    return запись


def отметки(каталог_памяти: str) -> dict[str, dict[str, Any]]:
    """Последняя отметка на каждую пару «задача|модель»."""
    путь = os.path.join(os.path.abspath(каталог_памяти), "local", ФАЙЛ_ОТМЕТОК)
    итог: dict[str, dict[str, Any]] = {}
    if os.path.exists(путь):
        with open(путь, encoding="utf-8") as f:
            for строка in f:
                try:
                    з = json.loads(строка)
                except ValueError:
                    continue
                итог[f"{з['task']}|{з['model']}"] = з
    return итог


# --- запись и отчёт ----------------------------------------------------------------

def записать(прогон: Прогон, каталог_памяти: str) -> str:
    данные = прогон.to_dict()
    каталог = _каталог(каталог_памяти)
    записать_атомарно(os.path.join(каталог, ФАЙЛ_ПРОГОНА), json.dumps(данные, ensure_ascii=False, indent=1))
    свой = os.path.abspath(каталог_памяти) != os.path.abspath(os.path.join(_корень(), "memory"))
    путь = os.path.join(каталог if свой else _корень(), ФАЙЛ_ОТЧЁТА)
    записать_атомарно(путь, в_markdown(данные))
    return путь


def прочитать(каталог_памяти: str) -> dict[str, Any] | None:
    путь = os.path.join(os.path.abspath(каталог_памяти), "local", ФАЙЛ_ПРОГОНА)
    if not os.path.exists(путь):
        return None
    with open(путь, encoding="utf-8") as f:
        данные = json.load(f)
    данные["marks"] = отметки(каталог_памяти)
    return данные


def в_markdown(данные: dict[str, Any]) -> str:
    модели, задачи = данные["models"], данные["tasks"]
    итоги = данные["totals"]
    строки = ["# Локальные модели и облако: запросы разной сложности", "",
              f"Прогон {данные['when']}, {данные['seconds']} с. Одни и те же запросы — каждой модели, "
              "ответ потоком, время до первого токена и скорость считает клиент, верность — код. "
              "Локальные модели — Ollama на этой машине без видеокарты, облачная — для сравнения.", "",
              f"Машина до прогона: {машина_строкой((данные.get('machine') or {}).get('до') or {})}.",
              f"После: {машина_строкой((данные.get('machine') or {}).get('после') or {})}.", "",
              "## Итог", "",
              "| модель | где | верно | простой | средний | сложный | ток/с | до 1-го токена, с | загрузка, с | время, с | стоимость, $ |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for к in модели:
        и = итоги[к]
        у = и["по_уровням"]
        строки.append(f"| {и['label']} | {'локально' if и['local'] else 'облако'} | {и['верно']} из {и['задач']} | "
                      + " | ".join(f"{у[ур][0]} из {у[ур][1]}" for ур in УРОВНИ)
                      + f" | {и['tok_s']} | {и['ttft_s']} | {и['загрузка_s'] or '—'} | {и['секунд']} | {и['стоимость']} |")
    выбор = данные.get("chosen") or {}
    if выбор:
        строки += ["", f"**Основная локальная модель: {выбор['label']}** — {выбор['верно']} из {выбор['задач']} "
                   f"верно, {выбор['tok_s']} ток/с. Правило: {выбор['rule']}."]
    система = next((з.get("система") for з in задачи if з.get("система")), "")
    строки += ["", "## Задачи", ""]
    if система:
        строки += [f"Системная подсказка всем задачам, кроме задачи по документам: «{система}»", ""]
    строки += ["| # | уровень | задача | предел ответа, ток. | проверка |", "|---|---|---|---:|---|"]
    for з in задачи:
        строки.append(f"| {з['id']} | {з['уровень']} | {з['название']} | {з.get('предел', ПРЕДЕЛ_ОТВЕТА)} | {з['проверка']['вид']} |")
    for з in задачи:
        строки += ["", f"### {з['id']}. {з['название']} ({з['уровень']})", "",
                   "> " + з["вопрос"].replace("\n", "\n> ")[:900], "",
                   "| модель | верно | ответ | ток/с | до 1-го, с | проверка |", "|---|---|---|---:|---:|---|"]
        for к in модели:
            о = данные["answers"].get(к, {}).get(з["id"], {})
            текст = re.sub(r"\s+", " ", о.get("text") or о.get("error") or "")[:240].replace("|", "/")
            строки.append(f"| {итоги[к]['label']} | {'✓' if о.get('ok') else '✗'} | {текст} | "
                          f"{о.get('tok_s', 0)} | {о.get('ttft_s', 0)} | {о.get('note', '')} |")
    return "\n".join(строки) + "\n"
