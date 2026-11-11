"""The poke must be a real turn, not a note waiting for someone to type."""

from __future__ import annotations

import pytest

from gateway_restart.gwrestart import poke

REPORT = {
    "id": "20260101-000000-aaaa",
    "report_path": "/tmp/r.json",
    "drain": {"cut": [{"kind": "kanban_worker", "ref": "t_9"}]},
    "wake": {"kanban": {"still_dead": [{"board": "b", "task": "t_8"}]}},
}


def test_prompt_tells_the_turn_what_to_do_and_what_not_to_do():
    text = poke.build_prompt(REPORT)
    assert "restarted successfully" in text
    assert "/tmp/r.json" in text
    assert "Do not restart anything" in text
    assert "t_9" in text and "t_8" in text


def test_settings_defaults_and_override():
    assert poke.poke_settings({})["deliver"] == "auto"
    custom = poke.poke_settings({"poke": {"deliver": "bot-chat:work", "delay_s": 5}})
    assert custom["deliver"] == "bot-chat:work"
    assert custom["delay_s"] == 5
    assert custom["enabled"] is True


def test_disabled_poke_creates_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(poke, "_run", lambda *a, **k: called.append(a) or (0, ""))
    out = poke.schedule_poke(REPORT, {"poke": {"enabled": False}})
    assert out["created"] is False
    assert called == []


def test_schedule_builds_a_one_shot_cron_job(monkeypatch):
    seen = {}

    def fake_run(cmd, timeout=60):
        seen["cmd"] = cmd
        return 0, "Created job: abc123\n  Name: x\n  Schedule: once"

    monkeypatch.setattr(poke, "_run", fake_run)
    out = poke.schedule_poke(REPORT, {"poke": {"delay_s": 30}})
    assert out["created"] is True
    assert out["job_id"] == "abc123"
    cmd = seen["cmd"]
    assert cmd[1:3] == ["cron", "create"]
    assert "--deliver" in cmd and cmd[cmd.index("--deliver") + 1] == "bot-chat"
    assert "--repeat" in cmd and cmd[cmd.index("--repeat") + 1] == "1"
    # the schedule is an ISO timestamp in the near future, not "30m"
    assert "T" in cmd[3]


def test_failed_creation_is_reported_not_hidden(monkeypatch):
    monkeypatch.setattr(poke, "_run", lambda *a, **k: (1, "boom"))
    out = poke.schedule_poke(REPORT, {})
    assert out["created"] is False
    assert "boom" in out["why"]


CFG = {"session_only_platforms": ["", "local", "cli", "cron", "api_server"]}


def test_delivery_follows_the_origin_channel():
    origin = {"platform": "discord", "chat_id": "123", "thread_id": ""}
    assert poke.resolve_deliver(origin, CFG) == "discord:123"


def test_delivery_keeps_the_thread():
    origin = {"platform": "telegram", "chat_id": "-100", "thread_id": "42"}
    assert poke.resolve_deliver(origin, CFG) == "telegram:-100:42"


def test_cli_origin_wakes_the_bot_chat_it_came_from():
    origin = {"platform": "local", "chat_id": "local-abc", "session_id": "local-abc"}
    assert poke.resolve_deliver(origin, CFG) == "bot-chat"
    named = {"platform": "cli", "chat_id": "local-abc", "profile": "work"}
    assert poke.resolve_deliver(named, CFG) == "bot-chat:work"


def test_explicit_target_still_wins():
    origin = {"platform": "discord", "chat_id": "123"}
    cfg = dict(CFG, poke={"deliver": "bot-chat"})
    assert poke.resolve_deliver(origin, cfg) == "bot-chat"
