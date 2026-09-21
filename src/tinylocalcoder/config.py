"""Application settings for low-RAM local inference."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from tinylocalcoder.model_config import load_model_choice


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ollama_base_url: str = "http://127.0.0.1:11434"
    model_name: str = Field(default_factory=lambda: load_model_choice().ollama)
    num_ctx: int = Field(default_factory=lambda: load_model_choice().num_ctx)
    thinking_enabled: bool = True
    auto_fix: bool = True
    auto_fix_max: int = 1
    auto_skip: bool = True
    auto_replan: bool = True
    auto_install: bool = True
    workspace_dir: Path = Path("workspace")
    exec_timeout_sec: int = 60
    host: str = "0.0.0.0"
    port: int = 8000
    temperature: float = 0.2
    max_chunk_chars: int = 1200
    retrieve_top_k: int = 3
    # Ceilings for the workspace chunk index. A 3B model reads a handful of
    # chunks; anything past these bounds is memory spent to no purpose.
    max_index_file_bytes: int = 262_144
    max_index_total_chars: int = 4_194_304


@lru_cache
def get_settings() -> Settings:
    return Settings()
