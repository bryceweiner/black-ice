"""What the auto-reloader watches, and more importantly what it must not."""

from pathlib import Path

from blackice import reload as reload_cfg
from blackice.config import get_settings


def test_core_and_plugin_sources_are_watched():
    dirs = [Path(d) for d in reload_cfg.watch_dirs()]
    assert any(d.name == "blackice" for d in dirs)
    assert any(d.name == "plugins" for d in dirs)


def test_installed_plugins_are_located():
    """Plugins are installed editable, so their source lives outside the
    package tree; watching only the working directory would miss one installed
    from elsewhere."""
    found = reload_cfg.plugin_dirs()
    assert found, "no installed plugins found"
    assert all(p.is_dir() for p in found)
    assert any("clock" in str(p) or "heartbeat" in str(p) for p in found)


def test_the_data_directory_is_never_watched():
    """The SQLite WAL, the rotating log and captured media all change while the
    service runs. Watching them restarts the process in a loop."""
    data = get_settings().data_dir.resolve()
    for d in reload_cfg.watch_dirs():
        assert not Path(d).is_relative_to(data), d


def test_volatile_files_are_excluded():
    for pattern in ("*.db-wal", "*.log", "*.jsonl", "*/data/*"):
        assert pattern in reload_cfg.EXCLUDES, pattern


def test_source_and_prompt_files_are_included():
    assert "*.py" in reload_cfg.INCLUDES
    assert "*.sql" in reload_cfg.INCLUDES        # schema.sql
    assert any("prompt" in p for p in reload_cfg.INCLUDES)


def test_watch_list_has_no_nested_duplicates():
    """Handing watchfiles the same tree twice doubles the work for nothing."""
    dirs = sorted(Path(d) for d in reload_cfg.watch_dirs())
    for i, a in enumerate(dirs):
        for b in dirs[i + 1:]:
            assert not b.is_relative_to(a), f"{b} is inside {a}"


def test_describe_is_human_readable():
    assert "watching" in reload_cfg.describe()
