# Black Ice

Local-first home monitoring. Sensor plugins emit events, a local LLM triages them
for threat, and a live dashboard plus a voice assistant surface what matters.
Nothing leaves the LAN.

## Running it

```bash
cp .env.example .env
uv run blackice hash-password    # paste into ADMIN_PASSWORD_HASH
./start.sh                       # everything, on http://localhost:8080
```

`start.sh` syncs dependencies, installs local plugins, starts LM Studio if it
is not already up, builds the dashboard when the bundle is stale, and runs the
API with voice enabled. One process, one Ctrl-C.

```
./start.sh --no-voice   dashboard + API only
./start.sh --dev        Vite dev server on :5173 with hot reload
./start.sh --rebuild    force a dashboard rebuild
```

The API serves the built dashboard itself, so `:8080` is the whole thing; the
Vite server is only for frontend hot reload. Captured media is behind
`/media/<path>` and requires a session -- it is footage from inside a house, so
it is never a bare static mount.

**Dependencies live in `pyproject.toml` extras, including the two Hugging Face
packages.** `uv sync` prunes anything undeclared, so use `uv sync --extra all`
(what `start.sh` does) or it will quietly uninstall voice2, kokoro-memory and
piper. Editable plugin installs are pruned the same way.

## Auto-reload

`AUTO_RELOAD` (default on) restarts the service when core code, `schema.sql`,
a `*.prompt*` file, or an installed plugin changes. Plugin sources are resolved
through `find_spec`, so an editable install is watched where it actually lives
rather than wherever it was launched from. `data/` is explicitly excluded --
the SQLite WAL, the rotating log and captured media all change while the
service runs, and watching them restarts it in a loop.

Two costs worth knowing before leaving it on:

- **A broken save takes monitoring offline.** The reloader survives a syntax
  error but the app does not; the port stops answering until the file is valid
  again. It recovers on its own once it is.
- **With voice on, every save costs ~15-25s of deafness** while Whisper, Silero
  and Piper reload. The mic is closed for that whole window.

`./start.sh` honours the setting; `blackice serve --no-reload` overrides it for
a single run.

## Layout

| Path | What it is |
|---|---|
| `blackice/services/` | The action layer. REST routes and LLM tools both call these, which is why every dashboard action is also reachable by voice. |
| `blackice/llm/` | LM Studio client, `normalize()`, PromptGuard, tool registry, harness loop |
| `blackice/triage/` | Three tiers: rules → small model → 27B |
| `blackice/plugins/` | Plugin contract, entry-point discovery, supervisor, per-plugin SQLite |
| `blackice/memory/`, `blackice/rsi/` | kokoro-memory wrapper and the feedback loop |
| `dashboard/src/blackice/` | Pages, widget registry, live socket, console |
| `plugins/` | First-party plugins, installed with `uv pip install -e` |

## Writing a plugin

Subclass `SensorPlugin`, declare an entry point in the `blackice.plugins` group,
and return a `SensorDescriptor` from `describe()`. See
`plugins/blackice-plugin-heartbeat/` for a complete, minimal example.

Widgets are declared as JSON (`WidgetSpec`), not shipped as browser code. The
renderer registry lives in `dashboard/src/blackice/widgets.jsx`; an unrecognised
type renders a labelled fallback rather than a blank panel.

## Voice

```bash
uv run blackice voice-check    # prerequisites
uv run blackice voice          # listen; one Ctrl-C exits
```

Wake word is `ASSISTANT_NAME`. Follow-ups inside 30s need no wake word.

Setup notes, each of which cost an afternoon:

- **CA certificates.** The python.org framework build ships without a CA bundle,
  so `torch.hub` (Silero VAD) and Hugging Face downloads fail and report it as
  "no internet connection". `blackice/_certs.py` points SSL at certifi at import.
- **`torchaudio`** is required by Silero but missing from voice2's requirements.
- **Piper** comes from PyPI (`uv pip install piper-tts`); there is no brew
  formula. Voices are `.onnx` + `.onnx.json` pairs under
  `~/.local/share/piper-voices/`, selected with `PIPER_VOICE`.
- **Speaking rate** lives in the voice's `.onnx.json` (`inference.length_scale`),
  because voice2 invokes `piper` without a rate flag. Lower is faster.
- **Latency.** voice2 defaults to 5s of trailing silence before it will answer;
  `VOICE_END_SILENCE_MS` overrides it. `MODEL_VOICE` lets spoken replies use a
  faster model than the dashboard.
- **Slow replies announce themselves.** If the model takes longer than
  `VOICE_FILLER_DELAY_S` (3s), a short phrase is spoken so a pause does not read
  as "it never heard me". It reuses the in-flight turn id -- starting a new one
  would make the real answer stale and playback would discard it.
- **Cues only confirm being addressed** (`VOICE_CUE_MODE=wake`). voice2 chimes
  on every detected utterance and again when it starts thinking, both of which
  happen before anyone knows the speech was meant for it. `all` restores that,
  `off` leaves only the failure buzz.
- **Spacebar interrupt is off** (`VOICE_KEYBOARD_INTERRUPT`). voice2's keyboard
  worker calls `tty.setraw()`, which clears ISIG so Ctrl-C never becomes SIGINT,
  and OPOST so console output walks diagonally down the screen. Barge-in by
  voice is unaffected. Turn it on only on a real terminal you don't mind that
  happening to.

## Logging

Standard `logging`, configured once in `blackice/logging_setup.py` and used by
every entry point. `LOG_LEVEL` and `LOG_FILE` control it; the file handler
rotates at 10 MB.

voice2 ships its own logger that calls `print()` directly, so it ignored levels,
handlers and the log file. `blackice/voice/voice2_logging.py` replaces that
function -- its workers look it up as a module attribute, so one substitution
reaches all of them -- and keeps the JSON-lines latency file it writes. Routine
per-turn chatter (`floor_set`, `transition`, `speak_entry`) drops to DEBUG;
`vad_load_error` and friends are ERROR.

## The daily self-review

Once every `RSI_REVIEW_HOURS` the primary model reads back what happened --
triage outcomes, escalations, and your verdicts on them -- and may propose
edits to the triage prompt and to its own system prompt. Run it by hand with
`uv run blackice review`.

Nothing goes live because the model liked it. A candidate is stored as an
inactive version, then replayed over a golden set built from events *you have
judged*; it must agree with you at least as often as the prompt it would
replace. Only then does `RSI_SELF_EDIT_ENABLED` decide whether it activates or
waits in the review queue. Below `RSI_GOLDEN_SET_MIN` judged events the gate
refuses to rule at all, because it cannot tell two prompts apart on noise.

Every version keeps its parent, rationale, author and diff, and
`promptstore.rollback()` returns to the newest *human*-authored version rather
than stepping back one edit at a time.

**This is the riskiest part of the system.** A prompt that quietly stops
escalating is indistinguishable from a quiet week. The gate, the minimum
golden set and the default-off activation are what make it acceptable; keep
`RSI_SELF_EDIT_ENABLED=false` until you have judged enough escalations for the
gate to mean something.

## Tests

```bash
uv run pytest              # unit and service tests, scripted models
uv run pytest -m integration   # the real models in LM Studio, slow
```

The integration tests cover the parts scripted clients cannot: that the small
model actually escalates a break-in and absorbs routine motion, that it answers
without a thinking block, and that the primary model produces a usable review
of its own prompts.

## Two rules worth knowing

**Trust is split by origin.** Text you type or say is a command channel: a
PromptGuard hit blocks it. Text a *sensor* supplies (OCR, device labels,
transcripts) is data: a hit wraps it as untrusted and delivers it anyway, because
discarding it would blind you to the event you wanted to see.

**Sensor text can never become a memory.** Consolidation builds facts from an
allow-list of structured fields plus your own conversation, never from
sensor-supplied strings — otherwise anyone who controls what a camera sees could
write persistent instructions into the assistant's long-term memory.
`tests/test_guard.py` and the memory-trust test guard both rules.
