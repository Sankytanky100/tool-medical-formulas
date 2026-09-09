"""Standalone no-op metrics (do not import the monorepo module)."""
from __future__ import annotations


def incr_counter(name: str, delta: int = 1, *, labels: dict[str, str] | None = None) -> int:
    return 0
