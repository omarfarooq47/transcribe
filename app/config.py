from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    upload_dir: Path = Path("uploads")
    output_dir: Path = Path("outputs")
    whisper_model: str = "base"
    max_file_size: int = 25 * 1024 * 1024  # 25 MB
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "sqlite:///./transcriptions.db"
    max_retries: int = 3
    allowed_extensions: str = "mp3,wav,m4a,flac"
    log_level: str = "INFO"

    @property
    def allowed_extension_set(self) -> set[str]:
        return {ext.strip().lower().lstrip(".") for ext in self.allowed_extensions.split(",") if ext.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
