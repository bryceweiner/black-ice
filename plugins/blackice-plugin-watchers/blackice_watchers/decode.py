"""Annex B in, pictures out.

The fleet hands out access units, not images, and decoding is this plugin's
job. PyAV rather than an ffmpeg subprocess per camera, for three reasons:

* one code path covers H.264 and HEVC — the 3-lens cameras emit HEVC — where
  the subprocess route needs a different `-f` and a pipe per camera;
* frames arrive as numpy arrays directly, with no pipe, no raw-frame framing
  to parse, and no second process to supervise and reap;
* a subprocess that wedges is a zombie holding a pipe, while a `CodecContext`
  that gets confused is fixed by dropping it and waiting for a keyframe.

The cost is that decoding is a blocking C call on the calling thread, so every
entry point here is written to be run inside an executor, never on the loop.

A subscriber that falls behind loses frames (the fleet queue is drop-oldest),
which leaves the decoder missing references and producing green smears. So a
decoder that has seen a gap refuses to decode until the next keyframe, and says
so — a corrupt picture fed to a face model is worse than no picture.
"""

from __future__ import annotations

import fractions
import io
import logging

import av
import numpy as np

log = logging.getLogger("blackice.plugin.watchers.decode")

#: PyAV decoder names, keyed by what the V380 `Codec` enum calls the stream.
_DECODERS = {"h264": "h264", "h265": "hevc", "hevc": "hevc", "unknown": "h264"}


class AnnexBDecoder:
    """One camera's video decoder.

    Stateful, and not thread-safe: give each camera its own, and only ever
    touch it from the one worker thread that owns it.
    """

    def __init__(self, codec: str = "h264") -> None:
        self.codec_name = _DECODERS.get(str(codec).lower(), "h264")
        self._ctx: av.CodecContext | None = None
        self._need_keyframe = True
        self.decoded = 0
        self.errors = 0

    def _context(self) -> av.CodecContext:
        if self._ctx is None:
            self._ctx = av.CodecContext.create(self.codec_name, "r")
        return self._ctx

    def reset(self, codec: str | None = None) -> None:
        """Drop the decoder state. Used after a gap, and when a camera's codec
        turns out to differ from what we guessed."""
        if codec is not None:
            name = _DECODERS.get(str(codec).lower(), self.codec_name)
            if name != self.codec_name:
                self.codec_name = name
                self._ctx = None
        if self._ctx is not None:
            with _quiet():
                self._ctx.close()
            self._ctx = None
        self._need_keyframe = True

    def note_gap(self) -> None:
        """Told by the worker that the subscription dropped frames."""
        self._need_keyframe = True

    def decode(self, payload: bytes, *, keyframe: bool) -> list[np.ndarray]:
        """Pictures from one access unit, as RGB24 arrays.

        Usually zero or one. Returns an empty list rather than raising when the
        unit cannot be decoded: a camera that hiccups should cost a frame, not
        the worker.
        """
        if self._need_keyframe:
            if not keyframe:
                return []
            self._need_keyframe = False
            # The old context may hold references to frames that never
            # arrived; the cheapest way to be sure is to start again.
            if self._ctx is not None:
                with _quiet():
                    self._ctx.close()
                self._ctx = None

        try:
            packet = av.Packet(payload)
            frames = self._context().decode(packet)
        except Exception as exc:
            self.errors += 1
            log.debug("decode failed (%s); waiting for a keyframe: %s", self.codec_name, exc)
            self._need_keyframe = True
            self._ctx = None
            return []

        images = []
        for frame in frames:
            try:
                images.append(frame.to_ndarray(format="rgb24"))
            except Exception:  # pragma: no cover - malformed frame geometry
                self.errors += 1
        self.decoded += len(images)
        return images


class _quiet:
    """Swallow errors from tearing a decoder down. Closing a context that is
    already broken is normal here and is never worth propagating."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


def crop(image: np.ndarray, box: tuple[float, float, float, float],
         *, margin: float = 0.08) -> np.ndarray:
    """The part of the picture inside a box, with a little breathing room.

    Clamped to the image: detector boxes routinely run a few pixels outside the
    frame, and numpy would silently return an empty array rather than complain.
    """
    height, width = image.shape[:2]
    x1, y1, x2, y2 = box
    pad_x = (x2 - x1) * margin
    pad_y = (y2 - y1) * margin
    left = max(0, int(x1 - pad_x))
    top = max(0, int(y1 - pad_y))
    right = min(width, int(x2 + pad_x))
    bottom = min(height, int(y2 + pad_y))
    if right <= left or bottom <= top:
        return image[0:0, 0:0]
    return image[top:bottom, left:right]


def encode_jpeg(image: np.ndarray, quality: int = 85) -> bytes:
    """An RGB array as JPEG bytes.

    Through PyAV rather than Pillow so that decoding and encoding come from the
    one dependency this plugin already needs; adding an imaging library to
    write a thumbnail would be a second way to do the same thing.
    """
    if image.size == 0:
        return b""
    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format="mjpeg") as container:
        stream = container.add_stream("mjpeg", rate=fractions.Fraction(1, 1))
        stream.width = int(image.shape[1])
        stream.height = int(image.shape[0])
        stream.pix_fmt = "yuvj420p"
        # 1 is best and 31 is worst in ffmpeg's scale, which is the opposite of
        # every other quality setting anyone has ever met.
        stream.codec_context.qmin = stream.codec_context.qmax = max(
            2, min(31, int(round(31 - (quality / 100) * 29)))
        )
        frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(image), format="rgb24")
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return buffer.getvalue()


def decode_image_file(path: str) -> np.ndarray | None:
    """A JPEG or PNG on disk as an RGB array, for file enrolment."""
    try:
        with av.open(path) as container:
            for frame in container.decode(video=0):
                return frame.to_ndarray(format="rgb24")
    except Exception as exc:
        log.warning("could not read image %s: %s", path, exc)
    return None
