---
name: new-plugin
description: Create a new Black Ice sensor plugin, start to finish — interview the author until the design is unambiguous, then scaffold, implement, install, and test it. Use whenever someone wants to add a sensor, integrate a device or API into Black Ice, expose a new capability to the assistant, or says "write a plugin" / "add a sensor" / "make blackice able to X".
---

# Writing a Black Ice plugin

A plugin is a Python package that subclasses `SensorPlugin` and is discovered
through the `blackice.plugins` entry-point group. It can emit events, declare
tools the LLM may call, and declare dashboard widgets — all without importing
core services.

Work in this order. **Do not write code before finishing the interview.**

## 1. Interview

Most plugin bugs are specification bugs. Ask before building.

Skip any question the author already answered unambiguously in their request;
never skip one by guessing. Use `AskUserQuestion` (max 4 per call), in two
rounds, and offer concrete options rather than open prompts.

**Round 1 — what it is**

| Ask | Why it changes the code |
|---|---|
| What does this sensor observe, and where does the reading come from — a local device, a network API, a file/log, or nothing external (computed)? | Decides dependencies, credentials, and whether `start()` can fail |
| How does data arrive: polled on an interval, pushed to us, or read only when asked? | Polled/pushed needs a background task and `stop()` cancellation; on-demand needs neither |
| Does it need history — a private SQLite table — or is only the latest reading meaningful? | Decides whether `start()` runs a schema |
| Does any text come from the outside world (OCR, transcripts, device-supplied labels, filenames)? | That text is `sensor_text`, never `summary`. See "Trust" below |

**Round 2 — what it exposes**

| Ask | Why it changes the code |
|---|---|
| Which events reach the timeline, and at what severity (info / low / medium / high / critical)? | Wrong severity either floods triage or hides a real event |
| What should the assistant be able to *do* here — the tools, in the author's words? | Becomes `ToolSpec`s, projected to the LLM as `<plugin>.<tool>` |
| Can any tool cause a physical or irreversible action (unlock, disarm, actuate, delete, spend)? | Never assume "no". Changes confirmation and severity design |
| What should the dashboard show, from: stat, gauge, timeseries, bar, donut, table, kv, log, status, image, gallery, video, audio, map, toggle? | Each widget needs a `data_source` that `query()` answers. Types outside this list render a fallback panel |

Also settle, if not already obvious: alarm rules and whether each is armed by
default; configuration and secrets (env var names); the plugin's short name.

**Close the interview** by playing back a numbered spec — sensor id, events,
tools with parameters, widgets with data sources, alarm rules, config — and
getting an explicit yes. Ambiguity that survives to this point becomes a stated
assumption in the summary, not a silent decision.

## 2. Scaffold

Naming is conventional; follow it exactly:

```
plugins/blackice-plugin-<name>/
  pyproject.toml            # entry point: <name> = "blackice_<name>:<Name>Plugin"
  blackice_<name>/
    __init__.py             # the plugin class, and any module constants tests import
tests/test_<name>.py
```

Sensor ids are `<name>.<sensor>` (e.g. `clock.local`, `heartbeat.pulse`).
Copy the `pyproject.toml` from `plugins/blackice-plugin-clock/` and change the
three names — it is nine lines and already correct.

## 3. Implement

Read one existing plugin first and match its shape:

- `plugins/blackice-plugin-heartbeat/` — background loop, private SQLite, events, alarm rule
- `plugins/blackice-plugin-clock/` — on-demand only, no task, no storage

The contract itself is `blackice/plugins/base.py`; the models are
`blackice/models.py`. Read `references/contract.md` for the full surface —
`PluginContext`, `Event`, `SensorDescriptor`, and what core does with each.

**The rules that are easy to get wrong**

Every call into a plugin crosses `Supervisor` (`blackice/plugins/supervisor.py`),
and that shapes four things:

1. **An exception marks the whole plugin unhealthy on the dashboard.** Bad
   *caller* input — an unknown timezone, a malformed id — is not a plugin
   fault: return `{"error": "..."}` from `handle_command`. Reserve raising for
   genuine plugin failure.
2. **Every call is under a 30s timeout**, `start()` included. Long or endless
   work goes in `asyncio.create_task`, cancelled in `stop()`.
3. **`stop()` must be idempotent** — it is called on failure paths too.
4. **`describe()` is synchronous and must never raise.** It cannot be timed
   out; if it throws, the plugin reports no sensors at all.

And four more from the wider design:

5. **Trust.** Text the sensor supplies goes in `Event.sensor_text`, which
   triage wraps as untrusted and memory refuses to consolidate. Never fold it
   into `summary` — `summary` is plugin-authored text. Neither `summary`,
   `sensor_text`, nor `payload` can become a durable memory
   (`blackice/memory/consolidate.py`), and that is deliberate.
6. **Emit only through `ctx.emit`.** It stamps `plugin` for you and returns the
   stored event id. Never write to core tables.
7. **Storage is `ctx.db`** — a private SQLite file per plugin, opened for you.
   Create tables in `start()` with `CREATE TABLE IF NOT EXISTS`.
8. **Widgets are JSON, never browser code.** Every `WidgetSpec.data_source`
   must be answered by `query()`, or its panel shows an error.

## 4. Install, test, verify

Entry-point discovery only sees installed packages, so install before testing —
otherwise the discovery test fails for a reason that looks like a bug:

```bash
uv pip install -e plugins/blackice-plugin-<name>
```

Write `tests/test_<name>.py` modelled on `tests/test_clock.py`, using the
`data_dir` fixture and a `Registry` fixture. Cover, at minimum:

- discovery finds the plugin by name
- `start()` projects the sensor (and any alarm rules) into core tables
- each tool returns what the interview said it would
- each widget `data_source` returns data
- bad input returns an error **and leaves the plugin healthy**
- tools reach the LLM: `project_plugin_tools` yields `<plugin>.<tool>`

Done means all three are clean:

```bash
uv run pytest -q
uv run ruff check .
uv run blackice serve      # plugin appears on /api/plugins with state "healthy"
```

Report which parts of the interviewed spec are implemented and which, if any,
were left out.
