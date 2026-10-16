#!/usr/bin/env bash
# Показ и проверка Дня 33 одной командой: мини-сервис поддержки (DEMO-CHROME.md, «показ33»).
# Поднимает сервис на ВРЕМЕННЫХ данных (seed, свой MEMORY_DIR — ваша память не трогается) на
# свободном порту 127.0.0.1 без TLS, выдаёт ключи u01, u03, u06, u07 и сотрудника, запускает
# Chrome со своим профилем (headless) и водителя browser.mjs. Запись — кадрами самого Chrome
# (CDP Page.startScreencast): в неё попадает только страница. Ключи — в окружении водителя, не в
# аргументах и не в журнале; после показа временные данные и ключи удаляются.
#
# Сцены: «Почему не работает авторизация?» от трёх сотрудников — три разные причины по данным
# (блокировка, истёкший пароль, просроченное приглашение → заявка); попытка обойти правила;
# пульт — очередь, карточка, подтверждение заявки; у пользователя — сообщение и оценка 👍.
#
# Имена переменных латиницей: bash не понимает кириллицу в идентификаторах.
#
# Запуск:
#   bash demo_support.sh                 # показ с записью в demo/
#   bash demo_support.sh --no-record     # показ без записи
#   bash demo_support.sh --check         # проверка: режим «поддержка», без подписей и пауз
#   bash demo_support.sh --url https://93.88.203.91:8450 [--ssh day33-vps]
#                                        # то же на развёрнутом сервисе (DEPLOY.md): данные там
#                                        # сбрасываются к seed, ключи показа выдаются в контейнере
#                                        # по ssh и отзываются после показа
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/day33-показ-XXXX")"
PAUSE=3000
RECORD=1
MODE="показ33"
URL=""
SSH_HOST="day33-vps"
free_port() { "$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'; }
DEBUG_PORT="$(free_port)"
APP_PORT="$(free_port)"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-record|--без-записи) RECORD=0 ;;
    --check|--проверка) MODE="поддержка"; RECORD=0 ;;
    --pause|--пауза) PAUSE="$2"; shift ;;
    --url|--адрес) URL="${2%/}"; shift ;;
    --ssh) SSH_HOST="$2"; shift ;;
    -h|--help|--помощь) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Не знаю ключа «$1»."; exit 1 ;;
  esac
  shift
done
command -v google-chrome >/dev/null 2>&1 || { echo "Не нашёл google-chrome"; exit 1; }
command -v node >/dev/null 2>&1 || { echo "Нет node — им запускается browser.mjs"; exit 1; }
[ -n "$URL" ] || [ -f "$ROOT/index-support/chunks.json" ] || { echo "Нет базы знаний: .venv/bin/python cli.py --поддержка-индекс"; exit 1; }
# Python в контейнере на VPS: ключи показа (создать / отозвать) и сброс данных.
remote_py() { ssh "$SSH_HOST" "cd /opt/support && docker compose exec -T support python -" ; }
KEYS_PY='import json
from agent.support import access
к = access.КлючиПоддержки()
print(json.dumps({**{у: к.создать("пользователь", у) for у in ("u01", "u03", "u06", "u07")},
                  "staff": к.создать("сотрудник", "показ")}))'

cleanup() {
  if [ -n "$URL" ] && [ -n "${SUPPORT_KEYS:-}" ]; then
    printf '%s\n' 'from agent.support import access' 'к = access.КлючиПоддержки()' \
      '[к.отозвать("пользователь", у) for у in ("u01", "u03", "u06", "u07")]' 'к.отозвать("сотрудник", "показ")' \
      | remote_py >/dev/null 2>&1 && echo "Ключи показа на сервисе отозваны."
  fi
  [ -n "${CHROME_PID:-}" ] && kill "$CHROME_PID" 2>/dev/null && wait "$CHROME_PID" 2>/dev/null
  [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null && wait "$APP_PID" 2>/dev/null
  sleep 1      # Chrome дописывает профиль после сигнала — иначе rm оставляет каталог
  rm -rf "${SCRATCH:?}"
}
trap cleanup EXIT INT TERM

if [ -n "$URL" ]; then
  echo "1/4 Развёрнутый сервис: $URL (данные — к seed, ключи показа — по ssh $SSH_HOST)"
  curl -sk -o /dev/null "$URL/api/support/health" || { echo "Сервис не отвечает: $URL"; exit 1; }
  ssh "$SSH_HOST" 'cd /opt/support && docker compose exec -T support python support_app.py --сброс' | tail -1
  SUPPORT_KEYS="$(printf '%s\n' "$KEYS_PY" | remote_py)"
  [ -n "$SUPPORT_KEYS" ] || { echo "Не выдал ключи в контейнере"; exit 1; }
  export SUPPORT_KEYS
  START="$URL/support"
  CHROME_EXTRA="--ignore-certificate-errors"     # сертификат своего центра (DEPLOY.md, шаг 4)
else
  export MEMORY_DIR="$SCRATCH/memory"
  unset SUPPORT_DATA
  echo "1/4 Сервис на временных данных: http://127.0.0.1:$APP_PORT"
  (cd "$ROOT" && "$PY" support_app.py --сброс) | tail -1
  SUPPORT_KEYS="$(cd "$ROOT" && printf '%s\n' "$KEYS_PY" | "$PY" -)"
  export SUPPORT_KEYS
  (cd "$ROOT" && exec "$PY" support_app.py --http --адрес 127.0.0.1 --порт "$APP_PORT") > "$SCRATCH/app.log" 2>&1 &
  APP_PID=$!
  for _ in $(seq 1 60); do curl -s -o /dev/null "http://127.0.0.1:$APP_PORT/api/support/health" && break; sleep 0.5; done
  curl -s -o /dev/null "http://127.0.0.1:$APP_PORT/api/support/health" || { echo "Сервис не поднялся:"; tail -5 "$SCRATCH/app.log"; exit 1; }
  START="http://127.0.0.1:$APP_PORT/support"
  CHROME_EXTRA=""
fi

echo "2/4 Chrome со своим профилем, без окна на экране (ваш профиль и вкладки не трогаю)…"
mkdir -p "$SCRATCH/chrome/Default"
printf '%s' '{"credentials_enable_service": false, "profile": {"password_manager_enabled": false, "exit_type": "Normal", "exited_cleanly": true}}' \
  > "$SCRATCH/chrome/Default/Preferences"
google-chrome --headless=new --remote-debugging-port="$DEBUG_PORT" --user-data-dir="$SCRATCH/chrome" \
  --no-first-run --no-default-browser-check --hide-crash-restore-bubble --lang=ru \
  --window-size=1600,980 $CHROME_EXTRA "about:blank" > "$SCRATCH/chrome.log" 2>&1 &
CHROME_PID=$!
for _ in $(seq 1 40); do curl -s -o /dev/null "http://127.0.0.1:$DEBUG_PORT/json/version" && break; sleep 0.5; done
curl -s -o /dev/null "http://127.0.0.1:$DEBUG_PORT/json/version" || { echo "Chrome не отдал порт отладки"; exit 1; }

FILE=""
if [ "$RECORD" = 1 ]; then
  mkdir -p "$ROOT/demo"
  FILE="$ROOT/demo/поддержка-$(date +%Y%m%d-%H%M).mp4"
  echo "3/4 Запись страницы через CDP: $FILE"
else
  echo "3/4 Запись выключена"
fi
CDP="http://127.0.0.1:$DEBUG_PORT" PAUSE="$PAUSE" MARKS="$SCRATCH/marks.json" RECORD_CDP="$FILE" \
  node "$ROOT/browser.mjs" "$START" ds-flash "" "$MODE"
SHOW_CODE=$?

if [ "$RECORD" = 1 ] && [ -f "$FILE" ]; then
  rm -f "$FILE.start"
  echo "4/4 Запись: $FILE ($(du -h "$FILE" | cut -f1))"
else
  echo "4/4 Готово"
fi
echo "Показ окончен (код водителя $SHOW_CODE)."
exit "$SHOW_CODE"
