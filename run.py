#!/usr/bin/env python3
"""Print VOH HLS stream info: pip ffmpeg (imageio-ffmpeg), else raw .m3u8 over HTTPS."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from radio_hls.hls_stream import DEFAULT_VOH_HLS


def _bundled_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _ffmpeg_info_lines(ffmpeg: str, url: str, timeout: float = 90.0) -> list[str]:
    # -rw_timeout: fail stalled HTTP/HLS reads (µs). -t: stop after ~5s decoded output (live streams).
    r = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-rw_timeout",
            "25000000",
            "-i",
            url,
            "-t",
            "5",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    err = r.stderr or ""
    out: list[str] = ["=== ffmpeg -i (imageio-ffmpeg) ==="]
    for ln in err.splitlines():
        s = ln.rstrip()
        if not s:
            continue
        if s.startswith(("Input #", "Duration:", "Stream #", "Metadata:")):
            out.append(s)
        elif "Audio:" in s or "Video:" in s or "bitrate:" in s.lower():
            out.append(s)
    if len(out) == 1:
        out.append(err.strip()[:2500] or "(empty stderr)")
    return out


def _dir_url(url: str) -> str:
    u = url.split("?", 1)[0]
    return u.rsplit("/", 1)[0] + "/" if "/" in u else url


def _http_get(url: str, timeout: float = 30.0) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; radio-hls/1.0)"})
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _print_hls(url: str, *, pip_hint: bool = True) -> None:
    if pip_hint:
        print("(install deps: pip install -r requirements.txt)\n", file=sys.stderr)
    try:
        master = _http_get(url)
    except URLError as e:
        print(f"Download failed: {e}", file=sys.stderr)
        sys.exit(1)
    print("=== Master playlist ===\n", master.rstrip(), sep="")

    lines = [ln.rstrip() for ln in master.splitlines()]
    inf = rel = None
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF"):
            inf = ln
            for j in range(i + 1, min(i + 5, len(lines))):
                t = lines[j].strip()
                if t and not t.startswith("#"):
                    rel = t
                    break
            break
    if not rel:
        return

    print("\n=== Variant ===\n", inf or "", "\nmedia_playlist:", rel, sep="")
    try:
        media = _http_get(urljoin(_dir_url(url), rel))
    except URLError as e:
        print(f"\nMedia playlist failed: {e}", file=sys.stderr)
        return
    m = media.splitlines()
    print("\n=== Media playlist (start) ===\n" + "\n".join(m[:30]) + ("\n…" if len(m) > 30 else ""))


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VOH_HLS
    ff = _bundled_ffmpeg()
    if ff:
        print("Probing stream (~5s of audio via ffmpeg; can take up to ~90s on slow links)…", flush=True)
        try:
            lines = _ffmpeg_info_lines(ff, url)
        except subprocess.TimeoutExpired:
            print(
                "ffmpeg timed out — fetching .m3u8 over HTTPS instead (no codec probe):\n",
                file=sys.stderr,
                flush=True,
            )
            _print_hls(url, pip_hint=False)
            return 0
        print("\n".join(lines))
        return 0
    _print_hls(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
