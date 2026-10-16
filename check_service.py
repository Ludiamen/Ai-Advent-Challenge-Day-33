#!/usr/bin/env python3
"""Проверка задания Дня 30: локальная LLM как приватный сервис.

Скрипт идёт по пунктам задания против живой службы `day31-llm` (systemd
пользователя) — так, как работает клиент: по HTTPS, с ключами API, из
контейнера Docker, то есть с другого сетевого хоста. Ключи проверки создаются
на время прогона и отзываются в конце.

  Часть 1  СЛУЖБА И HTTPS. Служба активна; /health по адресу в домашней сети с
           хоста и из Docker; сертификат проверяется своим центром (без него —
           отказ TLS); в сертификате — нужные адреса.
  Часть 2  API ИЗ DOCKER. Клиент `openai` (официальный SDK): /v1/models — одна
           модель; чат без потока и с потоком; usage; журнал видит адрес Docker.
  Часть 3  ДОСТУП. Без ключа 401, отозванный 401, «чат» к документам 403, чужая
           сеть Docker (10.99.0.0/24) 403; документы — ответ с дословной цитатой.
  Часть 4  ЛИМИТЫ. 413 по контексту до модели; max_tokens урезан; лимит ключа —
           429 и проход после Retry-After; всплеск — 429 queue_full; обрыв
           потока освобождает очередь.
  Часть 5  ПРИВАТНОСТЬ. Журнал сети сервиса — внешний хост только api.deepseek.com
           (День 31: ассистент проекта в режиме «облако»), иначе 0; журнал запросов без
           текстов; Ollama слушает только 127.0.0.1.
  Часть 6  НАГРУЗКА. Последний полный прогон 1→2→4→8: ошибок 0, отказы 429 прошли при
           повторе, p50/p95 и ресурсы записаны; документы — с цитатами.

Запуск:
    python check_service.py             # (≈ 10–15 минут на CPU)
    python check_service.py --в RESULTS.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.service import certs, config, load, manage  # noqa: E402
from check_tools import LINE, Отчёт, _секунды  # noqa: E402

МАРКЕР = "МАРКЕР-ПРОВЕРКИ-ДНЯ-30"


class Стенд:
    def __init__(self) -> None:
        н = config.загрузить()
        self.адрес = f"https://{certs.адрес_в_сети()}:{н.порт}"
        self.ca = os.path.join(manage.каталог_tls(), "ca.crt")
        self.лимиты = н.лимиты
        self.ключи: dict[str, str] = {}

    def ключ(self, имя: str, права: list[str]) -> str:
        полное = f"проверка-{имя}"
        manage.отозвать(полное)
        self.ключи[полное] = manage.создать_ключ(полное, права)
        return self.ключи[полное]

    def убрать(self) -> None:
        for имя in self.ключи:
            manage.отозвать(имя)

    def h(self, ключ: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {ключ}"}

    def http(self, **ещё) -> httpx.Client:
        return httpx.Client(base_url=self.адрес, verify=self.ca, timeout=900, **ещё)

    def docker(self, код: str, окружение: dict[str, str], сеть: str = "") -> tuple[int, str]:
        команда = ["docker", "run", "--rm", *(["--network", сеть] if сеть else []), "-v", f"{self.ca}:/ca.crt:ro",
                   *sum((["-e", f"{к}={в}"] for к, в in окружение.items()), []), manage.ОБРАЗ, "python", "-c", код]
        готово = subprocess.run(команда, capture_output=True, text=True, timeout=1200)
        return готово.returncode, (готово.stdout + готово.stderr).strip()


def часть_1(отчёт: Отчёт, с: Стенд) -> None:
    from agent.service import unit
    отчёт.часть(1, "Служба и HTTPS: systemd пользователя, адрес в домашней сети, свой центр сертификации")
    ст = unit.статус()
    отчёт(f"  служба day31-llm: {ст['состояние']} ({ст['подсостояние']}) · PID {ст['pid']} · с {ст['с']}")
    отчёт.пункт(ст["состояние"] == "active", "служба пользователя systemd «day31-llm» активна")
    with с.http() as к:
        здоровье = к.get("/health").json()
    отчёт(f"  GET {с.адрес}/health → {json.dumps(здоровье, ensure_ascii=False)[:160]}")
    отчёт.пункт(здоровье.get("status") == "ok" and здоровье.get("model") == "loc-rag-docs",
                "/health по адресу в домашней сети с хоста, сертификат проверен по ca.crt")
    try:
        httpx.get(с.адрес + "/health", timeout=10)
        без_ca = "соединение установлено"
    except httpx.ConnectError as exc:
        без_ca = "отказ TLS: " + ("CERTIFICATE_VERIFY_FAILED" if "CERTIFICATE_VERIFY_FAILED" in str(exc) else str(exc)[:60])
    отчёт(f"  без ca.crt: {без_ca}")
    отчёт.пункт(без_ca.startswith("отказ TLS"), "без своего центра клиент сертификату не верит — HTTPS настоящий")
    san = certs.сведения(os.path.join(manage.каталог_tls(), "server.crt"))
    отчёт(f"  сертификат: {', '.join(san['san'])} · до {san['до']}")
    отчёт.пункт(certs.адрес_в_сети() in san["san"] and "127.0.0.1" in san["san"], "в сертификате адрес в домашней сети и 127.0.0.1")
    код, вывод = с.docker("import httpx, os; r = httpx.get(os.environ['URL'] + '/health', verify='/ca.crt', timeout=30); "
                          "print(r.status_code, r.json()['status'])", {"URL": с.адрес})
    отчёт(f"  из Docker: {вывод[-80:]}")
    отчёт.пункт(код == 0 and вывод.endswith("200 ok"), "из контейнера Docker (другой сетевой хост) — /health по HTTPS")


КОД_OPENAI = r"""
import httpx, json, os
from openai import OpenAI
к = OpenAI(base_url=os.environ["URL"] + "/v1", api_key=os.environ["KEY"], http_client=httpx.Client(verify="/ca.crt", timeout=600))
модели = [м.id for м in к.models.list().data]
r = к.chat.completions.create(model="loc-rag-docs", max_tokens=20,
    messages=[{"role": "user", "content": "Ответь одним словом: столица Франции? " + os.environ["MARK"]}])
поток = к.chat.completions.create(model="loc-rag-docs", max_tokens=20, stream=True,
    messages=[{"role": "user", "content": "Назови один цвет радуги одним словом. " + os.environ["MARK"]}])
куски, причина = [], None
for ч in поток:
    if ч.choices and ч.choices[0].delta.content:
        куски.append(ч.choices[0].delta.content)
    if ч.choices and ч.choices[0].finish_reason:
        причина = ч.choices[0].finish_reason
print(json.dumps({"модели": модели, "ответ": r.choices[0].message.content, "usage": r.usage.model_dump(),
                  "причина": r.choices[0].finish_reason, "поток": "".join(куски), "кусков": len(куски),
                  "причина_потока": причина}, ensure_ascii=False))
"""


def часть_2(отчёт: Отчёт, с: Стенд) -> None:
    отчёт.часть(2, "OpenAI-совместимый API из Docker официальным клиентом openai")
    ключ = с.ключ("openai", ["чат"])
    начало = time.monotonic()
    код, вывод = с.docker(КОД_OPENAI, {"URL": с.адрес, "KEY": ключ, "MARK": МАРКЕР})
    try:
        д = json.loads(вывод.splitlines()[-1])
    except (ValueError, IndexError):
        д = {}
        отчёт(f"  вывод: {вывод[-300:]}")
    отчёт(f"  models: {д.get('модели')} · ответ: «{(д.get('ответ') or '').strip()[:60]}» · usage {д.get('usage')}")
    отчёт(f"  поток: «{(д.get('поток') or '').strip()[:60]}» · кусков {д.get('кусков')} · причина {д.get('причина_потока')}")
    отчёт.пункт(код == 0 and д.get("модели") == ["loc-rag-docs"], "/v1/models — одна модель loc-rag-docs")
    отчёт.пункт(bool((д.get("ответ") or "").strip()) and (д.get("usage") or {}).get("prompt_tokens", 0) > 0
                and д.get("причина") in ("stop", "length"), "чат без потока: ответ, usage, finish_reason")
    отчёт.пункт(bool((д.get("поток") or "").strip()) and (д.get("кусков") or 0) >= 1 and д.get("причина_потока"),
                f"чат с потоком (SSE) — клиент openai собрал ответ из кусков · {_секунды(time.monotonic() - начало)}")
    with с.http() as к:
        журнал = к.get("/admin/api/state", headers=с.h(с.ключ("админ", ["админ"]))).json()["requests"]
    докер = [з for з in журнал if з.get("ключ") == "проверка-openai"]
    адреса = sorted({з["адрес"] for з in докер})
    отчёт(f"  журнал сервиса: запросов ключа проверка-openai {len(докер)}, адреса {', '.join(адреса)}")
    отчёт.пункт(bool(докер) and all(а.startswith("172.17.") for а in адреса),
                "журнал видит адрес контейнера Docker (172.17.x) — запросы пришли по сети")


def часть_3(отчёт: Отчёт, с: Стенд) -> None:
    отчёт.часть(3, "Доступ: ключи, права, подсети; ответ по документам")
    чат, док = с.ключ("чат", ["чат"]), с.ключ("документы", ["документы"])
    отозванный = с.ключ("отозванный", ["чат"])
    manage.отозвать("проверка-отозванный")
    with с.http() as к:
        r1 = к.get("/v1/models")
        r2 = к.get("/v1/models", headers=с.h(отозванный))
        r3 = к.post("/v1/documents/answer", headers=с.h(чат), json={"question": "Срок оплаты?"})
    for r, что in ((r1, "без ключа"), (r2, "отозванный ключ"), (r3, "ключ «чат» → документы")):
        отчёт(f"  {что}: {r.status_code} {r.json()['error']['code']}")
    отчёт.пункт((r1.status_code, r2.status_code, r3.status_code) == (401, 401, 403),
                "без ключа 401, отозванный 401, ключ без права «документы» 403")
    subprocess.run(["docker", "network", "rm", "day30-foreign"], capture_output=True)
    subprocess.run(["docker", "network", "create", "--subnet", "10.99.0.0/24", "day30-foreign"], capture_output=True, check=True)
    try:
        код, вывод = с.docker("import httpx, os; r = httpx.get(os.environ['URL'] + '/health', verify='/ca.crt', timeout=30); "
                              "print(r.status_code, r.json()['error']['code'])", {"URL": с.адрес}, сеть="day30-foreign")
    finally:
        subprocess.run(["docker", "network", "rm", "day30-foreign"], capture_output=True)
    отчёт(f"  из чужой сети Docker 10.99.0.0/24: {вывод[-60:]}")
    отчёт.пункт(вывод.endswith("403 forbidden_network"), "чужая подсеть — 403 до проверки ключа; сеть удалена")
    from agent.rag import localbench
    вопрос = localbench.вопросы(ids=["c01"])[0]["вопрос"]
    начало = time.monotonic()
    with с.http() as к:
        r = к.post("/v1/documents/answer", headers=с.h(док), json={"question": вопрос})
    д = r.json()
    цитаты = [ц for ц in д.get("quotes") or [] if ц.get("confirmed")]
    отчёт(f"  > {(д.get('answer') or '')[:120]}")
    for и in (д.get("sources") or [])[:1]:
        отчёт(f"    [{и['n']}] {и['source']} · {и.get('section', '')} · {и['chunk_id']}")
    for ц in цитаты[:1]:
        отчёт(f"      ✓ {ц['check']} «{ц['text'][:100]}»")
    отчёт.пункт(r.status_code == 200 and re.search(r"\b20\b", д.get("answer") or "") is not None
                and any(ц.get("check") == "дословно" for ц in цитаты),
                f"документы с ключом «документы»: «20 числа», источник и дословная цитата · {_секунды(time.monotonic() - начало)}")


async def _всплеск(с: Стенд, ключи: list[str]) -> list[tuple[int, str]]:
    async with httpx.AsyncClient(base_url=с.адрес, verify=с.ca, timeout=600) as к:
        async def один(ключ: str, i: int):
            await asyncio.sleep(i * 0.05)
            r = await к.post("/v1/chat/completions", headers=с.h(ключ), json={
                "max_tokens": 16, "messages": [{"role": "user", "content": f"Скажи число {i}. {МАРКЕР}"}]})
            return r.status_code, (r.json().get("error") or {}).get("code", "")
        return await asyncio.gather(*(один(к, i) for i, к in enumerate(ключи)))


def часть_4(отчёт: Отчёт, с: Стенд) -> None:
    отчёт.часть(4, "Лимиты: контекст, предел ответа, лимит ключа, очередь, обрыв потока")
    л = с.лимиты
    чат = с.ключ("лимиты", ["чат"])
    with с.http() as к:
        начало = time.monotonic()
        r = к.post("/v1/chat/completions", headers=с.h(чат),
                   json={"messages": [{"role": "user", "content": "Проверка предела. " * 1500}]})
        секунд = time.monotonic() - начало
        отчёт(f"  длинный вход: {r.status_code} {r.json()['error']['code']} · X-Prompt-Tokens {r.headers.get('x-prompt-tokens')} · "
              f"{секунд:.2f} с")
        отчёт.пункт(r.status_code == 413 and r.json()["error"]["code"] == "context_length_exceeded"
                    and int(r.headers.get("x-prompt-tokens", 0)) > л.вход_токенов and секунд < 3,
                    f"413 context_length_exceeded до вызова модели (вход > {л.вход_токенов} токенов, ответ за секунды)")
        r = к.post("/v1/chat/completions", headers=с.h(чат),
                   json={"max_tokens": 2000, "messages": [{"role": "user", "content": f"Скажи: да. {МАРКЕР}"}]})
        отчёт(f"  max_tokens 2000: {r.status_code} · X-Max-Tokens-Clamped {r.headers.get('x-max-tokens-clamped')}")
        отчёт.пункт(r.status_code == 200 and r.headers.get("x-max-tokens-clamped") == f"2000->{л.ответ_токенов}",
                    f"max_tokens больше {л.ответ_токенов} — урезан, ответ об этом говорит")
        лимит = с.ключ("ведро", ["чат"])
        коды = [к.get("/v1/models", headers=с.h(лимит)) for _ in range(л.запросов_в_минуту + 1)]
        последний = коды[-1]
        повтор = int(последний.headers.get("retry-after", "0"))
        отчёт(f"  {л.запросов_в_минуту + 1} запросов подряд: {' '.join(str(r.status_code) for r in коды)} · "
              f"{последний.json().get('error', {}).get('code')} · Retry-After {повтор} с")
        time.sleep(повтор + 0.5)
        после = к.get("/v1/models", headers=с.h(лимит)).status_code
        отчёт(f"  через Retry-After: {после}")
        отчёт.пункт([r.status_code for r in коды] == [200] * л.запросов_в_минуту + [429] and после == 200,
                    f"лимит ключа {л.запросов_в_минуту}/мин: следующий — 429 rate_limited, после Retry-After — снова 200")
    ключи_всплеска = [с.ключ(f"всплеск-{i}", ["чат"]) for i in range(л.очередь + 2)]
    итоги = asyncio.run(_всплеск(с, ключи_всплеска))
    отчёт(f"  всплеск {len(итоги)} запросов: {', '.join(f'{к}{(' ' + о) if о else ''}' for к, о in итоги)}")
    отказы = [о for к, о in итоги if к == 429]
    отчёт.пункт(sum(1 for к, _ in итоги if к == 200) == л.очередь + 1 and отказы == ["queue_full"],
                f"очередь: 1 генерация + {л.очередь} ждут, следующий — 429 queue_full с Retry-After")
    обрыв = с.ключ("обрыв", ["чат"])
    with с.http() as к:
        with к.stream("POST", "/v1/chat/completions", headers=с.h(обрыв), json={
                "stream": True, "max_tokens": 600,
                "messages": [{"role": "user", "content": f"Перечисли подробно тридцать фактов о космосе. {МАРКЕР}"}]}) as r:
            for строка in r.iter_lines():
                if строка.startswith("data: {") and '"content"' in строка:
                    break
        начало = time.monotonic()
        r = к.post("/v1/chat/completions", headers=с.h(обрыв),
                   json={"max_tokens": 5, "messages": [{"role": "user", "content": f"Скажи: да. {МАРКЕР}"}]})
        секунд = time.monotonic() - начало
        состояние = к.get("/health").json()["queue"]
    отчёт(f"  после обрыва потока (600 токенов) следующий запрос: {r.status_code} за {секунд:.1f} с · очередь "
          f"идёт {состояние['идёт']}, ждут {состояние['ждут']}")
    отчёт.пункт(r.status_code == 200 and секунд < 40 and состояние["ждут"] == 0,
                "обрыв клиента освобождает очередь и прерывает генерацию (600 токенов шли бы ≈ 2–3 мин)")


def часть_5(отчёт: Отчёт, с: Стенд) -> None:
    отчёт.часть(5, "Приватность: сеть сервиса, журнал без текстов, Ollama только на 127.0.0.1")
    with с.http() as к:
        сост = к.get("/admin/api/state", headers=с.h(с.ключи["проверка-админ"])).json()
    сеть = сост.get("network") or {}
    хосты = set(сеть.get("внешние_хосты") or [])
    облако = config.загрузить().проект.облачный_хост
    отчёт(f"  журнал сети сервиса: локальных {сеть.get('локальных')}, внешних {сеть.get('внешних')} "
          f"{sorted(хосты) or ''}, заблокировано {сеть.get('заблокировано')}")
    # День 31: единственный разрешённый внешний хост — облачная модель ассистента проекта
    # (/v1/project/help в режиме «облако»); чат и документы наружу не ходят.
    отчёт.пункт(хосты <= {облако} and (сеть.get("локальных") or 0) > 0,
                f"сервис в офлайн-режиме: внешний хост — только {облако} (ассистент проекта), остальное не уходит с машины")
    путь = os.path.join(config.каталог_данных(), "requests.jsonl")
    текст = open(путь, encoding="utf-8").read()
    отчёт(f"  журнал запросов: {len(текст.splitlines())} строк; маркер вопросов проверки в нём: "
          f"{'есть' if МАРКЕР in текст else 'нет'}")
    отчёт.пункт(МАРКЕР not in текст and "Проверка предела" not in текст, "в журнале запросов нет текстов вопросов")
    слушают = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True).stdout
    ollama = sorted({с.split()[3] for с in слушают.splitlines() if с.split()[3].endswith(":11434")})
    сервис = sorted({с.split()[3] for с in слушают.splitlines() if с.split()[3].endswith(f":{config.загрузить().порт}")})
    отчёт(f"  слушают: Ollama {', '.join(ollama)} · сервис {', '.join(сервис)}")
    отчёт.пункт(ollama == ["127.0.0.1:11434"], "Ollama — только 127.0.0.1: наружу выходит только шлюз")


def часть_6(отчёт: Отчёт) -> None:
    отчёт.часть(6, "Нагрузка: последний полный прогон из Docker — ступени, ошибки, отказы, ресурсы")
    прогон = load.полный(load.каталог_итогов(config.КОРЕНЬ))
    if not прогон:
        отчёт.пункт(False, "полного прогона 1 → 2 → 4 → 8 нет — python cli.py --сервис-нагрузка")
        return
    св = load.сводка(прогон)
    отчёт(f"  прогон {прогон['файл']} → {св['адрес']}")
    for ст in св["ступени"]:
        р = ст.get("ресурсы") or {}
        отчёт(f"    {ст['клиентов']} клиентов: запросов {ст['запросов']}, успешно {ст['успешно']}, ошибок {ст['ошибок']}, "
              f"429 {ст['отказов_429']}, p50 {ст['задержка_p50_с']} с, p95 {ст['задержка_p95_с']} с, ожидание p50 "
              f"{ст['ожидание_p50_с']} с, в минуту {ст['ответов_в_минуту']}, пик ОЗУ {р.get('пик_мб')} МБ")
    д = св.get("документы") or {}
    отчёт(f"    документы: запросов {д.get('запросов')}, успешно {д.get('успешно')}, p50 {д.get('задержка_p50_с')} с")
    ступени = [ст["клиентов"] for ст in св["ступени"]]
    отчёт.пункт(ступени == [1, 2, 4, 8], "ступени 1 → 2 → 4 → 8 одновременных клиентов")
    отчёт.пункт(св["всего_ошибок"] == 0 and all(ст["успешно"] == ст["запросов"] for ст in св["ступени"]),
                "ошибок 0: каждый запрос получил ответ")
    восемь = св["ступени"][-1] if св["ступени"] else {}
    отчёт.пункт(восемь.get("отказов_429", 0) > 0, "на ступени 8 очередь отказывала (429) — и запросы прошли при повторе")
    отчёт.пункт(all(ст["задержка_p95_с"] and (ст.get("ресурсы") or {}).get("пик_мб") for ст in св["ступени"]),
                "у каждой ступени p50/p95 и пик памяти раннера")
    записи_док = [з for з in прогон["записи"] if з.get("вид") == "документы"]
    отчёт.пункт(bool(записи_док) and all(з.get("код") == 200 and з.get("цитат_подтверждено") for з in записи_док),
                "вопросы по документам под нагрузкой — ответы с подтверждёнными цитатами")


def main() -> int:
    разбор = argparse.ArgumentParser(description="Проверка Дня 30: локальная LLM как приватный сервис.")
    разбор.add_argument("--в", dest="out", default="", metavar="ФАЙЛ")
    а = разбор.parse_args()
    отчёт = Отчёт()
    с = Стенд()
    начало = time.monotonic()
    отчёт(LINE)
    отчёт(f"Проверка Дня 30: приватный AI-сервис · {с.адрес} · модель loc-rag-docs")
    try:
        часть_1(отчёт, с)
        часть_2(отчёт, с)
        часть_3(отчёт, с)
        часть_4(отчёт, с)
        часть_5(отчёт, с)
        часть_6(отчёт)
    except (httpx.HTTPError, subprocess.SubprocessError, RuntimeError, OSError, ValueError, KeyError) as exc:
        отчёт.пункт(False, f"прервано: {type(exc).__name__}: {exc}")
    finally:
        с.убрать()
    отчёт(LINE)
    отчёт(f"ИТОГ: проверок {отчёт.проверок}, провалов {отчёт.провалов}. Прогон {_секунды(time.monotonic() - начало)}.")
    if а.out:
        with open(а.out, "a", encoding="utf-8") as f:
            f.write("\n```text\n" + "\n".join(отчёт.строки) + "\n```\n")
    return 1 if отчёт.провалов else 0


if __name__ == "__main__":
    sys.exit(main())
