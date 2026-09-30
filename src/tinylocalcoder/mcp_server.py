"""Curated MCP tools over the same session the REST API uses.

Clients (Claude, OpenCode, and anything else that speaks streamable HTTP)
call these instead of a generated mirror of every route. Shell commands stay
behind the approval gate: a ``pending_approval`` result is resolved with
``approve``, never auto-allowed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from fastmcp import FastMCP

if TYPE_CHECKING:
    from tinylocalcoder.api.app import ApiSession


def build_mcp(session: ApiSession) -> FastMCP:
    mcp = FastMCP("TinyLocalCoder")

    def _run(
        mode: str,
        prompt: str,
        thinking_enabled: bool | None,
        *,
        workflow: str | None = None,
    ) -> dict[str, Any]:
        return session.run_until_settled(
            mode, prompt, thinking_enabled, workflow=workflow
        ).model_dump(mode="json")

    @mcp.tool
    def health() -> dict[str, Any]:
        """Check Ollama reachability and the configured model name."""
        from tinylocalcoder.api.app import probe_ollama

        return probe_ollama()

    @mcp.tool
    def status() -> dict[str, Any]:
        """Return the current run snapshot.

        If status is pending_approval, call approve with that command_id.
        If status is running, call status again.
        """
        return session.snapshot(session._last_mode).model_dump(mode="json")

    @mcp.tool
    def list_files() -> dict[str, Any]:
        """List workspace-relative files, excluding archive and the chunk index."""
        return {"files": session.pipeline.memory.list_files()}

    @mcp.tool
    def read_file(path: str) -> dict[str, Any]:
        """Read one workspace-relative file. Paths that escape the workspace are rejected."""
        try:
            content = session.pipeline.memory.read_workspace_file(path)
        except (ValueError, FileNotFoundError, OSError) as exc:
            raise RuntimeError(str(exc)) from exc
        return {"path": path, "content": content}

    @mcp.tool
    def plan(prompt: str = "", thinking_enabled: bool | None = None) -> dict[str, Any]:
        """Write workspace/plan.md from the prompt. One pipeline pass.

        If status is pending_approval, call approve with that command_id and
        decision allow, deny, or allow_all. If status is running, call status.
        """
        return _run("plan", prompt, thinking_enabled)

    @mcp.tool
    def code(prompt: str = "", thinking_enabled: bool | None = None) -> dict[str, Any]:
        """Run the next create or refine todo, or a freeform file edit. One pipeline pass.

        If status is pending_approval, call approve with that command_id and
        decision allow, deny, or allow_all. If status is running, call status.
        """
        return _run("code", prompt, thinking_enabled)

    @mcp.tool
    def execute(prompt: str = "", thinking_enabled: bool | None = None) -> dict[str, Any]:
        """Walk open todos in workspace/plan.md. Shell commands stop for approval.

        If status is pending_approval, call approve with that command_id and
        decision allow, deny, or allow_all. If status is running, call status.
        """
        return _run("execute", prompt, thinking_enabled)

    @mcp.tool
    def ask(prompt: str = "", thinking_enabled: bool | None = None) -> dict[str, Any]:
        """Answer a question and append it to workspace/ask.md.

        If status is pending_approval, call approve with that command_id and
        decision allow, deny, or allow_all. If status is running, call status.
        """
        return _run("ask", prompt, thinking_enabled)

    @mcp.tool
    def review(prompt: str = "", thinking_enabled: bool | None = None) -> dict[str, Any]:
        """Inventory the workspace, write review.md, then execute that review plan.

        If status is pending_approval, call approve with that command_id and
        decision allow, deny, or allow_all. If status is running, call status.
        """
        return _run("review", prompt, thinking_enabled, workflow="review")

    @mcp.tool
    def review_fix(prompt: str = "", thinking_enabled: bool | None = None) -> dict[str, Any]:
        """Plan fixes from review.md findings, then execute them.

        If status is pending_approval, call approve with that command_id and
        decision allow, deny, or allow_all. If status is running, call status.
        """
        return _run("review-fix", prompt, thinking_enabled, workflow="review-fix")

    @mcp.tool
    def fix_plan(prompt: str = "", thinking_enabled: bool | None = None) -> dict[str, Any]:
        """Rewrite open todos so plan.md matches the requirement. Does not execute.

        If status is pending_approval, call approve with that command_id and
        decision allow, deny, or allow_all. If status is running, call status.
        """
        return _run("fix-plan", prompt, thinking_enabled, workflow="fix-plan")

    @mcp.tool
    def test(prompt: str = "", thinking_enabled: bool | None = None) -> dict[str, Any]:
        """Write a short runnable test plan, then execute it.

        If status is pending_approval, call approve with that command_id and
        decision allow, deny, or allow_all. If status is running, call status.
        """
        return _run("test", prompt, thinking_enabled, workflow="test")

    @mcp.tool
    def approve(
        command_id: str,
        decision: Literal["allow", "deny", "allow_all"],
    ) -> dict[str, Any]:
        """Resolve one parked shell command. Nothing runs until this is called.

        decision is allow (once), deny, or allow_all (rest of the session).
        If status is pending_approval again, call approve with the new
        command_id. If status is running, call status.
        """
        if not session.gate.approve(command_id, decision):
            raise RuntimeError("Unknown command_id")
        return session.wait_until_settled("execute").model_dump(mode="json")

    return mcp
