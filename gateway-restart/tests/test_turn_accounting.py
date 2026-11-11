"""The arming turn must never be counted as work worth waiting for."""

from __future__ import annotations

from gateway_restart.gwrestart import probe

CFG = {"gateway_turn_eta_s": 60}


def _state(agents):
    return {"pid": 1, "active_agents": agents, "gateway_state": "running", "platforms": {}}


def test_own_turn_is_excluded():
    items = probe.gateway_turns(_state(1), CFG, 0, exclude_self_turn=True)
    assert items == []


def test_other_turn_still_counts():
    items = probe.gateway_turns(_state(2), CFG, 0, exclude_self_turn=True)
    assert len(items) == 1
    assert items[0]["count"] == 1


def test_runner_sees_every_turn():
    items = probe.gateway_turns(_state(1), CFG, 0)
    assert len(items) == 1
    assert items[0]["count"] == 1
