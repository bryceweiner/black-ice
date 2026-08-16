from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Blank in .env means "no limit", not a parse error.
OptDays = Annotated[int | None, BeforeValidator(lambda v: None if v in ("", None) else v)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    assistant_name: str = "Ice"
    owner_name: str = "owner"

    host: str = "0.0.0.0"
    port: int = 8080
    secret_key: str = "dev-insecure-key"
    admin_username: str = "admin"
    admin_password_hash: str = ""

    lmstudio_base_url: str = "http://localhost:1234/v1"
    model_primary: str = "qwen3.8-27b-abliterated-mlx"
    model_triage: str = "qwen3.5-9b"

    data_dir: Path = Path("data")

    log_level: str = "INFO"
    # Relative paths land in DATA_DIR/logs. Blank disables the file handler.
    log_file: str = "blackice.log"

    guard_model: str = "meta-llama/Llama-Prompt-Guard-2-86M"
    guard_threshold: float = 0.5

    memory_enabled: bool = True
    kokoro_memory_root: Path = Path("data/memory")

    rsi_feedback_enabled: bool = True
    rsi_proposals_enabled: bool = True
    rsi_self_edit_enabled: bool = False
    rsi_golden_set_min: int = 50

    retain_media_days: OptDays = 30
    retain_events_days: OptDays = 365
    retain_llm_days: OptDays = None

    voice_enabled: bool = False
    piper_voice: str = ""
    voice_end_silence_ms: int = 900
    # voice2's spacebar interrupt puts the tty in raw mode, which breaks
    # Ctrl-C and console formatting. Barge-in by voice works regardless.
    voice_keyboard_interrupt: bool = False
    # Voice answers are spoken, so latency matters more than depth.
    # Blank uses MODEL_PRIMARY.
    model_voice: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "blackice.db"

    @property
    def plugin_db_dir(self) -> Path:
        return self.data_dir / "plugins"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.plugin_db_dir,
            self.media_dir,
            self.log_dir,
            self.kokoro_memory_root,
        ):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
