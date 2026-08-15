"""
Ollama Usage Plugin for Hermes

Provides:
  - /ollama slash command: returns full Ollama Cloud usage (CLI + Discord)

Uses register_command() — handled at the command-dispatch level,
BEFORE any agent/LLM call. Zero LLM calls, instant response.

Data source: official API endpoint https://ollama.com/api/usage
  Auth: Authorization: Bearer <OLLAMA_API_KEY> (or raw key in header)
  Key:  OLLAMA_API_KEY env var, or ~/.hermes/.env

Response shape (as of Aug 2026):
  {
    "activity": { "cost": "0.00000", "period": {...} },
    "limits": {
      "session": { "usage": 0.066, "models": [...] },   # fraction, 0.066 = 6.6%
      "weekly":  { "usage": 0.404, "models": [...] }
    }
  }

NOTE: the API does NOT expose reset timestamps. The windows are fixed-clock:
  - Session: 5h cycle anchored at 17:00 local (resets 17h, 22h, 3h, 8h, 13h)
  - Weekly:  Monday 02:00 local
We compute the next reset from these fixed anchors. If Ollama ever exposes
the real reset time, prefer it over this approximation.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent
ENV_FILE = Path.home() / ".hermes" / ".env"
USAGE_URL = "https://ollama.com/api/usage"
TIMEOUT = 15

# Fixed-clock reset anchors (local time)
SESSION_ANCHOR_HOUR = 17  # session resets every 5h, anchored at 17:00
WEEKLY_RESET_HOUR = 2     # weekly resets Monday 02:00


# ── helpers ──────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_api_key() -> str:
    """Resolve the Ollama API key: env var first, then ~/.hermes/.env."""
    key = os.environ.get("OLLAMA_API_KEY", "").strip()
    if key:
        return key
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("OLLAMA_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise FileNotFoundError(
        "OLLAMA_API_KEY not found. Set it in the environment or ~/.hermes/.env"
    )


def _format_reset(dt: datetime) -> str:
    """Format a reset datetime as a human-readable 'in Xh Ym' string."""
    delta = dt - _utc_now()
    total_s = int(delta.total_seconds())
    if total_s <= 0:
        return f"now ({dt.astimezone().strftime('%H:%M %Z')})"
    hours, rem = divmod(total_s, 3600)
    minutes = rem // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        rel = f"in {days}d {hours}h"
    elif hours > 0:
        rel = f"in {hours}h {minutes}m"
    else:
        rel = f"in {minutes}m"
    return f"{rel} ({dt.astimezone().strftime('%H:%M %Z')})"


# ── API fetch ────────────────────────────────────────────────────────────

def _fetch_usage_json() -> dict:
    """Call the Ollama usage API and return the parsed JSON."""
    key = _load_api_key()
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": key,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _next_session_reset(now: datetime) -> datetime:
    """Next session reset: every 5h on the hour, anchored at 17:00.

    Reset times are 17:00, 22:00, 03:00, 08:00, 13:00, 18:00, ... (5h steps).
    Because 5 and 24 are coprime, the hour drifts each day — so walk forward
    in 5h steps from a known anchor rather than assuming a fixed set of hours.
    """
    anchor = now.replace(hour=SESSION_ANCHOR_HOUR, minute=0, second=0, microsecond=0)
    candidate = anchor
    while candidate > now:
        candidate -= timedelta(hours=5)
    while candidate <= now:
        candidate += timedelta(hours=5)
    return candidate


def _next_weekly_reset(now: datetime) -> datetime:
    """Next weekly reset: Monday 02:00 local."""
    # weekday(): Monday=0 ... Sunday=6
    days_until_monday = (0 - now.weekday()) % 7
    candidate = (now + timedelta(days=days_until_monday)).replace(
        hour=WEEKLY_RESET_HOUR, minute=0, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _parse_usage(data: dict) -> dict:
    """Extract usage fields from the API JSON."""
    # Reset anchors are in LOCAL time (17h session, Monday 02h weekly), so
    # compute from the local clock, not UTC.
    now = datetime.now().astimezone()
    limits = data.get("limits", {})

    session = limits.get("session", {})
    weekly = limits.get("weekly", {})

    return {
        "session_used_pct": session.get("usage"),
        "session_reset": _next_session_reset(now),
        "session_models": session.get("models", []),
        "weekly_used_pct": weekly.get("usage"),
        "weekly_reset": _next_weekly_reset(now),
        "weekly_models": weekly.get("models", []),
    }


def _format_models(models: list) -> list:
    """Format per-model request counts as one line per model."""
    if not models:
        return []
    lines = []
    for m in models:
        name = m.get("name", "?")
        count = m.get("request_count", 0)
        lines.append(f"    • {name} ×{count}")
    return lines


def _format_usage(data: dict) -> str:
    """Format the usage data for display."""
    lines = []
    lines.append("📊 Ollama Cloud")

    session_pct = data.get("session_used_pct")
    if session_pct is not None:
        pct = float(session_pct) * 100
        remaining = max(0, 100 - pct)
        reset = data.get("session_reset")
        line = f"  Session: {remaining:.0f}% remaining ({pct:.1f}% used)"
        if reset:
            line += f" • resets {_format_reset(reset)}"
        lines.append(line)
        lines.extend(_format_models(data.get("session_models", [])))
    else:
        lines.append("  Session: unavailable")

    weekly_pct = data.get("weekly_used_pct")
    if weekly_pct is not None:
        pct = float(weekly_pct) * 100
        remaining = max(0, 100 - pct)
        reset = data.get("weekly_reset")
        line = f"  Weekly:  {remaining:.0f}% remaining ({pct:.1f}% used)"
        if reset:
            line += f" • resets {_format_reset(reset)}"
        lines.append(line)
        lines.extend(_format_models(data.get("weekly_models", [])))
    else:
        lines.append("  Weekly:  unavailable")

    return "\n".join(lines)


def _fetch_usage_text() -> str:
    """Fetch Ollama Cloud usage — returns formatted text or error message."""
    try:
        data = _parse_usage(_fetch_usage_json())
        return _format_usage(data)
    except FileNotFoundError as e:
        return f"Ollama Cloud usage: API key not configured.\n{e}"
    except Exception as e:
        logger.warning("Ollama usage fetch failed: %s", e)
        return "Ollama Cloud usage: unavailable (API key invalid or endpoint changed)"


# ── plugin command ───────────────────────────────────────────────────────

def _ollama_handler(raw_args: str) -> str:
    """
    Handle /ollama command.
    Called at command-dispatch level — zero LLM calls.
    """
    return _fetch_usage_text()


def register(ctx):
    """Register the /ollama command."""
    ctx.register_command(
        "ollama",
        _ollama_handler,
        description="Show Ollama Cloud usage (session + weekly quotas)",
        args_hint="",
    )
