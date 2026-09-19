"""LangGraph orchestration: plan | code | execute | ask | fix | replan → optional critic."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from langgraph.graph import END, StateGraph

from tinylocalcoder.agents.ask import run_ask_agent
from tinylocalcoder.agents.coding import pending_file_paths, run_coding_agent, run_coding_step
from tinylocalcoder.agents.critic import run_critic
from tinylocalcoder.agents.execution import run_execution_step
from tinylocalcoder.agents.fixing import (
    classify_plan_smell,
    is_junk_run_failure,
    run_fix_agent,
)
from tinylocalcoder.agents.replan import run_replan_agent
from tinylocalcoder.agents.thinking import run_plan_agent
from tinylocalcoder.config import Settings, get_settings
from tinylocalcoder.exec.gate import ApprovalGate
from tinylocalcoder.exec.runner import CommandRunner
from tinylocalcoder.graph.state import AgentState
from tinylocalcoder.memory.files import TodoStep, WorkspaceMemory

ProgressCallback = Callable[[str], None]


def _short(text: str, limit: int = 72) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class Pipeline:
    def __init__(
        self,
        memory: WorkspaceMemory | None = None,
        gate: ApprovalGate | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.memory = memory or WorkspaceMemory(self.settings)
        self.gate = gate or ApprovalGate()
        self.runner = CommandRunner(self.memory, self.gate, self.settings)
        self._on_progress: ProgressCallback | None = None
        self._trace_prefix: str = ""
        self.graph = self._build()

    def set_progress_callback(self, callback: ProgressCallback | None) -> None:
        """Optional UI hook: called with a short status line while work runs."""
        self._on_progress = callback

    def _notify(self, message: str) -> None:
        if self._on_progress is None:
            return
        try:
            self._on_progress(message)
        except Exception:  # noqa: BLE001 — UI updates must never abort the graph
            pass

    def _todo_status(self, todo: TodoStep, *, verb: str | None = None) -> str:
        total = self.memory.progress_summary().get("todos_total", "?")
        action = verb or todo.action
        target = _short(todo.target, 56)
        return f"thinking … step {todo.number}/{total}: {action} `{target}`"

    def _trace_todo(
        self, todo: TodoStep, *, verb: str = "running", status: str | None = None
    ) -> None:
        """Live `workflow »` line for compound flows (e.g. /test)."""
        if not self._trace_prefix:
            return
        total = self.memory.progress_summary().get("todos_total", "?")
        target = _short(todo.target, 72)
        if status:
            self._notify(
                f"{self._trace_prefix} » done {todo.number}/{total} [{status}]: "
                f"{todo.action} `{target}`"
            )
            return
        kind = " (shell)" if todo.action in {"run", "test"} else ""
        self._notify(
            f"{self._trace_prefix} » {verb} {todo.number}/{total}: "
            f"{todo.action} `{target}`{kind}"
        )

    def _notify_todo_board(self) -> None:
        """Ask the TUI to reprint the full todo list after a step changes."""
        self._notify("todos » board")

    def _build(self):
        g: StateGraph = StateGraph(AgentState)
        g.add_node("route", self._route_prep)
        g.add_node("plan", self._plan_node)
        g.add_node("code", self._code_node)
        g.add_node("execute_step", self._execute_step_node)
        g.add_node("fix", self._fix_node)
        g.add_node("replan", self._replan_node)
        g.add_node("ask", self._ask_node)
        g.add_node("critic", self._critic_node)

        g.set_entry_point("route")
        g.add_conditional_edges(
            "route",
            self._select_mode,
            {
                "plan": "plan",
                "code": "code",
                "execute": "execute_step",
                "fix": "fix",
                "ask": "ask",
            },
        )
        g.add_edge("plan", "critic")
        g.add_edge("code", "critic")
        g.add_conditional_edges(
            "fix",
            self._after_fix,
            {
                "continue_execute": "execute_step",
                "replan": "replan",
                "critic": "critic",
            },
        )
        g.add_conditional_edges(
            "replan",
            self._after_replan,
            {
                "continue_execute": "execute_step",
                "critic": "critic",
            },
        )
        g.add_conditional_edges(
            "execute_step",
            self._after_execute_step,
            {
                "more_steps": "execute_step",
                "auto_fix": "fix",
                "auto_replan": "replan",
                "critic": "critic",
            },
        )
        g.add_edge("ask", "critic")
        g.add_conditional_edges(
            "critic",
            self._after_critic,
            {
                "continue_code": "code",
                "continue_execute": "execute_step",
                "continue_plan": "plan",
                "continue_ask": "ask",
                "continue_fix": "fix",
                "end": END,
            },
        )
        return g.compile()

    def _failed_step_number(self, state: AgentState) -> int | None:
        failure = self.memory.read_last_failure()
        if failure and failure.get("step"):
            return int(failure["step"])
        todo = self.memory.next_todo()
        return todo.number if todo else None

    def _auto_skip_failed_step(
        self, state: AgentState, reason: str = "failed after auto-fix"
    ) -> int | None:
        if not self.settings.auto_skip:
            return None
        failure = self.memory.read_last_failure() or {}
        cmd = str(failure.get("command") or "")
        step = self._failed_step_number(state)
        if step is None:
            return None
        self.memory.mark_todo_skipped(step, reason)
        clones = self.memory.skip_clone_run_todos(
            cmd, reason=f"clone of failed import ({reason})"
        )
        if clones:
            self._notify(
                f"thinking … skipped {len(clones)} similar run todo(s)"
            )
        self.memory.clear_last_failure()
        self._notify_todo_board()
        return step

    def _route_prep(self, state: AgentState) -> dict[str, Any]:
        progress = self.memory.progress_summary()
        pending = self.memory.pending_todos()
        return {
            "pending_initial_files": pending_file_paths(self.memory),
            "pending_commands": [
                t.target for t in pending if t.action in {"run", "test"}
            ],
            "progress": progress,
            "thinking_enabled": self.settings.thinking_enabled
            if state.get("thinking_enabled") is None
            else state.get("thinking_enabled"),
            "critic_used": False,
            "critic_continue": False,
            "ran_shell": False,
            "status": "running",
            "phase": "idle",
            "fix_attempts": int(state.get("fix_attempts") or 0),
            "replan_attempts": int(state.get("replan_attempts") or 0),
        }

    def _select_mode(self, state: AgentState) -> str:
        return state.get("mode") or "ask"

    def _plan_node(self, state: AgentState) -> dict[str, Any]:
        prompt = state.get("prompt") or ""
        hint = _short(prompt, 48) if prompt else "rewrite plan"
        self._notify(f"thinking … planning: {hint}")
        text = run_plan_agent(self.memory, prompt)
        return {
            "output": text,
            "phase": "done",
            "progress": self.memory.progress_summary(),
            "status": "ok",
        }

    def _code_node(self, state: AgentState) -> dict[str, Any]:
        todo = self.memory.next_todo()
        prompt = state.get("prompt") or ""
        if todo and todo.action in {"create", "write", "refine"}:
            self._notify(self._todo_status(todo))
        elif prompt:
            self._notify(f"thinking … coding: {_short(prompt, 56)}")
        else:
            self._notify("thinking … coding")
        result = run_coding_agent(self.memory, prompt)
        result["progress"] = self.memory.progress_summary()
        result["status"] = result.get("status") or "ok"
        self._notify_todo_board()
        return result

    def _fix_node(self, state: AgentState) -> dict[str, Any]:
        prior_failed = state.get("status") == "failed"
        failure = self.memory.read_last_failure() or {}
        step = failure.get("step")
        cmd = _short(str(failure.get("command") or ""), 48)
        if step and cmd:
            self._notify(f"thinking … fixing step {step}: `{cmd}`")
        elif cmd:
            self._notify(f"thinking … fixing: `{cmd}`")
        else:
            self._notify("thinking … diagnosing last failure")

        # Junk run commands: skip immediately — do not LLM-fix or burn replan
        if is_junk_run_failure(failure):
            skipped = self._auto_skip_failed_step(
                state, reason="junk run command (not a valid verify)"
            )
            return {
                "output": (
                    f"Skipped junk run command `{failure.get('command', '')}`.\n"
                    f"Auto-skip: step {skipped} marked [!]."
                    if skipped
                    else "Junk run command; nothing to fix."
                ),
                "status": "skipped" if skipped else "failed",
                "phase": "executing",
                "progress": self.memory.progress_summary(),
                "fix_attempts": int(state.get("fix_attempts") or 0),
                "replan_attempts": int(state.get("replan_attempts") or 0),
                "fixes": [],
                "plan_smell": False,
                "junk_command": True,
            }

        result = run_fix_agent(self.memory, state.get("prompt") or "")
        attempts = int(state.get("fix_attempts") or 0) + 1
        result["progress"] = self.memory.progress_summary()
        result["fix_attempts"] = attempts
        result["replan_attempts"] = int(state.get("replan_attempts") or 0)

        needs_replan = result.get("needs_replan")
        plan_smell = bool(result.get("plan_smell"))
        if not plan_smell and needs_replan:
            plan_smell = True
        result["plan_smell"] = plan_smell

        if state.get("mode") == "execute":
            retry_status = None
            # Skip pointless retry when the model (or classifier) says replan
            if result.get("fixes") and not plan_smell:
                failure = self.memory.read_last_failure() or {}
                failed_step = failure.get("step")
                if failed_step:
                    self.memory.clear_todo_skip(int(failed_step))
                todo = self.memory.next_todo()
                if todo and todo.action in {"run", "test"}:
                    self._notify(self._todo_status(todo, verb="retry"))
                    self._trace_todo(todo, verb="retry")
                    retry = run_execution_step(
                        self.memory, self.runner, todo, state.get("prompt") or ""
                    )
                    result["output"] = (
                        f"{result.get('output', '')}\n\n"
                        f"— retry after fix —\n{retry.get('output', '')}"
                    )
                    retry_status = retry.get("status")
                    self._trace_todo(todo, status=str(retry_status or "ok"))
                    self._notify_todo_board()
                    result["last_command"] = retry.get("last_command") or ""
                    result["ran_shell"] = True
                    result["phase"] = retry.get("phase") or "executing"
                    if retry_status == "failed":
                        failure2 = self.memory.read_last_failure() or failure
                        plan_smell = classify_plan_smell(
                            self.memory,
                            failure=failure2,
                            fixes=result.get("fixes") or [],
                            needs_replan=needs_replan,
                        )
                        result["plan_smell"] = plan_smell

            if retry_status == "ok":
                result["status"] = "ok"
                result["plan_smell"] = False
            elif plan_smell and self._can_replan(state):
                result["status"] = "needs_replan"
                result["needs_replan"] = needs_replan or "plan todo does not match code"
            elif retry_status == "failed" or (prior_failed and not result.get("fixes")):
                result["status"] = "failed"
            else:
                result["status"] = result.get("status") or "ok"

            if result.get("status") == "failed" and self.settings.auto_skip:
                skipped = self._auto_skip_failed_step(state)
                if skipped is not None:
                    self._notify(
                        f"thinking … skipped step {skipped}; continuing"
                    )
                    result["output"] = (
                        f"{result.get('output', '')}\n"
                        f"Auto-skip: step {skipped} marked [!] skipped; continuing plan."
                    )
                    result["status"] = "skipped"
                    result["phase"] = "executing"
                    result["progress"] = self.memory.progress_summary()
        else:
            if plan_smell:
                result["output"] = (
                    f"{result.get('output', '')}\n"
                    "Hint: this looks like a bad plan todo — run /execute-plan "
                    "with auto-replan on, or /plan to rewrite."
                )
            result["status"] = result.get("status") or "ok"

        return result

    def _can_replan(self, state: AgentState) -> bool:
        if not self.settings.auto_replan:
            return False
        return int(state.get("replan_attempts") or 0) < 1

    def _replan_node(self, state: AgentState) -> dict[str, Any]:
        failure = self.memory.read_last_failure() or {}
        reason = state.get("needs_replan") or "plan todo does not match code"
        # Peek: prefer tool path notify (actual mode returned after)
        self._notify("thinking … replanning (tools first)")
        result = run_replan_agent(
            self.memory,
            failure=failure,
            reason=str(reason),
        )
        mode = result.get("mode") or "tool"
        detail = result.get("detail") or ""
        if mode == "tool":
            self._notify("thinking … tool replan applied")
        elif mode == "llm":
            self._notify("thinking … LLM replan (open todos only)")
        else:
            self._notify("thinking … replan fallback")
        attempts = int(state.get("replan_attempts") or 0) + 1
        text = result.get("plan") or result.get("output") or ""
        return {
            "output": f"Replan agent ({mode}: {detail})\n{text}",
            "status": "ok",
            "phase": "executing",
            "progress": self.memory.progress_summary(),
            "replan_attempts": attempts,
            "fix_attempts": 0,
            "plan_smell": False,
            "needs_replan": "",
            "mode": "execute",
        }

    def _after_replan(
        self, state: AgentState
    ) -> Literal["continue_execute", "critic"]:
        if state.get("mode") == "execute" and self.memory.next_todo():
            return "continue_execute"
        return "critic"

    def _after_fix(
        self, state: AgentState
    ) -> Literal["continue_execute", "replan", "critic"]:
        if state.get("mode") != "execute":
            return "critic"
        if state.get("status") == "needs_replan":
            return "replan"
        if state.get("status") in {"ok", "skipped"} and self.memory.next_todo():
            return "continue_execute"
        if state.get("status") == "failed" and not self.settings.auto_skip:
            return "critic"
        if state.get("status") == "skipped" and self.memory.next_todo():
            return "continue_execute"
        return "critic"

    def _execute_step_node(self, state: AgentState) -> dict[str, Any]:
        """Run exactly one numbered todo with step-scoped LLM context."""
        todo = self.memory.next_todo()
        if not todo:
            self._notify("thinking … no remaining todos")
            return {
                "phase": "done",
                "output": "All todos complete.",
                "progress": self.memory.progress_summary(),
                "status": "ok",
                "ran_shell": bool(state.get("ran_shell")),
            }

        self._notify(self._todo_status(todo))
        self._trace_todo(todo)
        prompt = state.get("prompt") or ""
        if todo.action in {"create", "write", "refine"}:
            result = run_coding_step(self.memory, todo, prompt)
            result["ran_shell"] = bool(state.get("ran_shell"))
        else:
            result = run_execution_step(self.memory, self.runner, todo, prompt)
            result["ran_shell"] = True
        self._trace_todo(todo, status=str(result.get("status") or "ok"))
        self._notify_todo_board()

        result["progress"] = self.memory.progress_summary()
        result["output"] = (
            f"{result.get('output', '')}\n"
            f"(step {todo.number}/{self.memory.progress_summary().get('todos_total', '?')})"
        )
        result["fix_attempts"] = int(state.get("fix_attempts") or 0)
        result["replan_attempts"] = int(state.get("replan_attempts") or 0)
        return result

    def _after_execute_step(
        self, state: AgentState
    ) -> Literal["more_steps", "auto_fix", "auto_replan", "critic"]:
        status = state.get("status") or ""

        if status == "skipped":
            if self.memory.next_todo():
                return "more_steps"
            return "critic"

        if status == "failed":
            attempts = int(state.get("fix_attempts") or 0)
            if self.settings.auto_fix and attempts < self.settings.auto_fix_max:
                return "auto_fix"
            failure = self.memory.read_last_failure() or {}
            if self._can_replan(state) and classify_plan_smell(
                self.memory, failure=failure, fixes=[], needs_replan=None
            ):
                return "auto_replan"
            if self.settings.auto_skip:
                skipped = self._auto_skip_failed_step(state)
                if skipped is not None:
                    self._notify(
                        f"thinking … skipped step {skipped}; continuing"
                    )
                    if self.memory.next_todo():
                        return "more_steps"
            return "critic"

        if status == "denied":
            return "critic"

        nxt = self.memory.next_todo()
        if not nxt:
            return "critic"

        if nxt.action in {"create", "write", "refine"}:
            return "more_steps"

        if state.get("ran_shell") and not self.gate.allow_all:
            return "critic"
        return "more_steps"

    def _ask_node(self, state: AgentState) -> dict[str, Any]:
        prompt = state.get("prompt") or ""
        hint = _short(prompt, 56) if prompt else "workspace question"
        self._notify(f"thinking … asking: {hint}")
        text = run_ask_agent(self.memory, prompt)
        return {"output": text, "phase": "done", "status": "ok"}

    def _critic_node(self, state: AgentState) -> dict[str, Any]:
        enabled = state.get("thinking_enabled", self.settings.thinking_enabled)
        if not enabled or state.get("critic_used"):
            return {
                "critic_continue": False,
                "critic_used": True,
            }
        mode = state.get("mode") or "ask"
        self._notify(f"thinking … critic review ({mode})")
        result = run_critic(
            self.memory,
            state.get("prompt") or "",
            state.get("output") or "",
            mode=state.get("mode") or "",
            replan_attempts=int(state.get("replan_attempts") or 0),
        )
        return {
            "critic_continue": result["critic_continue"],
            "critic_used": True,
            "error": result.get("output") or "",
        }

    def _after_critic(self, state: AgentState) -> str:
        if not state.get("critic_continue"):
            return "end"
        if state.get("mode") == "execute" and state.get("status") in {
            "failed",
            "denied",
            "needs_replan",
        }:
            return "end"
        mode = state.get("mode") or "ask"
        mapping = {
            "plan": "continue_plan",
            "code": "continue_code",
            "execute": "continue_execute",
            "ask": "continue_ask",
            "fix": "continue_fix",
        }
        return mapping.get(mode, "end")

    def invoke(self, mode: str, prompt: str = "", **extra: Any) -> dict[str, Any]:
        initial: AgentState = {
            "mode": mode,  # type: ignore[typeddict-item]
            "prompt": prompt,
            "messages": [],
            "thinking_enabled": self.settings.thinking_enabled,
            "critic_used": False,
            "critic_continue": False,
            "ran_shell": False,
            "fix_attempts": 0,
            "replan_attempts": 0,
        }
        initial.update(extra)  # type: ignore[typeddict-item]
        result = self.graph.invoke(initial, config={"recursion_limit": 40})
        result["progress"] = self.memory.progress_summary()
        return result

    def invoke_workflow(
        self, kind: Literal["review", "test", "review-fix", "fix-plan"], prompt: str = "", **extra: Any
    ) -> dict[str, Any]:
        """Convenience: /plan with a fixed workflow prompt, then /execute-plan."""
        from tinylocalcoder.agents.workflows import run_workflow

        return run_workflow(self, kind, prompt, **extra)


def build_pipeline(
    memory: WorkspaceMemory | None = None,
    gate: ApprovalGate | None = None,
    settings: Settings | None = None,
) -> Pipeline:
    return Pipeline(memory=memory, gate=gate, settings=settings)
