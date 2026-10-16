#!/usr/bin/env bash
# Показ Telegram-бота Дня 30 (дополнение, telegram-plan § 6, D-T14): Telegram Web
# владельца в отдельном профиле Chrome, чат с ботом в одну колонку (окно 880 —
# список чатов не виден), запись окна и шесть сцен сценария DEMO-CHROME.md. После
# записи — ускоренная копия: ожидание модели ×8 (demo_speedup.py).
#
# Профиль с сеансом Telegram Web — memory/telegram-web (0700, вне репозитория):
# вход по QR делает владелец один раз (--вход). Скрипт пишет только в чат с ботом
# и перед каждой отправкой сверяет заголовок чата. Профиль не удаляется — его
# удаляет владелец, а сеанс завершает в Telegram → Настройки → Устройства.
#
# Ключ показа «показ-админ» (для вкладки /admin) создаётся перед показом и
# отзывается после; вводится как пароль. Менеджер паролей профиля выключен —
# окно «Сохранить пароль?» закрывало /admin в кадре (первая запись Дня 30).
#
# Имена переменных латиницей: bash не понимает кириллицу в идентификаторах.
#
# Запуск:
#   bash demo_telegram.sh --вход        # один раз: открыть Telegram Web для входа по QR
#   bash demo_telegram.sh               # Chrome + запись + показ (службы day31-llm и day31-tg работают)
#   bash demo_telegram.sh --no-record   # без записи
#   bash demo_telegram.sh --pause 4000  # длиннее паузы между сценами
#   MODE=телеграм32 bash demo_telegram.sh   # День 32: только показать уведомление AI-ревью (ничего не отправляет)
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
PROFILE="$ROOT/memory/telegram-web"
SCRATCH="${TMPDIR:-/tmp}/day30-телеграм-$$"
PAUSE=3000
RECORD=1
LOGIN=0
DEBUG_PORT="$("${PY}" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-record|--без-записи) RECORD=0 ;;
    --pause|--пауза) PAUSE="$2"; shift ;;
    --вход|--login) LOGIN=1 ;;
    -h|--help|--помощь) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Не знаю ключа «$1». Список: --вход, --no-record, --pause"; exit 1 ;;
  esac
  shift
done

command -v google-chrome >/dev/null 2>&1 || { echo "Не нашёл google-chrome."; exit 1; }
command -v node >/dev/null 2>&1 || { echo "Нет node — им запускается browser.mjs"; exit 1; }
if pgrep -f "user-data-dir=$PROFILE" >/dev/null; then
  echo "Профиль $PROFILE уже открыт в Chrome — закройте то окно и повторите."; exit 1
fi
mkdir -p "$PROFILE/Default" && chmod 700 "$PROFILE"

# Менеджер паролей профиля — выключен (Chrome закрыт, файл можно править).
"$PY" - "$PROFILE/Default/Preferences" <<'PYCODE'
import json, sys
путь = sys.argv[1]
try:
    данные = json.load(open(путь, encoding="utf-8"))
except (OSError, ValueError):
    данные = {}
данные["credentials_enable_service"] = False
данные.setdefault("profile", {})["password_manager_enabled"] = False
# Прошлый Chrome мог быть погашен сигналом: без этого он весь показ держит окно «Восстановить
# страницы?» поверх чата (седьмая запись бота — 121 кадр из 122).
данные["profile"]["exit_type"] = "Normal"
данные["profile"]["exited_cleanly"] = True
json.dump(данные, open(путь, "w", encoding="utf-8"))
PYCODE

if [ "$LOGIN" = 1 ]; then
  google-chrome --new-window --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check \
    --window-size=880,980 --window-position=40,40 "https://web.telegram.org/k/" >/dev/null 2>&1 &
  echo "Открыл Telegram Web: войдите по QR (Telegram → Настройки → Устройства → Подключить устройство),"
  echo "затем закройте окно. Профиль: $PROFILE"
  exit 0
fi

KEYS=(показ-админ:админ)
# День 32 (MODE=телеграм32): только показать уведомление AI-ревью — службы, ключи и /admin не нужны.
[ "${MODE:-}" = "телеграм32" ] && KEYS=()

cleanup() {
  [ -n "${REC_PID:-}" ] && kill -TERM "$REC_PID" 2>/dev/null && wait "$REC_PID" 2>/dev/null
  [ -n "${CHROME_PID:-}" ] && kill "$CHROME_PID" 2>/dev/null
  pkill -f "user-data-dir=$ROOT/memory/telegram-we[b]" 2>/dev/null
  for k in "${KEYS[@]}"; do "$PY" "$ROOT/cli.py" --сервис-отозвать "${k%%:*}" >/dev/null 2>&1; done
  rm -rf "$SCRATCH"
}
trap cleanup EXIT INT TERM

if [ "${MODE:-}" = "телеграм32" ]; then
echo "1/5 Бот (только имя — по токену из .env, токен не печатается)…"
BOT="$("$PY" - "$ROOT/.env" <<'PYCODE'
import sys, httpx
from dotenv import dotenv_values
т = dotenv_values(sys.argv[1]).get("TELEGRAM_BOT_TOKEN", "")
я = httpx.post(f"https://api.telegram.org/bot{т}/getMe", timeout=30).json()["result"]
print(f"{я['username']}\t{я['first_name']}")
PYCODE
)" || { echo "Bot API не ответил (VPN включён?)"; exit 1; }
BOT_USERNAME="${BOT%%$'\t'*}"; BOT_TITLE="${BOT#*$'\t'}"
URL=""; SPKI=""
mkdir -p "$SCRATCH" && chmod 700 "$SCRATCH"
printf '{}' > "$SCRATCH/keys.json"
else
echo "1/5 Службы, бот и ключ показа…"
"$PY" "$ROOT/cli.py" --сервис статус | grep -q "active (running)" || {
  echo "Служба day31-llm не работает: python cli.py --сервис старт"; exit 1; }
"$PY" "$ROOT/cli.py" --телеграм статус | grep -q "Служба day31-tg: active (running)" || {
  echo "Бот не работает: python cli.py --телеграм старт"; exit 1; }
BOT="$("$PY" - <<'PYCODE'
import sys, httpx
sys.path.insert(0, ".")
from agent.service import manage
с = manage.секреты_бота()
я = httpx.post(f"https://api.telegram.org/bot{с['TELEGRAM_BOT_TOKEN']}/getMe", timeout=30).json()["result"]
print(f"{я['username']}\t{я['first_name']}")
PYCODE
)" || { echo "Bot API не ответил (VPN включён?)"; exit 1; }
BOT_USERNAME="${BOT%%$'\t'*}"; BOT_TITLE="${BOT#*$'\t'}"
ADDR="$("$PY" -c 'import sys; sys.path.insert(0, "'"$ROOT"'"); from agent.service import certs; print(certs.адрес_в_сети())')"
PORT="$("$PY" -c 'import json; print(json.load(open("'"$ROOT"'/service.json"))["порт"])')"
URL="https://$ADDR:$PORT"
mkdir -p "$SCRATCH" && chmod 700 "$SCRATCH"
"$PY" "$ROOT/cli.py" --сервис-отозвать показ-админ >/dev/null 2>&1
KEY="$("$PY" "$ROOT/cli.py" --сервис-ключ показ-админ --права админ | grep -o 'sk-day30-[A-Za-z0-9_-]*')"
printf '{"админ": "%s"}' "$KEY" > "$SCRATCH/keys.json"
chmod 600 "$SCRATCH/keys.json"
SPKI="$(openssl x509 -in "$ROOT/memory/service/tls/server.crt" -pubkey -noout | openssl pkey -pubin -outform der \
  | openssl dgst -sha256 -binary | base64)"
fi

echo "2/5 Chrome с профилем Telegram Web (окно 880 — одна колонка, только чат с @$BOT_USERNAME)…"
google-chrome --new-window \
  --remote-debugging-port="$DEBUG_PORT" \
  --user-data-dir="$PROFILE" \
  ${SPKI:+--ignore-certificate-errors-spki-list="$SPKI"} \
  --no-first-run --no-default-browser-check --hide-crash-restore-bubble \
  --window-size=880,980 --window-position=40,40 \
  "about:blank" > "$SCRATCH/chrome.log" 2>&1 &
CHROME_PID=$!
for _ in $(seq 1 40); do
  curl -s -o /dev/null "http://127.0.0.1:$DEBUG_PORT/json/version" && break
  sleep 0.5
done
curl -s -o /dev/null "http://127.0.0.1:$DEBUG_PORT/json/version" || { echo "Chrome не отдал порт отладки"; exit 1; }
sleep 2

# Водитель сначала открывает чат с ботом и скрывает список чатов владельца (файл READY), и
# только потом начинается запись (файл RECORDING): в первой записи бота список чатов — имена
# и превью личных переписок — попал в кадр, пока Telegram Web загружался.
echo "3/5 Открываю чат с ботом и скрываю список чатов (до записи)…"
# CDP_TIMEOUT: вызовы показа короткие — подвисшая под нагрузкой страница не должна вешать показ.
# День 31: запись — через CDP (Page.startScreencast, RECORD_CDP): кадры отдаёт сам Chrome, и
# водитель начинает её только после того, как список чатов скрыт. Захват области экрана
# (x11grab) в показе Дня 31 поймал окно редактора под ещё не поднятым Chrome.
FILE=""
if [ "$RECORD" = 1 ]; then
  mkdir -p "$ROOT/demo"
  FILE="$ROOT/demo/${MODE:-телеграм31}-$(date +%Y%m%d-%H%M).mp4"
fi
CDP="http://127.0.0.1:$DEBUG_PORT" PAUSE="$PAUSE" MARKS="$SCRATCH/marks.json" SERVICE_KEYS="$SCRATCH/keys.json" \
  ADMIN_URL="$URL/admin" BOT_TITLE="$BOT_TITLE" CDP_TIMEOUT=60000 RECORD_CDP="$FILE" \
  READY="$SCRATCH/ready" RECORDING="$SCRATCH/recording" \
  node "$ROOT/browser.mjs" "https://web.telegram.org/k/#@$BOT_USERNAME" ds-flash "" "${MODE:-телеграм31}" &
DRIVER_PID=$!
for _ in $(seq 1 300); do
  [ -f "$SCRATCH/ready" ] && break
  [ -d "/proc/$DRIVER_PID" ] || break
  sleep 0.2
done
if [ ! -f "$SCRATCH/ready" ]; then
  echo "Чат с ботом не открылся или список чатов не скрыт — запись не начата."
  wait "$DRIVER_PID"; exit 1
fi
[ "$RECORD" = 1 ] && echo "   запись страницы через CDP: $FILE" || echo "   запись выключена ключом --no-record"
touch "$SCRATCH/recording"
echo "4/5 Прохожу сцены (ответы ассистента облаком — секунды, /status — сразу)…"
wait "$DRIVER_PID"
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
echo "Показ окончен (код водителя $SHOW_CODE). Ключ показа отозван; профиль Telegram Web — $PROFILE (удаляется по слову владельца)."
