"""The V380 wire protocol: framing, login, and the crypto around both.

A Python port of the reverse-engineered protocol in Vasang123/camera-v380decoder
(itself descended from prsyahmi/v380). Everything here is pure byte handling
plus AES-128-ECB; nothing in this module talks to a socket, so it can be tested
against captured frames.

Two separate encryptions live here and they are unrelated:

* the *password* sent at login is AES-encrypted twice, once under a fixed
  vendor key and once under a random per-login key that travels in the clear
  alongside it (``build_password``);
* the *media* stream on devices with ``device_version > 30`` is encrypted under
  a key derived from the auth ticket (``media_key``), and video, audio, and the
  older "pre-2k" devices each scramble a different subset of the bytes.
"""

from __future__ import annotations

import secrets
import struct
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# --- Commands and results --------------------------------------------------

CMD_LOGIN = 1167
CMD_LOGIN_REPLY = 1168
CMD_STREAM_LOGIN = 301
CMD_STREAM_LOGIN_REPLY = 401
CMD_STREAM_START = 303

LOGIN_OK = 1001
LOGIN_BAD_USERNAME = 1011
LOGIN_BAD_PASSWORD = 1012
LOGIN_BAD_DEVICE_ID = 1018

#: Login results the camera will keep rejecting no matter how often we retry.
LOGIN_FATAL = {
    LOGIN_BAD_USERNAME: "invalid username",
    LOGIN_BAD_PASSWORD: "invalid password",
    LOGIN_BAD_DEVICE_ID: "invalid device id",
}

DEFAULT_PORT = 8800
QUALITY_SD = 0
QUALITY_HD = 1

#: Media is only encrypted above this device version.
ENCRYPTED_ABOVE_VERSION = 30
#: Communication version whose media uses the older whole-block scramble.
COMM_VERSION_PRE2K = 21

# --- Fragment framing ------------------------------------------------------

FRAGMENT_MAGIC = 0x7F
FRAGMENT_HEADER_LEN = 12
#: Fragment payloads larger than this mean we have lost sync with the stream.
MAX_FRAGMENT_PAYLOAD = 20000

#: Reassembled frames carry a 16-byte header: frame id, type, rate, timestamp.
FRAME_HEADER_LEN = 16

TYPE_KEYFRAME = 0x00
TYPE_AUDIO_RAW = 0x16
TYPE_AUDIO_TIMED = 0x1A
#: Keep-alive/telemetry fragments. Not media, and safe to drop.
TYPE_IGNORED = 0x5B

# --- Control payloads ------------------------------------------------------

#: 16-byte control words written straight to the streaming socket.
CONTROL: dict[str, bytes] = {
    "ptz_right": bytes([0xAA, 0, 0, 0, 0xE8, 3, 0xE8, 3, 0xEA, 3, 0xE8, 3, 0, 0, 1, 0]),
    "ptz_left": bytes([0xAA, 0, 0, 0, 0xE8, 3, 0xE8, 3, 0xE9, 3, 0xE8, 3, 0, 0, 1, 0]),
    "ptz_up": bytes([0xAA, 0, 0, 0, 0xE8, 3, 0xE8, 3, 0xE8, 3, 0xEB, 3, 0, 0, 1, 0]),
    "ptz_down": bytes([0xAA, 0, 0, 0, 0xE8, 3, 0xE8, 3, 0xE8, 3, 0xEC, 3, 0, 0, 1, 0]),
    "ptz_stop": bytes([0xAA, 0, 0, 0, 0xE8, 3, 0xE8, 3, 0xE8, 3, 0xE8, 3, 0, 0, 1, 0]),
    "light_on": bytes([0xC4, 0, 0, 0, 0xE9, 3, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]),
    "light_off": bytes([0xC4, 0, 0, 0, 0xEA, 3, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]),
    "light_auto": bytes([0xC4, 0, 0, 0, 0xEB, 3, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]),
    "image_color": bytes([0xC5, 0, 0, 0, 0xE9, 3, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]),
    "image_bw": bytes([0xC5, 0, 0, 0, 0xEA, 3, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]),
    "image_auto": bytes([0xC5, 0, 0, 0, 0xEB, 3, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]),
    "image_flip": bytes([0xBE, 0, 0, 0, 0xE8, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
}

PTZ_DIRECTIONS = ("up", "down", "left", "right", "stop")
LIGHT_MODES = ("on", "off", "auto")
IMAGE_MODES = ("color", "bw", "auto", "flip")

# --- Little-endian helpers -------------------------------------------------
#
# The protocol is little-endian throughout. These read out of a bytes-like and
# write into a bytearray, and are deliberately bounds-checked: a short or
# corrupt packet should raise here rather than silently decode as zeroes.


def u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def i32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<i", buf, off)[0]


def u64(buf: bytes, off: int) -> int:
    return struct.unpack_from("<Q", buf, off)[0]


def put_u16(buf: bytearray, off: int, value: int) -> None:
    struct.pack_into("<H", buf, off, value & 0xFFFF)


def put_u32(buf: bytearray, off: int, value: int) -> None:
    struct.pack_into("<I", buf, off, value & 0xFFFFFFFF)


def put_u64(buf: bytearray, off: int, value: int) -> None:
    struct.pack_into("<Q", buf, off, value & 0xFFFFFFFFFFFFFFFF)


def put_bytes(buf: bytearray, off: int, data: bytes, limit: int) -> None:
    """Copy at most `limit` bytes into a fixed-width field, truncating."""
    chunk = data[:limit]
    buf[off : off + len(chunk)] = chunk


# --- AES -------------------------------------------------------------------


def _ecb(key: bytes):
    return Cipher(algorithms.AES(key), modes.ECB())


def aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    enc = _ecb(key).encryptor()
    return enc.update(data) + enc.finalize()


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    dec = _ecb(key).decryptor()
    return dec.update(data) + dec.finalize()


#: Fixed vendor key, the first of the two password encryptions.
VENDOR_KEY = b"macrovideo+*#!^@"
_PASSWORD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
PASSWORD_FIELD_LEN = 64
_PASSWORD_BODY_LEN = 48


def build_password(password: str, random_key: bytes | None = None) -> bytes:
    """The 64-byte password field: a random 16-byte key, then the password
    encrypted under the vendor key and then under that random key.

    The random key is sent in the clear, so this is obfuscation rather than
    secrecy — but the camera checks it byte for byte, so it has to match.
    `random_key` exists so tests can pin the randomness.
    """
    if random_key is None:
        random_key = bytes(
            ord(secrets.choice(_PASSWORD_ALPHABET)) for _ in range(16)
        )
    if len(random_key) != 16:
        raise ValueError("random_key must be 16 bytes")

    body = password.encode("ascii", "ignore")[:_PASSWORD_BODY_LEN]
    body = body.ljust(_PASSWORD_BODY_LEN, b"\0")

    body = aes_ecb_encrypt(VENDOR_KEY, body)
    body = aes_ecb_encrypt(random_key, body)
    return random_key + body


#: Constants the camera folds into the media key alongside the auth ticket.
_MEDIA_KEY_MID = 0x618123462C14795C
_MEDIA_KEY_TAIL = 0x82800DF0


def media_key(auth_ticket: int) -> bytes:
    """Derive the 16-byte media AES key from the ticket the login handed back."""
    key = bytearray(16)
    put_u32(key, 0, auth_ticket)
    put_u64(key, 4, _MEDIA_KEY_MID)
    put_u32(key, 12, _MEDIA_KEY_TAIL)
    return bytes(key)


# --- Media decryption ------------------------------------------------------
#
# Three variants, differing only in which bytes are scrambled. All are ECB, so
# each contiguous run can go through OpenSSL in one call rather than block by
# block.

#: Video is encrypted in 64-byte runs spaced 80 bytes apart — the 16 bytes
#: between runs are left in the clear.
_VIDEO_RUN = 64
_VIDEO_STRIDE = 80


def decrypt_video(key: bytes, data: bytes) -> bytes:
    out = bytearray(data)
    for offset in range(0, max(len(data) - _VIDEO_RUN + 1, 0), _VIDEO_STRIDE):
        out[offset : offset + _VIDEO_RUN] = aes_ecb_decrypt(
            key, bytes(out[offset : offset + _VIDEO_RUN])
        )
    return bytes(out)


def decrypt_audio(key: bytes, data: bytes) -> bytes:
    """Audio scrambles every whole 16-byte block; any tail is left alone."""
    aligned = (len(data) // 16) * 16
    if aligned == 0:
        return data
    return aes_ecb_decrypt(key, data[:aligned]) + data[aligned:]


def decrypt_pre2k(key: bytes, data: bytes, mode: int = 1) -> bytes:
    """The `communication_version == 21` scramble.

    Untested against real hardware in the C# original too — kept faithful to it
    rather than guessed at. `mode == 0` caps the encrypted region at 2048 bytes.
    """
    length = 2048 if mode == 0 and len(data) > 2048 else (len(data) // 16) * 16
    if length == 0:
        return data
    return aes_ecb_decrypt(key, data[:length]) + data[length:]


# --- Login packets ---------------------------------------------------------

LOGIN_PACKET_LEN = 520
LOGIN_REPLY_LEN = 256
STREAM_LOGIN_PACKET_LEN = 256
STREAM_LOGIN_REPLY_LEN = 412
STREAM_START_PACKET_LEN = 256

#: Sent as `unknown2` in both login packets. The camera rejects other values.
_PROTOCOL_VERSION = 31
_LAN_MAGIC = 120
_CLOUD_MAGIC = 1022
#: Audio: 4096 disables it, 4097 asks the camera to include an audio track.
_AUDIO_ON = 4097


def cloud_hostname(device_id: int) -> str:
    return f"{device_id}.nvdvr.net"


def build_login(
    device_id: int,
    username: str,
    password: str,
    *,
    cloud: bool = False,
    port: int = DEFAULT_PORT,
    random_key: bytes | None = None,
) -> bytes:
    """Command 1167 — authenticate and collect a ticket."""
    pkt = bytearray(LOGIN_PACKET_LEN)
    user = username.encode("ascii", "ignore")
    secret = build_password(password, random_key)

    put_u32(pkt, 0, CMD_LOGIN)
    if not cloud:
        put_u32(pkt, 4, _LAN_MAGIC)
        pkt[8] = _PROTOCOL_VERSION
        put_u32(pkt, 9, 1)
        put_u32(pkt, 13, device_id)
        put_bytes(pkt, 49, user, 32)
        put_bytes(pkt, 81, secret, PASSWORD_FIELD_LEN)
    else:
        put_u32(pkt, 4, _CLOUD_MAGIC)
        pkt[8] = _PROTOCOL_VERSION
        put_u32(pkt, 9, 1)
        put_u32(pkt, 13, device_id)
        put_bytes(pkt, 17, cloud_hostname(device_id).encode("ascii"), 50)
        put_u32(pkt, 67, port)
        put_bytes(pkt, 71, user, 32)
        put_bytes(pkt, 103, secret, PASSWORD_FIELD_LEN)
    return bytes(pkt)


@dataclass(slots=True)
class LoginReply:
    result: int
    auth_ticket: int = 0
    session_id: int = 0
    device_version: int = 0
    device_type: int = 0
    cam_type: int = 0
    vendor_id: int = 0
    domain: str = ""

    @property
    def ok(self) -> bool:
        return self.result == LOGIN_OK

    @property
    def fatal(self) -> str | None:
        """The reason retrying is pointless, or None if it is worth retrying."""
        if self.ok:
            return None
        return LOGIN_FATAL.get(self.result, f"login failed with result {self.result}")

    @property
    def encrypted(self) -> bool:
        return self.device_version > ENCRYPTED_ABOVE_VERSION


def parse_login_reply(buf: bytes) -> LoginReply:
    """Command 1168. Raises ValueError if this is not a login reply at all."""
    if len(buf) < 26:
        raise ValueError(f"login reply too short: {len(buf)} bytes")
    cmd = u32(buf, 0)
    if cmd != CMD_LOGIN_REPLY:
        raise ValueError(f"expected command {CMD_LOGIN_REPLY}, got {cmd}")

    reply = LoginReply(result=u32(buf, 4))
    if not reply.ok:
        return reply

    reply.device_version = buf[12]
    reply.auth_ticket = u32(buf, 13)
    reply.session_id = u32(buf, 17)
    reply.device_type = buf[21]
    reply.cam_type = buf[22]
    reply.vendor_id = u16(buf, 23)
    if len(buf) >= 58:
        reply.domain = buf[26:58].split(b"\0", 1)[0].decode("ascii", "replace")
    return reply


def build_stream_login(
    device_id: int,
    auth_ticket: int,
    *,
    session_id: int = 0,
    quality: int = QUALITY_HD,
    cloud: bool = False,
    port: int = DEFAULT_PORT,
) -> bytes:
    """Command 301 — open the media socket using the ticket from login."""
    pkt = bytearray(STREAM_LOGIN_PACKET_LEN)
    put_u32(pkt, 0, CMD_STREAM_LOGIN)
    if not cloud:
        put_u32(pkt, 4, device_id)
        put_u32(pkt, 8, 0)
        put_u16(pkt, 12, 20)
        put_u32(pkt, 14, auth_ticket)
        put_u32(pkt, 22, _AUDIO_ON)
        put_u32(pkt, 26, quality)
    else:
        put_u32(pkt, 4, _CLOUD_MAGIC)
        put_bytes(pkt, 8, cloud_hostname(device_id).encode("ascii"), 50)
        put_u32(pkt, 58, port)
        put_u32(pkt, 62, device_id)
        put_u32(pkt, 66, auth_ticket)
        put_u32(pkt, 70, session_id)
        put_u32(pkt, 74, quality)
        pkt[78] = 20
        put_u32(pkt, 79, 1)
    return bytes(pkt)


@dataclass(slots=True)
class StreamLoginReply:
    result: int
    communication_version: int = 0
    width: int = 1280
    height: int = 720
    max_packet_size: int = 0
    audio_freq: int = 0
    audio_bits: int = 8
    audio_channels: int = 1

    @property
    def ok(self) -> bool:
        # -11 and -12 are the camera refusing the ticket; anything else it
        # returns here has historically been benign.
        return self.result not in (-11, -12)


def parse_stream_login_reply(buf: bytes, *, cloud: bool = False) -> StreamLoginReply:
    """Command 401. The media parameters are only present in LAN mode."""
    if len(buf) < 8:
        raise ValueError(f"stream login reply too short: {len(buf)} bytes")
    cmd = u32(buf, 0)
    if cmd != CMD_STREAM_LOGIN_REPLY:
        raise ValueError(f"expected command {CMD_STREAM_LOGIN_REPLY}, got {cmd}")

    reply = StreamLoginReply(result=i32(buf, 4))
    if not reply.ok or cloud or len(buf) < 25:
        return reply

    reply.communication_version = u16(buf, 8)
    reply.width = u32(buf, 10)
    reply.height = u32(buf, 14)
    reply.max_packet_size = u32(buf, 18)
    reply.audio_freq = buf[22]
    reply.audio_bits = buf[23]
    reply.audio_channels = buf[24]
    return reply


# --- Talkback (audio to the camera's speaker) ------------------------------
#
# A separate command family from the media stream, on its own socket: command
# 377 opens it, then 16-byte-headed blocks of encrypted IMA-ADPCM follow.
# Layouts from HSLiveDataV2Transmitter::sendSpeakAudioToDevice, by way of
# jericjan/v380-audio-player.

CMD_SPEAK = 377
SPEAK_HANDSHAKE_LEN = 85
#: Fixed prefix of every speak packet; the last byte is a wrapping counter.
_SPEAK_HEADER = bytes.fromhex("b40000000100160000000000000001")
SPEAK_HEADER_LEN = len(_SPEAK_HEADER) + 1


def build_speak_handshake(device_id: int, auth_ticket: int) -> bytes:
    """Command 377 — tell the camera to open its speaker for us."""
    pkt = bytearray(SPEAK_HANDSHAKE_LEN)
    put_u32(pkt, 0, CMD_SPEAK)
    put_u32(pkt, 4, device_id)
    put_u32(pkt, 8, auth_ticket)
    return bytes(pkt)


def build_speak_header(sequence: int) -> bytes:
    """The 16-byte header before each ADPCM block.

    The counter starts at 1 and wraps at 256; the camera uses it to notice a
    dropped packet, and rejects a stream whose numbering restarts.
    """
    return _SPEAK_HEADER + bytes([(sequence + 1) % 256])


def build_stream_start() -> bytes:
    """Command 303 — the camera stays silent until it receives this."""
    pkt = bytearray(STREAM_START_PACKET_LEN)
    put_u32(pkt, 0, CMD_STREAM_START)
    put_u16(pkt, 4, 0x3001)
    return bytes(pkt)


# --- Fragment reassembly ---------------------------------------------------


@dataclass(slots=True)
class FragmentHeader:
    type: int
    total: int
    index: int
    length: int

    @property
    def is_last(self) -> bool:
        return self.index == self.total - 1


def parse_fragment_header(buf: bytes) -> FragmentHeader | None:
    """Validate a 12-byte fragment header, or None if it is not one.

    Returning None rather than raising is deliberate: losing sync mid-stream is
    routine (the camera reboots, the Wi-Fi stutters), and the caller recovers by
    resyncing on the next magic byte, not by tearing down the session.
    """
    if len(buf) < FRAGMENT_HEADER_LEN or buf[0] != FRAGMENT_MAGIC:
        return None
    header = FragmentHeader(
        type=buf[1], total=u16(buf, 3), index=u16(buf, 5), length=u16(buf, 7)
    )
    if header.length == 0 or header.length > MAX_FRAGMENT_PAYLOAD:
        return None
    if header.total == 0 or header.index >= header.total:
        return None
    return header


@dataclass(slots=True)
class Reassembler:
    """Glues fragments back into whole media frames.

    The camera numbers fragments within a frame, so a gap means bytes were
    dropped and the partial frame is worthless. Rather than emit a corrupt
    frame we throw the whole thing away and restart at the next index 0.
    """

    parts: bytearray = field(default_factory=bytearray)
    total: int = 0
    next_index: int = 0
    frame_type: int = 0
    active: bool = False
    #: Frames abandoned mid-assembly. Surfaced as a health signal, not an error.
    resyncs: int = 0

    def push(self, header: FragmentHeader, payload: bytes) -> tuple[int, bytes] | None:
        """Feed one fragment. Returns (type, frame) once a frame completes."""
        in_sequence = (
            self.active
            and header.total == self.total
            and header.index == self.next_index
        )
        if not in_sequence:
            if header.index != 0:
                # Mid-frame: the fragments before this one are gone, so this
                # frame can never be completed. Drop everything until the next
                # frame starts rather than emitting a truncated one — a partial
                # access unit decodes to garbage, which is worse than a gap.
                if self.active:
                    self.resyncs += 1
                self.reset()
                return None
            self.parts.clear()
            self.total = header.total
            self.next_index = 0
            self.frame_type = header.type
            self.active = True

        self.parts += payload
        self.next_index = header.index + 1

        if not header.is_last:
            return None

        frame = bytes(self.parts)
        self.parts.clear()
        self.active = False
        # Anything this short cannot even hold the 16-byte frame header.
        if len(frame) < FRAME_HEADER_LEN:
            return None
        return self.frame_type, frame

    def reset(self) -> None:
        self.parts.clear()
        self.total = 0
        self.next_index = 0
        self.active = False


@dataclass(slots=True)
class FrameHeader:
    """The 16 bytes every reassembled media frame starts with."""

    frame_id: int
    frame_type: int
    frame_rate: int
    timestamp: int


def parse_frame_header(frame: bytes) -> FrameHeader:
    return FrameHeader(
        frame_id=u32(frame, 0),
        frame_type=u16(frame, 4),
        frame_rate=u16(frame, 6),
        timestamp=u64(frame, 8),
    )
