# Examples — captured from this tree

All payloads below were produced on **2026-09-09** by calling `execute_native` / `execute_operation` in this snapshot (`PYTHONPATH=src`, `TOOL_MEDICAL_FORMULAS_SLIM=1`). Inputs are synthetic. They are not a patient record.

Do not treat the numbers as clinical advice. They demonstrate that the engine returns a computed value or a typed failure.

---

## 1. CKD-EPI 2021 — age 68, female, Scr 1.8 mg/dL

```python
from tool_medical_formulas.native_ops import execute_native

await execute_native(
    "calculate_formula",
    {
        "formula_id": "ckd_epi",
        "inputs": {"age": 68, "sex": "female", "creatinine": 1.8},
    },
    {},
)
```

```json
{
  "status": "success",
  "result": 30.3,
  "units": "mL/min/1.73m2",
  "formula_id": "ckd-epi-equations-for-glomerular-filtration-rate-gfr",
  "formula_name": "CKD-EPI Equations for Glomerular Filtration Rate (GFR)",
  "validated_inputs": {
    "age": 68.0,
    "sex": "female",
    "creatinine": 1.8,
    "equation": "2021 CKD-EPI Creatinine"
  },
  "calculation_type": "mathematical",
  "provenance": {
    "deterministic": true,
    "engine": "ckd_epi_2021_creatinine",
    "guideline": "Inker et al. 2021 (race-free)"
  },
  "interpretation": "CKD G3b (moderate-severe)"
}
```

Direct equation (same inputs): `ckd_epi_2021_creatinine_egfr(age_years=68.0, female=True, creatinine_mg_dl=1.8)` printed `30.3`.

---

## 2. Same formula, creatinine omitted

The engine does not invent an eGFR.

```python
await execute_native(
    "calculate_formula",
    {
        "formula_id": "ckd_epi",
        "inputs": {"age": 68, "sex": "female"},
    },
    {},
)
```

```json
{
  "status": "validation_failed",
  "error": "missing_required_inputs",
  "missing_fields": ["creatinine"],
  "formula_id": "ckd-epi-equations-for-glomerular-filtration-rate-gfr"
}
```

---

## 3. Interpret on the slim surface

`interpret_medical_formula_result` is not implemented in slim mode. Captured:

```text
RuntimeError: operation_not_available_in_slim_mode:interpret_medical_formula_result
```

That is the snapshot telling you it will not narrate a score it did not calculate. The fuller interpret path is not published here.

---

## Not captured (and why)

| Call | Why it is absent |
|---|---|
| CHA₂DS₂-VASc / MELD via JSON corpus | Formula JSON is not in this public snapshot (licence unconfirmed) |
| `labwise_insights` `answer_summary` | Wrong repo |
| Freshness `hard_block` | Enforced on the **contract** / gateway, not by `execute_native` |
