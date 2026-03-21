"""
POST /v1/media/image-score, /v1/media/audio-score, /v1/media/text-score — 1–100 score + explanation.

LLM: OpenAI direct (OPENAI_API_KEY) or OpenRouter (OPENROUTER_API_KEY); see LLM_BACKEND in api/llm_provider.py.
Audio: local faster-whisper by default; AUDIO_TRANSCRIBE_BACKEND=openrouter uses chat input_audio; with OpenAI,
that API path uses OpenAI Whisper instead. ?direct=true = one-shot audio JSON via chat.
"""
from __future__ import annotations

import asyncio
import base64
import functools
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from api.llm_provider import (
    chat_completions_headers,
    chat_completions_url,
    default_audio_chat_model,
    default_chat_model,
    default_transcribe_chat_model,
    llm_backend,
    normalize_model_for_backend,
    openai_transcriptions_url,
    openai_whisper_model,
)
from api.prompts import (
    MEDIA_AUDIO_SYSTEM_PROMPT,
    MEDIA_TEXT_SYSTEM_PROMPT,
    ROAD_VISION_SYSTEM_PROMPT,
)

load_dotenv()
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARS = 32_000

# Romanized + Vietnamese tokens: if present, text score must respect severity floor (model often returns 1–20 by habit).
_VN_DISRUPTION_RE = re.compile(
    r"(?i)(ùn\s*tắc|\bun\s+tac\b|tắc\s*đường|\btac\s+duong\b|kẹt\s*xe|\bket\s+xe\b|kẹt\s*cứng|"
    r"triều\s*cường|trieu\s*cuong|ngập|ngap\s+ung|ngap\s+nuoc|\btai\s+nan\b|tai\s+nạn|đường\s*đóng|"
    r"xe\s*hỏng|ùn\s*ứ|\bun\s+u\b|đông\s*xe|\bdong\s+xe\b)",
    re.UNICODE,
)
_VN_DISRUPTION_SEVERE_RE = re.compile(
    r"(?i)(cực\s*mạnh|\bcuc\s+manh\b|cực\s*đông|\bcuc\s+dong\b|nghiêm\s*trọng|nghiem\s*trong|"
    r"cực\s*nghiêm|kẹt\s*cứng|\bket\s+cung\b|ùn\s*nặng|un\s*nang)",
    re.UNICODE,
)


def _vn_disruption_score_floor(text: str) -> int | None:
    """Minimum score when disruption keywords appear (severity rubric: higher = worse news)."""
    if not _VN_DISRUPTION_RE.search(text):
        return None
    if _VN_DISRUPTION_SEVERE_RE.search(text):
        return 71
    return 51


router = APIRouter(tags=["media-score"])


class RoadLikeAnalysis(BaseModel):
    pavement_surface: str = ""
    visibility_environment: str = ""
    hazards_constraints: str = ""
    scene_context: str = ""


class MediaImageScoreResponse(BaseModel):
    score: int = Field(..., ge=1, le=100)
    rationale: str | None = None
    explanation: str | None = None
    analysis: RoadLikeAnalysis = Field(default_factory=RoadLikeAnalysis)
    model: str | None = None


class AudioAnalysis(BaseModel):
    transcription_summary: str = ""
    traffic_relevance: str = ""
    clarity: str = ""
    limitations: str = ""


class MediaAudioScoreResponse(BaseModel):
    score: int = Field(..., ge=1, le=100)
    rationale: str | None = None
    explanation: str | None = None
    analysis: AudioAnalysis = Field(default_factory=AudioAnalysis)
    model: str | None = Field(None, description="Text model used when scoring via transcript.")
    transcript: str | None = Field(
        None,
        description="Verbatim transcript when using transcribe-then-score (default audio path).",
    )
    transcribe_model: str | None = Field(None, description="Audio-capable model used for transcription.")


class TextAnalysis(BaseModel):
    summary: str = ""
    traffic_relevance: str = ""
    specificity: str = ""
    limitations: str = ""


class MediaTextScoreResponse(BaseModel):
    score: int = Field(..., ge=1, le=100)
    rationale: str | None = None
    explanation: str | None = None
    analysis: TextAnalysis = Field(default_factory=TextAnalysis)
    model: str | None = None


class MediaTextScoreIn(BaseModel):
    text: str = Field(..., max_length=MAX_TEXT_CHARS)


def _llm_http_error(prefix: str, r: httpx.Response) -> ValueError:
    return ValueError(f"{prefix} {r.status_code}: {r.text[:2000]}")


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("No JSON object in model output")
    return json.loads(m.group())


def _coerce_road_like(raw: Any) -> RoadLikeAnalysis:
    if not isinstance(raw, dict):
        return RoadLikeAnalysis()
    return RoadLikeAnalysis(
        pavement_surface=str(raw.get("pavement_surface") or raw.get("pavement") or "").strip(),
        visibility_environment=str(
            raw.get("visibility_environment") or raw.get("visibility") or ""
        ).strip(),
        hazards_constraints=str(
            raw.get("hazards_constraints") or raw.get("hazards") or ""
        ).strip(),
        scene_context=str(raw.get("scene_context") or raw.get("context") or "").strip(),
    )


def _coerce_audio_analysis(raw: Any) -> AudioAnalysis:
    if not isinstance(raw, dict):
        return AudioAnalysis()
    return AudioAnalysis(
        transcription_summary=str(raw.get("transcription_summary") or "").strip(),
        traffic_relevance=str(raw.get("traffic_relevance") or "").strip(),
        clarity=str(raw.get("clarity") or "").strip(),
        limitations=str(raw.get("limitations") or "").strip(),
    )


def _coerce_text_analysis(raw: Any) -> TextAnalysis:
    if not isinstance(raw, dict):
        return TextAnalysis()
    return TextAnalysis(
        summary=str(raw.get("summary") or "").strip(),
        traffic_relevance=str(raw.get("traffic_relevance") or "").strip(),
        specificity=str(raw.get("specificity") or "").strip(),
        limitations=str(raw.get("limitations") or "").strip(),
    )


def _message_content_from_response(data: dict[str, Any]) -> str:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise ValueError(f"Unexpected OpenRouter payload: {e}") from e
    if isinstance(content, list):
        content = "".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    if not isinstance(content, str):
        content = str(content)
    return content


def score_media_image_sync(
    image_bytes: bytes,
    media_type: str,
    model: str,
) -> MediaImageScoreResponse:
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError("Image too large (max 20 MB)")
    m = normalize_model_for_backend(model)
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    data_url = f"data:{media_type};base64,{b64}"
    body = {
        "model": m,
        "messages": [
            {"role": "system", "content": ROAD_VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Score this road/traffic image and fill every JSON field per the rules.",
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
    }
    with httpx.Client(timeout=120.0) as client:
        r = client.post(chat_completions_url(), headers=chat_completions_headers(), json=body)
    if r.status_code != 200:
        raise _llm_http_error("LLM image-score error", r)
    content = _message_content_from_response(r.json())
    obj = _extract_json_object(content)
    score = int(obj["score"])
    if score < 1 or score > 100:
        raise ValueError("score out of range")
    explanation = obj.get("explanation")
    if explanation is not None:
        explanation = str(explanation).strip() or None
    return MediaImageScoreResponse(
        score=score,
        rationale=(str(obj["rationale"]).strip() if obj.get("rationale") else None),
        explanation=explanation,
        analysis=_coerce_road_like(obj.get("analysis")),
        model=m,
    )


def _strip_fenced_text(content: str) -> str:
    t = (content or "").strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if len(lines) >= 2 and lines[-1].strip() == "```":
            t = "\n".join(lines[1:-1]).strip()
        else:
            t = "\n".join(lines[1:]).strip()
    return t


def transcribe_audio_sync(
    audio_bytes: bytes,
    *,
    openrouter_format: str,
    model: str,
) -> str:
    """Speech → plain text (no JSON). Uses chat completions + input_audio (OpenRouter or OpenAI if supported)."""
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise ValueError("Audio too large (max 25 MB)")
    m = normalize_model_for_backend(model)
    b64 = base64.standard_b64encode(audio_bytes).decode("ascii")
    body = {
        "model": m,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You transcribe audio. Reply with ONLY the spoken words, in the same language "
                    "as the speech. No titles, no JSON, no markdown fences, no commentary. "
                    "If there is no intelligible speech, reply exactly: [inaudible]"
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Transcribe this audio."},
                    {"type": "input_audio", "input_audio": {"data": b64, "format": openrouter_format}},
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 8000,
    }
    with httpx.Client(timeout=180.0) as client:
        r = client.post(chat_completions_url(), headers=chat_completions_headers(), json=body)
    if r.status_code != 200:
        raise _llm_http_error("LLM transcribe error", r)
    content = _message_content_from_response(r.json())
    return _strip_fenced_text(content)


def _audio_upload_mime(suffix: str) -> str:
    return {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".webm": "audio/webm",
    }.get(suffix.lower(), "application/octet-stream")


def transcribe_openai_whisper(audio_bytes: bytes, *, filename_hint: str | None = None) -> str:
    """Speech → plain text via OpenAI /v1/audio/transcriptions (whisper-1)."""
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise ValueError("Audio too large (max 25 MB)")
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise ValueError("OPENAI_API_KEY is not set")
    suf = Path(filename_hint or "audio.mp3").suffix.lower()
    if not suf or len(suf) > 6 or not suf.startswith("."):
        suf = ".mp3"
    fd, path = tempfile.mkstemp(suffix=suf)
    try:
        os.write(fd, audio_bytes)
        os.close(fd)
        wm = openai_whisper_model()
        url = openai_transcriptions_url()
        with httpx.Client(timeout=180.0) as client:
            with open(path, "rb") as f:
                r = client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": (f"clip{suf}", f, _audio_upload_mime(suf))},
                    data={"model": wm},
                )
        if r.status_code != 200:
            raise ValueError(f"OpenAI transcription error {r.status_code}: {r.text[:2000]}")
        data = r.json()
        return (data.get("text") or "").strip()
    finally:
        Path(path).unlink(missing_ok=True)


def audio_openrouter_format(content_type: str | None, filename: str | None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    mime_map = {
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/ogg": "ogg",
        "audio/mp4": "m4a",
        "audio/x-m4a": "m4a",
        "audio/aac": "aac",
        "audio/flac": "flac",
        "audio/webm": "webm",
    }
    if ct in mime_map:
        return mime_map[ct]
    name = (filename or "").lower()
    suf = Path(name).suffix.lstrip(".")
    if suf in ("mp3", "wav", "ogg", "m4a", "aac", "flac", "webm"):
        return suf
    return "mp3"


def score_media_audio_sync(
    audio_bytes: bytes,
    *,
    openrouter_format: str,
    model: str,
) -> MediaAudioScoreResponse:
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise ValueError("Audio too large (max 25 MB)")
    m = normalize_model_for_backend(model)
    b64 = base64.standard_b64encode(audio_bytes).decode("ascii")
    body = {
        "model": m,
        "messages": [
            {"role": "system", "content": MEDIA_AUDIO_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Listen to this audio. Score traffic/mobility usefulness 1–100 "
                            "and return only the JSON object described in your instructions."
                        ),
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {"data": b64, "format": openrouter_format},
                    },
                ],
            },
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
    }
    with httpx.Client(timeout=180.0) as client:
        r = client.post(chat_completions_url(), headers=chat_completions_headers(), json=body)
    if r.status_code != 200:
        raise _llm_http_error("LLM audio-score error", r)
    content = _message_content_from_response(r.json())
    obj = _extract_json_object(content)
    score = int(obj["score"])
    if score < 1 or score > 100:
        raise ValueError("score out of range")
    explanation = obj.get("explanation")
    if explanation is not None:
        explanation = str(explanation).strip() or None
    return MediaAudioScoreResponse(
        score=score,
        rationale=(str(obj["rationale"]).strip() if obj.get("rationale") else None),
        explanation=explanation,
        analysis=_coerce_audio_analysis(obj.get("analysis")),
        model=m,
        transcript=None,
        transcribe_model=None,
    )


def score_media_audio_via_transcript_sync(
    audio_bytes: bytes,
    *,
    openrouter_format: str,
    transcribe_model: str | None = None,
    score_model: str | None = None,
    filename_hint: str | None = None,
    whisper_language: str | None = None,
) -> MediaAudioScoreResponse:
    """Transcribe audio (Whisper by default), then score transcript like /v1/media/text-score."""
    tm = transcribe_model or default_transcribe_chat_model()
    sm = normalize_model_for_backend(score_model or default_chat_model())
    backend = os.getenv("AUDIO_TRANSCRIBE_BACKEND", "whisper").strip().lower()
    transcribe_label: str

    if backend in ("openrouter", "remote", "api"):
        if llm_backend() == "openai":
            transcript = transcribe_openai_whisper(audio_bytes, filename_hint=filename_hint)
            transcribe_label = f"openai:{openai_whisper_model()}"
        else:
            transcript = transcribe_audio_sync(
                audio_bytes,
                openrouter_format=openrouter_format,
                model=tm,
            )
            transcribe_label = tm
    else:
        try:
            from api.whisper_transcribe import transcribe_audio_bytes_whisper, whisper_available

            if not whisper_available():
                raise ImportError("faster-whisper not installed")
            transcript = transcribe_audio_bytes_whisper(
                audio_bytes,
                filename_hint=filename_hint,
                language=whisper_language,
            )
            transcribe_label = f"whisper:{os.getenv('WHISPER_MODEL_SIZE', 'base')}"
        except ImportError:
            if llm_backend() == "openai":
                transcript = transcribe_openai_whisper(audio_bytes, filename_hint=filename_hint)
                transcribe_label = f"openai:{openai_whisper_model()}"
            else:
                transcript = transcribe_audio_sync(
                    audio_bytes,
                    openrouter_format=openrouter_format,
                    model=tm,
                )
                transcribe_label = tm

    tnorm = transcript.strip()
    if len(tnorm) < 2 or tnorm.lower() in ("[inaudible]", "inaudible", "(inaudible)"):
        raise ValueError("No usable transcript from audio (silent or unintelligible)")
    text_res = score_media_text_sync(transcript, sm)
    ta = text_res.analysis
    return MediaAudioScoreResponse(
        score=text_res.score,
        rationale=text_res.rationale,
        explanation=text_res.explanation,
        analysis=AudioAnalysis(
            transcription_summary=transcript[:8000] if len(transcript) > 8000 else transcript,
            traffic_relevance=ta.traffic_relevance,
            clarity=ta.specificity,
            limitations=ta.limitations,
        ),
        model=sm,
        transcript=transcript[:MAX_TEXT_CHARS] if len(transcript) > MAX_TEXT_CHARS else transcript,
        transcribe_model=transcribe_label,
    )


def score_media_text_sync(text: str, model: str) -> MediaTextScoreResponse:
    t = (text or "").strip()
    if not t:
        raise ValueError("Empty text")
    if len(t) > MAX_TEXT_CHARS:
        t = t[:MAX_TEXT_CHARS]
    m = normalize_model_for_backend(model)
    body = {
        "model": m,
        "messages": [
            {"role": "system", "content": MEDIA_TEXT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Score the following paragraph on the 1–100 **severity / significance** scale defined in your "
                    "system instructions (higher = more serious disruption for travelers). "
                    "Return only the JSON object from your instructions.\n\n---\n" + t + "\n---"
                ),
            },
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
    }
    with httpx.Client(timeout=120.0) as client:
        r = client.post(chat_completions_url(), headers=chat_completions_headers(), json=body)
    if r.status_code != 200:
        raise _llm_http_error("LLM text-score error", r)
    content = _message_content_from_response(r.json())
    obj = _extract_json_object(content)
    score = int(obj["score"])
    if score < 1 or score > 100:
        raise ValueError("score out of range")
    floor = _vn_disruption_score_floor(t)
    if floor is not None and score < floor:
        score = floor
    explanation = obj.get("explanation")
    if explanation is not None:
        explanation = str(explanation).strip() or None
    return MediaTextScoreResponse(
        score=score,
        rationale=(str(obj["rationale"]).strip() if obj.get("rationale") else None),
        explanation=explanation,
        analysis=_coerce_text_analysis(obj.get("analysis")),
        model=m,
    )


@router.post(
    "/media/image-score",
    response_model=MediaImageScoreResponse,
    summary="Score road/traffic image (same rubric as /v1/road-score)",
)
async def media_image_score(
    image: UploadFile = File(..., description="Image (JPEG, PNG, WebP, …)."),
    model: str | None = Query(
        None, description="Vision model id (default OPENAI_MODEL or OPENROUTER_MODEL per LLM_BACKEND)."
    ),
) -> MediaImageScoreResponse:
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail="Upload an image file (image/jpeg, image/png, image/webp, …).",
        )
    raw = await image.read()
    use_model = model or default_chat_model()
    try:
        return await asyncio.to_thread(
            score_media_image_sync, raw, image.content_type, use_model
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post(
    "/media/audio-score",
    response_model=MediaAudioScoreResponse,
    summary="Transcribe audio then score transcript (default), or score raw audio (?direct=true)",
)
async def media_audio_score(
    audio: UploadFile = File(..., description="Audio (mp3, wav, ogg, m4a, …)."),
    direct: bool = Query(
        False,
        description="If true, one-shot audio JSON scoring (legacy). Default: transcribe then text-score.",
    ),
    transcribe_model: str | None = Query(
        None,
        description="Audio-input model for transcription (default OPENROUTER_TRANSCRIBE_MODEL or MEDIA_AUDIO model).",
    ),
    score_model: str | None = Query(
        None,
        description="Text model used to score the transcript (default OPENROUTER_MODEL).",
    ),
    whisper_language: str | None = Query(
        None,
        description="When using local Whisper: force language (e.g. vi). Overrides WHISPER_LANGUAGE.",
    ),
    model: str | None = Query(
        None,
        description="When direct=true: audio model for one-shot scoring (default OPENROUTER_MEDIA_AUDIO_MODEL).",
    ),
) -> MediaAudioScoreResponse:
    ct = audio.content_type or ""
    if ct and not ct.startswith("audio/") and "octet-stream" not in ct.lower():
        raise HTTPException(
            status_code=400,
            detail="Upload an audio file (audio/* or application/octet-stream).",
        )
    raw = await audio.read()
    if len(raw) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=400, detail="Audio too large (max 25 MB).")
    fmt = audio_openrouter_format(audio.content_type, audio.filename)
    try:
        if direct:
            use_model = model or default_audio_chat_model()
            return await asyncio.to_thread(
                score_media_audio_sync, raw, openrouter_format=fmt, model=use_model
            )
        return await asyncio.to_thread(
            functools.partial(
                score_media_audio_via_transcript_sync,
                raw,
                openrouter_format=fmt,
                transcribe_model=transcribe_model,
                score_model=score_model,
                filename_hint=audio.filename,
                whisper_language=whisper_language,
            ),
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post(
    "/media/text-score",
    response_model=MediaTextScoreResponse,
    summary="Score traffic relevance of a paragraph (text-only)",
)
async def media_text_score(
    body: MediaTextScoreIn,
    model: str | None = Query(
        None, description="Chat model id (default OPENAI_MODEL or OPENROUTER_MODEL per LLM_BACKEND)."
    ),
) -> MediaTextScoreResponse:
    use_model = model or default_chat_model()
    try:
        return await asyncio.to_thread(score_media_text_sync, body.text, use_model)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
