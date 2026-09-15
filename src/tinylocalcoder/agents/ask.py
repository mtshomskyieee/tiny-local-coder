"""Ask agent — Q&A appended to ask.md with indexed context."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from tinylocalcoder.llm import invoke_llm
from tinylocalcoder.memory.files import WorkspaceMemory


ASK_SYSTEM = """You answer briefly using only the provided indexed workspace slices.
If context is insufficient, say what file slice is missing.
Keep answers short for a small-context model.
"""


def run_ask_agent(memory: WorkspaceMemory, prompt: str) -> str:
    memory.append_ask("user", prompt)
    ctx = "\n\n".join(
        [
            memory.context_slices("plan", prompt),
            memory.context_slices("prototypes", prompt),
            memory.context_slices("ask", prompt),
            memory.context_slices("exec", prompt),
        ]
    )
    messages = [
        SystemMessage(content=ASK_SYSTEM),
        HumanMessage(content=f"Context slices:\n{ctx}\n\nQuestion:\n{prompt}"),
    ]
    result = invoke_llm(messages)
    text = result.content if isinstance(result.content, str) else str(result.content)
    memory.append_ask("assistant", text)
    return text.strip()
