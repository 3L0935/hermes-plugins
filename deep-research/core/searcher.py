"""Search abstraction for the deep-research loop.

Multi-strategy: tries web_search first, falls back to web_crawl for domain
discovery, then web_extract for known URLs. This makes the plugin work
regardless of which search backend is available.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from .types import ResearchGoal, SearchResult

logger = logging.getLogger("hermes.deep-research.searcher")

# ── Known domains for common queries (fallback when search returns nothing) ──
# These are used as crawl targets when web_search returns 0 results.
KNOWN_DOMAINS: dict[str, list[str]] = {
    "model": [
        "huggingface.co", "github.com", "ollama.com",
    ],
    "benchmark": [
        "huggingface.co", "artificialanalysis.ai", "lmarena.ai",
    ],
    "coding": [
        "github.com", "stackoverflow.com", "dev.to", "medium.com",
    ],
    "ai": [
        "huggingface.co", "arxiv.org", "reddit.com/r/LocalLLaMA",
        "news.ycombinator.com", "venturebeat.com",
    ],
    "default": [
        "huggingface.co", "github.com", "reddit.com",
        "medium.com", "arxiv.org",
    ],
}


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _tokenize(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())}


def _guess_domains(query: str) -> list[str]:
    """Guess relevant domains from query keywords."""
    q = query.lower()
    domains: list[str] = []

    # Model names → huggingface
    if re.search(r"qwen|llama|deepseek|gemma|mistral|mixtral|phi|falcon|step-", q):
        domains.extend(KNOWN_DOMAINS["model"])

    # Benchmark keywords
    if re.search(r"benchmark|tok/s|perf|speed|comparison|score", q):
        domains.extend(KNOWN_DOMAINS["benchmark"])

    # Coding keywords
    if re.search(r"code|coding|agent|program|dev|software|api", q):
        domains.extend(KNOWN_DOMAINS["coding"])

    # AI/ML keywords
    if re.search(r"ai|llm|model|neural|transformer|moe|gpu|vram", q):
        domains.extend(KNOWN_DOMAINS["ai"])

    # Deduplicate
    seen: set[str] = set()
    unique: list[str] = []
    for d in domains:
        if d not in seen:
            seen.add(d)
            unique.append(d)

    return unique or KNOWN_DOMAINS["default"][:]


def _guess_urls(query: str) -> list[str]:
    """Guess direct URLs from query (model names → HF pages, etc.).

    Generates specific, high-relevance URLs that can be passed directly to
    ``web_extract`` — avoiding the need to ``web_crawl`` generic homepages.

    For model names like "Qwen3.6-35B-A3B" or "Qwen/Qwen3.6-35B-A3B", this
    produces:
      - https://huggingface.co/Qwen/Qwen3.6-35B-A3B
      - https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF
      - https://huggingface.co/models?search=Qwen3.6-35B-A3B
      - https://huggingface.co/{model_name}  (bare name, may redirect)
    """
    urls: list[str] = []
    q = query.strip()

    # Match model names like "Qwen3.6-35B-A3B" or "Qwen/Qwen3.6-35B-A3B"
    # Pattern: word chars + at least one hyphen/dot group → likely a model
    model_match = re.search(
        r"([A-Za-z0-9]+(?:[-.][A-Za-z0-9]+)+)", q
    )
    if model_match:
        model_name = model_match.group(1)

        # Check for explicit org/model pattern (e.g. "Qwen/Qwen3.6-35B-A3B")
        org_model = re.search(r"([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+)", q)
        if org_model:
            full_path = org_model.group(0)
            # Only treat as org/model if the second part looks like a model
            # (contains a hyphen or dot, e.g. "Qwen3.6-35B-A3B" not "search")
            model_part = org_model.group(2)
            if re.search(r"[-.]", model_part):
                urls.append(f"https://huggingface.co/{full_path}")
                # Also try GGUF variant
                if not full_path.endswith("-GGUF"):
                    urls.append(f"https://huggingface.co/unsloth/{model_part}-GGUF")
                    urls.append(f"https://huggingface.co/bartowski/{model_part}-GGUF")
            else:
                # org/model didn't match — use bare model name
                urls.append(f"https://huggingface.co/models?search={model_name}")
                urls.append(f"https://huggingface.co/{model_name}")
        else:
            # No org prefix — try common orgs for well-known model families
            # Extract the brand prefix (e.g. "Qwen" from "Qwen3.6-35B-A3B")
            brand_match = re.match(r"^([A-Za-z]+)", model_name)
            brand = brand_match.group(1) if brand_match else ""

            # Common brand → org mappings on HuggingFace
            org_map = {
                "qwen": "Qwen",
                "llama": "meta-llama",
                "deepseek": "deepseek-ai",
                "gemma": "google",
                "mistral": "mistralai",
                "mixtral": "mistralai",
                "phi": "microsoft",
                "falcon": "tiiuae",
                "step": "stepfun-ai",
            }
            org = org_map.get(brand.lower(), brand)

            if org:
                urls.append(f"https://huggingface.co/{org}/{model_name}")
            urls.append(f"https://huggingface.co/models?search={model_name}")
            # Try unsloth GGUF variant (very common for quantized models)
            urls.append(f"https://huggingface.co/unsloth/{model_name}-GGUF")
            urls.append(f"https://huggingface.co/bartowski/{model_name}-GGUF")
            # Bare name as last resort
            urls.append(f"https://huggingface.co/{model_name}")

    return urls


# ── Searcher ─────────────────────────────────────────────────────────────────


class Searcher:
    """Multi-strategy search: web_search → web_extract guessed URLs → web_crawl fallback.

    Tries each strategy in order until results are found. When search returns
    nothing, the searcher first tries to extract directly from guessed specific
    URLs (e.g. HuggingFace model pages) before falling back to crawling generic
    homepages.
    """

    def __init__(self, tool_dispatch: Callable[..., Any], max_depth: int = 1):
        self._dispatch = tool_dispatch
        self._max_depth = max(1, max_depth)

    async def search(
        self,
        queries: list[str],
        goal: Optional[ResearchGoal] = None,
        max_per_query: int = 10,
    ) -> list[SearchResult]:
        """Run search using the best available strategy.

        Strategy priority:
        1. web_search (SERP) — works with SearXNG, Tavily, Brave, etc.
        2. web_extract on guessed specific URLs — model pages, repo pages
        3. web_crawl on guessed domains — deeper traversal with max_depth
        """
        all_results: list[SearchResult] = []

        # Strategy 1: web_search
        all_results = await self._try_web_search(queries, max_per_query)
        if all_results:
            logger.info("web_search returned %d results", len(all_results))
            if goal:
                all_results = self._filter_domains(all_results, goal)
            all_results.sort(key=lambda r: r.relevance, reverse=True)
            return all_results

        # Strategy 2: web_extract on guessed specific URLs (model pages etc.)
        # This is tried BEFORE web_crawl because it targets specific, relevant
        # pages (e.g. https://huggingface.co/Qwen/Qwen3.6-35B-A3B) rather than
        # generic homepages.
        logger.info("web_search returned 0 results — trying web_extract on guessed URLs")
        all_results = await self._try_web_extract_guess(queries)
        if all_results:
            logger.info("web_extract guess returned %d results", len(all_results))
            if goal:
                all_results = self._filter_domains(all_results, goal)
            all_results.sort(key=lambda r: r.relevance, reverse=True)
            return all_results

        # Strategy 3: web_crawl on guessed domains (deeper traversal)
        logger.info("web_extract guess also empty — trying web_crawl discovery")
        all_results = await self._try_web_crawl_discovery(queries, max_per_query)
        if all_results:
            logger.info("web_crawl discovery returned %d results", len(all_results))
            if goal:
                all_results = self._filter_domains(all_results, goal)
            all_results.sort(key=lambda r: r.relevance, reverse=True)
            return all_results

        logger.warning("all search strategies returned 0 results")
        return []

    # ── Strategy 1: web_search ──────────────────────────────────────────────

    async def _try_web_search(
        self, queries: list[str], max_per_query: int
    ) -> list[SearchResult]:
        """Try standard web_search for each query."""
        all_results: list[SearchResult] = []
        seen_urls: set[str] = set()

        for query in queries:
            try:
                raw = await self._dispatch(
                    "web_search",
                    {"query": query, "limit": max_per_query},
                )
            except Exception as exc:
                logger.debug("web_search failed for %r: %s", query, exc)
                continue

            results = self._parse_search_results(raw, query)
            for r in results:
                if r.url in seen_urls:
                    continue
                seen_urls.add(r.url)
                all_results.append(r)

        return all_results

    # ── Strategy 2: web_crawl discovery ─────────────────────────────────────

    async def _try_web_crawl_discovery(
        self, queries: list[str], max_per_query: int
    ) -> list[SearchResult]:
        """Use web_crawl on guessed domains to discover relevant pages."""
        all_domains: list[str] = []
        for query in queries:
            all_domains.extend(_guess_domains(query))

        seen_domains: set[str] = set()
        unique_domains: list[str] = []
        for d in all_domains:
            if d not in seen_domains:
                seen_domains.add(d)
                unique_domains.append(d)

        if not unique_domains:
            logger.info("web_crawl discovery: no domains guessed from queries")
            return []

        all_results: list[SearchResult] = []
        seen_urls: set[str] = set()
        pages_per_domain = max(1, max_per_query // len(unique_domains))

        for domain in unique_domains[:5]:
            try:
                url = f"https://{domain}"
                logger.info(
                    "web_crawl discovery: crawling %s (limit=%d, maxDepth=%d)",
                    url, pages_per_domain, self._max_depth,
                )
                raw = await self._dispatch("web_crawl", {
                    "url": url,
                    "limit": pages_per_domain,
                    "maxDepth": self._max_depth,
                })
                logger.info("web_crawl discovery: %s returned %s", domain, type(raw).__name__)
            except Exception as exc:
                logger.warning("web_crawl failed for %s: %s: %s", domain, type(exc).__name__, exc)
                continue

            results = self._parse_crawl_results(raw, queries)
            logger.info("web_crawl discovery: %s -> %d results", domain, len(results))
            for r in results:
                if r.url in seen_urls:
                    continue
                seen_urls.add(r.url)
                all_results.append(r)

        return all_results

    # ── Strategy 3: web_extract on guessed URLs ─────────────────────────────

    async def _try_web_extract_guess(
        self, queries: list[str]
    ) -> list[SearchResult]:
        """Use web_extract on directly guessed URLs (model pages, etc.)."""
        all_urls: list[str] = []
        for query in queries:
            all_urls.extend(_guess_urls(query))

        if not all_urls:
            return []

        all_results: list[SearchResult] = []
        seen_urls: set[str] = set()

        for url in all_urls[:8]:  # max 8 URLs (was 5, increased for model variants)
            if url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                raw = await self._dispatch("web_extract", {"urls": [url]})
            except Exception as exc:
                logger.debug("web_extract failed for %s: %s", url, exc)
                continue

            result = self._parse_extract_result(raw, url, queries)
            if result:
                all_results.append(result)

        return all_results

    # ── Parsers ─────────────────────────────────────────────────────────────

    def _parse_search_results(
        self, raw: Any, query: str
    ) -> list[SearchResult]:
        """Parse web_search JSON into SearchResult list."""
        items: list[dict] = []

        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                logger.warning("unparseable web_search output: %.120s", raw)
                return []

        if isinstance(raw, list):
            items = [i for i in raw if isinstance(i, dict)]
        elif isinstance(raw, dict):
            for key in ("results", "data", "items", "organic"):
                if key in raw and isinstance(raw[key], list):
                    items = [i for i in raw[key] if isinstance(i, dict)]
                    break
            else:
                if "url" in raw or "link" in raw:
                    items = [raw]

        query_tokens = _tokenize(query)
        results: list[SearchResult] = []
        for item in items:
            url = item.get("url") or item.get("link") or item.get("href") or ""
            if not url:
                continue
            title = item.get("title") or item.get("name") or ""
            snippet = item.get("snippet") or item.get("description") or ""
            snippet = snippet if isinstance(snippet, str) else str(snippet)

            text_tokens = _tokenize(f"{title} {snippet}")
            relevance = 0.0
            if query_tokens:
                relevance = len(query_tokens & text_tokens) / len(query_tokens)

            results.append(SearchResult(
                url=url, title=title, snippet=snippet,
                domain=_domain(url), relevance=relevance,
            ))

        return results

    def _parse_crawl_results(
        self, raw: Any, queries: list[str]
    ) -> list[SearchResult]:
        """Parse web_crawl JSON into SearchResult list."""
        all_tokens = set()
        for q in queries:
            all_tokens |= _tokenize(q)

        try:
            if isinstance(raw, str):
                raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []

        if not isinstance(raw, dict):
            return []

        pages = raw.get("results", []) if raw.get("success") else []
        if not pages and isinstance(raw.get("data"), list):
            pages = raw["data"]

        results: list[SearchResult] = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            url = page.get("url") or page.get("metadata", {}).get("url", "")
            if not url:
                continue
            title = page.get("title") or page.get("metadata", {}).get("title", "")
            content = page.get("content") or page.get("markdown", "") or ""
            snippet = content[:300] if content else ""

            text_tokens = _tokenize(f"{title} {snippet}")
            relevance = 0.0
            if all_tokens:
                relevance = len(all_tokens & text_tokens) / len(all_tokens)

            results.append(SearchResult(
                url=url, title=title, snippet=snippet,
                domain=_domain(url), relevance=relevance,
            ))

        return results

    def _parse_extract_result(
        self, raw: Any, url: str, queries: list[str]
    ) -> Optional[SearchResult]:
        """Parse web_extract JSON into a single SearchResult."""
        all_tokens = set()
        for q in queries:
            all_tokens |= _tokenize(q)

        try:
            if isinstance(raw, str):
                raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

        if not isinstance(raw, dict):
            return None

        # web_extract returns {"results": [{"url": ..., "title": ..., "content": ...}]}
        results_list = raw.get("results", [])
        if results_list and isinstance(results_list, list):
            item = results_list[0]
            if isinstance(item, dict):
                title = item.get("title", "")
                content = item.get("content", "")
                snippet = content[:300] if content else ""

                text_tokens = _tokenize(f"{title} {snippet}")
                relevance = 0.0
                if all_tokens:
                    relevance = len(all_tokens & text_tokens) / len(all_tokens)

                return SearchResult(
                    url=url, title=title, snippet=snippet,
                    domain=_domain(url), relevance=relevance,
                )

        return None

    # ── Domain filtering ───────────────────────────────────────────────────

    def _filter_domains(
        self, results: list[SearchResult], goal: ResearchGoal
    ) -> list[SearchResult]:
        filtered: list[SearchResult] = []
        for r in results:
            d = r.domain
            if goal.exclude_domains and any(
                d == ex or d.endswith("." + ex) for ex in goal.exclude_domains
            ):
                continue
            if goal.focus_domains and not any(
                d == f or d.endswith("." + f) for f in goal.focus_domains
            ):
                continue
            filtered.append(r)
        return filtered


__all__ = ["Searcher"]
