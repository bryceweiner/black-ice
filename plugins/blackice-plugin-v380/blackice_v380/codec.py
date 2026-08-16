"""Finding the video inside a V380 media frame.

The camera hands us a blob that is *nearly* Annex B: there is a variable amount
of junk before the first start code, the payload may or may not be encrypted,
and 3-lens H.265 models fragment differently from the older H.264 ones. So we
do not trust a header field to tell us where the video is — we generate the
plausible readings of the frame and score each on how much it looks like real
NAL units, which is what `pick_video_payload` does.

H.264 and H.265 read their NAL type out of *different bits of the same byte*
(`& 0x1F` versus `(>> 1) & 0x3F`), so no single check distinguishes them.
Wherever that ambiguity matters the resolution order is: unambiguous H.264 VCL
slices first, then the H.265 range, then the broad H.264 range as a fallback.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

START_CODE = b"\x00\x00\x00\x01"

# H.264 nal_unit_type
H264_NON_IDR = 1
H264_IDR = 5
H264_SPS = 7
H264_PPS = 8
H264_AUD = 9
#: Types at or above this are reserved/unspecified — a header claiming one is
#: a good sign we are looking at noise rather than video.
H264_TYPE_LIMIT = 24

# H.265 nal_unit_type
H265_IDR_W_RADL = 19
H265_IDR_N_LP = 20
H265_VPS = 32
H265_SPS = 33
H265_PPS = 34
H265_TYPE_LIMIT = 48

H264_KEY_TYPES = frozenset({H264_IDR, H264_SPS, H264_PPS})
H265_KEY_TYPES = frozenset({H265_IDR_W_RADL, H265_IDR_N_LP, H265_VPS, H265_SPS, H265_PPS})
H264_PARAMETER_SETS = frozenset({H264_SPS, H264_PPS})
H265_PARAMETER_SETS = frozenset({H265_VPS, H265_SPS, H265_PPS})
H264_VCL = frozenset({H264_NON_IDR, H264_IDR})
H265_IDR = frozenset({H265_IDR_W_RADL, H265_IDR_N_LP})

#: How far into a frame a leading start code may be before we give up. Real
#: frames have theirs within a couple of bytes; a match 500 bytes in is a
#: coincidence inside the picture data.
_START_CODE_SEARCH_LIMIT = 128


class Codec(StrEnum):
    UNKNOWN = "unknown"
    H264 = "h264"
    H265 = "h265"

    @property
    def ffmpeg_format(self) -> str:
        """What to pass to `ffmpeg -f`. Unknown streams are tried as H.264."""
        return "hevc" if self is Codec.H265 else "h264"


def h264_type(nal_header: int) -> int:
    return nal_header & 0x1F


def h265_type(nal_header: int) -> int:
    return (nal_header >> 1) & 0x3F


def is_plausible_nal_header(data: bytes, index: int) -> bool:
    """Whether the byte at `index` could be the start of a real NAL unit."""
    if index >= len(data):
        return False
    header = data[index]
    if 0 < h264_type(header) < H264_TYPE_LIMIT:
        return True
    return 0 < h265_type(header) < H265_TYPE_LIMIT


def find_start_code(data: bytes, start: int = 0) -> int:
    """Index of the next 3- or 4-byte start code, or -1."""
    i = start
    limit = len(data) - 3
    while i < limit:
        found = data.find(b"\x00\x00", i, limit)
        if found < 0:
            return -1
        if data[found + 2] == 1:
            return found
        if data[found + 2] == 0 and data[found + 3] == 1:
            return found
        i = found + 1
    return -1


def start_code_length(data: bytes, index: int) -> int:
    return 3 if data[index + 2] == 1 else 4


def find_leading_start_code(data: bytes) -> int:
    """Where the video actually begins, skipping any header junk.

    Only a start code followed by a plausible NAL header counts — three zero
    bytes and a one turn up by chance inside encrypted payloads.
    """
    limit = min(len(data) - 4, _START_CODE_SEARCH_LIMIT)
    for i in range(limit + 1):
        if data[i] or data[i + 1]:
            continue
        if data[i + 2] == 0 and data[i + 3] == 1 and is_plausible_nal_header(data, i + 4):
            return i
        if data[i + 2] == 1 and is_plausible_nal_header(data, i + 3):
            return i
    return -1


def iter_nals(data: bytes, codec: Codec = Codec.H264) -> Iterator[tuple[int, bytes]]:
    """Yield (nal_type, nal_bytes) pairs, start codes stripped."""
    i = 0
    length = len(data)
    while i < length:
        sc = find_start_code(data, i)
        if sc < 0:
            return
        nal_start = sc + start_code_length(data, sc)
        if nal_start >= length:
            return
        nxt = find_start_code(data, nal_start)
        nal_end = length if nxt < 0 else nxt
        header = data[nal_start]
        nal_type = h265_type(header) if codec is Codec.H265 else h264_type(header)
        yield nal_type, data[nal_start:nal_end]
        i = nal_end


def normalize(payload: bytes) -> bytes | None:
    """Trim junk before the first start code, and widen a 3-byte start code
    to 4 bytes so downstream consumers see uniform Annex B. None if there is no
    credible video in here at all.
    """
    start = find_leading_start_code(payload)
    if start < 0:
        return None
    if payload[start + 2] == 1:
        return b"\x00" + payload[start:]
    return payload[start:]


#: Scoring weights. Absolute values do not matter — only that a payload with
#: real parameter sets and IDR slices outranks one that merely contains a
#: start-code-shaped coincidence.
_SCORE_PER_NAL = 10
_SCORE_PARAMETER_SET = 50
_SCORE_IDR = 30
_SCORE_BAD_HEADER = -20
_BONUS_PER_NAL = 5
_BONUS_PER_PARAMETER_SET = 40
_BONUS_PER_IDR = 20
#: Enough NALs to tell signal from noise; scanning the whole frame is wasted work.
_SCORE_NAL_LIMIT = 12


def score(payload: bytes) -> int | None:
    """How much this looks like real video. None means "not video"."""
    total = 0
    nals = 0
    parameter_sets = 0
    idrs = 0

    i = 0
    while i < len(payload) - 4 and nals < _SCORE_NAL_LIMIT:
        sc = find_start_code(payload, i)
        if sc < 0:
            break
        nal_start = sc + start_code_length(payload, sc)
        if not is_plausible_nal_header(payload, nal_start):
            i = sc + 1
            total += _SCORE_BAD_HEADER
            continue

        nals += 1
        header = payload[nal_start]
        h264 = h264_type(header)
        h265 = h265_type(header)

        if h264 in H264_PARAMETER_SETS or h265 in H265_PARAMETER_SETS:
            parameter_sets += 1
            total += _SCORE_PARAMETER_SET
        if h264 == H264_IDR or h265 in H265_IDR:
            idrs += 1
            total += _SCORE_IDR

        total += _SCORE_PER_NAL
        i = nal_start + 1

    if nals == 0:
        return None
    return (
        total
        + nals * _BONUS_PER_NAL
        + parameter_sets * _BONUS_PER_PARAMETER_SET
        + idrs * _BONUS_PER_IDR
    )


def pick_video_payload(candidates: Iterator[bytes] | list[bytes]) -> bytes | None:
    """Choose the reading of a frame that looks most like video.

    Callers pass the decrypted and the plaintext interpretation of the same
    bytes; whichever scores higher is the real one. This is how the client
    copes with cameras whose encryption state we cannot know in advance.
    """
    best: bytes | None = None
    best_score: int | None = None
    for candidate in candidates:
        payload = normalize(candidate)
        if payload is None:
            continue
        value = score(payload)
        if value is None:
            continue
        if best_score is None or value > best_score:
            best_score = value
            best = payload
    return best


def detect_codec(payload: bytes) -> Codec:
    """Identify the codec from the first NAL header.

    Ordered most- to least-specific: real H.264 VCL slice types (1 and 5) are
    effectively unambiguous, so they settle it first; the H.265 range comes
    next, because the broad H.264 range below would otherwise swallow genuine
    HEVC; the broad H.264 range is the fallback for parameter sets, SEI, and AUD.
    """
    if len(payload) < 5:
        return Codec.UNKNOWN
    offset = 3 if payload[2] == 1 else 4
    if len(payload) <= offset:
        return Codec.UNKNOWN

    header = payload[offset]
    if h264_type(header) in H264_VCL:
        return Codec.H264
    if 0 < h265_type(header) < H265_TYPE_LIMIT:
        return Codec.H265
    if 0 < h264_type(header) < H264_TYPE_LIMIT:
        return Codec.H264
    return Codec.UNKNOWN


def is_keyframe(raw_type: int, frame_type: int, payload: bytes) -> bool:
    """Whether this frame can be decoded without any frame before it.

    Trusts the parsed NAL header over the camera's own frame-type field, which
    newer firmware sets inconsistently.
    """
    if raw_type == 0x00:
        return True
    offset = 4 if len(payload) >= 4 and payload[2] == 0 and payload[3] == 1 else 3
    if len(payload) <= offset:
        return False
    header = payload[offset]
    if h264_type(header) in H264_KEY_TYPES:
        return True
    if h265_type(header) in H265_KEY_TYPES:
        return True
    return frame_type == 0


@dataclass(slots=True)
class ParameterSets:
    """The VPS/SPS/PPS needed to make a keyframe independently decodable.

    The camera sends these once in a while rather than with every keyframe, so
    we cache the last of each and prepend them on the way to the decoder.
    """

    codec: Codec = Codec.UNKNOWN
    vps: bytes | None = None
    sps: bytes | None = None
    pps: bytes | None = None

    @property
    def complete(self) -> bool:
        return self.sps is not None and self.pps is not None

    def observe(self, payload: bytes) -> bool:
        """Harvest any parameter sets in this frame. True if anything changed.

        Scans under both codec readings, because the codec is not yet known the
        first time this runs.
        """
        changed = False
        for _, nal in iter_nals(payload, Codec.H264):
            if not nal:
                continue
            header = nal[0]
            h264, h265 = h264_type(header), h265_type(header)

            if h264 == H264_SPS:
                self.codec, self.sps, changed = Codec.H264, nal, True
            elif h264 == H264_PPS:
                self.codec, self.pps, changed = Codec.H264, nal, True
            elif h265 == H265_VPS:
                self.codec, self.vps, changed = Codec.H265, nal, True
            elif h265 == H265_SPS:
                self.codec, self.sps, changed = Codec.H265, nal, True
            elif h265 == H265_PPS:
                self.codec, self.pps, changed = Codec.H265, nal, True
        return changed

    def prepend(self, keyframe: bytes) -> bytes | None:
        """A self-contained access unit, or None if we have not seen the
        parameter sets yet."""
        if not self.complete:
            return None
        out = bytearray()
        if self.codec is Codec.H265 and self.vps is not None:
            out += START_CODE + self.vps
        out += START_CODE + self.sps
        out += START_CODE + self.pps
        out += keyframe
        return bytes(out)

    def clear(self) -> None:
        self.codec = Codec.UNKNOWN
        self.vps = self.sps = self.pps = None
