#!/usr/bin/env python3
"""Ускоренная копия записи показа: ожидание модели ×N, сцены — как есть (День 27).

Показ Дня 27 — две живые генерации на CPU по 5–10 минут. Основная запись
остаётся без монтажа: в ней честное время ноутбука. Эта копия для просмотра:
отрезки, где модель думает (их начало и конец водитель показа пишет в файл
меток), ускорены в N раз, и на них в углу надпись «ускорено ×N — модель пишет
на CPU». Сцены, ответы и подписи — в обычной скорости.

    python demo_speedup.py demo/показ-….mp4 метки.json НАЧАЛО_ЗАПИСИ_МС [--раз 8]

НАЧАЛО_ЗАПИСИ_МС — время запуска записи в миллисекундах эпохи (demo_show.sh
запоминает его, запуская record_demo.sh); метки — в той же шкале (Date.now()
водителя). Копия ложится рядом: показ-….mp4 → показ-…-ускорено.mp4.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ШРИФТ = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
МИН_ОЖИДАНИЕ = 10.0      # короче — ускорять незачем
FPS = 25                 # как у record_demo.sh


def длительность(путь: str) -> float:
    готово = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=nw=1:nk=1", путь], capture_output=True, text=True, check=True)
    return float(готово.stdout.strip())


def отрезки(метки: list[dict], старт_мс: float, всего: float) -> list[tuple[float, float, bool]]:
    """(начало, конец, ускорить) по всей записи, в секундах от её начала."""
    итог: list[tuple[float, float, bool]] = []
    t = 0.0
    for м in sorted(метки, key=lambda м: м["начало"]):
        a = max(t, (м["начало"] - старт_мс) / 1000)
        b = min(всего, (м["конец"] - старт_мс) / 1000)
        if b - a < МИН_ОЖИДАНИЕ:
            continue
        if a > t:
            итог.append((t, a, False))
        итог.append((a, b, True))
        t = b
    if t < всего:
        итог.append((t, всего, False))
    return итог


def собрать(вход: str, выход: str, части: list[tuple[float, float, bool]], раз: int) -> None:
    """Каждый отрезок — отдельным проходом ffmpeg с перемоткой входа, потом склейка.

    Один граф с несколькими trim из одного входа (первая версия) держит в памяти
    несжатые кадры всех ещё не склеенных отрезков: на записи 1568×938 в 13 минут
    это гигабайты, подкачка и 45 минут без результата. Отдельные проходы читают
    только свой кусок и кодируют только свой выход."""
    каталог = tempfile.mkdtemp(prefix="ускорение-")
    надпись = os.path.join(каталог, "надпись.txt")
    with open(надпись, "w", encoding="utf-8") as f:
        # Надпись — из файла: в тексте фильтра пришлось бы экранировать «:» и кавычки.
        f.write(f"ускорено ×{раз} — модель пишет на CPU")
    try:
        куски = []
        for i, (a, b, ускорить) in enumerate(части):
            фильтр = f"setpts=(PTS-STARTPTS){'/' + str(раз) if ускорить else ''}"
            if ускорить:
                фильтр += (f",drawtext=fontfile={ШРИФТ}:textfile={надпись}:x=w-tw-28:y=28:fontsize=28:"
                           "fontcolor=white:box=1:boxcolor=0x0f172aD9:boxborderw=12")
            кусок = os.path.join(каталог, f"{i:02d}.mp4")
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", вход,
                            "-vf", фильтр + f",fps={FPS}", "-an", "-c:v", "libx264", "-preset", "veryfast",
                            "-crf", "26", "-pix_fmt", "yuv420p", кусок], check=True)
            куски.append(кусок)
        список = os.path.join(каталог, "список.txt")
        with open(список, "w", encoding="utf-8") as f:
            f.writelines(f"file '{к}'\n" for к in куски)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", список,
                        "-c", "copy", "-movflags", "+faststart", выход], check=True)
    finally:
        shutil.rmtree(каталог, ignore_errors=True)


def main() -> int:
    разбор = argparse.ArgumentParser(description="Ускоренная копия записи показа: ожидание модели ×N.")
    разбор.add_argument("запись")
    разбор.add_argument("метки")
    разбор.add_argument("старт", type=float, nargs="?", default=None,
                        help="время запуска записи, мс эпохи (по умолчанию — «старт_записи» из меток)")
    разбор.add_argument("--раз", type=int, default=8)
    аргументы = разбор.parse_args()
    with open(аргументы.метки, encoding="utf-8") as f:
        данные = json.load(f)
    метки = данные.get("ожидания") or []
    старт = аргументы.старт if аргументы.старт is not None else данные.get("старт_записи")
    if старт is None:
        print("Нет времени начала записи: ни аргумента, ни «старт_записи» в метках.", file=sys.stderr)
        return 2
    всего = длительность(аргументы.запись)
    части = отрезки(метки, float(старт), всего)
    if not any(у for _, _, у in части):
        print("Ожиданий длиннее 10 с в метках нет — ускорять нечего.")
        return 1
    выход = аргументы.запись[:-4] + "-ускорено.mp4"
    собрать(аргументы.запись, выход, части, аргументы.раз)
    стало = длительность(выход)
    for a, b, у in части:
        print(f"  {a:7.1f}–{b:7.1f} с {'×' + str(аргументы.раз) if у else 'как есть'}")
    print(f"Ускоренная копия: {выход} — {стало / 60:.1f} мин вместо {всего / 60:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
