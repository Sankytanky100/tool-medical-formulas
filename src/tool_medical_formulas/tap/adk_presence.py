"""
ADK Runner path for TAP (E.3b).

Uses ``google.adk`` ``LlmAgent`` + ``Runner`` for audit-compatible agent execution.
Falls back to :mod:`llm_presence` on failure.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from tool_medical_formulas.tap.config import TapBudget, tap_model_id
from tool_medical_formulas.tap.prompt_loader import load_prompt

logger = logging.getLogger(__name__)


async def handle_tap_converse(envelope: dict[str, Any]) -> dict[str, Any]:
    purpose = str(envelope.get("purpose") or "build_form")
    if purpose not in ("build_form", "route_request", "suggest_prefill"):
        from tool_medical_formulas.tap import llm_presence

        return await llm_presence.handle_tap_converse(envelope)

    started = time.monotonic()
    try:
        return await _run_adk(purpose, envelope, TapBudget.from_envelope(envelope), started)
    except Exception as exc:
        logger.warning("ADK TAP failed, falling back to llm_presence: %s", exc)
        from tool_medical_formulas.tap import llm_presence

        return await llm_presence.handle_tap_converse(envelope)


async def _run_adk(
    purpose: str,
    envelope: dict[str, Any],
    budget: TapBudget,
    started: float,
) -> dict[str, Any]:
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    ctx = dict(envelope.get("context") or {})
    system = load_prompt(purpose if purpose != "build_form" else "build_form")
    instruction = (
        f"{system}\n\n"
        "Respond with a single JSON object only. No markdown fences."
    )
    model = tap_model_id()

    agent = LlmAgent(
        name="medical_formulas_tap",
        model=model,
        instruction=instruction,
    )
    session_service = InMemorySessionService()
    runner = Runner(
        agent=agent,
        app_name="tool_medical_formulas_tap",
        session_service=session_service,
    )
    user_id = "tap"
    session_id = str(uuid.uuid4())
    await session_service.create_session(
        app_name="tool_medical_formulas_tap",
        user_id=user_id,
        session_id=session_id,
    )
    user_text = json.dumps(
        {"purpose": purpose, "context": ctx, "budget": {"max_tokens": budget.max_tokens}},
        indent=2,
    )[:24000]
    new_message = types.Content(
        role="user",
        parts=[types.Part(text=user_text)],
    )

    final_text = ""
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=new_message,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    final_text += part.text

    from tool_medical_formulas.tap.llm_client import _parse_json_text

    parsed = _parse_json_text(final_text)
    return _shape_adk_response(purpose, parsed, ctx, started, model=model)


def _shape_adk_response(
    purpose: str,
    parsed: dict[str, Any],
    ctx: dict[str, Any],
    started: float,
    *,
    model: str,
) -> dict[str, Any]:
    from tool_medical_formulas.tap import presence_agent as det

    if purpose == "build_form":
        formula_id = str(parsed.get("formula_id") or det._resolve_formula_id(ctx))
        spec = det._load_formula_spec(formula_id) if formula_id else None
        fields = parsed.get("fields") if isinstance(parsed.get("fields"), list) else []
        if spec and fields:
            from tool_medical_formulas.tap.ui_schema_composer import (
                compose_ui_schema_from_llm_fields,
                validate_ui_schema_dict,
            )

            ui_schema, norms = compose_ui_schema_from_llm_fields(
                fields, formula_spec=spec, formula_id=formula_id
            )
            ui_schema = validate_ui_schema_dict(ui_schema)
            return {
                "tap_version": 1,
                "purpose_served": "build_form",
                "result": {
                    "ui_schema": ui_schema,
                    "operation_id": str(ctx.get("operation_id") or "compute_medical_formula"),
                    "operation_inputs_template": {"formula_id": formula_id},
                    "confidence": float(parsed.get("confidence") or 0.82),
                    "rationale_excerpt": str(parsed.get("rationale_excerpt") or "ADK TAP")[:2000],
                },
                "metadata": det._metadata(
                    started,
                    tokens=int(parsed.get("tokens_consumed") or 800),
                )
                | {"model_used": model, "composer_normalizations": norms},
            }
        if spec:
            return det._build_form_deterministic(ctx, started, formula_id, spec)

    if purpose == "route_request":
        return {
            "tap_version": 1,
            "purpose_served": "route_request",
            "result": {
                "operation_id": str(parsed.get("operation_id") or "compute_medical_formula"),
                "operation_inputs_template": {
                    "formula_id": str(parsed.get("formula_id") or "")
                },
                "confidence": float(parsed.get("confidence") or 0.8),
                "rationale_excerpt": str(parsed.get("rationale_excerpt") or "")[:2000],
            },
            "metadata": det._metadata(started, tokens=400) | {"model_used": model},
        }

    if purpose == "suggest_prefill":
        return {
            "tap_version": 1,
            "purpose_served": "suggest_prefill",
            "result": {
                "suggestions": parsed.get("suggestions") or [],
                "formula_id": det._resolve_formula_id(ctx),
            },
            "metadata": det._metadata(started, tokens=200) | {"model_used": model},
        }

    return {
        "tap_version": 1,
        "purpose_served": purpose,
        "result": parsed,
        "metadata": det._metadata(started, tokens=0) | {"model_used": model},
    }
