#!/usr/bin/env python3
"""Detached restart runner. Started by a systemd transient timer, outside the gateway cgroup.

Phases: wait for a drain window -> snapshot -> restart the unit -> verify health -> wake
interrupted subsystems -> write the report -> queue a notice for the arming session and
(optionally) post it into that channel.

Runs on stdlib only, both as ``python runner.py <spec>`` and ``python -m gwrestart.runner``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):  # executed as a script by systemd
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from gwrestart import arm, config, health, notices, probe  # noqa: E402
else:
    from . import arm, config, health, notices, probe  # noqa: F401,E402


def _iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch if epoch is not None else time.time()).isoformat(timespec="seconds")


def _unit_cfg(spec: dict) -> dict:
    cfg = config.settings()
    policy = spec.get("policy") or {}
    for key in ("young_worker_s", "heartbeat_stale_s", "cron_run_soft_cap_s", "cron_young_s", "gateway_turn_eta_s"):
        if policy.get(key) is not None:
            cfg[key] = policy[key]
    cfg["unit"] = spec.get("unit") or cfg.get("unit")
    if spec.get("hermes_bin"):
        cfg["hermes_bin"] = spec["hermes_bin"]
    if policy.get("drain_timeout_s") is not None:
        cfg["drain_timeout_s"] = policy["drain_timeout_s"]
    return cfg


def _compact_items(items: list[dict]) -> list[dict]:
    keys = ("kind", "ref", "board", "label", "pid", "klass", "age_s", "eta_s", "why", "profile")
    return [{k: item.get(k) for k in keys} for item in items]


def _send_channel(spec: dict, text: str, log) -> dict:
    origin = spec.get("origin") or {}
    platform = (origin.get("platform") or "").lower()
    session_only = {str(p).lower() for p in (config.settings().get("session_only_platforms") or [])}
    if platform in session_only or not origin.get("chat_id"):
        return {"sent": False, "why": "pas de canal (session locale/cli): notice in-session seulement"}
    target = f"{platform}:{origin['chat_id']}"
    if origin.get("thread_id"):
        target += f":{origin['thread_id']}"
    try:
        done = subprocess.run(
            [cfg_hermes_bin(spec), "send", "-t", target, "-f", "-", "-q"],
            input=text,
            capture_output=True,
            text=True,
            timeout=90,
            env=config.run_env(),
        )
    except Exception as exc:  # noqa: BLE001
        return {"sent": False, "target": target, "why": f"{exc.__class__.__name__}: {exc}"}
    payload = (done.stdout or done.stderr or "").strip()[:300]
    log(f"notify channel {target}: rc={done.returncode} {payload}")
    return {"sent": done.returncode == 0, "target": target, "output": payload}


def cfg_hermes_bin(spec: dict) -> str:
    return spec.get("hermes_bin") or config.hermes_bin()


def _next_steps(report: dict) -> list[str]:
    steps: list[str] = []
    health_res = report.get("health") or {}
    if not health_res.get("ok"):
        steps.append("gateway pas saine: lire ~/.hermes/logs/gateway.log et systemctl --user status")
    cut = (report.get("drain") or {}).get("cut") or []
    if cut:
        steps.append(f"{len(cut)} item(s) coupe(s) en vol: verifier leur reprise")
    still = ((report.get("wake") or {}).get("kanban") or {}).get("still_dead") or []
    if still:
        steps.append("kanban encore mort: reclaim manuel a relancer")
    cron = (report.get("wake") or {}).get("cron") or {}
    if cron and not cron.get("alive"):
        steps.append("ticker cron muet: verifier hermes cron status")
    return steps


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: runner.py <spec.json>", file=sys.stderr)
        return 2
    spec_path = Path(argv[1])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    restart_id = str(spec.get("id") or spec_path.parent.name)
    if spec.get("hermes_home"):
        os.environ["HERMES_HOME"] = str(spec["hermes_home"])
    cfg = _unit_cfg(spec)

    log_path = spec_path.parent / "runner.log"
    log_handle = log_path.open("a", encoding="utf-8", buffering=1)
    timeline: list[dict] = []

    def log(message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        try:
            print(line, flush=True)
            log_handle.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass

    def mark(event: str, **extra) -> None:
        timeline.append({"at": _iso(), "event": event, **extra})

    started = time.time()
    report: dict = {
        "id": restart_id,
        "reason": spec.get("reason") or "",
        "origin": spec.get("origin") or {},
        "policy": spec.get("policy") or {},
        "started_at": _iso(started),
        "before": spec.get("before") or {},
        "timeline": timeline,
    }
    log(f"runner start id={restart_id} unit={cfg.get('unit')} on_busy={(spec.get('policy') or {}).get('on_busy')}")

    try:
        # ---------------------------------------------------------------- wait for a window
        earliest = float(spec.get("earliest_epoch") or started)
        deadline = float(spec.get("drain_deadline_epoch") or earliest)
        wait_for_drain = bool((spec.get("policy") or {}).get("wait_for_drain"))
        mark("arming", earliest_at=_iso(earliest), drain_deadline_at=_iso(deadline))

        while True:
            now = time.time()
            if now < earliest:
                time.sleep(min(2.0, earliest - now))
                continue
            if not wait_for_drain:
                break
            live = probe.snapshot(cfg, now)
            drains = [item for item in live["items"] if item.get("klass") == "drain"]
            if not drains:
                log("drain: plus rien a attendre")
                break
            if now >= deadline:
                log(f"drain: timeout, {len(drains)} item(s) coupe(s): {[i['ref'] for i in drains]}")
                break
            time.sleep(3)

        # ------------------------------------------------------------------ final snapshot
        before_live = probe.snapshot(cfg)
        items = before_live["items"]
        cut = [i for i in items if i.get("klass") in {"cut_ok", "drain"}]
        drained = [record for record in (spec.get("before") or {}).get("items", [])
                   if record.get("ref") not in {i.get("ref") for i in items}]
        report["drain"] = {
            "waited_s": round(time.time() - earliest, 1) if time.time() > earliest else 0,
            "cut": _compact_items([i for i in items if i.get("klass") == "drain"]),
            "killed_young_or_dead": _compact_items([i for i in items if i.get("klass") == "cut_ok"]),
            "drained_while_waiting": len(drained),
            "reclaim_pending": _compact_items([i for i in items if i.get("klass") == "reclaim"]),
        }
        mark("snapshot_before_restart", items=len(items), cut=len(cut))

        # ------------------------------------------------------------------------ restart
        old_pid = (before_live.get("unit") or {}).get("main_pid") or 0
        unit = str(cfg.get("unit"))
        t_restart = time.time()
        done = subprocess.run(
            ["systemctl", "--user", "restart", unit],
            capture_output=True,
            text=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
            env=config.run_env(),
        )
        log(f"systemctl restart {unit}: rc={done.returncode} {(done.stderr or '').strip()[:200]}")
        mark("restart_issued", unit=unit, old_pid=old_pid, rc=done.returncode)

        # ------------------------------------------------------------------------- health
        health_res = health.wait_healthy(old_pid, cfg, log=log)
        downtime = round(time.time() - t_restart, 1)
        report["restart"] = {
            "unit": unit,
            "old_pid": old_pid,
            "new_pid": health_res.get("pid"),
            "command_rc": done.returncode,
            "command_output": (done.stderr or done.stdout or "").strip()[:400],
            "at": _iso(t_restart),
            "healthy_at": _iso() if health_res.get("ok") else None,
            "downtime_s": downtime,
        }
        report["health"] = health_res
        mark("healthy" if health_res.get("ok") else "unhealthy", pid=health_res.get("pid"), downtime_s=downtime)

        # --------------------------------------------------------------------------- wake
        report["wake"] = health.wake(cfg, (spec.get("before") or {}), restart_id=restart_id, log=log)
        mark("wake_done")

        report["next_steps"] = _next_steps(report)
        report["finished_at"] = _iso()
        report["duration_s"] = round(time.time() - started, 1)
        report["ok"] = bool(health_res.get("ok"))
    except Exception as exc:  # noqa: BLE001 - a broken runner must still leave a report
        report["ok"] = False
        report["error"] = f"{exc.__class__.__name__}: {exc}"
        report["traceback"] = traceback.format_exc()[-2000:]
        report.setdefault("health", {"ok": False, "note": report["error"]})
        report.setdefault("next_steps", ["runner en erreur: voir runner.log"])
        report["finished_at"] = _iso()
        report["duration_s"] = round(time.time() - started, 1)
        log(f"ERROR {report['error']}")

    # --------------------------------------------------------------- report + notice + ping
    report_path = spec_path.parent / "report.json"
    report["report_path"] = str(report_path)
    try:
        tmp = report_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(report_path)
        log(f"report ecrit: {report_path}")
    except OSError as exc:
        log(f"report non ecrit: {exc}")

    text = notices.render_report(report)
    try:
        notices.enqueue(report.get("origin") or {}, text, str(report_path), restart_id)
        log("notice enqueuee")
    except Exception as exc:  # noqa: BLE001
        log(f"notice non enqueuee: {exc}")
    if spec.get("notify"):
        report["channel"] = _send_channel(spec, notices.render_report(report, for_channel=True), log)
    try:
        notices.prune(cfg)
        arm.prune_reports(cfg)
    except Exception:  # noqa: BLE001
        pass
    try:
        log_handle.close()
    except Exception:  # noqa: BLE001
        pass
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
