"""
Transcribe audio bytes with local Whisper (faster-whisper).

Configure via env: WHISPER_MODEL_SIZE, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE, WHISPER_LANGUAGE.
Requires: pip install faster-whisper (see api/requirements.txt). FFmpeg is used internally for decoding.
"""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

_lock = threading.Lock()
_model = None
_model_key: tuple[str, str, str] | None = None


def _config() -> tuple[str, str, str, str | None]:
    size = os.getenv("WHISPER_MODEL_SIZE", "base")
    device = os.getenv("WHISPER_DEVICE", "cpu")
    compute_type = os.getenv("WHISPER_COMPUTE_TYPE") or (
        "int8" if device == "cpu" else "float16"
    )
    lang = os.getenv("WHISPER_LANGUAGE")
    if lang is not None and lang.strip() == "":
        lang = None
    return size, device, compute_type, lang


def _get_model():
    global _model, _model_key
    size, device, compute_type, _lang = _config()
    key = (size, device, compute_type)
    if _model is None or _model_key != key:
        from faster_whisper import WhisperModel

        _model = WhisperModel(size, device=device, compute_type=compute_type)
        _model_key = key
    return _model


def transcribe_audio_bytes_whisper(
    audio_bytes: bytes,
    *,
    filename_hint: str | None = None,
    language: str | None = None,
) -> str:
    """
    Write bytes to a temp file (suffix from filename_hint), run Whisper, return joined transcript.

    ``language`` overrides WHISPER_LANGUAGE for this call (ISO 639-1, e.g. ``vi``). When omitted, uses
    env WHISPER_LANGUAGE if set; otherwise faster-whisper auto-detects (often wrong for short clips).
    """
    if not audio_bytes:
        raise ValueError("empty audio")
    suf = Path(filename_hint or "audio.mp3").suffix.lower()
    if not suf or len(suf) > 6 or not suf.startswith("."):
        suf = ".mp3"

    with tempfile.NamedTemporaryFile(suffix=suf, delete=False) as tmp:
        tmp.write(audio_bytes)
        path = tmp.name

    try:
        _, _, _, env_lang = _config()
        effective = (language or "").strip() or ((env_lang or "").strip() or None)
        model = _get_model()
        kwargs: dict = {"beam_size": 5}
        if effective:
            kwargs["language"] = effective
        with _lock:
            segments, _info = model.transcribe(path, **kwargs)
            parts = [s.text.strip() for s in segments]
        text = " ".join(parts).strip()
        return text
    finally:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


def whisper_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("faster_whisper") is not None
