#!/usr/bin/env bash
# Run transcribe_stream.py in the background (survives terminal close).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

LOG="${TRANSCRIBE_LOG:-$SCRIPT_DIR/transcribe.log}"
PIDFILE="${TRANSCRIBE_PIDFILE:-$SCRIPT_DIR/transcribe.pid}"

usage() {
  cat <<EOF
Usage:
  $(basename "$0") -o FILE [other transcribe_stream.py args...]   # start in background
  $(basename "$0") stop
  $(basename "$0") status

Environment:
  TRANSCRIBE_LOG     log file (default: $SCRIPT_DIR/transcribe.log)
  TRANSCRIBE_PIDFILE pid file (default: $SCRIPT_DIR/transcribe.pid)

Examples:
  $(basename "$0") -o transcript.txt
  $(basename "$0") -o transcript.txt --model tiny --chunk-sec 10
  $(basename "$0") stop
EOF
}

case "${1:-}" in
  -h|--help|"")
    usage
    [[ -n "${1:-}" ]] || exit 1
    exit 0
    ;;
  stop)
    if [[ ! -f "$PIDFILE" ]]; then
      echo "No PID file; nothing to stop."
      exit 1
    fi
    pid=$(cat "$PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      echo "Stopped PID $pid"
    else
      echo "Stale PID $pid (process already gone)"
    fi
    rm -f "$PIDFILE"
    exit 0
    ;;
  status)
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "Running PID $(cat "$PIDFILE")  log: $LOG"
    else
      echo "Not running"
      [[ -f "$PIDFILE" ]] && rm -f "$PIDFILE"
    fi
    exit 0
    ;;
esac

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "Already running (PID $(cat "$PIDFILE")). Stop first: $(basename "$0") stop" >&2
  exit 1
fi

nohup python3 "$SCRIPT_DIR/transcribe_stream.py" "$@" >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"
echo "Started PID $(cat "$PIDFILE")"
echo "  Transcript: use -o path you passed (e.g. transcript.txt)"
echo "  Service log: $LOG"
echo "  Stop:       $(basename "$0") stop"
