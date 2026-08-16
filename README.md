# Black Ice

**Local-first home monitoring.** Sensor plugins emit events, a local LLM triages
them for threat, and a live dashboard plus a voice assistant surface what
matters. Nothing leaves the LAN.

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](pyproject.toml)
[![No telemetry](https://img.shields.io/badge/telemetry-none-brightgreen.svg)](#privacy)

Every piece of this runs on your own machine: the models (through LM Studio),
the database (SQLite in `data/`), the dashboard (served by the same process),
the speech recognition and the speech synthesis. There is no account, no cloud
tier, no phone-home, and no paid version. It is Apache-2.0, and the plugins are
ordinary Python packages you can fork, replace or delete.

---

## Contents

- [Privacy](#privacy) · [Requirements](#requirements) · [Quick start](#quick-start)
- [Plugins](#plugins) — [the catalogue](#the-catalogue) · [writing one](#writing-a-plugin)
- [Layout](#layout) · [Auto-reload](#auto-reload) · [Configuration](#configuration)
- [Voice](#voice) · [Memory](#memory) · [The daily self-review](#the-daily-self-review) · [Logging](#logging)
- [Tests](#tests) · [The security model](#the-security-model)
- [Contributing](#contributing) · [License and credits](#license-and-credits)

---

## Privacy

This is a system that watches a house, so the boundaries are worth stating
plainly rather than burying in a config file:

- **No outbound traffic that is not a plugin doing its declared job.** Core
  makes no analytics, update or licence calls. The plugins that do reach the
  internet (crypto balances, threat-intel lookups, model provisioning) say so in
  their own docs, and each is optional.
- **The dashboard makes no external requests.** No CDN fonts, no map tiles, no
  remote scripts. It is dark by default and works offline.
- **Captured media is authenticated.** Footage lives behind `/media/<path>` and
  requires a session — it is video from inside a house, so it is never a bare
  static mount.
- **Model weights arrive once, when you ask,** and are hashed into a lock file
  that is verified on every later load. There is no lazy first-use download.

## Requirements

| | |
|---|---|
| **Python** | 3.13 |
| **[uv](https://docs.astral.sh/uv/)** | dependency management and the launcher |
| **[LM Studio](https://lmstudio.ai/)** | serves the two local models on `:1234` |
| **Node** | only to build the dashboard bundle |
| **ffmpeg** | optional; V380 snapshots and H.265→H.264 transcode |

Two models, both local, configured in `.env`: `MODEL_PRIMARY` (the assistant and
the deep triage tier — a 27B-class model) and `MODEL_TRIAGE` (a small, fast
model for the first pass). Any OpenAI-compatible server on `LMSTUDIO_BASE_URL`
will do; LM Studio is simply what `start.sh` knows how to launch.

Developed and run on macOS. The core is portable, but the Find My plugin reads
Apple's on-disk cache and the optional CoreLocation extra is macOS-only.

## Quick start

```bash
git clone https://github.com/bryceweiner/black-ice.git
cd black-ice
cp .env.example .env
uv run blackice hash-password    # paste the result into ADMIN_PASSWORD_HASH
./start.sh                       # everything, on http://localhost:8080
```

`start.sh` syncs dependencies, installs local plugins, starts LM Studio if it is
not already up, builds the dashboard when the bundle is stale, and runs the API
with voice enabled. One process, one Ctrl-C.

```
./start.sh --no-voice   dashboard + API only
./start.sh --dev        Vite dev server on :5173 with hot reload
./start.sh --rebuild    force a dashboard rebuild
```

The API serves the built dashboard itself, so `:8080` is the whole thing; the
Vite server is only for frontend hot reload.

**Dependencies live in `pyproject.toml` extras, including the two Hugging Face
packages.** `uv sync` prunes anything undeclared, so use `uv sync --extra all`
(what `start.sh` does) or it will quietly uninstall voice2, kokoro-memory and
piper. Editable plugin installs are pruned the same way.

The dashboard opens on `/home`: uptime and throughput, the assistant inline, and
a live sensor rail, off one `/api/overview` call plus the websocket.

## Plugins

A plugin is a standalone Python package that declares an entry point in the
`blackice.plugins` group. It owns its own SQLite database, declares its sensors,
its alarm rules, its assistant tools and its dashboard widgets, and is
supervised so that a plugin which crashes does not take the service with it.
Install any of them with:

```bash
uv pip install -e plugins/blackice-plugin-<name>
```

All seven below are first-party and ship in this repository. Nothing is
mandatory — delete what you do not want, and the dashboard simply has fewer
panels.

### The catalogue

| Plugin | Sensors | What it does |
|---|---|---|
| **[clock](plugins/blackice-plugin-clock/)** | `clock.local`, `clock.reminders` | Time and date on request, plus reminders the assistant can create, edit, list and delete. A due reminder is an event, so it gets spoken. |
| **[crypto](plugins/blackice-plugin-crypto/)** | `crypto.balances` | Watches addresses across the top 100 L1/L2 chains, reports deposits and withdrawals, and raises a high-severity alert when too much leaves an address too quickly. |
| **[findmy](plugins/blackice-plugin-findmy/)** | `findmy.people`, `findmy.devices`, `findmy.items` | Reads Apple's local Find My cache: arrivals and departures across a home geofence, stale locations, low device batteries, items left behind. |
| **[heartbeat](plugins/blackice-plugin-heartbeat/)** | `heartbeat.pulse` | The reference plugin. Deliberately trivial — it exists to prove the contract and to be read as a complete, minimal example. |
| **[netguard](plugins/blackice-plugin-netguard/)** | `netguard.inventory`, `netguard.ids`, `netguard.posture` | What is on the network, what is happening that should not be, and how defensible this machine is. |
| **[v380](plugins/blackice-plugin-v380/)** | `v380.cameras` | Every V380 camera on the LAN in pure Python: discovery, AES-decrypted H.264/H.265 video, audio, PTZ and light control, live RTSP, two-way talkback. |
| **[watchers](plugins/blackice-plugin-watchers/)** | `watchers.people`, `watchers.pipeline` | Recognises people on the V380 cameras and puts each recognition on the timeline. |

#### clock

Two sensors from one plugin. `clock.local` is passive — it reads the wall clock
when asked and stores nothing. `clock.reminders` owns a table and a scheduler
loop, and emits an event the moment a reminder comes due; turning that event
into speech is `blackice/voice/announce.py`, not the plugin.

Tools: `get_time`, `get_date`, `create_reminder`, `list_reminders`,
`edit_reminder`, `delete_reminder`, `purge_reminders`.

#### crypto

One sensor covers every chain. A *watch* is a `(network, address)` pair, added
from the dashboard or by the assistant. Two rules:

- **`drain` — the security rule.** Holdings fell more than X% against the
  balance Y hours ago, with both sides valued at the *same current prices*, so
  the number reflects coins leaving the address and not the market moving. A 90%
  crash with no outflow raises nothing; moving 60% of your coins out raises a
  `HIGH` event.
- **`value_drop` — the portfolio rule.** The USD total fell more than X% over Y
  hours, whatever the cause. Never exceeds `LOW`, and off by default.

Every API key is optional and the plugin degrades rather than failing:
`ETHERSCAN_API_KEY` (or fall back to keyless Blockscout), `COINGECKO_API_KEY`,
`BLOCKFROST_PROJECT_ID`, `SUBSCAN_API_KEY`. Polling cost scales with the number
of addresses watched, not with the 100 chains in the registry. See its
[README](plugins/blackice-plugin-crypto/README.md).

#### findmy

Reads the JSON Find My leaves under
`~/Library/Caches/com.apple.findmy.fmipcore/` — no Apple credentials, no API,
nothing sent anywhere. Distances are computed against a home position; the
optional `location` extra lets CoreLocation supply that position, and without it
the plugin falls back to this Mac's own Find My record.

Tools: `where_is`, `who_is_home`, `list_subjects`.

#### netguard

Three sensors, because there are three jobs and they fail independently.
Everything is auto-detecting and degrades rather than failing: nmap if it is
installed, a rootless connect sweep if not; Suricata's alerts if Suricata is
running, its own heuristics if not; packet capture if the process is privileged
(install the `capture` extra for scapy), the ARP cache if not.

**Each sensor says which mode it is in, on the dashboard, because "quiet" and
"not looking" are very different states and a security tool that confuses them
is worse than none.**

Detects port scans, ARP spoofing, rogue DHCP servers, traffic to listed
addresses, beaconing, external IDS alerts, unrecognised devices joining, and
hardening-score regressions. Tools cover inventory (`list_devices`,
`investigate_host`, `scan_now`, `trust_device`, `forget_device`), connections
(`list_connections`, `check_destination`), blocking (`block_device`,
`confirm_block`, `unblock_device`, `list_blocks`) and `hardening_report`.

#### v380

A port of [Vasang123/camera-v380decoder](https://github.com/Vasang123/camera-v380decoder)
(C#/.NET) and [jericjan/v380-audio-player](https://github.com/jericjan/v380-audio-player)
(talkback), with **no .NET runtime and no per-camera subprocess** — one asyncio
session per camera inside Black Ice.

Credentials are a shared default (`V380_USERNAME`, `V380_PASSWORD`) with
per-camera overrides in `data/v380_cameras.json`, re-read on every scan.
Discovery finds cameras but cannot authenticate to them, so a camera with no
password is *listed* — you want to know it exists — and never connected to.
Live view is `rtsp://<host>:8554/<device_id>`, one path per camera, transcoded
to H.264 only while someone is actually watching.

Tools: `list_cameras`, `rescan`, `get_snapshot`, `ptz`, `set_light`, `speak`,
`play_sound`, `intercom`, `stop_speaking`, `set_image_mode`. See its
[README](plugins/blackice-plugin-v380/README.md).

#### watchers

Person detection and ByteTrack, ArcFace for faces, OSNet for appearance — so a
track whose face is never visible can still be matched, which is what answers
"the person who was at the drive is now at the door". Frames come from the v380
plugin, which publishes its live fleet for exactly this.

This is biometric machinery pointed at people in a home, including visitors, so
the model handling is deliberate:

```bash
uv pip install -e 'plugins/blackice-plugin-watchers[models]'   # the libraries
uv run blackice-watchers-provision                             # the weights
```

Weights arrive once, when you ask; they come from URLs written down in
`models.py` and overridable per model; they are SHA-256'd into
`data/watchers_models/manifest.lock.json` and verified on every later load; and
the libraries' own download paths are pointed at that directory and turned off.
A missing file means "no recognition, and here is why", never "go and get it".
The hashes are trust-on-first-use — verify the first fetch yourself if that
matters to you.

Until both steps are done the plugin starts, stays healthy, serves every widget
from its database, and says exactly which of the two is missing.

Tools: `list_enrolled`, `enrol_person`, `forget_person`, `rename_person`,
`who_was_seen`, `where_has_person_been`, `recognition_status`, `set_thresholds`.
See its [README](plugins/blackice-plugin-watchers/README.md).

### Writing a plugin

Subclass `SensorPlugin`, declare an entry point in the `blackice.plugins` group,
and return a `SensorDescriptor` from `describe()`. See
[plugins/blackice-plugin-heartbeat/](plugins/blackice-plugin-heartbeat/) for a
complete, minimal example.

Widgets are declared as JSON (`WidgetSpec`), not shipped as browser code. The
renderer registry lives in `dashboard/src/blackice/widgets.jsx`; an unrecognised
type renders a labelled fallback rather than a blank panel.

Plugins never reach through the core registry into each other. Where one plugin
genuinely feeds another — watchers consuming v380 frames — the producer
publishes an explicit interface for it.

## Layout

| Path | What it is |
|---|---|
| `blackice/services/` | The action layer. REST routes and LLM tools both call these, which is why every dashboard action is also reachable by voice. |
| `blackice/llm/` | LM Studio client, `normalize()`, PromptGuard, tool registry, harness loop |
| `blackice/triage/` | Three tiers: rules → small model → 27B |
| `blackice/plugins/` | Plugin contract, entry-point discovery, supervisor, per-plugin SQLite |
| `blackice/memory/`, `blackice/rsi/` | kokoro-memory wrapper, consolidation, and the feedback loop |
| `dashboard/src/blackice/` | Pages, widget registry, live socket, console, and `tokens.js` — every colour that carries meaning |
| `dashboard/public/assets/scss/` | The theme. Forked, so it is in git; the images and fonts beside it are not |
| `plugins/` | First-party plugins, installed with `uv pip install -e` |

## Configuration

Everything is environment variables, documented inline in
[.env.example](.env.example) — roughly 200 lines of settings with the reasoning
attached. `blackice/config.py` is the single reader. Nothing needs to be set for
the system to boot except `ADMIN_PASSWORD_HASH`.

## Auto-reload

`AUTO_RELOAD` (default on) restarts the service when core code, `schema.sql`, a
`*.prompt*` file, or an installed plugin changes. Plugin sources are resolved
through `find_spec`, so an editable install is watched where it actually lives
rather than wherever it was launched from. `data/` is explicitly excluded — the
SQLite WAL, the rotating log and captured media all change while the service
runs, and watching them restarts it in a loop.

Two costs worth knowing before leaving it on:

- **A broken save takes monitoring offline.** The reloader survives a syntax
  error but the app does not; the port stops answering until the file is valid
  again. It recovers on its own once it is.
- **With voice on, every save costs ~15-25s of deafness** while Whisper, Silero
  and Piper reload. The mic is closed for that whole window.

`./start.sh` honours the setting; `blackice serve --no-reload` overrides it for a
single run.

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
  as "it never heard me". It reuses the in-flight turn id — starting a new one
  would make the real answer stale and playback would discard it.
- **Cues only confirm being addressed** (`VOICE_CUE_MODE=wake`). voice2 chimes
  on every detected utterance and again when it starts thinking, both of which
  happen before anyone knows the speech was meant for it. `all` restores that,
  `off` leaves only the failure buzz.
- **It used to answer itself.** The speaker feeds the microphone, so a reply came
  back as a new utterance; inside the follow-up window that needs no wake word,
  so it looped. Worse, its own voice tripped barge-in at ~45x the noise baseline
  and cut each reply off after a word or two. Barge-in is therefore off by
  default (`VOICE_BARGE_IN`) — switch it on if you use headphones — and a
  transcript matching what was just said is discarded for `VOICE_ECHO_WINDOW_S`.
- **Spacebar interrupt is off** (`VOICE_KEYBOARD_INTERRUPT`). voice2's keyboard
  worker calls `tty.setraw()`, which clears ISIG so Ctrl-C never becomes SIGINT,
  and OPOST so console output walks diagonally down the screen. Barge-in by
  voice is unaffected. Turn it on only on a real terminal you don't mind that
  happening to.

## Memory

kokoro-memory holds durable facts under `KOKORO_MEMORY_ROOT`, and every
operation is mirrored into `memory_ops` for the audit trail. It is wired into the
loop at four points:

- **Read at the start of a session** — `build_startup_memory_block()` is appended
  to the system prompt, once per session.
- **Read during triage** — your past verdicts on a sensor and event kind are
  recalled and shown to both triage tiers as precedent. This is what makes
  marking something a false positive actually change the next classification.
- **Written from your verdicts** — judging an escalation writes a fact.
- **Written by consolidation** — conversation turns and structured event patterns
  become facts every `MEMORY_CONSOLIDATE_HOURS`, scheduled alongside the
  self-review. `blackice consolidate` runs it on demand.

Turn recording lives in the harness rather than at each call site, so console and
voice are both covered and a new channel cannot silently miss it.

## The daily self-review

Once every `RSI_REVIEW_HOURS` the primary model reads back what happened —
triage outcomes, escalations, and your verdicts on them — and may propose edits
to the triage prompt and to its own system prompt. Run it by hand with
`uv run blackice review`.

Nothing goes live because the model liked it. A candidate is stored as an
inactive version, then replayed over a golden set built from events *you have
judged*; it must agree with you at least as often as the prompt it would replace.
Only then does `RSI_SELF_EDIT_ENABLED` decide whether it activates or waits in
the review queue. Below `RSI_GOLDEN_SET_MIN` judged events the gate refuses to
rule at all, because it cannot tell two prompts apart on noise.

Every version keeps its parent, rationale, author and diff, and
`promptstore.rollback()` returns to the newest *human*-authored version rather
than stepping back one edit at a time.

**This is the riskiest part of the system.** A prompt that quietly stops
escalating is indistinguishable from a quiet week. The gate, the minimum golden
set and the default-off activation are what make it acceptable; keep
`RSI_SELF_EDIT_ENABLED=false` until you have judged enough escalations for the
gate to mean something.

## Logging

Standard `logging`, configured once in `blackice/logging_setup.py` and used by
every entry point. `LOG_LEVEL` and `LOG_FILE` control it; the file handler
rotates at 10 MB.

voice2 ships its own logger that calls `print()` directly, so it ignored levels,
handlers and the log file. `blackice/voice/voice2_logging.py` replaces that
function — its workers look it up as a module attribute, so one substitution
reaches all of them — and keeps the JSON-lines latency file it writes. Routine
per-turn chatter (`floor_set`, `transition`, `speak_entry`) drops to DEBUG;
`vad_load_error` and friends are ERROR.

## Tests

```bash
uv run pytest                  # unit and service tests, scripted models
uv run pytest -m integration   # the real models in LM Studio, slow
uv run ruff check .            # lint
```

The integration tests cover the parts scripted clients cannot: that the small
model actually escalates a break-in and absorbs routine motion, that it answers
without a thinking block, that the primary model produces a usable review of its
own prompts, and — the one that matters most — that the regression gate really
does refuse a prompt which stops escalating.

That last test seeds a golden set of judged events and scores a deliberately
crippled "always answer benign" prompt against the live one. Measured: the
shipped prompt agrees with the owner on 6 of 6, the crippled one on 3 of 6, and
it is held even with `RSI_SELF_EDIT_ENABLED=true`. The gate outranks the flag.

## The security model

**Trust is split by origin.** Text you type or say is a command channel: a
PromptGuard hit blocks it. Text a *sensor* supplies (OCR, device labels,
transcripts) is data: a hit wraps it as untrusted and delivers it anyway, because
discarding it would blind you to the event you wanted to see.

**Sensor text can never become a memory.** Consolidation builds facts from an
allow-list of structured fields plus your own conversation, never from
sensor-supplied strings — otherwise anyone who controls what a camera sees could
write persistent instructions into the assistant's long-term memory.
`tests/test_guard.py` and the memory-trust test guard both rules.

Found a vulnerability? Open an issue for anything that is already public, or
contact the maintainer directly for anything that is not.

## Contributing

Issues and pull requests are welcome.

- Python 3.13, `ruff` with the config in `pyproject.toml` (100 columns), and
  `uv run pytest` green before you open a PR.
- New sensors belong in a plugin, not in core. If the plugin contract cannot
  express what you need, that gap is itself worth an issue.
- Comments explain *why*, not *what* — the ones in this codebase mostly record a
  decision or a trap, and new ones should too.
- Do not add a runtime that is not already here. This is Python; C# and .NET
  reference implementations get ported, not shelled out to.

## License and credits

Apache License 2.0 — see [LICENSE](LICENSE).

Black Ice stands on a lot of other people's work:

- **[voice2](https://huggingface.co/AIIT-Threshold/voice2)** and
  **[kokoro-memory](https://huggingface.co/AIIT-Threshold/kokoro-memory)** — the
  voice loop and the durable-memory store.
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)**,
  **[Silero VAD](https://github.com/snakers4/silero-vad)** and
  **[Piper](https://github.com/rhasspy/piper)** — speech in and speech out.
- **[Llama Prompt Guard 2](https://huggingface.co/meta-llama/Llama-Prompt-Guard-2-86M)** —
  the injection classifier.
- **[camera-v380decoder](https://github.com/Vasang123/camera-v380decoder)** and
  **[v380-audio-player](https://github.com/jericjan/v380-audio-player)** — the
  V380 protocol work this plugin is a Python port of.
- **[Ultralytics YOLO11](https://github.com/ultralytics/ultralytics)**,
  **[insightface](https://github.com/deepinsight/insightface)** and
  **[BoxMOT](https://github.com/mikel-brostrom/boxmot)** — detection, faces and
  appearance re-identification. Note that Ultralytics is AGPL-3.0 and insightface
  models are non-commercial; the `models` extra is optional for that reason
  among others.
- **[FastAPI](https://fastapi.tiangolo.com/)**,
  **[LM Studio](https://lmstudio.ai/)**, **[uv](https://docs.astral.sh/uv/)** and
  **[Vite](https://vite.dev/)** — the plumbing.
