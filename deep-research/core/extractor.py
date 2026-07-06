"""Extraction abstraction for the deep-research loop.

The :class:`Extractor` picks the best available extraction strategy at
runtime, based on what the :class:`CapabilityRegistry` reports as
available.  Priority order:

1. **deep-crawl** (``web_crawl``) — used when multiple URLs share a domain
   and we want a recursive crawl rather than per-page fetches.
2. **browser-scrape** (``browser_navigate`` + ``browser_snapshot``) — used
   for JS-heavy pages (best-effort heuristic).
3. **page-extract** (``web_extract``) — default single-page extraction.
4. **Fallback** — skip the page, log a warning.

All strategies return ``list[Page]`` with raw markdown content.  No LLM
summarisation happens here — the synthesizer assembles everything later.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from .types import Page
from ..capabilities.base import BaseCapability, CapabilityContext
from ..capabilities.registry import CapabilityRegistry

logger = logging.getLogger("hermes.deep-research.extractor")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


_JS_HEURISTIC_DOMAINS = {
    "twitter.com",
    "x.com",
    "reddit.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "medium.com",
}


def _needs_js(urls: list[str]) -> bool:
    """Heuristic: any URL on a known SPA-heavy domain?"""
    for u in urls:
        d = _domain(u)
        if d in _JS_HEURISTIC_DOMAINS or any(
            d.endswith("." + x) for x in _JS_HEURISTIC_DOMAINS
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


class PageExtractStrategy:
    """Default strategy — one ``web_extract`` call for a batch of URLs."""

    def __init__(self, cap: BaseCapability):
        self.cap = cap

    async def extract(
        self,
        urls: list[str],
        tool_dispatch: Callable[..., Any],
        max_pages: int = 30,
    ) -> list[Page]:
        pages: list[Page] = []
        # web_extract accepts a list of URLs (up to 5 per call per its schema)
        BATCH = 5
        for i in range(0, len(urls), BATCH):
            if len(pages) >= max_pages:
                break
            batch = urls[i : i + BATCH]
            ctx = CapabilityContext(urls=batch, max_results=len(batch))
            try:
                raw_items = await self.cap.invoke(ctx, tool_dispatch)
            except Exception as exc:  # noqa: BLE001
                logger.warning("web_extract batch failed: %s", exc)
                for u in batch:
                    pages.append(
                        Page(
                            url=u,
                            title="",
                            content="",
                            domain=_domain(u),
                            extracted_via="web_extract",
                            extraction_error=str(exc),
                        )
                    )
                continue

            for item in raw_items:
                page = self._item_to_page(item)
                if page:
                    pages.append(page)
                    if len(pages) >= max_pages:
                        break
        return pages

    def _item_to_page(self, item: dict) -> Optional[Page]:
        url = item.get("url") or item.get("link") or ""
        if not url:
            return None
        content = item.get("content") or item.get("markdown") or item.get("text") or ""
        title = item.get("title") or ""
        return Page(
            url=url,
            title=title,
            content=content,
            domain=_domain(url),
            extracted_via="web_extract",
            extraction_error=item.get("error"),
        )


class DeepCrawlStrategy:
    """Uses ``web_crawl`` (firecrawl-crawler) for multi-page site extraction.

    Crawls once per domain (not per URL) to avoid redundant work.
    """

    def __init__(self, cap: BaseCapability, max_depth: int = 2, limit: int = 20):
        self.cap = cap
        self.max_depth = max_depth
        self.limit = limit
        self._crawled_domains: set[str] = set()

    async def extract(
        self,
        urls: list[str],
        tool_dispatch: Callable[..., Any],
        max_pages: int = 30,
    ) -> list[Page]:
        pages: list[Page] = []
        for url in urls:
            if len(pages) >= max_pages:
                break
            domain = _domain(url)
            if domain in self._crawled_domains:
                continue
            self._crawled_domains.add(domain)

            ctx = CapabilityContext(
                urls=[url],
                max_results=min(self.limit, max_pages - len(pages)),
                config={"maxDepth": self.max_depth, "limit": self.limit},
            )
            try:
                raw_items = await self.cap.invoke(ctx, tool_dispatch)
            except Exception as exc:  # noqa: BLE001
                logger.warning("web_crawl failed for %s: %s", url, exc)
                continue

            for item in raw_items:
                page = self._item_to_page(item, domain)
                if page:
                    pages.append(page)
                    if len(pages) >= max_pages:
                        break
        return pages

    def _item_to_page(self, item: dict, fallback_domain: str) -> Optional[Page]:
        url = item.get("url") or item.get("link") or ""
        if not url:
            return None
        content = item.get("content") or item.get("markdown") or item.get("text") or ""
        title = item.get("title") or ""
        return Page(
            url=url,
            title=title,
            content=content,
            domain=_domain(url) or fallback_domain,
            extracted_via="web_crawl",
        )


class BrowserScrapeStrategy:
    """Uses browser toolset (navigate + snapshot) for JS-heavy pages."""

    def __init__(self, cap: BaseCapability):
        self.cap = cap

    async def extract(
        self,
        urls: list[str],
        tool_dispatch: Callable[..., Any],
        max_pages: int = 30,
    ) -> list[Page]:
        pages: list[Page] = []
        for url in urls:
            if len(pages) >= max_pages:
                break
            ctx = CapabilityContext(urls=[url], max_results=1)
            try:
                raw_items = await self.cap.invoke(ctx, tool_dispatch)
            except Exception as exc:  # noqa: BLE001
                logger.warning("browser scrape failed for %s: %s", url, exc)
                pages.append(
                    Page(
                        url=url,
                        title="",
                        content="",
                        domain=_domain(url),
                        extracted_via="browser",
                        extraction_error=str(exc),
                    )
                )
                continue

            for item in raw_items:
                page = self._item_to_page(item, url)
                if page:
                    pages.append(page)
                    if len(pages) >= max_pages:
                        break
        return pages

    def _item_to_page(self, item: dict, fallback_url: str) -> Optional[Page]:
        url = item.get("url") or fallback_url
        # Browser snapshot returns text/snapshot, not "content"
        content = (
            item.get("content")
            or item.get("markdown")
            or item.get("text")
            or item.get("snapshot")
            or ""
        )
        if isinstance(content, list):
            content = "\n".join(str(c) for c in content)
        title = item.get("title") or ""
        return Page(
            url=url,
            title=title,
            content=str(content),
            domain=_domain(url),
            extracted_via="browser",
        )


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


class Extractor:
    """Auto-detects available extraction capabilities at runtime."""

    def __init__(self, cap_registry: CapabilityRegistry):
        self.cap_registry = cap_registry

    async def extract_batch(
        self,
        urls: list[str],
        tool_dispatch: Callable[..., Any],
        max_pages: int = 30,
    ) -> list[Page]:
        if not urls:
            return []

        # Select a strategy per-URL, then group URLs that share a strategy so
        # we still benefit from batching (e.g. web_extract's 5-URL batches).
        groups = self._group_urls_by_strategy(urls)

        all_pages: list[Page] = []
        for strategy, group_urls in groups:
            if len(all_pages) >= max_pages:
                break
            remaining = max_pages - len(all_pages)
            logger.debug(
                "extractor strategy: %s for %d urls (budget %d)",
                type(strategy).__name__, len(group_urls), remaining,
            )
            try:
                pages = await strategy.extract(group_urls, tool_dispatch, remaining)
            except Exception as exc:  # noqa: BLE001
                logger.error("extraction strategy %s failed: %s", type(strategy).__name__, exc)
                continue
            all_pages.extend(pages)

        # Filter out empty-content pages (but keep error pages for accounting)
        return all_pages

    def _group_urls_by_strategy(
        self, urls: list[str]
    ) -> list[tuple[Any, list[str]]]:
        """Group URLs by their best-fit strategy, preserving discovery order.

        Returns a list of ``(strategy, [urls])`` pairs suitable for batched
        extraction.  URLs needing the same strategy are grouped together so
        ``PageExtractStrategy`` can still issue 5-URL ``web_extract`` batches.
        """
        caps = self.cap_registry.discover()
        has_page_extract = "page-extract" in caps
        has_browser = "browser-scrape" in caps
        has_deep_crawl = "deep-crawl" in caps

        # Precompute domain counts to decide deep-crawl eligibility per-URL.
        domain_counts: dict[str, int] = {}
        for u in urls:
            d = _domain(u)
            if d:
                domain_counts[d] = domain_counts.get(d, 0) + 1

        # Map each URL to a strategy instance.  We reuse the same instance for
        # all URLs that share a strategy type so state (e.g. crawled_domains)
        # is preserved across the group.
        strategies: dict[str, Any] = {}

        def _get_strategy(name: str) -> Optional[Any]:
            if name not in strategies:
                if name == "deep-crawl" and has_deep_crawl:
                    strategies[name] = DeepCrawlStrategy(caps["deep-crawl"])
                elif name == "browser-scrape" and has_browser:
                    strategies[name] = BrowserScrapeStrategy(caps["browser-scrape"])
                elif name == "page-extract" and has_page_extract:
                    strategies[name] = PageExtractStrategy(caps["page-extract"])
                else:
                    strategies[name] = None
            return strategies[name]

        ordered: list[tuple[Any, list[str]]] = []
        index_by_strategy: dict[int, int] = {}  # id(strategy) -> position in ordered

        for url in urls:
            strat = self._select_strategy_for_url(
                url,
                domain_counts.get(_domain(url), 0),
                has_page_extract,
                has_browser,
                has_deep_crawl,
                _get_strategy,
            )
            if strat is None:
                logger.warning("no extraction capability for %s — skipping", url)
                continue
            key = id(strat)
            if key in index_by_strategy:
                ordered[index_by_strategy[key]][1].append(url)
            else:
                index_by_strategy[key] = len(ordered)
                ordered.append((strat, [url]))

        return ordered

    def _select_strategy_for_url(
        self,
        url: str,
        domain_count: int,
        has_page_extract: bool,
        has_browser: bool,
        has_deep_crawl: bool,
        get_strategy: Callable[[str], Optional[Any]],
    ) -> Optional[Any]:
        """Pick the best extraction strategy for a single URL."""
        # Priority 1: deep-crawl when this URL's domain has ≥3 URLs in the batch
        if has_deep_crawl and domain_count >= 3:
            return get_strategy("deep-crawl")

        # Priority 2: browser-scrape for JS-heavy domains
        if has_browser and _needs_js([url]):
            return get_strategy("browser-scrape")

        # Priority 3: page-extract (workhorse)
        if has_page_extract:
            return get_strategy("page-extract")

        # Priority 4: browser-scrape even if not JS-heavy (better than nothing)
        if has_browser:
            return get_strategy("browser-scrape")

        # Priority 5: deep-crawl even for single pages (last resort)
        if has_deep_crawl:
            return get_strategy("deep-crawl")

        return None


__all__ = [
    "Extractor",
    "PageExtractStrategy",
    "DeepCrawlStrategy",
    "BrowserScrapeStrategy",
]