"""Shared LangGraph state."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages


Mode = Literal["plan", "code", "execute", "ask", "fix"]
Phase = Literal["idle", "coding", "executing", "done", "fixing"]


class AgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    mode: Mode
    prompt: str
    phase: Phase
    pending_initial_files: list[str]
    pending_commands: list[str]
    last_file: str
    last_command: str
    output: str
    critic_continue: bool
    critic_used: bool
    thinking_enabled: bool
    pending_approval: dict[str, Any]
    status: str
    progress: dict[str, int]
    error: str
    ran_shell: bool
    fix_attempts: int
    replan_attempts: int
    fixes: list[str]
    needs_replan: str
    plan_smell: bool
