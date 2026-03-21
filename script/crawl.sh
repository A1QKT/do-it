#!/usr/bin/env bash
# Media seed crawl → media_seed_export.json (scripts/media_seed_crawl.py).
#   Foreground: ./script/crawl.sh [--once] …
#   Background: ./script/crawl.sh --background [--once] …  → nohup, crawl.log, crawl.pid
#   Env: CRAWL_BACKGROUND=1 ./script/crawl.sh
# Default loop: CRAWL_LOOP_FOREVER in media_seed_crawl.py. One shot: --once
# Deps: pip install -r requirements-crawl.txt && python -m playwright install chromium
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="python3"
if [[ -x "$ROOT/.venv/bin/python3" ]]; then
  PYTHON="$ROOT/.venv/bin/python3"
elif [[ -x "$ROOT/venv/bin/python3" ]]; then
  PYTHON="$ROOT/venv/bin/python3"
fi

if [[ "${CRAWL_NO_BOOTSTRAP:-}" != "1" ]]; then
  if ! "$PYTHON" -c "import httpx" 2>/dev/null; then
    if [[ "$PYTHON" == "$ROOT/.venv/bin/python3" || "$PYTHON" == "$ROOT/venv/bin/python3" ]]; then
      echo "crawl.sh: installing dependencies (requirements-crawl.txt) into venv…" >&2
      "$PYTHON" -m pip install -r "$ROOT/requirements-crawl.txt"
    fi
  fi
fi

if ! "$PYTHON" -c "import httpx" 2>/dev/null; then
  echo "crawl.sh: httpx not available for: $PYTHON" >&2
  echo "  Create a venv at repo root: python3 -m venv .venv" >&2
  echo "  Then run ./script/crawl.sh again (it will pip install -r requirements-crawl.txt)." >&2
  exit 1
fi

if [[ "${CRAWL_NO_PLAYWRIGHT_INSTALL:-}" != "1" ]]; then
  if "$PYTHON" -c "import playwright" 2>/dev/null; then
    echo "crawl.sh: ensuring Playwright Chromium is installed…" >&2
    "$PYTHON" -m playwright install chromium
  fi
fi

export PYTHONPATH="${PYTHONPATH:-}:$ROOT"

_background=0
case "${1:-}" in
  --background | -b)
    _background=1
    shift
    ;;
esac
[[ "${CRAWL_BACKGROUND:-}" == "1" ]] && _background=1

if [[ "$_background" == "1" ]]; then
  LOG="${ROOT}/crawl.log"
  PIDF="${ROOT}/crawl.pid"
  nohup "$PYTHON" "$ROOT/scripts/media_seed_crawl.py" "$@" >>"$LOG" 2>&1 &
  echo $! >"$PIDF"
  echo "crawl.sh: background pid=$(cat "$PIDF") log=$LOG" >&2
  echo "  stop (repo root): kill \"\$(cat crawl.pid)\"" >&2
  exit 0
fi

exec "$PYTHON" "$ROOT/scripts/media_seed_crawl.py" "$@"
