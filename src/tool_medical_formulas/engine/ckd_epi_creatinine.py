"""2021 CKD-EPI creatinine eGFR (Inker et al.) — deterministic special-case.

The scraped corpus entry ``ckd-epi-equations-for-glomerular-filtration-rate-gfr``
only exposes equation/sex/age (no Scr) and evaluates to a nonsense coefficient.
S1 / generic clinician asks need a correct eGFR when age, sex, and creatinine
are supplied in the tool call.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional

CKD_EPI_CANONICAL_ID = "ckd-epi-equations-for-glomerular-filtration-rate-gfr"

CKD_EPI_ALIASES = frozenset(
    {
        "ckd_epi",
        "ckd-epi",
        "ckd_epi_2021",
        "ckd-epi-2021",
        "egfr",
        "egfr_ckd_epi",
        "egfr-ckd-epi",
        CKD_EPI_CANONICAL_ID,
    }
)


def is_ckd_epi_formula_id(formula_id: str) -> bool:
    raw = (formula_id or "").strip().lower().replace("_", "-")
    if not raw:
        return False
    if raw in {a.replace("_", "-") for a in CKD_EPI_ALIASES}:
        return True
    return "ckd-epi" in raw and ("gfr" in raw or "glomerular" in raw)


def resolve_ckd_epi_formula_id(formula_id: str) -> Optional[str]:
    if is_ckd_epi_formula_id(formula_id):
        return CKD_EPI_CANONICAL_ID
    return None


def _to_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip().lower()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _is_female(sex: Any) -> bool:
    if sex is None:
        raise ValueError("sex_required")
    if isinstance(sex, (int, float)):
        # Corpus options: 0=Female, 1=Male
        return float(sex) < 0.5
    s = str(sex).strip().lower()
    if s in ("0", "f", "female", "woman", "w"):
        return True
    if s in ("1", "m", "male", "man"):
        return False
    raise ValueError(f"sex_unrecognized:{sex!r}")


def _creatinine_mg_dl(inputs: Mapping[str, Any]) -> Optional[float]:
    for key in (
        "creatinine",
        "serum_creatinine",
        "scr",
        "s_cr",
        "cr",
        "creat",
    ):
        if key in inputs:
            v = _to_float(inputs.get(key))
            if v is not None:
                return v
    return None


def ckd_epi_2021_creatinine_egfr(
    *,
    age_years: float,
    female: bool,
    creatinine_mg_dl: float,
) -> float:
    """Return eGFR mL/min/1.73m² (2021 CKD-EPI creatinine, race-free)."""
    age = float(age_years)
    scr = float(creatinine_mg_dl)
    if age <= 0 or age > 120:
        raise ValueError("age_out_of_range")
    if scr <= 0 or scr > 30:
        raise ValueError("creatinine_out_of_range")

    if female:
        kappa, alpha, sex_factor = 0.7, -0.241, 1.012
    else:
        kappa, alpha, sex_factor = 0.9, -0.302, 1.0

    scr_k = scr / kappa
    egfr = (
        142.0
        * (min(scr_k, 1.0) ** alpha)
        * (max(scr_k, 1.0) ** -1.200)
        * (0.9938 ** age)
        * sex_factor
    )
    return round(float(egfr), 1)


def try_calculate_ckd_epi_from_inputs(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """
    Attempt 2021 CKD-EPI creatinine from a loose inputs dict.

    Returns a calculator-shaped success dict, or ``validation_failed`` with
    ``missing_fields`` / ``error`` (never invents eGFR without Scr).
    """
    age = _to_float(inputs.get("age") or inputs.get("age_years"))
    creat = _creatinine_mg_dl(inputs)
    missing: list[str] = []
    if age is None:
        missing.append("age")
    if creat is None:
        missing.append("creatinine")
    sex_raw = inputs.get("sex")
    if sex_raw is None:
        sex_raw = inputs.get("gender")
    if sex_raw is None:
        missing.append("sex")
    if missing:
        return {
            "status": "validation_failed",
            "error": "missing_required_inputs",
            "missing_fields": missing,
            "formula_id": CKD_EPI_CANONICAL_ID,
        }
    try:
        female = _is_female(sex_raw)
        egfr = ckd_epi_2021_creatinine_egfr(
            age_years=float(age),
            female=female,
            creatinine_mg_dl=float(creat),
        )
    except ValueError as exc:
        return {
            "status": "validation_failed",
            "error": str(exc),
            "missing_fields": [],
            "formula_id": CKD_EPI_CANONICAL_ID,
        }

    return {
        "status": "success",
        "result": egfr,
        "units": "mL/min/1.73m2",
        "formula_id": CKD_EPI_CANONICAL_ID,
        "formula_name": "CKD-EPI Equations for Glomerular Filtration Rate (GFR)",
        "validated_inputs": {
            "age": age,
            "sex": "female" if female else "male",
            "creatinine": creat,
            "equation": "2021 CKD-EPI Creatinine",
        },
        "calculation_type": "mathematical",
        "provenance": {
            "deterministic": True,
            "engine": "ckd_epi_2021_creatinine",
            "guideline": "Inker et al. 2021 (race-free)",
        },
        "interpretation": (
            "CKD G3a (mild-moderate)"
            if 45 <= egfr < 60
            else "CKD G3b (moderate-severe)"
            if 30 <= egfr < 45
            else "CKD G4 (severe)"
            if 15 <= egfr < 30
            else "CKD G5 (kidney failure)"
            if egfr < 15
            else "CKD G2 (mild)"
            if 60 <= egfr < 90
            else "CKD G1 (normal/high)"
            if egfr >= 90
            else None
        ),
    }
