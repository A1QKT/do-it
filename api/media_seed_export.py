"""
GET /v1/media-seed/latest — human-readable route labels, scores (1 best … 100 worst), reasons from export.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

load_dotenv()

router = APIRouter(tags=["media-seed"])

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_EXPORT = _REPO_ROOT / "media_seed_export.json"


def _export_path() -> Path:
    raw = os.getenv("MEDIA_SEED_EXPORT_PATH", "").strip()
    if not raw:
        return _DEFAULT_EXPORT
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (_REPO_ROOT / p).resolve()
    return p


def _export_key_for_scored_media_item(item: dict[str, Any], idx: int) -> str:
    """Match scripts/media_seed_crawl.export_keys_from_media_openrouter key rules."""
    kind = item.get("kind") or "unknown"
    lp = item.get("local_path")
    if kind == "text" and lp:
        path = _REPO_ROOT / lp
        try:
            t = path.read_text(encoding="utf-8", errors="replace").strip()
            if t:
                line = (t.splitlines() or [t])[0].strip()
                if len(line) >= 6:
                    return line[:220]
        except OSError:
            pass
        return "crawl:text"
    if lp:
        name = Path(lp).name
        if kind == "image":
            return f"image:{name}"
        if kind == "audio":
            return f"audio:{name}"
        return f"{kind}:{name}"
    return f"crawl:{kind}:{idx}"


def _export_key_to_scored_media(
    media_downloads: list[dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Export key → (media row, openrouter payload)."""
    seen: dict[str, bool] = {}
    out: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for idx, item in enumerate(media_downloads):
        o = item.get("openrouter")
        if not isinstance(o, dict) or o.get("error"):
            continue
        if "score" not in o:
            continue
        try:
            int(o["score"])
        except (TypeError, ValueError):
            continue
        key = _export_key_for_scored_media_item(item, idx)
        base = key
        n = 2
        while key in seen:
            key = f"{base} ({n})"
            n += 1
        seen[key] = True
        out[key] = (item, o)
    return out


def _reason_from_openrouter(o: dict[str, Any]) -> str:
    ex = (o.get("explanation") or "").strip() if isinstance(o.get("explanation"), str) else ""
    ra = (o.get("rationale") or "").strip() if isinstance(o.get("rationale"), str) else ""
    if ex and ra:
        return f"{ra} {ex}".strip()
    return ex or ra or ""


def _parse_score_value(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        if 1 <= raw <= 100:
            return raw
        return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s.isdigit():
            return None
        n = int(s)
        if 1 <= n <= 100:
            return n
    return None


def _score_key_order(data: dict[str, Any]) -> list[str]:
    meta = data.get("_meta")
    if isinstance(meta, dict):
        for k in ("score_keys_this_run", "openrouter_media_score_keys"):
            v = meta.get(k)
            if isinstance(v, list) and v:
                return [str(x) for x in v if isinstance(x, str)]
    out: list[str] = []
    for k in data:
        if k == "_meta":
            continue
        if _parse_score_value(data[k]) is not None:
            out.append(k)
    return sorted(out)


_SUMMARY_PREFIXES = (
    "the paragraph states that ",
    "the paragraph describes ",
    "it details ",
    "it states that ",
    "the text states that ",
    "the text describes ",
)


def _strip_summary_boilerplate(s: str) -> str:
    t = s.strip()
    low = t.lower()
    for p in _SUMMARY_PREFIXES:
        if low.startswith(p):
            t = t[len(p) :].strip()
            low = t.lower()
    return t


def _truncate_at_word(s: str, max_len: int) -> str:
    s = s.strip()
    if len(s) <= max_len:
        return s
    cut = s[: max_len - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def _first_sentence(s: str, max_len: int = 140) -> str:
    s = s.strip()
    if not s:
        return ""
    for sep in ".!?。":
        pos = s.find(sep)
        if 0 < pos < min(len(s) - 1, max_len + 40):
            frag = s[: pos + 1].strip()
            if len(frag) <= max_len + 30:
                return _truncate_at_word(frag, max_len)
    return _truncate_at_word(s, max_len)


def _label_from_prose_export_key(export_key: str) -> str:
    """Short corridor-style title from paragraph text keys (not image:/audio:)."""
    key = export_key.strip()
    if not key:
        return "Route report"
    parts = [p.strip() for p in key.split(",") if p.strip()]
    if len(parts) >= 2 and re.search(r"đoạn|from|to|đến", parts[1], re.I):
        merged = f"{parts[0]}, {parts[1]}"
        return _truncate_at_word(merged, 120)
    return _truncate_at_word(parts[0], 120)


def _clean_filename_stem(export_key: str, prefix: str) -> str:
    rest = export_key[len(prefix) :].strip() if export_key.startswith(prefix) else export_key
    stem = Path(rest).stem
    stem = re.sub(r"_[0-9a-f]{6,}$", "", stem, flags=re.I)
    stem = re.sub(r"^\d+-", "", stem)
    return stem.strip() or "upload"


def _derive_route_label(
    export_key: str,
    item: dict[str, Any] | None,
    o: dict[str, Any] | None,
) -> str:
    """
    Human-readable route / segment title. Uses OpenRouter `analysis` when present (already LLM-derived);
    otherwise heuristics on export keys / filenames.
    """
    kind = (item or {}).get("kind") if item else None
    analysis = o.get("analysis") if isinstance(o, dict) and isinstance(o.get("analysis"), dict) else None

    if kind == "text" and export_key and not export_key.startswith(("image:", "audio:", "crawl:")):
        prose = _label_from_prose_export_key(export_key)
        if len(prose) >= 8:
            return prose

    if analysis and kind == "text":
        summary = analysis.get("summary")
        if isinstance(summary, str) and summary.strip():
            return _first_sentence(_strip_summary_boilerplate(summary), 140)

    if analysis and kind == "image":
        sc = (analysis.get("scene_context") or "").strip()
        if sc:
            return _first_sentence(f"Road conditions (photo): {sc}", 130)
        rat = (o or {}).get("rationale")
        if isinstance(rat, str) and rat.strip():
            return _first_sentence(f"Road conditions (photo): {rat.strip()}", 130)

    if analysis and kind == "audio":
        ts = (analysis.get("transcription_summary") or "").strip()
        if ts:
            return _first_sentence(f"Audio traffic report: {ts}", 130)
        tr = (o or {}).get("transcript")
        if isinstance(tr, str) and tr.strip():
            return _first_sentence(f"Audio traffic report: {tr.strip()}", 130)

    if o and kind == "image":
        rat = (o.get("rationale") or "").strip()
        if rat:
            return _first_sentence(f"Road conditions (photo): {rat}", 120)

    if o and kind == "audio":
        rat = (o.get("rationale") or "").strip()
        if rat:
            return _first_sentence(f"Audio clip: {rat}", 120)

    if export_key and not export_key.startswith(("image:", "audio:", "crawl:")):
        return _label_from_prose_export_key(export_key)

    if export_key.startswith("image:"):
        stem = _clean_filename_stem(export_key, "image:")
        return f"Road image ({stem})"

    if export_key.startswith("audio:"):
        stem = _clean_filename_stem(export_key, "audio:")
        return _truncate_at_word(f"Audio clip ({stem})", 100)

    return _truncate_at_word(export_key, 120) if export_key else "Unknown segment"


def build_route_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    media = meta.get("media_downloads") if isinstance(meta.get("media_downloads"), list) else []
    media_list = [x for x in media if isinstance(x, dict)]
    key_to_media = _export_key_to_scored_media(media_list)

    routes: list[dict[str, Any]] = []
    for export_key in _score_key_order(data):
        sc = _parse_score_value(data[export_key])
        if sc is None:
            continue
        pair = key_to_media.get(export_key)
        item, o = pair if pair else (None, None)
        reason = _reason_from_openrouter(o) if o else ""
        if not reason:
            reason = "No model explanation in export for this key (score may come from page text heuristics)."
        route = _derive_route_label(export_key, item, o)
        routes.append({"route": route, "score": sc, "reason": reason})
    return routes


class RouteScoreEntry(BaseModel):
    route: str = Field(
        ...,
        description="Human-readable route or road-segment label (from OpenRouter analysis or text heuristics).",
    )
    score: int = Field(
        ...,
        ge=1,
        le=100,
        description="Route / segment mobility quality: 1 = best route to use, 100 = worst.",
    )
    reason: str = Field(
        ...,
        description="Why this score (OpenRouter explanation/rationale when available).",
    )


class MediaSeedLatestResponse(BaseModel):
    routes: list[RouteScoreEntry] = Field(
        ...,
        description="Ordered route scores with reasons (order from score_keys_this_run when present).",
    )
    crawled_at: str | None = Field(None, description="ISO timestamp from export _meta when present.")
    source_url: str | None = Field(None, description="Crawled page URL from export _meta when present.")
    export: dict[str, Any] | None = Field(
        None,
        description="Full export root object (only when full=true).",
    )


@router.get(
    "/media-seed/latest",
    response_model=MediaSeedLatestResponse,
    summary="Latest route scores from media seed export",
    description=(
        "Reads `media_seed_export.json` and returns **routes**: each **route** (short human-readable label), "
        "**score** (1 = best to travel, 100 = worst), and **reason**. Labels are derived from OpenRouter "
        "`analysis` when available (same crawl already ran an LLM); otherwise from paragraph text or cleaned "
        "filenames. Set `full=true` for the raw export under **export**. "
        "Override path with `MEDIA_SEED_EXPORT_PATH`."
    ),
    responses={
        404: {"description": "Export file does not exist"},
        502: {"description": "File is not valid JSON"},
    },
)
async def get_media_seed_latest(
    full: bool = Query(
        False,
        description="If true, include the full parsed export JSON in `export`.",
    ),
) -> MediaSeedLatestResponse:
    path = _export_path()
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Media seed export not found: {path}",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not read export file: {e}",
        ) from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Invalid JSON in export file: {e}",
        ) from e
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=502,
            detail="Export file root must be a JSON object",
        )

    routes_list = build_route_results(data)
    routes = [RouteScoreEntry(**r) for r in routes_list]

    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    crawled = meta.get("crawled_at")
    url = meta.get("url")
    crawled_at = str(crawled) if crawled is not None else None
    source_url = str(url) if url is not None else None

    return MediaSeedLatestResponse(
        routes=routes,
        crawled_at=crawled_at,
        source_url=source_url,
        export=data if full else None,
    )
