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


def _is_multi_page_site(urls: list[str]) -> bool:
    """Heuristic: if ≥3 URLs share a domain, a crawl is worthwhile."""
    domain_counts: dict[str, int] = {}
    for u in urls:
        d = _domain(u)
        if d:
            domain_counts[d] = domain_counts.get(d, 0) + 1
    return any(c >= 3 for c in domain_counts.values())


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

        strategy = self._select_strategy(urls)
        if strategy is None:
            logger.warning(
                "no extraction capability available — skipping %d URLs", len(urls)
            )
            return []

        logger.debug(
            "extractor strategy: %s for %d urls", type(strategy).__name__, len(urls)
        )
        try:
            pages = await strategy.extract(urls, tool_dispatch, max_pages)
        except Exception as exc:  # noqa: BLE001
            logger.error("extraction strategy %s failed: %s", type(strategy).__name__, exc)
            return []

        # Filter out empty-content pages (but keep error pages for accounting)
        return pages

    def _select_strategy(self, urls: list[str]) -> Optional[Any]:
        """Pick the best extraction strategy based on available capabilities."""
        caps = self.cap_registry.discover()

        # Priority 1: deep-crawl for multi-page sites
        if "deep-crawl" in caps and _is_multi_page_site(urls):
            return DeepCrawlStrategy(caps["deep-crawl"])

        # Priority 2: browser-scrape for JS-heavy domains
        if "browser-scrape" in caps and _needs_js(urls):
            return BrowserScrapeStrategy(caps["browser-scrape"])

        # Priority 3: page-extract (workhorse)
        if "page-extract" in caps:
            return PageExtractStrategy(caps["page-extract"])

        # Priority 4: browser-scrape even if not JS-heavy (better than nothing)
        if "browser-scrape" in caps:
            return BrowserScrapeStrategy(caps["browser-scrape"])

        # Priority 5: deep-crawl even for single pages (last resort)
        if "deep-crawl" in caps:
            return DeepCrawlStrategy(caps["deep-crawl"])

        return None


__all__ = [
    "Extractor",
    "PageExtractStrategy",
    "DeepCrawlStrategy",
    "BrowserScrapeStrategy",
]