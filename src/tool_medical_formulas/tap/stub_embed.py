"""Deterministic stub embeddings for TAP (no monorepo ``services`` import)."""

from __future__ import annotations

import hashlib
import os
from typing import List


def stub_embed(text: str, *, dim: int | None = None) -> List[float]:
    d = dim if dim is not None else int(os.getenv("MEDICAL_FORMULAS_TAP_EMBED_DIM", "64") or "64")
    t = (text or "").strip().lower().encode("utf-8", errors="ignore")
    if not t:
        t = b"empty"
    h = hashlib.sha256(t).digest()
    out: List[float] = []
    i = 0
    while len(out) < d:
        chunk = h[i % len(h) : (i % len(h)) + 4]
        if len(chunk) < 4:
            chunk = (chunk + h)[:4]
        val = int.from_bytes(chunk[:4], "big", signed=False)
        out.append((val % 1000) / 1000.0 - 0.5)
        i += 1
    return out
