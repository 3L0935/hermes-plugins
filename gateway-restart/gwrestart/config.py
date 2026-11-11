"""Settings for gateway-restart. Read at call time so a config edit applies without a reload.

Config block (all keys optional), in ``~/.hermes/config.yaml``::

    plugins:
      entries:
        gateway-restart:
          unit: hermes-gateway
          default_delay_s: 15
          drain_timeout_s: 300
          companion_units: [hermes-hub, hermes-dashboard]

Nothing here hardcodes a user path or a profile name.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

PLUGIN_KEY = "gateway-restart"

DEFAULTS: dict[str, Any] = {
    # systemd --user unit that runs the gateway
    "unit": "hermes-gateway",
    # minimum window between `arm` and the restart, so a cancel is possible
    "default_delay_s": 15,
    # how long the runner keeps waiting for in-flight work to finish by itself
    "drain_timeout_s": 300,
    # a kanban worker younger than this has produced nothing worth keeping
    "young_worker_s": 120,
    # a kanban worker silent for longer than this is wedged, not working
    "heartbeat_stale_s": 180,
    # cron runs are interrupted by the scheduler after ~3 min; that is the cap we plan against
    "cron_run_soft_cap_s": 180,
    # a cron run younger than this has no progress worth preserving
    "cron_young_s": 30,
    # a live gateway turn (platform conversation) is short; wait at most this
    "gateway_turn_eta_s": 60,
    # post-restart health polling budget
    "health_timeout_s": 180,
    # delay before forcing a kanban reclaim ourselves (the dispatcher normally wins)
    "manual_reclaim_after_s": 150,
    # reports + notices kept on disk
    "keep_reports": 10,
    "keep_notices_days": 7,
    # units that share the session with the gateway; restarted only if they died
    "companion_units": ["hermes-hub", "hermes-dashboard"],
    # expected gateway boot time; only used for the human warning text
    "expected_downtime_s": 20,
    # notify_messaging_platforms: post the summary into the messaging channel the restart came from
    "notify_messaging_platforms": True,
    # poke: run a follow-up agent turn on its own, without waiting for the human to type
    "poke": {
        "enabled": True,
        "deliver": "auto",   # auto = origin chat (platform:chat, else bot-chat)
        "delay_s": 15,
        "prompt": "",
    },
    # platforms that have no channel to answer into (report stays session-only)
    "session_only_platforms": ["", "local", "cli", "cron", "api_server", "subagent"],
    # absolute path to the hermes CLI for detached runs; resolved when empty
    "hermes_bin": "",
}

# systemd user units get a narrow PATH: never rely on inheritance alone
_SAFE_PATH = ":".join(
    [
        str(Path.home() / ".local" / "bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
)


def hermes_home() -> Path:
    """Active Hermes home (profile-aware), without importing core when possible."""
    raw = os.environ.get("HERMES_HOME")
    if raw:
        return Path(raw).expanduser()
    try:  # inside Hermes: profile-correct
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def _config_entry() -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    path = hermes_home() / "config.yaml"
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except Exception:
        return {}
    entries = (cfg.get("plugins") or {}).get("entries") or {}
    entry = entries.get(PLUGIN_KEY) or {}
    return entry if isinstance(entry, dict) else {}


def settings() -> dict[str, Any]:
    merged = dict(DEFAULTS)
    for key, value in _config_entry().items():
        if value is not None:
            merged[key] = value
    return merged


def state_dir() -> Path:
    path = hermes_home() / "gateway" / "restart"
    path.mkdir(parents=True, exist_ok=True)
    return path


def reports_dir() -> Path:
    return state_dir()


def queue_dir() -> Path:
    path = state_dir() / "queue"
    path.mkdir(parents=True, exist_ok=True)
    return path


def python_bin() -> str:
    """The interpreter that is already running this plugin (profile pin included)."""
    return sys.executable or "/usr/bin/python3"


def hermes_bin(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or settings()
    configured = str(cfg.get("hermes_bin") or os.environ.get("HERMES_BIN") or "").strip()
    if configured and Path(configured).is_file():
        return configured
    for candidate in (
        Path.home() / ".local" / "bin" / "hermes",
        hermes_home() / "hermes-agent" / "venv" / "bin" / "hermes",
    ):
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return shutil.which("hermes") or "hermes"


def run_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("HOME", str(Path.home()))
    parts = [p for p in (env.get("PATH") or "").split(os.pathsep) if p]
    for extra in _SAFE_PATH.split(os.pathsep):
        if extra not in parts:
            parts.append(extra)
    env["PATH"] = os.pathsep.join(parts)
    return env
