"""V380 audio: IMA-ADPCM decoding and G.711 A-law encoding.

Two device families, distinguished by `audio_bits` from the stream login:

* `audio_bits == 8` — classic G.711 A-law, already in the format everything
  downstream expects, and passed through untouched;
* `audio_bits == 16` — IMA-ADPCM in fixed 256-byte blocks. The 16 there is the
  ADPCM predictor width, not a PCM sample width, which is why treating it as
  raw 16-bit PCM produces noise. These blocks are decoded to linear PCM and
  re-encoded to A-law so that one SDP (`PCMA/8000/1`) describes both families.

The ADPCM tables and block layout are the standard IMA ones, cross-checked
against jericjan/v380-audio-player, which is how the codec was identified.
"""

from __future__ import annotations

ADPCM_BLOCK_LEN = 256
#: 4-byte block header, then 252 bytes of packed nibbles: 1 + 252 * 2 samples.
ADPCM_BLOCK_SAMPLES = 505
ADPCM_HEADER_LEN = 4

_INDEX_TABLE = (
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8,
)

_STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
    724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499,
    2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630,
    9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623,
    27086, 29794, 32767,
)

_MAX_INDEX = len(_STEP_TABLE) - 1
_PCM_CLIP = 32767


def decode_adpcm_block(block: bytes) -> list[int]:
    """Decode one IMA-ADPCM block to signed 16-bit PCM samples.

    Layout: initial predictor (int16 LE), initial step index, one reserved
    byte, then nibble pairs low-first.
    """
    if len(block) < ADPCM_HEADER_LEN:
        return []

    predicted = int.from_bytes(block[0:2], "little", signed=True)
    index = min(max(block[2], 0), _MAX_INDEX)
    step = _STEP_TABLE[index]

    samples = [predicted]
    for byte in block[ADPCM_HEADER_LEN:]:
        for nibble in (byte & 0xF, byte >> 4):
            diff = step >> 3
            if nibble & 4:
                diff += step
            if nibble & 2:
                diff += step >> 1
            if nibble & 1:
                diff += step >> 2

            predicted += -diff if nibble & 8 else diff
            predicted = min(max(predicted, -_PCM_CLIP), _PCM_CLIP)

            index = min(max(index + _INDEX_TABLE[nibble], 0), _MAX_INDEX)
            step = _STEP_TABLE[index]
            samples.append(predicted)
    return samples


_ALAW_SEGMENT_ENDS = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def _segment(value: int) -> int:
    for i, end in enumerate(_ALAW_SEGMENT_ENDS):
        if value <= end:
            return i
    return len(_ALAW_SEGMENT_ENDS)


def _linear_to_alaw(sample: int) -> int:
    """Standard ITU-T G.711 linear-to-A-law encoding of one sample."""
    value = sample >> 3
    if value >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        value = -value - 1

    seg = _segment(value)
    if seg >= 8:
        return 0x7F ^ mask

    aval = seg << 4
    aval |= (value >> 1) & 0xF if seg < 2 else (value >> seg) & 0xF
    return aval ^ mask


#: A-law byte for every possible 16-bit sample, built once. The alternative is
#: a Python-level encode of ~500 samples per audio frame, several times a
#: second, per camera.
_ALAW_TABLE = bytes(_linear_to_alaw(s if s < 32768 else s - 65536) for s in range(65536))


def pcm_to_alaw(samples: list[int]) -> bytes:
    return bytes(_ALAW_TABLE[s & 0xFFFF] for s in samples)


def adpcm_to_alaw(block: bytes) -> bytes:
    """One 256-byte ADPCM block straight to A-law."""
    return pcm_to_alaw(decode_adpcm_block(block))


#: Per-packet header length ahead of the audio payload. The newer ADPCM devices
#: use the same 16-byte frame header as video; the older G.711 ones prefix four
#: more bytes.
def audio_header_length(audio_bits: int) -> int:
    return 16 if audio_bits == 16 else 20


# --- encoding, for talking back --------------------------------------------

#: One block of linear PCM: 505 samples at 16 bits.
PCM_BLOCK_BYTES = ADPCM_BLOCK_SAMPLES * 2
PCM_RATE = 8000
PCM_WIDTH = 2
PCM_CHANNELS = 1
#: Wall-clock duration of one block, and therefore how often one must be sent.
BLOCK_SECONDS = ADPCM_BLOCK_SAMPLES / PCM_RATE


class AdpcmEncoder:
    """The inverse of `decode_adpcm_block`, for audio sent *to* a camera.

    Stateful across blocks in the same way the decoder is — the predictor and
    step index carry over — so one encoder belongs to one talkback session and
    must be reset between them.

    Ported from acida/pyima by way of jericjan/v380-audio-player, with one
    change: after writing the block header the predictor is set to the sample
    the header actually carries. The reference leaves it at whatever the header
    sample encoded to, which puts the encoder a step out from the decoder that
    will read it and adds a little distortion to every block. The wire format
    is identical either way.
    """

    def __init__(self) -> None:
        self.predicted = 0
        self.index = 0

    def reset(self) -> None:
        self.predicted = 0
        self.index = 0

    def _encode_sample(self, sample: int) -> int:
        delta = sample - self.predicted
        if delta >= 0:
            nibble = 0
        else:
            nibble = 8
            delta = -delta

        step = _STEP_TABLE[self.index]
        diff = step >> 3

        if delta > step:
            nibble |= 4
            delta -= step
            diff += step
        step >>= 1

        if delta > step:
            nibble |= 2
            delta -= step
            diff += step
        step >>= 1

        if delta > step:
            nibble |= 1
            diff += step

        self.predicted += -diff if nibble & 8 else diff
        self.predicted = min(max(self.predicted, -0x8000), 0x7FFF)

        self.index = min(max(self.index + _INDEX_TABLE[nibble & 7], 0), _MAX_INDEX)
        return nibble

    def encode_block(self, pcm: bytes) -> bytes:
        """One 1010-byte block of 8 kHz mono 16-bit PCM to a 256-byte block.

        Short input is zero-padded, which is what the tail of a clip needs.
        """
        if len(pcm) > PCM_BLOCK_BYTES:
            raise ValueError(
                f"a block holds {ADPCM_BLOCK_SAMPLES} samples, got {len(pcm) // 2}"
            )
        pcm = pcm.ljust(PCM_BLOCK_BYTES, b"\0")
        samples = memoryview(pcm).cast("h")

        # The header carries the first sample verbatim plus the step index it
        # produced; the decoder starts from exactly these.
        self._encode_sample(samples[0])
        out = bytearray(pcm[0:2])
        out.append(self.index)
        out.append(0)
        self.predicted = samples[0]

        for i in range(1, ADPCM_BLOCK_SAMPLES - 1, 2):
            low = self._encode_sample(samples[i])
            high = self._encode_sample(samples[i + 1])
            out.append((high << 4) | low)
        return bytes(out)


def blocks(pcm: bytes) -> list[bytes]:
    """Split PCM into whole encoder blocks, padding the last one."""
    return [
        pcm[i : i + PCM_BLOCK_BYTES] for i in range(0, len(pcm), PCM_BLOCK_BYTES)
    ]
