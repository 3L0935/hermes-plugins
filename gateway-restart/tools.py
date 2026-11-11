"""Pre-declaration entry point for hosts that import ``tools.py`` before the session starts."""

from __future__ import annotations

from typing import Any


def register_tools(ctx: Any) -> None:
    from . import register as _register

    _register(ctx)
