"""MCP tools share the API session. No Ollama, no real pipeline."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.exceptions import ToolError

from tinylocalcoder.api.app import create_app
from tinylocalcoder.api.schemas import RunResponse
from tinylocalcoder.mcp_server import build_mcp

_TOOL_NAMES = {
    "health",
    "status",
    "list_files",
    "read_file",
    "plan",
    "code",
    "execute",
    "ask",
    "review",
    "review_fix",
    "fix_plan",
    "test",
    "approve",
}


class _Memory:
    def __init__(self) -> None:
        self.files = ["plan.md"]

    def list_files(self) -> list[str]:
        return list(self.files)

    def read_workspace_file(self, path: str) -> str:
        if path.startswith("..") or path.startswith("/"):
            raise ValueError(f"Path escapes workspace: {path}")
        raise FileNotFoundError(path)


class _Pipeline:
    def __init__(self) -> None:
        self.memory = _Memory()


class _Gate:
    def __init__(self) -> None:
        self.known = True

    def approve(self, command_id: str, decision: str) -> bool:
        return self.known and command_id == "cmd-1"


class StubSession:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.busy = False
        self._last_mode = "plan"
        self.pipeline = _Pipeline()
        self.gate = _Gate()
        self.wait_mode: str | None = None

    def run_until_settled(
        self,
        mode: str,
        prompt: str,
        thinking_enabled: bool | None,
        *,
        workflow: str | None = None,
    ) -> RunResponse:
        self.calls.append((mode, prompt, thinking_enabled, workflow))
        if self.busy:
            raise RuntimeError("A run is already in progress")
        return RunResponse(status="ok", mode=mode, output="done")

    def snapshot(self, mode: str) -> RunResponse:
        return RunResponse(status="running", mode=mode)

    def wait_until_settled(self, mode: str) -> RunResponse:
        self.wait_mode = mode
        return RunResponse(status="ok", mode=mode, output="approved")


def _call(session: StubSession, name: str, arguments: dict | None = None):
    mcp = build_mcp(session)

    async def _run():
        async with Client(mcp) as client:
            return await client.call_tool(name, arguments or {})

    return asyncio.run(_run())


def test_tool_names() -> None:
    mcp = build_mcp(StubSession())

    async def _run() -> set[str]:
        async with Client(mcp) as client:
            tools = await client.list_tools()
            return {tool.name for tool in tools}

    assert asyncio.run(_run()) == _TOOL_NAMES


def test_plan_calls_run_until_settled() -> None:
    session = StubSession()
    result = _call(session, "plan", {"prompt": "hello world"})
    assert session.calls == [("plan", "hello world", None, None)]
    assert result.data["status"] == "ok"
    assert result.data["mode"] == "plan"
    assert result.data["output"] == "done"


def test_review_passes_workflow() -> None:
    session = StubSession()
    _call(session, "review", {"prompt": "focus on src"})
    assert session.calls == [("review", "focus on src", None, "review")]


def test_busy_session_is_a_tool_error() -> None:
    session = StubSession()
    session.busy = True
    with pytest.raises(ToolError, match="already in progress"):
        _call(session, "execute", {"prompt": "go"})


def test_read_file_rejects_escaping_path() -> None:
    session = StubSession()
    with pytest.raises(ToolError, match="Path escapes workspace"):
        _call(session, "read_file", {"path": "../secret"})


def test_mcp_mounted_beside_rest() -> None:
    """Streamable HTTP is on /mcp and the existing health route still answers."""
    with TestClient(create_app()) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert "ollama" in health.json()
        # A bare GET is not a streamable-HTTP handshake; it must not be "no such route".
        mounted = client.get("/mcp")
        assert mounted.status_code != 404
