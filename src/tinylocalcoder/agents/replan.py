"""Replan agent — tool-first; tiny-framed LLM only as last resort."""

from __future__ import annotations

import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from tinylocalcoder.llm import invoke_llm, message_text
from tinylocalcoder.memory.files import (
    REPLAN_OPEN_CAP,
    TodoStep,
    WorkspaceMemory,
    is_valid_run_command,
    looks_fastapi_goal,
    strip_placeholder_path,
)
from tinylocalcoder.toolchains import detect_toolchains, toolchain_for_path

_MOD_NOT_FOUND = re.compile(
    r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_IMPORT_NAME = re.compile(
    r"ImportError:\s+cannot import name ['\"](\w+)['\"]",
    re.IGNORECASE,
)
_TODO_LINE = re.compile(
    r"^(\d+)\.\s+\[([ xX!])\]\s+"
    r"(?:(create|write|refine|update|fix|run|test)\s+)?"
    r"(?:`([^`]+)`|(\S+))\s*"
    r"(?:—|--|-)?\s*(.*)$",
    re.IGNORECASE,
)

REPLAN_SYSTEM = """You fix ONE failed verify todo for a tiny local LLM.
Return ONLY 1 or 2 open todo lines (numbered). No Goal. No Done. No [x] lines.

Example:
1. [ ] run `PYTHONPATH=path/to python3 -c "import db"` — expect success

Rules:
- Match imports/paths to real workspace files from the file list.
- Prefer ONE short verify command, or one refine of an existing file.
- Use the language the workspace is already written in (python3 -c, make, ./binary, …).
- Never invent path/to/ placeholders. Never repeat completed work.
"""


def _failure_blob(failure: dict) -> tuple[str, str]:
    cmd = str(failure.get("command") or "")
    err = "\n".join(
        [
            str(failure.get("stderr") or ""),
            str(failure.get("error") or ""),
            str(failure.get("stdout") or ""),
        ]
    )
    return cmd, err


def _file_has_app(memory: WorkspaceMemory, rel: str) -> bool:
    text = memory.read_prototype(rel) if memory.prototype_exists(rel) else ""
    if not text:
        return False
    return bool(re.search(r"\bapp\s*=", text)) or "FastAPI(" in text


def _verify_todo_for_module(
    *,
    module: str,
    rel_path: str,
    fastapi: bool,
    has_app: bool,
) -> TodoStep:
    parent = str(Path(rel_path).parent).replace("\\", "/")
    stem = Path(rel_path).stem
    if fastapi and has_app:
        inner = (
            f"from {stem} import app; "
            "print([getattr(r,'path',None) for r in app.routes])"
        )
        cmd = f'python3 -c "{inner}"'
        if parent not in {".", ""}:
            cmd = f"PYTHONPATH={parent} " + cmd
    else:
        cmd = f'python3 -c "import {stem}"'
        if parent not in {".", ""}:
            cmd = f"PYTHONPATH={parent} " + cmd
    return TodoStep(
        number=1,
        action="run",
        target=cmd,
        description="expect success",
        done=False,
        raw_line="",
    )


def _workspace_sources(memory: WorkspaceMemory) -> list[str]:
    """Candidate source files, shallowest first (list_files skips archive/.index)."""
    files = [
        f
        for f in memory.list_files()
        if not f.endswith("__init__.py") and toolchain_for_path(f) is not None
    ]
    files.sort(key=lambda p: (p.count("/"), len(p)))
    return files


def _fallback_verify_todo(memory: WorkspaceMemory, goal: str) -> TodoStep | None:
    """A short verify for whatever language the workspace actually contains.

    Previously this only ever produced `python3 -c "import …"`, so a replan in
    a C++ workspace emitted a Python command that could not pass.
    """
    sources = _workspace_sources(memory)
    if not sources:
        return None
    py = [f for f in sources if f.endswith(".py")]
    if py:
        rel = py[0]
        return _verify_todo_for_module(
            module=Path(rel).stem,
            rel_path=rel,
            fastapi=looks_fastapi_goal(goal),
            has_app=_file_has_app(memory, rel),
        )
    for tc in detect_toolchains(sources):
        owned = [f for f in sources if tc.owns_path(f)]
        cmd = tc.compile_cmd(owned) if (tc.compile_cmd and owned) else None
        if cmd:
            return TodoStep(
                number=1,
                action="run",
                target=cmd,
                description="expect success",
                done=False,
                raw_line="",
            )
    return None


def try_deterministic_replan(
    memory: WorkspaceMemory,
    *,
    failure: dict,
    goal: str,
) -> list[TodoStep] | None:
    """Return open todos if a tool can fix the failure without an LLM."""
    cmd, err = _failure_blob(failure)
    fastapi = looks_fastapi_goal(goal)

    mod_m = _MOD_NOT_FOUND.search(err)
    if mod_m:
        name = mod_m.group(1).split(".", 1)[0]
        rel = memory.find_module_py(name)
        if rel:
            has_app = _file_has_app(memory, rel)
            if fastapi and not has_app:
                return [
                    TodoStep(
                        number=1,
                        action="refine",
                        target=rel,
                        description=(
                            "FastAPI app with select/selectall/add/update/delete "
                            "routes using db.json — expect success"
                        ),
                        done=False,
                        raw_line="",
                    )
                ]
            return [
                _verify_todo_for_module(
                    module=name, rel_path=rel, fastapi=fastapi, has_app=has_app
                )
            ]

    imp = _IMPORT_NAME.search(err)
    if imp:
        missing = imp.group(1)
        # from db import app — find db.py
        m = re.search(r"from\s+([\w.]+)\s+import", cmd)
        mod = (m.group(1).split(".")[0] if m else "") or missing
        rel = memory.find_module_py(mod)
        if rel and missing == "app":
            if not _file_has_app(memory, rel):
                return [
                    TodoStep(
                        number=1,
                        action="refine",
                        target=rel,
                        description=(
                            "add FastAPI app = FastAPI() and REST endpoints — expect success"
                        ),
                        done=False,
                        raw_line="",
                    )
                ]
            return [
                _verify_todo_for_module(
                    module=mod, rel_path=rel, fastapi=True, has_app=True
                )
            ]
        if rel:
            return [
                _verify_todo_for_module(
                    module=mod,
                    rel_path=rel,
                    fastapi=fastapi,
                    has_app=_file_has_app(memory, rel),
                )
            ]

    # Fallback smoke: any .py in workspace root-ish
    py_files = [
        f
        for f in memory.list_files()
        if f.endswith(".py") and not f.endswith("__init__.py")
    ]
    if py_files and ("ModuleNotFoundError" in err or "ImportError" in err):
        py_files.sort(key=lambda p: (p.count("/"), len(p)))
        rel = py_files[0]
        return [
            _verify_todo_for_module(
                module=Path(rel).stem,
                rel_path=rel,
                fastapi=fastapi,
                has_app=_file_has_app(memory, rel),
            )
        ]
    return None


def _parse_open_todo_lines(text: str) -> list[TodoStep]:
    todos: list[TodoStep] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            continue
        m = _TODO_LINE.match(stripped)
        if not m:
            continue
        mark = m.group(2).lower()
        if mark in {"x"}:  # ignore [x] spam from model
            continue
        action_raw = (m.group(3) or "").lower()
        target = (m.group(4) or m.group(5) or "").strip()
        desc = (m.group(6) or "").strip()
        if action_raw in {"run", "test"}:
            action = "run"
        elif action_raw in {"refine", "update", "fix"}:
            action = "refine"
        elif action_raw in {"create", "write"}:
            action = "create"
        else:
            action = "run" if is_valid_run_command(target) else "create"
        if action in {"create", "refine"}:
            target = strip_placeholder_path(target)
        todos.append(
            TodoStep(
                number=len(todos) + 1,
                action=action,
                target=target,
                description=desc or "expect success",
                done=False,
                raw_line=stripped,
                skipped=False,
            )
        )
        if len(todos) >= REPLAN_OPEN_CAP:
            break
    return todos


def _looks_like_spam(text: str, todos: list[TodoStep]) -> bool:
    if len(todos) > REPLAN_OPEN_CAP:
        return True
    lines = [ln for ln in (text or "").splitlines() if _TODO_LINE.match(ln.strip())]
    if len(lines) > REPLAN_OPEN_CAP + 1:
        return True
    if len((text or "")) > 1200:
        return True
    if len(todos) >= 2:
        keys = [(t.action, t.target) for t in todos]
        if len(keys) != len(set(keys)):
            return True
    return False


def _llm_open_todos(
    memory: WorkspaceMemory,
    *,
    goal: str,
    done_summary: str,
    failure: dict,
    reason: str,
) -> list[TodoStep] | None:
    cmd, err = _failure_blob(failure)
    failed_line = ""
    for t in memory.parse_todos():
        if not t.done or t.skipped:
            failed_line = (
                t.raw_line
                or f"{t.number}. [ ] {t.action} `{t.target}` — {t.description}"
            )
            break
    files = ", ".join(memory.list_files()[:40]) or "(none)"

    messages = [
        SystemMessage(content=REPLAN_SYSTEM),
        HumanMessage(
            content=(
                f"Goal: {goal}\n"
                f"Done: {done_summary or '(none)'}\n"
                f"Reason: {reason or 'failed todo does not match code'}\n"
                f"Failed todo:\n{failed_line or cmd}\n"
                f"Error (tail):\n{(err or '')[-400:] or '(none)'}\n"
                f"Workspace files: {files}\n\n"
                "Return ONLY 1 or 2 new open todo lines."
            )
        ),
    ]
    result = invoke_llm(messages)
    text = message_text(result)
    todos = _parse_open_todo_lines(text)
    if not todos or _looks_like_spam(text, todos):
        return None
    return todos


def run_replan_agent(
    memory: WorkspaceMemory,
    *,
    failure: dict | None = None,
    reason: str = "",
) -> dict:
    """Tool-first replan; optional tiny LLM for open todos only.

    Returns dict with keys: plan (str), mode (tool|llm|fallback), detail (str).
    """
    failure = failure or memory.read_last_failure() or {}
    goal = memory.plan_goal() or "(see todos)"
    todos = memory.parse_todos()
    done_summary = memory.build_done_summary(todos)
    cmd = str(failure.get("command") or "")

    open_todos = try_deterministic_replan(memory, failure=failure, goal=goal)
    mode = "tool"
    detail = "deterministic path/import repair"

    if open_todos is None:
        llm_todos = _llm_open_todos(
            memory,
            goal=goal,
            done_summary=done_summary,
            failure=failure,
            reason=reason,
        )
        if llm_todos:
            open_todos = llm_todos
            mode = "llm"
            detail = "tiny LLM open-todo rewrite"
        else:
            fallback = _fallback_verify_todo(memory, goal)
            if fallback is not None:
                open_todos = [fallback]
                mode = "fallback"
                detail = "spam/empty LLM discarded; smoke verify fallback"
            else:
                open_todos = [
                    TodoStep(
                        number=1,
                        action="run",
                        target='python3 -c "print(1)"',
                        description="expect success",
                        done=False,
                        raw_line="",
                    )
                ]
                mode = "fallback"
                detail = "no source files; noop verify"

    final = memory.write_slim_replan_plan(goal, done_summary, open_todos)
    memory.append_exec_log(
        "\n".join(
            [
                "\n## REPLAN",
                f"mode: {mode}",
                f"detail: {detail}",
                f"reason: {reason or '(plan smell)'}",
                f"failed_command: {cmd}",
                f"done: {done_summary or '(none)'}",
            ]
        )
    )
    memory.clear_last_failure()
    return {
        "plan": final.strip(),
        "mode": mode,
        "detail": detail,
        "output": final.strip(),
    }


def open_todo_summary(memory: WorkspaceMemory) -> list[TodoStep]:
    return [t for t in memory.parse_todos() if not t.done or t.skipped]
