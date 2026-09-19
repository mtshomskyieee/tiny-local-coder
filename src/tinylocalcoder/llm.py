"""Local ChatOllama client tuned for small context."""

from __future__ import annotations

import re
from typing import Any, Sequence

from langchain_core.messages import BaseMessage
from langchain_ollama import ChatOllama

from tinylocalcoder.config import Settings, get_settings
from tinylocalcoder.usage import record_from_message

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)


def format_llm_error(exc: BaseException, model: str = "") -> str:
    """Turn Ollama runner crashes into an actionable message."""
    raw = str(exc)
    low = raw.lower()
    oom = (
        "cpu_repack" in low
        or "failed to allocate" in low
        or "unable to allocate" in low
        or "ggml_backend_cpu" in low
    )
    if not oom:
        return raw
    name = model or "the selected model"
    return (
        f"Ollama could not load {name}: the Docker VM ran out of RAM.\n"
        "qwen3.5:4b needs about 8 GB; Colima often starts with 2 GB.\n"
        "Stop the suite, then:\n"
        "  ./colima-start-stop-on-mac.sh\n"
        "  ./start-service.sh"
    )


def message_text(message: Any) -> str:
    """Visible model text — skip hidden thinking that never became a reply.

    qwen3.5 (and other reasoning models) stream tokens into Ollama's
    `message.thinking` field. With a 2048-token window those tokens often
    fill the context before `</think>`, so `content` is empty. ChatOllama
    also drops thinking unless `reasoning=True`. We never treat thinking
    as the user-facing answer.
    """
    parts: list[str] = []
    content = getattr(message, "content", None)
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type") or "").lower()
            if kind in {"thinking", "reasoning"}:
                continue
            text = block.get("text") or block.get("content")
            if isinstance(text, str):
                parts.append(text)
    raw = "\n".join(parts)
    raw = _THINK_BLOCK_RE.sub("", raw)
    raw = _THINK_OPEN_RE.sub("", raw)
    return raw.strip()


def get_llm(settings: Settings | None = None) -> ChatOllama:
    s = settings or get_settings()
    # Always off: thinking models eat the entire 2048-token ctx and return
    # an empty `content` (plan.md becomes Goal: (none)). Settings.thinking_enabled
    # only gates the critic, not Ollama CoT.
    return ChatOllama(
        model=s.model_name,
        base_url=s.ollama_base_url,
        temperature=s.temperature,
        num_ctx=s.num_ctx,
        reasoning=False,
    )


def invoke_llm(messages: Sequence[BaseMessage], settings: Settings | None = None) -> Any:
    """Invoke the local model and record token usage when available."""
    s = settings or get_settings()
    llm = get_llm(s)
    try:
        result = llm.invoke(list(messages))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(format_llm_error(exc, s.model_name)) from exc
    record_from_message(result)
    return result
