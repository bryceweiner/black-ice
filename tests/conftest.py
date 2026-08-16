import pytest

from blackice import db
from blackice.config import get_settings


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
