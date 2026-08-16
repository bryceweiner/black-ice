import pytest

from blackice import db
from blackice.config import get_settings
from blackice.llm import guard

# Settings read .env, so without this the suite depends on whatever the
# developer happens to have configured -- renaming the assistant in .env was
# enough to fail a dozen tests. Environment variables outrank .env in
# pydantic-settings, so pinning them here makes the suite self-contained.
FIXED_ENV = {
    "ASSISTANT_NAME": "Ice",
    "OWNER_NAME": "owner",
    "OWNER_GENDER": "",
    "OWNER_HONORIFIC": "",
    "MODEL_PRIMARY": "test-primary",
    "MODEL_TRIAGE": "test-triage",
    "MODEL_VOICE": "",
    "VOICE_ENABLED": "false",
    "MEMORY_ENABLED": "true",
    "RSI_FEEDBACK_ENABLED": "true",
    "GUARD_THRESHOLD": "0.5",
    "LOG_FILE": "",
}


@pytest.fixture(autouse=True)
def fixed_settings(monkeypatch):
    for key, value in FIXED_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    """Never load real PromptGuard weights in tests.

    Tests that care about scoring override this; the rest neither wait for a
    model to load nor depend on whether it is installed.
    """
    monkeypatch.setattr(guard.guard_model, "score", lambda text: 0.0)


@pytest.fixture
async def data_dir(tmp_path, monkeypatch):
    """Isolate settings + database into a temp dir for one test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KOKORO_MEMORY_ROOT", str(tmp_path / "memory"))
    get_settings.cache_clear()
    await db.close()
    await db.connect()
    yield tmp_path
    await db.close()
    get_settings.cache_clear()
