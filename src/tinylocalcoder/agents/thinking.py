"""Plan agent — numbered todo list for small-context step execution."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from tinylocalcoder.llm import invoke_llm, message_text
from tinylocalcoder.memory.files import WorkspaceMemory


PLAN_SYSTEM = """You write a SHORT markdown plan for a tiny local LLM (small context).

Required format (exactly):

# Plan
Goal: <one sentence>

## Todos
1. [ ] create `path/to/file.py` — what this file must do
2. [ ] run `python3 -m py_compile path/to/file.py` — expect success
3. [ ] run `python3 -c "import mod"` — expect success

Rules:
- Prefer ≤8 todos. Each todo is ONE tiny action: create | refine | run.
- Creates first, then ONE batched py_compile, then ONE short behavioral python3 -c.
- Do NOT stack many assert/run steps. One short check is enough.
- Never put meta commands in todos (reset-todo, skip-todo, auto-fix, clear-plan, review, review-fix, fix-plan, test, …).
- Paths/commands are workspace-relative. NEVER leading `/`.
- NEVER use placeholder paths like `path/to/file.py` — use real names (`db.py`, `src/api.py`).
- Prefer package-safe runs: `PYTHONPATH=. python3 -c "from src.api import app; …"`.
- Ensure `src/__init__.py` when using `src.*` imports.
- Run steps MUST exit quickly. Good: py_compile, short python3 -c. Bad: uvicorn/servers.
- For FastAPI/REST apps: verify `app` or route paths, NEVER `from mod import mod` or fake objects
  (bad: `from db import db; db.selectall()`; good: `from db import app; print([r.path for r in app.routes])`).
- Import only names the create step will define (usually `app`, not a second `db` object).
- For run todos, end with `expect success` or `expect True` (etc.).
- Keep Goal one line. No code blocks. Return the FULL plan.md only.
"""


def run_plan_agent(memory: WorkspaceMemory, prompt: str) -> str:
    current = memory.read_plan()
    # Only a tiny slice of the existing plan — not the whole history dump
    brief = current[:800]
    messages = [
        SystemMessage(content=PLAN_SYSTEM),
        HumanMessage(
            content=(
                f"User request:\n{prompt}\n\n"
                f"Current plan.md (may be empty/partial):\n{brief}\n\n"
                "Rewrite the full plan.md as a numbered todo list."
            )
        ),
    ]
    result = invoke_llm(messages)
    text = message_text(result)
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text:
        raise RuntimeError(
            "The model returned an empty plan (no todos). "
            "Reasoning models such as qwen3.5 can fill a 2048-token context "
            "with hidden thinking and never write plan.md. "
            "TinyLocalCoder now disables Ollama reasoning; rebuild/restart "
            "the suite if this session is still on an old image."
        )
    return memory.finalize_plan(text).strip()
