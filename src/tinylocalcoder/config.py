"""Application settings for low-RAM local inference."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ollama_base_url: str = "http://127.0.0.1:11434"
    model_name: str = "qwen2.5:3b"
    num_ctx: int = 2048
    thinking_enabled: bool = True
    auto_fix: bool = True
    auto_fix_max: int = 1
    auto_skip: bool = True
    auto_replan: bool = True
    workspace_dir: Path = Path("workspace")
    exec_timeout_sec: int = 60
    host: str = "0.0.0.0"
    port: int = 8000
    temperature: float = 0.2
    max_chunk_chars: int = 1200
    retrieve_top_k: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
