"""
Second-bundle TAP stub — clinical intake scores (checkpoint-2).

Serves ``clinical_intake_tap`` bundle id with deterministic ``build_form`` for
generic intake fields (BMI-style). Proves multi-tool TAP routing on one service.
"""

from __future__ import annotations

import time
from typing import Any


def _metadata(started: float, *, tokens: int = 0) -> dict[str, Any]:
    return {
        "tap_latency_ms": int((time.monotonic() - started) * 1000),
        "tokens_used": tokens,
        "model": "deterministic-clinical-intake",
    }


def handle_clinical_intake_tap(body: dict[str, Any]) -> dict[str, Any]:
    purpose = str(body.get("purpose") or "").strip()
    ctx = dict(body.get("context") or {})
    started = time.monotonic()

    if purpose not in (
        "build_form",
        "route_request",
        "interpret_inputs",
        "explain",
        "suggest_prefill",
    ):
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="tap_invalid_purpose")

    if purpose == "build_form":
        score_type = str(
            (ctx.get("operation_inputs_hint") or {}).get("score_type") or "general_intake"
        )
        return {
            "tap_version": 1,
            "purpose_served": "build_form",
            "result": {
                "ui_schema": {
                    "title": f"Clinical intake — {score_type.replace('_', ' ').title()}",
                    "sections": [
                        {
                            "title": "Measurements",
                            "fields": [
                                {
                                    "field": "weight_kg",
                                    "label": "Weight (kg)",
                                    "renderer": "number",
                                    "required": True,
                                },
                                {
                                    "field": "height_cm",
                                    "label": "Height (cm)",
                                    "renderer": "number",
                                    "required": True,
                                },
                                {
                                    "field": "activity_level",
                                    "label": "Activity level",
                                    "renderer": "segmented",
                                    "required": True,
                                    "enum": ["low", "moderate", "high"],
                                },
                            ],
                        }
                    ],
                },
                "operation_inputs_template": {
                    "score_type": score_type,
                },
                "confidence": 0.92,
            },
            "metadata": _metadata(started, tokens=35),
        }

    if purpose == "route_request":
        return {
            "tap_version": 1,
            "purpose_served": "route_request",
            "result": {
                "operation_id": "collect_intake",
                "operation_inputs_template": {"score_type": "general_intake"},
                "confidence": 0.85,
            },
            "metadata": _metadata(started, tokens=10),
        }

    if purpose == "suggest_prefill":
        draft = ctx.get("ui_schema_draft") or ctx.get("ui_schema") or {}
        return {
            "tap_version": 1,
            "purpose_served": "suggest_prefill",
            "result": {
                "enriched_ui_schema": draft,
                "mappings_added": 0,
                "mappings_skipped": [],
            },
            "metadata": _metadata(started, tokens=5),
        }

    if purpose == "interpret_inputs":
        utterance = str(ctx.get("user_utterance") or ctx.get("user_query") or "")
        extracted: dict[str, Any] = {}
        if "kg" in utterance.lower():
            import re

            m = re.search(r"(\d{2,3})\s*kg", utterance.lower())
            if m:
                extracted["weight_kg"] = int(m.group(1))
        return {
            "tap_version": 1,
            "purpose_served": "interpret_inputs",
            "result": {
                "extracted_inputs": extracted,
                "unresolved_inputs": [],
                "confidence_per_input": {k: 0.8 for k in extracted},
                "next_questions_for_clinician": [],
            },
            "metadata": _metadata(started, tokens=20),
        }

    # explain
    return {
        "tap_version": 1,
        "purpose_served": "explain",
        "result": {
            "title": "Clinical intake TAP",
            "summary_md": "Collects weight, height, and activity for risk scores.",
            "citations": [],
        },
        "metadata": _metadata(started, tokens=15),
    }
