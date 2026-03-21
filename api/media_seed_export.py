"""
GET /v1/media-seed/latest — route names → traffic mobility scores (1 best … 100 worst) + reasons from export.
"""
from __future__ import annotations

import json
import os
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


def _openrouter_key_map(media_downloads: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Export key → openrouter payload (same collision rules as the crawler)."""
    seen: dict[str, bool] = {}
    out: dict[str, dict[str, Any]] = {}
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
        out[key] = o
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


def build_route_results(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    media = meta.get("media_downloads") if isinstance(meta.get("media_downloads"), list) else []
    media_list = [x for x in media if isinstance(x, dict)]
    or_map = _openrouter_key_map(media_list)

    routes: list[dict[str, Any]] = []
    routes_map: dict[str, dict[str, Any]] = {}
    for name in _score_key_order(data):
        sc = _parse_score_value(data[name])
        if sc is None:
            continue
        o = or_map.get(name)
        reason = _reason_from_openrouter(o) if o else ""
        if not reason:
            reason = "No model explanation in export for this key (score may come from page text heuristics)."
        entry = {"name": name, "score": sc, "reason": reason}
        routes.append(entry)
        routes_map[name] = {"score": sc, "reason": reason}
    return routes, routes_map


class RouteScoreValue(BaseModel):
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


class RouteScoreEntry(BaseModel):
    name: str = Field(..., description="Route label / export key (e.g. corridor text, image:file, audio:file).")
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
    routes_map: dict[str, RouteScoreValue] = Field(
        ...,
        description="Same data as a map: route name → score and reason.",
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
        "Reads `media_seed_export.json` and returns **routes**: each **name** (route/segment key), "
        "**score** (1 = best to travel, 100 = worst), and **reason** (model text when available). "
        "**routes_map** is the same as a name → {score, reason} object. "
        "Set `full=true` to include the raw export under **export**. "
        "Override file path with `MEDIA_SEED_EXPORT_PATH`."
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

    routes_list, routes_map_raw = build_route_results(data)
    routes = [RouteScoreEntry(**r) for r in routes_list]
    routes_map = {k: RouteScoreValue(**v) for k, v in routes_map_raw.items()}

    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    crawled = meta.get("crawled_at")
    url = meta.get("url")
    crawled_at = str(crawled) if crawled is not None else None
    source_url = str(url) if url is not None else None

    return MediaSeedLatestResponse(
        routes=routes,
        routes_map=routes_map,
        crawled_at=crawled_at,
        source_url=source_url,
        export=data if full else None,
    )
