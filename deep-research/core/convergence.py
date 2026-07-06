"""Convergence detection — lightweight TF-IDF novelty scoring.

No external dependencies (no numpy, no scikit-learn).  Uses ``Counter`` and
set overlap on top tokens to compute a Jaccard-like novelty score.

The loop calls :func:`compute_novelty` after each extraction phase to see
how much *new* information the latest pages added compared to the existing
corpus.  :func:`should_converge` decides whether to stop iterating.
"""

from __future__ import annotations

import re
from collections import Counter
from math import sqrt
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types import Page

# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")

# Generic English stopwords and web-noise tokens only.
# Domain-significant terms (python, docker, kubernetes, github, react, linux,
# windows, chrome, etc.) are intentionally NOT stopwords here — this plugin's
# use case is LLM/dev research where those terms carry signal.
_STOPWORDS = frozenset(
    """
    the and for are was were been being have has had having will would could
    should may might must shall can this that these those with from into onto
    upon over under about above below between among through during before after
    not nor but yet however therefore thus hence more most less least very much
    many some any all both each few other such own same than then also only just
    here there where when why how what which who whom whose com org net http https
    www html php css
    monday tuesday wednesday thursday friday saturday sunday january february
    march april may june july august september october november december
    """.split()
)


def _tokenize(text: str) -> list[str]:
    return [w for w in _TOKEN_RE.findall(text.lower()) if w not in _STOPWORDS]


def _tokenize_pages(pages: list[Page]) -> list[str]:
    tokens: list[str] = []
    for p in pages:
        tokens.extend(_tokenize(p.title))
        tokens.extend(_tokenize(p.content))
    return tokens


def _top_tokens(tokens: list[str], n: int = 200) -> set[str]:
    return {w for w, _ in Counter(tokens).most_common(n)}


# ---------------------------------------------------------------------------
# TF-IDF cosine similarity (lightweight, no numpy)
# ---------------------------------------------------------------------------


def _tf_vector(tokens: list[str]) -> dict[str, float]:
    counts = Counter(tokens)
    total = len(tokens) or 1
    return {w: c / total for w, c in counts.items()}


def _cosine(v1: dict[str, float], v2: dict[str, float]) -> float:
    if not v1 or not v2:
        return 0.0
    dot = sum(v1[w] * v2.get(w, 0.0) for w in v1)
    n1 = sqrt(sum(x * x for x in v1.values()))
    n2 = sqrt(sum(x * x for x in v2.values()))
    if n1 == 0 or n2 == 0:
        return 0.0
    return dot / (n1 * n2)


# ---------------------------------------------------------------------------
# Novelty
# ---------------------------------------------------------------------------


def compute_novelty(
    new_pages: list[Page],
    existing_corpus: list[Page],
    method: str = "jaccard",
) -> float:
    """Novelty score: how much new information do ``new_pages`` add?

    Returns a float in ``[0.0, 1.0]``:

    * ``0.0`` — fully duplicate (new pages add nothing)
    * ``1.0`` — entirely new content

    Two methods are supported:

    * ``"jaccard"`` (default) — Jaccard distance on top-200 token sets.
      Fast, simple, good enough for convergence detection.
    * ``"cosine"`` — 1 - cosine similarity of TF vectors.  Slightly more
      sensitive to term frequency differences.
    """
    if not new_pages:
        return 0.0
    if not existing_corpus:
        return 1.0

    if method == "cosine":
        existing_vec = _tf_vector(_tokenize_pages(existing_corpus))
        new_vec = _tf_vector(_tokenize_pages(new_pages))
        sim = _cosine(existing_vec, new_vec)
        return max(0.0, min(1.0, 1.0 - sim))

    # Default: Jaccard on top tokens
    existing_top = _top_tokens(_tokenize_pages(existing_corpus), n=200)
    new_top = _top_tokens(_tokenize_pages(new_pages), n=200)

    overlap = len(existing_top & new_top)
    union = len(existing_top | new_top)

    if union == 0:
        return 0.0
    return 1.0 - (overlap / union)


# ---------------------------------------------------------------------------
# Convergence decision
# ---------------------------------------------------------------------------


def should_converge(
    novelty_history: list[float],
    threshold: float = 0.15,
    window: int = 2,
    min_iterations: int = 2,
) -> bool:
    """Decide whether the research loop should stop iterating.

    Convergence is reached when the last ``window`` novelty scores are all
    below ``threshold`` **and** at least ``min_iterations`` have run.
    """
    if len(novelty_history) < min_iterations:
        return False
    if len(novelty_history) < window:
        return False
    recent = novelty_history[-window:]
    return all(n < threshold for n in recent)


# ---------------------------------------------------------------------------
# Duplicate detection (used by synthesizer)
# ---------------------------------------------------------------------------


def is_near_duplicate(
    page_a: Page,
    page_b: Page,
    threshold: float = 0.85,
) -> bool:
    """Check if two pages are near-duplicates via token-set Jaccard similarity.

    Returns ``True`` if the similarity (overlap/union of top tokens) is
    above ``threshold``.
    """
    tokens_a = _top_tokens(_tokenize(page_a.content), n=100)
    tokens_b = _top_tokens(_tokenize(page_b.content), n=100)
    if not tokens_a or not tokens_b:
        return False
    union = tokens_a | tokens_b
    if not union:
        return False
    sim = len(tokens_a & tokens_b) / len(union)
    return sim >= threshold


__all__ = [
    "compute_novelty",
    "should_converge",
    "is_near_duplicate",
]