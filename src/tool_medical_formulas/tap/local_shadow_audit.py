"""Local TAP shadow audit JSONL (slim / offline; playbook §6)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _audit_path() -> Path:
    raw = (os.getenv("MEDICAL_FORMULAS_TAP_SHADOW_AUDIT_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(os.getenv("TMPDIR", "/tmp")) / "medical_formulas_tap_shadow.jsonl"


async def record_shadow_row(
    *,
    purpose: str,
    envelope: dict[str, Any],
    baseline: dict[str, Any],
    candidate: dict[str, Any] | None,
    candidate_error: str | None,
) -> None:
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool_id": "medical_formulas",
        "purpose": purpose,
        "envelope_keys": sorted(envelope.keys()),
        "baseline_status": baseline.get("status"),
        "candidate_status": (candidate or {}).get("status") if candidate else None,
        "candidate_error": candidate_error,
    }
    path = _audit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as exc:
        logger.debug("local shadow audit write skipped: %s", exc)

    if (os.getenv("MEDICAL_FORMULAS_TAP_PLATFORM_SHADOW") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        try:
            from tool_medical_formulas.monorepo_path import ensure_monorepo_root

            if ensure_monorepo_root() is None:
                return
            import importlib

            mod = importlib.import_module("services.generative_ui.tap_shadow_audit")
            await mod.audit_tool_internal_shadow(
                tool_id="medical_formulas",
                purpose=purpose,
                envelope=envelope,
                baseline_response=baseline,
                candidate_response=candidate,
                candidate_error=candidate_error,
            )
        except Exception as exc:
            logger.debug("platform shadow audit skipped: %s", exc)
