"""Golden tool operation fixtures (playbook §7 R-LP-9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = _ROOT / "tests" / "fixtures" / "tool_golden"


@pytest.mark.asyncio
async def test_golden_medical_formulas_calculate(monkeypatch):
    monkeypatch.setenv("TOOL_MEDICAL_FORMULAS_SLIM", "1")
    path = FIXTURES / "medical_formulas_calculate_chads2.json"
    if not path.is_file():
        pytest.skip("fixture missing")
    spec = json.loads(path.read_text(encoding="utf-8"))
    from tool_medical_formulas.native_ops import execute_native

    out = await execute_native(
        spec["operation_id"],
        dict(spec["inputs"]),
        {"plan": "enterprise"},
    )
    assert out is not None
    for key in spec["expect"].get("status_keys", []):
        assert key in out or key in (out.get("calculation") or {})
