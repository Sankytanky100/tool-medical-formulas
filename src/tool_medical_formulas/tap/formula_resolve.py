"""
Catalog-agnostic formula resolution for TAP (replaces CHADS2-only shortcuts).

Resolution order:
1. Explicit ``operation_inputs_hint`` / ``formula_id`` on context
2. Named-score patterns (Wells, CURB-65, MELD, …) when query names a score family
3. Corpus vector search (stub/vertex index) when corpus is available
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_WELLS_DVT = "wells-criteria-for-dvt"
_WELLS_PE = "wells-criteria-for-pulmonary-embolism"
_CHA2DS2_VASC = "cha2ds2-vasc-score-for-atrial-fibrillation-stroke-risk"
_CHADS2 = "chads2-score-for-atrial-fibrillation-stroke-risk"
_CURB65 = "curb-65-score-for-pneumonia-severity"
_MELD_NA = "meldnameld-na-score-for-liver-cirrhosis"
_HAS_BLED = "has-bled-score-for-major-bleeding-risk"
_PADUA = "padua-prediction-score-for-risk-of-vte"
_QSOFA = "qsofa-quick-sofa-score-for-sepsis"

_NAMED_SCORE_RE = re.compile(
    r"\b("
    r"wells|cha2ds2|chads2|curb[\s-]?65|meld|has[\s-]?bled|padua|qsofa"
    r"|score|risk\s+score|criteria"
    r")\b",
    re.I,
)


@dataclass(frozen=True)
class RouteDecision:
    formula_id: str
    confidence: float
    rationale_excerpt: str
    ambiguous: bool = False
    alternatives: tuple[str, ...] = ()


def _norm(q: str) -> str:
    return (q or "").strip().lower()


def _match_wells(q: str) -> str | None:
    if not re.search(r"\bwells\b", q):
        return None
    if re.search(
        r"\b(pe|pulmonary\s+embolism|pulmonary\s+embolus|wells\s+pe)\b",
        q,
    ):
        return _WELLS_PE
    if re.search(
        r"\b(dvt|deep\s+vein|deep\s+venous|wells\s+dvt|for\s+dvt)\b",
        q,
    ):
        return _WELLS_DVT
    return None


def _match_cha2ds2_family(q: str) -> str | None:
    if re.search(r"\bcha2ds2\b", q) or re.search(r"\bcha2ds2[\s-]?vasc\b", q):
        return _CHA2DS2_VASC
    if re.search(r"\bvasc\b", q) and re.search(r"\b(af|atrial|fibrillation|stroke\s+risk)\b", q):
        return _CHA2DS2_VASC
    if re.search(r"\bchads2\b", q) and not re.search(r"\bcha2ds2\b", q):
        return _CHADS2
    return None


def _match_other_named_scores(q: str) -> str | None:
    if re.search(r"\bcurb[\s-]?65\b", q) or re.search(r"\bcurb65\b", q):
        return _CURB65
    if re.search(r"\bmeld\b", q):
        return _MELD_NA
    if re.search(r"\bhas[\s-]?bled\b", q) or re.search(r"\bhasbled\b", q):
        return _HAS_BLED
    if re.search(r"\bpadua\b", q):
        return _PADUA
    if re.search(r"\bqsofa\b", q):
        return _QSOFA
    return None


def resolve_named_score_in_query(user_query: str) -> str | None:
    """Return canonical formula_id when the query clearly names one score family."""
    q = _norm(user_query)
    if len(q) < 4 or not _NAMED_SCORE_RE.search(q):
        return None
    for fn in (_match_wells, _match_cha2ds2_family, _match_other_named_scores):
        fid = fn(q)
        if fid:
            return fid
    return None


def resolve_formula_id_from_context(context: dict[str, Any]) -> str:
    """Best-effort formula_id for build_form / explain / interpret."""
    hint = context.get("operation_inputs_hint") or {}
    if isinstance(hint, dict):
        fid = hint.get("formula_id") or hint.get("formula")
        if fid:
            return str(fid).strip()

    explicit = str(context.get("formula_id") or "").strip()
    if explicit:
        return explicit

    uq = str(context.get("user_query") or context.get("user_utterance") or "")
    named = resolve_named_score_in_query(uq)
    if named:
        return named

    corpus_hit = _corpus_top_hit(uq)
    if corpus_hit:
        return corpus_hit
    return ""


def route_from_query(user_query: str) -> RouteDecision | None:
    """
    Pick a formula for ``route_request`` TAP purpose.

    Returns None when the query is too vague to route safely.
    """
    q = (user_query or "").strip()
    if len(q) < 3:
        return None

    wells = _match_wells(_norm(q))
    if wells is None and re.search(r"\bwells\b", _norm(q)):
        return RouteDecision(
            formula_id="",
            confidence=0.35,
            rationale_excerpt=(
                "Wells score mentioned but PE vs DVT context is unclear. "
                "Specify pulmonary embolism or DVT."
            ),
            ambiguous=True,
            alternatives=(_WELLS_PE, _WELLS_DVT),
        )

    named = resolve_named_score_in_query(q)
    if named:
        label = named.replace("-", " ").title()
        return RouteDecision(
            formula_id=named,
            confidence=0.88,
            rationale_excerpt=f"Named score match: {label}.",
        )

    hits = _corpus_search(q, top_k=3)
    if not hits:
        return None
    top = hits[0]
    if top.score < 0.42:
        return None
    alts = tuple(h.formula_id for h in hits[1:] if h.score >= 0.38)
    return RouteDecision(
        formula_id=top.formula_id,
        confidence=min(0.92, 0.55 + top.score * 0.4),
        rationale_excerpt=(
            f"Corpus retrieval: {top.name or top.formula_id} "
            f"(similarity {top.score:.2f})."
        ),
        ambiguous=len(alts) > 0 and (top.score - hits[1].score) < 0.06 if len(hits) > 1 else False,
        alternatives=alts,
    )


def _corpus_search(query: str, *, top_k: int = 3) -> list[Any]:
    try:
        from tool_medical_formulas.tap.corpus_vector_index import get_corpus_index

        idx = get_corpus_index()
        if not idx.formula_ids:
            return []
        return idx.search(query, top_k=top_k)
    except Exception as exc:
        logger.debug("corpus search skipped: %s", exc)
        return []


def _corpus_top_hit(query: str) -> str | None:
    hits = _corpus_search(query, top_k=1)
    if not hits:
        return None
    if hits[0].score < 0.45:
        return None
    return str(hits[0].formula_id)
