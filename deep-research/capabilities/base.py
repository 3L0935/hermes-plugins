"""Base capability interfaces for the deep-research plugin.

A *capability* is an abstract ability the research loop can use — e.g.
"search", "page-extract", "deep-crawl".  Capabilities are backed either by a
built-in Hermes tool (``ToolCapability``) or by an explicit JSON declaration
on disk (``ExplicitCapability``).

The indirection lets the loop stay search-agnostic and addon-agnostic: it
asks the :class:`CapabilityRegistry` "what can you do?" and receives a set of
``BaseCapability`` instances it can ``invoke``.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("hermes.deep-research.capabilities")


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class CapabilityContext:
    """Input bundle passed to :meth:`BaseCapability.invoke`.

    Attributes
    ----------
    query:
        Natural-language search query (used by search/map capabilities).
    urls:
        Optional list of URLs to extract (used by extract/crawl capabilities).
    max_results:
        Hint for the maximum number of results to return.
    config:
        Optional capability-specific configuration overrides.
    """

    query: str = ""
    urls: Optional[list[str]] = None
    max_results: int = 10
    config: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class BaseCapability(ABC):
    """Abstract interface every deep-research capability implements."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Capability name (e.g. ``"page-extract"``, ``"deep-crawl"``)."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what the capability does."""

    @property
    def tool_name(self) -> Optional[str]:
        """Name of the underlying Hermes tool, if any."""
        return None

    @property
    def priority(self) -> int:
        """Higher = preferred when multiple caps provide the same ability."""
        return 0

    @abstractmethod
    async def invoke(
        self, ctx: CapabilityContext, tool_dispatch: Callable[..., Any]
    ) -> list[dict]:
        """Invoke the capability.

        Returns a list of raw result dicts.  The shape depends on the
        capability — search caps return ``{url, title, snippet}`` dicts,
        extract caps return ``{url, title, content}`` dicts.  Callers
        (Searcher / Extractor) normalise these into typed dataclasses.
        """


# ---------------------------------------------------------------------------
# Tool-backed capability
# ---------------------------------------------------------------------------


class ToolCapability(BaseCapability):
    """Wrap a built-in Hermes tool as a deep-research capability.

    Example: ``ToolCapability("web_search", "search")`` exposes the
    ``web_search`` tool under the ``search`` capability name.
    """

    def __init__(self, tool_name: str, cap_name: str, description: str = ""):
        self._tool_name = tool_name
        self._cap_name = cap_name
        self._description = description or f"Built-in tool: {tool_name}"

    @property
    def name(self) -> str:
        return self._cap_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def tool_name(self) -> str:
        return self._tool_name

    async def invoke(
        self, ctx: CapabilityContext, tool_dispatch: Callable[..., Any]
    ) -> list[dict]:
        """Dispatch the underlying tool with a capability-appropriate payload.

        The payload is built from ``ctx`` and adapted to the tool name —
        ``web_search`` receives ``{query, limit}``, ``web_extract`` receives
        ``{urls}``, ``web_crawl`` / ``web_map`` receive ``{url, ...}``.
        """
        payload = self._build_payload(ctx)
        try:
            raw = await tool_dispatch(self._tool_name, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool %s failed: %s", self._tool_name, exc)
            return []

        return self._normalise(raw)

    # -- payload helpers --------------------------------------------------

    def _build_payload(self, ctx: CapabilityContext) -> dict[str, Any]:
        if self._tool_name == "web_search":
            return {"query": ctx.query, "limit": ctx.max_results}
        if self._tool_name in ("web_extract",):
            return {"urls": ctx.urls or []}
        if self._tool_name in ("web_crawl", "web_map"):
            url = (ctx.urls or [""])[0]
            payload: dict[str, Any] = {"url": url}
            payload.update(ctx.config)
            return payload
        if self._tool_name in ("browser_navigate", "browser_snapshot"):
            url = (ctx.urls or [""])[0]
            return {"url": url, "full": True}
        # Generic fallback
        payload = {"query": ctx.query}
        if ctx.urls:
            payload["urls"] = ctx.urls
        return payload

    def _normalise(self, raw: Any) -> list[dict]:
        """Best-effort normalisation of a tool's raw return into list[dict]."""
        if isinstance(raw, list):
            return [r if isinstance(r, dict) else {"raw": r} for r in raw]
        if isinstance(raw, dict):
            for key in ("results", "pages", "data", "items"):
                if key in raw and isinstance(raw[key], list):
                    return raw[key]
            return [raw]
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return [{"raw": raw}]
            return self._normalise(parsed)
        return []


# ---------------------------------------------------------------------------
# Explicit (filesystem-declared) capability
# ---------------------------------------------------------------------------


class ExplicitCapability(BaseCapability):
    """Capability loaded from a JSON declaration on disk.

    The JSON spec has the shape::

        {
          "name": "vector-search",
          "description": "...",
          "tool_name": "vault_search",
          "capabilities": ["semantic-search"],
          "priority": 50,
          "config": {...}
        }
    """

    def __init__(self, spec: dict[str, Any]):
        self._spec = spec

    @property
    def name(self) -> str:
        # ``capabilities`` is a list — use the first as primary, fall back
        # to ``name``.
        caps = self._spec.get("capabilities") or []
        if caps:
            return caps[0]
        return self._spec.get("name", "unknown")

    @property
    def description(self) -> str:
        return self._spec.get("description", "")

    @property
    def tool_name(self) -> Optional[str]:
        return self._spec.get("tool_name")

    @property
    def priority(self) -> int:
        return int(self._spec.get("priority", 50))

    async def invoke(
        self, ctx: CapabilityContext, tool_dispatch: Callable[..., Any]
    ) -> list[dict]:
        tool = self._spec.get("tool_name")
        if not tool:
            logger.warning("explicit capability %s has no tool_name", self.name)
            return []

        # Merge spec config with runtime ctx config (runtime wins)
        merged_cfg = {**self._spec.get("config", {}), **ctx.config}
        payload: dict[str, Any] = {}
        if ctx.query:
            payload["query"] = ctx.query
        if ctx.urls:
            payload["urls"] = ctx.urls
        payload["limit"] = ctx.max_results
        payload.update(merged_cfg)

        try:
            raw = await tool_dispatch(tool, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("explicit cap %s (tool=%s) failed: %s", self.name, tool, exc)
            return []

        return self._normalise(raw)

    def _normalise(self, raw: Any) -> list[dict]:
        if isinstance(raw, list):
            return [r if isinstance(r, dict) else {"raw": r} for r in raw]
        if isinstance(raw, dict):
            for key in ("results", "pages", "data", "items"):
                if key in raw and isinstance(raw[key], list):
                    return raw[key]
            return [raw]
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return [{"raw": raw}]
            return self._normalise(parsed)
        return []


# ---------------------------------------------------------------------------
# Convenience exports
# ---------------------------------------------------------------------------

__all__ = [
    "CapabilityContext",
    "BaseCapability",
    "ToolCapability",
    "ExplicitCapability",
]