"""Judgement tests: what gets cut, what gets waited for, what blocks nothing."""

from __future__ import annotations

from gateway_restart.gwrestart import probe

CFG = {
    "young_worker_s": 120,
    "heartbeat_stale_s": 180,
    "cron_run_soft_cap_s": 180,
    "cron_young_s": 30,
    "gateway_turn_eta_s": 60,
    "drain_timeout_s": 300,
}


def _kanban(age, hb, alive=True, budget=None):
    return {
        "kind": "kanban_worker",
        "ref": "t_1",
        "board": "default",
        "pid": 42,
        "pid_alive": alive,
        "age_s": age,
        "heartbeat_age_s": hb,
        "max_runtime_s": budget,
    }


def test_dead_worker_is_reclaim_not_drain():
    item = probe.classify(_kanban(600, 10, alive=False), CFG)
    assert item["klass"] == "reclaim"
    assert item["eta_s"] == 0


def test_young_worker_is_cut_ok():
    assert probe.classify(_kanban(30, 10), CFG)["klass"] == "cut_ok"


def test_silent_worker_is_cut_ok():
    assert probe.classify(_kanban(900, 400), CFG)["klass"] == "cut_ok"


def test_progressing_worker_drains_with_budget_eta():
    item = probe.classify(_kanban(300, 20, budget=900), CFG)
    assert item["klass"] == "drain"
    assert item["eta_s"] == 600


def test_progressing_worker_without_budget_drains_unknown():
    item = probe.classify(_kanban(300, 20), CFG)
    assert item["klass"] == "drain"
    assert item["eta_s"] is None


def test_cron_run_young_cut_old_drain():
    fresh = {"kind": "cron_run", "ref": "job", "pid": 7, "pid_alive": True, "age_s": 5}
    old = {"kind": "cron_run", "ref": "job", "pid": 7, "pid_alive": True, "age_s": 120}
    assert probe.classify(fresh, CFG)["klass"] == "cut_ok"
    assert probe.classify(old, CFG)["klass"] == "drain"
    assert probe.classify(old, CFG)["eta_s"] == 60


def test_gateway_turn_drains():
    item = probe.classify(
        {"kind": "gateway_turn", "ref": "gateway", "pid": 1, "pid_alive": True, "age_s": 0}, CFG
    )
    assert item["klass"] == "drain"
    assert item["eta_s"] == 60


def test_pid_alive_rejects_garbage():
    assert probe.pid_alive(0) is False
    assert probe.pid_alive(None) is False
    assert probe.pid_alive("nope") is False
