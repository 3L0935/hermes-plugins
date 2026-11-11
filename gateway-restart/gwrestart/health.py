"""Post-restart verification and the wake phase.

Nothing here guesses: every check reads a file or a unit property, and every "wake" is a
measurement first. The subsystems that self-heal (cron ticker, kanban dispatcher) are
waited for rather than poked; only a claim that stays dead past its grace window is
reclaimed by hand.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any, Callable

from . import config, probe


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
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


def wait_healthy(
    old_pid: int | None,
    cfg: dict[str, Any],
    *,
    log: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll until the gateway is up on a NEW pid with its platforms connected."""
    unit = str(cfg.get("unit") or "hermes-gateway")
    timeout = float(cfg.get("health_timeout_s") or 180)
    started = time.time()
    deadline = started + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        elapsed = round(time.time() - started, 1)
        state = probe.unit_state(unit, cfg)
        gws = probe.gateway_state() or {}
        pid = state.get("main_pid") or 0
        platforms = {
            name: (payload or {}).get("state")
            for name, payload in (gws.get("platforms") or {}).items()
            if isinstance(payload, dict)
        }
        last = {
            "unit_active": bool(state.get("active")),
            "unit_state": state.get("state"),
            "pid": pid,
            "gateway_state": gws.get("gateway_state"),
            "platforms": platforms,
            "elapsed_s": elapsed,
        }
        if log:
            log(f"health {elapsed}s pid={pid} state={gws.get('gateway_state')} platforms={platforms}")
        fresh_pid = bool(pid) and (old_pid is None or pid != old_pid)
        not_connected = {k: v for k, v in platforms.items() if v != "connected"}
        if state.get("active") and fresh_pid and gws.get("gateway_state") == "running" and not not_connected:
            last.update(ok=True, note="gateway up, toutes plateformes connectees")
            return last
        sleep(2)
    last.update(ok=False, note="timeout health: voir unit_state/platforms ci-dessus")
    return last


def _reclaim(task_id: str, board: str, reason: str, cfg: dict[str, Any]) -> dict[str, Any]:
    cmd = [config.hermes_bin(cfg), "kanban"]
    if board and board != "default":
        cmd += ["--board", board]
    cmd += ["reclaim", task_id, "--reason", reason]
    rc, out = _run(cmd, timeout=60)
    return {"task": task_id, "board": board, "ok": rc == 0, "output": out[:300]}


def wake(
    cfg: dict[str, Any],
    before: dict[str, Any],
    *,
    restart_id: str = "",
    log: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Bring back what the restart interrupted, and report what came back by itself."""
    result: dict[str, Any] = {"companions": {}, "cron": {}, "kanban": {}}

    # 1. companion units: only started if they were alive before and are not now
    for name in cfg.get("companion_units") or []:
        was = ((before.get("companions") or {}).get(name) or {}).get("active")
        now = probe.unit_state(str(name), cfg)
        entry = {"was_active": bool(was), "active": bool(now.get("active"))}
        if was and not now.get("active"):
            rc, out = _run(["systemctl", "--user", "start", str(name)], timeout=90)
            again = probe.unit_state(str(name), cfg)
            entry.update(restarted=rc == 0, active_after=bool(again.get("active")), output=out[:200])
            if log:
                log(f"wake companion {name}: rc={rc} active={again.get('active')}")
        result["companions"][str(name)] = entry

    # 2. cron ticker: it rewrites its heartbeat every tick; wait for a fresh one
    ticker_deadline = time.time() + 120
    age = probe.ticker_age_s()
    while (age is None or age > 90) and time.time() < ticker_deadline:
        sleep(5)
        age = probe.ticker_age_s()
    result["cron"] = {"ticker_age_s": None if age is None else round(age, 1), "alive": bool(age is not None and age <= 90)}
    if log:
        log(f"wake cron: ticker_age={result['cron']['ticker_age_s']}")

    # 3. kanban: dead claims are reclaimed by the dispatcher on its first tick.
    #    Only step in when a claim is still dead past manual_reclaim_after_s.
    grace = float(cfg.get("manual_reclaim_after_s") or 150)
    deadline = time.time() + grace
    manual: list[dict[str, Any]] = []
    while True:
        running = probe.kanban_running(time.time())
        dead = [item for item in running if not item.get("pid_alive")]
        if not dead or time.time() >= deadline:
            break
        sleep(10)
    reclaimed_total = 0
    for item in dead:
        if item.get("pid_alive"):  # re-dispatched in the meantime
            continue
        outcome = _reclaim(item["ref"], item.get("board") or "", f"gateway restart {restart_id}".strip(), cfg)
        manual.append(outcome)
        reclaimed_total += int(outcome.get("ok", False))
    still = [
        {"task": item["ref"], "board": item.get("board"), "pid": item.get("pid")}
        for item in probe.kanban_running(time.time())
        if not item.get("pid_alive")
    ]
    result["kanban"] = {
        "dead_claims_seen": len(dead),
        "manual_reclaim": manual,
        "manual_reclaimed": reclaimed_total,
        "still_dead": still,
    }
    if log:
        log(f"wake kanban: dead={len(dead)} manual={reclaimed_total} still_dead={len(still)}")
    return result
