"""
LLM-backed TAP presence (E.3b): retrieval + structured Gemini compose.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from tool_medical_formulas.tap.config import TapBudget
from tool_medical_formulas.tap.corpus_vector_index import FormulaHit, get_corpus_index
from tool_medical_formulas.tap.llm_client import TapLlmError, generate_tap_json
from tool_medical_formulas.tap.llm_schemas import (
    BUILD_FORM_SCHEMA,
    ROUTE_REQUEST_SCHEMA,
    SUGGEST_PREFILL_SCHEMA,
)
from tool_medical_formulas.tap.prompt_loader import load_prompt
from tool_medical_formulas.tap.ui_schema_composer import (
    compose_ui_schema_from_formula,
    compose_ui_schema_from_llm_fields,
    validate_ui_schema_dict,
)

logger = logging.getLogger(__name__)


def _load_formula_spec(formula_id: str) -> dict[str, Any] | None:
    from tool_medical_formulas.tap import presence_agent as det

    return det._load_formula_spec(formula_id)


def _resolve_formula_id(context: dict[str, Any]) -> str:
    from tool_medical_formulas.tap import presence_agent as det

    return det._resolve_formula_id(context)


def _metadata(started: float, *, tokens: int, model: str, normalizations: list[str]) -> dict[str, Any]:
    from tool_medical_formulas.tap import presence_agent as det

    meta = det._metadata(started, tokens=tokens)
    meta["model_used"] = model
    meta["composer_normalizations"] = list(normalizations)
    return meta


def _retrieval_block(hits: list[FormulaHit]) -> str:
    lines = []
    for h in hits:
        lines.append(f"- {h.formula_id} (score={h.score:.2f}): {h.name} — {h.snippet[:200]}")
    return "\n".join(lines) or "(no hits)"


async def handle_tap_converse(envelope: dict[str, Any]) -> dict[str, Any]:
    purpose = str(envelope.get("purpose") or "build_form")
    ctx = dict(envelope.get("context") or {})
    budget = TapBudget.from_envelope(envelope)

    if purpose in ("interpret_inputs", "explain"):
        from tool_medical_formulas.tap import presence_agent as det

        return await det._handle_deterministic(envelope)
    if purpose == "route_request":
        return await _route_request(ctx, budget)
    if purpose == "suggest_prefill":
        return await _suggest_prefill(ctx, budget)
    if purpose == "build_form":
        return await _build_form(ctx, budget)
    raise HTTPException(status_code=400, detail="tap_invalid_purpose")


async def _build_form(ctx: dict[str, Any], budget: TapBudget) -> dict[str, Any]:
    started = time.monotonic()
    formula_id = _resolve_formula_id(ctx)
    index = get_corpus_index()
    query = str(ctx.get("user_query") or formula_id or "clinical score")
    hits = index.search(query, top_k=5)

    if not formula_id and hits:
        formula_id = hits[0].formula_id

    spec = _load_formula_spec(formula_id) if formula_id else None
    if spec is None:
        return {
            "tap_version": 1,
            "purpose_served": "build_form",
            "result": {"error": "formula_not_found", "formula_id": formula_id},
            "metadata": _metadata(started, tokens=0, model="llm-tap", normalizations=[]),
        }

    # Fast path: full metadata → deterministic compose (hybrid E.3b)
    from tool_medical_formulas.tap import presence_agent as det

    if det._spec_has_full_metadata(spec):
        hints = det._prefill_hints_from_spec(spec)
        hints = _merge_patient_atom_hints(hints, ctx, spec)
        ui_schema = compose_ui_schema_from_formula(spec, formula_id=formula_id, prefill_hints=hints)
        return {
            "tap_version": 1,
            "purpose_served": "build_form",
            "result": {
                "ui_schema": ui_schema,
                "operation_id": str(ctx.get("operation_id") or "compute_medical_formula"),
                "operation_inputs_template": {"formula_id": formula_id},
                "confidence": 0.97,
                "rationale_excerpt": "Hybrid: corpus metadata complete; deterministic compose.",
            },
            "metadata": _metadata(
                started, tokens=0, model="deterministic-composer", normalizations=["hybrid_fast_path"]
            ),
        }

    system = load_prompt("build_form")
    user = _build_form_user_message(ctx, formula_id=formula_id, spec=spec, hits=hits)

    try:
        parsed, usage = await generate_tap_json(
            system_instruction=system,
            user_message=user,
            response_schema=BUILD_FORM_SCHEMA,
            budget=budget,
        )
    except TapLlmError as exc:
        logger.warning("LLM build_form failed, deterministic fallback: %s", exc)
        return await det._build_form_deterministic(ctx, started, formula_id, spec)

    fields = parsed.get("fields") if isinstance(parsed.get("fields"), list) else []
    normalizations: list[str] = []
    try:
        ui_schema, normalizations = compose_ui_schema_from_llm_fields(
            fields,
            formula_spec=spec,
            formula_id=formula_id,
            section_title=str(parsed.get("section_title") or "Inputs"),
        )
        ui_schema = validate_ui_schema_dict(ui_schema)
    except Exception as exc:
        logger.warning("LLM ui_schema validation failed: %s", exc)
        return await det._build_form_deterministic(ctx, started, formula_id, spec)

    followups = parsed.get("suggested_followups")
    if not isinstance(followups, list):
        followups = []

    return {
        "tap_version": 1,
        "purpose_served": "build_form",
        "result": {
            "ui_schema": ui_schema,
            "operation_id": str(ctx.get("operation_id") or "compute_medical_formula"),
            "operation_inputs_template": {"formula_id": formula_id},
            "confidence": float(parsed.get("confidence") or 0.85),
            "rationale_excerpt": str(parsed.get("rationale_excerpt") or "")[:2000],
            "suggested_followups": [str(x) for x in followups[:5]],
        },
        "metadata": _metadata(
            started,
            tokens=int(usage.get("tokens_consumed") or 0),
            model=str(usage.get("model_used") or "llm-tap"),
            normalizations=normalizations,
        ),
    }


async def _route_request(ctx: dict[str, Any], budget: TapBudget) -> dict[str, Any]:
    started = time.monotonic()
    index = get_corpus_index()
    uq = str(ctx.get("user_query") or "")
    hits = index.search(uq or "formula", top_k=8)

    system = load_prompt("route_request")
    user = (
        f"User query:\n{uq}\n\n"
        f"Retrieved formulas:\n{_retrieval_block(hits)}\n\n"
        f"Patient atom summary:\n{json.dumps(ctx.get('patient_atom_summary') or {}, indent=2)[:4000]}"
    )

    try:
        parsed, usage = await generate_tap_json(
            system_instruction=system,
            user_message=user,
            response_schema=ROUTE_REQUEST_SCHEMA,
            budget=budget,
        )
    except TapLlmError:
        from tool_medical_formulas.tap import presence_agent as det

        return det._route_request(ctx, started)

    fid = str(parsed.get("formula_id") or (hits[0].formula_id if hits else ""))
    return {
        "tap_version": 1,
        "purpose_served": "route_request",
        "result": {
            "operation_id": str(parsed.get("operation_id") or "compute_medical_formula"),
            "operation_inputs_template": {"formula_id": fid},
            "confidence": float(parsed.get("confidence") or 0.8),
            "rationale_excerpt": str(parsed.get("rationale_excerpt") or "")[:2000],
            "alternatives": parsed.get("alternatives") or [],
        },
        "metadata": _metadata(
            started,
            tokens=int(usage.get("tokens_consumed") or 0),
            model=str(usage.get("model_used") or "llm-tap"),
            normalizations=[],
        ),
    }


async def _suggest_prefill(ctx: dict[str, Any], budget: TapBudget) -> dict[str, Any]:
    started = time.monotonic()
    formula_id = _resolve_formula_id(ctx)
    spec = _load_formula_spec(formula_id) if formula_id else None
    if spec is None:
        from tool_medical_formulas.tap import presence_agent as det

        return det._suggest_prefill(ctx, started)

    system = load_prompt("suggest_prefill")
    user = (
        f"Formula: {formula_id}\n"
        f"Spec inputs:\n{json.dumps(spec.get('inputs') or [], indent=2)[:6000]}\n\n"
        f"Patient atom summary:\n{json.dumps(ctx.get('patient_atom_summary') or {}, indent=2)[:4000]}"
    )

    try:
        parsed, usage = await generate_tap_json(
            system_instruction=system,
            user_message=user,
            response_schema=SUGGEST_PREFILL_SCHEMA,
            budget=budget,
        )
    except TapLlmError:
        from tool_medical_formulas.tap import presence_agent as det

        return det._suggest_prefill(ctx, started)

    suggestions = parsed.get("suggestions") if isinstance(parsed.get("suggestions"), list) else []
    return {
        "tap_version": 1,
        "purpose_served": "suggest_prefill",
        "result": {
            "suggestions": suggestions,
            "formula_id": formula_id,
            "rationale_excerpt": str(parsed.get("rationale_excerpt") or "")[:1000],
        },
        "metadata": _metadata(
            started,
            tokens=int(usage.get("tokens_consumed") or 0),
            model=str(usage.get("model_used") or "llm-tap"),
            normalizations=[],
        ),
    }


def _build_form_user_message(
    ctx: dict[str, Any],
    *,
    formula_id: str,
    spec: dict[str, Any],
    hits: list[FormulaHit],
) -> str:
    return (
        f"Target formula_id: {formula_id}\n\n"
        f"Formula spec (authoritative inputs):\n{json.dumps(spec, indent=2)[:12000]}\n\n"
        f"User query: {ctx.get('user_query') or ''}\n\n"
        f"Patient atom summary:\n{json.dumps(ctx.get('patient_atom_summary') or {}, indent=2)[:4000]}\n\n"
        f"Conversation excerpt:\n{str(ctx.get('conversation_excerpt') or '')[:2000]}\n\n"
        f"Related corpus hits:\n{_retrieval_block(hits)}"
    )


def _merge_patient_atom_hints(
    hints: dict[str, str],
    ctx: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, str]:
    """Keep deterministic hints; LLM suggest_prefill can refine in full LLM path."""
    _ = ctx, spec
    return hints
