"""Deterministic text normalisation. No model, no network, no I/O.

Every piece of text entering the LLM -- typed, spoken, or supplied by a sensor
-- passes through here first, so that PromptGuard and the model see one
canonical form rather than a homoglyph variant chosen to evade them.

Folds, in order:
  1. invisible formatting (zero-width, bidi overrides, BOM)
  2. Unicode compatibility  (NFKC: mathematical alphanumerics, fullwidth,
     enclosed, ligatures)                              -- 𝒟 -> D, Ａ -> A, ﬁ -> fi
  3. cross-script confusables (Cyrillic/Greek lookalikes, small capitals)
  4. combining marks on ASCII bases                    -- é -> e
  5. whitespace and residual control characters
"""

from __future__ import annotations

import re
import unicodedata

from anyascii import anyascii

# Invisible characters: never legitimate in sensor or command text, and the
# cheapest way to smuggle a payload past a substring check.
INVISIBLE = dict.fromkeys(
    [
        0x00AD, 0x061C, 0x180E, 0xFEFF,
        *range(0x200B, 0x2010),  # ZWSP..RLM
        *range(0x202A, 0x202F),  # LRE..RLO
        *range(0x2060, 0x2065),  # word joiner..invisible plus
        *range(0xFFF9, 0xFFFC),  # interlinear annotation
        *range(0x1D173, 0x1D17B),  # musical format controls
        *range(0xE0000, 0xE0080),  # tag characters
    ]
)

# NFKC leaves these alone -- they are distinct letters, not compatibility
# variants -- but they are visually identical to ASCII.
_CONFUSABLE_PAIRS = [
    # Cyrillic. These map by *appearance*, not transliteration -- г is
    # romanised "g" but looks like "r", and the disguise is what matters here.
    ("АВЕКМНОРСТУХЅІЈԀԚԜҒ", "ABEKMHOPCTYXSIJDQWF"),
    ("аеорсухѕіјԁгпимтквн", "aeopcyxsijdrnumtkbh"),
    # Greek
    ("ΑΒΕΖΗΙΚΜΝΟΡΤΥΧΦ", "ABEZHIKMNOPTYXO"),
    ("αβεικνορτυχγ", "abeiknoptuxy"),
    # Armenian. Also by appearance: ո romanises to "vo" but reads as "n".
    ("ոօսրհյգաքՕՍՏ", "nourhjqwpOUS"),
    # Latin small capitals (U+1D00 block) -- no compatibility decomposition
    ("ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘqʀꜱᴛᴜᴠᴡʏᴢ", "ABCDEFGHIJKLMNOPQRSTUVWYZ"),
]

# Not 1:1, so kept explicit rather than as paired strings.
_SYMBOLS = {
    "ǀ": "l", "ǁ": "ll", "׀": "|",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "«": '"', "»": '"', "„": '"', "“": '"', "”": '"', "″": '"',
    "‘": "'", "’": "'", "′": "'",
}

CONFUSABLES = {
    ord(src): dst
    for pairs, targets in _CONFUSABLE_PAIRS
    for src, dst in zip(pairs, targets, strict=True)
} | {ord(k): v for k, v in _SYMBOLS.items()}

_WS = re.compile(r"\s+")

# Scripts that supply ASCII lookalikes. Everything outside this set (CJK,
# Arabic, Hebrew, Indic, ...) is left alone: those are not evasion vectors, and
# transliterating them would corrupt legitimate sensor text. The raw string is
# retained alongside the normalised one everywhere it is logged.
_FOLD_SCRIPTS = (
    "LATIN", "GREEK", "CYRILLIC", "ARMENIAN",
    "COPTIC", "CHEROKEE", "GLAGOLITIC", "DESERET",
)


def _fold_confusable_scripts(text: str) -> str:
    """Catch-all for ASCII lookalikes the explicit table does not name.

    The table above takes priority, because it encodes visual similarity while
    anyascii transliterates phonetically.
    """
    out = []
    for ch in text:
        if ch.isascii():
            out.append(ch)
            continue
        if unicodedata.name(ch, "").startswith(_FOLD_SCRIPTS):
            out.append(anyascii(ch) or ch)
        else:
            out.append(ch)
    return "".join(out)


def _strip_ascii_marks(text: str) -> str:
    """Drop combining marks that sit on an ASCII base (é -> e).

    Marks on non-Latin bases are preserved: folding those would corrupt
    legitimate text in other scripts rather than defeat an evasion.
    """
    out: list[str] = []
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.category(ch) == "Mn" and out and out[-1].isascii():
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def normalize(text: str) -> str:
    if not text:
        return ""
    text = text.translate(INVISIBLE)
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(CONFUSABLES)
    text = _strip_ascii_marks(text)
    text = _fold_confusable_scripts(text)
    # Residual control characters, keeping newline and tab as whitespace.
    text = "".join(
        " " if ch in "\n\r\t" else ch
        for ch in text
        if ch in "\n\r\t" or unicodedata.category(ch) not in ("Cc", "Cf")
    )
    return _WS.sub(" ", text).strip()
