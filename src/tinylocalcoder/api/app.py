"""FastAPI app sharing the LangGraph pipeline core."""

from __future__ import annotations

import threading
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from tinylocalcoder.api.schemas import ApproveRequest, PendingApproval, PromptRequest, RunResponse
from tinylocalcoder.config import get_settings
from tinylocalcoder.exec.gate import ApprovalGate, Decision, PendingCommand
from tinylocalcoder.graph.builder import Pipeline, build_pipeline

_SETTLED = frozenset({"pending_approval", "ok", "error", "denied"})
_POLL_ATTEMPTS = 120
_POLL_SECONDS = 0.5


def probe_ollama() -> dict[str, Any]:
    """Ollama reachability and the configured model name."""
    settings = get_settings()
    ollama_ok = False
    detail = ""
    try:
        r = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=3.0)
        ollama_ok = r.status_code == 200
        detail = "ok" if ollama_ok else r.text[:200]
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
    return {
        "status": "ok" if ollama_ok else "degraded",
        "ollama": ollama_ok,
        "ollama_detail": detail,
        "model": settings.model_name,
    }


class ApiSession:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.gate = ApprovalGate()
        self.pipeline = build_pipeline(gate=self.gate, settings=self.settings)
        self._lock = threading.Lock()
        self._run_thread: threading.Thread | None = None
        self._result: dict[str, Any] | None = None
        self._error: str | None = None
        self._done = threading.Event()
        self._last_mode: str = "plan"

    def _blocking_callback(self, pending: PendingCommand) -> Decision:
        # Park in gate maps so /approve can resume; wait for decision
        event = threading.Event()
        with self.gate._lock:
            self.gate._pending[pending.command_id] = pending
            self.gate._events[pending.command_id] = event
        self.gate.last_pending = pending  # type: ignore[attr-defined]
        event.wait()
        with self.gate._lock:
            decision = self.gate._decisions.pop(pending.command_id, Decision.DENY)
            self.gate._pending.pop(pending.command_id, None)
            self.gate._events.pop(pending.command_id, None)
        if decision == Decision.ALLOW_ALL:
            self.gate.allow_all = True
        return decision

    def start_run(
        self,
        mode: str,
        prompt: str,
        thinking_enabled: bool | None,
        *,
        workflow: str | None = None,
    ) -> None:
        with self._lock:
            if self._run_thread and self._run_thread.is_alive():
                raise RuntimeError("A run is already in progress")
            self._result = None
            self._error = None
            self._done.clear()
            self._last_mode = mode
            self.gate.set_callback(self._blocking_callback)

            def target() -> None:
                try:
                    extra: dict[str, Any] = {}
                    if thinking_enabled is not None:
                        extra["thinking_enabled"] = thinking_enabled
                    if workflow:
                        self._result = self.pipeline.invoke_workflow(
                            workflow, prompt, **extra  # type: ignore[arg-type]
                        )
                    else:
                        self._result = self.pipeline.invoke(mode, prompt, **extra)
                except Exception as exc:  # noqa: BLE001
                    self._error = str(exc)
                finally:
                    self._done.set()

            self._run_thread = threading.Thread(target=target, daemon=True)
            self._run_thread.start()

    def wait_briefly(self, timeout: float = 0.5) -> None:
        self._done.wait(timeout=timeout)

    def is_busy(self) -> bool:
        thread = self._run_thread
        return bool(thread and thread.is_alive())

    def snapshot(self, mode: str) -> RunResponse:
        pending_list = self.gate.list_pending()
        pending = pending_list[0] if pending_list else None
        if pending:
            return RunResponse(
                status="pending_approval",
                mode=mode,
                pending_approval=PendingApproval(
                    command_id=pending.command_id,
                    command=pending.command,
                    cwd=pending.cwd,
                    reason=pending.reason,
                ),
                progress=self.pipeline.memory.progress_summary(),
            )

        if self._done.is_set():
            if self._error:
                return RunResponse(status="error", mode=mode, error=self._error)
            result = self._result or {}
            return RunResponse(
                status=result.get("status") or "ok",
                mode=mode,
                output=str(result.get("output") or ""),
                phase=str(result.get("phase") or ""),
                last_file=str(result.get("last_file") or ""),
                last_command=str(result.get("last_command") or ""),
                progress=result.get("progress") or self.pipeline.memory.progress_summary(),
                critic=str(result.get("error") or ""),
                raw={k: v for k, v in result.items() if k != "messages"},
            )

        return RunResponse(
            status="running",
            mode=mode,
            progress=self.pipeline.memory.progress_summary(),
        )

    def run_until_settled(
        self,
        mode: str,
        prompt: str,
        thinking_enabled: bool | None,
        *,
        workflow: str | None = None,
    ) -> RunResponse:
        """Start a run and wait until it settles or the short poll window ends."""
        self.start_run(mode, prompt, thinking_enabled, workflow=workflow)
        return self.wait_until_settled(mode)

    def wait_until_settled(self, mode: str) -> RunResponse:
        """Poll until pending approval, a terminal status, or ~60s."""
        for _ in range(_POLL_ATTEMPTS):
            self.wait_briefly(_POLL_SECONDS)
            snap = self.snapshot(mode)
            if snap.status in _SETTLED:
                return snap
            if self._done.is_set():
                return self.snapshot(mode)
        return self.snapshot(mode)


def create_app(session: ApiSession | None = None) -> FastAPI:
    from tinylocalcoder.mcp_server import build_mcp

    state = session or ApiSession()
    mcp_app = build_mcp(state).http_app(path="/", transport="streamable-http")
    app = FastAPI(
        title="TinyLocalCoder",
        version="0.1.0",
        lifespan=mcp_app.lifespan,
    )
    app.state.session = state

    @app.get("/health")
    def health() -> dict[str, Any]:
        return probe_ollama()

    def _run_mode(
        mode: str,
        body: PromptRequest,
        *,
        workflow: str | None = None,
    ) -> RunResponse:
        try:
            return state.run_until_settled(
                mode, body.prompt, body.thinking_enabled, workflow=workflow
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/plan", response_model=RunResponse)
    def plan(body: PromptRequest) -> RunResponse:
        return _run_mode("plan", body)

    @app.post("/v1/code", response_model=RunResponse)
    def code(body: PromptRequest) -> RunResponse:
        return _run_mode("code", body)

    @app.post("/v1/execute", response_model=RunResponse)
    def execute(body: PromptRequest) -> RunResponse:
        return _run_mode("execute", body)

    @app.post("/v1/ask", response_model=RunResponse)
    def ask(body: PromptRequest) -> RunResponse:
        return _run_mode("ask", body)

    @app.post("/v1/review", response_model=RunResponse)
    def review(body: PromptRequest) -> RunResponse:
        return _run_mode("review", body, workflow="review")

    @app.post("/v1/review-fix", response_model=RunResponse)
    def review_fix(body: PromptRequest) -> RunResponse:
        return _run_mode("review-fix", body, workflow="review-fix")

    @app.post("/v1/fix-plan", response_model=RunResponse)
    def fix_plan(body: PromptRequest) -> RunResponse:
        return _run_mode("fix-plan", body, workflow="fix-plan")

    @app.post("/v1/test", response_model=RunResponse)
    def test_workflow(body: PromptRequest) -> RunResponse:
        return _run_mode("test", body, workflow="test")

    @app.post("/v1/soup-to-nuts", response_model=RunResponse)
    def soup_to_nuts(body: PromptRequest) -> RunResponse:
        return _run_mode("soup-to-nuts", body, workflow="soup-to-nuts")

    @app.get("/v1/status", response_model=RunResponse)
    def status() -> RunResponse:
        return state.snapshot(state._last_mode)

    @app.post("/v1/execute/approve", response_model=RunResponse)
    def approve(body: ApproveRequest) -> RunResponse:
        ok = state.gate.approve(body.command_id, body.decision)
        if not ok:
            raise HTTPException(status_code=404, detail="Unknown command_id")
        return state.wait_until_settled("execute")

    @app.post("/v1/workspace/clear")
    def clear_workspace() -> dict[str, Any]:
        """Same as TUI /clear-workspace: archive live files, blank plan/ask/session."""
        if state.is_busy():
            raise HTTPException(status_code=409, detail="A run is already in progress")
        try:
            dest = state.pipeline.memory.clear_workspace()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        rel = dest.relative_to(state.pipeline.memory.root)
        return {"status": "ok", "archive": str(rel)}

    @app.get("/v1/workspace/files")
    def list_files() -> dict[str, Any]:
        return {"files": state.pipeline.memory.list_files()}

    @app.get("/v1/workspace/file")
    def read_file(path: str = Query(..., description="Relative workspace path")) -> JSONResponse:
        try:
            content = state.pipeline.memory.read_workspace_file(path)
        except (ValueError, FileNotFoundError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"path": path, "content": content})

    app.mount("/mcp", mcp_app)
    return app


app = create_app()
