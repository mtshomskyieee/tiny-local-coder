"""LLM client helpers — extract visible text and disable Ollama reasoning."""

from __future__ import annotations

import pytest

from tinylocalcoder.agents.thinking import run_plan_agent
from tinylocalcoder.llm import get_llm, message_text
from tinylocalcoder.memory.files import WorkspaceMemory


class _Msg:
    def __init__(self, content="", additional_kwargs=None) -> None:
        self.content = content
        if additional_kwargs is not None:
            self.additional_kwargs = additional_kwargs


def test_message_text_plain_string() -> None:
    assert message_text(_Msg("hello")) == "hello"


def test_message_text_strips_think_tags() -> None:
    raw = "<think>ramble forever</think>\n# Plan\nGoal: build api\n"
    assert message_text(_Msg(raw)) == "# Plan\nGoal: build api"


def test_message_text_drops_unclosed_think() -> None:
    assert message_text(_Msg("<think>still thinking, no answer")) == ""


def test_message_text_skips_thinking_blocks() -> None:
    content = [
        {"type": "thinking", "thinking": "hidden"},
        {"type": "text", "text": "1. [ ] create `db.py` — api"},
    ]
    assert message_text(_Msg(content)) == "1. [ ] create `db.py` — api"


def test_message_text_ignores_reasoning_kwargs() -> None:
    """Hidden CoT must not become plan.md when the visible reply is empty."""
    msg = _Msg("", additional_kwargs={"reasoning_content": "I will write a plan…"})
    assert message_text(msg) == ""


def test_get_llm_disables_reasoning(settings) -> None:
    llm = get_llm(settings)
    assert getattr(llm, "reasoning", None) is False


def test_run_plan_agent_rejects_empty_reply(
    memory: WorkspaceMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "tinylocalcoder.agents.thinking.invoke_llm",
        lambda _messages, settings=None: _Msg(""),
    )
    before = memory.read_plan()
    with pytest.raises(RuntimeError, match="empty plan"):
        run_plan_agent(memory, "create src/db.py")
    assert memory.read_plan() == before
