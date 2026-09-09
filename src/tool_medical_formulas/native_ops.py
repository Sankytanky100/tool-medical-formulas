"""
Slim-image operations (no monorepo ``laer.bundles``).

Supports deterministic calculate paths via vendored formula engine.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

NATIVE_OPERATION_IDS: frozenset[str] = frozenset(
    {
        "calculate_formula",
        "compute_medical_formula",
    }
)


def _calculator():
    from tool_medical_formulas.formula_corpus import resolve_formula_corpus_dir
    from tool_medical_formulas.engine.deterministic_calculator import DeterministicCalculator

    return DeterministicCalculator(formulas_dir=str(resolve_formula_corpus_dir()), formula_service=None)


async def execute_native(
    operation_id: str,
    inputs: dict[str, Any],
    user_context: dict[str, Any],
) -> dict[str, Any] | None:
    op = (operation_id or "").strip()
    if op not in NATIVE_OPERATION_IDS:
        return None

    formula_id = str(inputs.get("formula_id") or "").strip()
    if not formula_id:
        return {"status": "validation_failed", "error": "formula_id_required", "operation": op}

    nested = inputs.get("inputs")
    calc_inputs: dict[str, Any] = (
        {"formula_id": formula_id, **nested}
        if isinstance(nested, dict) and nested
        else dict(inputs)
    )

    from tool_medical_formulas.engine.ckd_epi_creatinine import (
        is_ckd_epi_formula_id,
        resolve_ckd_epi_formula_id,
        try_calculate_ckd_epi_from_inputs,
    )

    if is_ckd_epi_formula_id(formula_id):
        resolved = resolve_ckd_epi_formula_id(formula_id) or formula_id

        def _ckd() -> dict[str, Any]:
            payload = {k: v for k, v in calc_inputs.items() if k != "formula_id"}
            out = try_calculate_ckd_epi_from_inputs(payload)
            out["formula_id"] = resolved
            return out

        raw = await asyncio.to_thread(_ckd)
        if op == "compute_medical_formula":
            if raw.get("status") in ("validation_failed", "calculation_failed") or raw.get("error"):
                return {
                    "status": raw.get("status") or "error",
                    "formula_id": resolved,
                    "error": raw.get("error"),
                    "missing_fields": raw.get("missing_fields"),
                    "raw": raw,
                    "operation": op,
                }
            return {
                "status": "success",
                "formula_id": resolved,
                "result": raw.get("result"),
                "units": raw.get("units"),
                "provenance": raw.get("provenance")
                or {"deterministic": True, "engine": "ckd_epi_2021_creatinine", "slim": True},
                "calculation": raw,
                "operation": op,
            }
        return raw

    calc = _calculator()

    def _run() -> dict[str, Any]:
        validation = calc.validate_inputs(formula_id, dict(calc_inputs))
        if not validation.get("valid"):
            return {
                "status": "validation_failed",
                "error": validation.get("error", "Validation failed"),
                "missing_fields": validation.get("missing_fields", []),
                "formula_id": formula_id,
            }
        return calc.calculate(formula_id, dict(calc_inputs))

    raw = await asyncio.to_thread(_run)
    if op == "compute_medical_formula":
        if raw.get("status") in ("validation_failed", "calculation_failed") or raw.get("error"):
            return {
                "status": raw.get("status") or "error",
                "formula_id": formula_id,
                "error": raw.get("error"),
                "missing_fields": raw.get("missing_fields"),
                "raw": raw,
                "operation": op,
            }
        return {
            "status": "success",
            "formula_id": formula_id,
            "result": raw.get("result"),
            "units": raw.get("units"),
            "provenance": {"deterministic": True, "engine": "DeterministicCalculator", "slim": True},
            "calculation": raw,
            "operation": op,
        }
    return raw
