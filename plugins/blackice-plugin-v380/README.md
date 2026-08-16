# blackice-plugin-v380

Every V380 camera on the LAN, in pure Python — discovery, login, AES-decrypted
H.264/H.265 video, IMA-ADPCM audio, PTZ/light/image control, live RTSP, and
two-way audio through the camera's speaker.

A port of [Vasang123/camera-v380decoder][decoder] (C#/.NET) and
[jericjan/v380-audio-player][player] (talkback), with no .NET runtime and no
per-camera subprocess: one asyncio session per camera inside Black Ice.

[decoder]: https://github.com/Vasang123/camera-v380decoder
[player]: https://github.com/jericjan/v380-audio-player

## What it needs

`ffmpeg` on PATH, for snapshots and the H.265→H.264 transcode. `piper` and a
Piper voice, only if you want the `speak` tool. `cryptography` is installed as
a dependency.

## Configuration

Credentials are a shared default with per-camera overrides. Discovery finds
cameras but cannot authenticate to them, so a camera with no password is listed
— you want to know it exists — and never connected to.

```bash
V380_USERNAME=admin      # several models use the device id, not "admin"
V380_PASSWORD=secret     # applied to every camera found
```

`data/v380_cameras.json` overrides that per device id, and is re-read on every
scan, so editing it and asking the assistant to rescan is enough:

```json
{
  "95886601": {"password": "other", "label": "Front door", "quality": "sd"},
  "11112222": {"ip": "10.0.4.7", "port": 8800, "enabled": false}
}
```

Recognised keys: `username`, `password`, `label`, `ip`, `port`, `quality`
(`sd`/`hd`), `cloud`, `enabled`. See `.env.example` for the rest of the
settings — RTSP port, discovery interval, snapshot interval, offline threshold,
audio directory.

Only one Black Ice process streams. `blackice serve` and `blackice voice` both
load every plugin, so the first to take `data/v380.lock` owns the cameras and
the other runs read-only off the shared database.

## Live view

```
rtsp://<host>:8554/<device_id>
```

One listener, one path per camera. H.265 cameras are transcoded to H.264 on the
way out, and only while someone is actually watching.

## Feeding recognition models

This is the extension point for face recognition, ReID, and anything else that
wants frames. Subscribers get Annex B access units on a drop-oldest queue, so a
model that falls behind loses frames instead of stalling the camera.

`PluginContext` gives a plugin no way to reach another plugin, so this one
publishes its fleet instead. Ask for it by function — never by reaching through
the core registry:

```python
from blackice_v380 import wait_for_fleet

# Start order between plugins is not guaranteed, so wait rather than look once.
fleet = await wait_for_fleet()
if fleet is None:
    return          # this process does not own the cameras; do nothing

with fleet.subscribe("95886601", keyframes_only=True) as feed:
    async for frame in feed:
        # frame.payload is Annex B; frame.codec says h264 or h265
        await recognise(frame.payload)
```

`active_fleet()` is the non-waiting form. `None` is a normal answer, not an
error: in the process that did not take the camera lock it is the only answer.

A long-lived consumer should use `on_fleet_change(listener)` rather than hold
the object, because the supervisor can restart this plugin and build a new
`Fleet`. It fires immediately with the current value, and returns the function
that unsubscribes.

Frames are not decoded to images here on purpose: a face recogniser and a gait
model want different resolutions and cadences, and decoding once centrally
would serve neither. For a picture instead, `await fleet.snapshot(device_id)`
returns a JPEG.

## Tools

| Tool | Effect |
|---|---|
| `list_cameras`, `rescan` | Read-only |
| `get_snapshot` | Captures a still, attaches it to the timeline |
| `ptz`, `set_light`, `set_image_mode` | **Moves hardware / switches on a light** |
| `speak`, `play_sound`, `intercom` | **Audible in the room the camera watches** |
| `stop_speaking` | Silences the speaker |

Everything in bold emits an event, so what the assistant did to a camera is on
the timeline. `play_sound` can only reach files inside the audio directory.

## Not ported

* **ONVIF** (768 lines of SOAP in the original). It exists so third-party NVR
  software can discover the camera; nothing in Black Ice calls it. The RTSP URL
  above is what those clients actually need.
* **The C# web UI.** The Black Ice dashboard replaces it.

## Layout

| File | |
|---|---|
| `protocol.py` | Wire format: packets, framing, reassembly, AES |
| `codec.py` | Finding and classifying the video inside a frame |
| `audio.py` | IMA-ADPCM both ways, G.711 A-law |
| `client.py` | One camera session |
| `discovery.py` | UDP broadcast |
| `snapshot.py` | Keyframe → JPEG via ffmpeg |
| `talkback.py` | Audio to the speaker; TTS, clips, microphone |
| `rtsp.py` | RTSP server, RTP over TCP |
| `transcode.py` | H.265 → H.264 |
| `relay.py` | Cloud relay lookup |
| `fleet.py` | All cameras, and where frames go |
| `plugin.py` | The Black Ice sensor |
