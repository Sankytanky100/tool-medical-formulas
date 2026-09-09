"""Slim-mode native medical formula operations."""

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.asyncio
async def test_native_calculate_requires_formula_id(monkeypatch):
    monkeypatch.setenv("TOOL_MEDICAL_FORMULAS_SLIM", "1")
    from tool_medical_formulas.native_ops import execute_native

    out = await execute_native("calculate_formula", {}, {})
    assert out is not None
    assert out.get("status") == "validation_failed"


@pytest.mark.asyncio
async def test_slim_runtime_lists_native_ops_only(monkeypatch):
    monkeypatch.setenv("TOOL_MEDICAL_FORMULAS_SLIM", "1")
    from tool_medical_formulas.runtime import list_implemented_operations

    ops = list_implemented_operations({})
    assert "calculate_formula" in ops
    assert "identify_medical_formulas" not in ops
