"""Code-writing helpers: fence stripping and path extraction.

A 3B model wraps files in markdown fences and mentions paths in prose; if
either helper slips, the workspace gets a file full of ``` or a file named
after a shell command.
"""

from __future__ import annotations

from tinylocalcoder.agents.coding import (
    _paths_for_todo,
    _strip_fences,
    pending_file_paths,
)
from tinylocalcoder.memory.files import TodoStep, WorkspaceMemory


def _todo(action: str, target: str, raw: str = "") -> TodoStep:
    return TodoStep(
        number=1,
        action=action,
        target=target,
        description="",
        done=False,
        raw_line=raw,
    )


def test_strip_fences_removes_language_tag() -> None:
    assert _strip_fences("```python\nx = 1\n```") == "x = 1"


def test_strip_fences_removes_plain_fence() -> None:
    assert _strip_fences("```\nx = 1\n```").strip() == "x = 1"


def test_strip_fences_leaves_plain_code_alone() -> None:
    assert _strip_fences("x = 1\n") == "x = 1"


def test_strip_fences_keeps_inner_fences_of_prose_file() -> None:
    """A README legitimately contains fences — only the wrapper comes off."""
    body = "# Doc\n\n```bash\nls\n```\n"
    assert "ls" in _strip_fences(f"```markdown\n{body}```")


def test_paths_for_todo_puts_the_target_first() -> None:
    todo = _todo("create", "b.py", "1. [ ] create `b.py` and `db.json` — json db")
    assert _paths_for_todo(todo)[0] == "b.py"
    assert "db.json" in _paths_for_todo(todo)


def test_paths_for_todo_normalizes_absolute_and_quoted_paths() -> None:
    todo = _todo("create", "/src/api.py", "1. [ ] create `/src/api.py`")
    assert _paths_for_todo(todo) == ["src/api.py"]


def test_paths_for_todo_drops_shell_lookalikes() -> None:
    todo = _todo(
        "create",
        "b.py",
        "1. [ ] create `b.py` — verify with `python3 -m py_compile b.py`",
    )
    paths = _paths_for_todo(todo)
    assert paths[0] == "b.py"
    assert not any(p.startswith(("python", "curl", "http")) for p in paths)


def test_paths_for_todo_always_returns_the_target_as_fallback() -> None:
    assert _paths_for_todo(_todo("create", "Makefile", "1. [ ] create Makefile")) == [
        "Makefile"
    ]


def test_pending_file_paths_lists_only_open_creates(write_plan) -> None:
    mem: WorkspaceMemory = write_plan(
        "# Plan\nGoal: g\n\n## Todos\n"
        "1. [x] create `a.py` — done\n"
        "2. [ ] create `b.py` — todo\n"
        "3. [ ] refine `c.py` — todo\n"
        "4. [ ] run `python3 -m py_compile b.py` — expect success\n"
    )
    assert pending_file_paths(mem) == ["b.py", "c.py"]
