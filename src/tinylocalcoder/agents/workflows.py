"""Compound workflows: fixed /plan prompt then /execute-plan."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from tinylocalcoder.tools.manifest import fulfill_open_manifest_todos
from tinylocalcoder.tools.review import fulfill_open_review_todos

if TYPE_CHECKING:
    from tinylocalcoder.graph.builder import Pipeline

WorkflowKind = Literal["review", "test"]

REVIEW_PROMPT = """Write a SHORT numbered todo plan for a code review of this workspace.

Goal: inventory project files into manifest.txt, then write review comments into review.md.

Required shape:
1. create `manifest.txt` — tool inventory (skip archive/, .index/, plan.md, ask.md, exec.log, session.md). Do NOT invent paths.
2. create `review.md` — tool writes opinionated notes for EACH path in manifest.txt. Do NOT invent file lists; do NOT review manifest.txt/review.md themselves.
3. Prefer ≤4 todos. No refine loops for review.md. No run/find/ls todos — both files are tool-written.
4. No long-running servers. No meta commands as todos.

Return the FULL plan.md only."""

TEST_PROMPT = """Write a SHORT numbered todo plan that IS a runnable test plan for this workspace.

Goal: find existing tests and run them (or add a tiny missing smoke test if none exist).

Required shape:
1. Prefer run todos that execute real tests: `python3 -m pytest` or `python3 -m unittest discover -s tests -v` (or a specific test file if that is clearer).
2. If there are no tests, create a minimal `tests/__init__.py` + one tiny `tests/test_*.py`, then ONE run step.
3. Prefer ≤8 todos. Creates first (only if needed), then run steps. No servers. No meta commands as todos.
4. Paths/commands are workspace-relative.

Return the FULL plan.md only — that plan is the test plan to execute next."""


def workflow_prompt(kind: WorkflowKind, extra: str = "") -> str:
    base = REVIEW_PROMPT if kind == "review" else TEST_PROMPT
    note = (extra or "").strip()
    if not note:
        return base
    return f"{base}\n\nAdditional user note:\n{note}"


def run_workflow(
    pipeline: Pipeline,
    kind: WorkflowKind,
    extra: str = "",
    **invoke_extra: Any,
) -> dict[str, Any]:
    """Run existing plan agent, then execute the resulting plan.md."""
    if kind not in {"review", "test"}:
        raise ValueError(f"unknown workflow: {kind}")
    prompt = workflow_prompt(kind, extra)
    pipeline._notify(f"thinking … {kind}: planning")
    plan_result = pipeline.invoke("plan", prompt, **invoke_extra)
    plan_out = str(plan_result.get("output") or "").strip()

    # Review inventory + notes are deterministic — fulfill before LLM coding can stall.
    if kind == "review":
        pipeline._notify("thinking … review: writing manifest.txt (tool)")
        tool_hits = fulfill_open_manifest_todos(pipeline.memory, focus_hint=extra)
        if tool_hits:
            n = tool_hits[-1].get("manifest_count", 0)
            pipeline._notify(f"thinking … review: manifest ready ({n} paths)")

        pipeline._notify("thinking … review: writing review.md (tool)")
        review_hits = fulfill_open_review_todos(pipeline.memory, focus_hint=extra)
        if review_hits:
            n = review_hits[-1].get("review_files", 0)
            h = review_hits[-1].get("review_high", 0)
            pipeline._notify(f"thinking … review: review.md ready ({n} files, {h} high)")
        elif not tool_hits:
            # Plan omitted todos — still produce artifacts.
            from tinylocalcoder.tools.manifest import write_manifest
            from tinylocalcoder.tools.review import write_review

            write_manifest(pipeline.memory, focus_hint=extra)
            write_review(pipeline.memory, focus_hint=extra)
            pipeline._notify("thinking … review: wrote manifest.txt + review.md (tool fallback)")

    pipeline._notify(f"thinking … {kind}: executing plan")
    exec_result = pipeline.invoke("execute", "", **invoke_extra)

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

    # Review deliverable is the document — surface it as the primary output.
    if kind == "review":
        from tinylocalcoder.tools.review import REVIEW_NAME

        body = pipeline.memory.read_prototype(REVIEW_NAME).strip()
        merged["last_file"] = REVIEW_NAME
        merged["review_markdown"] = body
        if body:
            merged["output"] = f"Review complete — `{REVIEW_NAME}`:\n\n{body}"
        else:
            merged["output"] = (
                merged.get("output") or f"Review finished but `{REVIEW_NAME}` is empty."
            )

    return merged
