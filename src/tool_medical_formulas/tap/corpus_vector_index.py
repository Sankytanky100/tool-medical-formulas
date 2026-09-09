"""
Formula corpus vector index for TAP retrieval (GENERATIVE_UI_PLAN E.3b §5.5).

Embeddings: Vertex ``text-embedding-005`` when ``MEDICAL_FORMULAS_TAP_EMBED=vertex``,
else deterministic stub vectors (CI / offline). Index persisted as ``.npz`` beside corpus.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_INDEX_LOCK = threading.Lock()
_INDEX: "FormulaCorpusIndex | None" = None
_INDEX_SOURCE: str = "none"  # none | disk | built | memory


@dataclass(frozen=True)
class FormulaHit:
    formula_id: str
    score: float
    name: str
    snippet: str
    path: str


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def _embed_batch(texts: list[str]) -> list[list[float]]:
    from tool_medical_formulas.tap.config import tap_embed_mode

    mode = tap_embed_mode()
    if mode == "vertex":
        try:
            from google import genai

            project = (os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID") or "").strip()
            location = (os.getenv("GOOGLE_CLOUD_LOCATION") or "us-central1").strip()
            model = (
                os.getenv("MEDICAL_FORMULAS_TAP_EMBED_MODEL")
                or os.getenv("EMBEDDING_MODEL")
                or "text-embedding-005"
            ).strip()
            client = genai.Client(vertexai=True, project=project or None, location=location)
            out: list[list[float]] = []
            for text in texts:
                resp = client.models.embed_content(model=model, contents=text or "formula")
                emb = getattr(resp, "embedding", None) or getattr(resp, "embeddings", None)
                if emb is None:
                    raise RuntimeError("vertex embed returned no embedding")
                vec = getattr(emb[0], "values", None) if isinstance(emb, list) else getattr(emb, "values", None)
                if vec is None:
                    raise RuntimeError("vertex embed returned empty vector")
                out.append(list(vec))
            return out
        except Exception as exc:
            logger.warning("TAP vertex embed failed, using stub: %s", exc)

    from tool_medical_formulas.tap.stub_embed import stub_embed

    dim = int(os.getenv("MEDICAL_FORMULAS_TAP_EMBED_DIM", "64") or "64")
    return [stub_embed(t, dim=dim) for t in texts]


def _formula_doc_text(doc: dict[str, Any], *, path: Path) -> str:
    parts = [
        str(doc.get("id") or path.stem),
        str(doc.get("name") or ""),
        str(doc.get("description") or "")[:2000],
        str(doc.get("category") or ""),
    ]
    for inp in (doc.get("inputs") or [])[:24]:
        if isinstance(inp, dict):
            parts.append(
                f"{inp.get('id') or inp.get('name')}: {inp.get('label') or ''} {inp.get('help_text') or ''}"
            )
    return "\n".join(p for p in parts if p.strip())


class FormulaCorpusIndex:
    def __init__(
        self,
        *,
        formula_ids: list[str],
        vectors: list[list[float]],
        names: list[str],
        snippets: list[str],
        paths: list[str],
    ) -> None:
        self.formula_ids = formula_ids
        self.vectors = vectors
        self.names = names
        self.snippets = snippets
        self.paths = paths

    def search(self, query: str, *, top_k: int = 5) -> list[FormulaHit]:
        if not self.vectors:
            return []
        qv = _embed_batch([query or "formula"])[0]
        scored: list[tuple[float, int]] = []
        for i, vec in enumerate(self.vectors):
            scored.append((_cosine(qv, vec), i))
        scored.sort(key=lambda x: x[0], reverse=True)
        out: list[FormulaHit] = []
        for score, idx in scored[: max(1, top_k)]:
            out.append(
                FormulaHit(
                    formula_id=self.formula_ids[idx],
                    score=score,
                    name=self.names[idx],
                    snippet=self.snippets[idx],
                    path=self.paths[idx],
                )
            )
        return out

    def persist(self, index_path: Path) -> None:
        import numpy as np

        index_path.parent.mkdir(parents=True, exist_ok=True)
        mat = np.array(self.vectors, dtype=np.float32)
        np.savez_compressed(
            index_path,
            vectors=mat,
            formula_ids=np.array(self.formula_ids, dtype=object),
            names=np.array(self.names, dtype=object),
            snippets=np.array(self.snippets, dtype=object),
            paths=np.array(self.paths, dtype=object),
        )
        meta = {
            "built_at": time.time(),
            "count": len(self.formula_ids),
            "embed_mode": os.getenv("MEDICAL_FORMULAS_TAP_EMBED", "stub"),
        }
        index_path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, index_path: Path) -> "FormulaCorpusIndex | None":
        if not index_path.is_file():
            return None
        try:
            import numpy as np

            data = np.load(index_path, allow_pickle=True)
            vectors = [list(row) for row in data["vectors"]]
            return cls(
                formula_ids=[str(x) for x in data["formula_ids"].tolist()],
                vectors=vectors,
                names=[str(x) for x in data["names"].tolist()],
                snippets=[str(x) for x in data["snippets"].tolist()],
                paths=[str(x) for x in data["paths"].tolist()],
            )
        except Exception as exc:
            logger.warning("TAP index load failed %s: %s", index_path, exc)
            return None

    @classmethod
    def build_from_corpus(cls, corpus_dir: Path) -> "FormulaCorpusIndex":
        texts: list[str] = []
        formula_ids: list[str] = []
        names: list[str] = []
        snippets: list[str] = []
        paths: list[str] = []

        for path, doc, fid in _iter_formula_docs(corpus_dir):
            text = _formula_doc_text(doc, path=path)
            texts.append(text)
            formula_ids.append(fid)
            names.append(str(doc.get("name") or fid))
            snippets.append(str(doc.get("description") or "")[:400])
            paths.append(str(path))

        logger.info("TAP corpus index: embedding %d formulas", len(texts))
        vectors = _embed_batch(texts) if texts else []
        return cls(
            formula_ids=formula_ids,
            vectors=vectors,
            names=names,
            snippets=snippets,
            paths=paths,
        )


def default_index_path(corpus_dir: Path) -> Path:
    custom = (os.getenv("MEDICAL_FORMULAS_TAP_INDEX_DIR") or "").strip()
    if custom:
        return Path(custom).expanduser() / "tap_corpus_index.npz"
    return corpus_dir.parent / "tap_index" / "tap_corpus_index.npz"


def reset_corpus_index() -> None:
    """Drop the in-process index (tests / force reload)."""
    global _INDEX, _INDEX_SOURCE
    with _INDEX_LOCK:
        _INDEX = None
        _INDEX_SOURCE = "none"


def corpus_index_source() -> str:
    return _INDEX_SOURCE


def _iter_formula_docs(corpus_dir: Path):
    """Yield indexable formula JSON docs (skip sidecars like list dumps)."""
    for path in sorted(corpus_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        fid = str(doc.get("id") or path.stem).strip()
        if not fid:
            continue
        yield path, doc, fid


def _corpus_formula_count(corpus_dir: Path) -> int:
    return sum(1 for _ in _iter_formula_docs(corpus_dir))


def _load_usable_disk_index(index_path: Path, *, corpus_dir: Path) -> FormulaCorpusIndex | None:
    loaded = FormulaCorpusIndex.load(index_path)
    if loaded is None:
        return None
    meta_path = index_path.with_suffix(".meta.json")
    embed = (os.getenv("MEDICAL_FORMULAS_TAP_EMBED") or "stub").strip().lower()
    expected = _corpus_formula_count(corpus_dir)
    if expected and len(loaded.formula_ids) != expected:
        logger.info(
            "TAP index stale: disk=%d corpus=%d path=%s",
            len(loaded.formula_ids),
            expected,
            index_path,
        )
        return None
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            disk_mode = str(meta.get("embed_mode") or "").strip().lower()
            if disk_mode and disk_mode != embed:
                logger.info("TAP index embed_mode mismatch disk=%s want=%s", disk_mode, embed)
                return None
        except Exception:
            pass
    return loaded


def get_corpus_index(*, force_rebuild: bool = False) -> FormulaCorpusIndex:
    global _INDEX, _INDEX_SOURCE
    with _INDEX_LOCK:
        if _INDEX is not None and not force_rebuild:
            _INDEX_SOURCE = "memory"
            return _INDEX

        from tool_medical_formulas.formula_corpus import resolve_formula_corpus_dir
        from tool_medical_formulas.tap.config import tap_rebuild_index

        corpus_dir = resolve_formula_corpus_dir()
        index_path = default_index_path(corpus_dir)
        if not force_rebuild and not tap_rebuild_index():
            loaded = _load_usable_disk_index(index_path, corpus_dir=corpus_dir)
            if loaded is not None:
                _INDEX = loaded
                _INDEX_SOURCE = "disk"
                logger.info("TAP corpus index loaded from %s (%d)", index_path, len(loaded.formula_ids))
                return _INDEX

        built = FormulaCorpusIndex.build_from_corpus(corpus_dir)
        try:
            built.persist(index_path)
            logger.info("TAP corpus index persisted to %s", index_path)
        except Exception as exc:
            logger.warning("TAP index persist skipped: %s", exc)
        _INDEX = built
        _INDEX_SOURCE = "built"
        return _INDEX
