# blackice-plugin-watchers

Recognises people on the V380 cameras and puts each recognition on the timeline.
Person detection and ByteTrack, ArcFace for faces, OSNet for appearance — so a
track whose face is never visible can still be matched, which is what answers
"the person who was at the drive is now at the door".

Frames come from `blackice-plugin-v380`, which publishes its live fleet for
exactly this. Nothing here reaches through the core registry.

## What it needs

Two steps, both explicit, neither of which happens at runtime:

```bash
uv pip install -e 'plugins/blackice-plugin-watchers[models]'   # the libraries
uv run blackice-watchers-provision                             # the weights
```

Or press **Download models** on the *Watchers pipeline* panel, which runs the
same code on a background task.

Until both are done the plugin starts, stays **healthy**, serves every widget
from its database, and says on the dashboard and through `recognition_status`
exactly which of the two is missing. That is also what it does in the process
that did not take `data/v380.lock`: there is no fleet there, so there is no
recognition there, and that is a normal answer rather than a failure.

The base install needs only `numpy` and `av`. The recognition stack is an extra
because ultralytics, insightface, and boxmot pin their own torch, and a failed
build of one of them must not stop Black Ice from booting.

## Models, and the promise about them

This is biometric machinery pointed at people in a home, including visitors, so:

* **Weights arrive once, when you ask.** Provisioning is a separate step. There
  is no lazy first-use download, and `models.py` is the only file that touches
  the network.
* **They come from URLs written down in `models.py`**, overridable per model
  with `WATCHERS_DETECTOR_URL`, `WATCHERS_FACE_URL`, `WATCHERS_REID_URL`.
* **They are hashed.** The first provision records the SHA-256 of every file
  into `data/watchers_models/manifest.lock.json`; every later provision and
  every load verifies against it. A file that changes underneath you is refused,
  not quietly loaded. Read the lock file and check the hashes against upstream
  if you want to.
* **The libraries' own download paths are pointed at that directory and turned
  off**, and the loaders are given absolute paths. A missing file means "no
  recognition, and here is why", never "go and get it".

The hashes are trust-on-first-use: this plugin was written without fetching the
weights, so nothing is hash-pinned in source. Verify the first fetch yourself if
that matters to you.

| Model | File | For |
|---|---|---|
| YOLO11n | `yolo11n.pt` | person detection |
| insightface buffalo_l | `buffalo_l.zip` | SCRFD face detection + ArcFace embedding |
| OSNet x0.25 (MSMT17) | `osnet_x0_25_msmt17.pt` | person appearance embedding |

`blackice-watchers-provision --status` reports what is present and verified and
downloads nothing.

## The pipeline

One worker per camera, on a drop-oldest subscription. Per camera:

1. Decode the Annex B access units with PyAV — one code path for H.264 and
   HEVC, no subprocess per camera, frames straight to numpy. A worker that has
   seen a gap waits for the next keyframe rather than feed a smeared picture to
   a face model.
2. Detect people, track them. **The track is the unit of identity**, not the
   frame.
3. Embed a face when one is large enough, and the body crop always.
4. Accumulate evidence; emit once when the track is called.

Decoding and inference run in a thread pool, never on the event loop. The plugin
keeps no queue of its own: if it falls behind, the fleet's queue drops the oldest
frames and the counters say so. Achieved frames per second and the drop count
are a widget, and a sustained shortfall raises an event and an alarm.

Re-emission is deliberate and rare: only a genuine change of mind (a ReID guess
corrected by a face) or a new track after the person left and came back.

## Events

Sensor `watchers.people`, kinds `recognition`, `recognition_revised`,
`person_lingering`, `enrolment`. Each carries the camera, the track, the
identity or the absence of one, the confidence, which modality decided it, and a
JPEG crop as a `MediaRef` under `data/media/watchers/`.

Severity:

| | day | night (22:00–06:00) |
|---|---|---|
| enrolled household member | `INFO` | `INFO` |
| …on a camera they have never been on | `LOW` | `LOW` |
| unrecognised | `LOW` | `MEDIUM` |
| unrecognised, still there after 60s | `MEDIUM` | `HIGH` |

`watchers.pipeline` carries `throughput` and `models` events.

**Trust.** An enrolled name is text you typed, and a camera label is text you
wrote in the V380 config file, so both may appear in `summary`. A camera with no
label falls back to its device id, which came off the network — so an unlabelled
camera is described rather than named, and every device id goes in
`sensor_text`. No model output of any kind reaches `summary`.

## Enrolment and privacy

Three ways in, all through `enrol_person`:

```
enrol_person(name="Jane", track="95886601:7")      # a track seen in the last hour
enrol_person(name="Jane", camera="Front door")     # take a picture now
enrol_person(name="Jane", files=["jane-1.jpg", …]) # from data/watchers_enrol/
```

Several photographs collapse into one prototype per modality, which beats
keeping the best single shot.

The plugin's database holds **embeddings, never images**: `people`,
`embeddings` (float32 blobs), `sightings`, a one-hour cache of recent track
vectors so a track can be enrolled from after the person has walked away, and
`prefs`. The only pictures kept are the crops attached to events, which the
existing media retention prunes.

`forget_person` deletes the person row and cascades the embeddings, and scrubs
the name from this plugin's sightings. It is a deletion, not a hidden flag.
Events already on the timeline keep what they said at the time — a plugin cannot
rewrite core tables, and the tool says so.

Nothing leaves the machine. No cloud APIs, no telemetry, and the only network
access in the plugin is provisioning.

## Tools

| Tool | |
|---|---|
| `list_enrolled` | Who is known, and when each was last seen |
| `enrol_person` | Learn someone from a track, a camera, or files |
| `forget_person` | **Deletes** the descriptors |
| `rename_person` | Keeps what was learned |
| `who_was_seen` | Recent sightings on one camera, or everywhere |
| `where_has_person_been` | One person's trail across cameras |
| `recognition_status` | What is loaded, what is keeping up, what to fix next |
| `set_thresholds` | Tune matching without a restart |

`provision_models` is reachable from the dashboard button but is deliberately
**not** offered to the assistant: fetching model weights over the network is the
owner's decision.

## Configuration

| | |
|---|---|
| `WATCHERS_ENABLED` | Turn recognition off without uninstalling |
| `WATCHERS_MODEL_DIR` | Default `data/watchers_models` |
| `WATCHERS_ENROL_DIR` | Default `data/watchers_enrol` |
| `WATCHERS_DEVICE` | `auto` (MPS, else CUDA, else CPU), or a device name |
| `WATCHERS_FACE_THRESHOLD` / `WATCHERS_REID_THRESHOLD` | 0.55 / 0.70 |
| `WATCHERS_MIN_FRAMES` | Agreeing frames before anyone is named. 3 |
| `WATCHERS_ANALYSE_FPS` | Frames per second per camera into the models. 5 |
| `WATCHERS_NIGHT_START` / `WATCHERS_NIGHT_END` | 22 / 6 |
| `WATCHERS_LINGER_SECONDS` | 60 |
| `WATCHERS_RECENT_WINDOW` | Cross-camera matching window. 600s |
| `WATCHERS_FPS_FLOOR` | Below this a camera is "not keeping up". 1.0 |
| `WATCHERS_WORKERS` | Threads for decode and inference. 4 |

Thresholds set with `set_thresholds` are stored in the plugin database and win
over the environment.

## Layout

| File | |
|---|---|
| `settings.py` | Environment, paths, thresholds, the night window |
| `embeddings.py` | Normalisation, cosine, the flat gallery index |
| `gallery.py` | Enrolled people, sightings, the recent-track cache |
| `decode.py` | Annex B → pictures; crops and JPEG |
| `recognition.py` | The `Recognizer` interface the pipeline is written against |
| `models.py` | The manifest, the lock file, and the real model stack |
| `provision.py` | The `blackice-watchers-provision` command |
| `tracks.py` | Evidence, debouncing, revision, cross-camera linking |
| `pipeline.py` | One worker per camera; fleet reconciliation |
| `plugin.py` | The Black Ice sensors, tools, widgets, and events |
