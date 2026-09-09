"""Load TAP prompt templates from ``tap/prompts/*.md``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_PROMPTS = Path(__file__).resolve().parent / "prompts"


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    path = _PROMPTS / f"{name}.md"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()
