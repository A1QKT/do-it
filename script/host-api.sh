#!/usr/bin/env bash
# FastAPI: uvicorn api.main:app
#   Foreground (dev): ./script/host-api.sh          → --reload
#   Background:       ./script/host-api.sh --background   → nohup, api.log, api.pid (no --reload; add it after -b if you want)
#   Env: PORT=8080  API_BACKGROUND=1 ./script/host-api.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="python3"
if [[ -x "$ROOT/.venv/bin/python3" ]]; then
  PYTHON="$ROOT/.venv/bin/python3"
elif [[ -x "$ROOT/venv/bin/python3" ]]; then
  PYTHON="$ROOT/venv/bin/python3"
fi

export PYTHONPATH="${PYTHONPATH:-}:$ROOT"

PORT="${PORT:-8000}"

_background=0
case "${1:-}" in
  --background | -b)
    _background=1
    shift
    ;;
esac
[[ "${API_BACKGROUND:-}" == "1" ]] && _background=1

if [[ "$_background" == "1" ]]; then
  LOG="${ROOT}/api.log"
  PIDF="${ROOT}/api.pid"
  nohup "$PYTHON" -m uvicorn api.main:app --host 0.0.0.0 --port "$PORT" "$@" >>"$LOG" 2>&1 &
  echo $! >"$PIDF"
  echo "host-api.sh: background pid=$(cat "$PIDF") log=$LOG port=$PORT" >&2
  echo "  stop (repo root): kill \"\$(cat api.pid)\"" >&2
  exit 0
fi

exec "$PYTHON" -m uvicorn api.main:app --reload --host 0.0.0.0 --port "$PORT" "$@"
