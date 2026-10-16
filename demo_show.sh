#!/usr/bin/env bash
# Показ Дня 31 одной командой: ассистент разработчика — /help на странице мини-чата агента.
# Стенд (demo_stand.sh, страница на 5001), Chrome со своим профилем (headless), девять сцен
# сценария DEMO-CHROME.md (режим «показ31» в browser.mjs) и запись СТРАНИЦЫ: кадры отдаёт сам
# Chrome через CDP (Page.startScreencast), поэтому в запись не может попасть ничего, кроме
# страницы. Захват области экрана (record_demo.sh, x11grab) в первую секунду дубля 3 поймал
# окно редактора под ещё не поднятым Chrome — с Дня 31 показ так не пишется. После записи —
# ускоренная копия (ожидание модели ×8 с надписью в кадре, demo_speedup.py).
#
# Проект — CRM владельца, только чтение: индекс её документации (python cli.py
# --проект-индекс) и git-сервер MCP. Показ CRM не меняет.
#
# Имена переменных латиницей: bash не понимает кириллицу в идентификаторах.
#
# Запуск:
#   bash demo_show.sh                 # стенд + Chrome + запись + показ
#   bash demo_show.sh --no-record     # без записи (посмотреть глазами)
#   bash demo_show.sh --pause 4000    # длиннее паузы между сценами
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
SCRATCH="${TMPDIR:-/tmp}/day31-показ-$$"
PAUSE=3000
RECORD=1
URL="http://127.0.0.1:5001"
DEBUG_PORT="$("${PY}" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-record|--без-записи) RECORD=0 ;;
    --pause|--пауза) PAUSE="$2"; shift ;;
    -h|--help|--помощь) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Не знаю ключа «$1». Список: --no-record, --pause"; exit 1 ;;
  esac
  shift
done

command -v google-chrome >/dev/null 2>&1 || { echo "Не нашёл google-chrome. Показ водит именно его."; exit 1; }
command -v node >/dev/null 2>&1 || { echo "Нет node — им запускается browser.mjs"; exit 1; }
[ -f "$ROOT/index-projects/crm/docs.db" ] || { echo "Нет индекса проекта: python cli.py --проект-индекс"; exit 1; }

STAND_STARTED=0
cleanup() {
  [ -n "${CHROME_PID:-}" ] && kill "$CHROME_PID" 2>/dev/null
  [ "$STAND_STARTED" = 1 ] && bash "$ROOT/demo_stand.sh" --stop >/dev/null 2>&1
  rm -rf "${SCRATCH:?}"
}
trap cleanup EXIT INT TERM

echo "1/5 Стенд на 5001…"
if ! curl -s -o /dev/null "$URL/chat"; then
  bash "$ROOT/demo_stand.sh" | grep -E "готов|Порт" || { echo "Стенд не поднялся"; exit 1; }
  STAND_STARTED=1
fi

echo "2/5 Chrome со своим профилем, без окна на экране (ваш профиль и вкладки не трогаю)…"
mkdir -p "$SCRATCH/chrome/Default" && chmod 700 "$SCRATCH"
# Без менеджера паролей и с «нормальным выходом»: окна «Сохранить пароль?» и «Восстановить
# страницы?» закрывали кадр (Дни 30–31).
printf '%s' '{"credentials_enable_service": false, "profile": {"password_manager_enabled": false, "exit_type": "Normal", "exited_cleanly": true}}' \
  > "$SCRATCH/chrome/Default/Preferences"
google-chrome --headless=new \
  --remote-debugging-port="$DEBUG_PORT" \
  --user-data-dir="$SCRATCH/chrome" \
  --no-first-run --no-default-browser-check --hide-crash-restore-bubble \
  --window-size=1600,980 --window-position=40,40 \
  "about:blank" > "$SCRATCH/chrome.log" 2>&1 &
CHROME_PID=$!
for _ in $(seq 1 40); do
  curl -s -o /dev/null "http://127.0.0.1:$DEBUG_PORT/json/version" && break
  sleep 0.5
done
curl -s -o /dev/null "http://127.0.0.1:$DEBUG_PORT/json/version" || { echo "Chrome не отдал порт отладки"; exit 1; }
sleep 2

FILE=""
if [ "$RECORD" = 1 ]; then
  mkdir -p "$ROOT/demo"
  FILE="$ROOT/demo/ассистент-$(date +%Y%m%d-%H%M).mp4"
  echo "3/5 Запись страницы через CDP: $FILE"
else
  echo "3/5 Запись выключена ключом --no-record"
fi

echo "4/5 Девять сцен (≈ 4–6 минут: ответы облачной модели — секунды)…"
CDP="http://127.0.0.1:$DEBUG_PORT" PAUSE="$PAUSE" MARKS="$SCRATCH/marks.json" RECORD_CDP="$FILE" \
  node "$ROOT/browser.mjs" "$URL" ds-flash "" показ31
SHOW_CODE=$?

if [ "$RECORD" = 1 ]; then
  echo "5/5 Запись готова"
  REC_START_MS="$(cat "$FILE.start" 2>/dev/null || echo 0)"
  rm -f "$FILE.start"
  [ -f "$FILE" ] && echo "Запись: $FILE ($(du -h "$FILE" | cut -f1))"
  if [ -n "$FILE" ] && [ -f "$SCRATCH/marks.json" ]; then
    echo "   ускоренная копия (ожидание модели ×8)…"
    "$PY" - "$SCRATCH/marks.json" "$REC_START_MS" "${FILE%.mp4}-метки.json" <<'PYCODE'
import json, sys
данные = json.load(open(sys.argv[1], encoding="utf-8"))
данные["старт_записи"] = int(sys.argv[2])
json.dump(данные, open(sys.argv[3], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
PYCODE
    "$PY" "$ROOT/demo_speedup.py" "$FILE" "${FILE%.mp4}-метки.json" --раз 8 | tail -1
  fi
else
  echo "5/5 Готово"
fi

echo
echo "Показ окончен (код водителя $SHOW_CODE)."
