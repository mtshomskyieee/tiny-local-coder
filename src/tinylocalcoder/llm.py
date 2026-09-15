"""Local ChatOllama client tuned for small context."""

from __future__ import annotations

from typing import Any, Sequence

from langchain_core.messages import BaseMessage
from langchain_ollama import ChatOllama

from tinylocalcoder.config import Settings, get_settings
from tinylocalcoder.usage import record_from_message


def get_llm(settings: Settings | None = None) -> ChatOllama:
    s = settings or get_settings()
    return ChatOllama(
        model=s.model_name,
        base_url=s.ollama_base_url,
        temperature=s.temperature,
        num_ctx=s.num_ctx,
    )


def invoke_llm(messages: Sequence[BaseMessage], settings: Settings | None = None) -> Any:
    """Invoke the local model and record token usage when available."""
    llm = get_llm(settings)
    result = llm.invoke(list(messages))
    record_from_message(result)
    return result
