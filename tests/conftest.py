"""Shared fixtures — every test runs against a throwaway workspace, no LLM."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tinylocalcoder.config import Settings, get_settings
from tinylocalcoder.memory.files import WorkspaceMemory


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the cached global Settings at tmp_path.

    Modules that reach for `get_settings()` directly (usage tracker, runner)
    must never touch the real ./workspace during tests.
    """
    monkeypatch.setitem(os.environ, "WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(workspace_dir=str(tmp_path))


@pytest.fixture
def memory(settings: Settings) -> WorkspaceMemory:
    return WorkspaceMemory(settings)


@pytest.fixture
def write_plan(memory: WorkspaceMemory):
    """Write plan.md verbatim (bypassing finalize) and return the memory."""

    def _write(text: str) -> WorkspaceMemory:
        memory.write_plan(text)
        return memory

    return _write
