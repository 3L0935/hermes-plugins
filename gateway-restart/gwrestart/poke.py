"""Poke: push the post-restart report back without waiting for the human to type.

Two lanes, both optional and config-driven:

``channel``  ``hermes send`` into the platform the restart was armed from. Only for messaging
             origins (discord/telegram/...); a CLI origin has no channel to answer into.

``poke``     a one-shot cron job whose output is admitted as a **real inbound turn** in the
             profile's Bot Chat (``--deliver bot-chat``). That is the only native way to make the
             agent run a turn on its own: the session notice can only ride the next user message,
             so on a CLI origin the agent would stay silent until someone typed.

The poke turn is told to read the report, verify the subsystems and resume what was cut, and
explicitly not to restart anything.
"""

from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime
from typing import Any

from . import config

POKE_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # "auto" = wherever the tool call came from: the messaging channel it was armed in, or the
    # profile's canonical Bot Chat for a CLI/local origin. Override with an explicit cron target.
    "deliver": "auto",
    # seconds between "healthy" and the follow-up turn
    "delay_s": 15,
    # empty = built-in prompt
    "prompt": "",
}

_JOB_ID_RE = re.compile(r"Created job:\s*([0-9a-zA-Z_\-]+)")


def poke_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    block = dict(POKE_DEFAULTS)
    raw = cfg.get("poke")
    if isinstance(raw, dict):
        block.update({k: v for k, v in raw.items() if v is not None})
    return block


def channel_target(origin: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Cron/hermes-send target for the chat the tool call came from ("" when there is none)."""
    platform = str((origin or {}).get("platform") or "").lower()
    chat_id = str((origin or {}).get("chat_id") or "")
    session_only = {str(p).lower() for p in (cfg.get("session_only_platforms") or [])}
    if not platform or not chat_id or platform in session_only:
        return ""
    target = f"{platform}:{chat_id}"
    if (origin or {}).get("thread_id"):
        target += f":{(origin or {}).get('thread_id')}"
    return target


def resolve_deliver(origin: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Where the poke goes: the origin chat, so whoever asked is the one who gets woken."""
    explicit = str(poke_settings(cfg).get("deliver") or "auto").strip()
    if explicit and explicit.lower() != "auto":
        return explicit
    channel = channel_target(origin, cfg)
    if channel:
        return channel
    # CLI / local: the profile's canonical Bot Chat *is* the chat the human is typing in
    profile = str((origin or {}).get("profile") or "").strip()
    return f"bot-chat:{profile}" if profile else "bot-chat"


def build_prompt(report: dict[str, Any]) -> str:
    """Short wake-up prompt: the turn reads the report itself, then acts."""
    rid = report.get("id") or "?"
    path = report.get("report_path") or ""
    lines = [
        f"Gateway restarted successfully (id {rid}).",
        f"Report: {path}",
        "Read it, check that cron and the kanban dispatcher are back, resume what was cut, "
        "then give elo a 3-line status. Do not restart anything.",
    ]
    cut = (report.get("drain") or {}).get("cut") or []
    still = ((report.get("wake") or {}).get("kanban") or {}).get("still_dead") or []
    if cut:
        lines.append("cut in flight: " + ", ".join(f"{c.get('kind')}:{c.get('ref')}" for c in cut))
    if still:
        lines.append("still dead: " + ", ".join(f"{s.get('board')}:{s.get('task')}" for s in still))
    return "\n".join(lines)


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        done = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=config.run_env(),
        )
        return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"{exc.__class__.__name__}: {exc}"


def schedule_poke(report: dict[str, Any], cfg: dict[str, Any], *, log=None) -> dict[str, Any]:
    """Create the one-shot follow-up turn. Returns what was decided and why."""
    block = poke_settings(cfg)
    if not block.get("enabled"):
        return {"created": False, "why": "poke desactive dans la config"}
    origin = report.get("origin") or {}
    deliver = resolve_deliver(origin, cfg)
    when = time.time() + float(block.get("delay_s") or 45)
    schedule = datetime.fromtimestamp(when).strftime("%Y-%m-%dT%H:%M:%S")
    prompt = str(block.get("prompt") or "").strip() or build_prompt(report)
    name = f"gateway-restart {report.get('id') or '?'}"
    cmd = [
        config.hermes_bin(cfg), "cron", "create", schedule, prompt,
        "--name", name, "--deliver", deliver, "--repeat", "1",
    ]
    rc, out = _run(cmd)
    if log:
        log(f"poke cron rc={rc} deliver={deliver} at={schedule}: {out[:200]}")
    if rc != 0:
        return {"created": False, "why": out[:300], "deliver": deliver, "at": schedule}
    match = _JOB_ID_RE.search(out)
    return {
        "created": True,
        "job_id": match.group(1) if match else "",
        "deliver": deliver,
        "at": schedule,
        "output": out[:300],
    }
