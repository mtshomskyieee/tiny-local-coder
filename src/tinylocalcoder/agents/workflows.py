"""Compound workflows: fixed /plan prompt then /execute-plan."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from tinylocalcoder.memory.files import TodoStep, WorkspaceMemory
from tinylocalcoder.toolchains import toolchain_for_path
from tinylocalcoder.tools.manifest import (
    MANIFEST_NAME,
    fulfill_open_manifest_todos,
    is_manifest_todo,
    write_manifest,
)
from tinylocalcoder.tools.review import (
    REVIEW_NAME,
    ReviewIssue,
    format_issues_for_plan,
    fulfill_open_review_todos,
    is_review_todo,
    issues_from_review,
    write_review,
)

if TYPE_CHECKING:
    from tinylocalcoder.graph.builder import Pipeline

WorkflowKind = Literal["review", "test", "review-fix", "fix-plan"]

REVIEW_PROMPT = """Write a SHORT numbered todo plan for a code review of this workspace.

manifest.txt already exists (tool-written inventory). Use ONLY those paths.

Goal: write review comments into review.md for EACH path listed in manifest.txt.

Required shape:
1. create `review.md` — tool writes opinionated notes for each manifest path. Do NOT invent file lists; do NOT review manifest.txt/review.md themselves.
2. Prefer ≤3 todos. No refine loops. No run/find/ls todos — review.md is tool-written.
3. No long-running servers. No meta commands as todos.

Return the FULL plan.md only."""

TEST_PROMPT = """Write a SHORT numbered todo plan that IS a runnable test plan for this workspace.

Goal: find existing tests and run them (or add a tiny missing smoke test if none exist).

Required shape:
1. Prefer run todos that execute real tests in the language the workspace is already written in:
   Python `python3 -m pytest`, a Makefile `make test`, `cargo test`, `go test ./...`, `npm test`.
2. If there are no tests, create ONE tiny test in that same language, then ONE run step.
3. Prefer ≤8 todos. Creates first (only if needed), then run steps. No servers. No meta commands as todos.
4. Paths/commands are workspace-relative.

Return the FULL plan.md only — that plan is the test plan to execute next."""

REVIEW_FIX_PROMPT = """Write a SHORT numbered todo plan that FIXES the issues in review.md.

review.md already exists. Use ONLY the listed files and findings. Do not invent paths.

Goal: apply the review findings as refine/fix todos on existing files.

Required shape:
1. Each todo is refine `existing/path` — fix the quoted finding. Prefer one todo per file (bundle that file's findings).
2. After refines, ONE build/compile check for the edited files in their own language
   (`python3 -m py_compile …`, `make`, `g++ -Wall -o …`, `cargo build`). If tests exist, ONE short test run.
3. Do NOT create or overwrite review.md, manifest.txt, plan.md, or ask.md.
4. Prefer ≤8 todos. No servers. No meta commands as todos.
5. Skip info-only notes. Fix high and medium first; include clear lows when cheap.

Return the FULL plan.md only."""

FIX_PLAN_PROMPT = """Rewrite plan.md so the remaining todos actually deliver the user REQUIREMENT.

You will see: the requirement, current plan.md, workspace files, and GAP notes from tools.

Required shape:
1. Goal is the user's requirement (one sentence). Keep finished work in a one-line Done: note; do not re-create files that already exist — use refine.
2. Every GAP note must become an open refine/run todo (or be covered by one).
3. A start_*.sh that does not run Python must be refined so it imports the FastAPI `app` and prints routes, then EXITS. Never hang on uvicorn.
4. Missing endpoints in the requirement → refine the existing FastAPI file (do not invent src/main.py unless no app file exists).
5. Prefer ≤8 todos. Refines first, then ONE compile/build check, then ONE short behavioral run.
   For a FastAPI app that smoke is `python3 -c "… app.routes"` or `bash start_*.sh` (expect a route path).
6. No long-running servers. No meta commands as todos.

Return the FULL plan.md only."""


def workflow_prompt(kind: WorkflowKind, extra: str = "") -> str:
    prompts = {
        "review": REVIEW_PROMPT,
        "test": TEST_PROMPT,
        "review-fix": REVIEW_FIX_PROMPT,
        "fix-plan": FIX_PLAN_PROMPT,
    }
    base = prompts[kind]
    note = (extra or "").strip()
    if not note:
        return base
    return f"{base}\n\nAdditional user note:\n{note}"


_ENDPOINT_HINTS = (
    "selectall",
    "select",
    "insert",
    "update",
    "delete",
    "showtables",
    "show_tables",
)


def plan_requirement_gaps(memory: WorkspaceMemory, requirement: str) -> list[str]:
    """Deterministic gaps: stub start scripts and missing required endpoints."""
    gaps: list[str] = []
    req = (requirement or "").lower()
    files = [
        p.replace("\\", "/")
        for p in memory.list_files()
        if ".index" not in p.replace("\\", "/").split("/")
        and not p.replace("\\", "/").startswith("archive/")
    ]
    py_blob = ""
    for rel in files:
        if rel.endswith(".py"):
            try:
                py_blob += "\n" + memory.read_prototype(rel)
            except OSError:
                continue
    py_l = py_blob.lower()
    py_compact = re.sub(r"[^a-z0-9]", "", py_l)
    req_compact = re.sub(r"[^a-z0-9]", "", req)
    for rel in files:
        name = Path(rel).name.lower()
        if name.endswith(".sh") and "start" in name:
            text = ""
            try:
                text = memory.read_prototype(rel)
            except OSError:
                text = ""
            if not re.search(r"python|uvicorn|fastapi", text, re.IGNORECASE):
                gaps.append(
                    f"`{rel}` exists but does not start or check the app "
                    "(no python/uvicorn/fastapi)"
                )
    for word in _ENDPOINT_HINTS:
        key = re.sub(r"[^a-z0-9]", "", word)
        if key not in req_compact:
            continue
        if key in py_compact or f"/{word}" in py_l:
            continue
        if word in {"showtables", "show_tables"} and "/tables" in py_l:
            continue
        gaps.append(
            f"requirement mentions `{word}` but no matching route/function is in source"
        )
    if not memory.plan_goal().strip() or memory.plan_goal().strip() in {"(none)", "none"}:
        if requirement.strip():
            gaps.append("plan Goal is empty — set Goal from the user requirement")
    if not memory.parse_todos() and requirement.strip():
        gaps.append("plan has no todos")
    return gaps


def _run_fix_plan(
    pipeline: Pipeline,
    extra: str,
    **invoke_extra: Any,
) -> dict[str, Any]:
    """Compare requirement vs plan.md, rewrite the plan (does not execute)."""
    log_lines: list[str] = []
    _trace(pipeline, log_lines, "fix-plan » 1/3 reading requirement and plan.md")
    requirement = pipeline.memory.last_user_requirement(extra)
    current = pipeline.memory.read_plan().strip()
    files = [
        p
        for p in pipeline.memory.list_files()
        if not p.replace("\\", "/").startswith(("archive/", ".index/"))
    ]
    req_one = re.sub(r"\s+", " ", requirement).strip()
    _trace(
        pipeline,
        log_lines,
        f"fix-plan » requirement: {req_one[:120] or '(none)'}",
    )
    if current:
        _trace(pipeline, log_lines, "fix-plan » current plan.md:")
        for line in current.splitlines()[:20]:
            _trace(pipeline, log_lines, f"fix-plan »   {line}")
    else:
        _trace(pipeline, log_lines, "fix-plan » plan.md is empty")

    _trace(pipeline, log_lines, "fix-plan » 2/3 checking plan against requirement")
    gaps = plan_requirement_gaps(pipeline.memory, requirement)
    if gaps:
        _trace(pipeline, log_lines, f"fix-plan » {len(gaps)} gap(s)")
        for gap in gaps:
            _trace(pipeline, log_lines, f"fix-plan »   • {gap}")
    else:
        _trace(pipeline, log_lines, "fix-plan » no structural gaps flagged")

    listed_files = "\n".join(f"- {p}" for p in files[:40]) or "(none)"
    listed_gaps = "\n".join(f"- {g}" for g in gaps) or "(none)"
    prompt = (
        f"{workflow_prompt('fix-plan', extra)}\n\n"
        f"REQUIREMENT:\n{requirement or '(not recorded — use Additional user note)'}\n\n"
        f"Current plan.md:\n{current or '(empty)'}\n\n"
        f"Workspace files:\n{listed_files}\n\n"
        f"GAP notes (must be addressed):\n{listed_gaps}\n"
    )
    _trace(pipeline, log_lines, "fix-plan » 3/3 rewriting plan.md")
    plan_result = pipeline.invoke("plan", prompt, **invoke_extra)
    plan_out = str(plan_result.get("output") or "").strip()
    plan_text = pipeline.memory.read_plan().strip()
    todos = pipeline.memory.parse_todos()
    _trace(pipeline, log_lines, f"fix-plan » plan.md ready ({len(todos)} step(s))")
    for todo in todos:
        _trace(pipeline, log_lines, f"fix-plan »   • {_todo_bullet(todo)}")

    return {
        "workflow": "fix-plan",
        "log_lines": log_lines,
        "requirement": requirement,
        "gaps": gaps,
        "plan_output": plan_out,
        "plan_text": plan_text,
        "output": plan_out or plan_text,
        "last_file": "plan.md",
        "progress": pipeline.memory.progress_summary(),
        "skipped_execute": True,
    }


def _close_review_file_todos(memory: WorkspaceMemory) -> None:
    """Mark leftover manifest/review creates done so execute cannot overwrite them."""
    for todo in list(memory.pending_todos()):
        if is_manifest_todo(todo) or is_review_todo(todo):
            memory.mark_todo_done(todo.number)


def _review_body(memory: WorkspaceMemory) -> str:
    if not memory.prototype_exists(REVIEW_NAME):
        return ""
    return memory.read_prototype(REVIEW_NAME).strip()


def _trace(pipeline: Pipeline, log_lines: list[str], message: str) -> None:
    log_lines.append(message)
    pipeline._notify(message)


def _write_manifest(
    pipeline: Pipeline, extra: str = "", log_lines: list[str] | None = None
) -> list[str]:
    memory = pipeline.memory
    notes = log_lines if log_lines is not None else []
    _trace(pipeline, notes, "review » 1/4 writing manifest.txt")
    fulfill_open_manifest_todos(memory, focus_hint=extra)
    _name, paths = write_manifest(memory, focus_hint=extra)
    _trace(
        pipeline,
        notes,
        f"review » manifest.txt ready ({len(paths)} path{'s' if len(paths) != 1 else ''})",
    )
    for path in paths:
        _trace(pipeline, notes, f"review »   • {path}")
    return paths


def _write_review(
    pipeline: Pipeline, extra: str = "", log_lines: list[str] | None = None
) -> None:
    memory = pipeline.memory
    notes = log_lines if log_lines is not None else []
    _trace(pipeline, notes, "review » 3/4 reviewing each manifest path")
    fulfill_open_review_todos(memory, focus_hint=extra)
    _out, reviews = write_review(memory, focus_hint=extra)
    for item in reviews:
        worst = item.worst or "ok"
        n = len(item.findings)
        _trace(
            pipeline,
            notes,
            f"review »   • {item.path}  [{worst}, {n} finding{'s' if n != 1 else ''}]",
        )
    highs = sum(1 for r in reviews for f in r.findings if f.severity == "high")
    _trace(
        pipeline,
        notes,
        f"review » review.md saved ({len(reviews)} files, {highs} high)",
    )
    _close_review_file_todos(memory)


def _load_review_for_fix(
    pipeline: Pipeline, extra: str, log_lines: list[str]
) -> tuple[str, list[ReviewIssue]]:
    memory = pipeline.memory
    _trace(pipeline, log_lines, "review-fix » 1/3 loading review.md")
    if not _review_body(memory):
        _trace(pipeline, log_lines, "review-fix » review.md missing — writing from manifest")
        write_review(memory, focus_hint=extra)
    _close_review_file_todos(memory)
    body = _review_body(memory)
    issues = issues_from_review(memory)
    _trace(
        pipeline,
        log_lines,
        f"review-fix » review.md ready ({len(issues)} actionable finding"
        f"{'' if len(issues) == 1 else 's'})",
    )
    for issue in issues:
        bullet = issue.format_plan_line()
        if bullet.startswith("- "):
            bullet = bullet[2:]
        _trace(pipeline, log_lines, f"review-fix »   • {bullet}")
    return body, issues


_TEST_NAME_RE = re.compile(r"(?:^|[_./-])tests?(?:[_.-]|$)", re.IGNORECASE)


def _list_workspace_tests(memory: WorkspaceMemory) -> list[str]:
    """Workspace-relative test files, in any registered language.

    Previously this only matched `test_*.py`, so a `test_square_root.cpp` or a
    `make test` target was invisible to /test.
    """
    found: list[str] = []
    for rel in memory.list_files():
        norm = rel.replace("\\", "/")
        name = Path(norm).name
        if name == "__init__.py" or toolchain_for_path(norm) is None:
            continue
        if _TEST_NAME_RE.search(Path(name).stem):
            found.append(norm)
            continue
        if "/tests/" in f"/{norm}/":
            found.append(norm)
    return found


def _todo_bullet(todo: TodoStep) -> str:
    mark = "!" if getattr(todo, "skipped", False) else ("x" if todo.done else " ")
    desc = f" — {todo.description}" if todo.description else ""
    return f"{todo.number}. [{mark}] {todo.action} `{todo.target}`{desc}"


def _run_test(
    pipeline: Pipeline,
    extra: str,
    **invoke_extra: Any,
) -> dict[str, Any]:
    log_lines: list[str] = []
    _trace(pipeline, log_lines, "test » 1/3 looking for existing tests")
    tests = _list_workspace_tests(pipeline.memory)
    if tests:
        _trace(
            pipeline,
            log_lines,
            f"test » found {len(tests)} test file{'s' if len(tests) != 1 else ''}",
        )
        for path in tests:
            _trace(pipeline, log_lines, f"test »   • {path}")
    else:
        _trace(pipeline, log_lines, "test » no test files yet — plan may add a smoke test")

    listed = "\n".join(tests) if tests else "(none found)"
    prompt = (
        f"{workflow_prompt('test', extra)}\n\n"
        f"Existing test files (prefer running these; do not invent extra suites):\n"
        f"{listed}\n"
    )
    _trace(pipeline, log_lines, "test » 2/3 planning how tests will run")
    plan_result = pipeline.invoke("plan", prompt, **invoke_extra)
    plan_out = str(plan_result.get("output") or "").strip()
    todos = pipeline.memory.parse_todos()
    _trace(pipeline, log_lines, f"test » plan.md ready ({len(todos)} step(s))")
    for todo in todos:
        _trace(pipeline, log_lines, f"test »   • {_todo_bullet(todo)}")

    prior_prefix = getattr(pipeline, "_trace_prefix", "")
    pipeline._trace_prefix = "test"
    try:
        _trace(pipeline, log_lines, "test » 3/3 executing test plan")
        exec_result = pipeline.invoke("execute", "", **invoke_extra)
    finally:
        pipeline._trace_prefix = prior_prefix

    todos = pipeline.memory.parse_todos()
    done = sum(1 for t in todos if t.done)
    skipped = sum(1 for t in todos if getattr(t, "skipped", False))
    open_n = sum(1 for t in todos if not t.done and not getattr(t, "skipped", False))
    _trace(
        pipeline,
        log_lines,
        f"test » finished ({done} done, {skipped} skipped, {open_n} open)",
    )
    for todo in todos:
        _trace(pipeline, log_lines, f"test »   • {_todo_bullet(todo)}")

    merged = dict(exec_result)
    merged["workflow"] = "test"
    merged["log_lines"] = log_lines
    merged["plan_output"] = plan_out
    merged["plan_text"] = pipeline.memory.read_plan().strip()
    merged["test_files"] = tests
    merged["progress"] = pipeline.memory.progress_summary()
    if not merged.get("last_file"):
        merged["last_file"] = plan_result.get("last_file") or "plan.md"
    exec_out = str(exec_result.get("output") or "").strip()
    bits = [b for b in (plan_out, exec_out) if b]
    merged["output"] = (
        "\n\n".join(bits) if bits else "Test workflow: planned and executed tests."
    )
    return merged


def _run_review_fix(
    pipeline: Pipeline,
    extra: str,
    **invoke_extra: Any,
) -> dict[str, Any]:
    log_lines: list[str] = []
    body, issues = _load_review_for_fix(pipeline, extra, log_lines)
    listed = format_issues_for_plan(issues)
    merged: dict[str, Any] = {
        "workflow": "review-fix",
        "log_lines": log_lines,
        "review_markdown": body,
        "issue_count": len(issues),
        "issues_text": listed,
        "plan_output": "",
        "plan_text": "",
        "output": "",
        "last_file": REVIEW_NAME,
        "progress": pipeline.memory.progress_summary(),
    }
    if not issues:
        _trace(pipeline, log_lines, "review-fix » no actionable findings — nothing to fix")
        merged["output"] = (
            "Review-fix: review.md has no high/medium/low findings to apply."
        )
        merged["skipped_execute"] = True
        return merged

    prompt = (
        f"{workflow_prompt('review-fix', extra)}\n\n"
        f"Actionable findings from `{REVIEW_NAME}` (do not invent paths):\n{listed}\n"
    )
    _trace(pipeline, log_lines, "review-fix » 2/3 planning fixes from review.md")
    plan_result = pipeline.invoke("plan", prompt, **invoke_extra)
    plan_out = str(plan_result.get("output") or "").strip()
    plan_text = pipeline.memory.read_plan().strip()
    merged["plan_output"] = plan_out
    merged["plan_text"] = plan_text
    n_todos = pipeline.memory.progress_summary().get("todos_total", 0)
    _trace(pipeline, log_lines, f"review-fix » plan.md ready ({n_todos} todo(s))")

    _trace(pipeline, log_lines, "review-fix » 3/3 executing fix plan")
    exec_result = pipeline.invoke("execute", "", **invoke_extra)
    merged.update(exec_result)
    merged["workflow"] = "review-fix"
    merged["log_lines"] = log_lines
    merged["review_markdown"] = body
    merged["issue_count"] = len(issues)
    merged["issues_text"] = listed
    merged["plan_output"] = plan_out
    merged["plan_text"] = pipeline.memory.read_plan().strip()
    merged["skipped_execute"] = False
    merged["progress"] = pipeline.memory.progress_summary()
    if not merged.get("last_file"):
        merged["last_file"] = plan_result.get("last_file") or "plan.md"
    exec_out = str(exec_result.get("output") or "").strip()
    bits = [b for b in (plan_out, exec_out) if b]
    merged["output"] = (
        "\n\n".join(bits)
        if bits
        else "Review-fix: planned and executed fixes from review.md."
    )
    return merged


def run_workflow(
    pipeline: Pipeline,
    kind: WorkflowKind,
    extra: str = "",
    **invoke_extra: Any,
) -> dict[str, Any]:
    """Review / review-fix / test: tool prep, plan, then execute."""
    if kind not in {"review", "test", "review-fix", "fix-plan"}:
        raise ValueError(f"unknown workflow: {kind}")

    if kind == "review-fix":
        return _run_review_fix(pipeline, extra, **invoke_extra)
    if kind == "fix-plan":
        return _run_fix_plan(pipeline, extra, **invoke_extra)
    if kind == "test":
        return _run_test(pipeline, extra, **invoke_extra)

    log_lines: list[str] = []
    if kind == "review":
        # Contract: inventory first, plan from that list, then execute.
        _write_manifest(pipeline, extra, log_lines)
        manifest_txt = ""
        if pipeline.memory.prototype_exists(MANIFEST_NAME):
            manifest_txt = pipeline.memory.read_prototype(MANIFEST_NAME).strip()
        listed = manifest_txt or "(no source files in workspace)"
        prompt = (
            f"{workflow_prompt(kind, extra)}\n\n"
            f"Existing `{MANIFEST_NAME}` (do not invent paths):\n{listed}\n"
        )
        _trace(pipeline, log_lines, "review » 2/4 planning from manifest")
        plan_result = pipeline.invoke("plan", prompt, **invoke_extra)
        _write_review(pipeline, extra, log_lines)

    plan_out = str(plan_result.get("output") or "").strip()

    _trace(pipeline, log_lines, "review » 4/4 executing remaining plan")
    exec_result = pipeline.invoke("execute", "", **invoke_extra)

    if kind == "review" and not _review_body(pipeline.memory):
        _trace(pipeline, log_lines, "review » restoring review.md (execute wiped it)")
        write_review(pipeline.memory, focus_hint=extra)
        _close_review_file_todos(pipeline.memory)

    bits: list[str] = []
    if plan_out:
        bits.append(plan_out)
    exec_out = str(exec_result.get("output") or "").strip()
    if exec_out:
        bits.append(exec_out)

    merged = dict(exec_result)
    merged["workflow"] = kind
    merged["plan_output"] = plan_out
    merged["output"] = "\n\n".join(bits) if bits else merged.get("output") or ""
    if plan_result.get("last_file") and not merged.get("last_file"):
        merged["last_file"] = plan_result.get("last_file")
    merged["progress"] = pipeline.memory.progress_summary()

    merged["log_lines"] = log_lines
    if kind == "review":
        body = _review_body(pipeline.memory)
        if not body:
            write_review(pipeline.memory, focus_hint=extra)
            _close_review_file_todos(pipeline.memory)
            body = _review_body(pipeline.memory)
        manifest_txt = ""
        if pipeline.memory.prototype_exists(MANIFEST_NAME):
            manifest_txt = pipeline.memory.read_prototype(MANIFEST_NAME).strip()
        merged["last_file"] = REVIEW_NAME
        merged["review_markdown"] = body
        merged["manifest_text"] = manifest_txt
        if body:
            merged["output"] = f"Review complete — `{REVIEW_NAME}`:\n\n{body}"
        else:
            merged["output"] = (
                f"Review finished but `{REVIEW_NAME}` is still empty. "
                "Check that the workspace has source files to inventory."
            )

    return merged
