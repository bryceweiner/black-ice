"""voice2 backend: Silero VAD + faster-whisper + Piper, with barge-in.

voice2 calls ask_fn synchronously from its own worker thread. The harness is
async and owns the database connection, so the call hops back onto the running
loop rather than opening a second one.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import random
import shutil
import threading
import time
from typing import Any

from ..config import get_settings
from . import voice2_logging
from .cues import GatedCues
from .gateway import VoiceGateway

log = logging.getLogger("blackice.voice.voice2")


def _pick_varied(choices: tuple[str, ...], last: str | None) -> str:
    """Random, but never the same one twice running."""
    return random.choice([c for c in choices if c != last] or list(choices))

ASK_TIMEOUT_S = 300.0

#: Spoken when an announcement has to cut across something. An alert loud
#: enough to raise is worth interrupting for; the courtesy is in saying so, and
#: in leaving the owner free to wave it away or ask for more. Varied, so a run
#: of alerts does not sound like a recording. `{address}` becomes ", sir" or
#: nothing at all, depending on OWNER_GENDER / OWNER_HONORIFIC.
INTERJECTIONS = (
    "Excuse me{address}, but we have a situation that requires your attention.",
    "Excuse me{address}, but something needs your attention.",
    "Sorry to interrupt{address}.",
    "Forgive the interruption{address}.",
    "A moment{address} — this needs you.",
)

#: How long to let the owner finish a sentence before cutting in. Their speech
#: is the only sound that counts as a conversation; a television is owed no
#: such courtesy, and an alert that waits for a quiet room may never be heard.
ANNOUNCE_USER_GRACE_S = 6.0
#: How long to let playback unwind its audio stream after an interrupt. It
#: polls between chunks, so this is tens of milliseconds in practice.
INTERRUPT_SETTLE_S = 0.5
#: How long to give the playback worker to take the turn it was handed. Past
#: this the engine is left THINKING, which is a state nothing else exits.
ANNOUNCE_HANDOFF_S = 5.0

# Spoken when the model is taking a while, so a pause does not read as "it
# never heard me". Short, and varied so it does not sound like a recording.
FILLERS = (
    "Hang on...",
    "Just a moment.",
    "One second.",
    "Give me a second.",
    "Let me check that.",
    "Bear with me.",
    "This is taking a second.",
    "Still working on it.",
    "On it, one sec.",
)


class Voice2Backend(VoiceGateway):
    def __init__(self, harness=None) -> None:
        super().__init__(harness)
        self.engine: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._filler_timer: threading.Timer | None = None
        self._last_filler: str | None = None
        self._last_interjection: str | None = None

    # --- preflight ---------------------------------------------------------

    @staticmethod
    def preflight() -> list[str]:
        """Missing prerequisites, as human-readable strings. Empty means ready."""
        problems = []
        try:
            import voice2  # noqa: F401
        except ImportError:
            problems.append(
                "voice2 is not installed "
                "(uv pip install git+https://huggingface.co/AIIT-Threshold/voice2)"
            )
            return problems  # nothing else is checkable

        if shutil.which("piper") is None:
            problems.append(
                "the `piper` binary is not on PATH "
                "(uv pip install piper-tts -- there is no brew formula)"
            )

        from pathlib import Path

        # Check the voice we will actually use, not voice2's default: a stale
        # default on disk would otherwise mask a bad PIPER_VOICE.
        model = Path(Voice2Backend.voice_path())
        if not model.exists():
            problems.append(
                f"no Piper voice at {model} (set PIPER_VOICE to a .onnx file)"
            )
        elif not model.with_suffix(".onnx.json").exists():
            problems.append(
                f"{model.name} has no matching .onnx.json config beside it"
            )
        try:
            import sounddevice  # noqa: F401
        except Exception as exc:
            problems.append(f"sounddevice unusable: {exc}")
        return problems

    # --- lifecycle ---------------------------------------------------------

    @staticmethod
    def voice_path() -> str:
        """The Piper voice actually in use: PIPER_VOICE, else voice2's default."""
        from voice2 import VoiceConfig

        return get_settings().piper_voice or VoiceConfig().tts.model_path

    def build_config(self):
        from voice2 import VoiceConfig

        s = get_settings()
        cfg = VoiceConfig()
        cfg.log_file = str(s.log_dir / "voice_engine.jsonl")
        cfg.tts.model_path = self.voice_path()
        # voice2 defaults to 5s of trailing silence before it will even start
        # thinking. That is patient for dictation and interminable for "Ice,
        # arm the alarms", so it is configurable and much shorter by default.
        cfg.vad.end_silence_ms = s.voice_end_silence_ms
        cfg.asr.model_size = s.voice_asr_model
        if not s.voice_barge_in:
            # RMS never exceeds 1.0, so this disables interruption without
            # patching voice2's worker out of the pipeline.
            cfg.interrupt_vad.threshold = 10.0
            cfg.interrupt_vad.energy_multiplier = 1e6
        return cfg

    def _ask(self, text: str) -> str:
        """Sync bridge called from voice2's worker thread."""
        if self._loop is None:
            self.buzz()
            return "The assistant is not running."
        try:
            future = asyncio.run_coroutine_threadsafe(self.respond(text), self._loop)
            heard = future.result(timeout=ASK_TIMEOUT_S)
        except Exception as exc:
            # Buzz as well as speak: a failure can leave the turn in a state
            # where the spoken reply is refused, and silence looks identical to
            # not being heard.
            log.exception("voice ask failed")
            self.buzz()
            return f"Something went wrong handling that: {exc}"
        # Not addressed: stay silent rather than talking over the room.
        return heard.reply or ""

    @staticmethod
    def _disable_keyboard_worker() -> None:
        """Stop voice2 putting the terminal into raw mode.

        Its spacebar-interrupt worker calls tty.setraw() on stdin, which clears
        both ISIG (so Ctrl-C never becomes SIGINT) and OPOST (so console output
        walks diagonally down the screen). Barge-in by voice does not need it.

        The engine already tolerates this worker failing to start, so raising
        from start() uses its own supported path rather than patching internals.
        """
        from voice2 import engine as engine_module

        class _Disabled(engine_module.KeyboardWorker):
            def start(self) -> None:
                raise RuntimeError("keyboard interrupt disabled by configuration")

        engine_module.KeyboardWorker = _Disabled

    async def start(self) -> None:
        problems = self.preflight()
        if problems:
            for p in problems:
                log.error("voice disabled: %s", p)
            raise RuntimeError("; ".join(problems))

        voice2_logging.install()
        voice2_logging.quiet_torch_hub()
        if not get_settings().voice_keyboard_interrupt:
            self._disable_keyboard_worker()
            log.info("spacebar interrupt disabled; barge-in is by voice")

        from voice2 import VoiceEngine

        self._loop = asyncio.get_running_loop()
        self.engine = VoiceEngine(self.build_config(), self._ask)
        # Wrap before engine.start(): the workers are handed this object then,
        # and voice2 chimes on every utterance whether or not it was for us.
        self.engine.cues = GatedCues(self.engine.cues, get_settings().voice_cue_mode)
        # Model loading is heavy and blocking; keep it off the event loop.
        await asyncio.to_thread(self._load_models_quietly)
        await asyncio.to_thread(self.engine.start)
        log.info("voice online; wake word is %r", get_settings().assistant_name)

    def _load_models_quietly(self) -> None:
        """Load Whisper/Silero, routing their stray prints into the log.

        torch.hub and faster-whisper write progress straight to stdout, which
        bypasses log levels and handlers entirely.
        """
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                self.engine.load_models()
        finally:
            for line in buffer.getvalue().splitlines():
                if line.strip():
                    log.debug("model loader: %s", line.strip())

    async def stop(self) -> None:
        if self.engine is not None:
            await asyncio.to_thread(self.engine.stop)
            self.engine = None

    async def wait_closed(self, poll_s: float = 0.2) -> None:
        """Block until voice2 decides it is shutting down.

        Its keyboard worker puts stdin in raw mode, so the tty driver stops
        translating Ctrl-C into SIGINT and delivers the byte instead. The worker
        consumes it and sets an internal event -- which never reaches us as a
        signal. Watching that event is the only way to notice.
        """
        while self.engine is not None and not self.engine.shared.shutdown.is_set():
            await asyncio.sleep(poll_s)

    def status(self) -> dict[str, Any]:
        if self.engine is None:
            return {"running": False}
        try:
            return {"running": True, **self.engine.status()}
        except Exception:
            return {"running": True}

    # --- "hang on" while the model thinks ----------------------------------

    def _pick_filler(self) -> str:
        """Random, but never the same one twice running."""
        self._last_filler = _pick_varied(FILLERS, self._last_filler)
        return self._last_filler

    def on_thinking_start(self) -> None:
        delay = get_settings().voice_filler_delay_s
        if delay <= 0 or self.engine is None:
            return
        self._filler_timer = threading.Timer(delay, self._speak_filler)
        self._filler_timer.daemon = True
        self._filler_timer.start()

    def on_thinking_end(self) -> None:
        if self._filler_timer is not None:
            self._filler_timer.cancel()
            self._filler_timer = None

    def _speak_filler(self) -> None:
        phrase = self._pick_filler()
        log.debug("filler: %s", phrase)
        self._speak_aside(phrase)

    def _speak_aside(self, text: str) -> None:
        """Speak without touching the turn state machine.

        The playback worker is not usable here. A turn gets exactly one
        THINKING -> SPEAKING transition; spending it on a filler drops the
        machine back to LISTENING, and the real answer is then refused with
        `invalid_transition` and never spoken at all. voice2's cue player owns
        a separate output stream for precisely this kind of aside.
        """
        engine = self.engine
        cues = getattr(engine, "cues", None)
        tts = getattr(engine, "_tts", None)
        if engine is None or cues is None or tts is None:
            return
        try:
            import numpy as np

            chunks = [c for c in tts.synthesize(text) if len(c)]
            if not chunks:
                return
            cues._play(np.concatenate(chunks))
        except Exception:
            log.exception("could not speak aside %r", text[:40])

    def on_addressed(self) -> None:
        cues = getattr(self.engine, "cues", None)
        if cues is not None:
            with contextlib.suppress(Exception):
                cues.acknowledge()

    def buzz(self) -> None:
        """Audible failure signal, on the cue stream so it always gets out."""
        cues = getattr(self.engine, "cues", None)
        if cues is not None:
            with contextlib.suppress(Exception):
                cues.error()

    def _interjection(self) -> str:
        """The apology for cutting in, addressed as the owner prefers."""
        from ..llm.prompts import honorific

        self._last_interjection = _pick_varied(INTERJECTIONS, self._last_interjection)
        term = honorific()
        return self._last_interjection.format(address=f", {term}" if term else "")

    def _make_room(self) -> bool:
        """Stop whatever is happening so an announcement can be heard.

        Returns True if something was actually cut short -- which is what earns
        the apology, and what distinguishes an interruption from simply
        speaking into a quiet room.

        The owner mid-sentence is the only sound worth deferring to, and only
        briefly: an alert raised at all is worth interrupting for, and one that
        waits for a quiet room may never be heard. Ambient noise is not a
        conversation and gets no such consideration.

        voice2's interrupt is written for a person barging in, so it hands the
        floor to the USER and parks the machine in INTERRUPTING -- a state only
        the listen worker leaves, and only when somebody speaks. Nobody is
        going to speak here, so the announcement resolves its own interrupt.
        """
        from voice2.enums import EngineState, FloorOwner, InterruptSource, TransitionReason

        engine, ctrl = self.engine, self.engine.ctrl

        grace = time.monotonic() + ANNOUNCE_USER_GRACE_S
        while ctrl.get_floor_owner() is FloorOwner.USER and time.monotonic() < grace:
            time.sleep(0.2)

        busy = (ctrl.get_state() in (EngineState.SPEAKING, EngineState.THINKING)
                or ctrl.get_floor_owner() is FloorOwner.USER)
        if not busy:
            return False

        engine.interrupt.trigger(InterruptSource.PROGRAMMATIC, reason="announcement")

        # Wait for playback to actually notice. It polls the flag between audio
        # chunks and releases the floor on its way out, so the floor going back
        # is the signal that the old line has stopped. Clearing too soon leaves
        # it playing to the end with the announcement queued behind it -- stale
        # and refused by the time it is reached, which is nine seconds of the
        # wrong sentence. Neither the state nor `shared.speaking` can be used
        # here: the interrupt changes both synchronously, before any audio has
        # seen anything, so waiting on them returns at once and proves nothing.
        settle = time.monotonic() + INTERRUPT_SETTLE_S
        while ctrl.get_floor_owner() is FloorOwner.USER and time.monotonic() < settle:
            time.sleep(0.02)

        # Clear it before speaking: playback aborts on a flag that is still
        # set, so a stale interrupt would cut off the announcement that caused
        # it -- and the floor is the user's until we take it back.
        engine.interrupt.clear()
        engine.floor.release_floor(reason="announcement")
        if ctrl.get_state() is EngineState.INTERRUPTING:
            ctrl.transition(EngineState.IDLE, TransitionReason.INTERRUPT_RESOLVED)
        return True

    def _announce(self, text: str) -> None:
        """Speak unprompted, as a turn of the assistant's own. Any thread.

        voice2 reaches SPEAKING only from THINKING, and the playback worker
        makes that transition itself. Handing it text while the engine sits in
        IDLE is therefore refused --

            transition_rejected from_state=IDLE to_state=SPEAKING
                                floor_owner=AGENT reason=invalid_transition

        -- whereupon the worker releases the floor and drops the line without
        raising, which is why every announcement was silent and nothing showed
        up in the log but that one line. Moving to THINKING first is what makes
        an unprompted line legal. Playback then transitions to SPEAKING and
        leaves the engine LISTENING when it finishes, so the owner can answer
        the announcement without saying the wake word again.
        """
        from voice2.enums import EngineState, TransitionReason
        from voice2.logging_util import LatencyTrace

        engine = self.engine
        if engine is None:
            return
        ctrl = engine.ctrl

        if self._make_room():
            text = f"{self._interjection()} {text}"

        turn_id = ctrl.start_new_turn()
        if not ctrl.transition(EngineState.THINKING, TransitionReason.THINK_START,
                               turn_id=turn_id):
            log.warning("could not take a turn to announce; speaking aside")
            self._speak_aside(text)
            return

        engine._playback_worker.submit(text, turn_id, engine._tts, LatencyTrace(turn_id))

        # If playback declines the turn -- a newer one started, or the floor
        # went to the user -- THINKING is a dead end: nothing else moves the
        # engine out of it, and an engine stuck thinking never listens again.
        handoff = time.monotonic() + ANNOUNCE_HANDOFF_S
        while ctrl.get_state() is EngineState.THINKING:
            if time.monotonic() >= handoff:
                log.warning("playback did not take the announcement; releasing the turn")
                ctrl.transition(EngineState.IDLE, TransitionReason.THINK_COMPLETE,
                                turn_id=turn_id)
                self._speak_aside(text)
                return
            time.sleep(0.1)

    async def say(self, text: str) -> None:
        """Speak without being asked -- reminders coming due, escalations.

        Not `engine.submit_text`: that injects text *as if ASR produced it*, so
        an announcement would be fed to the LLM as a user utterance instead of
        spoken. The playback worker is the only real output path, and voice2
        exposes no public wrapper for it.
        """
        if self.engine is None or not text.strip():
            return
        try:
            await asyncio.to_thread(self._announce, text)
        except Exception:
            log.exception("announcement failed")
