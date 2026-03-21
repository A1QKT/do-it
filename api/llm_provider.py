"""
Shared Chat Completions URL + auth for OpenAI (direct) vs OpenRouter.

Env:
  LLM_BACKEND=openai | openrouter | auto   (default auto: OpenAI if OPENAI_API_KEY is set, else OpenRouter)
  OPENAI_API_KEY                         — https://platform.openai.com/api-keys
  OPENAI_BASE_URL                        — optional override (default https://api.openai.com/v1)
  OPENAI_MODEL                           — default chat/vision/text model (default gpt-4o-mini)
  OPENROUTER_API_KEY                     — unchanged when using OpenRouter
"""
from __future__ import annotations

import os
from typing import Literal

LLMBackend = Literal["openai", "openrouter"]

_OPENAI_CHAT_PATH = "/chat/completions"


def llm_backend() -> LLMBackend:
    raw = (os.getenv("LLM_BACKEND") or "auto").strip().lower()
    if raw == "openai":
        return "openai"
    if raw == "openrouter":
        return "openrouter"
    if os.getenv("OPENAI_API_KEY", "").strip():
        return "openai"
    return "openrouter"


def openai_base_url() -> str:
    base = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
    return base


def chat_completions_url() -> str:
    if llm_backend() == "openai":
        return f"{openai_base_url()}{_OPENAI_CHAT_PATH}"
    return "https://openrouter.ai/api/v1/chat/completions"


def chat_completions_headers() -> dict[str, str]:
    if llm_backend() == "openai":
        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            raise ValueError(
                "OPENAI_API_KEY is not set. Add it to .env or set LLM_BACKEND=openrouter with OPENROUTER_API_KEY."
            )
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise ValueError("OPENROUTER_API_KEY is not set. Copy .env.example to .env or use OPENAI_API_KEY.")
    headers: dict[str, str] = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    ref = os.getenv("OPENROUTER_HTTP_REFERER")
    if ref:
        headers["HTTP-Referer"] = ref
    title = os.getenv("OPENROUTER_APP_TITLE")
    if title:
        headers["X-Title"] = title
    return headers


def default_chat_model() -> str:
    if llm_backend() == "openai":
        return os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    return os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip() or "openai/gpt-4o-mini"


def default_audio_chat_model() -> str:
    """Model for chat-style audio JSON (OpenRouter-style input_audio). OpenAI: override if needed."""
    if llm_backend() == "openai":
        return os.getenv("OPENAI_AUDIO_CHAT_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip() or "gpt-4o-mini"
    return os.getenv(
        "OPENROUTER_MEDIA_AUDIO_MODEL",
        "google/gemini-2.0-flash-001",
    ).strip() or "google/gemini-2.0-flash-001"


def default_transcribe_chat_model() -> str:
    if llm_backend() == "openai":
        return os.getenv("OPENAI_TRANSCRIBE_CHAT_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip() or "gpt-4o-mini"
    return (
        os.getenv("OPENROUTER_TRANSCRIBE_MODEL") or default_audio_chat_model()
    ).strip() or default_audio_chat_model()


def normalize_model_for_backend(model: str) -> str:
    """OpenRouter uses provider prefixes; OpenAI API expects short ids (e.g. gpt-4o-mini)."""
    m = (model or "").strip()
    if not m:
        m = default_chat_model()
    if llm_backend() == "openai":
        if m.startswith("openai/"):
            return m.split("/", 1)[-1]
        return m
    return m


def openai_transcriptions_url() -> str:
    return f"{openai_base_url()}/audio/transcriptions"


def openai_whisper_model() -> str:
    return os.getenv("OPENAI_WHISPER_MODEL", "whisper-1").strip() or "whisper-1"
