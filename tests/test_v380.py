"""The V380 plugin: the wire protocol, and the sensor built on it.

The protocol half is tested against a fake camera that speaks the real thing —
login, stream login, fragmented and AES-encrypted media — because a port of a
reverse-engineered protocol is worth nothing if it only satisfies its own
assumptions. The plugin half is tested through `Registry`, so every call goes
through the supervisor the way it will in production.
"""

from __future__ import annotations

import asyncio
import json
import math

import pytest
from blackice_v380 import (
    SENSOR_ID,
    Codec,
    Fleet,
    V380Client,
    V380Plugin,
    discovery,
    protocol,
    talkback,
)
from blackice_v380 import audio as v380_audio
from blackice_v380 import codec as v380_codec
from blackice_v380 import settings as v380_settings
from blackice_v380.client import AuthenticationError

from blackice import db
from blackice.llm.tools import ToolRegistry, project_plugin_tools
from blackice.plugins.registry import Registry
from blackice.services import events

DEVICE_ID = 95886601
PASSWORD = "s3cret"
USERNAME = "admin"

# --- a fake camera ---------------------------------------------------------

SPS = bytes([0x67, 0x42, 0x00, 0x1F, 0xE1, 0x00, 0x20])
PPS = bytes([0x68, 0xCE, 0x3C, 0x80])
IDR = bytes([0x65, 0x88, 0x84, 0x00]) + bytes(range(0, 200))
NON_IDR = bytes([0x61, 0x9A, 0x20]) + bytes(range(0, 120))


def annexb(*nals: bytes) -> bytes:
    return b"".join(b"\x00\x00\x00\x01" + n for n in nals)


def keyframe_payload() -> bytes:
    return annexb(SPS, PPS, IDR)


def encrypt_video(key: bytes, data: bytes) -> bytes:
    """The inverse of `protocol.decrypt_video`, so fixtures can be encrypted."""
    out = bytearray(data)
    run, stride = 64, 80
    for offset in range(0, max(len(data) - run + 1, 0), stride):
        out[offset : offset + run] = protocol.aes_ecb_encrypt(
            key, bytes(out[offset : offset + run])
        )
    return bytes(out)


def media_frame(payload: bytes, *, frame_id: int = 1, frame_type: int = 0) -> bytes:
    """A reassembled frame: the 16-byte header the camera prefixes, then media."""
    head = bytearray(protocol.FRAME_HEADER_LEN)
    protocol.put_u32(head, 0, frame_id)
    protocol.put_u16(head, 4, frame_type)
    protocol.put_u16(head, 6, 15)
    protocol.put_u64(head, 8, 1_700_000_000_000)
    return bytes(head) + payload


def fragments(frame: bytes, raw_type: int, *, chunk: int = 64) -> bytes:
    """Chop a frame into wire fragments the way the camera does."""
    pieces = [frame[i : i + chunk] for i in range(0, len(frame), chunk)] or [b""]
    out = bytearray()
    for index, piece in enumerate(pieces):
        header = bytearray(protocol.FRAGMENT_HEADER_LEN)
        header[0] = protocol.FRAGMENT_MAGIC
        header[1] = raw_type
        protocol.put_u16(header, 3, len(pieces))
        protocol.put_u16(header, 5, index)
        protocol.put_u16(header, 7, len(piece))
        out += header + piece
    return bytes(out)


class FakeCamera:
    """A V380 that answers the real handshake and streams one keyframe.

    `device_version` above 30 turns on media encryption, which is the path the
    3-lens cameras actually use.
    """

    def __init__(self, *, device_version: int = 31, login_result: int = protocol.LOGIN_OK):
        self.device_version = device_version
        self.login_result = login_result
        self.auth_ticket = 0x11223344
        self.server: asyncio.Server | None = None
        self.port = 0
        self.control_words: list[bytes] = []
        self.streaming = asyncio.Event()

    @property
    def key(self) -> bytes | None:
        if self.device_version <= protocol.ENCRYPTED_ABOVE_VERSION:
            return None
        return protocol.media_key(self.auth_ticket)

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is None:
            return
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            head = await reader.readexactly(4)
            command = protocol.u32(head, 0)
            if command == protocol.CMD_LOGIN:
                await self._login(reader, writer)
            elif command == protocol.CMD_STREAM_LOGIN:
                await self._stream(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _login(self, reader, writer) -> None:
        await reader.readexactly(protocol.LOGIN_PACKET_LEN - 4)
        reply = bytearray(protocol.LOGIN_REPLY_LEN)
        protocol.put_u32(reply, 0, protocol.CMD_LOGIN_REPLY)
        protocol.put_u32(reply, 4, self.login_result)
        reply[12] = self.device_version
        protocol.put_u32(reply, 13, self.auth_ticket)
        protocol.put_u32(reply, 17, 7)
        writer.write(bytes(reply))
        await writer.drain()

    async def _stream(self, reader, writer) -> None:
        await reader.readexactly(protocol.STREAM_LOGIN_PACKET_LEN - 4)
        reply = bytearray(protocol.STREAM_LOGIN_REPLY_LEN)
        protocol.put_u32(reply, 0, protocol.CMD_STREAM_LOGIN_REPLY)
        protocol.put_u32(reply, 4, 0)
        protocol.put_u16(reply, 8, 20)  # communication version
        protocol.put_u32(reply, 10, 1920)
        protocol.put_u32(reply, 14, 1080)
        protocol.put_u32(reply, 18, 20000)
        reply[22], reply[23], reply[24] = 8, 8, 1
        writer.write(bytes(reply))
        await writer.drain()

        # The camera stays silent until told to start.
        await reader.readexactly(protocol.STREAM_START_PACKET_LEN)
        self.streaming.set()

        payload = keyframe_payload()
        if self.key is not None:
            payload = encrypt_video(self.key, payload)
        writer.write(fragments(media_frame(payload), raw_type=0x01))
        await writer.drain()

        # Anything the client sends now is a control word.
        while True:
            word = await reader.readexactly(16)
            self.control_words.append(word)


@pytest.fixture
async def camera():
    cam = FakeCamera()
    await cam.start()
    yield cam
    await cam.stop()


async def collect_one_frame(cam: FakeCamera, **kwargs):
    """Run a client against the fake camera until it decodes a frame."""
    client = V380Client(
        "127.0.0.1", DEVICE_ID, USERNAME, PASSWORD, port=cam.port, **kwargs
    )
    got: asyncio.Future = asyncio.get_running_loop().create_future()
    client.add_video_sink(lambda f: None if got.done() else got.set_result(f))

    task = asyncio.create_task(client.run())
    try:
        frame = await asyncio.wait_for(got, timeout=10)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.close()
    return client, frame


# --- protocol: packets -----------------------------------------------------

def test_login_packet_has_the_layout_the_camera_expects():
    packet = protocol.build_login(DEVICE_ID, USERNAME, PASSWORD, random_key=b"K" * 16)

    assert len(packet) == protocol.LOGIN_PACKET_LEN
    assert protocol.u32(packet, 0) == protocol.CMD_LOGIN
    assert protocol.u32(packet, 13) == DEVICE_ID
    assert packet[49 : 49 + len(USERNAME)] == USERNAME.encode()
    assert packet[81:97] == b"K" * 16


def test_password_is_encrypted_under_the_vendor_key_then_the_random_key():
    field = protocol.build_password(PASSWORD, random_key=b"K" * 16)

    body = PASSWORD.encode().ljust(48, b"\0")
    expected = protocol.aes_ecb_encrypt(
        b"K" * 16, protocol.aes_ecb_encrypt(protocol.VENDOR_KEY, body)
    )
    assert field == b"K" * 16 + expected
    assert len(field) == protocol.PASSWORD_FIELD_LEN


def test_password_field_differs_every_login():
    assert protocol.build_password(PASSWORD) != protocol.build_password(PASSWORD)


def test_login_reply_reports_a_fatal_reason_for_a_bad_password():
    reply = bytearray(protocol.LOGIN_REPLY_LEN)
    protocol.put_u32(reply, 0, protocol.CMD_LOGIN_REPLY)
    protocol.put_u32(reply, 4, protocol.LOGIN_BAD_PASSWORD)

    parsed = protocol.parse_login_reply(bytes(reply))
    assert not parsed.ok
    assert parsed.fatal == "invalid password"


def test_login_reply_rejects_a_packet_that_is_not_one():
    with pytest.raises(ValueError, match="expected command"):
        protocol.parse_login_reply(bytes(protocol.LOGIN_REPLY_LEN))


def test_media_key_derives_from_the_auth_ticket():
    assert protocol.media_key(0xDEADBEEF).hex() == "efbeadde5c79142c46238161f00d8082"


def test_cloud_login_carries_the_relay_hostname():
    packet = protocol.build_login(DEVICE_ID, USERNAME, PASSWORD, cloud=True)
    assert packet[17:37].startswith(f"{DEVICE_ID}.nvdvr.net".encode())


# --- protocol: framing -----------------------------------------------------

def test_fragment_header_rejects_impossible_values():
    good = bytearray(protocol.FRAGMENT_HEADER_LEN)
    good[0] = protocol.FRAGMENT_MAGIC
    protocol.put_u16(good, 3, 2)
    protocol.put_u16(good, 5, 0)
    protocol.put_u16(good, 7, 100)
    assert protocol.parse_fragment_header(bytes(good)) is not None

    oversized = bytearray(good)
    protocol.put_u16(oversized, 7, protocol.MAX_FRAGMENT_PAYLOAD + 1)
    assert protocol.parse_fragment_header(bytes(oversized)) is None

    past_the_end = bytearray(good)
    protocol.put_u16(past_the_end, 5, 5)
    assert protocol.parse_fragment_header(bytes(past_the_end)) is None

    no_magic = bytearray(good)
    no_magic[0] = 0x00
    assert protocol.parse_fragment_header(bytes(no_magic)) is None


def _push_all(reassembler, wire: bytes):
    out = []
    offset = 0
    while offset < len(wire):
        header = protocol.parse_fragment_header(wire[offset : offset + 12])
        assert header is not None
        offset += 12
        completed = reassembler.push(header, wire[offset : offset + header.length])
        offset += header.length
        if completed is not None:
            out.append(completed)
    return out


def test_reassembly_joins_fragments_back_into_a_frame():
    frame = media_frame(keyframe_payload())
    completed = _push_all(protocol.Reassembler(), fragments(frame, raw_type=0x01))

    assert completed == [(0x01, frame)]


def test_reassembly_discards_a_frame_with_a_missing_fragment():
    frame = media_frame(keyframe_payload())
    wire = bytearray(fragments(frame, raw_type=0x01))

    # Drop the second fragment: header plus its 64-byte payload.
    del wire[12 + 64 : 2 * (12 + 64)]
    reassembler = protocol.Reassembler()

    assert _push_all(reassembler, bytes(wire)) == []
    assert reassembler.resyncs == 1


# --- protocol: crypto ------------------------------------------------------

def test_video_decryption_inverts_the_camera_encryption():
    key = protocol.media_key(0x11223344)
    plain = keyframe_payload()
    assert protocol.decrypt_video(key, encrypt_video(key, plain)) == plain


def test_audio_decryption_leaves_the_unaligned_tail_alone():
    key = protocol.media_key(1)
    data = bytes(range(40))
    assert protocol.decrypt_audio(key, data)[32:] == data[32:]


# --- codec -----------------------------------------------------------------

def test_start_code_search_ignores_a_coincidental_match():
    # 00 00 00 01 followed by a NAL type of 0, which is not a real unit.
    noise = b"\xff" * 8 + b"\x00\x00\x00\x01\x00" + b"\xff" * 8
    assert v380_codec.find_leading_start_code(noise) == -1


def test_normalize_trims_junk_and_widens_a_three_byte_start_code():
    payload = b"\xaa\xbb" + b"\x00\x00\x01" + PPS
    assert v380_codec.normalize(payload) == b"\x00\x00\x00\x01" + PPS


def test_the_decrypted_reading_outscores_the_encrypted_one():
    key = protocol.media_key(0x11223344)
    plain = keyframe_payload()
    cipher = encrypt_video(key, plain)

    chosen = v380_codec.pick_video_payload([protocol.decrypt_video(key, cipher), cipher])
    assert chosen == plain


def test_codec_detection_separates_h264_from_h265():
    assert v380_codec.detect_codec(keyframe_payload()) is Codec.H264
    # H.265 VPS: nal type 32 in bits 1-6 of the header byte.
    hevc = b"\x00\x00\x00\x01" + bytes([32 << 1, 0x01, 0x0C, 0x01])
    assert v380_codec.detect_codec(hevc) is Codec.H265


def test_keyframes_are_recognised_and_inter_frames_are_not():
    assert v380_codec.is_keyframe(0x01, 1, keyframe_payload())
    assert not v380_codec.is_keyframe(0x01, 1, annexb(NON_IDR))


def test_parameter_sets_make_a_keyframe_self_contained():
    sets = v380_codec.ParameterSets()
    assert sets.observe(keyframe_payload())
    assert sets.complete and sets.codec is Codec.H264

    unit = sets.prepend(annexb(IDR))
    assert unit is not None
    assert unit.count(b"\x00\x00\x00\x01") == 3


def test_parameter_sets_refuse_to_build_a_unit_before_they_are_known():
    assert v380_codec.ParameterSets().prepend(annexb(IDR)) is None


# --- audio -----------------------------------------------------------------

def test_adpcm_block_decodes_to_the_expected_number_of_samples():
    block = bytes([0x00, 0x00, 0x10, 0x00]) + bytes(range(256))[:252]
    assert len(v380_audio.decode_adpcm_block(block)) == v380_audio.ADPCM_BLOCK_SAMPLES


def test_alaw_encoding_matches_the_reference_values():
    # G.711 A-law: silence is 0xD5, and the encoding is sign-symmetric.
    assert v380_audio.pcm_to_alaw([0]) == b"\xd5"
    assert v380_audio.pcm_to_alaw([32767, -32767]) == b"\xaa\x2a"


def test_adpcm_devices_use_the_shorter_packet_header():
    assert v380_audio.audio_header_length(16) == 16
    assert v380_audio.audio_header_length(8) == 20


# --- discovery -------------------------------------------------------------

def test_discovery_reply_is_parsed_into_a_camera():
    reply = b"NVDEVRESULT^1^AA:BB:CC:DD:EE:FF^192.168.1.50^" + b"x^" * 8 + b"95886601"
    found = discovery.parse_reply(reply)

    assert found is not None
    assert (found.device_id, found.ip, found.mac) == (
        "95886601", "192.168.1.50", "AA:BB:CC:DD:EE:FF",
    )


@pytest.mark.parametrize(
    "payload",
    [b"NVDEVRESULT^too^few^fields", b"SOMETHINGELSE^" + b"x^" * 14, b"", b"\xff\xfe"],
)
def test_discovery_ignores_anything_that_is_not_a_camera(payload):
    assert discovery.parse_reply(payload) is None


# --- the client against a real handshake -----------------------------------

async def test_client_decodes_an_encrypted_keyframe_from_a_live_camera(camera):
    client, frame = await collect_one_frame(camera)

    assert frame.keyframe
    assert frame.codec is Codec.H264
    assert frame.payload == keyframe_payload()
    assert client.parameter_sets.complete
    assert client.stats.keyframes == 1


async def test_client_decodes_a_clear_stream_from_an_older_camera():
    cam = FakeCamera(device_version=20)
    await cam.start()
    try:
        _, frame = await collect_one_frame(cam)
        assert frame.payload == keyframe_payload()
    finally:
        await cam.stop()


async def test_client_reads_the_stream_geometry_from_the_handshake(camera):
    client, _ = await collect_one_frame(camera)
    assert (client.width, client.height) == (1920, 1080)
    assert client.device_version == 31


async def test_client_gives_up_on_a_rejected_password():
    cam = FakeCamera(login_result=protocol.LOGIN_BAD_PASSWORD)
    await cam.start()
    try:
        client = V380Client("127.0.0.1", DEVICE_ID, USERNAME, "wrong", port=cam.port)
        # run() swallows it and returns rather than retrying forever.
        await asyncio.wait_for(client.run(), timeout=10)
        assert client.stats.last_error == "invalid password"

        with pytest.raises(AuthenticationError):
            await client._authenticate()
    finally:
        await cam.stop()


async def test_control_words_reach_the_camera(camera):
    client = V380Client("127.0.0.1", DEVICE_ID, USERNAME, PASSWORD, port=camera.port)
    task = asyncio.create_task(client.run())
    try:
        assert await client.wait_connected(10)
        await asyncio.wait_for(camera.streaming.wait(), timeout=10)
        assert await client.send_control("ptz_left")

        for _ in range(50):
            if camera.control_words:
                break
            await asyncio.sleep(0.05)
        assert camera.control_words == [protocol.CONTROL["ptz_left"]]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.close()


async def test_control_before_connecting_is_reported_not_raised():
    client = V380Client("127.0.0.1", DEVICE_ID, USERNAME, PASSWORD, port=1)
    assert await client.send_control("ptz_up") is False
    with pytest.raises(KeyError):
        await client.send_control("self_destruct")


# --- talkback --------------------------------------------------------------

def silence(samples: int) -> bytes:
    return bytes(samples * 2)


def tone(samples: int, hz: float = 300.0, amplitude: int = 9000, phase: float = 0.0) -> bytes:
    """A sine at 8 kHz. Smooth, because ADPCM tracks a slope, not a jump —
    a sawtooth's wrap is beyond any correct encoder's slew rate and would make
    the round-trip assertion measure the signal rather than the codec."""
    out = bytearray()
    for i in range(samples):
        value = int(amplitude * math.sin(2 * math.pi * hz * i / 8000 + phase))
        out += value.to_bytes(2, "little", signed=True)
    return bytes(out)


def as_samples(pcm: bytes) -> list[int]:
    return [
        int.from_bytes(pcm[i : i + 2], "little", signed=True)
        for i in range(0, len(pcm), 2)
    ]


def test_encoded_block_is_the_size_the_camera_expects():
    block = v380_audio.AdpcmEncoder().encode_block(tone(v380_audio.ADPCM_BLOCK_SAMPLES))
    assert len(block) == v380_audio.ADPCM_BLOCK_LEN


def test_a_short_final_block_is_padded_rather_than_rejected():
    block = v380_audio.AdpcmEncoder().encode_block(tone(10))
    assert len(block) == v380_audio.ADPCM_BLOCK_LEN


def test_a_block_longer_than_one_frame_is_refused():
    with pytest.raises(ValueError, match="505 samples"):
        v380_audio.AdpcmEncoder().encode_block(tone(600))


def test_adpcm_round_trip_reconstructs_the_waveform():
    original = as_samples(tone(v380_audio.ADPCM_BLOCK_SAMPLES))
    decoded = v380_audio.decode_adpcm_block(
        v380_audio.AdpcmEncoder().encode_block(tone(v380_audio.ADPCM_BLOCK_SAMPLES))
    )

    assert len(decoded) == len(original)
    # ADPCM is lossy, so this is about tracking, not equality. The step size
    # starts small and has to ramp, which is why the opening samples carry
    # nearly all of the error and the mean is the meaningful figure.
    mean = sum(abs(a - b) for a, b in zip(original, decoded, strict=True)) / len(original)
    assert mean < 500


def test_the_encoder_tracks_a_signal_that_does_not_start_at_silence():
    """The block header states a predictor the decoder will start from, and the
    encoder has to agree with it. When it does not, error compounds across the
    block instead of staying bounded — audible as a rasp under the audio."""
    pcm = tone(v380_audio.ADPCM_BLOCK_SAMPLES * 3, phase=math.pi / 2)
    encoder = v380_audio.AdpcmEncoder()

    original = as_samples(pcm)
    decoded: list[int] = []
    for block in v380_audio.blocks(pcm):
        decoded += v380_audio.decode_adpcm_block(encoder.encode_block(block))

    mean = sum(abs(a - b) for a, b in zip(original, decoded, strict=True)) / len(original)
    assert mean < 500


def test_the_encoder_carries_state_between_blocks():
    encoder = v380_audio.AdpcmEncoder()
    pcm = tone(v380_audio.ADPCM_BLOCK_SAMPLES)
    first = encoder.encode_block(pcm)
    second = encoder.encode_block(pcm)

    assert first != second
    encoder.reset()
    assert encoder.encode_block(pcm) == first


def test_speak_handshake_carries_the_device_and_the_ticket():
    packet = protocol.build_speak_handshake(DEVICE_ID, 0x11223344)

    assert len(packet) == protocol.SPEAK_HANDSHAKE_LEN
    assert protocol.u32(packet, 0) == protocol.CMD_SPEAK
    assert protocol.u32(packet, 4) == DEVICE_ID
    assert protocol.u32(packet, 8) == 0x11223344


def test_speak_headers_count_from_one_and_wrap():
    assert protocol.build_speak_header(0)[-1] == 1
    assert protocol.build_speak_header(254)[-1] == 255
    assert protocol.build_speak_header(255)[-1] == 0
    assert len(protocol.build_speak_header(0)) == protocol.SPEAK_HEADER_LEN


class SpeakingCamera(FakeCamera):
    """A fake camera that accepts talkback and keeps what it was sent."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.speak_handshake: bytes | None = None
        self.speak_packets: list[bytes] = []
        self.heard = asyncio.Event()

    async def _handle(self, reader, writer):
        try:
            head = await reader.readexactly(4)
            command = protocol.u32(head, 0)
            if command == protocol.CMD_LOGIN:
                await self._login(reader, writer)
            elif command == protocol.CMD_STREAM_LOGIN:
                await self._stream(reader, writer)
            elif command == protocol.CMD_SPEAK:
                await self._speak(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _speak(self, reader, writer) -> None:
        self.speak_handshake = head = bytes(4) + await reader.readexactly(
            protocol.SPEAK_HANDSHAKE_LEN - 4
        )
        assert len(head) == protocol.SPEAK_HANDSHAKE_LEN
        while True:
            packet = await reader.readexactly(
                protocol.SPEAK_HEADER_LEN + v380_audio.ADPCM_BLOCK_LEN
            )
            self.speak_packets.append(packet)
            self.heard.set()


async def test_talkback_streams_encrypted_adpcm_to_the_camera():
    cam = SpeakingCamera()
    await cam.start()
    try:
        session = talkback.TalkbackSession(
            "127.0.0.1", DEVICE_ID, USERNAME, PASSWORD, port=cam.port
        )
        async with session:
            await session.play(talkback.from_pcm(tone(v380_audio.ADPCM_BLOCK_SAMPLES * 2)))

        assert cam.speak_handshake is not None
        assert protocol.u32(cam.speak_handshake, 8) == cam.auth_ticket

        # Two blocks of audio plus the trailing silence that flushes the buffer.
        assert len(cam.speak_packets) == 2 + talkback.TRAILING_SILENCE_BLOCKS
        assert [p[protocol.SPEAK_HEADER_LEN - 1] for p in cam.speak_packets[:3]] == [1, 2, 3]

        # Encrypted, because this fake reports device version 31.
        key = protocol.media_key(cam.auth_ticket)
        block = cam.speak_packets[0][protocol.SPEAK_HEADER_LEN :]
        assert len(protocol.aes_ecb_decrypt(key, block)) == v380_audio.ADPCM_BLOCK_LEN
    finally:
        await cam.stop()


async def test_talkback_on_an_older_camera_is_sent_in_the_clear():
    cam = SpeakingCamera(device_version=20)
    await cam.start()
    try:
        async with talkback.TalkbackSession(
            "127.0.0.1", DEVICE_ID, USERNAME, PASSWORD, port=cam.port
        ) as session:
            await session.play(talkback.from_pcm(tone(v380_audio.ADPCM_BLOCK_SAMPLES)))

        sent = cam.speak_packets[0][protocol.SPEAK_HEADER_LEN :]
        expected = v380_audio.AdpcmEncoder().encode_block(
            tone(v380_audio.ADPCM_BLOCK_SAMPLES)
        )
        assert sent == expected
    finally:
        await cam.stop()


async def test_talkback_refuses_to_play_before_it_is_open():
    session = talkback.TalkbackSession("127.0.0.1", DEVICE_ID, USERNAME, PASSWORD, port=1)
    with pytest.raises(talkback.TalkbackError, match="not open"):
        await session.play(talkback.from_pcm(silence(100)))


async def test_talkback_gives_up_on_a_rejected_password():
    cam = SpeakingCamera(login_result=protocol.LOGIN_BAD_PASSWORD)
    await cam.start()
    try:
        session = talkback.TalkbackSession(
            "127.0.0.1", DEVICE_ID, USERNAME, "wrong", port=cam.port
        )
        with pytest.raises(AuthenticationError):
            await session.open()
    finally:
        await cam.stop()


async def test_a_clip_name_cannot_escape_the_audio_directory(monkeypatch, tmp_path):
    audio = tmp_path / "audio"
    audio.mkdir()
    (audio / "siren.wav").write_bytes(b"RIFF")
    (tmp_path / "secret.txt").write_text("no")
    monkeypatch.setenv(v380_settings.ENV_AUDIO_DIR, str(audio))

    assert v380_settings.resolve_clip("siren.wav") is not None
    assert v380_settings.resolve_clip("../secret.txt") is None
    assert v380_settings.resolve_clip("/etc/passwd") is None
    assert v380_settings.resolve_clip("nope.wav") is None
    assert v380_settings.list_clips() == ["siren.wav"]


# --- settings --------------------------------------------------------------

def test_environment_password_applies_to_every_discovered_camera(monkeypatch, tmp_path):
    monkeypatch.setenv(v380_settings.ENV_USERNAME, "admin")
    monkeypatch.setenv(v380_settings.ENV_PASSWORD, "shared")
    monkeypatch.setenv(v380_settings.ENV_CAMERAS_FILE, str(tmp_path / "none.json"))

    config = v380_settings.load().camera_for("95886601", ip="192.168.1.50")
    assert config.password == "shared"
    assert config.configured


def test_a_file_entry_overrides_the_shared_credential(monkeypatch, tmp_path):
    path = tmp_path / "cameras.json"
    path.write_text(json.dumps({
        "95886601": {"password": "specific", "label": "Front door", "quality": "sd"},
    }))
    monkeypatch.setenv(v380_settings.ENV_PASSWORD, "shared")
    monkeypatch.setenv(v380_settings.ENV_CAMERAS_FILE, str(path))

    settings = v380_settings.load()
    front = settings.camera_for("95886601", ip="10.0.0.5")
    other = settings.camera_for("11112222", ip="10.0.0.6")

    assert (front.password, front.label) == ("specific", "Front door")
    assert front.quality == protocol.QUALITY_SD
    assert other.password == "shared"
    assert other.display_name == "11112222"


def test_a_camera_without_a_password_is_listed_but_not_connected(monkeypatch, tmp_path):
    monkeypatch.delenv(v380_settings.ENV_PASSWORD, raising=False)
    monkeypatch.setenv(v380_settings.ENV_CAMERAS_FILE, str(tmp_path / "none.json"))

    config = v380_settings.load().camera_for("95886601", ip="192.168.1.50")
    assert not config.configured
    assert "no password" in config.reason_unconfigured


def test_a_broken_config_file_degrades_to_no_overrides(monkeypatch, tmp_path):
    path = tmp_path / "cameras.json"
    path.write_text("{ this is not json")
    monkeypatch.setenv(v380_settings.ENV_CAMERAS_FILE, str(path))

    assert v380_settings.load().overrides == {}


# --- the fleet -------------------------------------------------------------

@pytest.fixture
def quiet_fleet_env(monkeypatch, tmp_path):
    """A fleet that neither broadcasts nor binds a port."""
    monkeypatch.setenv(v380_settings.ENV_DISCOVERY_ENABLED, "false")
    monkeypatch.setenv(v380_settings.ENV_RTSP_ENABLED, "false")
    monkeypatch.setenv(v380_settings.ENV_CAMERAS_FILE, str(tmp_path / "cameras.json"))
    monkeypatch.delenv(v380_settings.ENV_PASSWORD, raising=False)


async def test_fleet_connects_a_configured_camera_and_reports_it_online(
    camera, monkeypatch, tmp_path
):
    path = tmp_path / "cameras.json"
    path.write_text(json.dumps({
        str(DEVICE_ID): {
            "password": PASSWORD, "ip": "127.0.0.1",
            "port": camera.port, "label": "Front door",
        }
    }))
    monkeypatch.setenv(v380_settings.ENV_CAMERAS_FILE, str(path))
    monkeypatch.setenv(v380_settings.ENV_DISCOVERY_ENABLED, "false")
    monkeypatch.setenv(v380_settings.ENV_RTSP_ENABLED, "false")

    changes = []
    fleet = Fleet(v380_settings.load(), on_state_change=changes.append)
    await fleet.start()
    try:
        for _ in range(100):
            if fleet.cameras[str(DEVICE_ID)].online:
                break
            await asyncio.sleep(0.05)

        entry = fleet.cameras[str(DEVICE_ID)]
        assert entry.online
        assert entry.label == "Front door"
        assert entry.codec is Codec.H264
        assert [c.current for c in changes][-1] == "online"

        # A subscriber sees frames without touching the protocol layer.
        with fleet.subscribe(str(DEVICE_ID)) as feed:
            assert feed.camera is entry
    finally:
        await fleet.stop()


async def test_fleet_lists_an_unconfigured_camera_without_connecting(quiet_fleet_env):
    fleet = Fleet(v380_settings.load())
    fleet._ensure(fleet.settings.camera_for("95886601", ip="192.168.9.9"))
    fleet._sync_sessions()

    summary = fleet.summaries()[0]
    assert summary["state"] == "unconfigured"
    assert fleet.cameras["95886601"].task is None


async def test_a_rejected_password_is_not_retried_on_every_scan(monkeypatch, tmp_path):
    """Hammering a camera with a password it already refused can lock the
    account, so a fatal rejection parks the camera until its config changes."""
    cam = FakeCamera(login_result=protocol.LOGIN_BAD_PASSWORD)
    await cam.start()
    path = tmp_path / "cameras.json"
    path.write_text(json.dumps({
        str(DEVICE_ID): {"password": "wrong", "ip": "127.0.0.1", "port": cam.port}
    }))
    monkeypatch.setenv(v380_settings.ENV_CAMERAS_FILE, str(path))
    monkeypatch.setenv(v380_settings.ENV_DISCOVERY_ENABLED, "false")
    monkeypatch.setenv(v380_settings.ENV_RTSP_ENABLED, "false")

    fleet = Fleet(v380_settings.load())
    await fleet.start()
    try:
        entry = fleet.cameras[str(DEVICE_ID)]
        for _ in range(100):
            if entry.fatal:
                break
            await asyncio.sleep(0.05)
        assert entry.fatal
        assert entry.state == "error"

        fleet._sync_sessions()
        assert entry.task is None or entry.task.done()

        # Correcting the file clears it, so a fixed password is picked up.
        path.write_text(json.dumps({
            str(DEVICE_ID): {"password": PASSWORD, "ip": "127.0.0.1", "port": cam.port}
        }))
        await fleet.scan()
        assert not entry.fatal
    finally:
        await fleet.stop()
        await cam.stop()


async def test_subscribing_to_an_unknown_camera_raises(quiet_fleet_env):
    with pytest.raises(KeyError):
        Fleet(v380_settings.load()).subscribe("nope")


async def test_a_slow_subscriber_drops_frames_instead_of_blocking(quiet_fleet_env):
    fleet = Fleet(v380_settings.load())
    fleet._ensure(fleet.settings.camera_for("1", ip="10.0.0.1"))
    feed = fleet.subscribe("1", depth=1)

    from blackice_v380.client import VideoFrame

    def frame(n):
        return VideoFrame(
            payload=b"", codec=Codec.H264, keyframe=True,
            frame_id=n, timestamp=n, frame_rate=15, received_at=0.0,
        )

    for n in range(5):
        feed.offer(frame(n))

    assert feed.dropped == 4
    assert (await feed.get()).frame_id == 4


# --- the plugin, through the supervisor ------------------------------------

@pytest.fixture
async def reg(data_dir, monkeypatch):
    monkeypatch.setenv(v380_settings.ENV_DISCOVERY_ENABLED, "false")
    monkeypatch.setenv(v380_settings.ENV_RTSP_ENABLED, "false")
    monkeypatch.setenv(v380_settings.ENV_CAMERAS_FILE, str(data_dir / "cameras.json"))
    monkeypatch.delenv(v380_settings.ENV_PASSWORD, raising=False)

    r = Registry()
    await r.start_plugin(V380Plugin, events.record)
    yield r
    await r.stop_all()


def healthy(reg) -> bool:
    return reg.supervisors["v380"].health()["state"] == "healthy"


async def test_discovery_finds_the_installed_plugin(data_dir):
    assert "v380" in [c.name for c in Registry().discover()]


async def test_start_projects_the_sensor_and_its_alarm_rules(reg, data_dir):
    sensors = [r["id"] for r in await db.fetchall("SELECT id FROM sensors")]
    assert SENSOR_ID in sensors

    rules = [
        r["key"]
        for r in await db.fetchall("SELECT key FROM alarm_rules WHERE plugin = 'v380'")
    ]
    assert {"camera_offline", "new_camera"} <= set(rules)


async def test_both_alarm_rules_are_armed_by_default(reg, data_dir):
    armed = await db.fetchall(
        """SELECT r.key, s.armed FROM alarm_rules r
           JOIN alarm_state s ON s.rule_id = r.id WHERE r.plugin = 'v380'"""
    )
    assert all(row["armed"] for row in armed)


async def test_tools_reach_the_llm_with_the_plugin_prefix(reg, data_dir):
    registry = ToolRegistry()
    project_plugin_tools(reg, registry)
    names = set(registry.tools)

    assert {
        "v380.list_cameras", "v380.rescan", "v380.get_snapshot",
        "v380.ptz", "v380.set_light", "v380.set_image_mode",
        "v380.speak", "v380.play_sound", "v380.intercom", "v380.stop_speaking",
    } <= names


async def test_list_cameras_answers_even_with_nothing_on_the_network(reg):
    result = await reg.command("v380", "list_cameras")
    assert result["cameras"] == []
    assert result["live"] is True


async def test_rescan_reports_that_discovery_is_switched_off(reg):
    result = await reg.command("v380", "rescan")
    assert result["found"] == 0
    assert healthy(reg)


@pytest.mark.parametrize(
    ("cmd", "kwargs"),
    [
        ("get_snapshot", {"camera": "nonexistent"}),
        ("ptz", {"camera": "nonexistent", "direction": "left"}),
        ("set_light", {"camera": "nonexistent", "mode": "on"}),
        ("set_image_mode", {"camera": "nonexistent", "mode": "bw"}),
        ("speak", {"camera": "nonexistent", "text": "hello"}),
        ("stop_speaking", {"camera": "nonexistent"}),
    ],
)
async def test_an_unknown_camera_is_an_error_not_a_plugin_failure(reg, cmd, kwargs):
    result = await reg.command("v380", cmd, **kwargs)

    assert "no camera called" in result["error"]
    assert healthy(reg)


@pytest.mark.parametrize(
    ("cmd", "kwargs"),
    [
        ("ptz", {"camera": "x", "direction": "sideways"}),
        ("set_light", {"camera": "x", "mode": "flashing"}),
        ("set_image_mode", {"camera": "x", "mode": "sepia"}),
    ],
)
async def test_an_invalid_action_is_rejected_before_any_camera_lookup(reg, cmd, kwargs):
    result = await reg.command("v380", cmd, **kwargs)

    assert "is not one of" in result["error"]
    assert healthy(reg)


async def test_a_missing_camera_argument_is_an_error_not_a_crash(reg):
    result = await reg.command("v380", "get_snapshot")

    assert "which camera" in result["error"].lower()
    assert healthy(reg)


@pytest.mark.parametrize(
    "source",
    ["online_count", "fleet_status", "cameras", "latest_snapshot",
     "snapshot_gallery", "recent_changes"],
)
async def test_every_declared_widget_source_returns_data(reg, source):
    declared = {w.data_source for w in reg.descriptors()[0].widgets}
    assert source in declared

    result = await reg.query("v380", source)
    assert result is not None
    assert healthy(reg)


async def test_an_unknown_widget_source_leaves_the_plugin_healthy(reg):
    with pytest.raises(Exception, match="data source"):
        await reg.query("v380", "not_a_widget")


async def test_speaking_with_nothing_to_say_is_an_error(reg):
    result = await reg.command("v380", "speak", camera="x", text="   ")

    assert "nothing to say" in result["error"]
    assert healthy(reg)


async def test_play_sound_without_a_clip_lists_what_is_available(reg, monkeypatch, tmp_path):
    audio = tmp_path / "audio"
    audio.mkdir()
    (audio / "siren.wav").write_bytes(b"RIFF")
    monkeypatch.setenv(v380_settings.ENV_AUDIO_DIR, str(audio))

    result = await reg.command("v380", "play_sound", camera="x")

    assert result["clips"] == ["siren.wav"]
    assert healthy(reg)


async def test_intercom_rejects_a_nonsense_duration(reg):
    assert "not a number" in (
        await reg.command("v380", "intercom", camera="x", seconds="soon")
    )["error"]
    assert "positive time" in (
        await reg.command("v380", "intercom", camera="x", seconds=-5)
    )["error"]
    assert healthy(reg)


async def test_talkback_through_the_fleet_reaches_the_camera(monkeypatch, tmp_path):
    cam = SpeakingCamera()
    await cam.start()
    path = tmp_path / "cameras.json"
    path.write_text(json.dumps({
        str(DEVICE_ID): {
            "password": PASSWORD, "ip": "127.0.0.1",
            "port": cam.port, "label": "Front door",
        }
    }))
    monkeypatch.setenv(v380_settings.ENV_CAMERAS_FILE, str(path))
    monkeypatch.setenv(v380_settings.ENV_DISCOVERY_ENABLED, "false")
    monkeypatch.setenv(v380_settings.ENV_RTSP_ENABLED, "false")

    fleet = Fleet(v380_settings.load())
    await fleet.start()
    try:
        # A long clip, so it is still playing when we ask it to stop.
        pcm = tone(v380_audio.ADPCM_BLOCK_SAMPLES * 60)
        await fleet.speak(str(DEVICE_ID), talkback.from_pcm(pcm))
        assert fleet.speaking(str(DEVICE_ID))

        await asyncio.wait_for(cam.heard.wait(), timeout=5)
        assert await fleet.stop_speaking(str(DEVICE_ID))
        assert not fleet.speaking(str(DEVICE_ID))
    finally:
        await fleet.stop()
        await cam.stop()


async def test_talking_to_an_unconfigured_camera_is_refused(quiet_fleet_env):
    fleet = Fleet(v380_settings.load())
    fleet._ensure(fleet.settings.camera_for("95886601", ip="192.168.9.9"))

    with pytest.raises(talkback.TalkbackError, match="no password"):
        await fleet.speak("95886601", talkback.from_pcm(silence(100)))


async def test_a_second_process_runs_read_only(data_dir, monkeypatch):
    """Two Black Ice processes must not both stream from the cameras."""
    monkeypatch.setenv(v380_settings.ENV_DISCOVERY_ENABLED, "false")
    monkeypatch.setenv(v380_settings.ENV_RTSP_ENABLED, "false")

    first, second = Registry(), Registry()
    await first.start_plugin(V380Plugin, events.record)
    try:
        await second.start_plugin(V380Plugin, events.record)
        owner = first.supervisors["v380"].plugin
        passenger = second.supervisors["v380"].plugin

        assert owner.active and owner.fleet is not None
        assert not passenger.active and passenger.fleet is None

        # The passenger still answers, and says why it cannot act.
        result = await second.command("v380", "ptz", camera="x", direction="left")
        assert "another Black Ice process" in result["error"]
        assert second.supervisors["v380"].health()["state"] == "healthy"
    finally:
        await second.stop_all()
        await first.stop_all()
