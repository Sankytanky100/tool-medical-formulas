"""Unit tests for 2021 CKD-EPI creatinine special-case (A3.0 S1)."""

from __future__ import annotations

import pytest

from tool_medical_formulas.engine.ckd_epi_creatinine import (
    CKD_EPI_CANONICAL_ID,
    ckd_epi_2021_creatinine_egfr,
    is_ckd_epi_formula_id,
    resolve_ckd_epi_formula_id,
    try_calculate_ckd_epi_from_inputs,
)


@pytest.mark.parametrize(
    "fid",
    ["ckd_epi", "ckd-epi", "egfr", CKD_EPI_CANONICAL_ID],
)
def test_ckd_epi_aliases_resolve(fid: str) -> None:
    assert is_ckd_epi_formula_id(fid)
    assert resolve_ckd_epi_formula_id(fid) == CKD_EPI_CANONICAL_ID


def test_ckd_epi_2021_female_scr_1_8_age_68() -> None:
    # Age 68 F, Scr 1.8 → ~30 mL/min/1.73m² (G3b ballpark)
    egfr = ckd_epi_2021_creatinine_egfr(
        age_years=68.0, female=True, creatinine_mg_dl=1.8
    )
    assert 28.0 <= egfr <= 35.0


def test_try_calculate_accepts_alias_inputs() -> None:
    out = try_calculate_ckd_epi_from_inputs(
        {"age": 68, "sex": "female", "creatinine": 1.8}
    )
    assert out["status"] == "success"
    assert 28.0 <= float(out["result"]) <= 35.0
    assert out["formula_id"] == CKD_EPI_CANONICAL_ID


def test_try_calculate_missing_creatinine() -> None:
    out = try_calculate_ckd_epi_from_inputs({"age": 68, "sex": 0})
    assert out["status"] == "validation_failed"
    assert "creatinine" in (out.get("missing_fields") or [])


@pytest.mark.asyncio
async def test_native_ops_ckd_epi_alias() -> None:
    from tool_medical_formulas.native_ops import execute_native

    raw = await execute_native(
        "calculate_formula",
        {
            "formula_id": "ckd_epi",
            "inputs": {"age": 68, "sex": "female", "creatinine": 1.8},
        },
        {},
    )
    assert raw is not None
    assert raw.get("status") == "success"
    assert 28.0 <= float(raw["result"]) <= 35.0
