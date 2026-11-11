"""Arming a restart: build the plan, write the spec, hand it to a detached systemd unit.

Why a systemd transient timer instead of just calling ``systemctl restart`` here: the
agent's tool calls run inside the gateway's own cgroup, so a restart issued from this
process kills the process that would report the result. ``systemd-run --user`` creates a
unit owned by the user manager, outside that cgroup, which survives the restart and can
report afterwards.
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, probe

UNIT_PREFIX = "hermes-gwrestart-"


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


def build_plan(
    snap: dict[str, Any],
    cfg: dict[str, Any],
    *,
    delay_s: int | None = None,
    drain_timeout_s: int | None = None,
    on_busy: str = "auto",
    reason: str = "",
    origin: dict[str, Any] | None = None,
    restart_id: str | None = None,
) -> dict[str, Any]:
    """Turn a snapshot + policy into the spec the detached runner will execute."""
    now = time.time()
    delay = int(delay_s if delay_s is not None else cfg.get("default_delay_s") or 15)
    delay = max(1, delay)
    drain_timeout = int(drain_timeout_s if drain_timeout_s is not None else cfg.get("drain_timeout_s") or 300)
    on_busy = on_busy if on_busy in {"auto", "wait", "force"} else "auto"

    verdict = snap.get("verdict") or {}
    wait_for_drain = on_busy != "force"
    if on_busy == "auto" and verdict.get("when") != "wait":
        wait_for_drain = True  # re-judged by the runner; young/dead work never blocks

    earliest = now + delay
    return {
        "id": restart_id or new_id(),
        "created_at": _iso(now),
        "created_epoch": now,
        "reason": reason or "",
        "origin": origin or {},
        "unit": str(cfg.get("unit") or "hermes-gateway"),
        "python": config.python_bin(),
        "hermes_bin": config.hermes_bin(cfg),
        "hermes_home": str(config.hermes_home()),
        "policy": {
            "on_busy": on_busy,
            "delay_s": delay,
            "drain_timeout_s": drain_timeout,
            "wait_for_drain": wait_for_drain,
            "young_worker_s": cfg.get("young_worker_s"),
            "heartbeat_stale_s": cfg.get("heartbeat_stale_s"),
        },
        "earliest_epoch": earliest,
        "drain_deadline_epoch": earliest + (drain_timeout if wait_for_drain else 0),
        "notify": bool(cfg.get("notify_messaging_platforms")) and bool(origin),
        "before": {
            "verdict": verdict,
            "items": snap.get("items") or [],
            "unit_pid": (snap.get("unit") or {}).get("main_pid"),
            "gateway_state": (snap.get("gateway") or {}).get("state"),
            "platforms": (snap.get("gateway") or {}).get("platforms") or {},
            "companions": snap.get("companions") or {},
        },
    }


def spec_dir(spec: dict[str, Any]) -> Path:
    path = config.state_dir() / str(spec["id"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_spec(spec: dict[str, Any]) -> Path:
    path = spec_dir(spec) / "spec.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def read_spec(restart_id: str) -> dict[str, Any] | None:
    path = config.state_dir() / restart_id / "spec.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_specs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    root = config.state_dir()
    try:
        for child in sorted(root.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            spec = read_spec(child.name)
            if spec:
                out.append(spec)
    except OSError:
        pass
    return out


def runner_path() -> Path:
    return Path(__file__).resolve().parent / "runner.py"


def launch(spec: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Create the detached transient timer. Returns the unit name + scheduling proof."""
    if not shutil.which("systemd-run"):
        raise RuntimeError(
            "systemd-run introuvable: impossible de detacher le restart du cgroup de la gateway. "
            "Aucun fallback (setsid resterait dans le meme cgroup et serait tue)."
        )
    spec_path = write_spec(spec)
    unit = f"{UNIT_PREFIX}{spec['id']}"
    delay = max(1, int(spec["earliest_epoch"] - time.time()))
    cmd = [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        f"--on-active={delay}s",
        "--collect",
        "--quiet",
        "--description=Hermes gateway restart armed by agent",
        spec["python"],
        str(runner_path()),
        str(spec_path),
    ]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=config.run_env())
    if done.returncode != 0:
        raise RuntimeError(f"systemd-run a echoue: {(done.stderr or done.stdout or '').strip()[:400]}")
    show = subprocess.run(
        ["systemctl", "--user", "show", f"{unit}.timer", "-p", "ActiveState", "-p", "NextElapseUSecRealtime"],
        capture_output=True,
        text=True,
        timeout=15,
        env=config.run_env(),
    )
    proof = {}
    for line in (show.stdout or "").splitlines():
        key, _, value = line.partition("=")
        proof[key] = value.strip()
    if proof.get("ActiveState") != "active":
        raise RuntimeError(f"timer cree mais non actif: {proof or (show.stderr or '').strip()[:300]}")
    return {"unit": unit, "timer": proof, "spec_path": str(spec_path), "delay_s": delay}


def cancel(restart_id: str, cfg: dict[str, Any]) -> dict[str, Any]:
    spec = read_spec(restart_id)
    if spec is None:
        return {"cancelled": False, "error": f"spec inconnu: {restart_id}"}
    unit = f"{UNIT_PREFIX}{restart_id}"
    stopped = []
    for suffix in (".timer", ".service"):
        done = subprocess.run(
            ["systemctl", "--user", "stop", f"{unit}{suffix}"],
            capture_output=True,
            text=True,
            timeout=20,
            env=config.run_env(),
        )
        if done.returncode == 0:
            stopped.append(suffix.lstrip("."))
    spec["cancelled_at"] = _iso(time.time())
    write_spec(spec)
    alive = probe.unit_state(f"{unit}.timer", cfg)
    return {"cancelled": not alive.get("active"), "stopped": stopped, "unit": unit, "timer_state": alive}


def report_path(restart_id: str) -> Path:
    return config.state_dir() / restart_id / "report.json"


def read_report(restart_id: str | None = None) -> dict[str, Any] | None:
    if restart_id:
        path = report_path(restart_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    root = config.state_dir()
    try:
        candidates = sorted(
            (child for child in root.iterdir() if (child / "report.json").is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    if not candidates:
        return None
    try:
        return json.loads((candidates[0] / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def prune_reports(cfg: dict[str, Any]) -> None:
    keep = int(cfg.get("keep_reports") or 10)
    root = config.state_dir()
    try:
        dirs = [d for d in root.iterdir() if d.is_dir() and (d / "report.json").is_file()]
    except OSError:
        return
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in dirs[keep:]:
        shutil.rmtree(stale, ignore_errors=True)
