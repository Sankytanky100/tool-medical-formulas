# TAP purpose: suggest_prefill

Map formula input fields to **patient atom field_ids** for `prefill_from_atom`.

## Rules

- Only suggest mappings you are confident about from patient_atom_summary
- Use dotted atom ids: `demographics.age`, `lab.creatinine`, `condition.diabetes`, etc.
- Output `suggestions`: `{ "field", "prefill_field_id", "confidence" }`
- Do not suggest PHI the summary does not support

## Output (JSON only)

`suggestions` array and optional `rationale_excerpt`
