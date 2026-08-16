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
    # Drives how the assistant addresses you: male -> "sir",
    # female -> "ma'am", anything else -> no honorific.
    owner_gender: str = ""
    # Explicit override, for a term the gender mapping will not guess.
    owner_honorific: str = ""

    host: str = "0.0.0.0"
    port: int = 8080
    secret_key: str = "dev-insecure-key"
    admin_username: str = "admin"
    admin_password_hash: str = ""

    lmstudio_base_url: str = "http://localhost:1234/v1"
    model_primary: str = "qwen3.8-27b-abliterated-mlx"
    model_triage: str = "qwen3.5-4b-mlx"
    # Triage wants a verdict, not deliberation. LM Studio ignores the
    # usual thinking switches, so this prefills a closed <think> block.
    triage_no_think: bool = True

    data_dir: Path = Path("data")

    # Restart when core code, prompts or an installed plugin changes.
    auto_reload: bool = True

    log_level: str = "INFO"
    # Relative paths land in DATA_DIR/logs. Blank disables the file handler.
    log_file: str = "blackice.log"

    guard_model: str = "meta-llama/Llama-Prompt-Guard-2-86M"
    guard_threshold: float = 0.5

    memory_enabled: bool = True
    kokoro_memory_root: Path = Path("data/memory")
    # How often conversation and event patterns become durable facts.
    memory_consolidate_hours: float = 6.0

    rsi_feedback_enabled: bool = True
    rsi_proposals_enabled: bool = True
    rsi_self_edit_enabled: bool = False
    rsi_golden_set_min: int = 50
    # The daily self-review always runs and records proposals;
    # rsi_self_edit_enabled decides whether a passing one goes live.
    rsi_review_enabled: bool = True
    rsi_review_hours: int = 24

    retain_media_days: OptDays = 30
    retain_events_days: OptDays = 365
    retain_llm_days: OptDays = None

    voice_enabled: bool = False
    piper_voice: str = ""
    voice_end_silence_ms: int = 900
    # Speak a short filler if the model takes longer than this.
    # 0 disables it.
    voice_filler_delay_s: float = 3.0
    # Comma-separated mishearings of ASSISTANT_NAME to also treat as the
    # wake word. Populate from `blackice wake-report`.
    wake_aliases: str = ""
    # faster-whisper size. small.en mangles names; medium.en is markedly
    # better and still fast on Apple silicon.
    voice_asr_model: str = "small.en"
    # voice2's spacebar interrupt puts the tty in raw mode, which breaks
    # Ctrl-C and console formatting. Barge-in by voice works regardless.
    voice_keyboard_interrupt: bool = False
    # wake: one chime once the wake word matches (default).
    # all: voice2's chime-at-everything. off: failures only.
    voice_cue_mode: str = "wake"
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
