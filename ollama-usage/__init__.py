"""
Ollama Usage Plugin for Hermes

Provides:
  - /ollama slash command: Ollama Cloud quota (5h session + weekly) and balance

Uses register_command() — handled at the command-dispatch level,
BEFORE any agent/LLM call. Zero LLM calls, instant response.

Data source: official balance endpoint https://ollama.com/api/balance
  Auth: Authorization: <key> (raw key works, same as the hermes-hub tracker)
  Key:  OLLAMA_API_KEY env var, or ~/.hermes/.env

Response shape (Nov 2026, legacy plan with session/weekly windows):
  {
    "included": {
      "session": {"remaining_percent": 97.45, "resets_at": "2026-11-14T16:00:00Z"},
      "weekly":  {"remaining_percent": 60.63, "resets_at": "2026-11-16T00:00:00Z"}
    },
    "purchased": {"balance_usd": 0}
  }

NOTE (14/11/2026): /api/usage no longer carries the quota block — it only
returns request counts and cost per bucket, so the old parser read
limits.session / limits.weekly as None and printed "unavailable". The quota
lives on /api/balance, whose resets_at are real timestamps: no more guessing
the fixed-clock anchors (17:00 local session, Monday 02:00 local weekly).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_FILE = Path.home() / ".hermes" / ".env"
BALANCE_URL = "https://ollama.com/api/balance"
TIMEOUT = 15

# (response key, display label)
WINDOWS = (("session", "5h  "), ("weekly", "Week"))


# ── helpers ──────────────────────────────────────────────────────────────

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


def _format_reset(dt: datetime | None) -> str:
    """Format a reset datetime as 'in Xh Ym (HH:MM TZ)' in local time."""
    if dt is None:
        return "reset unknown"
    local = dt.astimezone()
    total_s = int((dt - datetime.now(timezone.utc)).total_seconds())
    if total_s <= 0:
        rel = "now"
    else:
        days, rem = divmod(total_s, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            rel = f"in {days}d {hours}h"
        elif hours:
            rel = f"in {hours}h {minutes}m"
        else:
            rel = f"in {minutes}m"
    return f"{rel} ({local.strftime('%H:%M %Z')})"


def _parse_reset(raw: object) -> datetime | None:
    """Parse the API's ISO-8601 UTC timestamp into an aware datetime."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _bar(remaining_pct: float, width: int = 10) -> str:
    filled = round(max(0.0, min(100.0, remaining_pct)) / 100 * width)
    return "█" * filled + "░" * (width - filled)


# ── API fetch ────────────────────────────────────────────────────────────

def _fetch_balance() -> dict:
    """Call the Ollama balance API and return the parsed JSON."""
    req = urllib.request.Request(
        BALANCE_URL,
        headers={
            "Authorization": _load_api_key(),
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) hermes-ollama-usage",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _window_line(label: str, bucket: dict) -> str:
    """One quota line: bar, remaining %, reset countdown."""
    try:
        remaining = float(bucket.get("remaining_percent"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return f"  {label}  unavailable"
    remaining = max(0.0, min(100.0, remaining))
    used = 100.0 - remaining
    reset = _format_reset(_parse_reset(bucket.get("resets_at")))
    return (
        f"  {label}  [{_bar(remaining)}] {remaining:5.1f}% left"
        f" ({used:.1f}% used) • resets {reset}"
    )


def _format_usage(data: dict) -> str:
    """Format the balance payload for display."""
    included = data.get("included")
    if not isinstance(included, dict):
        return "Ollama Cloud usage: unavailable (unexpected /api/balance answer)"

    lines = ["📊 Ollama Cloud"]
    windows = [(k, l) for k, l in WINDOWS if isinstance(included.get(k), dict)]
    if windows:
        for key, label in windows:
            lines.append(_window_line(label, included[key]))
    elif isinstance(included.get("balance_usd"), (int, float)):
        # Credits plan: included = {balance_usd, allowance_usd, period{from,until}}
        left = float(included["balance_usd"])
        allowance = included.get("allowance_usd")
        pct = left / float(allowance) * 100 if isinstance(allowance, (int, float)) and allowance else None
        line = f"  Credits  [{_bar(pct)}] ${left:.2f} left" if pct is not None else f"  Credits  ${left:.2f} left"
        if isinstance(allowance, (int, float)) and allowance:
            line += f" of ${allowance:.0f}"
        period_until = (included.get("period") or {}).get("until")
        if period_until:
            line += f" • resets {_format_reset(_parse_reset(period_until))}"
        lines.append(line)
    else:
        lines.append("  quota unavailable (unexpected /api/balance answer)")

    purchased = (data.get("purchased") or {}).get("balance_usd")
    if isinstance(purchased, (int, float)) and purchased > 0:
        lines.append(f"  Extra balance: ${purchased:.2f}")
    return "\n".join(lines)


def _fetch_usage_text() -> str:
    """Fetch Ollama Cloud usage — returns formatted text or error message."""
    try:
        return _format_usage(_fetch_balance())
    except FileNotFoundError as e:
        return f"Ollama Cloud usage: API key not configured.\n{e}"
    except urllib.error.HTTPError as e:
        logger.warning("Ollama balance fetch failed: HTTP %s", e.code)
        return f"Ollama Cloud usage: unavailable (HTTP {e.code} from /api/balance)"
    except Exception as e:  # noqa: BLE001 — a slash command must never raise
        logger.warning("Ollama balance fetch failed: %s", e)
        return f"Ollama Cloud usage: unavailable ({type(e).__name__})"


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
        description="Show Ollama Cloud quota (5h session + weekly) and balance",
        args_hint="",
    )
