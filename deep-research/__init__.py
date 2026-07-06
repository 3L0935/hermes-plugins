"""Deep Research Plugin for Hermes Agent.

Iterative multi-source deep research tool. Search-agnostic core that works
with any Hermes web search backend. Auto-discovers addons at runtime.
Returns raw structured markdown — no LLM summarization.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("hermes.deep_research.plugin")

# ── Tool schema ──────────────────────────────────────────────────────────────

DEEP_RESEARCH_SCHEMA = {
    "name": "deep_research",
    "description": (
        "Run an iterative multi-source deep research on a topic. "
        "Automatically searches the web, extracts page content as raw markdown, "
        "detects convergence, and returns a structured markdown document with "
        "all sources. Search-agnostic: uses whatever search backend Hermes is "
        "configured with (SearXNG, Firecrawl, Tavily, Brave). Auto-discovers "
        "addons (firecrawl crawl, browser scraping) at runtime. "
        "Returns RAW markdown — no LLM summarization that truncates content. "
        "Use this when you need thorough, multi-source research with full "
        "content extraction, not just search snippets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The research question or topic to investigate deeply.",
            },
            "max_pages": {
                "type": "integer",
                "description": "Maximum pages to extract (default: 30). Budget control.",
                "default": 30,
                "minimum": 5,
                "maximum": 100,
            },
            "max_iterations": {
                "type": "integer",
                "description": "Maximum search-extract-converge iterations (default: 5).",
                "default": 5,
                "minimum": 1,
                "maximum": 10,
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Total timeout in seconds (default: 600).",
                "default": 600,
                "minimum": 60,
                "maximum": 1800,
            },
            "focus_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: limit search to specific domains (e.g. ['arxiv.org', 'github.com'])",
            },
            "exclude_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional: exclude these domains from results",
            },
            "max_depth": {
                "type": "integer",
                "description": (
                    "Crawl depth for web_crawl discovery fallback "
                    "(default: 1 = homepage + 1 link level). "
                    "Increase to 2-3 for deeper site traversal when "
                    "search backends return no results."
                ),
                "default": 1,
                "minimum": 1,
                "maximum": 5,
            },
        },
        "required": ["query"],
    },
}

# ── Plugin-level state ──────────────────────────────────────────────────────

_plugin_ctx: Any = None  # set during register(), used by handler


def _check_available() -> bool:
    """Check if any search backend is configured (required for the loop)."""
    try:
        import os
        backends = [
            "SEARXNG_URL", "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL",
            "TAVILY_API_KEY", "EXA_API_KEY", "BRAVE_SEARCH_API_KEY",
        ]
        if any(os.getenv(b) for b in backends):
            return True
        # Also check Hermes config for web.search_backend
        try:
            from hermes_cli.config import load_config
            cfg = load_config() or {}
            web_cfg = cfg.get("web", {}) or {}
            backend = web_cfg.get("search_backend", "")
            if backend:
                return True
        except Exception:
            pass
        return False
    except Exception:
        return False


async def _handle_deep_research(args: dict, **kw) -> str:
    """Main handler — orchestrates the deep research loop."""
    from .core.loop import ResearchLoop
    from .core.budget import Budget
    from .core.types import ResearchGoal
    from .capabilities.registry import CapabilityRegistry

    query = args.get("query", "").strip()
    if not query:
        return json.dumps({"success": False, "error": "Missing 'query' parameter"})

    budget = Budget(
        max_pages=args.get("max_pages", 30),
        max_iterations=args.get("max_iterations", 5),
        timeout_seconds=args.get("timeout_seconds", 600),
        max_depth=args.get("max_depth", 1),
    )

    goal = ResearchGoal(
        query=query,
        focus_domains=args.get("focus_domains", []),
        exclude_domains=args.get("exclude_domains", []),
        max_depth=args.get("max_depth", 1),
    )

    # Build the dispatch function using the stored plugin context
    async def _dispatch(tool_name: str, tool_args: dict) -> str:
        if _plugin_ctx is not None:
            return _plugin_ctx.dispatch_tool(tool_name, tool_args)
        return json.dumps({"success": False, "error": "Plugin context not initialized"})

    # Discover available tools by querying the Hermes tool registry directly.
    # Hermes doesn't pass ``available_tools`` in handler kwargs, so we introspect
    # the live registry instead.  This is the source of truth — any tool
    # registered (built-in or by a plugin) is visible here.
    from .capabilities.registry import BUILTIN_TOOL_CAPABILITY_MAP
    try:
        from tools.registry import registry as _tool_registry
        all_tool_names = set(_tool_registry.get_all_tool_names())
    except Exception:
        all_tool_names = set()
    available_tools = all_tool_names & set(BUILTIN_TOOL_CAPABILITY_MAP.keys())
    logger.debug("deep_research available_tools: %s", sorted(available_tools))

    cap_registry = CapabilityRegistry(
        available_tools=available_tools,
        tool_dispatch=_dispatch,
    )

    loop = ResearchLoop(
        budget=budget,
        cap_registry=cap_registry,
        tool_dispatch=_dispatch,
    )
    result = await loop.run(goal)

    # Convert ResearchResult dataclass to dict for JSON serialization
    result_dict = {
        "success": result.success,
        "query": result.query,
        "iterations": result.iterations,
        "pages_extracted": result.pages_extracted,
        "converged": result.converged,
        "novelty_final": result.novelty_final,
        "sources": result.sources,
        "markdown": result.markdown,
        "full_content_path": result.full_content_path,
        "truncated": result.truncated,
        "total_chars": result.total_chars,
        "error": result.error,
    }

    return json.dumps(result_dict, indent=2, ensure_ascii=False)


def register(ctx) -> None:
    """Register the deep_research tool."""
    global _plugin_ctx
    _plugin_ctx = ctx

    ctx.register_tool(
        name="deep_research",
        toolset="web",
        schema=DEEP_RESEARCH_SCHEMA,
        handler=_handle_deep_research,
        check_fn=_check_available,
        requires_env=[],
        is_async=True,
        emoji="🔬",
    )
    logger.info("deep-research plugin registered: deep_research tool")
