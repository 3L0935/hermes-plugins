"""Post-restart notices: how the agent learns that the gateway came back, and where.

The notice is bound to the session that armed the restart (session id first, then
platform+chat as a fallback for a rotated session on the same conversation). It is
delivered by the plugin's ``pre_llm_call`` hook, which injects it into the next turn's
user message, and by a channel message when the restart was armed from a messaging
platform. Both paths consume the same record exactly once.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config


def _iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch if epoch is not None else time.time()).isoformat(timespec="seconds")


def origin_from_env(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Where this tool call is running: session, platform, chat, thread.

    Read from the process env Hermes sets per session, then refined from the session row
    (``state.db``) for the thread id, which the env does not carry.
    """
    cfg = cfg or config.settings()
    platform = (os.environ.get("HERMES_SESSION_PLATFORM") or "").strip().lower()
    session_id = (os.environ.get("HERMES_SESSION_ID") or "").strip()
    chat_id = (os.environ.get("HERMES_SESSION_CHAT_ID") or "").strip()
    thread_id = ""
    if session_id:
        try:
            import sqlite3

            db = config.hermes_home() / "state.db"
            if db.is_file():
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
                try:
                    row = conn.execute(
                        "select source, chat_id, thread_id from sessions where id = ?", (session_id,)
                    ).fetchone()
                finally:
                    conn.close()
                if row:
                    platform = (row[0] or platform or "").strip().lower()
                    chat_id = (row[1] or chat_id or "").strip()
                    thread_id = (row[2] or "").strip()
        except Exception:  # noqa: BLE001 - origin lookup must never block a restart
            pass
    session_only = {str(p).lower() for p in (cfg.get("session_only_platforms") or [])}
    return {
        "session_id": session_id,
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "channel": platform not in session_only and bool(chat_id),
        "cwd": os.getcwd(),
    }


def _queue_file(restart_id: str) -> Path:
    return config.queue_dir() / f"{restart_id}.json"


def enqueue(origin: dict[str, Any], text: str, report_path: str, restart_id: str) -> Path:
    record = {
        "id": restart_id,
        "created_at": _iso(),
        "session_id": (origin or {}).get("session_id") or "",
        "platform": (origin or {}).get("platform") or "",
        "chat_id": (origin or {}).get("chat_id") or "",
        "thread_id": (origin or {}).get("thread_id") or "",
        "text": text,
        "report_path": report_path,
        "injected_at": None,
        "injected_where": None,
    }
    path = _queue_file(restart_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def _matches(record: dict[str, Any], session_id: str, platform: str, chat_id: str) -> bool:
    if record.get("injected_at"):
        return False
    origin_sid = record.get("session_id") or ""
    if origin_sid and session_id and origin_sid == session_id:
        return True
    origin_platform = (record.get("platform") or "").lower()
    origin_chat = record.get("chat_id") or ""
    if origin_platform and origin_chat and origin_platform == (platform or "").lower() and origin_chat == chat_id:
        return True
    return False


def pending(
    session_id: str, platform: str, chat_id: str = "", limit: int = 3
) -> list[tuple[Path, dict[str, Any]]]:
    """Pending notices for this session. O(empty dir) when nothing was ever armed."""
    root = config.state_dir() / "queue"
    if not root.is_dir():
        return []
    found: list[tuple[Path, dict[str, Any]]] = []
    try:
        for entry in os.scandir(root):
            if not entry.name.endswith(".json") or entry.name.endswith(".json.tmp"):
                continue
            try:
                record = json.loads(Path(entry.path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if _matches(record, session_id, platform, chat_id):
                found.append((Path(entry.path), record))
    except OSError:
        return []
    found.sort(key=lambda pair: pair[1].get("created_at") or "")
    return found[:limit]


def pending_texts(session_id: str, platform: str, chat_id: str = "") -> list[str]:
    return [record.get("text") or "" for _path, record in pending(session_id, platform, chat_id) if record.get("text")]


def mark_injected(path: Path, where: str = "session") -> None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    record["injected_at"] = _iso()
    record["injected_where"] = where
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def prune(cfg: dict[str, Any] | None = None) -> None:
    cfg = cfg or config.settings()
    days = float(cfg.get("keep_notices_days") or 7)
    cutoff = time.time() - days * 86400
    root = config.state_dir() / "queue"
    if not root.is_dir():
        return
    try:
        for entry in os.scandir(root):
            try:
                if entry.stat().st_mtime < cutoff:
                    Path(entry.path).unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:
        pass


def _fmt_age(seconds: Any) -> str:
    if seconds is None:
        return "?"
    seconds = float(seconds)
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def render_report(report: dict[str, Any], *, for_channel: bool = False) -> str:
    """Compact French summary shared by the session notice and the channel message."""
    rid = report.get("id") or "?"
    health = report.get("health") or {}
    wake = report.get("wake") or {}
    drain = report.get("drain") or {}
    restart = report.get("restart") or {}

    head = (
        f"[gateway-restart {rid}] Gateway saine a {str(restart.get('healthy_at') or restart.get('at') or '')[11:19]}"
        f" (commande a {str(restart.get('at') or '')[11:19]}, {_fmt_age(restart.get('downtime_s'))} d'indispo)"
        if health.get("ok")
        else f"[gateway-restart {rid}] Restart PAS sain: {health.get('note') or 'inconnu'}"
    )
    lines = [head]
    platforms = health.get("platforms") or {}
    if platforms:
        lines.append("plateformes: " + ", ".join(f"{k}={v}" for k, v in platforms.items()))
    cut = drain.get("cut") or []
    drained = drain.get("drained") or []
    if drained:
        lines.append(f"attendu puis fini seul: {len(drained)} item(s)")
    if cut:
        lines.append("coupe au restart: " + ", ".join(f"{c.get('kind')}:{c.get('ref')}" for c in cut))
    cron = wake.get("cron") or {}
    if cron:
        lines.append(f"cron: ticker {'vivant' if cron.get('alive') else 'MUET'} (vu il y a {_fmt_age(cron.get('ticker_age_s'))})")
    kanban_wake = wake.get("kanban") or {}
    if kanban_wake:
        still = kanban_wake.get("still_dead") or []
        lines.append(
            f"kanban: {kanban_wake.get('dead_claims_seen', 0)} claim(s) mort(s),"
            f" reclaim manuel {kanban_wake.get('manual_reclaimed', 0)},"
            f" encore morts {len(still)}"
        )
        if still:
            lines.append("  a reprendre: " + ", ".join(f"{s.get('board')}:{s.get('task')}" for s in still[:6]))
    companions = wake.get("companions") or {}
    started = [name for name, entry in companions.items() if entry.get("restarted")]
    if started:
        lines.append("units relancees: " + ", ".join(started))
    steps = report.get("next_steps") or []
    if steps:
        lines.append("a faire: " + " | ".join(steps))
    if not for_channel:
        lines.append(f"rapport complet: {report.get('report_path') or ''}")
    return "\n".join(lines)
