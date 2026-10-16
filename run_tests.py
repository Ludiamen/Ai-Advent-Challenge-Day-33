#!/usr/bin/env python3
"""Безопасный единый запуск автоматических тестов Дня 30.

Запускать из корня проекта:

    .venv/bin/python -B run_tests.py

Runner переносит отслеживаемые Git файлы и явно разрешённые новые тесты во
временный каталог, поэтому корневой .env, рабочая память, документы и другие
локальные данные не читаются тестируемым кодом и его дочерними процессами.
Живые проверки из ``tests.py`` остаются пропущенными: этот runner не принимает
флаг ``--живые``.
"""

from __future__ import annotations

import os
import argparse
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile


МОДУЛИ = (
    "tests",
    "test_gateway_validation",
    "test_service_journal_security",
    "test_telegram_offset",
    "test_telegram_reliability",
    "test_telegram_security",
    "test_devassist",
    "test_review",
)

# Новый security-тест должен входить в снимок до первого commit, но остальные
# неотслеживаемые файлы по-прежнему запрещены.
ДОПОЛНИТЕЛЬНЫЕ_ФАЙЛЫ = (Path("test_service_journal_security.py"),)

_БЕЗОПАСНОЕ_ОКРУЖЕНИЕ = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TZ", "TERM", "COLORTERM",
}


def _окружение_git() -> dict[str, str]:
    окружение = {
        ключ: значение for ключ, значение in os.environ.items()
        if ключ.upper() in {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ"}
    }
    окружение.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return окружение


def _отслеживаемые(корень: Path) -> list[Path]:
    """Возвращает пути из индекса Git, не просматривая неотслеживаемые файлы."""
    результат = subprocess.run(
        ["git", "ls-files", "-z", "--cached"],
        cwd=корень,
        env=_окружение_git(),
        check=True,
        stdout=subprocess.PIPE,
    )
    пути = []
    for сырой in результат.stdout.split(b"\0"):
        if not сырой:
            continue
        относительный = Path(os.fsdecode(сырой))
        if относительный.is_absolute() or ".." in относительный.parts:
            raise RuntimeError(f"Небезопасный путь в индексе Git: {относительный}")
        if any(часть == ".env" or (часть.startswith(".env.") and часть != ".env.example")
               for часть in относительный.parts):
            continue
        пути.append(относительный)
    return пути


def _копировать(корень: Path, снимок: Path) -> None:
    пути = _отслеживаемые(корень)
    for относительный in ДОПОЛНИТЕЛЬНЫЕ_ФАЙЛЫ:
        if (корень / относительный).exists() and относительный not in пути:
            пути.append(относительный)
    # Проверяем весь набор до первого копирования. Ссылка способна замаскировать
    # неотслеживаемый .env под безопасным именем, поэтому в снимке их нет вовсе.
    for относительный in пути:
        источник = корень / относительный
        if источник.is_symlink():
            raise RuntimeError(f"Отслеживаемые ссылки запрещены в тестовом снимке: {относительный}")
        if not источник.is_file():
            raise RuntimeError(f"Отслеживаемый файл отсутствует: {относительный}")

    for относительный in пути:
        источник = корень / относительный
        назначение = снимок / относительный
        назначение.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(источник, назначение)

    # До первого коммита runner ещё не находится в индексе.
    runner = Path(__file__).resolve()
    shutil.copy2(runner, снимок / runner.name)

    # load_dotenv() ищет файл вверх по дереву. Пустая синтетическая граница не
    # позволяет ему дойти до случайного /tmp/.env или родительского каталога.
    (снимок / ".env").write_text("# synthetic empty test environment\n", encoding="utf-8")


def _окружение(временный: Path, предохранитель: Path) -> dict[str, str]:
    """Убирает унаследованные секреты и направляет состояние в temp."""
    окружение = {
        ключ: значение
        for ключ, значение in os.environ.items()
        if ключ.upper() in _БЕЗОПАСНОЕ_ОКРУЖЕНИЕ
    }
    окружение.update({
        "PYTHONPATH": str(предохранитель),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(временный / "tmp"),
        "MEMORY_DIR": str(временный / "memory"),
        "RAG_DIR": str(временный / "rag"),
        "DOCS_DIR": str(временный / "docs"),
        "REPORTS_DIR": str(временный / "reports"),
        "SCHEDULER_DB": str(временный / "memory" / "scheduler.db"),
        "HF_HOME": str(временный / "huggingface"),
        "XDG_CACHE_HOME": str(временный / "cache"),
        "HF_HUB_OFFLINE": "1",
        "OLLAMA_HOST": "127.0.0.1:9",
        "OLLAMA_URL": "http://127.0.0.1:9",
        "TRACKER_MOCK_TOKEN": "synthetic-mock-token",
        "TRACKER_MOCK_ORG": "synthetic-mock-org",
        "NO_PROXY": "*",
    })
    for каталог in ("tmp", "memory", "rag", "docs", "reports", "huggingface", "cache"):
        (временный / каталог).mkdir(parents=True, exist_ok=True)
    return окружение


_ПРЕДОХРАНИТЕЛЬ = r'''"""Сетевой предохранитель только для безопасного test runner."""
import ipaddress
import json
import os
import socket
import subprocess
import threading
import weakref

_настоящий_getaddrinfo = socket.getaddrinfo
_настоящий_bind = socket.socket.bind
_настоящий_connect = socket.socket.connect
_настоящий_connect_ex = socket.socket.connect_ex
_настоящий_create_connection = socket.create_connection
_настоящий_close = socket.socket.close
_настоящий_listen = socket.socket.listen
_настоящий_popen_init = subprocess.Popen.__init__
_служебные_порты = {8443, 11434, 11435, 11436}
_связанные = weakref.WeakKeyDictionary()
_слушатели = weakref.WeakKeyDictionary()
_файлы_разрешений = weakref.WeakKeyDictionary()
_замок_сети = threading.RLock()
_каталог_guard = os.path.dirname(os.path.abspath(__file__))
_каталог_разрешений = os.path.join(_каталог_guard, "listeners")
os.makedirs(_каталог_разрешений, mode=0o700, exist_ok=True)
os.environ["DAY30_TEST_GUARD"] = "1"

def _тип(sock):
    значение = int(sock.type)
    for имя in ("SOCK_NONBLOCK", "SOCK_CLOEXEC"):
        значение &= ~int(getattr(socket, имя, 0))
    return значение

def _протокол(sock, тип):
    if sock.proto:
        return int(sock.proto)
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        if тип == socket.SOCK_STREAM:
            return socket.IPPROTO_TCP
        if тип == socket.SOCK_DGRAM:
            return socket.IPPROTO_UDP
    return 0

def _endpoint(sock, адрес):
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return None
    if not isinstance(адрес, tuple) or len(адрес) < 2:
        raise OSError("Некорректный сетевой адрес запрещён безопасным test runner")
    хост, порт = адрес[0], адрес[1]
    if isinstance(хост, bytes):
        хост = хост.decode("ascii", "strict")
    if хост == "localhost":
        хост = "127.0.0.1" if sock.family == socket.AF_INET else "::1"
    if хост in (None, ""):
        raise OSError("Wildcard bind запрещён безопасным test runner")
    try:
        ip = ipaddress.ip_address(хост)
    except ValueError:
        raise OSError("Внешняя сеть запрещена безопасным test runner")
    if ip.is_unspecified:
        raise OSError("Wildcard bind запрещён безопасным test runner")
    if not ip.is_loopback:
        raise OSError("Внешняя сеть запрещена безопасным test runner")
    if (sock.family == socket.AF_INET and ip.version != 4) or (sock.family == socket.AF_INET6 and ip.version != 6):
        raise OSError("Семейство сетевого адреса не совпадает с сокетом")
    if not isinstance(порт, int):
        raise OSError("Некорректный сетевой порт запрещён безопасным test runner")
    тип = _тип(sock)
    return (int(sock.family), тип, _протокол(sock, тип), str(ip), порт)

def _активен(sock, endpoint, слушает):
    try:
        if sock.fileno() < 0 or _endpoint(sock, sock.getsockname()) != endpoint:
            return False
        if слушает and not sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
            return False
        return True
    except OSError:
        return False

def _старт_процесса(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            остаток = f.read().rsplit(b")", 1)[1].split()
        return остаток[19].decode("ascii")
    except (OSError, IndexError, UnicodeError):
        return None

def _удалить_файл_разрешения(sock):
    with _замок_сети:
        путь = _файлы_разрешений.pop(sock, None)
    if путь:
        try:
            os.unlink(путь)
        except FileNotFoundError:
            pass

def _записать_разрешение(sock, endpoint):
    pid, fd = os.getpid(), sock.fileno()
    inode = os.fstat(fd).st_ino
    старт = _старт_процесса(pid)
    if старт is None:
        raise OSError("Не удалось подтвердить процесс тестового listener")
    имя = f"{pid}-{fd}-{inode}.json"
    путь = os.path.join(_каталог_разрешений, имя)
    временный = путь + f".tmp-{threading.get_ident()}"
    данные = {"endpoint": list(endpoint), "pid": pid, "fd": fd, "inode": inode, "start": старт}
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    _удалить_файл_разрешения(sock)
    file_fd = os.open(временный, flags, 0o600)
    try:
        f = os.fdopen(file_fd, "w", encoding="utf-8")
        file_fd = -1
        with f:
            json.dump(данные, f, separators=(",", ":"))
        os.replace(временный, путь)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        try:
            os.unlink(временный)
        except FileNotFoundError:
            pass
    with _замок_сети:
        _файлы_разрешений[sock] = путь

def _разрешение_процесса(endpoint):
    try:
        имена = os.listdir(_каталог_разрешений)
    except OSError:
        return False
    for имя in имена:
        if not имя.endswith(".json"):
            continue
        путь = os.path.join(_каталог_разрешений, имя)
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(путь, flags)
            try:
                f = os.fdopen(fd, encoding="utf-8")
                fd = -1
                with f:
                    данные = json.load(f)
            finally:
                if fd >= 0:
                    os.close(fd)
            if tuple(данные["endpoint"]) != endpoint:
                continue
            pid, socket_fd, inode = int(данные["pid"]), int(данные["fd"]), int(данные["inode"])
            if данные["start"] != _старт_процесса(pid):
                raise OSError("stale process")
            if os.readlink(f"/proc/{pid}/fd/{socket_fd}") != f"socket:[{inode}]":
                raise OSError("stale socket")
            return True
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            try:
                os.unlink(путь)
            except FileNotFoundError:
                pass
    return False

def _разрешён(sock, endpoint):
    тип = endpoint[1]
    реестр = _слушатели if тип == socket.SOCK_STREAM else _связанные
    слушает = тип == socket.SOCK_STREAM
    with _замок_сети:
        for владелец, зарегистрирован in list(реестр.items()):
            if зарегистрирован == endpoint and _активен(владелец, зарегистрирован, слушает):
                return True
    return _разрешение_процесса(endpoint)

def _адрес(sock, адрес):
    endpoint = _endpoint(sock, адрес)
    if endpoint is None:
        return
    порт = endpoint[-1]
    if isinstance(порт, int) and порт in _служебные_порты:
        raise OSError("Подключение к локальной production-службе запрещено test runner")
    if _разрешён(sock, endpoint):
        return
    raise OSError("Сетевые подключения к localhost запрещены безопасным test runner")

def bind(self, address):
    endpoint = _endpoint(self, address)
    if endpoint is None:
        return _настоящий_bind(self, address)
    if endpoint[-1] in _служебные_порты:
        raise OSError("Bind на порту локальной production-службы запрещён test runner")
    результат = _настоящий_bind(self, address)
    фактический = _endpoint(self, self.getsockname())
    if фактический[-1] in _служебные_порты:
        self.close()
        raise OSError("Bind на порту локальной production-службы запрещён test runner")
    with _замок_сети:
        _связанные[self] = фактический
    if фактический[1] == socket.SOCK_DGRAM:
        try:
            _записать_разрешение(self, фактический)
        except Exception:
            _настоящий_close(self)
            raise
    return результат

def listen(self, *args, **kwargs):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        with _замок_сети:
            endpoint = _связанные.get(self)
        if endpoint is None:
            raise OSError("TCP-listener должен быть явно привязан к loopback")
    else:
        endpoint = None
    результат = _настоящий_listen(self, *args, **kwargs)
    if endpoint is not None:
        try:
            _записать_разрешение(self, endpoint)
            with _замок_сети:
                _слушатели[self] = endpoint
        except Exception:
            _настоящий_close(self)
            raise
    return результат

def close(self):
    _удалить_файл_разрешения(self)
    with _замок_сети:
        _связанные.pop(self, None)
        _слушатели.pop(self, None)
    return _настоящий_close(self)

def getaddrinfo(host, *args, **kwargs):
    if host not in (None, "localhost", b"localhost"):
        try:
            значение = host.decode("ascii", "strict") if isinstance(host, bytes) else host
            if not ipaddress.ip_address(значение).is_loopback:
                raise OSError("DNS внешнего адреса запрещён безопасным test runner")
        except ValueError:
            raise OSError("DNS внешнего адреса запрещён безопасным test runner") from None
    return _настоящий_getaddrinfo(host, *args, **kwargs)

def connect(self, address):
    _адрес(self, address)
    return _настоящий_connect(self, address)

def connect_ex(self, address):
    _адрес(self, address)
    return _настоящий_connect_ex(self, address)

def create_connection(address, *args, **kwargs):
    # Настоящий create_connection проходит через защищённые getaddrinfo и
    # socket.connect, где известны family/type/protocol клиентского сокета.
    return _настоящий_create_connection(address, *args, **kwargs)

def popen_init(self, *args, **kwargs):
    # Некоторые MCP-библиотеки передают процессу узкий env={...}. Без явного
    # PYTHONPATH такой Python-потомок потерял бы сетевой предохранитель.
    env = kwargs.get("env")
    if env is not None:
        env = dict(env)
        прежний = env.get("PYTHONPATH", "")
        части = [часть for часть in прежний.split(os.pathsep) if часть]
        if _каталог_guard not in части:
            части.insert(0, _каталог_guard)
        env["PYTHONPATH"] = os.pathsep.join(части)
        kwargs["env"] = env
    return _настоящий_popen_init(self, *args, **kwargs)

socket.getaddrinfo = getaddrinfo
socket.socket.bind = bind
socket.socket.close = close
socket.socket.listen = listen
socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.create_connection = create_connection
subprocess.Popen.__init__ = popen_init
'''


def _аргументы(argv: list[str] | None = None) -> argparse.Namespace:
    разбор = argparse.ArgumentParser(description="Безопасный запуск шести автоматических модулей Day-30")
    разбор.add_argument("-q", "--quiet", action="store_true", help="показывать только итог unittest")
    return разбор.parse_args(argv)


def _дождаться(команда: list[str], снимок: Path, окружение: dict[str, str], таймаут: int = 900) -> int:
    процесс = subprocess.Popen(команда, cwd=снимок, env=окружение, start_new_session=True)
    try:
        return процесс.wait(timeout=таймаут)
    except subprocess.TimeoutExpired:
        print(f"Тесты не завершились за {таймаут} с", file=sys.stderr)
        os.killpg(процесс.pid, signal.SIGTERM)
        try:
            процесс.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(процесс.pid, signal.SIGKILL)
            процесс.wait()
        return 124
    except BaseException:
        os.killpg(процесс.pid, signal.SIGTERM)
        try:
            процесс.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(процесс.pid, signal.SIGKILL)
            процесс.wait()
        raise


def _проверить_ipc() -> None:
    """Fail fast, если внешняя sandbox запрещает self-pipe asyncio/TestClient."""
    левый, правый = socket.socketpair()
    try:
        левый.sendall(b"x")
        if правый.recv(1) != b"x":
            raise RuntimeError("socketpair вернул неверные данные")
    except OSError as exc:
        raise RuntimeError(
            "Внешняя sandbox запрещает Unix socketpair, необходимый asyncio/TestClient; "
            "запустите runner в окружении с разрешённым локальным IPC"
        ) from exc
    finally:
        левый.close()
        правый.close()


def main(argv: list[str] | None = None) -> int:
    аргументы = _аргументы(argv)
    _проверить_ipc()
    корень = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="day30-safe-tests-") as temp:
        временный = Path(temp)
        снимок = временный / "project"
        снимок.mkdir()
        _копировать(корень, снимок)

        предохранитель = временный / "guard"
        предохранитель.mkdir()
        (предохранитель / "sitecustomize.py").write_text(_ПРЕДОХРАНИТЕЛЬ, encoding="utf-8")

        команда = [sys.executable, "-B", "-m", "unittest"]
        if аргументы.quiet:
            команда.append("-q")
        команда.extend(МОДУЛИ)
        return _дождаться(команда, снимок, _окружение(временный, предохранитель))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
