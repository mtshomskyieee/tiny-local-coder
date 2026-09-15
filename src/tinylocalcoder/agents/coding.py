"""Coding agent — plan todos plus freeform file read/edit (no tool-calling)."""

from __future__ import annotations

import re

from langchain_core.messages import HumanMessage, SystemMessage

from tinylocalcoder.llm import invoke_llm
from tinylocalcoder.memory.files import TodoStep, WorkspaceMemory
from tinylocalcoder.tools.manifest import fulfill_manifest_todo, is_manifest_todo
from tinylocalcoder.tools.review import fulfill_review_todo, is_review_todo


CODE_CREATE_SYSTEM = """You are a coding agent on a small-context local LLM.
You receive ONLY the current todo step. Create that ONE file completely.
Output ONLY the file contents — no markdown fences, no explanation.
Keep the file short and correct.
"""

CODE_REFINE_SYSTEM = """You refine ONE existing file for the current todo step only.
Return the FULL updated file contents only — no markdown fences.
Apply the user request precisely (e.g. change a port number).
"""


_FENCE_RE = re.compile(r"^```(?:\w+)?\n([\s\S]*?)\n```$", re.MULTILINE)
_BACKTICK_PATH_RE = re.compile(r"`([^`]+)`")
_PATH_RE = re.compile(
    r"(?:^|[\s`'\"])((?:src/|workspace/)?[\w./-]+\.(?:py|md|txt|json|toml|sh))(?:$|[\s`'\":,])",
    re.IGNORECASE,
)
_SHOW_RE = re.compile(
    r"\b(?:show|read|display|cat|print|open)\b.*?\b([\w./-]+\.(?:py|md|txt|json|toml|sh))\b",
    re.IGNORECASE,
)
_EDIT_HINT_RE = re.compile(
    r"\b(?:update|change|fix|edit|write|save|set|modify|replace|refactor)\b",
    re.IGNORECASE,
)


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1)
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return text


def _norm_path(path: str) -> str:
    return WorkspaceMemory.normalize_workspace_path(path.strip().strip("`'\""))


def _paths_in_text(text: str) -> list[str]:
    found: list[str] = []
    for m in _BACKTICK_PATH_RE.finditer(text or ""):
        p = _norm_path(m.group(1))
        if p and p not in found:
            found.append(p)
    for m in _PATH_RE.finditer(text or ""):
        p = _norm_path(m.group(1))
        if p and p not in found:
            found.append(p)
    return found


def _paths_for_todo(todo: TodoStep) -> list[str]:
    paths = _paths_in_text(todo.raw_line)
    target = _norm_path(todo.target)
    if target and target not in paths:
        paths.insert(0, target)
    # Drop shell-looking "paths"
    return [
        p
        for p in paths
        if not p.startswith(("python", "curl", "http"))
        and ("/" in p or p.endswith((".py", ".md", ".txt", ".json", ".toml", ".sh")))
    ] or ([target] if target else [])


def pending_file_paths(memory: WorkspaceMemory) -> list[str]:
    return [
        t.target
        for t in memory.parse_todos()
        if not t.done and t.action in {"create", "write", "refine"}
    ]


def _show_file(memory: WorkspaceMemory, path: str) -> dict:
    path = _norm_path(path)
    if not memory.prototype_exists(path):
        return {
            "last_file": path,
            "output": f"File not found: `{path}`",
            "phase": "done",
            "status": "failed",
            "pending_initial_files": pending_file_paths(memory),
        }
    content = memory.read_prototype(path)
    return {
        "last_file": path,
        "output": f"Contents of `{path}`:\n```\n{content.rstrip()}\n```",
        "phase": "done",
        "status": "ok",
        "pending_initial_files": pending_file_paths(memory),
    }


def _write_one_file(
    memory: WorkspaceMemory,
    path: str,
    *,
    step_ctx: str,
    prompt: str,
    refine: bool,
) -> str:
    existing = memory.read_prototype(path) if refine or memory.prototype_exists(path) else ""
    if existing and len(existing) > 4000:
        existing = existing[:4000] + "\n# …truncated…"
    if refine or existing:
        messages = [
            SystemMessage(content=CODE_REFINE_SYSTEM),
            HumanMessage(
                content=(
                    f"{step_ctx}\n"
                    f"File to update: `{path}`\n"
                    f"User request: {prompt or '(follow the step)'}\n\n"
                    f"Current file contents:\n{existing or '(empty)'}\n"
                )
            ),
        ]
    else:
        messages = [
            SystemMessage(content=CODE_CREATE_SYSTEM),
            HumanMessage(
                content=(
                    f"{step_ctx}\n"
                    f"User request: {prompt or '(none)'}\n"
                    f"Write the complete file `{path}` now."
                )
            ),
        ]
    result = invoke_llm(messages)
    content = _strip_fences(
        result.content if isinstance(result.content, str) else str(result.content)
    )
    memory.write_prototype(path, content.rstrip() + "\n")
    return path


def run_coding_step(memory: WorkspaceMemory, todo: TodoStep, prompt: str = "") -> dict:
    """Execute a single create/refine todo (may touch multiple paths in the line)."""
    # Inventory is filesystem work — never ask a small LLM to invent path lists.
    if is_manifest_todo(todo):
        return fulfill_manifest_todo(memory, todo, focus_hint=prompt)
    # Per-file review from the manifest — never ask the LLM to invent the inventory again.
    if is_review_todo(todo):
        return fulfill_review_todo(memory, todo, focus_hint=prompt)

    step_ctx = memory.current_step_prompt(todo)
    paths = _paths_for_todo(todo)
    if not paths:
        return {
            "last_file": "",
            "output": f"Step {todo.number}: no file path in todo",
            "phase": "coding",
            "status": "failed",
            "pending_initial_files": pending_file_paths(memory),
        }

    written: list[str] = []
    for path in paths:
        refine = todo.action == "refine" or memory.prototype_exists(path)
        written.append(
            _write_one_file(
                memory,
                path,
                step_ctx=step_ctx,
                prompt=prompt or todo.description,
                refine=refine,
            )
        )

    memory.mark_todo_done(todo.number)
    rel = ", ".join(written)
    return {
        "last_file": written[-1],
        "output": f"Step {todo.number}: wrote {rel}",
        "phase": "coding",
        "status": "ok",
        "pending_initial_files": pending_file_paths(memory),
    }


def run_coding_agent(memory: WorkspaceMemory, prompt: str) -> dict:
    """Plan-driven create/refine, or freeform show/edit from the user prompt."""
    prompt = (prompt or "").strip()

    # Freeform: show/read a file (no LLM write)
    show = _SHOW_RE.search(prompt)
    if show and not _EDIT_HINT_RE.search(prompt):
        return _show_file(memory, show.group(1))
    if prompt.lower() in {"show", "read"} or prompt.lower().startswith(
        ("show ", "read ", "display ", "cat ")
    ):
        paths = _paths_in_text(prompt)
        if paths:
            return _show_file(memory, paths[0])

    # Freeform edit when the user names a file and an edit intent
    if prompt and _EDIT_HINT_RE.search(prompt):
        paths = _paths_in_text(prompt)
        if paths:
            notes: list[str] = []
            for path in paths:
                _write_one_file(
                    memory,
                    path,
                    step_ctx=f"Goal: apply user file edit\nFile: `{path}`",
                    prompt=prompt,
                    refine=memory.prototype_exists(path),
                )
                notes.append(path)
                # If this path matches an open coding todo, mark it done
                for t in memory.pending_todos():
                    if t.action in {"create", "write", "refine"} and (
                        t.target == path or path in _paths_for_todo(t)
                    ):
                        memory.mark_todo_done(t.number)
            return {
                "last_file": notes[-1],
                "output": "Updated: " + ", ".join(f"`{p}`" for p in notes),
                "phase": "coding",
                "status": "ok",
                "pending_initial_files": pending_file_paths(memory),
            }

    todo = memory.next_todo()
    if todo and todo.action in {"create", "write", "refine"}:
        return run_coding_step(memory, todo, prompt)

    # Next plan step is run/test — don't pretend coding finished the plan
    if todo and todo.action in {"run", "test"}:
        return {
            "last_file": "",
            "output": (
                "No coding todos pending (next plan step is a run/test). "
                "Use /execute-plan for shell steps, or /code with an explicit "
                "file edit like: update `src/main.py` port to 8888"
            ),
            "phase": "done",
            "status": "ok",
            "pending_initial_files": [],
        }

    creates = [t for t in memory.parse_todos() if t.action in {"create", "write", "refine"}]
    if not creates:
        return {
            "last_file": "",
            "output": (
                "No coding todos in the plan. "
                "Ask to edit a file explicitly, e.g. "
                "`/code update src/main.py port to 8888`, or `/code show src/main.py`."
            ),
            "phase": "done",
            "status": "ok",
            "pending_initial_files": [],
        }
    step = TodoStep(
        number=creates[0].number,
        action="refine",
        target=creates[0].target,
        description=prompt or "polish the file",
        done=False,
        raw_line=creates[0].raw_line,
    )
    return run_coding_step(memory, step, prompt)
