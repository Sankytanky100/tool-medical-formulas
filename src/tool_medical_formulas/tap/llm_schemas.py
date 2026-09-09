"""JSON schemas for Gemini structured output (TAP E.3b)."""

from __future__ import annotations

from typing import Any

BUILD_FORM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "label": {"type": "string"},
                    "renderer": {"type": "string"},
                    "required": {"type": "boolean"},
                    "units": {"type": "string"},
                    "enum": {"type": "array", "items": {"type": "string"}},
                    "description": {"type": "string"},
                    "prefill_from_atom": {
                        "type": "object",
                        "properties": {
                            "field_id": {"type": "string"},
                            "strategy": {"type": "string"},
                        },
                    },
                },
                "required": ["field", "label", "renderer"],
            },
        },
        "rationale_excerpt": {"type": "string"},
        "confidence": {"type": "number"},
        "suggested_followups": {"type": "array", "items": {"type": "string"}},
        "section_title": {"type": "string"},
    },
    "required": ["fields", "rationale_excerpt", "confidence"],
}

ROUTE_REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "formula_id": {"type": "string"},
        "operation_id": {"type": "string"},
        "confidence": {"type": "number"},
        "rationale_excerpt": {"type": "string"},
        "alternatives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "formula_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
    "required": ["formula_id", "operation_id", "confidence", "rationale_excerpt"],
}

SUGGEST_PREFILL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "prefill_field_id": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["field", "prefill_field_id"],
            },
        },
        "rationale_excerpt": {"type": "string"},
    },
    "required": ["suggestions"],
}
