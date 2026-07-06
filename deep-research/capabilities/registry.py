"""Capability registry — runtime discovery of available research abilities.

Two discovery mechanisms:

1. **Tool introspection** (automatic): if a Hermes tool whose name matches
   ``BUILTIN_TOOL_CAPABILITY_MAP`` is available in the session, a
   :class:`~capabilities.base.ToolCapability` is created for it.

2. **Filesystem declarations** (explicit): JSON files under
   ``capabilities/registered/*.json`` are loaded as
   :class:`~capabilities.base.ExplicitCapability` instances.  Addon plugins
   write these files in their ``register(ctx)`` to declare richer
   capabilities (priority, config, custom tool names).

The registry is rebuilt on every :meth:`discover` call — it reflects the
*current* session state, not a frozen snapshot taken at plugin load time.
This matters because plugins may be loaded in any order; by the time
``deep_research`` is actually invoked, all tools are registered.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from .base import (
    BaseCapability,
    CapabilityContext,
    ExplicitCapability,
    ToolCapability,
)

logger = logging.getLogger("hermes.deep-research.capabilities")


# ---------------------------------------------------------------------------
# Static mapping: Hermes tool name -> deep-research capability name
# ---------------------------------------------------------------------------

BUILTIN_TOOL_CAPABILITY_MAP: dict[str, str] = {
    "web_search": "search",
    "web_extract": "page-extract",
    "web_crawl": "deep-crawl",
    "web_map": "site-map",
    "browser_navigate": "browser-scrape",
    "browser_snapshot": "browser-scrape",
}

# Human-readable descriptions for auto-discovered tool capabilities
_BUILTIN_DESCRIPTIONS: dict[str, str] = {
    "search": "SERP search — queries to URLs (delegates to web_search)",
    "page-extract": "Single-page extraction to markdown (delegates to web_extract)",
    "deep-crawl": "Recursive multi-page site crawl (delegates to web_crawl)",
    "site-map": "URL discovery for a site (delegates to web_map)",
    "browser-scrape": "JS-rendered page extraction via browser toolset",
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class CapabilityRegistry:
    """Discover and hold the set of capabilities available in this session."""

    def __init__(
        self,
        available_tools: Optional[set[str]] = None,
        tool_dispatch: Optional[Callable[..., Any]] = None,
        cap_dir: Optional[Path] = None,
    ):
        """
        Parameters
        ----------
        available_tools:
            Set of tool names known to be registered in the current Hermes
            session.  If ``None``, discovery via tool introspection is
            skipped (only filesystem declarations are loaded).
        tool_dispatch:
            Optional async dispatcher ``dispatch(tool_name, args) -> str``.
            Stored but not used directly by the registry — capabilities
            receive it at invoke time.
        cap_dir:
            Override for the filesystem capability directory (testing).
        """
        self._tools: set[str] = set(available_tools or ())
        self._dispatch = tool_dispatch
        self._cap_dir = cap_dir or (
            Path(__file__).resolve().parent / "registered"
        )
        # Lazy cache — invalidated if tools change
        self._cache: Optional[dict[str, BaseCapability]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_available_tools(self, tools: set[str]) -> None:
        """Update the set of available tools (invalidates cache)."""
        self._tools = set(tools)
        self._cache = None

    def discover(self) -> dict[str, BaseCapability]:
        """Build and return the full capability map for the current session.

        If a tool_dispatch is available, probes each known tool to verify
        it actually exists in the current session (try-call pattern).
        """
        if self._cache is not None:
            return self._cache

        caps: dict[str, BaseCapability] = {}

        # 1. Tool introspection — probe each known tool
        for tool_name, cap_name in BUILTIN_TOOL_CAPABILITY_MAP.items():
            available = tool_name in self._tools

            # If we have a dispatch, also try probing (more reliable)
            if not available and self._dispatch is not None:
                available = self._probe_tool(tool_name)

            if available:
                if cap_name not in caps:
                    caps[cap_name] = ToolCapability(
                        tool_name=tool_name,
                        cap_name=cap_name,
                        description=_BUILTIN_DESCRIPTIONS.get(cap_name, ""),
                    )

        # 2. Filesystem declarations (may override tool-based ones if
        #    priority is higher — explicit declarations are richer)
        explicit_caps = self._load_explicit()
        for cap in explicit_caps:
            existing = caps.get(cap.name)
            if existing is None or cap.priority > existing.priority:
                caps[cap.name] = cap

        self._cache = caps
        logger.debug(
            "capability discovery: %d capabilities -> %s",
            len(caps),
            sorted(caps.keys()),
        )
        return caps

    def get_capabilities(self) -> dict[str, BaseCapability]:
        """Alias for :meth:`discover`."""
        return self.discover()

    def has(self, cap_name: str) -> bool:
        return cap_name in self.discover()

    def get(self, cap_name: str) -> Optional[BaseCapability]:
        return self.discover().get(cap_name)

    @property
    def dispatch(self) -> Optional[Callable[..., Any]]:
        return self._dispatch

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_explicit(self) -> list[ExplicitCapability]:
        caps: list[ExplicitCapability] = []
        if not self._cap_dir.exists():
            return caps

        for json_file in sorted(self._cap_dir.glob("*.json")):
            try:
                spec = json.loads(json_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("bad capability file %s: %s", json_file, exc)
                continue

            if not isinstance(spec, dict) or "name" not in spec:
                logger.warning("capability file %s missing 'name'", json_file)
                continue

            # Only include if the backing tool is actually available
            tool = spec.get("tool_name")
            if tool and self._tools and tool not in self._tools:
                logger.debug(
                    "explicit cap %s skipped (tool %s not available)",
                    spec["name"],
                    tool,
                )
                continue

            caps.append(ExplicitCapability(spec))

        return caps

    def _probe_tool(self, tool_name: str) -> bool:
        """Probe whether a tool exists.

        .. deprecated::

            Probing via ``tool_dispatch`` dispatched real tool calls
            (``browser_navigate``, etc.) with probe payloads at runtime —
            a side-effectful, dangerous pattern.  The handler now introspects
            ``tools.registry.get_all_tool_names()`` and passes the result as
            ``available_tools``, so probing is unnecessary.  This method is
            kept for API compatibility but always returns ``False`` so the
            registry falls back to the explicit ``available_tools`` set.
        """
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def discover_available_tools_from_registry() -> set[str]:
    """Introspect the Hermes tool registry for known web tools.

    This replaces the old probe-based discovery (which dispatched real tool
    calls with ``{"__probe__": True}`` payloads).  It reads the live registry
    directly — no side effects, safe to call at any time.
    """
    try:
        from tools.registry import registry as _reg
        all_names = set(_reg.get_all_tool_names())
    except Exception:
        return set()
    return all_names & set(BUILTIN_TOOL_CAPABILITY_MAP.keys())


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "CapabilityRegistry",
    "BUILTIN_TOOL_CAPABILITY_MAP",
    "discover_available_tools_from_registry",
]