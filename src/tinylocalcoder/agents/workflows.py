"""Compound workflows: fixed /plan prompt then /execute-plan."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

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
    from tinylocalcoder.memory.files import WorkspaceMemory

WorkflowKind = Literal["review", "test", "review-fix"]

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
1. Prefer run todos that execute real tests: `python3 -m pytest` or `python3 -m unittest discover -s tests -v` (or a specific test file if that is clearer).
2. If there are no tests, create a minimal `tests/__init__.py` + one tiny `tests/test_*.py`, then ONE run step.
3. Prefer ≤8 todos. Creates first (only if needed), then run steps. No servers. No meta commands as todos.
4. Paths/commands are workspace-relative.

Return the FULL plan.md only — that plan is the test plan to execute next."""

REVIEW_FIX_PROMPT = """Write a SHORT numbered todo plan that FIXES the issues in review.md.

review.md already exists. Use ONLY the listed files and findings. Do not invent paths.

Goal: apply the review findings as refine/fix todos on existing files.

Required shape:
1. Each todo is refine `existing/path` — fix the quoted finding. Prefer one todo per file (bundle that file's findings).
2. After refines, ONE `run python3 -m py_compile …` covering edited files. If tests exist, ONE short `run python3 -m pytest` (or unittest) smoke.
3. Do NOT create or overwrite review.md, manifest.txt, plan.md, or ask.md.
4. Prefer ≤8 todos. No servers. No meta commands as todos.
5. Skip info-only notes. Fix high and medium first; include clear lows when cheap.

Return the FULL plan.md only."""


def workflow_prompt(kind: WorkflowKind, extra: str = "") -> str:
    prompts = {
        "review": REVIEW_PROMPT,
        "test": TEST_PROMPT,
        "review-fix": REVIEW_FIX_PROMPT,
    }
    base = prompts[kind]
    note = (extra or "").strip()
    if not note:
        return base
    return f"{base}\n\nAdditional user note:\n{note}"


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
    """Review: inventory first, plan from that manifest, then execute.

    Test: plan then execute (unchanged).
    """
    if kind not in {"review", "test", "review-fix"}:
        raise ValueError(f"unknown workflow: {kind}")

    if kind == "review-fix":
        return _run_review_fix(pipeline, extra, **invoke_extra)

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
    else:
        prompt = workflow_prompt(kind, extra)
        pipeline._notify(f"thinking … {kind}: planning")
        plan_result = pipeline.invoke("plan", prompt, **invoke_extra)

    plan_out = str(plan_result.get("output") or "").strip()

    if kind == "review":
        _trace(pipeline, log_lines, "review » 4/4 executing remaining plan")
    else:
        pipeline._notify(f"thinking … {kind}: executing plan")
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
