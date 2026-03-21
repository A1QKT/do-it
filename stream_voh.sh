#!/usr/bin/env bash
# VOH / other HLS radio streams via ffmpeg (see radio_hls package).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

DEFAULT_URL="https://strm.voh.com.vn/radio/channel5/playlist.m3u8"

usage() {
  cat <<EOF
Usage: $(basename "$0") <command> [options]

  metadata              ffprobe tags once
  play                  ffplay / mpv
  record -o FILE        save stream (-c copy); optional -t SECONDS
  watch-metadata        poll ffprobe for tag changes

Default URL: $DEFAULT_URL
Override:    --url 'https://.../playlist.m3u8'

Examples:
  $(basename "$0") metadata
  $(basename "$0") play
  $(basename "$0") record -o voh.aac -t 120

Requires: ffmpeg (brew install ffmpeg); for play: ffplay or mpv.
EOF
}

if [[ $# -lt 1 ]] || [[ "$1" == "-h" ]] || [[ "$1" == "--help" ]]; then
  usage
  exit 0
fi

exec python3 -m radio_hls "$@"
