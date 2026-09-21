"""Provisioning: install a missing toolchain instead of misreading it as a bug.

The container ships without a compiler, so `make` exits 127. That is an
environment problem — the fix agent has nothing to patch — and the old ladder
spent a fix attempt on it and then skipped the step.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import tinylocalcoder.toolchains as toolchains_mod
from tinylocalcoder.memory.files import WorkspaceMemory
from tinylocalcoder.toolchains import TOOLCHAINS
from tinylocalcoder.tools.provision import (
    PROVISION_TIMEOUT_SEC,
    RECORD_FILE,
    missing_toolchains,
    provision,
    record_packages,
    toolchains_for_missing_binaries,
)


@dataclass
class _Result:
    command: str
    allowed: bool = True
    decision: str = "allow"
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    pid: int | None = None


class _FakeRunner:
    """Records what would have been run; never touches apt."""

    def __init__(self, exit_code: int = 0, allowed: bool = True) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self.exit_code = exit_code
        self.allowed = allowed

    def run(self, command: str, reason: str = "", timeout: int | None = None):
        self.calls.append((command, timeout))
        return _Result(
            command=command,
            allowed=self.allowed,
            decision="allow" if self.allowed else "deny",
            exit_code=self.exit_code,
            stderr="E: Unable to locate package" if self.exit_code else "",
        )


@pytest.fixture
def no_toolchains(monkeypatch: pytest.MonkeyPatch):
    """Pretend nothing is installed."""
    monkeypatch.setattr(toolchains_mod.shutil, "which", lambda name: None)


@pytest.fixture
def all_toolchains(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(toolchains_mod.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_missing_toolchains_reads_the_plan(
    memory: WorkspaceMemory, no_toolchains
) -> None:
    memory.write_plan(
        "# Plan\nGoal: C++ square root.\n\n"
        "## Todos\n"
        "1. [ ] create `square_root.cpp` — sqrt of argv\n"
        "2. [ ] run `make` — expect success\n"
    )
    assert [tc.name for tc in missing_toolchains(memory)] == ["cpp"]


def test_nothing_missing_when_installed(
    memory: WorkspaceMemory, all_toolchains
) -> None:
    memory.write_plan(
        "# Plan\nGoal: C++.\n\n## Todos\n1. [ ] create `a.cpp` — a thing\n"
    )
    assert missing_toolchains(memory) == []


def test_binaries_map_to_installing_toolchains() -> None:
    assert [tc.name for tc in toolchains_for_missing_binaries(["make"])] == ["cpp"]
    assert toolchains_for_missing_binaries(["square_root"]) == []
    # Deduped
    assert len(toolchains_for_missing_binaries(["g++", "make"])) == 1


def test_provision_runs_apt_with_a_longer_timeout(
    memory: WorkspaceMemory, all_toolchains
) -> None:
    runner = _FakeRunner()
    ok, note = provision(memory, runner, [TOOLCHAINS["cpp"]])
    assert ok, note
    (command, timeout) = runner.calls[0]
    assert command.startswith("apt-get update && apt-get install -y")
    assert "g++" in command and "make" in command
    # The default 60s verify budget would kill apt mid-install
    assert timeout == PROVISION_TIMEOUT_SEC


def test_provision_reports_a_failed_install(
    memory: WorkspaceMemory, all_toolchains
) -> None:
    ok, note = provision(memory, _FakeRunner(exit_code=100), [TOOLCHAINS["cpp"]])
    assert not ok
    assert "Unable to locate package" in note


def test_provision_reports_a_denied_install(
    memory: WorkspaceMemory, all_toolchains
) -> None:
    ok, note = provision(memory, _FakeRunner(allowed=False), [TOOLCHAINS["cpp"]])
    assert not ok
    assert "deny" in note


def test_provision_notices_the_binary_is_still_absent(
    memory: WorkspaceMemory, no_toolchains
) -> None:
    """apt exiting 0 is not proof the compiler is usable."""
    ok, note = provision(memory, _FakeRunner(), [TOOLCHAINS["cpp"]])
    assert not ok
    assert "still absent" in note


def test_installed_packages_are_recorded_for_the_next_image_build(
    memory: WorkspaceMemory
) -> None:
    """Runtime installs are ephemeral; the rebuild needs to know about them."""
    record_packages(memory, [TOOLCHAINS["cpp"]])
    record_packages(memory, [TOOLCHAINS["c"]])
    text = (memory.root / RECORD_FILE).read_text(encoding="utf-8")
    packages = text.split()
    assert packages.count("make") == 1
    assert {"g++", "gcc", "make"} <= set(packages)
