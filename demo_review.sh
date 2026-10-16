#!/usr/bin/env bash
# Показ Дня 32 одной командой: автоматическое AI-ревью PR на GitHub (DEMO-CHROME.md, «показ32»).
# Chrome со своим профилем (headless), публичный просмотр GitHub без входа в аккаунт, запись
# СТРАНИЦЫ кадрами самого Chrome (CDP Page.startScreencast) — в запись не попадает ничего,
# кроме страницы. Сцены: PR с настоящей доработкой (🟢), прогон Actions, PR с посеянными
# ошибками (🔴), исправляющий коммит — Action перезапускается, тот же комментарий 🟢.
# Исправление отправляет FIX_CMD этого скрипта: git push ветки PR токеном GITHUB_PAT через
# GIT_ASKPASS (out/askpass.sh) — токен не в аргументах и не в журнале. После записи —
# ускоренная копия (ожидание Action ×8, demo_speedup.py).
#
# Имена переменных латиницей: bash не понимает кириллицу в идентификаторах.
#
# Запуск:
#   bash demo_review.sh --pr-clean 2 --pr-seeded 3 --fix-branch demo/seeded --fix-dir out/wt-seeded --fix-commit SHA
#   bash demo_review.sh ... --no-record      # без записи
#   bash demo_review.sh ... --no-fix         # без сцены исправления
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
SCRATCH="${TMPDIR:-/tmp}/day32-показ-$$"
PAUSE=3000
RECORD=1
FIX=1
REPO="${GH_REPO:-Ludiamen/Ai-Advent-Challenge-Day-32}"
PR_CLEAN=""
PR_SEEDED=""
FIX_BRANCH=""
FIX_DIR=""
FIX_COMMIT=""
DEBUG_PORT="$("${PY}" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-record|--без-записи) RECORD=0 ;;
    --no-fix|--без-исправления) FIX=0 ;;
    --pause|--пауза) PAUSE="$2"; shift ;;
    --pr-clean) PR_CLEAN="$2"; shift ;;
    --pr-seeded) PR_SEEDED="$2"; shift ;;
    --fix-branch) FIX_BRANCH="$2"; shift ;;
    --fix-dir) FIX_DIR="$2"; shift ;;
    --fix-commit) FIX_COMMIT="$2"; shift ;;
    -h|--help|--помощь) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Не знаю ключа «$1»."; exit 1 ;;
  esac
  shift
done
[ -n "$PR_CLEAN" ] && [ -n "$PR_SEEDED" ] || { echo "Нужны --pr-clean N и --pr-seeded N"; exit 1; }
command -v google-chrome >/dev/null 2>&1 || { echo "Не нашёл google-chrome"; exit 1; }
command -v node >/dev/null 2>&1 || { echo "Нет node — им запускается browser.mjs"; exit 1; }

FIX_CMD=""
if [ "$FIX" = 1 ]; then
  [ -n "$FIX_BRANCH" ] && [ -d "$FIX_DIR" ] || { echo "Для сцены исправления нужны --fix-branch и --fix-dir (или --no-fix)"; exit 1; }
  # Исправление — готовый коммит (--fix-commit: настоящие правки по ревью) или, без него, откат
  # посеянного коммита. Откат оставляет PR без изменений — ревьюить нечего, и «зелёный» ничего
  # не доказывает (первая запись Дня 32), поэтому для показа нужен --fix-commit.
  if [ -n "$FIX_COMMIT" ]; then
    APPLY="git cherry-pick $FIX_COMMIT >/dev/null"
  else
    APPLY="git revert --no-edit HEAD >/dev/null"
  fi
  FIX_CMD="cd '$FIX_DIR' && $APPLY && GIT_TERMINAL_PROMPT=0 GIT_ASKPASS='$ROOT/out/askpass.sh' git push -q origin HEAD:refs/heads/$FIX_BRANCH"
fi

cleanup() {
  [ -n "${CHROME_PID:-}" ] && kill "$CHROME_PID" 2>/dev/null && wait "$CHROME_PID" 2>/dev/null
  sleep 1      # Chrome дописывает профиль после сигнала — иначе rm оставляет каталог (первый дубль)
  rm -rf "${SCRATCH:?}"
}
trap cleanup EXIT INT TERM

echo "1/4 Chrome со своим профилем, без окна на экране (ваш профиль и вкладки не трогаю)…"
mkdir -p "$SCRATCH/chrome/Default" && chmod 700 "$SCRATCH"
printf '%s' '{"credentials_enable_service": false, "profile": {"password_manager_enabled": false, "exit_type": "Normal", "exited_cleanly": true}}' \
  > "$SCRATCH/chrome/Default/Preferences"
google-chrome --headless=new --remote-debugging-port="$DEBUG_PORT" --user-data-dir="$SCRATCH/chrome" \
  --no-first-run --no-default-browser-check --hide-crash-restore-bubble --lang=ru \
  --window-size=1600,980 "about:blank" > "$SCRATCH/chrome.log" 2>&1 &
CHROME_PID=$!
for _ in $(seq 1 40); do curl -s -o /dev/null "http://127.0.0.1:$DEBUG_PORT/json/version" && break; sleep 0.5; done
curl -s -o /dev/null "http://127.0.0.1:$DEBUG_PORT/json/version" || { echo "Chrome не отдал порт отладки"; exit 1; }

FILE=""
if [ "$RECORD" = 1 ]; then
  mkdir -p "$ROOT/demo"
  FILE="$ROOT/demo/ревью-github-$(date +%Y%m%d-%H%M).mp4"
  echo "2/4 Запись страницы через CDP: $FILE"
else
  echo "2/4 Запись выключена"
fi

# Токен для API GitHub (лимит анонимных запросов — 60 в час) — в окружение процесса водителя, не в аргументы.
GH_API_TOKEN="$("$PY" -c 'import sys; from dotenv import dotenv_values; print(dotenv_values(sys.argv[1]).get("GITHUB_PAT", ""))' "$ROOT/.env")"
export GH_API_TOKEN
echo "3/4 Сцены показа (исправление ждёт прогон Action — ≈ 1–2 мин)…"
CDP="http://127.0.0.1:$DEBUG_PORT" PAUSE="$PAUSE" MARKS="$SCRATCH/marks.json" RECORD_CDP="$FILE" \
  GH_REPO="$REPO" PR_CLEAN="$PR_CLEAN" PR_SEEDED="$PR_SEEDED" FIX_CMD="$FIX_CMD" \
  node "$ROOT/browser.mjs" "https://github.com/$REPO/pull/$PR_CLEAN" ds-flash "" показ32
SHOW_CODE=$?

if [ "$RECORD" = 1 ] && [ -f "$FILE" ]; then
  REC_START_MS="$(cat "$FILE.start" 2>/dev/null || echo 0)"
  rm -f "$FILE.start"
  echo "4/4 Запись: $FILE ($(du -h "$FILE" | cut -f1))"
  if [ -f "$SCRATCH/marks.json" ]; then
    "$PY" - "$SCRATCH/marks.json" "$REC_START_MS" "${FILE%.mp4}-метки.json" <<'PYCODE'
import json, sys
данные = json.load(open(sys.argv[1], encoding="utf-8"))
данные["старт_записи"] = int(sys.argv[2])
json.dump(данные, open(sys.argv[3], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
PYCODE
    "$PY" "$ROOT/demo_speedup.py" "$FILE" "${FILE%.mp4}-метки.json" --раз 8 | tail -1
  fi
else
  echo "4/4 Готово"
fi
echo "Показ окончен (код водителя $SHOW_CODE)."
