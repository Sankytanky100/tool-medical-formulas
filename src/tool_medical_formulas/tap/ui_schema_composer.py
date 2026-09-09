"""
ui_schema composer for formula inputs (GENERATIVE_UI_PLAN E.3 / E.3b).

Deterministic path from corpus JSON; LLM path normalizes model output then validates.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_ALLOWED_RENDERERS = frozenset(
    {
        "text",
        "textarea",
        "number",
        "toggle",
        "segmented",
        "select",
        "multiselect",
        "slider",
        "date",
        "voice_text",
        "image_upload",
        "scribble",
        "body_diagram",
        "lab_table",
        "file_link",
    }
)


def _renderer_for_input(inp: dict[str, Any]) -> str:
    opts = inp.get("options")
    if isinstance(opts, list) and len(opts) >= 2:
        if len(opts) <= 5:
            return "segmented"
        return "select"
    t = str(inp.get("type") or "number").lower()
    if t in ("boolean", "toggle"):
        return "toggle"
    if t in ("text", "string"):
        return "text"
    return "number"


def _enum_from_options(opts: list[Any]) -> list[str]:
    out: list[str] = []
    for o in opts:
        if isinstance(o, dict):
            out.append(str(o.get("value", o.get("label", ""))))
        else:
            out.append(str(o))
    return out


def compose_ui_schema_from_formula(
    formula_spec: dict[str, Any],
    *,
    formula_id: str,
    prefill_hints: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Build a valid ui_schema from a formula JSON spec (no LLM).
    """
    hints = dict(prefill_hints or {})
    fields: list[dict[str, Any]] = []
    for inp in formula_spec.get("inputs") or []:
        if not isinstance(inp, dict):
            continue
        fid = str(inp.get("id") or inp.get("name") or "").strip()
        if not fid:
            continue
        renderer = _renderer_for_input(inp)
        field: dict[str, Any] = {
            "field": fid,
            "label": str(inp.get("label") or fid),
            "renderer": renderer,
            "required": bool(inp.get("required", True)),
        }
        if inp.get("unit"):
            field["units"] = str(inp["unit"])
        opts = inp.get("options")
        if isinstance(opts, list) and renderer in ("segmented", "select", "multiselect"):
            field["enum"] = _enum_from_options(opts)
        hint = hints.get(fid)
        if hint:
            field["prefill_from_atom"] = {"field_id": hint, "strategy": "most_recent"}
        fields.append(field)

    return {
        "title": str(formula_spec.get("name") or formula_id),
        "description": str(formula_spec.get("description") or "")[:4000] or None,
        "sections": [{"title": "Inputs", "fields": fields}],
    }


def _normalize_renderer(raw: str, inp: dict[str, Any] | None) -> str:
    r = (raw or "").strip().lower()
    if r in _ALLOWED_RENDERERS:
        return r
    if inp is not None:
        return _renderer_for_input(inp)
    return "text"


def compose_ui_schema_from_llm_fields(
    llm_fields: list[Any],
    *,
    formula_spec: dict[str, Any],
    formula_id: str,
    section_title: str = "Inputs",
) -> tuple[dict[str, Any], list[str]]:
    """
    Merge LLM field proposals with authoritative formula spec; return (ui_schema, normalization notes).
    """
    spec_by_id: dict[str, dict[str, Any]] = {}
    for inp in formula_spec.get("inputs") or []:
        if isinstance(inp, dict):
            fid = str(inp.get("id") or inp.get("name") or "").strip()
            if fid:
                spec_by_id[fid] = inp

    normalizations: list[str] = []
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()

    for raw in llm_fields:
        if not isinstance(raw, dict):
            continue
        fid = str(raw.get("field") or raw.get("id") or "").strip()
        if not fid or fid in seen:
            continue
        seen.add(fid)
        spec_inp = spec_by_id.get(fid)
        renderer = _normalize_renderer(str(raw.get("renderer") or ""), spec_inp)
        if renderer != str(raw.get("renderer") or "").strip().lower():
            normalizations.append("renderer_corrected")

        field: dict[str, Any] = {
            "field": fid,
            "label": str(raw.get("label") or (spec_inp or {}).get("label") or fid),
            "renderer": renderer,
            "required": bool(
                raw.get("required", (spec_inp or {}).get("required", True))
            ),
        }
        unit = raw.get("units") or (spec_inp or {}).get("unit")
        if unit:
            field["units"] = str(unit)
        desc = raw.get("description") or (spec_inp or {}).get("help_text")
        if desc:
            field["description"] = str(desc)[:2000]
        enum = raw.get("enum")
        if isinstance(enum, list) and enum:
            field["enum"] = [str(x) for x in enum]
        elif spec_inp and isinstance(spec_inp.get("options"), list):
            field["enum"] = _enum_from_options(spec_inp["options"])
            if renderer not in ("segmented", "select", "multiselect"):
                field["renderer"] = _renderer_for_input(spec_inp)
                normalizations.append("enum_renderer_sync")

        pre = raw.get("prefill_from_atom")
        if isinstance(pre, dict) and pre.get("field_id"):
            field["prefill_from_atom"] = {
                "field_id": str(pre["field_id"]).lstrip("$."),
                "strategy": str(pre.get("strategy") or "most_recent"),
            }
        fields.append(field)

    # Ensure every required spec input appears
    for fid, spec_inp in spec_by_id.items():
        if fid in seen:
            continue
        if bool(spec_inp.get("required", True)):
            renderer = _renderer_for_input(spec_inp)
            added: dict[str, Any] = {
                "field": fid,
                "label": str(spec_inp.get("label") or fid),
                "renderer": renderer,
                "required": True,
            }
            if isinstance(spec_inp.get("options"), list):
                added["enum"] = _enum_from_options(spec_inp["options"])
            unit = spec_inp.get("unit") or spec_inp.get("units")
            if unit:
                added["units"] = str(unit)
            fields.append(added)
            normalizations.append("missing_field_added_from_spec")

    schema = {
        "title": str(formula_spec.get("name") or formula_id),
        "description": str(formula_spec.get("description") or "")[:4000] or None,
        "sections": [{"title": section_title, "fields": fields}],
    }
    return schema, normalizations


def validate_ui_schema_dict(schema: dict[str, Any]) -> dict[str, Any]:
    """Pydantic validate against laer_platform UiSchema; raises on invalid renderers."""
    import sys

    from tool_medical_formulas.monorepo_path import ensure_monorepo_root

    root = ensure_monorepo_root()
    if root is not None:
        plat = root / "_packaging" / "laer-platform" / "src"
        if str(plat) not in sys.path:
            sys.path.insert(0, str(plat))

    from laer_platform.schema.ui_schema import UiSchema

    validated = UiSchema.model_validate(schema)
    return validated.model_dump(mode="json", exclude_none=True)
