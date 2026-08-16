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
from typing import Any

from ..config import get_settings
from . import voice2_logging
from .gateway import VoiceGateway

log = logging.getLogger("blackice.voice.voice2")

ASK_TIMEOUT_S = 300.0

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
        choices = [f for f in FILLERS if f != self._last_filler] or list(FILLERS)
        self._last_filler = random.choice(choices)
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

    def buzz(self) -> None:
        """Audible failure signal, on the cue stream so it always gets out."""
        cues = getattr(self.engine, "cues", None)
        if cues is not None:
            with contextlib.suppress(Exception):
                cues.error()

    def _speak_now(self, text: str, *, new_turn: bool) -> None:
        """Push text straight to playback. Safe to call from any thread.

        `new_turn` matters: playback discards anything whose turn id is not the
        current one. A filler spoken *during* a turn must reuse that turn, or it
        would bump the counter and get the real answer suppressed as stale.
        """
        if self.engine is None or not text.strip():
            return
        try:
            from voice2.logging_util import LatencyTrace

            ctrl = self.engine.ctrl
            turn_id = ctrl.start_new_turn() if new_turn else ctrl.current_turn()
            self.engine._playback_worker.submit(
                text, turn_id, self.engine._tts, LatencyTrace(turn_id)
            )
        except Exception:
            log.exception("could not speak %r", text[:40])

    async def say(self, text: str) -> None:
        """Speak without being asked -- used to announce escalations.

        Not `engine.submit_text`: that injects text *as if ASR produced it*, so
        an announcement would be fed to the LLM as a user utterance instead of
        spoken. The playback worker is the only real output path, and voice2
        exposes no public wrapper for it.
        """
        await asyncio.to_thread(self._speak_now, text, new_turn=True)
