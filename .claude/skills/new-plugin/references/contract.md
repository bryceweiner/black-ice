# The plugin surface, in detail

Source of truth, in order: `blackice/plugins/base.py`, `blackice/models.py`,
`blackice/plugins/registry.py`, `blackice/plugins/supervisor.py`. If this file
disagrees with them, they win — re-read them.

## What core calls, and when

| Method | Called by | Notes |
|---|---|---|
| `start(ctx)` | `Registry.start_plugin`, at boot | Under the 30s timeout. Failure means the plugin never registers its sensors |
| `stop()` | shutdown, restart, failure | Must be idempotent and must not raise |
| `describe()` | after start, and on every dashboard/tool projection | **Sync**, called often, must never raise |
| `handle_command(cmd, **kwargs)` | the LLM, via a projected tool | `kwargs` are the model's tool arguments |
| `query(source, **kwargs)` | `GET /api/sensors/{id}/widgets/{source}` | Backs one `WidgetSpec.data_source` |

`Registry` mirrors each `SensorDescriptor` into the core `sensors` table and
each `AlarmRuleSpec` into `alarm_rules`, on every start. Arm state is seeded
only on first sight — a re-declare never clobbers the user's toggle.

## PluginContext

Everything a plugin may touch. Plugins do not import core services.

```python
ctx.plugin        # str, the plugin's name
ctx.db            # aiosqlite.Connection — private file at data/plugins/<name>.db,
                  # row_factory=Row, WAL, foreign keys on
ctx.log           # logging.Logger "blackice.plugin.<name>"
await ctx.emit(event) -> int    # stores the event, returns its id, stamps event.plugin
ctx.media         # currently None; attach media as MediaRef on the Event instead
```

## Event

```python
Event(
    sensor_id="cam.front",       # must match a declared SensorDescriptor.id
    severity=SEVERITY_MEDIUM,    # INFO 0, LOW 1, MEDIUM 2, HIGH 3, CRITICAL 4
    kind="motion",               # your taxonomy; used by rules and feedback
    summary="Motion at the front door",   # plugin-authored text
    sensor_text=None,            # text the outside world supplied — see Trust
    payload={"zone": 3},         # structured detail, JSON-serialisable
    media=[MediaRef(path=..., mime="image/jpeg")],
)
```

`plugin` and `ts` are filled in for you.

### Trust

`sensor_text` is the channel for anything an attacker could influence: OCR
output, ASR transcripts, device-supplied names, filenames, third-party API
strings. Triage wraps it with `wrap_untrusted()` before the model sees it
(`blackice/triage/pipeline.py`), and memory consolidation refuses to build facts
from it. Putting that text in `summary` instead defeats the wrapper. Nothing
event-borne except `sensor_id`, `kind`, `severity`, `ts`, `tier`, and `verdict`
can ever inform a durable memory — `tests/test_memory.py` guards the allow-list.

## SensorDescriptor

```python
SensorDescriptor(
    id="clock.local",       # "<plugin>.<sensor>"
    name="Clock",           # shown on the dashboard
    kind="clock",           # free-form category
    widgets=[WidgetSpec(...)],
    streams=[StreamDescriptor(kind="mjpeg"|"hls"|"webrtc"|"clip", url=..., name=...)],
    alarm_rules=[AlarmRuleSpec(key=..., name=..., description=..., spec={},
                               default_armed=False)],
    tools=[ToolSpec(name=..., description=..., parameters={JSON Schema})],
)
```

`ToolSpec.parameters` is a JSON Schema object; `{"type": "object", "properties": {}}`
for a no-argument tool. Descriptions are the model's only instruction manual —
write them for a reader who cannot see the code.

Tools are exposed to the LLM as `<plugin>.<tool>` by
`project_plugin_tools` (`blackice/llm/tools.py`), which routes back through
`Registry.command` and therefore through the supervisor.

## Widget types and the data each renderer expects

The registry is `dashboard/src/blackice/widgets.jsx`. An unrecognised type
renders a labelled fallback, not a blank panel. Return these shapes from
`query()`:

| Type | Shape |
|---|---|
| `stat` | `{"value": ..., "label": "..."}` |
| `gauge` | `{"value": 0-100, "label": "..."}` |
| `timeseries`, `bar`, `donut` | list of rows; **first key is the x/category, second is the value**; rendered reversed, so return newest-first |
| `table` | list of uniform dicts — keys become columns |
| `kv` | one flat dict, insertion-ordered |
| `log` | list of dicts; values are joined into one line each |
| `status` | `{"state": "online"|"healthy"|"degraded"|"offline"|"unhealthy"}` |
| `image` | `{"url": ...}` |
| `gallery` | `[{"url": ..., "thumb": ...}, ...]` |
| `video` | `{"kind": "mjpeg"|"hls"|..., "url": ...}` |
| `audio` | `{"url": ...}` |
| `map` | `{"lat": ..., "lon": ..., "label": "..."}`, or `{"points": [{"lat", "lon", "label"}, ...]}` for a scatter. Positions are plotted relative to each other — there is no tile layer, by design |
| `toggle` | `{"armed": bool}` |

`span` is bootstrap columns out of 12 (default 6).

## Speaking unprompted

A plugin cannot speak. It emits an event; something in the voice process turns
that into speech. Today the only such thing is `blackice/voice/announce.py`,
which watches for `kind="reminder"` events and announces them — see
`plugins/blackice-plugin-clock/` for the emitting half.

Two constraints if you add another announcement path. The bus is **in-process**,
and `blackice serve` and `blackice voice` are separate processes with separate
buses and separate plugin instances, so a bus subscriber only sees events raised
on its own side; the shared database is the only thing both can see. And if a
plugin's scheduler can run in both processes, the plugin must make firing
exactly-once itself — `ReminderStore.claim` does it with a compare-and-set.

## Failure handling

`Supervisor` wraps every call: 30s timeout, exception capture, health persisted
to the `plugin_health` table and surfaced on the dashboard. A raise sets
`state="unhealthy"` and records `last_error`; the next successful call clears
it. `restart()` backs off exponentially and gives up after 5 attempts.

This is why expected, caller-caused errors should be returned as data
(`{"error": "..."}`) rather than raised: a user's typo should not light up a
health badge. Genuine failures — the device is gone, the API is down — *should*
raise, so they show up.

## Testing

`tests/conftest.py` provides `data_dir`, which redirects `DATA_DIR` at a
tmp_path and reopens the core database. The usual fixture:

```python
@pytest.fixture
async def reg(data_dir):
    r = Registry()
    await r.start_plugin(MyPlugin, events.record)
    yield r
    await r.stop_all()
```

`Registry.discover()` reads installed entry points, so
`uv pip install -e plugins/blackice-plugin-<name>` must have been run first.
Assert failure containment explicitly — that a bad argument returns an error
*and* leaves `reg.supervisors["<name>"].health()["state"] == "healthy"`.
