"""Load the plugin package under a stable import name (its directory name has a dash)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _install() -> None:
    if "gateway_restart" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "gateway_restart", str(PLUGIN_DIR / "__init__.py"), submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["gateway_restart"] = module
    spec.loader.exec_module(module)


_install()
