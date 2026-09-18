"""Local ChatOllama client tuned for small context."""

from __future__ import annotations

from typing import Any, Sequence

from langchain_core.messages import BaseMessage
from langchain_ollama import ChatOllama

from tinylocalcoder.config import Settings, get_settings
from tinylocalcoder.usage import record_from_message


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
    s = settings or get_settings()
    llm = get_llm(s)
    try:
        result = llm.invoke(list(messages))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(format_llm_error(exc, s.model_name)) from exc
    record_from_message(result)
    return result
