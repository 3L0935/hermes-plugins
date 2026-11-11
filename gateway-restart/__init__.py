"""gateway-restart: restart the Hermes gateway from inside a session, without losing the turn.

Tool ``gateway_restart`` (actions: status / arm / cancel / report) plus a ``pre_llm_call``
hook that injects the post-restart report into the session that armed it.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

TOOL_NAME = "gateway_restart"
TOOLSET = "gateway_ops"

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Restart the Hermes gateway (systemd unit) from inside a session without killing the "
        "current turn. 'status' probes in-flight work (kanban workers, cron runs, live gateway "
        "turns) and judges per item whether it can be cut or is worth waiting for. 'arm' writes "
        "the plan, launches a detached systemd transient timer, then verifies health, wakes what "
        "the restart interrupted, and reports back into THIS session on the next turn. "
        "'cancel' stops an armed restart. 'report' reads the last run. Requires systemd --user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "arm", "cancel", "report"],
                "description": "status = read-only probe; arm = schedule the restart; cancel = stop an armed timer; report = last report.",
            },
            "delay_s": {
                "type": "integer",
                "description": "Minimum seconds before the restart (cancel window). Default from config.",
            },
            "drain_timeout_s": {
                "type": "integer",
                "description": "Max extra seconds the runner waits for in-flight work to finish by itself.",
            },
            "on_busy": {
                "type": "string",
                "enum": ["auto", "wait", "force"],
                "description": "auto = cut what is young/silent, wait for what progresses; wait = never cut; force = restart after delay_s whatever runs.",
            },
            "reason": {"type": "string", "description": "Why the restart is needed (kept in the report)."},
            "restart_id": {"type": "string", "description": "For cancel/report: which armed restart."},
            "notify": {"type": "boolean", "description": "Post the report into the arming channel too (messaging platforms only)."},
        },
        "required": ["action"],
    },
}


def _payload(ok: bool, action: str, summary: str, **extra: Any) -> str:
    body = {"success": ok, "action": action, "summary": summary}
    body.update(extra)
    return json.dumps(body, ensure_ascii=False)


def _handle(args: dict[str, Any], **kwargs: Any) -> str:
    from .gwrestart import arm, config, notices, probe

    action = str(args.get("action") or "status").lower()
    cfg = config.settings()
    origin = notices.origin_from_env(cfg)

    try:
        if action == "status":
            snap = probe.snapshot(cfg, exclude_self_turn=True)
            return _payload(True, action, probe.render_status(snap), snapshot=snap)

        if action == "arm":
            snap = probe.snapshot(cfg, exclude_self_turn=True)
            on_busy = str(args.get("on_busy") or "auto")
            spec = arm.build_plan(
                snap,
                cfg,
                delay_s=args.get("delay_s"),
                drain_timeout_s=args.get("drain_timeout_s"),
                on_busy=on_busy,
                reason=str(args.get("reason") or ""),
                origin=origin,
            )
            if args.get("notify") is False:
                spec["notify"] = False
            info = arm.launch(spec, cfg)
            verdict = snap.get("verdict") or {}
            will_cut = verdict.get("cut") or []
            will_wait = verdict.get("drain") or []
            at_local = time.strftime("%H:%M:%S", time.localtime(spec["earliest_epoch"]))
            summary = (
                f"restart arme id={spec['id']} unit={info['unit']} a partir de {at_local} "
                f"(>={info['delay_s']}s, timer {info['timer'].get('ActiveState', '?')}). "
                f"attente de drain: {'oui' if spec['policy']['wait_for_drain'] else 'non'} "
                f"max {spec['policy']['drain_timeout_s']}s. "
                f"en vol: {len(will_wait)} a laisser finir, {len(will_cut)} jeune(s)/coupe(s). "
                f"annuler: gateway_restart(action='cancel', restart_id='{spec['id']}')."
            )
            warning = ""
            if origin.get("platform") == "cron":
                warning = (
                    "appel depuis une session cron: ce job mourra avec la gateway. "
                    "Prevoir un delay superieur au temps restant du job."
                )
            return _payload(True, action, summary, restart_id=spec["id"], unit=info["unit"],
                            spec=spec, timer=info["timer"], warning=warning)

        if action == "cancel":
            restart_id = str(args.get("restart_id") or "").strip()
            if not restart_id:
                return _payload(False, action, "restart_id requis pour cancel")
            result = arm.cancel(restart_id, cfg)
            ok = bool(result.get("cancelled"))
            return _payload(ok, action, f"cancel {restart_id}: {'ok' if ok else 'echec'}", result=result)

        if action == "report":
            report = arm.read_report(str(args.get("restart_id") or "").strip() or None)
            if not report:
                return _payload(False, action, "aucun rapport de restart trouve")
            return _payload(True, action, notices.render_report(report), report=report)

        return _payload(False, action, f"action inconnue: {action}")
    except Exception as exc:  # noqa: BLE001 - a tool must answer, never raise
        return _payload(False, action, f"{exc.__class__.__name__}: {exc}")


def _on_pre_llm_call(**kwargs: Any) -> Any:
    """Deliver a pending post-restart report to the session that armed it, exactly once."""
    try:
        platform = str(kwargs.get("platform") or "")
        if platform.lower() == "cron":
            return None
        from .gwrestart import notices

        session_id = str(kwargs.get("session_id") or "")
        chat_id = os.environ.get("HERMES_SESSION_CHAT_ID") or ""
        pairs = notices.pending(session_id, platform, chat_id)
        if not pairs:
            return None
        texts: list[str] = []
        for path, record in pairs:
            text = str(record.get("text") or "").strip()
            if text:
                texts.append(text)
            notices.mark_injected(path, "session")
        if not texts:
            return None
        return {"context": "\n\n".join(texts)}
    except Exception:  # noqa: BLE001 - a hook must never break a turn
        return None


def register(ctx: Any) -> None:
    ctx.register_tool(
        name=TOOL_NAME,
        toolset=TOOLSET,
        schema=TOOL_SCHEMA,
        handler=_handle,
        check_fn=lambda: True,
        emoji="\u267b\ufe0f",
    )
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
