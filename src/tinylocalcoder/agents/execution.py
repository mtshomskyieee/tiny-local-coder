"""Gated execution agent — one run todo at a time."""

from __future__ import annotations

import re

from tinylocalcoder.exec.runner import CommandRunner, RunResult
from tinylocalcoder.memory.files import TodoStep, WorkspaceMemory


_EXPECT_RE = re.compile(
    r"(?:^|\b)expect(?:ing)?\s+(\S.+?)(?=\s+[—-]\s+SKIPPED\b|\s*[.;]|$)",
    re.IGNORECASE,
)
_SKIPPED_TAIL_RE = re.compile(r"\s+[—-]\s+SKIPPED\b.*$", re.IGNORECASE)

# Expect clauses that only assert "the command worked"
_EXIT_ONLY = frozenset(
    {"success", "ok", "exit 0", "exit=0", "exit code 0", "no error", "no errors", "succeed"}
)


def _clean_expect(raw: str) -> str:
    """Strip skip markers / trailing junk from an expect clause."""
    text = _SKIPPED_TAIL_RE.sub("", (raw or "").strip())
    # Stop at a second em-dash fragment if present
    text = re.split(r"\s+[—]\s+", text, maxsplit=1)[0].strip()
    text = text.strip().rstrip(".;")
    return text


def parse_expectation(todo: TodoStep) -> str | None:
    """Pull the 'expect …' clause from the todo description or raw line."""
    for blob in (todo.description or "", todo.raw_line or ""):
        cleaned = _SKIPPED_TAIL_RE.sub("", blob)
        m = re.search(
            r"(?:—|--|-)\s*expect(?:ing)?\s+(.+)$", cleaned, re.IGNORECASE
        )
        if m:
            return _clean_expect(m.group(1)) or None
        m = _EXPECT_RE.search(cleaned)
        if m:
            return _clean_expect(m.group(1)) or None
    return None


def expectation_met(expect: str | None, *, exit_code: int | None, stdout: str, stderr: str) -> tuple[bool, str]:
    """Return (ok, detail). Exit must be 0 first; then match expect against stdout."""
    if exit_code != 0:
        return False, f"exit code {exit_code}"

    if not expect:
        return True, "exit 0 (no expect clause)"

    exp = _clean_expect(expect)
    # Boolean expects: only the first token matters ("True", not "True — …")
    first = exp.split()[0] if exp.split() else exp
    exp_l = first.lower().rstrip(",.;")
    out = (stdout or "").strip()
    out_l = out.lower()
    combined = f"{stdout}\n{stderr}"

    # exit-only expectations — check the whole clause too, so multi-word
    # phrasings ("expect exit 0", "expect no error") are not dropped into
    # substring matching and failed on a perfectly clean run.
    exp_full = exp.lower().strip().rstrip(",.;")
    if exp_full in _EXIT_ONLY or exp_l in _EXIT_ONLY:
        return True, "expect success (exit 0)"

    # boolean print checks — common in verify todos
    if exp_l in {"true", "false"}:
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        last = (lines[-1] if lines else out_l).lower()
        if last == exp_l or out_l == exp_l or out_l.endswith(exp_l):
            return True, f"stdout matches expect {first}"
        return False, f"expected stdout {first!r}, got {out!r}"

    # path / substring expectations (e.g. /getbeer, 8888, beer)
    needle = re.sub(
        r"^(?:a |an |the |json response with |that |to see )+",
        "",
        exp,
        flags=re.IGNORECASE,
    ).strip()
    quoted = re.search(r"[`'\"]([^`'\"]+)[`'\"]", needle)
    if quoted:
        needle = quoted.group(1)
    elif "/" in needle and " " in needle:
        path_tok = re.search(r"(/[\w/-]+)", needle)
        if path_tok:
            needle = path_tok.group(1)

    if needle and needle.lower() in combined.lower():
        return True, f"output contains {needle!r}"

    if len(needle) <= 40 and needle:
        for ln in [ln.strip() for ln in out.splitlines() if ln.strip()] or [out]:
            if needle.lower() in ln.lower():
                return True, f"stdout contains {needle!r}"
        return False, f"expected output containing {needle!r}, got {out!r}"

    return True, "expect clause too vague; accepted exit 0"


def run_execution_step(
    memory: WorkspaceMemory,
    runner: CommandRunner,
    todo: TodoStep,
    prompt: str = "",
) -> dict:
    command = WorkspaceMemory.normalize_run_command(todo.target)
    reason = todo.description or prompt or f"todo step {todo.number}"
    result: RunResult = runner.run(command, reason=reason)

    if not result.allowed:
        return {
            "output": (
                f"Step {todo.number}: `{todo.target}` decision={result.decision} (not run)"
            ),
            "phase": "executing",
            "last_command": todo.target,
            "pending_commands": [t.target for t in memory.pending_todos() if t.action == "run"],
            "status": "denied",
            "ran_shell": True,
        }

    expect = parse_expectation(todo)
    ok, detail = expectation_met(
        expect,
        exit_code=result.exit_code,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )
    if ok and not result.error:
        memory.mark_todo_done(todo.number)
        memory.clear_last_failure()
        status = "ok"
        note = f"\nExpectation: {detail}" if expect else ""
    else:
        status = "failed"
        fail_reason = result.error or detail
        memory.write_last_failure(
            {
                "step": todo.number,
                "command": command,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "error": fail_reason,
                "todo_raw": todo.raw_line,
                "pid": result.pid,
                "expect": expect or "",
            }
        )
        note = (
            f"\nExpectation failed: {fail_reason}"
            if expect and result.exit_code == 0 and not result.error
            else (
                "\nCommand failed. "
                "If auto-fix is on it will repair and retry once; "
                "if still failing and auto-skip is on the step will be marked [!] skipped."
            )
        )

    remaining = [
        t.target
        for t in memory.parse_todos()
        if not t.done and t.action in {"run", "test"}
    ]
    return {
        "output": (
            f"Step {todo.number}: `{todo.target}` decision={result.decision} "
            f"exit={result.exit_code}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            + (f"\nerror: {result.error}" if result.error else "")
            + note
        ),
        "phase": "done" if not remaining or status != "ok" else "executing",
        "last_command": todo.target,
        "pending_commands": remaining,
        "status": status,
        "ran_shell": True,
    }


def run_execution_agent(memory: WorkspaceMemory, runner: CommandRunner, prompt: str = "") -> dict:
    pending = [t for t in memory.parse_todos() if not t.done and t.action in {"run", "test"}]
    if not pending:
        return {
            "output": "No pending run todos.",
            "phase": "done",
            "last_command": "",
            "pending_commands": [],
            "status": "ok",
            "ran_shell": False,
        }
    return run_execution_step(memory, runner, pending[0], prompt)
