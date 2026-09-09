"""Pytest bootstrap for tool-medical-formulas (TEP-5/residual co-located tests)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parents[1]
_SRC = _TOOL_ROOT / "src"
_LAER_PLATFORM = _TOOL_ROOT.parent / "laer-platform" / "src"
_paths = [str(_SRC)]
if _LAER_PLATFORM.is_dir():
    _paths.append(str(_LAER_PLATFORM))
for _p in _paths:
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("TOOL_MEDICAL_FORMULAS_SLIM", "1")
os.environ.setdefault("TSE_MANIFEST_HASH", "sha256:" + ("a" * 64))
