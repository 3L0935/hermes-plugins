# gateway-restart

Restart the Hermes gateway **from inside a session**, without losing the turn that asked for it.

## Why a plugin is needed

`systemctl --user restart hermes-gateway` works from a shell, but an agent cannot use it: the
agent's terminal calls run **inside the gateway's own cgroup** (`/proc/self/cgroup` →
`.../app.slice/hermes-gateway.service`). The restart therefore kills the process that is
executing the command, and the turn dies with no result to report.

This plugin moves the restart into a **systemd transient unit** (`systemd-run --user
--on-active=Ns`), which systemd's user manager owns. That unit is outside the gateway cgroup,
survives the restart, and reports back afterwards.

## Tool: `gateway_restart`

| action | what it does |
|---|---|
| `status` | read-only probe: unit state, gateway state, platforms, cron ticker, companion units, and every in-flight item with a verdict |
| `arm` | writes the plan, launches the detached timer, returns the id + cancel handle |
| `cancel` | stops an armed timer (`systemctl --user stop <unit>.timer`) |
| `report` | last run report (health, what was cut, what was woken) |

Parameters: `delay_s`, `drain_timeout_s`, `on_busy` (`auto` | `wait` | `force`), `reason`,
`restart_id`, `notify`.

### The per-item judgement

Every in-flight item gets one of three verdicts, so the restart does not blindly cut work:

| verdict | meaning |
|---|---|
| `reclaim` | process already gone: nothing to lose, the owning scheduler reclaims it |
| `cut_ok` | alive but **young** (< `young_worker_s`), **silent** (heartbeat older than `heartbeat_stale_s`), or a cron run inside its cheap window: restarting it costs almost nothing |
| `drain` | alive and progressing: worth waiting for, bounded by the item's budget when known |

`on_busy=auto` (default) cuts the `cut_ok` ones immediately and waits up to `drain_timeout_s`
for the `drain` ones; the runner re-probes every 3 s and fires as soon as they are gone.
`on_busy=wait` never cuts. `on_busy=force` restarts after `delay_s` regardless (the report
still lists what was killed).

Sources used (all read-only): `systemctl --user show`, `<hermes_home>/gateway_state.json`,
`<hermes_home>/cron/executions.db` (`status in ('claimed','running')`),
`<hermes_home>/cron/ticker_heartbeat`, and every kanban board DB
(`<hermes_home>/kanban.db` + `<hermes_home>/kanban/boards/*/kanban.db`, `task_runs.status='running'`
joined with `tasks`).

### Post-restart phases

1. **health**: poll until the unit is `active`, the `MainPID` is a *new* pid, `gateway_state` is
   `running`, and every platform that was connected is `connected` again (`health_timeout_s`).
2. **wake**: companion units that were alive before and are dead now are started; the cron
   ticker is waited for (it self-heals); kanban claims whose worker pid is dead are left to the
   dispatcher's first tick, and reclaimed by hand only if still dead after
   `manual_reclaim_after_s`.
3. **report**: JSON in `<hermes_home>/gateway/restart/<id>/report.json` + `runner.log`.
4. **re-poke**: two lanes, because a session notice alone is not a push.
   - **poke (default, autonomous)**: a one-shot cron job fires `delay_s` after the gateway is
     healthy and delivers its output as a **real inbound turn**, so the agent wakes up on its own
     and works the report. Nobody has to be in front of the machine.
     `poke.deliver: auto` (default) resolves to **the chat the tool call came from**:
     a messaging origin (discord/telegram/...) is addressed directly (`platform:chat_id[:thread]`),
     while a CLI/local origin falls back to `bot-chat` (the profile's canonical Bot Chat) because
     `local` is not a valid cron delivery target — `deliver: local:<chat_id>` is rejected with
     `[blocked_config] delivery platform 'local' is not a known cron delivery target`.
     Set `poke.deliver` explicitly (e.g. `discord:#hermes`) to always poke the same place.
   - **channel**: for a messaging origin (discord/telegram/...), the same summary is posted into
     that channel with `hermes send`.
   - **session notice**: a record bound to the arming session (`session_id`, then platform+chat as
     a fallback) is injected into that session's next turn by the plugin's `pre_llm_call` hook,
     exactly once. This is continuity for the conversation, not the wake-up.

## Config

All keys optional, in `~/.hermes/config.yaml`:

```yaml
plugins:
  entries:
    gateway-restart:
      unit: hermes-gateway          # systemd --user unit to restart
      default_delay_s: 15           # minimum cancel window
      drain_timeout_s: 300          # max extra wait for in-flight work
      young_worker_s: 120           # younger than this -> cut_ok
      heartbeat_stale_s: 180        # silent longer than this -> cut_ok
      cron_run_soft_cap_s: 180      # scheduler interrupts cron runs ~3 min
      cron_young_s: 30
      gateway_turn_eta_s: 60
      health_timeout_s: 180
      manual_reclaim_after_s: 150
      keep_reports: 10
      keep_notices_days: 7
      companion_units: [hermes-hub, hermes-dashboard]
      notify_messaging_platforms: true
      poke:
        enabled: true               # autonomous follow-up turn after the restart
        deliver: bot-chat           # bot-chat | bot-chat:<profile> | platform:chat
        delay_s: 45
        prompt: ""                  # empty = built-in prompt
      hermes_bin: ""                # resolved automatically when empty
```

Nothing is hardcoded: unit name, paths, thresholds and delivery all come from here or from the
active Hermes home (`HERMES_HOME`).

## Enabling

```bash
ln -s /path/to/hermes-plugins/gateway-restart ~/.hermes/plugins/gateway-restart
hermes plugins enable gateway-restart
hermes tools enable gateway_ops            # toolset exposed by the plugin
# a NEW session (or a gateway restart) is required before the tool is visible
```

## Limits (honest)

- **Requires `systemd-run --user`.** There is no fallback: `setsid`/`nohup` stay in the gateway
  cgroup and would be killed with it. The tool refuses instead of pretending.
- **A cut worker loses its work.** Dispatcher and cron reclaim it, so the card/job restarts from
  scratch; nothing resumes mid-generation.
- **`gateway_state.json` is the health oracle.** If the gateway is broken *before* the restart in
  a way that stops it writing that file, health will report a timeout, and the log is the place
  to look.
- An armed restart fires even if the arming session never comes back. `cancel`, or
  `systemctl --user stop hermes-gwrestart-<id>.timer`, stops it.

## Tests

```bash
~/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -q
```
