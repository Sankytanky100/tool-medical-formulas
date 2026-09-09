"""Locate the private monorepo when present; no-op in a standalone clone."""
from __future__ import annotations

import sys
from pathlib import Path


def ensure_monorepo_root() -> Path | None:
    try:
        root = Path(__file__).resolve().parents[4]
    except IndexError:
        return None
    if not (root / "tools" / "registry").is_dir():
        return None
    s = str(root)
    if s not in sys.path:
        sys.path.insert(0, s)
    return root
