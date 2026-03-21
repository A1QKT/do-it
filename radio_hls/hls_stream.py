"""Consume HLS (``.m3u8``) radio streams with **ffmpeg** / **ffprobe**."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# VOH — https://strm.voh.com.vn/radio/
DEFAULT_VOH_HLS = "https://strm.voh.com.vn/radio/channel5/playlist.m3u8"


def _need(bin_name: str) -> str:
    p = shutil.which(bin_name)
    if not p:
        print(
            f"{bin_name} not found. Install ffmpeg (e.g. brew install ffmpeg).",
            file=sys.stderr,
        )
        sys.exit(1)
    return p


def ffprobe_json(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    """Run ffprobe and return parsed JSON (format + streams)."""
    cmd = [
        _need("ffprobe"),
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        url,
    ]
    try:
        out = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print("ffprobe timed out (network or server).", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(e.stderr or e.stdout or str(e), file=sys.stderr)
        sys.exit(1)
    return json.loads(out.stdout)


def format_metadata_lines(data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    fmt = data.get("format") or {}
    tags = fmt.get("tags") or {}
    if tags:
        lines.append("format.tags:")
        for k in sorted(tags.keys(), key=str.lower):
            lines.append(f"  {k}={tags[k]}")
    else:
        lines.append("format.tags: (none)")

    for i, st in enumerate(data.get("streams") or []):
        st_tags = st.get("tags") or {}
        if not st_tags:
            continue
        lines.append(f"stream[{i}].tags:")
        for k in sorted(st_tags.keys(), key=str.lower):
            lines.append(f"  {k}={st_tags[k]}")
    lines.append(
        f"format.duration={fmt.get('duration', '?')}  bitrate={fmt.get('bit_rate', '?')}"
    )
    return lines


def cmd_metadata(args: argparse.Namespace) -> int:
    data = ffprobe_json(args.url, timeout=args.timeout)
    for line in format_metadata_lines(data):
        print(line)
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    _need("ffmpeg")
    out = Path(args.output)
    cmd: list[str] = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "info",
        "-i",
        args.url,
    ]
    if args.duration and args.duration > 0:
        cmd.extend(["-t", str(args.duration)])
    cmd.extend(["-c", "copy", str(out)])
    print(" ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd).returncode


def cmd_play(args: argparse.Namespace) -> int:
    if shutil.which("ffplay"):
        return subprocess.run(
            ["ffplay", "-nodisp", "-loglevel", "info", args.url],
        ).returncode
    if shutil.which("mpv"):
        return subprocess.run(["mpv", "--no-video", args.url]).returncode
    print(
        "Install ffplay or mpv for playback:\n"
        "  brew install ffmpeg mpv",
        file=sys.stderr,
    )
    return 1


def cmd_watch_metadata(args: argparse.Namespace) -> int:
    """Poll ffprobe every interval (many HLS AAC streams expose few tags)."""
    seen: str | None = None
    while True:
        try:
            data = ffprobe_json(args.url, timeout=args.timeout)
            blob = json.dumps(
                (data.get("format") or {}).get("tags") or {},
                sort_keys=True,
            )
            if blob != seen:
                seen = blob
                ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                print(f"{ts} {blob}")
                for line in format_metadata_lines(data):
                    print(f"  {line}")
                print()
        except KeyboardInterrupt:
            return 0
        time.sleep(args.interval)


def cli() -> None:
    raise SystemExit(main())


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--url",
        default=DEFAULT_VOH_HLS,
        metavar="URL",
        help="HLS playlist URL (default: VOH channel 5)",
    )
    common.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SEC",
        help="ffprobe network timeout (seconds)",
    )

    p = argparse.ArgumentParser(
        description="HLS internet radio via ffmpeg/ffprobe.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_meta = sub.add_parser(
        "metadata",
        parents=[common],
        help="One-shot ffprobe format/stream tags",
    )
    p_meta.set_defaults(func=cmd_metadata)

    p_rec = sub.add_parser("record", parents=[common], help="Save stream (-c copy)")
    p_rec.add_argument(
        "-o",
        "--output",
        required=True,
        help="Output file (e.g. out.aac)",
    )
    p_rec.add_argument(
        "-t",
        "--duration",
        type=float,
        default=0,
        help="Seconds to record (0 = until Ctrl+C)",
    )
    p_rec.set_defaults(func=cmd_record)

    p_play = sub.add_parser(
        "play",
        parents=[common],
        help="Play with ffplay or mpv",
    )
    p_play.set_defaults(func=cmd_play)

    p_watch = sub.add_parser(
        "watch-metadata",
        parents=[common],
        help="Poll ffprobe when tag blob changes",
    )
    p_watch.add_argument(
        "-i",
        "--interval",
        type=float,
        default=15.0,
        help="Seconds between probes",
    )
    p_watch.set_defaults(func=cmd_watch_metadata)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    cli()
