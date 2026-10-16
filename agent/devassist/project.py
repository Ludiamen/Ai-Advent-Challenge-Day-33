"""Проект для ассистента разработчика: где лежит, что индексировать, где индекс (plan D-01, D-02).

Проекты описаны в projects.json. В индекс попадают только файлы под git (`git ls-files`),
прошедшие «включить / исключить»: неотслеживаемое и игнорируемое не индексируется вовсе.
Проект — чужой репозиторий (CRM), и над ним может работать другая сессия, поэтому git
вызывается только на чтение и без служебных блокировок (GIT_OPTIONAL_LOCKS=0, spec NFR-01).

Индексатор Дней 21–30 не меняется. Файлы проекта после маски ПД точного профиля
(D-02a) копируются в `<индекс>/src/` с теми же относительными путями, и обычный
`Индексатор(документы=src, индекс=<индекс>)` строит индекс — маска гарантированно
применена до разбиения, а `source` фрагмента — путь в проекте. Векторы кэшируются по
тексту фрагмента, поэтому повторная сборка считает только изменённое.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.service import pd

КОРЕНЬ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ФАЙЛ = "projects.json"
ПРОФИЛЬ_ПД = "точный"
СПИСОК = "manifest.json"


class ОшибкаПроекта(RuntimeError):
    """Проект не описан, путь не репозиторий git или git не ответил."""


def окружение_git() -> dict[str, str]:
    """git только читает: без необязательных блокировок индекса и без запросов пароля."""
    return dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C.UTF-8")


@dataclass
class Проект:
    имя: str
    путь: str
    описание: str = ""
    включить: list[str] = field(default_factory=list)
    исключить: list[str] = field(default_factory=list)
    индекс: str = ""
    # Подсказки к вопросам о схемах (plan D-04): слова вопроса → файл (urls.py, models.py) и
    # синонимы приложений («уведомления» → notify). Только из projects.json, не из кода.
    схемы: dict[str, Any] = field(default_factory=dict)

    @property
    def src(self) -> str:
        return os.path.join(self.индекс, "src")

    def подходит(self, путь: str) -> bool:
        return (any(fnmatch.fnmatch(путь, ш) for ш in self.включить)
                and not any(fnmatch.fnmatch(путь, ш) for ш in self.исключить))

    def to_dict(self) -> dict[str, Any]:
        return {"имя": self.имя, "путь": self.путь, "описание": self.описание,
                "включить": self.включить, "исключить": self.исключить, "индекс": self.индекс,
                "схемы": self.схемы}


def путь_конфига(путь: str = "") -> str:
    return os.path.abspath(путь or os.getenv("PROJECTS_FILE") or os.path.join(КОРЕНЬ, ФАЙЛ))


def загрузить(имя: str = "", путь: str = "") -> Проект:
    файл = путь_конфига(путь)
    try:
        with open(файл, encoding="utf-8") as f:
            данные = json.load(f)
    except OSError as e:
        raise ОшибкаПроекта(f"нет файла проектов {файл}: {e.strerror}") from e
    except json.JSONDecodeError as e:
        raise ОшибкаПроекта(f"{файл}: не JSON — {e}") from e
    проекты = данные.get("проекты") or {}
    имя = имя or данные.get("по_умолчанию") or ""
    if имя not in проекты:
        raise ОшибкаПроекта(f"проект «{имя}» не описан в {файл}; есть: {', '.join(проекты) or 'ничего'}")
    о = проекты[имя]
    каталог = os.path.expanduser(os.getenv(f"PROJECT_{имя.upper()}_DIR") or о.get("путь") or "")
    if каталог and not os.path.isabs(каталог):
        # «.» и прочие относительные пути — от каталога projects.json, а не от текущего каталога
        # процесса (День 32: проект «advent» — сам репозиторий, на раннере — каталог checkout).
        каталог = os.path.join(os.path.dirname(файл), каталог)
    каталог = os.path.abspath(каталог)
    индекс = о.get("индекс") or os.path.join("index-projects", имя)
    if not os.path.isabs(индекс):
        индекс = os.path.join(os.path.dirname(файл), индекс)
    return Проект(имя, каталог, о.get("описание", ""), list(о.get("включить") or []),
                  list(о.get("исключить") or []), os.path.abspath(индекс), dict(о.get("схемы") or {}))


def git(проект: Проект | str, *аргументы: str, таймаут: float = 20.0) -> str:
    """Команда git в каталоге проекта (только чтение); ошибка — ОшибкаПроекта с текстом git."""
    каталог = проект.путь if isinstance(проект, Проект) else проект
    if not os.path.isdir(каталог):
        raise ОшибкаПроекта(f"каталога проекта нет: {каталог}")
    try:
        итог = subprocess.run(["git", "-C", каталог, *аргументы], capture_output=True, text=True,
                              env=окружение_git(), timeout=таймаут, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        raise ОшибкаПроекта(f"git {аргументы[0]}: нет ответа {таймаут:.0f} с") from e
    except OSError as e:
        raise ОшибкаПроекта(f"git не запускается: {e}") from e
    if итог.returncode != 0:
        raise ОшибкаПроекта(f"git {аргументы[0]}: {(итог.stderr or итог.stdout).strip()[:300]}")
    return итог.stdout


def файлы(проект: Проект, ссылка: str = "") -> list[str]:
    """Пути под git, прошедшие «включить / исключить» (относительно корня проекта). Со ссылкой —
    файлы коммита (`ls-tree`), без неё — индекса git рабочего каталога (`ls-files`)."""
    if ссылка:
        вывод = git(проект, "ls-tree", "-r", "-z", "--name-only", "--full-tree", ссылка)
    else:
        вывод = git(проект, "ls-files", "-z")
    return sorted(п for п in вывод.split("\0") if п and проект.подходит(п))


def содержимое(проект: Проект, ссылка: str, пути: list[str]) -> dict[str, bytes]:
    """Байты файлов коммита одним `git cat-file --batch` (объекты git, не диск; без textconv).
    Подмодули и отсутствующие пути пропускаются."""
    if not пути:
        return {}
    каталог = проект.путь
    запрос = "".join(f"{ссылка}:{п}\n" for п in пути).encode("utf-8")
    try:
        итог = subprocess.run(["git", "-c", "core.fsmonitor=", "-C", каталог, "cat-file", "--batch"],
                              input=запрос, capture_output=True, env=окружение_git(), timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise ОшибкаПроекта(f"git cat-file: {e}") from e
    if итог.returncode != 0:
        raise ОшибкаПроекта(f"git cat-file: {итог.stderr.decode('utf-8', 'replace').strip()[:300]}")
    данные, i, ответ = итог.stdout, 0, {}
    for путь in пути:
        конец = данные.index(b"\n", i)
        заголовок = данные[i:конец].split()
        i = конец + 1
        if len(заголовок) == 2 and заголовок[1] == b"missing":
            continue
        размер = int(заголовок[2])
        if заголовок[1] == b"blob":
            ответ[путь] = данные[i:i + размер]
        i += размер + 1
    return ответ


def _хэш(данные: bytes) -> str:
    return hashlib.sha256(данные).hexdigest()


def подготовить(проект: Проект, ссылка: str = "") -> dict[str, Any]:
    """Скопировать файлы проекта под маской ПД в `<индекс>/src/`; вернуть отчёт без значений ПД.

    Изменённым считается файл, у которого поменялось содержимое в проекте; удалённые из
    проекта (или из «включить») файлы убираются из src. Со ссылкой (День 32, plan D-02) файлы
    берутся из коммита, а не с диска: ревью строит опору из базы PR, и правка в PR не может
    переписать правила, по которым PR проверяют.
    """
    список = файлы(проект, ссылка)
    байты = содержимое(проект, ссылка, список) if ссылка else None
    if байты is not None:
        список = [п for п in список if п in байты]
    os.makedirs(проект.src, exist_ok=True)
    путь_списка = os.path.join(проект.индекс, СПИСОК)
    try:
        with open(путь_списка, encoding="utf-8") as f:
            прежний = json.load(f).get("файлы", {})
    except (OSError, json.JSONDecodeError):
        прежний = {}
    новый: dict[str, str] = {}
    изменено, замены = [], {}
    for путь in список:
        if байты is not None:
            сырые = байты[путь]
        else:
            with open(os.path.join(проект.путь, путь), "rb") as f:
                сырые = f.read()
        хэш = _хэш(сырые)
        новый[путь] = хэш
        цель = os.path.join(проект.src, путь)
        if прежний.get(путь) == хэш and os.path.exists(цель):
            continue
        текст, виды = pd.скрыть(сырые.decode("utf-8", errors="replace"), профиль=ПРОФИЛЬ_ПД)
        for вид, n in виды.items():
            замены[вид] = замены.get(вид, 0) + n
        os.makedirs(os.path.dirname(цель), exist_ok=True)
        with open(цель, "w", encoding="utf-8") as f:
            f.write(текст)
        изменено.append(путь)
    удалено = sorted(set(прежний) - set(новый))
    for путь in удалено:
        цель = os.path.join(проект.src, путь)
        if os.path.exists(цель):
            os.remove(цель)
    for корень, _, имена in os.walk(проект.src):  # лишнее в src (например, после правки списков)
        for имя in имена:
            отн = os.path.relpath(os.path.join(корень, имя), проект.src).replace(os.sep, "/")
            if отн not in новый:
                os.remove(os.path.join(корень, имя))
                if отн not in удалено:
                    удалено.append(отн)
    коммит = git(проект, "rev-parse", "--verify", f"{ссылка or 'HEAD'}^{{commit}}").strip()
    with open(путь_списка, "w", encoding="utf-8") as f:
        json.dump({"проект": проект.имя, "коммит": коммит, "профиль_пд": ПРОФИЛЬ_ПД,
                   "файлы": новый, "время": time.strftime("%Y-%m-%dT%H:%M:%S")}, f,
                  ensure_ascii=False, indent=1)
    return {"файлов": len(список), "изменено": изменено, "удалено": удалено,
            "замены_пд": замены, "коммит": коммит}


def индексатор(проект: Проект, эмбеддер: str = ""):
    from agent.rag.indexer import Индексатор
    return Индексатор(документы=проект.src, индекс=проект.индекс, эмбеддер=эмбеддер, ocr=False)


def построить(проект: Проект, эмбеддер: str = "",
              прогресс: Callable[[str, int, int, str], None] | None = None,
              ссылка: str = "") -> dict[str, Any]:
    """Подготовить файлы и собрать индекс стратегии «структура» (векторы — из кэша, где есть).
    Со ссылкой — из файлов этого коммита (ревью PR: из базы)."""
    начало = time.monotonic()
    отчёт = подготовить(проект, ссылка)
    и = индексатор(проект, эмбеддер)
    сборка = и.построить(("структура",), прогресс=прогресс or (lambda *_: None))
    стр = сборка["стратегии"]["структура"]
    отчёт.update({"фрагментов": стр.get("чанков", 0), "из_кэша": стр.get("из_кэша", 0),
                  "секунд_векторов": стр.get("секунд_векторов", 0.0),
                  "секунд": round(time.monotonic() - начало, 1), "индекс": проект.индекс})
    return отчёт


def файлы_индекса(проект: Проект) -> list[str]:
    """Пути, попавшие в индекс при последней подготовке (manifest.json)."""
    try:
        with open(os.path.join(проект.индекс, СПИСОК), encoding="utf-8") as f:
            return sorted(json.load(f).get("файлы", {}))
    except (OSError, json.JSONDecodeError):
        return []


def файлы_схем(проект: Проект, вопрос: str, предел: int = 6) -> list[str]:
    """Файлы схем данных и API, о которых спрашивают: «маршруты уведомлений» → …/notify/urls.py.

    Косинус по всему индексу их часто не поднимает: вопрос — по-русски, файл — код. Слова
    и синонимы — из projects.json («схемы»); приложение не названо — все такие файлы.
    """
    вопрос = вопрос.lower()
    файлы_слова = (проект.схемы or {}).get("файлы") or {}
    приложения = (проект.схемы or {}).get("приложения") or {}
    нужные = [имя for имя, слова in файлы_слова.items() if any(с in вопрос for с in слова)]
    if not нужные:
        return []
    названные = [п for п, синонимы in приложения.items()
                 if п.lower() in вопрос or any(с in вопрос for с in синонимы)]
    итог = []
    for путь in файлы_индекса(проект):
        части = путь.split("/")
        if части[-1] in нужные and (not названные or any(п in части[:-1] for п in названные)):
            итог.append(путь)
    return итог[:предел]


def индекс_есть(проект: Проект) -> bool:
    return os.path.exists(os.path.join(проект.индекс, "docs.db"))
