"""
Clinical unit conversion (Wave A R17 / D-9 / F-R20).

Uses Pint for dimensionally consistent conversions. Curated analytes when
``substance`` is set on a formula input include **glucose**, **creatinine**,
**BUN** (mg/dL ↔ urea mmol/L), **HbA1c** (% ↔ mmol/mol IFCC), **cholesterol**
(total/LDL/HDL), **triglycerides**, **albumin** (g/dL ↔ g/L), and **common
electrolytes** (mmol/L ↔ mEq/L identity for monovalent ions). Others fall back to
Pint where dimensions allow.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Informal / legacy strings → Pint-parseable tokens
_UNIT_ALIASES: Dict[str, str] = {
    "lbs": "lb",
    "pounds": "lb",
    "lb": "lb",
    "kg": "kilogram",
    "g": "gram",
    "grams": "gram",
    "cm": "centimeter",
    "c": "degC",
    "f": "degF",
    "°c": "degC",
    "°f": "degF",
}


def _normalize_unit_token(raw: str) -> str:
    s = str(raw).strip()
    if not s:
        return s
    key = s.lower().replace(" ", "")
    return _UNIT_ALIASES.get(key, s)


@lru_cache(maxsize=1)
def _registry():
    import pint

    ureg = pint.UnitRegistry(case_sensitive=False)
    defs = Path(__file__).resolve().parent.parent / "config" / "clinical_units.txt"
    if defs.is_file():
        try:
            ureg.load_definitions(str(defs))
        except Exception as e:
            logger.warning("Could not load %s: %s", defs, e)
    return ureg


def clinical_convert(value: float, from_unit: str, to_unit: str) -> float:
    """
    Convert a scalar magnitude between compatible Pint units.

    Raises:
        ValueError: non-finite value, unknown unit, or incompatible dimensions.
    """
    if value is None or (isinstance(value, float) and (value != value)):  # NaN
        raise ValueError("value must be a finite number")
    fu = _normalize_unit_token(from_unit)
    tu = _normalize_unit_token(to_unit)
    ureg = _registry()
    try:
        q = ureg.Quantity(value, fu)
        return float(q.to(tu).magnitude)
    except Exception as e:
        raise ValueError(f"cannot convert {value!r} from {from_unit!r} to {to_unit!r}: {e}") from e


# Clinical mg/dL ↔ mmol/L factors (conventional US reporting).
_GLUCOSE_MG_PER_MMOL = 18.0182
_CREATININE_MGDL_PER_MMOLL = 88.4
# BUN (mg/dL nitrogen) ↔ blood urea (mmol/L): urea_mmol/L ≈ BUN × 0.357 (SI / ADA convention).
_BUN_MGDL_TO_UREA_MMOLL = 0.357
# Total/LDL/HDL cholesterol (mg/dL ↔ mmol/L); triglycerides use a distinct factor.
_CHOLESTEROL_MGDL_PER_MMOLL = 38.67
_TRIGLYCERIDE_MGDL_PER_MMOLL = 88.57

# HbA1c NGSP % ↔ IFCC mmol/mol (ADAG / DCCT–IFCC master equation).
_HBA1C_IFCC_SCALE = 10.929
_HBA1C_IFCC_OFFSET = 2.15


def _conc_key(unit: str) -> str:
    s = str(unit or "").strip().lower().replace(" ", "").replace("μ", "u")
    if "mg/dl" in s or s.endswith("mg/dl") or s == "mgdl":
        return "mg_dl"
    if "mmol/l" in s or s.endswith("mmol/l") or s == "mmoll":
        return "mmol_l"
    return s


def _hba1c_unit_key(unit: str) -> str:
    s = str(unit or "").strip().lower().replace(" ", "")
    if "mmol/mol" in s or "mmolmol" in s:
        return "mmol_mol"
    if "%" in s:
        return "percent"
    return s


def _protein_mass_key(unit: str) -> str:
    """g/dL vs g/L for albumin-style reporting."""
    s = str(unit or "").strip().lower().replace(" ", "").replace("μ", "u")
    if "g/dl" in s or s.endswith("g/dl"):
        return "g_dl"
    if "g/l" in s or s.endswith("g/l"):
        return "g_l"
    return ""


def _is_equiv_mmol_or_meq_per_l(unit: str) -> bool:
    """True if unit is mmol/L or mEq/L style (monovalent ions: numeric identity)."""
    s = str(unit or "").strip().lower().replace(" ", "")
    return "mmol/l" in s or "meq/l" in s or s.endswith("mmol/l") or s.endswith("meq/l")


def clinical_convert_concentration(value: float, from_unit: str, to_unit: str, substance: str) -> float:
    """
    Convert lab concentration when ``substance`` matches a curated analyte.

    Falls back to :func:`clinical_convert` when units are handled by Pint or
    the analyte is not in the curated table.
    """
    if value is None or (isinstance(value, float) and (value != value)):
        raise ValueError("value must be a finite number")
    a = str(substance or "").strip().lower()
    fk = _conc_key(from_unit)
    tk = _conc_key(to_unit)
    pk = _protein_mass_key(from_unit)
    pk_to = _protein_mass_key(to_unit)
    if fk == tk:
        if _hba1c_unit_key(from_unit) == _hba1c_unit_key(to_unit):
            return float(value)

    # Albumin (and similar serum proteins): g/dL ↔ g/L only for F-R20.
    if a in ("albumin", "alb", "serum_albumin"):
        if pk == "g_dl" and pk_to == "g_l":
            return float(value) * 10.0
        if pk == "g_l" and pk_to == "g_dl":
            return float(value) / 10.0

    # Monovalent electrolytes: mmol/L and mEq/L are numerically equal for Na⁺, K⁺, Cl⁻.
    if a in (
        "sodium",
        "na",
        "potassium",
        "k",
        "chloride",
        "cl",
        "bicarbonate",
        "hco3",
        "co2",
        "total_co2",
    ):
        if _is_equiv_mmol_or_meq_per_l(from_unit) and _is_equiv_mmol_or_meq_per_l(to_unit):
            return float(value)

    def _glucose() -> float:
        if fk == "mg_dl" and tk == "mmol_l":
            return float(value) / _GLUCOSE_MG_PER_MMOL
        if fk == "mmol_l" and tk == "mg_dl":
            return float(value) * _GLUCOSE_MG_PER_MMOL
        raise ValueError("unsupported glucose unit pair")

    def _creat() -> float:
        if fk == "mg_dl" and tk == "mmol_l":
            return float(value) / _CREATININE_MGDL_PER_MMOLL
        if fk == "mmol_l" and tk == "mg_dl":
            return float(value) * _CREATININE_MGDL_PER_MMOLL
        raise ValueError("unsupported creatinine unit pair")

    def _cholesterol() -> float:
        if fk == "mg_dl" and tk == "mmol_l":
            return float(value) / _CHOLESTEROL_MGDL_PER_MMOLL
        if fk == "mmol_l" and tk == "mg_dl":
            return float(value) * _CHOLESTEROL_MGDL_PER_MMOLL
        raise ValueError("unsupported cholesterol unit pair")

    def _triglyceride() -> float:
        if fk == "mg_dl" and tk == "mmol_l":
            return float(value) / _TRIGLYCERIDE_MGDL_PER_MMOLL
        if fk == "mmol_l" and tk == "mg_dl":
            return float(value) * _TRIGLYCERIDE_MGDL_PER_MMOLL
        raise ValueError("unsupported triglyceride unit pair")

    if a in ("glucose", "bg", "blood_glucose", "glu"):
        try:
            return _glucose()
        except ValueError:
            pass
    if a in ("creatinine", "creat", "scr", "cr"):
        try:
            return _creat()
        except ValueError:
            pass

    if a in (
        "cholesterol",
        "total_cholesterol",
        "tc",
        "ldl",
        "ldl_c",
        "hdl",
        "hdl_c",
        "non_hdl",
        "non-hdl",
        "vldl",
    ):
        try:
            return _cholesterol()
        except ValueError:
            pass

    if a in ("triglyceride", "triglycerides", "tg", "trig"):
        try:
            return _triglyceride()
        except ValueError:
            pass

    def _bun() -> float:
        if fk == "mg_dl" and tk == "mmol_l":
            return float(value) * _BUN_MGDL_TO_UREA_MMOLL
        if fk == "mmol_l" and tk == "mg_dl":
            return float(value) / _BUN_MGDL_TO_UREA_MMOLL
        raise ValueError("unsupported BUN / urea unit pair")

    if a in ("bun", "blood_urea_nitrogen", "urea_nitrogen", "un"):
        try:
            return _bun()
        except ValueError:
            pass

    def _hba1c() -> float:
        hk = _hba1c_unit_key(from_unit)
        tk_ = _hba1c_unit_key(to_unit)
        if hk == tk_:
            return float(value)
        if hk == "percent" and tk_ == "mmol_mol":
            return float(_HBA1C_IFCC_SCALE * (float(value) - _HBA1C_IFCC_OFFSET))
        if hk == "mmol_mol" and tk_ == "percent":
            return float(value) / _HBA1C_IFCC_SCALE + _HBA1C_IFCC_OFFSET
        raise ValueError("unsupported HbA1c unit pair")

    if a in (
        "hba1c",
        "a1c",
        "hemoglobin_a1c",
        "glycated_hemoglobin",
        "hgba1c",
        "hb_a1c",
    ):
        try:
            return _hba1c()
        except ValueError:
            pass
    return clinical_convert(value, from_unit, to_unit)


def infer_conversion_from_inputs(inputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    If a single input declares both ``unit`` and ``canonical_unit``, return
    a conversion descriptor for that field.
    """
    candidates = []
    for inp in inputs or []:
        iid = inp.get("id")
        u = inp.get("unit")
        cu = inp.get("canonical_unit")
        if not iid or not u or not cu:
            continue
        if str(u).strip().lower() == str(cu).strip().lower():
            continue
        candidates.append({"value_input": iid, "from_unit": str(u).strip(), "to_unit": str(cu).strip()})
    if len(candidates) == 1:
        return candidates[0]
    return None
