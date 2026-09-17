"""Plan parsing and the [ ] / [x] / [!] state machine on disk.

Disk is the shared state between graph nodes, so these transitions are the
real contract between the plan, code and execute nodes.
"""

from __future__ import annotations

import pytest

from tinylocalcoder.memory.files import WorkspaceMemory

PLAN = """# Plan
Goal: build b.py

## Todos
1. [ ] create `b.py` — json db
2. [ ] run `python3 -m py_compile b.py` — expect success
3. [ ] run `python3 -c "import b"` — expect success
"""


def test_parse_todos_reads_actions_targets_and_marks(write_plan) -> None:
    mem = write_plan(PLAN)
    todos = mem.parse_todos()
    assert [t.number for t in todos] == [1, 2, 3]
    assert todos[0].action == "create"
    assert todos[0].target == "b.py"
    assert todos[0].description == "json db"
    assert todos[1].action == "run"
    assert all(not t.done and not t.skipped for t in todos)
    assert todos[0].kind == "deliverable"
    assert todos[1].kind == "run_test"


def test_parse_todos_infers_action_when_verb_omitted(write_plan) -> None:
    mem = write_plan(
        "# Plan\nGoal: g\n\n## Todos\n"
        "1. [ ] `src/api.py` — module\n"
        "2. [ ] `python3 -m py_compile src/api.py` — expect success\n"
    )
    todos = mem.parse_todos()
    assert todos[0].action == "create"
    assert todos[1].action == "run"


def test_parse_todos_normalizes_absolute_paths_and_commands(write_plan) -> None:
    """Models emit /src/api.py; the workspace is the root, so it must be relative."""
    mem = write_plan(
        "# Plan\nGoal: g\n\n## Todos\n"
        "1. [ ] create `/src/api.py` — module\n"
        "2. [ ] run `python3 -m py_compile /src/api.py` — expect success\n"
    )
    todos = mem.parse_todos()
    assert todos[0].target == "src/api.py"
    assert todos[1].target == "python3 -m py_compile src/api.py"


def test_mark_done_and_skipped_round_trip(write_plan) -> None:
    mem = write_plan(PLAN)
    mem.mark_todo_done(1)
    mem.mark_todo_skipped(3, "junk command")

    todos = {t.number: t for t in mem.parse_todos()}
    assert todos[1].done and not todos[1].skipped
    assert todos[3].skipped and todos[3].done  # [!] counts as "not open"
    assert not todos[2].done
    # Skip reason goes to exec.log, never onto the expect clause
    assert "junk command" in mem.read_exec_log()
    assert todos[3].description == "expect success"


def test_mark_done_is_idempotent_and_targeted(write_plan) -> None:
    mem = write_plan(PLAN)
    mem.mark_todo_done(1)
    first = mem.read_plan()
    mem.mark_todo_done(1)
    assert mem.read_plan() == first


def test_clear_todo_skip_reopens_one(write_plan) -> None:
    mem = write_plan(PLAN)
    mem.mark_todo_skipped(2)
    mem.clear_todo_skip(2)
    todos = {t.number: t for t in mem.parse_todos()}
    assert not todos[2].skipped and not todos[2].done


def test_clear_all_todo_skips_returns_numbers(write_plan) -> None:
    mem = write_plan(PLAN)
    mem.mark_todo_skipped(2)
    mem.mark_todo_skipped(3)
    assert mem.clear_all_todo_skips() == [2, 3]
    assert mem.clear_all_todo_skips() == []
    assert all(not t.skipped for t in mem.parse_todos())


def test_skip_markers_are_stripped_from_expect_on_reopen(write_plan) -> None:
    """A stale `— SKIPPED` tail used to break stdout matching on retry."""
    mem = write_plan(
        "# Plan\nGoal: g\n\n## Todos\n"
        "1. [!] run `python3 -c \"print(True)\"` — expect True — SKIPPED after failure\n"
    )
    mem.clear_todo_skip(1)
    todo = mem.parse_todos()[0]
    assert "SKIPPED" not in todo.raw_line
    assert todo.description == "expect True"


def test_next_todo_returns_refine_for_existing_file(write_plan) -> None:
    """A create whose file already exists must not be silently marked done."""
    mem = write_plan(PLAN)
    mem.write_prototype("b.py", "x = 1\n")
    nxt = mem.next_todo()
    assert nxt is not None
    assert nxt.number == 1
    assert nxt.action == "refine"
    assert nxt.description  # carries a fallback hint for the coding agent


def test_next_todo_skips_completed_and_returns_none_when_finished(write_plan) -> None:
    mem = write_plan(PLAN)
    mem.mark_todo_done(1)
    assert mem.next_todo().number == 2
    mem.mark_todo_done(2)
    mem.mark_todo_skipped(3)
    assert mem.next_todo() is None


def test_progress_summary_counts_skips_separately(write_plan) -> None:
    mem = write_plan(PLAN)
    mem.mark_todo_done(2)
    mem.mark_todo_skipped(3)
    p = mem.progress_summary()
    assert p["todos_total"] == 3
    assert p["todos_done"] == 1
    assert p["todos_skipped"] == 1
    assert p["cmds_total"] == 2
    assert p["files_total"] == 1


def test_mark_item_done_by_target(write_plan) -> None:
    mem = write_plan(PLAN)
    mem.mark_item_done("b.py")
    assert mem.parse_todos()[0].done


def test_build_done_summary_is_a_short_narrative(write_plan) -> None:
    mem = write_plan(PLAN)
    mem.mark_todo_done(1)
    mem.mark_todo_done(2)
    mem.mark_todo_skipped(3)
    summary = mem.build_done_summary()
    assert "`b.py` (create)" in summary
    assert "py_compile" in summary
    # Skipped work is not claimed as done
    assert "verify" not in summary


def test_skip_clone_run_todos_marks_same_failing_import(write_plan) -> None:
    mem = write_plan(
        "# Plan\nGoal: g\n\n## Todos\n"
        '1. [ ] run `python3 -c "from b import app"` — expect success\n'
        '2. [ ] run `PYTHONPATH=. python3 -c "from b import app; print(1)"` — expect 1\n'
        '3. [ ] run `python3 -c "import os"` — expect success\n'
    )
    skipped = mem.skip_clone_run_todos('python3 -c "from b import app"')
    assert skipped == [1, 2]
    todos = {t.number: t for t in mem.parse_todos()}
    assert todos[3].skipped is False


def test_skip_clone_run_todos_noop_without_import_pattern(write_plan) -> None:
    mem = write_plan(PLAN)
    assert mem.skip_clone_run_todos("python3 -m py_compile b.py") == []


def test_resolve_path_rejects_embedded_escape(memory: WorkspaceMemory) -> None:
    with pytest.raises(ValueError):
        memory.resolve_path("src/../../etc/passwd")


def test_resolve_path_neutralizes_leading_dots(memory: WorkspaceMemory) -> None:
    """A leading ../ is stripped rather than rejected — it stays in-workspace."""
    assert memory.resolve_path("../../etc/passwd") == memory.root / "etc/passwd"


def test_resolve_path_normalizes_leading_slash(memory: WorkspaceMemory) -> None:
    assert memory.resolve_path("/b.py") == memory.resolve_path("b.py")


def test_find_module_py_prefers_shallow_path(memory: WorkspaceMemory) -> None:
    memory.write_prototype("deep/nested/b.py", "x=1\n")
    memory.write_prototype("b.py", "x=1\n")
    assert memory.find_module_py("b") == "b.py"
    assert memory.find_module_py("nested.b") is None
    assert memory.find_module_py("") is None


def test_find_module_py_finds_nested_only_hit(memory: WorkspaceMemory) -> None:
    memory.write_prototype("src/api.py", "app = 1\n")
    assert memory.find_module_py("api") == "src/api.py"


def test_archive_plan_moves_plan_and_leaves_template(memory: WorkspaceMemory) -> None:
    memory.write_plan(PLAN)
    dest = memory.archive_plan()
    assert dest.exists()
    assert "b.py" in dest.read_text(encoding="utf-8")
    assert memory.parse_todos() == []


def test_sanitize_archive_name_is_filesystem_safe() -> None:
    assert WorkspaceMemory.sanitize_archive_name("Beer API!") == "Beer-API"
    assert WorkspaceMemory.sanitize_archive_name("  my_plan v2  ") == "my_plan-v2"


@pytest.mark.parametrize("bad", ["", ".", "..", "../etc", "a/b", "a\\b"])
def test_sanitize_archive_name_rejects_path_traversal(bad: str) -> None:
    with pytest.raises(ValueError):
        WorkspaceMemory.sanitize_archive_name(bad)
