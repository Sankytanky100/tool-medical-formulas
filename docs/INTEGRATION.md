# Integration

This package is the **calculation engine**. The HTTP envelope — operations, egress, freshness `hard_block`, PHI flag — lives in the public contract catalog:

- [laer-tool-contract](https://github.com/Sankytanky100/laer-tool-contract) → `medical_formulas.http.yaml`

In the platform, the gateway calls `POST /v1/tools/medical_formulas/{operation_id}` after the conformance gate. This snapshot does not include that gateway.

## What you can run here

| Surface | Needs |
|---|---|
| `ckd_epi_2021_creatinine_egfr` / slim `calculate_formula` for CKD-EPI aliases | This repo + `pip install -e ".[slim]"` |
| JSON-corpus calculators (MELD, CHA₂DS₂-VASc, …) | A formula JSON tree via `TOOL_MEDICAL_FORMULAS_FORMULAS_DIR` — **not shipped** |
| FastAPI `main.py`, TAP converse, TSE | Private `laer-platform` (not on public PyPI) |

## Slim operations

`calculate_formula` and `compute_medical_formula` only. Other contract operation ids raise `operation_not_available_in_slim_mode`.
