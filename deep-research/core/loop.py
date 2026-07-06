"""Research loop — orchestrates the 5 phases: plan → search → extract → converge → synthesize.

This is the heart of the deep-research plugin.  It ties together the
planner, searcher, extractor, convergence detector, and synthesizer into
a single iterative loop controlled by a :class:`Budget`.

The loop is fully async — every tool dispatch (web_search, web_extract,
web_crawl) and every LLM call (planner) is awaited.  The ``tool_dispatch``
callable is provided by the plugin handler (which receives it from Hermes'
plugin context).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from .budget import Budget, BudgetGuard
from .convergence import compute_novelty, should_converge
from .extractor import Extractor
from .planner import Planner, _get_auxiliary_client
from .searcher import Searcher
from .synthesizer import assemble
from .types import Page, ResearchGoal, ResearchResult, SearchQuery, SearchResult
from ..capabilities.registry import CapabilityRegistry

logger = logging.getLogger("hermes.deep-research.loop")


# ---------------------------------------------------------------------------
# ResearchLoop
# ---------------------------------------------------------------------------


class ResearchLoop:
    """Orchestrates the full iterative research process.

    Parameters
    ----------
    budget:
        Resource limits (max_pages, max_iterations, timeout, etc.).
    cap_registry:
        :class:`CapabilityRegistry` for discovering available tools.
    tool_dispatch:
        ``async dispatch(tool_name, args) -> str`` callable.  Required —
        the loop cannot function without it.
    planner:
        Optional pre-constructed :class:`Planner`.  If ``None``, one is
        built from the auxiliary client.
    """

    def __init__(
        self,
        budget: Budget,
        cap_registry: CapabilityRegistry,
        tool_dispatch: Callable[..., Any],
        planner: Optional[Planner] = None,
    ):
        self.budget = budget
        self.guard = BudgetGuard(budget)
        self.cap_registry = cap_registry
        self.dispatch = tool_dispatch

        # Components
        self.searcher = Searcher(tool_dispatch, max_depth=budget.max_depth)
        self.extractor = Extractor(cap_registry)
        self.planner = planner or self._make_planner()

        # State
        self.corpus: list[Page] = []
        self.queries_used: list[str] = []
        self.novelty_history: list[float] = []
        self.capabilities_used: set[str] = set()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, goal: ResearchGoal) -> ResearchResult:
        """Execute the full research loop and return a :class:`ResearchResult."""
        start_time = time.monotonic()
        logger.info("starting deep research: %r", goal.query)

        try:
            return await self._run_loop(goal, start_time)
        except Exception as exc:
            logger.error("research loop failed: %s", exc, exc_info=True)
            return ResearchResult(
                success=False,
                query=goal.query,
                iterations=0,
                pages_extracted=len(self.corpus),
                converged=False,
                novelty_final=0.0,
                sources=[],
                markdown="",
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _run_loop(self, goal: ResearchGoal, start_time: float) -> ResearchResult:
        iteration = 0
        converged = False

        # Phase 1: PLAN — generate initial queries
        self.guard.llm_calls += 1
        initial_queries = await self.planner.generate_initial_queries(
            goal, max_queries=self.budget.max_queries_per_iteration
        )
        self.queries_used.extend(initial_queries)
        logger.info("iteration 0: %d initial queries", len(initial_queries))

        current_queries = initial_queries

        # Capabilities used for metadata
        caps = self.cap_registry.discover()
        self.capabilities_used = set(caps.keys())

        # Main loop
        while not self.budget.exhausted(iteration, len(self.corpus)):
            # Check time budget
            if self.guard.is_expired():
                logger.info("time budget exhausted — stopping")
                break

            # Phase 2: SEARCH
            logger.info("iteration %d: searching %d queries", iteration, len(current_queries))
            search_results: list[SearchResult] = await self.searcher.search(
                current_queries,
                goal=goal,
                max_per_query=self.budget.max_results_per_query,
            )

            # Filter out already-visited URLs
            visited_urls = {p.url for p in self.corpus}
            new_urls = [r.url for r in search_results if r.url not in visited_urls]

            if not new_urls:
                logger.info("iteration %d: no new URLs found — converging", iteration)
                converged = True
                break

            # Cap at remaining page budget
            remaining = self.budget.max_pages - len(self.corpus)
            urls_to_extract = new_urls[:remaining]

            # Phase 3: EXTRACT
            if self.guard.can_extract(len(urls_to_extract)):
                # Determine which capabilities the extractor will use
                # (for metadata)
                logger.info(
                    "iteration %d: extracting %d URLs", iteration, len(urls_to_extract)
                )
                pages: list[Page] = await self.extractor.extract_batch(
                    urls_to_extract,
                    self.dispatch,
                    max_pages=remaining,
                )

                # Apply relevance from search results
                relevance_map = {r.url: r.relevance for r in search_results}
                for p in pages:
                    if p.relevance == 0.0 and p.url in relevance_map:
                        p.relevance = relevance_map[p.url]

                # Track extraction methods
                for p in pages:
                    if p.extraction_error is None:
                        self.capabilities_used.add(p.extracted_via)

                self.corpus.extend(pages)
            else:
                logger.warning("time budget too low for extraction — stopping")
                break

            # Phase 4: CONVERGE
            novelty = compute_novelty(pages, self.corpus[: -len(pages)] if pages else self.corpus)
            self.novelty_history.append(novelty)
            logger.info(
                "iteration %d: novelty=%.3f, corpus=%d pages",
                iteration,
                novelty,
                len(self.corpus),
            )

            if should_converge(
                self.novelty_history,
                threshold=self.budget.novelty_threshold,
                window=self.budget.convergence_window,
                min_iterations=self.budget.min_iterations,
            ):
                converged = True
                logger.info("convergence reached at iteration %d", iteration)
                break

            # Generate followup queries for next iteration
            if self.guard.can_call_llm():
                self.guard.llm_calls += 1
                followup = await self.planner.generate_followup_queries(
                    goal, self.corpus, max_queries=3
                )
                self.queries_used.extend(followup)
                current_queries = followup
            else:
                logger.info("LLM budget exhausted — stopping followup generation")
                break

            iteration += 1

        # Phase 5: SYNTHESIZE
        elapsed = time.monotonic() - start_time
        logger.info(
            "synthesizing: %d pages, %d iterations, %.1fs elapsed, converged=%s",
            len(self.corpus),
            iteration + 1,
            elapsed,
            converged,
        )

        synth = assemble(
            corpus=self.corpus,
            goal=goal,
            novelty_history=self.novelty_history,
            queries_used=self.queries_used,
            capabilities_used=sorted(self.capabilities_used),
        )

        # Build sources metadata
        sources_meta = [
            {
                "url": p.url,
                "title": p.title,
                "domain": p.domain,
                "relevance": round(p.relevance, 3),
                "content_chars": p.content_chars,
                "extracted_via": p.extracted_via,
            }
            for p in self.corpus
        ]

        novelty_final = self.novelty_history[-1] if self.novelty_history else 0.0

        return ResearchResult(
            success=True,
            query=goal.query,
            iterations=iteration + 1,
            pages_extracted=len(self.corpus),
            converged=converged,
            novelty_final=round(novelty_final, 4),
            sources=sources_meta,
            markdown=synth["markdown"],
            full_content_path=synth["full_content_path"],
            truncated=synth["truncated"],
            total_chars=synth["total_chars"],
        )

    # ------------------------------------------------------------------
    # Planner construction
    # ------------------------------------------------------------------

    def _make_planner(self) -> Planner:
        """Build a :class:`Planner` using the auxiliary LLM client."""
        client, model = _get_auxiliary_client()
        if client is not None:
            logger.info("planner using auxiliary LLM (model=%s)", model)
        else:
            logger.info("planner using fallback (no LLM) mode")
        return Planner(llm_client=client, model=model)


__all__ = ["ResearchLoop"]