"""
Execute medical_formulas manifest operations (TEP-4 slim-only).

Packaged tool images never import ``laer.bundles`` — native deterministic ops only.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from tool_medical_formulas.native_ops import NATIVE_OPERATION_IDS, execute_native

logger = logging.getLogger(__name__)

OPERATION_IDS: frozenset[str] = frozenset(
    {
        "identify_medical_formulas",
        "identify_formula",
        "extract_formula_inputs_from_query",
        "collect_inputs_for_formula",
        "compute_medical_formula",
        "calculate_formula",
        "interpret_medical_formula_result",
        "analyze_result",
        "generate_report",
        "get_formula_candidates",
        "store_formula_candidates",
        "select_formula",
        "clear_formula_selection",
    }
)


def slim_mode() -> bool:
    """Prod images set ``TOOL_MEDICAL_FORMULAS_SLIM=1``. Non-slim monorepo bridge retired (TEP-4)."""
    return (os.getenv("TOOL_MEDICAL_FORMULAS_SLIM") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def execute_operation(
    operation_id: str,
    inputs: dict[str, Any],
    user_context: dict[str, Any],
) -> dict[str, Any]:
    op = (operation_id or "").strip()
    if op not in OPERATION_IDS:
        raise KeyError(f"unknown_operation:{op}")

    native = await execute_native(op, dict(inputs or {}), user_context)
    if native is not None:
        return native
    raise RuntimeError(f"operation_not_available_in_slim_mode:{op}")


def list_implemented_operations(user_context: dict[str, Any]) -> list[str]:
    del user_context
    return sorted(NATIVE_OPERATION_IDS)
