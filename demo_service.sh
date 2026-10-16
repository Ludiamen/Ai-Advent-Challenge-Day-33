#!/usr/bin/env bash
# Показ Дня 30 (приватный сервис; с Дня 31 — регрессия, показ дня — demo_show.sh): живая служба day31-llm, видимый Chrome, запись окна
# и восемь сцен сценария DEMO-CHROME.md. После записи — ускоренная копия:
# ожидание локальной модели ×8 с надписью в кадре (demo_speedup.py), сцены и
# ответы — в обычной скорости. Основная запись — без монтажа.
#
# Ключи показа (админ, чат, документы, лимит) создаются перед показом и
# отзываются после него; в поля страниц они вводятся как пароль. Chrome — со
# своим профилем и доверяет только сертификату сервиса: по отпечатку его ключа
# (--ignore-certificate-errors-spki-list), а не отключением всех проверок.
#
# Имена переменных латиницей: bash не понимает кириллицу в идентификаторах.
#
# Запуск:
#   bash demo_show.sh                 # Chrome + запись + показ (служба должна работать)
#   bash demo_show.sh --no-record     # без записи (посмотреть глазами)
#   bash demo_show.sh --pause 4000    # длиннее паузы между сценами
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
# Каталог прогона — со своим номером процесса: у двух прогонов разные профили Chrome.
SCRATCH="${TMPDIR:-/tmp}/day30-показ-$$"
PAUSE=3000
RECORD=1
# Порт отладки — свободный: с фиксированным водитель мог подключиться к чужому Chrome.
DEBUG_PORT="$("${PY}" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-record|--без-записи) RECORD=0 ;;
    --pause|--пауза) PAUSE="$2"; shift ;;
    -h|--help|--помощь) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Не знаю ключа «$1». Список: --no-record, --pause"; exit 1 ;;
  esac
  shift
done

command -v google-chrome >/dev/null 2>&1 || { echo "Не нашёл google-chrome. Показ водит именно его."; exit 1; }
command -v node >/dev/null 2>&1 || { echo "Нет node — им запускается browser.mjs"; exit 1; }

KEYS=(показ-админ:админ показ-чат:чат показ-документы:документы показ-лимит:чат)

cleanup() {
  # TERM, а не INT: SIGINT в фоновом задании неинтерактивной оболочки игнорируется,
  # и запись оставалась незакрытой — mp4 без moov-атома.
  [ -n "${REC_PID:-}" ] && kill -TERM "$REC_PID" 2>/dev/null && wait "$REC_PID" 2>/dev/null
  [ -n "${CHROME_PID:-}" ] && kill "$CHROME_PID" 2>/dev/null
  pkill -f "user-data-dir=$SCRATCH/chrom[e]" 2>/dev/null
  pkill -INT -f "x11grab.*dem[o]/показ" 2>/dev/null
  # Ключи показа живут только на время показа.
  for k in "${KEYS[@]}"; do "$PY" "$ROOT/cli.py" --сервис-отозвать "${k%%:*}" >/dev/null 2>&1; done
  rm -rf "$SCRATCH"
}
trap cleanup EXIT INT TERM

echo "1/5 Служба и ключи показа…"
"$PY" "$ROOT/cli.py" --сервис статус | grep -q "active (running)" || {
  echo "Служба day31-llm не работает: python cli.py --сервис старт"; exit 1; }
ADDR="$("$PY" -c 'import sys; sys.path.insert(0, "'"$ROOT"'"); from agent.service import certs; print(certs.адрес_в_сети())')"
PORT="$("$PY" -c 'import json; print(json.load(open("'"$ROOT"'/service.json"))["порт"])')"
URL="https://$ADDR:$PORT"
mkdir -p "$SCRATCH" && chmod 700 "$SCRATCH"
{
  echo "{"
  first=1
  for k in "${KEYS[@]}"; do
    name="${k%%:*}"; rights="${k#*:}"
    "$PY" "$ROOT/cli.py" --сервис-отозвать "$name" >/dev/null 2>&1
    key="$("$PY" "$ROOT/cli.py" --сервис-ключ "$name" --права "$rights" | grep -o 'sk-day30-[A-Za-z0-9_-]*')"
    [ "$first" = 1 ] || echo ","
    first=0
    printf '"%s": "%s"' "${name#показ-}" "$key"
  done
  echo "}"
} > "$SCRATCH/keys.json"
chmod 600 "$SCRATCH/keys.json"
SPKI="$(openssl x509 -in "$ROOT/memory/service/tls/server.crt" -pubkey -noout | openssl pkey -pubin -outform der \
  | openssl dgst -sha256 -binary | base64)"

echo "2/5 Видимый Chrome со своим профилем (ваш профиль и вкладки не трогаю)…"
# Профиль показа не предлагает сохранить ключ как пароль: в первой записи Дня 30
# окно «Сохранить пароль?» после ввода ключа закрывало правую часть /admin.
mkdir -p "$SCRATCH/chrome/Default"
printf '%s' '{"credentials_enable_service": false, "profile": {"password_manager_enabled": false}}' \
  > "$SCRATCH/chrome/Default/Preferences"
google-chrome --new-window \
  --remote-debugging-port="$DEBUG_PORT" \
  --user-data-dir="$SCRATCH/chrome" \
  --ignore-certificate-errors-spki-list="$SPKI" \
  --no-first-run --no-default-browser-check \
  --window-size=1600,980 --window-position=40,40 \
  "$URL/admin" > "$SCRATCH/chrome.log" 2>&1 &
CHROME_PID=$!
for _ in $(seq 1 40); do
  curl -s -o /dev/null "http://127.0.0.1:$DEBUG_PORT/json/version" && break
  sleep 0.5
done
if ! curl -s -o /dev/null "http://127.0.0.1:$DEBUG_PORT/json/version"; then
  echo "Chrome не отдал порт отладки $DEBUG_PORT. Смотрите $SCRATCH/chrome.log"; exit 1
fi
sleep 2

if [ "$RECORD" = 1 ]; then
  echo "3/5 Запись экрана…"
  # --pid: пишем именно то окно, которое сами открыли, а не весь экран.
  REC_START_MS="$(date +%s%3N)"
  bash "$ROOT/record_demo.sh" --name показ --pid "$CHROME_PID" > "$SCRATCH/запись.log" 2>&1 &
  REC_PID=$!
  sleep 2
else
  echo "3/5 Запись выключена ключом --no-record"
fi

echo "4/5 Прохожу восемь сцен (15–25 минут: генерации локальной модели на CPU идут по-настоящему)…"
CDP="http://127.0.0.1:$DEBUG_PORT" PAUSE="$PAUSE" MARKS="$SCRATCH/marks.json" SERVICE_KEYS="$SCRATCH/keys.json" \
  node "$ROOT/browser.mjs" "$URL/admin" ds-flash "" показ
SHOW_CODE=$?

if [ "$RECORD" = 1 ]; then
  echo "5/5 Останавливаю запись…"
  kill -TERM "$REC_PID" 2>/dev/null
  wait "$REC_PID" 2>/dev/null
  REC_PID=""
  FILE="$(ls -t "$ROOT/demo"/показ-*.mp4 2>/dev/null | grep -v -- '-ускорено' | head -1)"
  [ -n "$FILE" ] && echo "Запись: $FILE ($(du -h "$FILE" | cut -f1))"
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
echo "Показ окончен (код водителя $SHOW_CODE). Ключи показа отозваны; служба day31-llm работает дальше."
