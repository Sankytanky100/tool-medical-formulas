"""
TAP runtime configuration (E.3b LLM TAP).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def tap_mode() -> str:
    """
    ``deterministic`` | ``llm`` | ``shadow`` | ``adk``.

    - deterministic: rule-based composer (E.3-lite)
    - llm: Vertex/Gemini structured JSON + vector retrieval
    - adk: google-adk LlmAgent + InMemoryRunner per request
    - shadow: run LLM + deterministic; log diff; return per ``TAP_SHADOW_RETURN``
    """
    return (os.getenv("MEDICAL_FORMULAS_TAP_MODE") or "deterministic").strip().lower()


def tap_shadow_return() -> str:
    """Which path to return in shadow mode: ``llm`` or ``deterministic``."""
    return (os.getenv("MEDICAL_FORMULAS_TAP_SHADOW_RETURN") or "llm").strip().lower()


def tap_llm_enabled() -> bool:
    return tap_mode() in ("llm", "adk", "shadow")


@dataclass(frozen=True)
class TapBudget:
    max_tokens: int
    max_latency_ms: int

    @classmethod
    def from_envelope(cls, envelope: dict) -> "TapBudget":
        raw = envelope.get("budget") if isinstance(envelope.get("budget"), dict) else {}
        return cls(
            max_tokens=int(raw.get("max_tokens") or os.getenv("MEDICAL_FORMULAS_TAP_MAX_TOKENS") or 4096),
            max_latency_ms=int(
                raw.get("max_latency_ms") or os.getenv("MEDICAL_FORMULAS_TAP_MAX_LATENCY_MS") or 8000
            ),
        )


def tap_model_id() -> str:
    return (
        os.getenv("MEDICAL_FORMULAS_TAP_MODEL")
        or os.getenv("VERTEX_AI_MODEL")
        or "gemini-2.5-flash"
    ).strip()


def tap_embed_mode() -> str:
    """``stub`` (CI/offline) or ``vertex``."""
    return (os.getenv("MEDICAL_FORMULAS_TAP_EMBED") or "stub").strip().lower()


def tap_index_dir() -> str:
    return (
        os.getenv("MEDICAL_FORMULAS_TAP_INDEX_DIR")
        or ""
    ).strip()


def tap_rebuild_index() -> bool:
    return (os.getenv("MEDICAL_FORMULAS_TAP_REBUILD_INDEX") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
