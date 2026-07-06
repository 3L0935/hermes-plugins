"""
Ollama Usage Plugin for Hermes

Provides:
  - /ollama slash command: returns full Ollama Cloud usage (CLI + Discord)

Uses register_command() — handled at the command-dispatch level,
BEFORE any agent/LLM call. Zero LLM calls, instant response.

Cookie: ~/.hermes/ollama_cookie.txt (__Secure-session=<value> from ollama.com/settings)
"""

from __future__ import annotations

import logging
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent
COOKIE_FILE = Path.home() / ".hermes" / "ollama_cookie.txt"
SETTINGS_URL = "https://ollama.com/settings"
TIMEOUT = 15


# ── helpers ──────────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_reset(iso_str: str) -> str:
    """Format an ISO timestamp as a human-readable 'in Xh Ym' string."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
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
    except (ValueError, TypeError):
        return iso_str


# ── scraping ─────────────────────────────────────────────────────────────

def _load_cookie() -> str:
    """Read the session cookie from the cookie file."""
    if not COOKIE_FILE.exists():
        raise FileNotFoundError(
            f"Cookie file not found at {COOKIE_FILE}\n"
            f"Run: echo '__Secure-session=<value>' > {COOKIE_FILE}"
        )
    cookie = COOKIE_FILE.read_text().strip()
    if not cookie:
        raise ValueError("Cookie file is empty")
    return cookie


def _fetch_settings_page(cookie: str) -> str:
    """Fetch the Ollama settings page with the session cookie."""
    req = urllib.request.Request(
        SETTINGS_URL,
        headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:136.0) Gecko/20100101 Firefox/136.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_usage(html: str) -> dict:
    """Parse the settings page HTML for usage data."""
    result = {
        "plan": None,
        "session_used_pct": None,
        "session_reset": None,
        "weekly_used_pct": None,
        "weekly_reset": None,
    }

    # Plan tier
    plan_match = re.search(
        r'Cloud usage</span>\s*\n?\s*<span[^>]*>\s*(pro|free|max)\s*<',
        html, re.IGNORECASE
    )
    if plan_match:
        result["plan"] = plan_match.group(1).capitalize()
    else:
        fallback = re.search(r'(Pro|Free|Max)[^<]*Cloud usage', html, re.IGNORECASE)
        if fallback:
            result["plan"] = fallback.group(1).capitalize()
        else:
            plans = re.findall(r'\b(Pro|Free|Max)\b', html)
            if plans:
                result["plan"] = max(set(plans), key=plans.count)

    # Session usage %
    session_pct = re.search(r'Session usage\s+([0-9.]+)%', html)
    if session_pct:
        result["session_used_pct"] = float(session_pct.group(1))

    # Weekly usage %
    weekly_pct = re.search(r'Weekly usage\s+([0-9.]+)%', html)
    if weekly_pct:
        result["weekly_used_pct"] = float(weekly_pct.group(1))

    # Reset timestamps — data-time attributes, first two are session then weekly
    resets = re.findall(r'data-time="([^"]*)"', html)
    if len(resets) >= 1:
        result["session_reset"] = resets[0]
    if len(resets) >= 2:
        result["weekly_reset"] = resets[1]

    return result


def _format_usage(data: dict) -> str:
    """Format the usage data for display."""
    lines = []
    plan = data.get("plan") or "Unknown"
    lines.append(f"📊 Ollama Cloud — {plan}")

    session_pct = data.get("session_used_pct")
    if session_pct is not None:
        remaining = max(0, 100 - session_pct)
        reset = data.get("session_reset")
        line = f"  Session: {remaining:.0f}% remaining ({session_pct:.1f}% used)"
        if reset:
            line += f" • resets {_format_reset(reset)}"
        lines.append(line)
    else:
        lines.append("  Session: unavailable")

    weekly_pct = data.get("weekly_used_pct")
    if weekly_pct is not None:
        remaining = max(0, 100 - weekly_pct)
        reset = data.get("weekly_reset")
        line = f"  Weekly:  {remaining:.0f}% remaining ({weekly_pct:.1f}% used)"
        if reset:
            line += f" • resets {_format_reset(reset)}"
        lines.append(line)
    else:
        lines.append("  Weekly:  unavailable")

    return "\n".join(lines)


def _fetch_usage_text() -> str:
    """Fetch Ollama Cloud usage — returns formatted text or error message."""
    try:
        cookie = _load_cookie()
        html = _fetch_settings_page(cookie)
        data = _parse_usage(html)
        return _format_usage(data)
    except FileNotFoundError as e:
        return f"Ollama Cloud usage: cookie not configured.\n{e}"
    except Exception as e:
        logger.warning("Ollama usage fetch failed: %s", e)
        return "Ollama Cloud usage: unavailable (cookie expired or settings page changed)"


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
