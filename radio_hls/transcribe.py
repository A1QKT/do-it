"""
Stream HLS audio and append speech-to-text lines to a file (local Whisper via faster-whisper).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from radio_hls.hls_stream import DEFAULT_VOH_HLS


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _drain_stderr(proc: subprocess.Popen[bytes]) -> None:
    if proc.stderr is None:
        return
    for line in iter(proc.stderr.readline, b""):
        if line:
            sys.stderr.buffer.write(line)


def run_transcription(
    *,
    url: str,
    output: Path,
    model_size: str,
    chunk_sec: float,
    language: str | None,
    device: str,
    compute_type: str | None,
) -> int:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print(
            "Missing faster-whisper. Install with:\n"
            "  pip install -e \".[stt]\"\n"
            "or: pip install faster-whisper",
            file=sys.stderr,
        )
        return 1

    ct = compute_type or ("int8" if device == "cpu" else "float16")
    print(f"Loading Whisper model {model_size!r} ({device}, {ct})…", file=sys.stderr, flush=True)
    model = WhisperModel(model_size, device=device, compute_type=ct)

    ffmpeg = _ffmpeg_exe()
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rw_timeout",
        "25000000",
        "-i",
        url,
        "-f",
        "s16le",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-acodec",
        "pcm_s16le",
        "pipe:1",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    threading.Thread(target=_drain_stderr, args=(proc,), daemon=True).start()

    if proc.stdout is None:
        print("ffmpeg stdout not available", file=sys.stderr)
        return 1

    sample_rate = 16000
    bytes_per_sample = 2
    chunk_bytes = int(sample_rate * bytes_per_sample * chunk_sec)
    buffer = bytearray()
    output.parent.mkdir(parents=True, exist_ok=True)

    def transcribe_chunk(pcm: bytes) -> str:
        if len(pcm) < sample_rate * bytes_per_sample:
            return ""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        kwargs: dict = dict(beam_size=1, vad_filter=True)
        if language:
            kwargs["language"] = language
        segments, _ = model.transcribe(audio, **kwargs)
        return "".join(s.text for s in segments).strip()

    with output.open("a", encoding="utf-8") as fout:
        header = f"# stream_stt start {datetime.now(timezone.utc).isoformat()} url={url}\n"
        fout.write(header)
        fout.flush()
        print(header, end="", flush=True)

        try:
            while True:
                block = proc.stdout.read(65536)
                if not block:
                    break
                buffer.extend(block)
                while len(buffer) >= chunk_bytes:
                    pcm = bytes(buffer[:chunk_bytes])
                    del buffer[:chunk_bytes]
                    text = transcribe_chunk(pcm)
                    if text:
                        ts = datetime.now(timezone.utc).isoformat()
                        line = f"[{ts}] {text}\n"
                        fout.write(line)
                        fout.flush()
                        print(line, end="", flush=True)
        except KeyboardInterrupt:
            print("\nStopping…", file=sys.stderr, flush=True)
        finally:
            if buffer:
                text = transcribe_chunk(bytes(buffer))
                if text:
                    ts = datetime.now(timezone.utc).isoformat()
                    line = f"[{ts}] {text}\n"
                    fout.write(line)
                    fout.flush()
                    print(line, end="", flush=True)
            proc.kill()
            proc.wait(timeout=5)

    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Infinite HLS → speech-to-text; appends lines to a file.",
    )
    p.add_argument("--url", default=DEFAULT_VOH_HLS, help="HLS playlist URL")
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="Text file to append transcripts to",
    )
    p.add_argument(
        "--model",
        default="base",
        help="Whisper model: tiny, base, small, medium, large-v3, …",
    )
    p.add_argument(
        "--chunk-sec",
        type=float,
        default=8.0,
        help="Audio window per transcription (seconds)",
    )
    p.add_argument(
        "--language",
        default="vi",
        help="Language code (e.g. vi). Use empty string for auto-detect.",
    )
    p.add_argument("--device", default="cpu", help="cpu or cuda")
    p.add_argument(
        "--compute-type",
        default=None,
        help="Override int8/float16 (default: int8 on cpu, float16 on cuda)",
    )
    args = p.parse_args()
    lang = args.language if args.language else None
    return run_transcription(
        url=args.url,
        output=args.output,
        model_size=args.model,
        chunk_sec=args.chunk_sec,
        language=lang,
        device=args.device,
        compute_type=args.compute_type,
    )


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    cli()
