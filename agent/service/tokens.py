"""Подсчёт токенов входа до вызова модели (spec FR-10, plan D-09).

В Ollama 0.34.4 нет /api/tokenize. Словарь берётся из GGUF-файла самой модели
через llama-cpp-python (`vocab_only=True` — тот же код словаря, что у раннера
Ollama, веса не грузятся, ≈ 0,8 с). Шаблон чата измерен на воротах 0 против
`prompt_eval_count`: 7 коротких входов — точно, длинный (1500 токенов) — на 1
больше, то есть счёт не меньше настоящего.

  Токенизатор(путь_весов).вход(сообщения) -> int
  путь_весов(адрес_ollama, модель) -> путь к GGUF из строки FROM модельфайла
"""

from __future__ import annotations

import re
import threading
from typing import Any, Callable

import httpx

# <|im_start|>роль\nтекст<|im_end|>\n × N, затем начало ответа без размышлений (think: false).
ХВОСТ = "<|im_start|>assistant\n<think>\n\n</think>\n\n"


def шаблон(сообщения: list[dict[str, Any]]) -> str:
    return "".join(f"<|im_start|>{с.get('role', '')}\n{с.get('content', '')}<|im_end|>\n" for с in сообщения) + ХВОСТ


def путь_весов(адрес: str, модель: str, http: httpx.Client | None = None) -> str:
    к = http or httpx.Client(timeout=30)
    ответ = к.post(f"{адрес}/api/show", json={"model": модель})
    ответ.raise_for_status()
    найдено = re.search(r"^FROM\s+(\S+)", ответ.json().get("modelfile") or "", re.M)
    if not найдено:
        raise RuntimeError(f"в модельфайле {модель} нет строки FROM")
    return найдено.group(1)


class Токенизатор:
    """Ленивый: словарь грузится при первом подсчёте. `счётчик` — подмена в тестах."""

    def __init__(self, путь: str = "", счётчик: Callable[[str], int] | None = None) -> None:
        self.путь = путь
        self._счётчик = счётчик
        self._замок = threading.RLock()

    def _загрузить(self) -> Callable[[str], int]:
        with self._замок:
            if self._счётчик is None:
                from llama_cpp import Llama
                м = Llama(model_path=self.путь, vocab_only=True, verbose=False)
                self._счётчик = lambda текст: len(м.tokenize(текст.encode("utf-8"), add_bos=False, special=True))
        return self._счётчик

    def число(self, текст: str) -> int:
        счётчик = self._загрузить()
        with self._замок:                 # словарь llama_cpp — по одному вызову за раз
            return счётчик(текст)

    def вход(self, сообщения: list[dict[str, Any]]) -> int:
        return self.число(шаблон(сообщения))
