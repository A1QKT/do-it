"""
POST /v1/road-score — image in, road quality score 1–100 out (OpenAI or OpenRouter vision).

1 = best street to use, 100 = worst. Set OPENAI_API_KEY or OPENROUTER_API_KEY in .env (see .env.example).
"""
from __future__ import annotations

import base64
import json
import os
import re
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from api.llm_provider import (
    chat_completions_headers,
    chat_completions_url,
    default_chat_model,
    normalize_model_for_backend,
)
from api.media_score import router as media_score_router
from api.media_seed_export import router as media_seed_export_router
from api.prompts import ROAD_VISION_SYSTEM_PROMPT

load_dotenv()

MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB

SYSTEM_PROMPT = ROAD_VISION_SYSTEM_PROMPT


class RoadAnalysis(BaseModel):
    pavement_surface: str = Field(
        default="",
        description="Surface condition, markings, defects visible in the image.",
    )
    visibility_environment: str = Field(
        default="",
        description="Sight lines, lighting, weather, visual clutter.",
    )
    hazards_constraints: str = Field(
        default="",
        description="Obstructions, water, construction, geometry, other risks visible.",
    )
    scene_context: str = Field(
        default="",
        description="Setting (urban/rural), infrastructure cues visible in the image.",
    )


class RoadScoreResponse(BaseModel):
    score: int = Field(
        ...,
        ge=1,
        le=100,
        description="Road quality: 1 = best street to use, 100 = worst.",
    )
    rationale: str | None = Field(None, description="One-line summary tied to the score.")
    explanation: str | None = Field(
        None,
        description="Multi-sentence narrative for end users.",
    )
    analysis: RoadAnalysis = Field(
        default_factory=RoadAnalysis,
        description="Structured breakdown of what was observed.",
    )
    model: str | None = Field(None, description="Chat model used (OpenAI or OpenRouter).")


def _cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "*")
    return [o.strip() for o in raw.split(",") if o.strip()]


app = FastAPI(
    title="Road score API",
    description="Image → road quality score (1 best, 100 worst), explanation, and structured analysis via OpenAI or OpenRouter vision.",
    version="1.6.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(media_score_router, prefix="/v1")
app.include_router(media_seed_export_router, prefix="/v1")


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("No JSON object in model output")
    return json.loads(m.group())


def _coerce_analysis(raw: Any) -> RoadAnalysis:
    if not isinstance(raw, dict):
        return RoadAnalysis()
    return RoadAnalysis(
        pavement_surface=str(raw.get("pavement_surface") or raw.get("pavement") or "").strip(),
        visibility_environment=str(
            raw.get("visibility_environment") or raw.get("visibility") or ""
        ).strip(),
        hazards_constraints=str(
            raw.get("hazards_constraints") or raw.get("hazards") or ""
        ).strip(),
        scene_context=str(raw.get("scene_context") or raw.get("context") or "").strip(),
    )


async def _llm_road_vision(
    *,
    image_bytes: bytes,
    media_type: str,
    model: str,
) -> RoadScoreResponse:
    try:
        headers = chat_completions_headers()
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    m = normalize_model_for_backend(model)
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    data_url = f"data:{media_type};base64,{b64}"

    body = {
        "model": m,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Score this road image and fill every JSON field per the rules.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            },
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(chat_completions_url(), headers=headers, json=body)

    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"LLM error {r.status_code}: {r.text[:2000]}",
        )

    data = r.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise HTTPException(status_code=502, detail=f"Unexpected LLM payload: {e}")

    if isinstance(content, list):
        content = "".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    if not isinstance(content, str):
        content = str(content)

    try:
        obj = _extract_json_object(content)
        score = int(obj["score"])
        if score < 1 or score > 100:
            raise ValueError("score out of range")
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not parse model JSON: {e}. Raw: {content[:500]}",
        )

    explanation = obj.get("explanation")
    if explanation is not None:
        explanation = str(explanation).strip() or None

    return RoadScoreResponse(
        score=score,
        rationale=(str(obj["rationale"]).strip() if obj.get("rationale") else None),
        explanation=explanation,
        analysis=_coerce_analysis(obj.get("analysis")),
        model=m,
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/v1/road-score",
    response_model=RoadScoreResponse,
    summary="Score road quality from an image",
    responses={
        400: {"description": "Bad image input"},
        502: {"description": "Upstream LLM or parse error"},
    },
)
async def road_score(
    image: UploadFile = File(..., description="Road/street photo (JPEG, PNG, or WebP)."),
    model: str | None = Query(
        None,
        description="Optional model id (defaults to OPENAI_MODEL or OPENROUTER_MODEL per LLM_BACKEND).",
    ),
) -> RoadScoreResponse:
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail="Upload an image file (image/jpeg, image/png, or image/webp).",
        )

    raw = await image.read()
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="Image too large (max 20 MB).")

    use_model = model or default_chat_model()
    return await _llm_road_vision(
        image_bytes=raw,
        media_type=image.content_type,
        model=use_model,
    )
