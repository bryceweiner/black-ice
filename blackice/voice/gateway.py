"""Voice gateway. The audio backend is swappable; this is the part that decides
what a heard utterance means and what gets said back.

Every spoken turn takes the same path a typed one does -- normalise, guard as
USER trust, then the harness -- so voice reaches every service function without
a separate command grammar.
"""

from __future__ import annotations

import difflib
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
from ..models import Trust

log = logging.getLogger("blackice.voice")

# After answering, keep listening for follow-ups without needing the wake word
# again -- "Ice, is the garage shut?" / "and the front door?"
FOLLOW_UP_WINDOW_S = 30.0
# How alike a transcript must be to what we just said to count as echo.
ECHO_SIMILARITY = 0.8
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
        self._last_spoken = ""
        self._last_spoken_at = 0.0

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    # --- wake word ---------------------------------------------------------

    def wake_terms(self) -> list[str]:
        """The assistant's name plus any configured mishearings.

        Small ASR models mangle names -- `small.en` renders "Edith" as "Eat
        it", "Eda", "Ace". Fuzzy matching cannot fix this: "eat it" and "with"
        both score 0.60 against "edith", so any threshold loose enough to catch
        the mishearing also fires on an ordinary word. Explicit aliases are
        exact, so they cost nothing in false wakes.
        """
        s = get_settings()
        terms = [normalize(s.assistant_name).lower()]
        terms += [
            normalize(alias).lower()
            for alias in s.wake_aliases.split(",")
            if alias.strip()
        ]
        return [t for t in terms if t]

    def note_spoken(self, text: str) -> None:
        """Remember what we just said, so we can recognise hearing it back."""
        self._last_spoken = text or ""
        self._last_spoken_at = time.monotonic()

    def is_echo(self, text: str) -> bool:
        """Did the microphone just pick up our own voice?

        The speaker feeds the mic, so a reply gets transcribed as a new
        utterance. Inside the follow-up window it needs no wake word, so it
        answers itself and loops -- observed going three rounds, each transcript
        being the opening words of the previous reply.

        Content matching rather than muting the mic while speaking, because
        barge-in is a feature and deafness during playback would remove it.
        """
        if not self._last_spoken:
            return False
        if time.monotonic() - self._last_spoken_at > get_settings().voice_echo_window_s:
            return False

        heard = " ".join(_words_in_order(text))
        said = " ".join(_words_in_order(self._last_spoken))
        if not heard or not said:
            return False
        # A fragment of what we said, or near enough to the whole of it.
        if heard in said:
            return True
        return difflib.SequenceMatcher(None, heard, said).ratio() >= ECHO_SIMILARITY

    def is_addressed(self, text: str) -> bool:
        """Wake word, or a follow-up inside the conversation window."""
        lowered = text.lower()
        words = _words(lowered)
        for term in self.wake_terms():
            # Single words match on a boundary; multi-word aliases ("eat it")
            # have to be matched as a phrase.
            if (term in words) if " " not in term else (term in lowered):
                return True
        return (time.monotonic() - self._last_reply_at) < FOLLOW_UP_WINDOW_S

    def strip_wake_word(self, text: str) -> str:
        """Remove a leading wake term so the model sees only the request."""
        stripped = text
        for term in sorted(self.wake_terms(), key=len, reverse=True):
            pattern = re.compile(rf"^\W*{re.escape(term)}\b[\s,.!?]*", re.IGNORECASE)
            if pattern.match(stripped):
                stripped = pattern.sub("", stripped, count=1)
                break
        return stripped.strip() or text

    # --- hooks -------------------------------------------------------------

    def on_addressed(self) -> None:  # noqa: B027 - optional hook
        """The wake word matched. Fired before any slow work begins."""

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

        if self.is_echo(normalized):
            log.info("ignoring our own voice back off the speaker: %r",
                     normalized[:60])
            await self._log(transcript, normalized, woke=False, reply=None)
            return Heard(transcript, normalized, False, None)

        if not self.is_addressed(normalized):
            # Silence is correct here, but invisible: without this line a
            # misheard wake word looks identical to the mic not working.
            log.info("heard but not addressed to me: %r", normalized[:80])
            await self._log(transcript, normalized, woke=False, reply=None)
            return Heard(transcript, normalized, False, None)

        # Acknowledge immediately: this is the only confirmation the speaker
        # gets that they were understood as addressing us.
        self.on_addressed()

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

        self._last_reply_at = time.monotonic()
        await self._log(transcript, normalized, woke=True, reply=reply,
                        verdict=str(checked.verdict), score=checked.score)
        # The log keeps the model's full text; only the spoken form is flattened.
        spoken_reply = for_speech(reply)
        self.note_spoken(spoken_reply)
        return Heard(transcript, normalized, True, spoken_reply)

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


def _words_in_order(text: str) -> list[str]:
    return [w for w in (x.strip(",.!?;:\"'").lower() for x in text.split()) if w]


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
