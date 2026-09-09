"""
Formula JSON corpus resolution (GENERATIVE_UI_PLAN E.2).

Single-source policy:
1. ``TOOL_MEDICAL_FORMULAS_FORMULAS_DIR`` when set (tool image / Cloud Run).
2. ``<package>/data/formulas`` when populated in the tool wheel/image.
3. Monorepo ``data/formulas`` via ``ensure_monorepo_root()`` (dev / cutover).
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_formula_corpus_dir() -> Path:
    default_target = Path(
        os.getenv("TOOL_MEDICAL_FORMULAS_FORMULAS_DIR") or "/app/corpus/formulas"
    )
    packaged_archive = Path(__file__).resolve().parents[2] / "data" / "formulas.tar.gz"
    if packaged_archive.is_file():
        try:
            from laer_platform.corpus_archive import ensure_directory_from_archive
        except ImportError:
            ensure_directory_from_archive = None
        if ensure_directory_from_archive is not None:
            ensure_directory_from_archive(
                archive_path=packaged_archive,
                target_dir=default_target,
                env_archive="TOOL_MEDICAL_FORMULAS_FORMULAS_ARCHIVE",
                env_target="TOOL_MEDICAL_FORMULAS_FORMULAS_DIR",
            )

    env = (os.getenv("TOOL_MEDICAL_FORMULAS_FORMULAS_DIR") or "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_dir() and any(p.glob("*.json")):
            return p

    packaged = Path(__file__).resolve().parents[2] / "data" / "formulas"
    if packaged.is_dir() and any(packaged.glob("*.json")):
        return packaged

    from tool_medical_formulas.monorepo_path import ensure_monorepo_root

    root = ensure_monorepo_root()
    if root is None:
        return packaged
    return root / "data" / "formulas"
