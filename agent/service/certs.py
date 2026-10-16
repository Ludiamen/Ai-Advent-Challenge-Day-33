"""HTTPS приватного сервиса: свой центр сертификации (spec FR-07, plan D-06).

Ключ и сертификат центра (`ca.key`, `ca.crt`) создаются один раз; сертификат
сервера (`server.crt`, `server.key`) выпускается этим центром на адреса и имена
из настроек плюс текущий адрес ноутбука в домашней сети. Сменился адрес
(DHCP) — выпустить заново, центр остаётся прежним, и клиентам не надо менять
`ca.crt`. Всё — в $MEMORY_DIR/service/tls (вне репозитория); через `openssl`.

  выпустить(каталог, адреса, имена, дней) -> {"ca": …, "crt": …, "key": …, "san": […]}
  адрес_в_сети() -> "192.168.1.43" | ""    адрес исходящего интерфейса (без пакетов наружу)
  сведения(crt) -> {"san": […], "до": "…"}
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any


def _openssl(*аргументы: str) -> str:
    готово = subprocess.run(["openssl", *аргументы], capture_output=True, text=True, timeout=60)
    if готово.returncode != 0:
        raise RuntimeError(f"openssl {аргументы[0]}: {готово.stderr.strip()[:300]}")
    return готово.stdout


def адрес_в_сети() -> str:
    """Адрес ноутбука в его сети — src маршрута по умолчанию. Не «маршрут до 1.1.1.1»: у владельца
    часть адресов идёт через VPN, и тогда получался бы адрес туннеля. Только чтение таблицы."""
    try:
        вывод = subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True, timeout=10).stdout
        найдено = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)", вывод)
        if найдено:
            return найдено.group(1)
        устройство = re.search(r"\bdev (\S+)", вывод)
        if устройство:
            адреса = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", устройство.group(1)],
                                    capture_output=True, text=True, timeout=10).stdout
            найдено = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", адреса)
            return найдено.group(1) if найдено else ""
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def выпустить(каталог: str, адреса: list[str], имена: list[str], дней: int = 825) -> dict[str, Any]:
    os.makedirs(каталог, exist_ok=True)
    ca_key, ca_crt = os.path.join(каталог, "ca.key"), os.path.join(каталог, "ca.crt")
    key, csr, crt = (os.path.join(каталог, и) for и in ("server.key", "server.csr", "server.crt"))
    if not (os.path.exists(ca_key) and os.path.exists(ca_crt)):
        _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", ca_key, "-out", ca_crt,
                 "-days", "3650", "-subj", "/CN=Day-30 private service CA",
                 "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        os.chmod(ca_key, 0o600)
    адреса = list(dict.fromkeys(а for а in адреса if а))
    имена = list(dict.fromkeys(и for и in имена if и))
    san = ",".join([f"IP:{а}" for а in адреса] + [f"DNS:{и}" for и in имена])
    _openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", csr, "-subj", "/CN=day30-llm")
    os.chmod(key, 0o600)
    расширения = os.path.join(каталог, "server.ext")
    with open(расширения, "w", encoding="utf-8") as f:
        f.write(f"subjectAltName={san}\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\n")
    _openssl("x509", "-req", "-in", csr, "-CA", ca_crt, "-CAkey", ca_key, "-CAcreateserial", "-out", crt,
             "-days", str(дней), "-sha256", "-extfile", расширения)
    for лишний in (csr, расширения):
        os.remove(лишний)
    return {"ca": ca_crt, "crt": crt, "key": key, **сведения(crt)}


def сведения(crt: str) -> dict[str, Any]:
    if not os.path.exists(crt):
        return {"san": [], "до": ""}
    текст = _openssl("x509", "-in", crt, "-noout", "-ext", "subjectAltName", "-enddate")
    san = re.findall(r"(?:IP Address|DNS):([^,\s]+)", текст)
    до = (re.search(r"notAfter=(.+)", текст) or [None, ""])[1].strip()
    return {"san": san, "до": до}
