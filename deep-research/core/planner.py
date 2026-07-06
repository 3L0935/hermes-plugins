"""Planner — generates search queries via an auxiliary LLM.

The planner makes two kinds of calls:

1. **Initial queries**: decompose the research question into 3-5 search
   queries.
2. **Followup queries**: given the corpus so far, generate 2-3 new queries
   to fill information gaps.

It uses Hermes' auxiliary LLM client (cheaper/faster than the main model)
via :func:`_get_auxiliary_client`.  If no auxiliary client is available, it
falls back to a deterministic keyword-based decomposition so the loop can
still run.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Optional

from .types import ResearchGoal
if TYPE_CHECKING:
    from .types import Page

logger = logging.getLogger("hermes.deep-research.planner")


# ---------------------------------------------------------------------------
# Auxiliary client
# ---------------------------------------------------------------------------


def _get_auxiliary_client() -> tuple[Optional[Any], Optional[str]]:
    """Get Hermes' auxiliary LLM client for planner/convergence calls.

    Returns ``(client, model_name)`` or ``(None, None)`` if unavailable.

    Uses the **async** auxiliary client because the planner runs inside an
    async context and awaits ``client.chat.completions.create``.  The sync
    variant returns a regular ``OpenAI`` whose ``create`` is not a coroutine,
    causing ``TypeError: object ChatCompletion can't be used in 'await'``.
    """
    # Prefer the async entry point (returns AsyncOpenAI or equivalent).
    entry_points = [
        ("agent.auxiliary_client", "get_async_text_auxiliary_client"),
        ("agent.auxiliary_client", "get_text_auxiliary_client"),
        ("agent.auxiliary", "get_async_text_auxiliary_client"),
        ("agent.auxiliary", "get_text_auxiliary_client"),
        ("agent.auxiliary_client", "get_auxiliary_client"),
        ("agent.auxiliary", "get_auxiliary_client"),
    ]

    for module_name, func_name in entry_points:
        try:
            mod = __import__(module_name, fromlist=[func_name])
            func = getattr(mod, func_name, None)
            if func is None:
                continue
            result = func()
            # Some versions return (client, model), others return just client
            if isinstance(result, tuple):
                client, model = result[0], result[1] if len(result) > 1 else None
            else:
                client, model = result, None
            if client is not None:
                logger.debug(
                    "auxiliary client loaded from %s.%s, model=%s",
                    module_name,
                    func_name,
                    model,
                )
                return client, model
        except ImportError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("auxiliary client attempt %s.%s failed: %s", module_name, func_name, exc)
            continue

    logger.warning("no auxiliary LLM client available — planner will use fallback mode")
    return None, None


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class Planner:
    """Generates search queries using an auxiliary LLM.

    Parameters
    ----------
    llm_client:
        An OpenAI-compatible async client (has
        ``client.chat.completions.create``).  If ``None``, the planner
        operates in *fallback mode* (deterministic keyword decomposition).
    model:
        Model name to pass to the client.  If ``None``, the client's
        default is used.
    """

    def __init__(self, llm_client: Optional[Any] = None, model: Optional[str] = None):
        self.client = llm_client
        self.model = model

    # ------------------------------------------------------------------
    # Initial queries
    # ------------------------------------------------------------------

    async def generate_initial_queries(
        self, goal: ResearchGoal, max_queries: int = 5
    ) -> list[str]:
        """Decompose the research question into 3-5 web search queries."""
        if self.client is None:
            return self._fallback_initial_queries(goal, max_queries)

        focus = ", ".join(goal.focus_domains) if goal.focus_domains else "none"
        exclude = ", ".join(goal.exclude_domains) if goal.exclude_domains else "none"

        prompt = f"""Decompose this research question into {max_queries} web search queries.
Return ONLY the queries, one per line. No numbering, no explanation, no preamble.

Research question: {goal.query}

Focus domains: {focus}
Exclude domains: {exclude}
"""
        try:
            response = await self._call_llm(prompt, max_tokens=500, temperature=0.3)
            queries = self._parse_queries(response, max_queries)
            if queries:
                return queries
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM initial-query call failed: %s — using fallback", exc)

        return self._fallback_initial_queries(goal, max_queries)

    # ------------------------------------------------------------------
    # Followup queries
    # ------------------------------------------------------------------

    async def generate_followup_queries(
        self,
        goal: ResearchGoal,
        corpus: list[Page],
        max_queries: int = 3,
    ) -> list[str]:
        """Generate 2-3 new queries to fill information gaps."""
        if self.client is None:
            return self._fallback_followup_queries(goal, corpus, max_queries)

        findings = self._summarize_findings(corpus)
        prompt = f"""You are researching: {goal.query}

Findings so far ({len(corpus)} pages extracted):
{findings}

Generate {max_queries} NEW search queries to fill information gaps.
Focus on aspects not yet covered. Return ONLY the queries, one per line.
No numbering, no explanation.
"""
        try:
            response = await self._call_llm(prompt, max_tokens=400, temperature=0.4)
            queries = self._parse_queries(response, max_queries)
            if queries:
                return queries
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM followup-query call failed: %s — using fallback", exc)

        return self._fallback_followup_queries(goal, corpus, max_queries)

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    async def _call_llm(
        self, prompt: str, max_tokens: int = 500, temperature: float = 0.3
    ) -> str:
        """Call the auxiliary LLM with a single user message."""
        kwargs: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if self.model:
            kwargs["model"] = self.model

        r = await self.client.chat.completions.create(**kwargs)
        return r.choices[0].message.content

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_queries(response: str, max_n: int) -> list[str]:
        """Parse an LLM response into a clean list of query strings."""
        lines = []
        for line in response.strip().splitlines():
            # Strip numbering like "1. " or "1) "
            cleaned = re.sub(r"^\s*\d+[\.\)]\s*", "", line).strip()
            # Strip quotes
            cleaned = cleaned.strip('"`').strip()
            if cleaned and len(cleaned) > 3:
                lines.append(cleaned)
        return lines[:max_n]

    # ------------------------------------------------------------------
    # Fallback (no LLM) — deterministic keyword decomposition
    # ------------------------------------------------------------------

    def _fallback_initial_queries(
        self, goal: ResearchGoal, max_queries: int
    ) -> list[str]:
        """Generate queries without an LLM — decompose into shorter sub-queries."""
        q = goal.query.strip()

        # Extract key terms: split on common separators, remove noise
        # Split on " vs ", " or ", " comparison ", " for ", commas
        parts = re.split(r'\s+(?:vs|or|comparison|for|and|that|with)\s+', q, flags=re.IGNORECASE)
        parts = [p.strip() for p in parts if len(p.strip()) > 10]

        queries = []
        key_terms: list[str] = []  # defined here so padding below never hits UnboundLocalError

        # Extract model names (e.g. Qwen3.6-35B-A3B) and key noun phrases from
        # the whole query up front — both branches use them.
        model_names = re.findall(r'[A-Z][a-zA-Z0-9]+(?:[-.][a-zA-Z0-9]+)+', q)
        words = re.findall(r'[A-Za-z][a-z]{3,}(?:[A-Z][a-z]+)*', q)
        key_terms = model_names + [w for w in words if w.lower() not in _STOPWORDS_FALLBACK]

        # If the query has clear sub-parts, use them
        if len(parts) >= 2:
            queries = parts[:max_queries]
        else:
            # key_terms already extracted above

            if len(key_terms) >= 3:
                # Group into sub-queries
                mid = len(key_terms) // 2
                queries.append(f"{' '.join(key_terms[:mid])} performance benchmark")
                queries.append(f"{' '.join(key_terms[mid:])} comparison review")
                queries.append(f"{' '.join(key_terms[:3])} tok/s speed")
            else:
                queries = [q]

        # Add generic variants
        if len(queries) < max_queries:
            lead = key_terms[0] if key_terms else q
            queries.append(f"{lead} local inference 24GB VRAM")

        if len(queries) < max_queries:
            lead = key_terms[0] if key_terms else q
            queries.append(f"{lead} coding agentic benchmark")

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for x in queries:
            if x not in seen:
                seen.add(x)
                unique.append(x)
        return unique[:max_queries]

    def _fallback_followup_queries(
        self, goal: ResearchGoal, corpus: list[Page], max_queries: int
    ) -> list[str]:
        """Generate followup queries without an LLM — use corpus gaps."""
        # Collect domains seen
        seen_domains = {p.domain for p in corpus if p.domain}
        # Collect top keywords from corpus
        from collections import Counter

        words = Counter()
        for p in corpus:
            words.update(re.findall(r"[a-z]{4,}", p.title.lower()))
            words.update(re.findall(r"[a-z]{4,}", p.content[:2000].lower()))

        top_keywords = [w for w, _ in words.most_common(10) if w not in _STOPWORDS_FALLBACK]

        queries: list[str] = []
        # Query 1: combine original + top corpus keyword
        if top_keywords:
            queries.append(f"{goal.query} {top_keywords[0]}")
        # Query 2: add "analysis" or "deep dive"
        queries.append(f"{goal.query} analysis deep dive")
        # Query 3: add "review" or "experience"
        queries.append(f"{goal.query} review experience")

        return queries[:max_queries]

    # ------------------------------------------------------------------
    # Findings summary for followup prompt
    # ------------------------------------------------------------------

    @staticmethod
    def _summarize_findings(corpus: list[Page]) -> str:
        """Lightweight summary: first 200 chars of each page + domain.

        Only the last 10 pages are included to keep the prompt small.
        """
        lines: list[str] = []
        for p in corpus[-10:]:
            snippet = p.content[:200].replace("\n", " ").strip()
            lines.append(f"- [{p.domain}] {p.title}: {snippet}...")
        return "\n".join(lines) if lines else "(no pages extracted yet)"


_STOPWORDS_FALLBACK = frozenset(
    "the and for are was were been being have has had having this that these those with from into".split()
)


__all__ = ["Planner", "_get_auxiliary_client"]