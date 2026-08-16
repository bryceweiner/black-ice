"""Control which of voice2's audio cues actually sound.

voice2 chimes on every detected utterance and again when it starts thinking,
both of which happen before anyone knows whether you were talking to it. In a
room with a television that is a lot of chiming about nothing.

This wraps its cue player so the default is a single acknowledgement once the
wake word has matched -- a beep that means "I heard you and you meant me".
Failure and interruption cues always sound; they carry information the silence
would not.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("blackice.voice.cues")

WAKE = "wake"   # acknowledge only once addressed (default)
ALL = "all"     # voice2's own behaviour
OFF = "off"     # silent except failures
MODES = (WAKE, ALL, OFF)


class GatedCues:
    """Proxies voice2's UICues, suppressing the noisy per-utterance cues."""

    def __init__(self, inner: Any, mode: str = WAKE) -> None:
        self.inner = inner
        self.mode = mode if mode in MODES else WAKE

    # --- suppressed unless the mode asks for everything --------------------

    def listening(self) -> None:
        if self.mode == ALL:
            self.inner.listening()

    def thinking(self) -> None:
        if self.mode == ALL:
            self.inner.thinking()

    def speaking(self) -> None:
        if self.mode == ALL:
            self.inner.speaking()

    # --- always audible: these mean something went differently -------------

    def interrupted(self) -> None:
        if self.mode != OFF:
            self.inner.interrupted()

    def error(self) -> None:
        self.inner.error()

    # --- ours --------------------------------------------------------------

    def acknowledge(self) -> None:
        """"I heard you, and you meant me." Fired once the wake word matches."""
        if self.mode in (WAKE, ALL):
            self.inner.listening()

    # --- passthrough -------------------------------------------------------

    def _play(self, samples: Any) -> None:
        self.inner._play(samples)

    def close(self) -> None:
        self.inner.close()

    def __getattr__(self, item: str) -> Any:
        # Anything voice2 adds later still reaches the real player.
        return getattr(self.inner, item)
