"""tool-medical-formulas — Tool Execution API (Week 3)."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# TEP-4: slim-only runtime (no monorepo_bridge / laer.bundles). Corpus resolution
# uses packaged data or monorepo_path inside formula_corpus when needed.

BUNDLE_ID = "medical_formulas"


class ToolExecuteRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    user_context: dict[str, Any] = Field(default_factory=dict)


def create_app() -> FastAPI:
    from laer_platform.tool_runtime.structured_log import configure_structured_tool_logging
    from laer_platform.tool_runtime.tse_startup import configure_tool_tse_env
    from laer_platform.tool_runtime.tse_middleware import install_tse_middleware

    configure_structured_tool_logging(bundle_id=BUNDLE_ID)
    configure_tool_tse_env(bundle_id=BUNDLE_ID)

    app = FastAPI(
        title="tool-medical-formulas",
        version="0.1.0",
        description="Medical scoring formulas — Laer Tool Execution API",
    )
    try:
        from api.fastapi_platform_bootstrap import install_platform_error_handling

        install_platform_error_handling(
            app,
            service_name=BUNDLE_ID,
            log_profile="tool",
        )
    except Exception:
        logger.debug("platform error handling skipped", exc_info=True)
    try:
        from utils.cors_config import install_laer_cors_middleware

        install_laer_cors_middleware(app, profile="tool_cloud_run")
    except ImportError:
        logger.warning("utils.cors_config unavailable — CORS middleware not installed")
    install_tse_middleware(app, bundle_id=BUNDLE_ID)
    try:
        from laer_platform.tool_runtime.otel_trace import configure_tool_cloud_trace

        configure_tool_cloud_trace(bundle_id=BUNDLE_ID, fastapi_app=app)
    except Exception:
        logger.debug("tool OTel skipped", exc_info=True)
    try:
        from laer_platform.tool_runtime.idempotency_middleware import install_idempotency_middleware

        window = 60
        try:
            from laer_platform.schema.manifest import IdempotencyConfig, ToolManifest

            m = getattr(app.state, "manifest", None)
            if isinstance(m, ToolManifest) and m.idempotency is not None:
                window = int(m.idempotency.window_seconds or 60)
        except Exception:
            pass
        install_idempotency_middleware(app, window_seconds=window)
    except Exception as exc:
        logger.warning("Idempotency middleware not installed: %s", exc)

    @app.on_event("startup")
    async def _tap_startup() -> None:
        try:
            from tool_medical_formulas.tap.presence_agent import (
                require_tap_manifest_hash_at_startup,
                warm_tap_index,
            )

            require_tap_manifest_hash_at_startup()
            warm_tap_index()
        except Exception as exc:
            logger.warning("TAP startup check skipped: %s", exc)

    @app.get("/health")
    async def health() -> dict[str, object]:
        out: dict[str, object] = {"status": "ok", "service": "tool-medical-formulas", "bundle": BUNDLE_ID}
        try:
            from tool_medical_formulas.tap.corpus_vector_index import corpus_index_source

            out["tap_index_source"] = corpus_index_source()
        except Exception:
            pass
        return out

    from laer_platform.tool_runtime.cold_start import register_tier2_cold_start_routes

    def _warmup() -> None:
        from tool_medical_formulas.runtime import list_implemented_operations
        from tool_medical_formulas.tap.presence_agent import warm_tap_index

        list_implemented_operations({"plan": "enterprise", "org_id": "warmup"})
        warm_tap_index()

    register_tier2_cold_start_routes(app, bundle_id=BUNDLE_ID, warmup_fn=_warmup)

    @app.get("/v1/tools/medical_formulas/operations")
    async def list_operations() -> dict[str, Any]:
        from tool_medical_formulas.runtime import list_implemented_operations

        uc = {"plan": "enterprise", "org_id": "ops"}
        return {"operations": list_implemented_operations(uc), "bundle_id": BUNDLE_ID}

    @app.post("/v1/tools/medical_formulas/_tap/converse")
    async def tap_converse(body: dict[str, Any]) -> dict[str, Any]:
        from tool_medical_formulas.tap.presence_agent import handle_tap_converse

        try:
            return await handle_tap_converse(body)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("tap converse failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/v1/tools/clinical_intake_tap/_tap/converse")
    async def clinical_intake_tap_converse(body: dict[str, Any]) -> dict[str, Any]:
        from tool_medical_formulas.tap.clinical_intake_tap import handle_clinical_intake_tap

        try:
            return handle_clinical_intake_tap(body)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("clinical_intake TAP failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/v1/tools/medical_formulas/{operation_id}")
    async def execute(operation_id: str, body: ToolExecuteRequest) -> dict[str, Any]:
        from tool_medical_formulas.runtime import OPERATION_IDS, execute_operation
        from laer_platform.tool_runtime.request_context import set_user_context
        from laer_platform.tool_result import ToolResult

        op = operation_id.strip()
        if op not in OPERATION_IDS:
            raise HTTPException(status_code=404, detail=f"unknown_operation:{op}")

        uc = dict(body.user_context or {})
        uc.setdefault("plan", uc.get("subscription_tier") or "free")
        set_user_context(uc)

        started = time.monotonic()
        try:
            raw = await execute_operation(op, dict(body.inputs or {}), uc)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown_operation:{op}") from None
        except Exception as exc:
            logger.exception("execute %s failed", op)
            tr = ToolResult(
                success=False,
                error="internal",
                error_detail=str(exc),
                tool_name=op,
                bundle_id=BUNDLE_ID,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            return tr.to_dict()

        latency_ms = int((time.monotonic() - started) * 1000)
        if isinstance(raw, dict) and raw.get("status") in (
            "error",
            "validation_failed",
            "calculation_failed",
        ):
            tr = ToolResult(
                success=False,
                error=str(raw.get("error") or raw.get("status")),
                error_detail=str(raw.get("message") or raw.get("error") or ""),
                data=raw,
                tool_name=op,
                bundle_id=BUNDLE_ID,
                latency_ms=latency_ms,
            )
            return tr.to_dict()

        tr = ToolResult(
            success=True,
            data=raw if isinstance(raw, dict) else {"result": raw},
            tool_name=op,
            bundle_id=BUNDLE_ID,
            latency_ms=latency_ms,
        )
        return tr.to_dict()

    return app


app = create_app()


def run() -> None:
    import uvicorn

    port = int(os.getenv("TOOL_MEDICAL_FORMULAS_PORT") or "8090")
    uvicorn.run(
        "tool_medical_formulas.main:app",
        host="0.0.0.0",
        port=port,
        reload=(os.getenv("TOOL_MEDICAL_FORMULAS_RELOAD") or "").lower() in ("1", "true"),
    )


if __name__ == "__main__":
    run()
