# Black Ice

Local-first home monitoring. Sensor plugins emit events, a local LLM triages them
for threat, and a live dashboard plus a voice assistant surface what matters.
Nothing leaves the LAN.

## Running it

```bash
uv sync                                   # Python 3.13
cp .env.example .env
uv run blackice hash-password             # paste into ADMIN_PASSWORD_HASH
uv run blackice serve                     # http://localhost:8080

cd dashboard && npm install && npm run dev # http://localhost:5173
```

The dashboard dev server proxies `/api`, `/media`, and `/ws` to the backend.
LM Studio must be serving on `:1234` with the models named in `.env`.

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

## Tests

```bash
uv run pytest -q      # 120 tests
uv run ruff check .
```
