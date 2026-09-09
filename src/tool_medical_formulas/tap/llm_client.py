"""
Vertex/Gemini client for TAP structured JSON (E.3b).

Uses ``google.genai`` with JSON schema response when available; falls back to text parse.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from tool_medical_formulas.tap.config import TapBudget, tap_model_id

logger = logging.getLogger(__name__)


class TapLlmError(Exception):
    pass


def _vertex_project_location() -> tuple[str, str]:
    project = (
        os.getenv("GCP_PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or ""
    ).strip()
    location = (
        os.getenv("VERTEX_AI_REGION")
        or os.getenv("GCP_REGION")
        or os.getenv("VERTEX_LOCATION")
        or "us-central1"
    ).strip()
    return project, location


def _build_client():
    from google import genai

    use_vertex = (os.getenv("GOOGLE_GENAI_USE_VERTEXAI") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if use_vertex:
        project, location = _vertex_project_location()
        return genai.Client(vertexai=True, project=project, location=location)
    api_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if api_key:
        return genai.Client(api_key=api_key)
    project, location = _vertex_project_location()
    return genai.Client(vertexai=True, project=project, location=location)


def _parse_json_text(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise TapLlmError("empty_llm_response")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        parsed = json.loads(m.group(1))
        if isinstance(parsed, dict):
            return parsed
    m2 = re.search(r"\{.*\}", text, re.DOTALL)
    if m2:
        parsed = json.loads(m2.group(0))
        if isinstance(parsed, dict):
            return parsed
    raise TapLlmError("json_parse_failed")


async def generate_tap_json(
    *,
    system_instruction: str,
    user_message: str,
    response_schema: dict[str, Any],
    budget: TapBudget,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Returns ``(parsed_dict, usage_metadata)`` where usage has tokens_consumed, model_used.
    """
    model = tap_model_id()
    timeout_s = max(1.0, budget.max_latency_ms / 1000.0)

    def _sync_call() -> tuple[dict[str, Any], dict[str, Any]]:
        from google.genai import types

        client = _build_client()
        config = types.GenerateContentConfig(
            temperature=0.15,
            max_output_tokens=min(budget.max_tokens, 8192),
            response_mime_type="application/json",
            response_schema=response_schema,
            system_instruction=system_instruction,
        )
        resp = client.models.generate_content(
            model=model,
            contents=user_message,
            config=config,
        )
        text = ""
        if resp and resp.text:
            text = resp.text
        elif resp and resp.candidates:
            for c in resp.candidates:
                if c.content and c.content.parts:
                    for p in c.content.parts:
                        if getattr(p, "text", None):
                            text += p.text
        parsed = _parse_json_text(text)
        usage: dict[str, Any] = {"model_used": model, "tokens_consumed": 0}
        um = getattr(resp, "usage_metadata", None)
        if um is not None:
            usage["tokens_consumed"] = int(
                getattr(um, "total_token_count", None)
                or getattr(um, "candidates_token_count", None)
                or 0
            )
        return parsed, usage

    try:
        return await asyncio.wait_for(asyncio.to_thread(_sync_call), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise TapLlmError(f"tap_llm_timeout_{int(timeout_s * 1000)}ms") from exc
    except TapLlmError:
        raise
    except Exception as exc:
        logger.exception("TAP LLM call failed")
        raise TapLlmError(str(exc)) from exc
