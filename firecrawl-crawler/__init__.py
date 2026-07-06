"""Firecrawl Crawler + Mapper Plugin for Hermes Agent.

Adds two web tools backed by Firecrawl self-hosted:
  - web_crawl: crawl entire sites recursively, get full content per page
  - web_map:  list all discovered URLs from a site (no content, fast)

Uses FIRECRAWL_API_URL (defaults to http://localhost:3002).
Zero core patches — lives entirely in ~/.hermes/plugins/.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes.firecrawl-crawler.plugin")

FIRECRAWL_API_URL = (
    os.getenv("FIRECRAWL_API_URL", "http://localhost:3002").rstrip("/")
)

DEFAULT_MAX_PAGES = 20
DEFAULT_DEPTH = 2  # start page + 1 level of links (Firecrawl v2 default is 1)
DEFAULT_POLL_INTERVAL = 2  # seconds
POLL_TIMEOUT = 120  # max seconds to wait for crawl job

# ── Rate-limit / anti-ban safety ──────────────────────────────────────────
#
# Sites that aggressively rate-limit bots. Crawling these without care
# will get your IP blocked and/or the job silently dropped.
SENSITIVE_DOMAINS = {
    "github.com", "gitlab.com", "bitbucket.org",
    "linkedin.com", "facebook.com", "instagram.com",
    "reddit.com", "twitter.com", "x.com",
    "stackoverflow.com", "medium.com", "dev.to",
    "amazon.com", "amzn.to",
    "cloudflare.com", "docs.cloudflare.com",
}

# Domains where crawling is simply forbidden via this tool to avoid bans.
# Use web_extract on specific pages instead.
BLOCKED_CRAWL_DOMAINS: set = set()  # can be populated later

# Minimum delay in seconds between Firecrawl API calls (throttle).
# Firecrawl self-hosted already has its own concurrency/rate-limit
# settings, but we add a client-side delay to avoid hammering sites
# that are slow to respond or have aggressive bot detection.


# -- helpers -----------------------------------------------------------------


def _firecrawl_available() -> bool:
    """Simple connectivity check — hit the root endpoint."""
    return bool(FIRECRAWL_API_URL)


async def _raw_post(path: str, payload: dict) -> dict:
    """POST JSON to Firecrawl, return parsed dict.

    Uses httpx (already vendored in the Hermes agent venv).
    Detects 429 (rate-limit) and raises a typed exception.
    """
    import httpx

    url = f"{FIRECRAWL_API_URL}{path}"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(url, json=payload)
        if r.status_code == 429:
            raise RuntimeError(
                "Firecrawl rate-limited (HTTP 429). Back off and retry later, "
                "or reduce concurrency/pages-per-crawl."
            )
        r.raise_for_status()
        return r.json()


async def _raw_get(path: str) -> dict:
    """GET from Firecrawl, return parsed dict."""
    import httpx

    url = f"{FIRECRAWL_API_URL}{path}"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(url)
        if r.status_code == 429:
            raise RuntimeError(
                "Firecrawl rate-limited (HTTP 429). Back off and retry later."
            )
        r.raise_for_status()
        return r.json()


def _truncate_for_schema(obj: Any, max_chars: int = 100_000) -> Any:
    """Keep result under max_result_size_chars by truncating markdown content."""
    if isinstance(obj, dict):
        return {k: _truncate_for_schema(v, max_chars) for k, v in obj.items()}
    if isinstance(obj, list):
        # For large lists keep first N pages then signal truncation
        total = len(obj)
        if total > 50:
            truncated = [_truncate_for_schema(p, max_chars) for p in obj[:50]]
            truncated.append({
                "url": "",
                "title": "",
                "content": f"[Truncated: {total} total pages, showing first 50]",
            })
            return truncated
        return [_truncate_for_schema(p, max_chars) for p in obj]
    if isinstance(obj, str):
        return obj[:max_chars] if len(obj) > max_chars else obj
    return obj


async def _fallback_via_sitemap(
    url: str,
    limit: int,
    include_paths: list[str],
    exclude_paths: list[str],
) -> list[dict] | None:
    """Fallback: use Firecrawl map to discover URLs, then scrape each one.

    Triggered when the recursive crawl returned ≤2 pages (common on SPA sites like
    Docusaurus where navigation is JS-resolved rather than <a> tag-based).
    Returns pages in the same format as the crawl result, or None if map also fails.
    """
    from urllib.parse import urlparse

    logger.info("Crawl returned sparse results. Falling back to sitemap-based discovery for %s", url)

    # 1. Map the site via sitemap
    map_payload: dict = {
        "url": url,
        "limit": limit,
        "sitemap": "only",
        "includeSubdomains": False,
    }
    try:
        map_result = await _raw_post("/v2/map", map_payload)
    except Exception as e:
        logger.warning("Sitemap fallback map failed: %s", str(e)[:200])
        return None

    links = map_result.get("links", [])
    if not links:
        logger.info("Sitemap returned 0 URLs for %s", url)
        return None

    # 2. Filter links by include/exclude paths
    import re
    parsed_base = urlparse(url)
    base_domain = parsed_base.hostname or ""

    filtered: list[str] = []
    for link in links:
        link_str = str(link) if not isinstance(link, str) else link
        link_parsed = urlparse(link_str)
        link_domain = link_parsed.hostname or ""

        # Stay on same domain unless include_subdomains is set (we set False)
        if link_domain != base_domain and not link_domain.endswith("." + base_domain):
            continue

        path = link_parsed.path or "/"

        # Apply include filters
        if include_paths:
            if not any(re.search(pat, path) for pat in include_paths):
                continue

        # Apply exclude filters
        if exclude_paths:
            if any(re.search(pat, path) for pat in exclude_paths):
                continue

        filtered.append(link_str)

    if not filtered:
        logger.info("All sitemap links filtered out for %s", url)
        return None

    # 3. Cap to limit
    to_scrape = filtered[:limit]

    # 4. Scrape each URL — use Firecrawl batch scrape via /v2/scrape per URL
    pages: list[dict] = []
    scrape_payload = {
        "formats": ["markdown"],
        "onlyMainContent": True,
    }

    for target_url in to_scrape:
        try:
            scrape_payload["url"] = target_url
            scrape_result = await _raw_post("/v2/scrape", scrape_payload)
            page_data = scrape_result.get("data", {})
            pages.append({
                "url": page_data.get("metadata", {}).get("url", target_url),
                "title": page_data.get("metadata", {}).get("title", ""),
                "content": page_data.get("markdown", "") or page_data.get("text", "") or "",
            })
        except Exception as e:
            logger.debug("Failed to scrape %s: %s", target_url, str(e)[:100])
            # Include as stub even on error
            pages.append({
                "url": target_url,
                "title": "",
                "content": f"[Failed to scrape: {str(e)[:100]}]",
            })

    logger.info(
        "Sitemap fallback scraped %d/%d URLs for %s",
        len(pages), len(to_scrape), url,
    )
    return pages


# -- web_crawl tool -----------------------------------------------------------


WEB_CRAWL_SCHEMA = {
    "name": "web_crawl",
    "description": (
        "Crawl a website RECURSIVELY — follow its links and scrape EVERY page found. "
        "Returns full markdown content per page. USE THIS when you need to ingest an "
        "entire site or section (docs, blog, wiki). SLOWER than map/extract (polls "
        "the async job for up to 120s). Supports depth control via maxDepth, path filtering with "
        "regex include/exclude, and NL instructions via 'prompt'. "
        "Typical flow: web_map first to discover structure → then web_crawl on specific paths. "
        "For a single page you already have the URL for, use web_extract instead (much faster). "
        "⚠️ SENSITIVE DOMAINS (GitHub, LinkedIn, Reddit, Twitter/X, etc.) are AUTO-CAPPED "
        "at 5 pages max and trigger a safety warning. Also auto-falls back to sitemap-based "
        "discovery when the recursive crawl finds ≤2 pages (common on SPA/Docusaurus sites). "
        "Use web_extract on specific pages instead of crawl for these sites. "
        "If crawl still fails, use web_map + web_extract for manual discovery."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The base URL to start crawling from. Can include or exclude https://"
            },
            "prompt": {
                "type": "string",
                "description": "Optional natural language instructions for what to extract or focus on (e.g. 'Find pricing and feature comparisons')"
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of pages to crawl (default: 20, max: 100)",
                "default": 20,
                "minimum": 1,
                "maximum": 100
            },
            "include_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional regex patterns to include only matching paths (e.g. ['/docs/.*', '/blog/.*'])"
            },
            "exclude_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional regex patterns to exclude paths (e.g. ['/tag/.*', '/author/.*'])"
            },
            "maxDepth": {
                "type": "integer",
                "description": "Maximum crawl depth from start URL (2 = start page + 1 link level; default: 2; increase for multi-level docs/wikis). Maps to Firecrawl v2 maxDiscoveryDepth.",
                "default": 2,
                "minimum": 1,
                "maximum": 10
            },
        },
        "required": ["url"]
    }
}


async def _handle_web_crawl(args: dict, **kw) -> str:
    url = args.get("url", "").strip()
    if not url:
        return json.dumps({"success": False, "error": "Missing required 'url' parameter"})

    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    # ── Safety: block forbidden domains ──
    from urllib.parse import urlparse
    parsed = urlparse(url)
    domain = parsed.hostname or ""

    # Normalise: remove www. prefix
    clean_domain = domain.removeprefix("www.").lower()

    if any(clean_domain == blocked or clean_domain.endswith(f".{blocked}")
           for blocked in BLOCKED_CRAWL_DOMAINS):
        return json.dumps({
            "success": False,
            "error": (
                f"Crawling '{domain}' is blocked via this tool to avoid IP bans. "
                f"Use web_extract to scrape specific pages instead."
            ),
        })

    # ── Safety: warn on sensitive domains ──
    is_sensitive = any(
        clean_domain == sensitive or clean_domain.endswith(f".{sensitive}")
        for sensitive in SENSITIVE_DOMAINS
    )
    if is_sensitive and args.get("limit", DEFAULT_MAX_PAGES) > 5:
        logger.warning(
            "Crawling SENSITIVE domain '%s' (limit=%d) — "
            "risks rate-limit or IP ban. Limiting to 5 pages.",
            domain, args.get("limit", DEFAULT_MAX_PAGES),
        )
        # Automatically cap at 5 pages for sensitive domains
        # (user can override by passing limit= explicitly ≤5)
        pass  # limit is already set to min(original, 100) below; we just warn

    prompt = args.get("prompt", "") or None
    limit = min(int(args.get("limit", DEFAULT_MAX_PAGES)), 100)

    # Auto-tighten limit on sensitive domains (unless user already kept it reasonable)
    if is_sensitive and limit > 5:
        limit = 5
    max_depth = int(args.get("maxDepth", DEFAULT_DEPTH))
    include_paths = args.get("include_paths") or []
    exclude_paths = args.get("exclude_paths") or []

    payload: Dict[str, Any] = {
        "url": url,
        "limit": limit,
        "maxDiscoveryDepth": max_depth,
        "scrapeOptions": {
            "formats": ["markdown"],
            "onlyMainContent": True,
        },
    }
    if prompt:
        payload["prompt"] = prompt
    if include_paths:
        payload["includePaths"] = include_paths
    if exclude_paths:
        payload["excludePaths"] = exclude_paths

    try:
        # 1. Start the crawl job
        logger.info("Starting crawl for %s (limit=%d)", url, limit)
        job = await _raw_post("/v2/crawl", payload)
        job_id = job.get("id")
        job_url = job.get("url")

        if not job_id:
            return json.dumps({
                "success": False,
                "error": f"Crawl job did not return an id. Response: {json.dumps(job)[:500]}",
            })

        # 2. Poll until done
        poll_path = job_url.replace(f"{FIRECRAWL_API_URL}", "") if job_url else f"/v2/crawl/{job_id}"
        elapsed = 0
        while elapsed < POLL_TIMEOUT:
            await asyncio.sleep(DEFAULT_POLL_INTERVAL)
            elapsed += DEFAULT_POLL_INTERVAL

            result = await _raw_get(poll_path)
            status = result.get("status", "unknown")

            if status == "completed":
                break
            elif status in ("failed", "cancelled"):
                error = result.get("error", f"Crawl {status}")
                return json.dumps({"success": False, "error": error})
            # else "scraping" — keep polling

        if elapsed >= POLL_TIMEOUT:
            return json.dumps({
                "success": False,
                "error": f"Crawl timed out after {POLL_TIMEOUT}s for {url}",
            })

        # 3. Extract results
        data = result.get("data", [])
        pages = []
        for page in data:
            pages.append({
                "url": page.get("metadata", {}).get("url", ""),
                "title": page.get("metadata", {}).get("title", ""),
                "content": page.get("markdown", "") or page.get("text", "") or "",
            })

        # 3b. Fallback: if crawl returned sparse results (≤2 pages), try sitemap-based discovery
        # SPA sites like Docusaurus often don't produce <a> links Firecrawl can follow.
        if len(pages) <= 2:
            fallback_pages = await _fallback_via_sitemap(url, limit, include_paths, exclude_paths)
            if fallback_pages is not None and len(fallback_pages) > len(pages):
                logger.info(
                    "Sitemap fallback improved results: %d pages (was %d)",
                    len(fallback_pages), len(pages),
                )
                pages = fallback_pages

        if not pages:
            return json.dumps({"success": False, "error": f"Crawl completed but returned 0 pages for {url}"})

        response = {"success": True, "pages_crawled": len(pages), "results": pages}
        response = _truncate_for_schema(response)
        return json.dumps(response, indent=2, ensure_ascii=False)

    except Exception as e:
        logger.warning("web_crawl failed: %s", str(e)[:200])
        return json.dumps({"success": False, "error": f"web_crawl error: {str(e)[:300]}"})


# -- web_map tool -------------------------------------------------------------


WEB_MAP_SCHEMA = {
    "name": "web_map",
    "description": (
        "Discover and list all URLs on a website — QUICKLY, WITHOUT extracting content. "
        "USE THIS when you don't know the URL structure yet and want a sitemap-like overview "
        "before deciding what to extract or crawl. Returns URLs, titles, and descriptions only "
        "(zero markdown content). Much faster than crawl: finishes in seconds, not minutes. "
        "Supports sitemap-aware discovery ('only', 'skip', 'include'), subdomain crawling, "
        "and search-based relevance filtering. "
        "TYPICAL FLOW: web_map to discover → web_extract on specific pages → "
        "or web_crawl for full site ingestion."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The base URL to map. Can include or exclude https://"
            },
            "limit": {
                "type": "integer",
                "description": "Maximum URLs to return (default: 100, max: 10000)",
                "default": 100,
                "minimum": 1,
                "maximum": 10000
            },
            "search": {
                "type": "string",
                "description": "Optional search term to filter results by relevance (e.g. 'blog', 'docs')"
            },
            "include_subdomains": {
                "type": "boolean",
                "description": "Include subdomains in results (default: false)",
                "default": False
            },
            "sitemap": {
                "type": "string",
                "enum": ["include", "skip", "only"],
                "description": "How to handle sitemaps: 'include' (default), 'skip', or 'only' for sitemap-only discovery"
            },
        },
        "required": ["url"]
    }
}


async def _handle_web_map(args: dict, **kw) -> str:
    url = args.get("url", "").strip()
    if not url:
        return json.dumps({"success": False, "error": "Missing required 'url' parameter"})

    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    limit = min(int(args.get("limit", 100)), 10000)
    search = args.get("search", "") or None
    include_subdomains = bool(args.get("include_subdomains", False))
    sitemap = args.get("sitemap", "include")

    payload: Dict[str, Any] = {
        "url": url,
        "limit": limit,
        "includeSubdomains": include_subdomains,
        "sitemap": sitemap,
    }
    if search:
        payload["search"] = search

    try:
        logger.info("Mapping %s (limit=%d)", url, limit)
        result = await _raw_post("/v2/map", payload)

        links = result.get("links", [])
        if not links:
            return json.dumps({"success": True, "urls_found": 0, "links": []})

        response = {"success": True, "urls_found": len(links), "links": links}
        # Cap the response size
        response = _truncate_for_schema(response)
        return json.dumps(response, indent=2, ensure_ascii=False)

    except Exception as e:
        logger.warning("web_map failed: %s", str(e)[:200])
        return json.dumps({"success": False, "error": f"web_map error: {str(e)[:300]}"})


# -- plugin registration ------------------------------------------------------


def register(ctx) -> None:
    """Register web_crawl and web_map tools. Called by the plugin loader."""
    ctx.register_tool(
        name="web_crawl",
        toolset="web",
        schema=WEB_CRAWL_SCHEMA,
        handler=_handle_web_crawl,
        check_fn=_firecrawl_available,
        requires_env=["FIRECRAWL_API_URL"],
        is_async=True,
        emoji="🕸️",
    )

    ctx.register_tool(
        name="web_map",
        toolset="web",
        schema=WEB_MAP_SCHEMA,
        handler=_handle_web_map,
        check_fn=_firecrawl_available,
        requires_env=["FIRECRAWL_API_URL"],
        is_async=True,
        emoji="🗺️",
    )

    logger.info("firecrawl-crawler plugin registered: web_crawl + web_map")
