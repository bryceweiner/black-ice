"""Voice gateway. The audio backend is swappable; this is the part that decides
what a heard utterance means and what gets said back.

Every spoken turn takes the same path a typed one does -- normalise, guard as
USER trust, then the harness -- so voice reaches every service function without
a separate command grammar.
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .. import db
from ..config import get_settings
from ..llm import guard
from ..llm.harness import Harness
from ..llm.harness import harness as default_harness
from ..llm.normalize import normalize
from ..memory import consolidate
from ..models import Trust

log = logging.getLogger("blackice.voice")

# After answering, keep listening for follow-ups without needing the wake word
# again -- "Ice, is the garage shut?" / "and the front door?"
FOLLOW_UP_WINDOW_S = 30.0
SESSION_ID = "voice"


@dataclass(slots=True)
class Heard:
    raw: str
    normalized: str
    woke: bool
    reply: str | None


class VoiceGateway(ABC):
    """Audio in, audio out. Implementations own devices; this owns meaning."""

    def __init__(self, harness: Harness | None = None) -> None:
        self.harness = harness or default_harness
        self._last_reply_at = 0.0

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    # --- wake word ---------------------------------------------------------

    def is_addressed(self, text: str) -> bool:
        """Wake word, or a follow-up inside the conversation window.

        ASR mishears short names, so match on a normalised word boundary rather
        than an exact string.
        """
        name = normalize(get_settings().assistant_name).lower()
        if name and name in _words(text.lower()):
            return True
        return (time.monotonic() - self._last_reply_at) < FOLLOW_UP_WINDOW_S

    def strip_wake_word(self, text: str) -> str:
        name = normalize(get_settings().assistant_name).lower()
        words = text.split()
        while words and words[0].lower().strip(",.!?") == name:
            words.pop(0)
        return " ".join(words).lstrip(",. ").strip() or text

    # --- hooks -------------------------------------------------------------

    def on_thinking_start(self) -> None:  # noqa: B027 - optional hook
        """Called once the utterance is ours and the model call begins.

        Optional: a gateway with no audio output has nothing to do here.
        """

    def on_thinking_end(self) -> None:  # noqa: B027 - optional hook
        """Called when the model returns, successfully or not."""

    # --- the one path every utterance takes --------------------------------

    async def respond(self, transcript: str) -> Heard:
        """Normalise, guard, wake-gate, answer. Fully logged, no audio needed."""
        normalized = normalize(transcript)
        if not normalized:
            return Heard(transcript, "", False, None)

        if not self.is_addressed(normalized):
            await self._log(transcript, normalized, woke=False, reply=None)
            return Heard(transcript, normalized, False, None)

        spoken = self.strip_wake_word(normalized)
        checked = await guard.inspect(spoken, trust=Trust.USER, channel="voice")

        if checked.blocked:
            reply = (
                "I did not act on that. It was rejected by the input filter "
                "and has been logged."
            )
        else:
            s = get_settings()
            # Only around the model call: it is the slow part, and the hooks
            # must never fire for speech that was not addressed to us.
            self.on_thinking_start()
            try:
                reply = await self.harness.run(
                    spoken, channel="voice", trust=Trust.USER,
                    session_id=SESSION_ID, model=s.model_voice or None,
                )
            finally:
                self.on_thinking_end()
            await consolidate.record_turn(spoken, reply, Trust.USER)

        self._last_reply_at = time.monotonic()
        await self._log(transcript, normalized, woke=True, reply=reply,
                        verdict=str(checked.verdict), score=checked.score)
        # The log keeps the model's full text; only the spoken form is flattened.
        return Heard(transcript, normalized, True, for_speech(reply))

    async def _log(
        self, raw: str, normalized: str, *, woke: bool, reply: str | None,
        verdict: str | None = None, score: float | None = None,
    ) -> None:
        await db.execute(
            """INSERT INTO voice_turns
                 (raw_transcript, normalized, guard_verdict, guard_score,
                  woke, reply, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (raw, normalized, verdict, score, int(woke), reply, SESSION_ID),
        )


def _words(text: str) -> set[str]:
    return {w.strip(",.!?;:\"'") for w in text.split()}


_MD_PATTERNS = (
    (re.compile(r"```.*?```", re.S), " "),        # code fences
    (re.compile(r"`([^`]*)`"), r"\1"),            # inline code
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),      # bold
    (re.compile(r"(?<!\w)\*([^*\n]+)\*(?!\w)"), r"\1"),  # italics
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),  # headings
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),      # bullets
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),  # links
    (re.compile(r"[ \t]*\n[ \t]*"), ". "),        # line breaks become pauses
    (re.compile(r"\.\s*\.\s*"), ". "),            # collapse doubled stops
    (re.compile(r"\s{2,}"), " "),
)


def for_speech(text: str) -> str:
    """Flatten markdown for TTS.

    The model formats for a screen by default; Piper would read the asterisks
    and hashes aloud.
    """
    out = text or ""
    for pattern, repl in _MD_PATTERNS:
        out = pattern.sub(repl, out)
    return out.strip(" .").strip() or ""
