"""Read-only snapshot of what is running, and the per-item judgement on what may be cut.

The judgement answers one question per in-flight item: if the gateway dies right now,
does this lose real work? Three verdicts:

``reclaim``  the process is already gone; nothing to lose, the owning scheduler reclaims it
``cut_ok``   alive but young / silent / out of budget: restarting it is cheap
``drain``    alive and making progress: worth waiting for (bounded by an ETA when known)

No writes here. Every source is read with the narrowest possible access (sqlite ``mode=ro``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config


# --------------------------------------------------------------------------- process helpers


def pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:  # a zombie answers kill(0) but is not doing work
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return stat.rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return True


def pid_cmdline(pid: Any, limit: int = 120) -> str:
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except (OSError, TypeError, ValueError):
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()[:limit]


def _run(cmd: list[str], timeout: int = 25) -> tuple[int, str]:
    try:
        done = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=config.run_env(),
        )
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        return 1, f"{exc.__class__.__name__}: {exc}"


def _iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(epoch if epoch is not None else time.time()).isoformat(timespec="seconds")


def _age_from_iso(text: Any, now: float) -> float | None:
    if not text:
        return None
    raw = str(text).strip().replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return max(0.0, now - stamp.timestamp())
    return max(0.0, now - stamp.timestamp())


# --------------------------------------------------------------------------- sources


def unit_state(unit: str, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    rc, out = _run(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "MainPID",
            "-p",
            "NRestarts",
        ]
    )
    data: dict[str, Any] = {"unit": unit, "active": False, "sub": "", "main_pid": 0, "nrestarts": 0}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key == "ActiveState":
            data["active"] = value.strip() == "active"
            data["state"] = value.strip()
        elif key == "SubState":
            data["sub"] = value.strip()
        elif key == "MainPID":
            try:
                data["main_pid"] = int(value.strip() or 0)
            except ValueError:
                data["main_pid"] = 0
        elif key == "NRestarts":
            try:
                data["nrestarts"] = int(value.strip() or 0)
            except ValueError:
                data["nrestarts"] = 0
    if rc != 0 and not data.get("state"):
        data["error"] = out.strip()[:400]
    return data


def gateway_state() -> dict[str, Any] | None:
    path = config.hermes_home() / "gateway_state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cron_job_names() -> dict[str, str]:
    path = config.hermes_home() / "cron" / "jobs.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    jobs = raw.get("jobs") if isinstance(raw, dict) else raw
    names: dict[str, str] = {}
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    for job in jobs or []:
        if not isinstance(job, dict):
            continue
        job_id = job.get("id") or job.get("job_id")
        if not job_id:
            continue
        names[str(job_id)] = str(job.get("name") or job.get("title") or (job.get("prompt") or "")[:60] or job_id)
    return names


def cron_in_flight(cfg: dict[str, Any], now: float) -> list[dict[str, Any]]:
    db = config.hermes_home() / "cron" / "executions.db"
    if not db.is_file():
        return []
    names = cron_job_names()
    items: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "select job_id, pid, status, started_at, claimed_at from executions"
            " where status in ('claimed','running')"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    for job_id, pid, status, started_at, claimed_at in rows:
        age = _age_from_iso(started_at, now)
        if age is None:
            age = _age_from_iso(claimed_at, now)
        items.append(
            {
                "kind": "cron_run",
                "ref": str(job_id),
                "board": "",
                "label": names.get(str(job_id), str(job_id)),
                "pid": int(pid) if pid else None,
                "pid_alive": pid_alive(pid),
                "age_s": age,
                "heartbeat_age_s": None,
                "max_runtime_s": int(cfg.get("cron_run_soft_cap_s") or 180),
                "status": status,
            }
        )
    return items


def _kanban_paths() -> list[tuple[str, Path]]:
    home = config.hermes_home()
    found: list[tuple[str, Path]] = []
    default_db = home / "kanban.db"
    if default_db.is_file():
        found.append(("default", default_db))
    boards = home / "kanban" / "boards"
    if boards.is_dir():
        try:
            for child in sorted(boards.iterdir()):
                db = child / "kanban.db"
                if db.is_file():
                    found.append((child.name, db))
        except OSError:
            pass
    return found


def kanban_running(now: float) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for board, db in _kanban_paths():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        except sqlite3.Error:
            continue
        try:
            rows = conn.execute(
                "select r.task_id, r.profile, r.worker_pid, r.started_at, r.last_heartbeat_at,"
                " r.max_runtime_seconds, t.title, t.status"
                " from task_runs r left join tasks t on t.id = r.task_id"
                " where r.status = 'running'"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        for task_id, profile, worker_pid, started_at, hb, max_runtime, title, task_status in rows:
            age = max(0.0, now - float(started_at)) if started_at else None
            hb_age = max(0.0, now - float(hb)) if hb else None
            items.append(
                {
                    "kind": "kanban_worker",
                    "ref": str(task_id),
                    "board": board,
                    "label": (str(title or task_id))[:80],
                    "profile": profile or "",
                    "pid": int(worker_pid) if worker_pid else None,
                    "pid_alive": pid_alive(worker_pid),
                    "age_s": age,
                    "heartbeat_age_s": hb_age,
                    "max_runtime_s": int(max_runtime) if max_runtime else None,
                    "status": task_status or "",
                }
            )
    return items


def gateway_turns(
    state: dict[str, Any] | None,
    cfg: dict[str, Any],
    now: float,
    *,
    exclude_self_turn: bool = False,
) -> list[dict[str, Any]]:
    if not state:
        return []
    items: list[dict[str, Any]] = []
    try:
        agents = int(state.get("active_agents") or 0)
    except (TypeError, ValueError):
        agents = 0
    if exclude_self_turn and agents > 0:
        agents -= 1  # the turn that is asking about the restart is not work to preserve
    if agents > 0:
        items.append(
            {
                "kind": "gateway_turn",
                "ref": "gateway",
                "board": "",
                "label": f"{agents} turn(s) en cours dans la gateway",
                "pid": state.get("pid"),
                "pid_alive": True,
                "age_s": 0.0,
                "heartbeat_age_s": 0.0,
                "max_runtime_s": int(cfg.get("gateway_turn_eta_s") or 60),
                "count": agents,
            }
        )
    return items


# --------------------------------------------------------------------------- judgement


def classify(item: dict[str, Any], cfg: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Fill ``class``/``eta_s``/``why`` on one in-flight item (mutates and returns it)."""
    now = now if now is not None else time.time()
    age = item.get("age_s")
    alive = bool(item.get("pid_alive"))
    kind = item["kind"]

    if kind in {"kanban_worker", "cron_run"} and not alive:
        item.update(
            klass="reclaim",
            eta_s=0,
            why="processus deja mort: rien a perdre, le reclaim est automatique",
        )
        return item

    if kind == "kanban_worker":
        young = float(cfg.get("young_worker_s") or 120)
        stale = float(cfg.get("heartbeat_stale_s") or 180)
        hb = item.get("heartbeat_age_s")
        if age is not None and age < young:
            item.update(klass="cut_ok", eta_s=0, why=f"lance il y a {int(age)}s: perte minime")
        elif hb is not None and hb > stale:
            item.update(klass="cut_ok", eta_s=0, why=f"silencieux depuis {int(hb)}s: rien ne progresse")
        else:
            budget = item.get("max_runtime_s")
            eta = max(0.0, float(budget) - float(age or 0)) if budget else None
            item.update(
                klass="drain",
                eta_s=eta,
                why="worker vivant qui progresse"
                + (f", budget restant ~{int(eta)}s" if eta is not None else ", duree inconnue"),
            )
        return item

    if kind == "cron_run":
        cap = float(cfg.get("cron_run_soft_cap_s") or 180)
        young = float(cfg.get("cron_young_s") or 30)
        eta = max(0.0, cap - float(age or 0))
        if age is not None and age < young:
            item.update(klass="cut_ok", eta_s=eta, why=f"run de {int(age)}s: cout de rejeu faible")
        else:
            item.update(
                klass="drain",
                eta_s=eta,
                why=f"run cron borne a ~{int(cap)}s, reste ~{int(eta)}s",
            )
        return item

    # gateway_turn
    item.update(
        klass="drain",
        eta_s=float(cfg.get("gateway_turn_eta_s") or 60),
        why="conversation en cours dans la gateway",
    )
    return item


def snapshot(
    cfg: dict[str, Any] | None = None, now: float | None = None, *, exclude_self_turn: bool = False
) -> dict[str, Any]:
    cfg = cfg or config.settings()
    now = now if now is not None else time.time()
    unit = unit_state(str(cfg.get("unit") or "hermes-gateway"), cfg)
    state = gateway_state()

    items = (
        kanban_running(now)
        + cron_in_flight(cfg, now)
        + gateway_turns(state, cfg, now, exclude_self_turn=exclude_self_turn)
    )
    for item in items:
        classify(item, cfg, now)

    drains = [i for i in items if i.get("klass") == "drain"]
    kills = [i for i in items if i.get("klass") == "cut_ok"]
    reclaims = [i for i in items if i.get("klass") == "reclaim"]

    if drains:
        eta_values = [i.get("eta_s") for i in drains]
        if any(v is None for v in eta_values):
            wait_s = int(cfg.get("drain_timeout_s") or 300)
        else:
            wait_s = int(min(float(cfg.get("drain_timeout_s") or 300), max(float(v) for v in eta_values)))
        when = "wait"
        why = f"{len(drains)} item(s) en cours de progression: attendre au plus {wait_s}s"
    else:
        wait_s = 0
        when = "now" if (kills or reclaims or not items) else "now"
        why = "rien a attendre" if not items else "seul du travail jeune ou deja mort serait coupe"

    platforms = {}
    if isinstance(state, dict) and isinstance(state.get("platforms"), dict):
        for name, payload in state["platforms"].items():
            if isinstance(payload, dict):
                platforms[name] = payload.get("state") or "?"

    companions = {}
    for name in cfg.get("companion_units") or []:
        companions[name] = unit_state(str(name), cfg)

    return {
        "taken_at": _iso(now),
        "taken_epoch": now,
        "unit": unit,
        "gateway": {
            "pid": (state or {}).get("pid"),
            "state": (state or {}).get("gateway_state"),
            "active_agents": (state or {}).get("active_agents"),
            "platforms": platforms,
        },
        "companions": companions,
        "cron_ticker_age_s": ticker_age_s(),
        "items": items,
        "verdict": {"when": when, "wait_s": wait_s, "why": why,
                    "drain": [i["ref"] for i in drains],
                    "cut": [i["ref"] for i in kills],
                    "reclaim": [i["ref"] for i in reclaims]},
    }


def ticker_age_s() -> float | None:
    path = config.hermes_home() / "cron" / "ticker_heartbeat"
    try:
        raw = path.read_text(encoding="utf-8").split()
    except OSError:
        return None
    try:
        return max(0.0, time.time() - float(raw[0]))
    except (IndexError, ValueError):
        return None


# --------------------------------------------------------------------------- rendering


def _fmt_age(seconds: Any) -> str:
    if seconds is None:
        return "?"
    seconds = float(seconds)
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def render_status(snap: dict[str, Any]) -> str:
    """Compact human summary; the JSON stays the source of truth."""
    unit = snap["unit"]
    gw = snap["gateway"]
    lines = [
        f"gateway  {unit['unit']}: {'active' if unit.get('active') else (unit.get('state') or 'inconnu')}"
        f" pid={unit.get('main_pid')} restarts={unit.get('nrestarts')}",
        f"etat     gateway_state={gw.get('state')} active_agents={gw.get('active_agents')}"
        f" plateformes={', '.join(f'{k}:{v}' for k, v in (gw.get('platforms') or {}).items()) or 'aucune'}",
        f"cron     ticker vu il y a {_fmt_age(snap.get('cron_ticker_age_s'))}",
    ]
    companions = snap.get("companions") or {}
    if companions:
        lines.append(
            "voisins  " + ", ".join(f"{name}:{'active' if st.get('active') else (st.get('state') or '?')}"
                                    for name, st in companions.items())
        )
    items = snap.get("items") or []
    if not items:
        lines.append("en vol   rien")
    else:
        lines.append(f"en vol   {len(items)} item(s)")
        for item in items:
            lines.append(
                f"  - [{item.get('klass')}] {item['kind']} {item['ref']}"
                f" ({(item.get('label') or '')[:50]}) age={_fmt_age(item.get('age_s'))}"
                f" pid={item.get('pid')} -> {item.get('why')}"
            )
    verdict = snap["verdict"]
    lines.append(f"verdict  {verdict['when']} (attente max {verdict['wait_s']}s) : {verdict['why']}")
    return "\n".join(lines)
