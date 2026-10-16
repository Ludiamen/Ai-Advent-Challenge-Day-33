#!/usr/bin/env python3
"""Нагрузочный клиент приватного сервиса Дня 30 (spec FR-18, plan D-13).

Запускается в контейнере Docker — то есть с другого сетевого хоста — и ходит в
сервис по адресу в домашней сети, проверяя сертификат по `ca.crt`:

    python service_load.py --задание task.json --ca ca.crt --выход run.json

Задание (JSON): адрес, ступени (например [1, 2, 4, 8]), вопросов на клиента,
max_tokens, вопросы чата (общие, без ПД) и документов (проверенные без ПД),
ключи — свой у каждого клиента нагрузки и ключ «админ», метка.

На ступень N: отметка начала на сервере (сервер снимает память и CPU раннера),
N клиентов одновременно, у каждого — K вопросов подряд с потоком (первый токен,
ожидание в очереди из последнего куска); после 429 — повтор по Retry-After до
общего срока запроса; отметка конца. Затем — вопросы по документам одновременно. В итог — только
метаданные запросов: тексты ответов не сохраняются.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx

# Retry-After — подсказка сервиса (медиана недавних генераций × очередь), а не обещание:
# после коротких проверочных запросов она мала, и клиент с пределом 12 попыток
# сдавался раньше, чем очередь разбиралась. Поэтому предел — общий срок запроса.
СРОК_С = 1200
ПОПЫТОК = 1000                      # предохранитель от бесконечного цикла


def _пауза_429(запись: dict, r: httpx.Response) -> float | None:
    """Учесть отказ 429; пауза по Retry-After или None — срок запроса выйдет раньше."""
    запись["отказов_429"] += 1
    пауза = float(r.headers.get("retry-after") or 5)
    if time.time() + пауза - запись["начало"] > СРОК_С:
        запись["ошибка"] = f"429 дольше {СРОК_С} с"
        return None
    return пауза


def _заголовки(ключ: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {ключ}"}


async def _чат(к: httpx.AsyncClient, ключ: str, вопрос: str, max_tokens: int) -> dict:
    запись = {"вид": "чат", "код": None, "ошибка": "", "отказов_429": 0, "ожидание_с": None,
              "первый_токен_с": None, "длительность_с": None, "ответ": None, "вход": None, "причина": None}
    запись["начало"] = time.time()
    for _ in range(ПОПЫТОК):
        t0 = time.time()
        try:
            async with к.stream("POST", "/v1/chat/completions", headers=_заголовки(ключ),
                                json={"model": "loc-rag-docs", "stream": True, "max_tokens": max_tokens,
                                      "messages": [{"role": "user", "content": вопрос}]}) as r:
                if r.status_code == 429:
                    await r.aread()
                    пауза = _пауза_429(запись, r)
                    if пауза is None:
                        break
                    await asyncio.sleep(пауза)
                    continue
                запись["код"] = r.status_code
                if r.status_code != 200:
                    запись["ошибка"] = (await r.aread()).decode("utf-8", "replace")[:200]
                    break
                async for строка in r.aiter_lines():
                    if not строка.startswith("data: "):
                        continue
                    данные = строка[6:]
                    if данные == "[DONE]":
                        break
                    ч = json.loads(данные)
                    if "error" in ч:
                        запись["ошибка"] = ч["error"].get("code") or "error"
                        break
                    выбор = (ч.get("choices") or [{}])[0]
                    if (выбор.get("delta") or {}).get("content") and запись["первый_токен_с"] is None:
                        запись["первый_токен_с"] = round(time.time() - t0, 2)
                    if выбор.get("finish_reason"):
                        запись["причина"] = выбор["finish_reason"]
                        запись["ожидание_с"] = ч.get("x_queue_wait")
                        запись["ответ"] = (ч.get("usage") or {}).get("completion_tokens")
                        запись["вход"] = (ч.get("usage") or {}).get("prompt_tokens")
            запись["длительность_с"] = round(time.time() - t0, 2)
            break
        except (httpx.HTTPError, ValueError) as exc:
            запись["код"], запись["ошибка"] = -1, f"{type(exc).__name__}: {exc}"[:200]
            break
    else:
        запись["ошибка"] = "429 без конца"
    запись["конец"] = time.time()
    запись["полное_с"] = round(запись["конец"] - запись["начало"], 2)
    return запись


async def _документы(к: httpx.AsyncClient, ключ: str, вопрос: str) -> dict:
    запись = {"вид": "документы", "ступень": 0, "код": None, "ошибка": "", "отказов_429": 0, "начало": time.time()}
    for _ in range(ПОПЫТОК):
        t0 = time.time()
        try:
            r = await к.post("/v1/documents/answer", headers=_заголовки(ключ), json={"question": вопрос})
        except httpx.HTTPError as exc:
            запись["код"], запись["ошибка"] = -1, f"{type(exc).__name__}: {exc}"[:200]
            break
        if r.status_code == 429:
            пауза = _пауза_429(запись, r)
            if пауза is None:
                break
            await asyncio.sleep(пауза)
            continue
        запись["код"] = r.status_code
        запись["длительность_с"] = round(time.time() - t0, 2)
        запись["ожидание_с"] = float(r.headers.get("x-queue-wait") or 0)
        if r.status_code == 200:
            д = r.json()
            запись["цитат_подтверждено"] = sum(1 for ц in д.get("quotes") or [] if ц.get("confirmed"))
            запись["не_знаю"] = bool(д.get("abstain"))
            запись["ответ"] = (д.get("usage") or {}).get("completion_tokens")
        else:
            запись["ошибка"] = r.text[:200]
        break
    запись["конец"] = time.time()
    return запись


async def _отметка(к: httpx.AsyncClient, ключ: str, событие: str, ступень: int | str) -> dict:
    try:
        r = await к.post("/admin/api/step", headers=_заголовки(ключ), json={"событие": событие, "ступень": ступень})
        return r.json() if r.status_code == 200 else {}
    except httpx.HTTPError:
        return {}


async def главная(задание: dict, ca: str) -> dict:
    итог = {"метка": задание["метка"], "адрес": задание["адрес"], "начало": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "записи": [], "ресурсы": {}}
    ключи = задание["ключи"]
    вопросы = list(задание["вопросы_чата"])
    номер = 0
    async with httpx.AsyncClient(base_url=задание["адрес"], verify=ca, timeout=httpx.Timeout(900, connect=15)) as к:
        for n in задание["ступени"]:
            print(f"ступень {n}: клиентов {n} × вопросов {задание['вопросов']}", flush=True)
            await _отметка(к, ключи["админ"], "начало", n)

            async def клиент(i: int) -> list[dict]:
                nonlocal номер
                записи = []
                for j in range(задание["вопросов"]):
                    вопрос = вопросы[номер % len(вопросы)]
                    номер += 1
                    з = await _чат(к, ключи["чат"][i], вопрос, задание["max_tokens"])
                    з.update({"ступень": n, "клиент": i + 1, "номер": j + 1})
                    записи.append(з)
                    print(f"  клиент {i + 1}.{j + 1}: код {з['код']} · {з['длительность_с']} с · ожидание "
                          f"{з['ожидание_с']} · 429×{з['отказов_429']} {з['ошибка'][:60]}", flush=True)
                return записи

            for группа in await asyncio.gather(*(клиент(i) for i in range(n))):
                итог["записи"].extend(группа)
            итог["ресурсы"][str(n)] = await _отметка(к, ключи["админ"], "конец", n)
        документы = задание.get("вопросы_документов") or []
        if документы:
            print(f"документы: {len(документы)} одновременно", flush=True)
            await _отметка(к, ключи["админ"], "начало", "документы")
            for з in await asyncio.gather(*(_документы(к, ключи["документы"][i % len(ключи["документы"])], в)
                                            for i, в in enumerate(документы))):
                итог["записи"].append(з)
                print(f"  документы: код {з['код']} · {з.get('длительность_с')} с · цитат "
                      f"{з.get('цитат_подтверждено')} · 429×{з['отказов_429']}", flush=True)
            итог["ресурсы"]["документы"] = await _отметка(к, ключи["админ"], "конец", "документы")
    итог["конец"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return итог


def main() -> int:
    разбор = argparse.ArgumentParser(description="Нагрузочный клиент приватного сервиса Дня 30")
    разбор.add_argument("--задание", required=True)
    разбор.add_argument("--ca", required=True)
    разбор.add_argument("--выход", required=True)
    а = разбор.parse_args()
    with open(а.задание, encoding="utf-8") as f:
        задание = json.load(f)
    итог = asyncio.run(главная(задание, а.ca))
    with open(а.выход, "w", encoding="utf-8") as f:
        json.dump(итог, f, ensure_ascii=False, indent=1)
    ошибок = sum(1 for з in итог["записи"] if з.get("код") != 200 or з.get("ошибка"))
    print(f"итог: запросов {len(итог['записи'])}, ошибок {ошибок} → {а.выход}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
