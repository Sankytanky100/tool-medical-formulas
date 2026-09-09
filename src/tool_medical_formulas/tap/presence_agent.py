"""
TAP presence handler for medical_formulas (GENERATIVE_UI_PLAN E.3 / E.3b).

Routes by ``MEDICAL_FORMULAS_TAP_MODE``:
  - deterministic (default): rule-based composer
  - llm: vector retrieval + Gemini structured JSON
  - adk: google-adk LlmAgent + Runner
  - shadow: run both paths, append shadow report, return configurable winner
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from tool_medical_formulas.tap.config import tap_mode, tap_shadow_return
from tool_medical_formulas.tap.ui_schema_composer import compose_ui_schema_from_formula

logger = logging.getLogger(__name__)

_ALLOWED_PURPOSES = frozenset(
    {"build_form", "route_request", "interpret_inputs", "explain", "suggest_prefill"}
)

_MANIFEST_HASH_CACHE: str | None = None

_FORMULA_DIR: Path | None = None


def _formula_dir() -> Path:
    global _FORMULA_DIR
    if _FORMULA_DIR is None:
        from tool_medical_formulas.formula_corpus import resolve_formula_corpus_dir

        _FORMULA_DIR = resolve_formula_corpus_dir()
    return _FORMULA_DIR


def _resolve_formula_id(context: dict[str, Any]) -> str:
    from tool_medical_formulas.tap.formula_resolve import resolve_formula_id_from_context

    return resolve_formula_id_from_context(context)


def _load_formula_spec(formula_id: str) -> dict[str, Any] | None:
    base = _formula_dir()
    if not base.is_dir():
        return None
    slug = formula_id.replace("_", "-").lower()
    for path in base.glob("*.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        doc_id = str(doc.get("id") or path.stem).lower()
        if doc_id == slug or slug in doc_id or doc_id.startswith(slug):
            return doc
        if slug.replace("-", "") in doc_id.replace("-", ""):
            return doc
    return None


def _spec_has_full_metadata(spec: dict[str, Any]) -> bool:
    """Same bar as legacy FormGenerationTool fast path."""
    for inp in spec.get("inputs") or []:
        if not isinstance(inp, dict):
            continue
        if not str(inp.get("label") or "").strip():
            return False
        if inp.get("options"):
            continue
        t = str(inp.get("type") or "number").lower()
        if t in ("number", "integer", "float") and not str(inp.get("unit") or inp.get("units") or "").strip():
            return False
    return bool(spec.get("inputs"))


async def handle_tap_converse(envelope: dict[str, Any]) -> dict[str, Any]:
    mode = tap_mode()
    if mode == "shadow":
        return await _handle_shadow(envelope)
    if mode == "llm":
        from tool_medical_formulas.tap import llm_presence

        return await llm_presence.handle_tap_converse(envelope)
    if mode == "adk":
        from tool_medical_formulas.tap import adk_presence

        return await adk_presence.handle_tap_converse(envelope)
    return await _handle_deterministic(envelope)


async def _handle_shadow(envelope: dict[str, Any]) -> dict[str, Any]:
    det = await _handle_deterministic(envelope)
    llm_resp: dict[str, Any] | None = None
    llm_err: str | None = None
    try:
        from tool_medical_formulas.tap import llm_presence

        llm_resp = await llm_presence.handle_tap_converse(envelope)
    except Exception as exc:
        llm_err = str(exc)
        logger.warning("shadow LLM path failed: %s", exc)

    _record_platform_shadow(envelope, det, llm_resp, llm_err)
    if tap_shadow_return() == "llm" and llm_resp is not None:
        return llm_resp
    return det


def _record_platform_shadow(
    envelope: dict[str, Any],
    det: dict[str, Any],
    llm: dict[str, Any] | None,
    llm_err: str | None,
) -> None:
    """Local JSONL shadow audit; optional platform audit when env enabled."""
    try:
        import asyncio

        from tool_medical_formulas.tap.local_shadow_audit import record_shadow_row

        purpose = str(envelope.get("purpose") or "build_form")

        async def _run() -> None:
            await record_shadow_row(
                purpose=purpose,
                envelope=envelope,
                baseline=det,
                candidate=llm,
                candidate_error=llm_err,
            )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_run())
        except RuntimeError:
            asyncio.run(_run())
    except Exception as exc:
        logger.debug("platform shadow audit skipped: %s", exc)

    try:
        from tool_medical_formulas.monorepo_path import ensure_monorepo_root

        root = ensure_monorepo_root()
        if root is None:
            return
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        out_dir = root / "reports" / "tap_shadow"
        out_dir.mkdir(parents=True, exist_ok=True)
        ctx = envelope.get("context") or {}
        fid = _resolve_formula_id(ctx if isinstance(ctx, dict) else {})
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "purpose": envelope.get("purpose"),
            "formula_id": fid,
            "deterministic_field_count": _field_count(det),
            "llm_field_count": _field_count(llm) if llm else None,
            "llm_error": llm_err,
            "field_mismatch": _field_count(det) != _field_count(llm) if llm else None,
        }
        path = out_dir / f"llm_shadow_{day}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as exc:
        logger.debug("legacy shadow row skipped: %s", exc)


def _field_count(resp: dict[str, Any] | None) -> int:
    if not resp:
        return 0
    result = resp.get("result") or {}
    schema = result.get("ui_schema") if isinstance(result, dict) else None
    if not isinstance(schema, dict):
        return 0
    n = 0
    for sec in schema.get("sections") or []:
        if isinstance(sec, dict):
            n += len(sec.get("fields") or [])
    return n


async def _handle_deterministic(envelope: dict[str, Any]) -> dict[str, Any]:
    purpose = str(envelope.get("purpose") or "build_form")
    if purpose not in _ALLOWED_PURPOSES:
        raise HTTPException(status_code=400, detail="tap_invalid_purpose")
    ctx = dict(envelope.get("context") or {})
    started = time.monotonic()

    if purpose == "route_request":
        return _route_request(ctx, started)
    if purpose == "suggest_prefill":
        return _suggest_prefill(ctx, started)
    if purpose == "interpret_inputs":
        return _interpret_inputs(ctx, started)
    if purpose == "explain":
        return _explain(ctx, started)

    formula_id = _resolve_formula_id(ctx)
    spec = _load_formula_spec(formula_id) if formula_id else None
    if spec is None:
        return {
            "tap_version": 1,
            "purpose_served": "build_form",
            "result": {"error": "formula_not_found", "formula_id": formula_id},
            "metadata": _metadata(started, tokens=0),
        }
    return await _build_form_deterministic(ctx, started, formula_id, spec)


async def _build_form_deterministic(
    ctx: dict[str, Any],
    started: float,
    formula_id: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    hints = _prefill_hints_from_spec(spec)
    ui_schema = compose_ui_schema_from_formula(
        spec, formula_id=formula_id, prefill_hints=hints
    )
    return {
        "tap_version": 1,
        "purpose_served": "build_form",
        "result": {
            "ui_schema": ui_schema,
            "operation_id": str(ctx.get("operation_id") or "compute_medical_formula"),
            "operation_inputs_template": {"formula_id": formula_id},
            "confidence": 0.95,
            "rationale_excerpt": f"Deterministic form from formula corpus: {formula_id}",
        },
        "metadata": _metadata(started, tokens=120),
    }


def _prefill_hints_from_spec(spec: dict[str, Any]) -> dict[str, str]:
    hints: dict[str, str] = {}
    for inp in spec.get("inputs") or []:
        if not isinstance(inp, dict):
            continue
        fid = str(inp.get("id") or inp.get("name") or "")
        name = str(inp.get("name") or fid).lower()
        atom = _default_atom_for_input(name, fid)
        if atom and fid:
            hints[fid] = atom
    return hints


def _suggest_prefill(ctx: dict[str, Any], started: float) -> dict[str, Any]:
    import copy

    formula_id = _resolve_formula_id(ctx)
    spec = _load_formula_spec(formula_id) if formula_id else None
    draft = ctx.get("ui_schema_draft")
    if isinstance(draft, dict) and draft.get("sections"):
        enriched = copy.deepcopy(draft)
    elif spec:
        enriched = compose_ui_schema_from_formula(
            spec, formula_id=formula_id, prefill_hints=_prefill_hints_from_spec(spec)
        )
    else:
        enriched = {"sections": [{"title": "Inputs", "fields": []}]}

    mappings_added = 0
    mappings_skipped: list[dict[str, str]] = []
    hints = _prefill_hints_from_spec(spec) if spec else {}

    for sec in enriched.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        for field in sec.get("fields") or []:
            if not isinstance(field, dict):
                continue
            fid = str(field.get("field") or "")
            atom = hints.get(fid) or _default_atom_for_input(
                str(field.get("label") or fid).lower(), fid
            )
            if not atom:
                continue
            if field.get("prefill_from_atom"):
                mappings_skipped.append({"field": fid, "reason": "already_mapped"})
                continue
            field["prefill_from_atom"] = {"field_id": atom, "strategy": "most_recent"}
            mappings_added += 1

    return {
        "tap_version": 1,
        "purpose_served": "suggest_prefill",
        "result": {
            "enriched_ui_schema": enriched,
            "mappings_added": mappings_added,
            "mappings_skipped": mappings_skipped,
        },
        "metadata": _metadata(started, tokens=40),
    }


def _interpret_inputs(ctx: dict[str, Any], started: float) -> dict[str, Any]:
    import re

    utterance = str(ctx.get("user_utterance") or ctx.get("user_query") or "").strip()
    lower = utterance.lower()
    extracted: dict[str, Any] = {}
    unresolved: list[str] = []
    confidence: dict[str, float] = {}

    formula_id = _resolve_formula_id(ctx)
    spec = _load_formula_spec(formula_id) if formula_id else None

    def _bool_mention(label: str) -> bool | None:
        m = re.search(rf"\b{re.escape(label)}\b\s*[:=]?\s*(yes|no|true|false|0|1)\b", lower)
        if m:
            return m.group(1) in ("yes", "true", "1")
        if re.search(rf"\b{re.escape(label)}\b", lower):
            return True
        return None

    if spec and isinstance(spec.get("inputs"), list):
        for inp in spec.get("inputs") or []:
            if not isinstance(inp, dict):
                continue
            key = str(inp.get("id") or inp.get("name") or "").strip()
            if not key:
                continue
            label = str(inp.get("label") or inp.get("name") or key).strip()
            itype = str(inp.get("type") or "number").lower()
            tokens = [t for t in re.split(r"[\s_/]+", label.lower()) if len(t) > 2]
            tokens.append(key.replace("_", " "))
            hit = False
            for tok in tokens:
                if not tok:
                    continue
                if itype in ("boolean", "bool", "yes_no", "checkbox"):
                    val = _bool_mention(tok)
                    if val is not None:
                        extracted[key] = val
                        confidence[key] = 0.88
                        hit = True
                        break
                elif "age" in tok or key.lower() == "age":
                    age_match = re.search(r"\bage\s*[:=]?\s*(\d{1,3})\b", lower)
                    if not age_match:
                        age_match = re.search(r"\b(\d{2,3})\s*(?:years? old|yo|y/?o)\b", lower)
                    if age_match:
                        extracted[key] = int(age_match.group(1))
                        confidence[key] = 0.9
                        hit = True
                        break
                elif re.search(rf"\b{re.escape(tok)}\b", lower):
                    if itype in ("boolean", "bool", "yes_no", "checkbox"):
                        extracted[key] = True
                        confidence[key] = 0.82
                    hit = True
                    break
            if not hit and inp.get("required") and key not in extracted:
                unresolved.append(key)
    else:
        if "female" in lower:
            extracted["sex"] = "female"
            confidence["sex"] = 0.9
        elif "male" in lower:
            extracted["sex"] = "male"
            confidence["sex"] = 0.9
        for label, key in (
            ("heart failure", "heart_failure"),
            ("hypertension", "hypertension"),
            ("diabetes", "diabetes"),
            ("stroke", "stroke"),
        ):
            val = _bool_mention(label)
            if val is not None:
                extracted[key] = val
                confidence[key] = 0.85

    next_q: list[str] = []
    if unresolved:
        next_q.append(f"Please confirm: {', '.join(unresolved)}")

    return {
        "tap_version": 1,
        "purpose_served": "interpret_inputs",
        "result": {
            "extracted_inputs": extracted,
            "unresolved_inputs": unresolved,
            "confidence_per_input": confidence,
            "next_questions_for_clinician": next_q,
            "formula_id": formula_id or None,
        },
        "metadata": _metadata(started, tokens=60),
    }


def _explain(ctx: dict[str, Any], started: float) -> dict[str, Any]:
    subject = str(ctx.get("subject") or "tool_overview")
    formula_id = _resolve_formula_id(ctx)
    spec = _load_formula_spec(formula_id) if formula_id else None

    manifest_result: dict[str, Any] | None = None
    try:
        root = Path(__file__).resolve().parents[4]
        reg = root / "tools" / "registry" / "medical_formulas.yaml"
        if reg.is_file():
            import yaml

            from laer_platform.tap.manifest_explain import build_explain_result

            doc = yaml.safe_load(reg.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                manifest_result = build_explain_result(
                    doc,
                    subject=subject,
                    operation_id=str(ctx.get("operation_id") or "") or None,
                    operation_inputs_hint=dict(ctx.get("operation_inputs_hint") or {}),
                    verbosity=str(ctx.get("verbosity") or "standard"),
                )
    except Exception:
        manifest_result = None

    if manifest_result and subject == "tool_overview" and not formula_id:
        result = manifest_result
    elif spec and subject != "tool_overview":
        title = str(spec.get("name") or formula_id)
        summary = str(spec.get("description") or f"Formula: {formula_id}")[:4000]
        cites = []
        ref = spec.get("reference") or spec.get("references")
        if isinstance(ref, str) and ref.strip():
            cites = [{"label": ref.strip()[:120], "url": None}]
        result = {
            "title": title,
            "summary_md": summary,
            "citations": cites,
            "next_action_hint": "build_form",
        }
    elif manifest_result:
        result = manifest_result
    else:
        title = "Medical formulas"
        summary = (
            "Clinical scoring and calculator tools backed by a curated formula corpus. "
            "Use **build_form** to render inputs, **route_request** to pick a score from natural language."
        )
        result = {
            "title": title,
            "summary_md": summary,
            "citations": [],
            "next_action_hint": "build_form" if formula_id else "route_request",
        }

    return {
        "tap_version": 1,
        "purpose_served": "explain",
        "result": result,
        "metadata": _metadata(started, tokens=30),
    }


def _default_atom_for_input(name: str, fid: str) -> str | None:
    mapping = {
        "heart_failure": "condition.heart_failure",
        "chf": "condition.heart_failure",
        "hypertension": "condition.hypertension",
        "diabetes": "condition.diabetes",
        "age_75": "demographics.age",
        "stroke": "condition.stroke_or_tia",
        "creatinine": "lab.creatinine",
    }
    for key, atom in mapping.items():
        if key in name or key in fid.lower():
            return atom
    return None


def _route_request(ctx: dict[str, Any], started: float) -> dict[str, Any]:
    from tool_medical_formulas.tap.formula_resolve import route_from_query

    uq = str(ctx.get("user_query") or ctx.get("user_utterance") or "").strip()
    decision = route_from_query(uq)

    if decision is None or (not decision.formula_id and not decision.ambiguous):
        return {
            "tap_version": 1,
            "purpose_served": "route_request",
            "result": {
                "operation_id": "calculate_formula",
                "operation_inputs_template": {},
                "confidence": 0.2,
                "rationale_excerpt": (
                    "Could not map query to a formula in the corpus. "
                    "Name the score (e.g. Wells PE, CURB-65, CHA2DS2-VASc) or pass formula_id in context."
                ),
                "needs_clarification": True,
            },
            "metadata": _metadata(started, tokens=40),
        }

    if decision.ambiguous and not decision.formula_id:
        return {
            "tap_version": 1,
            "purpose_served": "route_request",
            "result": {
                "operation_id": "calculate_formula",
                "operation_inputs_template": {},
                "confidence": decision.confidence,
                "rationale_excerpt": decision.rationale_excerpt,
                "needs_clarification": True,
                "alternative_formula_ids": list(decision.alternatives),
            },
            "metadata": _metadata(started, tokens=50),
        }

    return {
        "tap_version": 1,
        "purpose_served": "route_request",
        "result": {
            "operation_id": "calculate_formula",
            "operation_inputs_template": {"formula_id": decision.formula_id},
            "confidence": decision.confidence,
            "rationale_excerpt": decision.rationale_excerpt,
            "alternative_formula_ids": list(decision.alternatives) if decision.alternatives else [],
        },
        "metadata": _metadata(started, tokens=80),
    }


def _manifest_hash_for_tap() -> str:
    global _MANIFEST_HASH_CACHE
    if _MANIFEST_HASH_CACHE is not None:
        return _MANIFEST_HASH_CACHE
    mh = (
        os.getenv("TSE_MANIFEST_HASH")
        or os.getenv("TOOL_MEDICAL_FORMULAS_MANIFEST_HASH")
        or ""
    ).strip()
    if not mh or mh == "sha256:" + "0" * 64:
        raise RuntimeError(
            "TSE_MANIFEST_HASH is required when TAP is enabled (no all-zero fallback)"
        )
    if not mh.startswith("sha256:"):
        mh = f"sha256:{mh}"
    _MANIFEST_HASH_CACHE = mh
    return mh


def require_tap_manifest_hash_at_startup() -> None:
    """Fail-fast when TAP is deployed without manifest hash (audit §1.7)."""
    _manifest_hash_for_tap()


def _metadata(started: float, *, tokens: int) -> dict[str, Any]:
    mh = _manifest_hash_for_tap()
    return {
        "model_used": "deterministic-composer",
        "tokens_consumed": tokens,
        "cache_hit": False,
        "tap_latency_ms": int((time.monotonic() - started) * 1000),
        "manifest_hash": mh,
        "composer_normalizations": [],
    }


def warm_tap_index() -> None:
    """Load the TAP corpus index on startup (prebuilt ``.npz`` when shipped in the image)."""
    from tool_medical_formulas.tap.corpus_vector_index import get_corpus_index

    get_corpus_index()
