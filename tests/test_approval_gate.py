"""The approval gate is the safety boundary: no command runs unapproved.

Covers both host paths — the TUI's synchronous callback and the API's
park-then-approve flow — plus the runner's refusal to execute on DENY.
"""

from __future__ import annotations

import threading

from tinylocalcoder.exec.gate import ApprovalGate, Decision, PendingCommand
from tinylocalcoder.exec.runner import CommandRunner
from tinylocalcoder.memory.files import WorkspaceMemory


def test_callback_decision_is_returned() -> None:
    seen: list[PendingCommand] = []

    def cb(pending: PendingCommand) -> Decision:
        seen.append(pending)
        return Decision.DENY

    gate = ApprovalGate(callback=cb)
    assert gate.request("rm -rf /", "/ws", reason="scary") == Decision.DENY
    assert seen[0].command == "rm -rf /"
    assert seen[0].reason == "scary"
    assert seen[0].command_id


def test_allow_all_is_sticky_and_skips_the_callback() -> None:
    calls = {"n": 0}

    def cb(pending: PendingCommand) -> Decision:
        calls["n"] += 1
        return Decision.ALLOW_ALL

    gate = ApprovalGate(callback=cb)
    assert gate.request("echo 1", "/ws") == Decision.ALLOW_ALL
    assert gate.request("echo 2", "/ws") == Decision.ALLOW_ALL
    assert calls["n"] == 1, "second command must not re-prompt"


def test_allow_does_not_become_sticky() -> None:
    calls = {"n": 0}

    def cb(pending: PendingCommand) -> Decision:
        calls["n"] += 1
        return Decision.ALLOW

    gate = ApprovalGate(callback=cb)
    gate.request("echo 1", "/ws")
    gate.request("echo 2", "/ws")
    assert calls["n"] == 2, "each command needs its own approval"


def test_reset_session_clears_allow_all() -> None:
    gate = ApprovalGate(callback=lambda p: Decision.ALLOW_ALL)
    gate.request("echo 1", "/ws")
    assert gate.allow_all
    gate.reset_session()
    assert not gate.allow_all


def test_api_flow_parks_command_then_resumes_on_approve() -> None:
    gate = ApprovalGate()  # no callback → API/async path
    result: list[Decision] = []

    worker = threading.Thread(
        target=lambda: result.append(gate.request("echo hi", "/ws")), daemon=True
    )
    worker.start()

    # Wait for the command to show up as pending
    pending: PendingCommand | None = None
    for _ in range(200):
        items = gate.list_pending()
        if items:
            pending = items[0]
            break
        threading.Event().wait(0.01)
    assert pending is not None, "command should be parked for approval"
    assert gate.get_pending(pending.command_id) is pending

    assert gate.approve(pending.command_id, "allow") is True
    worker.join(timeout=5)
    assert result == [Decision.ALLOW]
    # Pending state is cleaned up so the id cannot be replayed
    assert gate.list_pending() == []
    assert gate.get_pending(pending.command_id) is None
    assert gate.approve(pending.command_id, Decision.ALLOW) is False


def test_api_flow_allow_all_sets_the_flag() -> None:
    gate = ApprovalGate()
    result: list[Decision] = []
    worker = threading.Thread(
        target=lambda: result.append(gate.request("echo hi", "/ws")), daemon=True
    )
    worker.start()
    for _ in range(200):
        if gate.list_pending():
            break
        threading.Event().wait(0.01)
    gate.approve(gate.list_pending()[0].command_id, Decision.ALLOW_ALL)
    worker.join(timeout=5)
    assert result == [Decision.ALLOW_ALL]
    assert gate.allow_all is True


def test_approve_unknown_id_is_rejected() -> None:
    assert ApprovalGate().approve("nope", Decision.ALLOW) is False


def test_create_pending_for_api_does_not_block() -> None:
    gate = ApprovalGate()
    pending = gate.create_pending_for_api("echo hi", "/ws", reason="smoke")
    assert gate.get_pending(pending.command_id) is pending
    assert gate.approve(pending.command_id, Decision.DENY) is True


# --- runner honours the decision --------------------------------------


def test_runner_does_not_execute_when_denied(memory: WorkspaceMemory, settings) -> None:
    marker = memory.root / "should-not-exist.txt"
    runner = CommandRunner(
        memory, ApprovalGate(callback=lambda p: Decision.DENY), settings
    )
    result = runner.run(f"touch {marker.name}", reason="test")

    assert result.allowed is False
    assert result.decision == "deny"
    assert result.exit_code is None
    assert not marker.exists(), "denied command must never run"
    assert "DENIED" in memory.read_exec_log()


def test_runner_executes_when_allowed(memory: WorkspaceMemory, settings) -> None:
    runner = CommandRunner(
        memory, ApprovalGate(callback=lambda p: Decision.ALLOW), settings
    )
    result = runner.run("echo hello-tlc", reason="test")

    assert result.allowed is True
    assert result.exit_code == 0
    assert "hello-tlc" in result.stdout
    assert result.pid


def test_runner_runs_inside_the_workspace(memory: WorkspaceMemory, settings) -> None:
    """Commands must be confined to the workspace cwd, not the repo."""
    runner = CommandRunner(
        memory, ApprovalGate(callback=lambda p: Decision.ALLOW), settings
    )
    result = runner.run("pwd")
    assert result.stdout.strip() == str(memory.root)


def test_runner_reports_failing_exit_code(memory: WorkspaceMemory, settings) -> None:
    runner = CommandRunner(
        memory, ApprovalGate(callback=lambda p: Decision.ALLOW), settings
    )
    result = runner.run("python3 -c 'import sys; sys.exit(3)'")
    assert result.allowed is True
    assert result.exit_code == 3
