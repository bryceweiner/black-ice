"""Forms of address, and how the manners block reaches the model."""

import pytest

from blackice import db
from blackice.config import get_settings
from blackice.llm import prompts


def _settings(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "gender,expected",
    [
        ("male", "sir"), ("Male", "sir"), ("M", "sir"), ("he/him", "sir"),
        ("female", "ma'am"), ("FEMALE", "ma'am"), ("she/her", "ma'am"),
        ("non-binary", ""), ("other", ""), ("", ""), ("   ", ""),
    ],
)
def test_honorific_mapping(monkeypatch, gender, expected):
    _settings(monkeypatch, OWNER_GENDER=gender, OWNER_HONORIFIC="")
    assert prompts.honorific() == expected


def test_explicit_honorific_overrides_gender(monkeypatch):
    """Someone whose preferred term the mapping would never guess."""
    _settings(monkeypatch, OWNER_GENDER="male", OWNER_HONORIFIC="Mx. Weiner")
    assert prompts.honorific() == "Mx. Weiner"


def test_manners_uses_the_honorific(monkeypatch):
    _settings(monkeypatch, OWNER_GENDER="male", OWNER_NAME="Bryce", OWNER_HONORIFIC="")
    text = prompts.manners()
    assert '"sir"' in text
    assert "Yes sir." in text and "Right away, sir." in text
    assert "Bryce" in text
    # The instruction that keeps it subtle rather than servile.
    assert "obsequious" in text


def test_manners_without_a_gender_avoids_inventing_a_title(monkeypatch):
    _settings(monkeypatch, OWNER_GENDER="non-binary", OWNER_NAME="Alex",
              OWNER_HONORIFIC="")
    text = prompts.manners()
    assert "without an honorific" in text
    assert "sir" not in text and "ma'am" not in text
    assert "Alex" in text


async def test_system_prompt_carries_manners(data_dir, monkeypatch):
    _settings(monkeypatch, OWNER_GENDER="female", OWNER_HONORIFIC="")
    text = await prompts.active(prompts.SYSTEM)
    assert "## Manners" in text
    assert "ma'am" in text


async def test_triage_prompt_has_no_manners(data_dir):
    """Triage emits JSON for the pipeline; nobody is being addressed."""
    text = await prompts.active(prompts.TRIAGE)
    assert "## Manners" not in text


async def test_manners_survive_a_rewritten_prompt(data_dir, monkeypatch):
    """The RSI layer may rewrite the system prompt. Manners are identity, not
    editable text, so a rewrite must not be able to drop them."""
    _settings(monkeypatch, OWNER_GENDER="male", OWNER_HONORIFIC="")
    await db.execute(
        """INSERT INTO prompt_versions (name, version, text, author, active)
           VALUES (?, 99, 'You are a terse monitoring assistant.', 'rsi', 1)""",
        (prompts.SYSTEM,),
    )
    text = await prompts.active(prompts.SYSTEM)
    assert "terse monitoring assistant" in text
    assert "## Manners" in text and '"sir"' in text
