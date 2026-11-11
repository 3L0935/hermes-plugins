"""Plan + notice tests: the restart must be schedulable and the re-poke must land once."""

from __future__ import annotations

import json
import time

import pytest

from gateway_restart.gwrestart import arm, config, notices

CFG = {
    "unit": "hermes-gateway",
    "default_delay_s": 15,
    "drain_timeout_s": 300,
    "young_worker_s": 120,
    "heartbeat_stale_s": 180,
}

SNAP_WAIT = {
    "items": [{"kind": "kanban_worker", "ref": "t_1", "klass": "drain", "eta_s": 90}],
    "verdict": {"when": "wait", "wait_s": 90, "drain": ["t_1"], "cut": [], "reclaim": []},
    "unit": {"main_pid": 1033},
    "gateway": {"state": "running", "platforms": {"discord": "connected"}},
    "companions": {"hermes-hub": {"active": True}},
}

SNAP_IDLE = {
    "items": [],
    "verdict": {"when": "now", "wait_s": 0, "drain": [], "cut": [], "reclaim": []},
    "unit": {"main_pid": 1033},
    "gateway": {"state": "running", "platforms": {}},
    "companions": {},
}

ORIGIN = {"session_id": "s1", "platform": "cli", "chat_id": "c1", "thread_id": ""}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def test_plan_waits_when_work_progresses():
    spec = arm.build_plan(SNAP_WAIT, CFG, delay_s=15, reason="test", origin=ORIGIN)
    assert spec["policy"]["wait_for_drain"] is True
    assert spec["earliest_epoch"] < spec["drain_deadline_epoch"]
    assert spec["drain_deadline_epoch"] - spec["earliest_epoch"] == pytest.approx(300, abs=2)


def test_force_never_waits():
    spec = arm.build_plan(SNAP_WAIT, CFG, delay_s=5, on_busy="force", origin=ORIGIN)
    assert spec["policy"]["wait_for_drain"] is False
    assert spec["drain_deadline_epoch"] == spec["earliest_epoch"]


def test_idle_plan_is_still_schedulable():
    spec = arm.build_plan(SNAP_IDLE, CFG, origin=ORIGIN)
    assert spec["earliest_epoch"] > time.time()
    assert spec["policy"]["delay_s"] >= 1


def test_spec_roundtrip(home):
    spec = arm.build_plan(SNAP_IDLE, CFG, delay_s=3, origin=ORIGIN, restart_id="20260101-000000-abcd")
    path = arm.write_spec(spec)
    assert path.is_file()
    again = arm.read_spec("20260101-000000-abcd")
    assert again and again["id"] == spec["id"]
    assert again["hermes_home"] == str(home)


def test_notice_is_delivered_exactly_once(home):
    origin = dict(ORIGIN)
    notices.enqueue(origin, "gateway up", "/tmp/report.json", "20260101-000000-abcd")
    assert notices.pending_texts("s1", "cli") == ["gateway up"]
    path, _record = notices.pending("s1", "cli")[0]
    notices.mark_injected(path)
    assert notices.pending_texts("s1", "cli") == []


def test_notice_falls_back_to_chat_when_session_rotated(home):
    notices.enqueue(
        {"session_id": "old-session", "platform": "discord", "chat_id": "123", "thread_id": ""},
        "gateway up",
        "/tmp/report.json",
        "20260101-000000-ef01",
    )
    assert notices.pending_texts("new-session", "discord", "123") == ["gateway up"]
    assert notices.pending_texts("new-session", "discord", "999") == []


def test_other_sessions_get_nothing(home):
    notices.enqueue(dict(ORIGIN), "gateway up", "/tmp/report.json", "20260101-000000-1111")
    assert notices.pending_texts("someone-else", "cli") == []


def test_render_report_says_what_was_cut_and_what_still_hurts():
    report = {
        "id": "x",
        "report_path": "/tmp/r.json",
        "restart": {"at": "2026-01-01T10:00:00", "downtime_s": 23},
        "health": {"ok": True, "platforms": {"discord": "connected"}},
        "drain": {"cut": [{"kind": "kanban_worker", "ref": "t_9"}], "drained": []},
        "wake": {
            "cron": {"alive": True, "ticker_age_s": 12},
            "kanban": {"dead_claims_seen": 1, "manual_reclaimed": 0, "still_dead": [{"board": "b", "task": "t_9"}]},
            "companions": {},
        },
        "next_steps": ["kanban encore mort: reclaim manuel a relancer"],
    }
    text = notices.render_report(report)
    assert "coupe au restart" in text
    assert "t_9" in text
    assert "a faire" in text
    channel = notices.render_report(report, for_channel=True)
    assert "rapport complet" not in channel


def test_spec_json_is_valid_utf8_json(home):
    spec = arm.build_plan(SNAP_IDLE, CFG, reason="accents: redemarrage", origin=ORIGIN)
    path = arm.write_spec(spec)
    json.loads(path.read_text(encoding="utf-8"))
