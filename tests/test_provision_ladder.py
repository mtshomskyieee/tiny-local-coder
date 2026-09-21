"""The environment rung of the recovery ladder, at the graph level.

`make: not found` used to spend the one auto-fix attempt on an error the model
could not patch, then skip the step. It must instead install the toolchain and
re-run the step.
"""

from __future__ import annotations

import pytest

import tinylocalcoder.graph.builder as builder_mod
import tinylocalcoder.toolchains as toolchains_mod
from tinylocalcoder.config import Settings
from tinylocalcoder.graph.builder import Pipeline
from tinylocalcoder.memory.files import WorkspaceMemory

_CPP_PLAN = (
    "# Plan\nGoal: C++ square root.\n\n"
    "## Todos\n"
    "1. [x] create `square_root.cpp` — sqrt of argv\n"
    "2. [ ] run `make` — expect success\n"
)

_MAKE_MISSING = {
    "step": 2,
    "command": "make",
    "exit_code": 127,
    "stdout": "",
    "stderr": "/bin/sh: 1: make: not found\n",
    "error": "exit code 127",
    "todo_raw": "2. [ ] run `make` — expect success",
}


@pytest.fixture
def no_toolchain(monkeypatch: pytest.MonkeyPatch):
    """The app container ships without a compiler; this host has one."""
    monkeypatch.setattr(toolchains_mod.shutil, "which", lambda name: None)


@pytest.fixture
def pipeline(
    settings: Settings, memory: WorkspaceMemory, no_toolchain
) -> Pipeline:
    memory.write_plan(_CPP_PLAN)
    memory.write_last_failure(dict(_MAKE_MISSING))
    return Pipeline(memory=memory, settings=settings)


def test_missing_toolchain_is_installed_and_the_step_retried(
    pipeline: Pipeline, memory: WorkspaceMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        builder_mod, "provision", lambda *_: (True, "installed cpp toolchain")
    )
    retried: list[str] = []

    def _fake_execute(mem, runner, todo, prompt=""):
        retried.append(todo.target)
        mem.mark_todo_done(todo.number)
        mem.clear_last_failure()
        return {"status": "ok", "output": "exit=0", "last_command": todo.target}

    monkeypatch.setattr(builder_mod, "run_execution_step", _fake_execute)

    result = pipeline._provision_and_retry({"mode": "execute"}, dict(_MAKE_MISSING))
    assert result is not None
    assert result["status"] == "ok"
    assert retried == ["make"], "the failed step must be re-run after the install"
    assert "installed cpp toolchain" in result["output"]
    assert "PROVISION" in memory.read_exec_log()


def test_a_failed_install_is_reported_as_an_environment_error(
    pipeline: Pipeline, memory: WorkspaceMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        builder_mod, "provision", lambda *_: (False, "install of cpp failed: no network")
    )
    result = pipeline._provision_and_retry({"mode": "execute"}, dict(_MAKE_MISSING))
    assert result is not None
    assert "Environment error" in result["output"]
    assert "no network" in result["output"]
    # The step is skipped rather than retried forever
    assert result["status"] == "skipped"


def test_auto_install_off_falls_through_to_the_normal_ladder(
    memory: WorkspaceMemory, settings: Settings
) -> None:
    memory.write_plan(_CPP_PLAN)
    settings.auto_install = False
    pipe = Pipeline(memory=memory, settings=settings)
    assert pipe._provision_and_retry({}, dict(_MAKE_MISSING)) is None


def test_a_missing_built_binary_is_not_provisioned(
    pipeline: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`./square_root: not found` is a build failure, not an apt problem."""
    monkeypatch.setattr(toolchains_mod.shutil, "which", lambda name: None)
    failure = {
        "step": 2,
        "command": "./square_root",
        "exit_code": 127,
        "stderr": "/bin/sh: 1: ./square_root: not found\n",
    }
    assert pipeline._provision_and_retry({}, failure) is None


def test_a_failed_install_does_not_retry_forever(
    pipeline: Pipeline, memory: WorkspaceMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale last_failure would re-classify as `environment` next pass."""
    monkeypatch.setattr(builder_mod, "provision", lambda *_: (False, "no network"))
    pipeline._provision_and_retry({"mode": "execute"}, dict(_MAKE_MISSING))
    assert memory.read_last_failure() in (None, {})


def test_a_failed_install_with_auto_skip_off_still_clears_the_failure(
    memory: WorkspaceMemory, settings: Settings, no_toolchain, monkeypatch
) -> None:
    memory.write_plan(_CPP_PLAN)
    memory.write_last_failure(dict(_MAKE_MISSING))
    settings.auto_skip = False
    monkeypatch.setattr(builder_mod, "provision", lambda *_: (False, "no network"))
    pipe = Pipeline(memory=memory, settings=settings)
    result = pipe._provision_and_retry({"mode": "execute"}, dict(_MAKE_MISSING))
    assert result["status"] == "failed"
    assert memory.read_last_failure() in (None, {})
