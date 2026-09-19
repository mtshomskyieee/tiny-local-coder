"""finalize_plan is the guardrail that keeps a 3B model's plan executable.

Everything here is deterministic Python — no LLM. These tests pin the
contract described in CLAUDE.md: strip leaked meta commands, drop junk run
targets, rewrite bad FastAPI verifies, cap to one py_compile + one smoke run.
"""

from __future__ import annotations

from tinylocalcoder.memory.files import (
    PLAN_TODO_CAP,
    TodoStep,
    WorkspaceMemory,
    is_meta_todo_target,
    is_valid_run_command,
    looks_fastapi_goal,
    meta_command_name,
    plan_text_equivalent,
    strip_placeholder_path,
    strip_placeholders_in_command,
)
from tinylocalcoder.tui.screens import plan_edit_needs_review


def _todo(action: str, target: str, desc: str = "", *, done: bool = False,
          skipped: bool = False, number: int = 1) -> TodoStep:
    return TodoStep(
        number=number,
        action=action,
        target=target,
        description=desc,
        done=done,
        raw_line="",
        skipped=skipped,
    )


# --- helper predicates -------------------------------------------------


def test_meta_command_name_accepts_slash_and_bare() -> None:
    assert meta_command_name("/reset-todo 8") == "reset-todo"
    assert meta_command_name("REVIEW") == "review"
    assert meta_command_name("/review-fix") == "review-fix"
    assert meta_command_name("/fix-plan") == "fix-plan"
    assert meta_command_name("") is None
    assert meta_command_name("create a fastapi service") is None


def test_is_meta_todo_target_catches_leaked_followups() -> None:
    assert is_meta_todo_target("/auto-fix on")
    assert is_meta_todo_target("execute-plan")
    # Files invented after meta leakage
    assert is_meta_todo_target("reset_todo_8.py")
    assert is_meta_todo_target("skip_todo_3.py")
    assert not is_meta_todo_target("src/api.py")
    assert not is_meta_todo_target("")


def test_is_valid_run_command_rejects_prose() -> None:
    assert is_valid_run_command("python3 -m py_compile b.py")
    assert is_valid_run_command("PYTHONPATH=. python3 -c 'import b'")
    assert is_valid_run_command("./run.sh")
    assert is_valid_run_command("pytest -q")
    # Prose the model emits as a "command"
    assert not is_valid_run_command("Define the endpoints")
    assert not is_valid_run_command("")
    assert not is_valid_run_command("curl http://localhost:8000/select")
    # PYTHONPATH= with no interpreter is not runnable
    assert not is_valid_run_command("PYTHONPATH=. echo hi")


def test_strip_placeholders() -> None:
    assert strip_placeholder_path("path/to/src/api.py") == "src/api.py"
    assert strip_placeholder_path("/path/to/b.py") == "b.py"
    assert strip_placeholder_path("./path/to/path/to/b.py") == "b.py"
    assert strip_placeholders_in_command(
        "python3 -m py_compile path/to/b.py"
    ) == "python3 -m py_compile b.py"


def test_looks_fastapi_goal_from_goal_or_todos() -> None:
    assert looks_fastapi_goal("Build a FastAPI service")
    assert looks_fastapi_goal("expose REST endpoints")
    assert not looks_fastapi_goal("write a csv parser")
    assert looks_fastapi_goal(
        "write a csv parser", [_todo("create", "api.py", "fastapi routes")]
    )


# --- finalize_plan end to end -----------------------------------------


def test_finalize_strips_meta_and_junk_runs(memory: WorkspaceMemory) -> None:
    final = memory.finalize_plan(
        "# Plan\n"
        "Goal: build b.py\n"
        "\n"
        "## Todos\n"
        "1. [ ] create `b.py` — json db\n"
        "2. [ ] run `reset-todo 8` — expect success\n"
        "3. [ ] run `Define the endpoints` — expect success\n"
    )
    assert "reset-todo" not in final
    assert "Define the endpoints" not in final
    assert "create `b.py`" in final
    # Plan is rewritten on disk, not just returned
    assert memory.read_plan() == final
    targets = [t.target for t in memory.parse_todos()]
    assert "b.py" in targets


def test_finalize_adds_one_compile_and_one_smoke_run(memory: WorkspaceMemory) -> None:
    memory.finalize_plan(
        "# Plan\nGoal: build b.py\n\n## Todos\n1. [ ] create `b.py` — json db\n"
    )
    runs = [t for t in memory.parse_todos() if t.action == "run"]
    assert len([r for r in runs if "py_compile" in r.target]) == 1
    assert len([r for r in runs if "py_compile" not in r.target]) == 1
    # Every synthesized run carries an expect clause so execution can judge it
    assert all("expect" in r.description.lower() for r in runs)


def test_finalize_does_not_add_compile_when_only_init_py(memory: WorkspaceMemory) -> None:
    memory.finalize_plan(
        "# Plan\nGoal: package marker\n\n## Todos\n1. [ ] create `src/__init__.py` — marker\n"
    )
    runs = [t for t in memory.parse_todos() if t.action == "run"]
    assert runs == []


def test_finalize_rewrites_broken_fastapi_verify(memory: WorkspaceMemory) -> None:
    """`from b import b` never works; for a FastAPI goal it becomes a route dump."""
    memory.finalize_plan(
        "# Plan\n"
        "Goal: FastAPI service in b.py using db.json\n"
        "\n"
        "## Todos\n"
        "1. [ ] create `b.py` — endpoints\n"
        '2. [ ] run `python3 -c "from b import b"` — expect success\n'
    )
    runs = [t for t in memory.parse_todos() if t.action == "run"]
    behavior = [r for r in runs if "py_compile" not in r.target]
    assert behavior, "smoke run should survive the rewrite"
    assert "from b import b" not in behavior[0].target
    assert "from b import app" in behavior[0].target
    assert "app.routes" in behavior[0].target


def test_finalize_preserves_goal_done_and_extra_block(memory: WorkspaceMemory) -> None:
    final = memory.finalize_plan(
        "# Plan\n"
        "Goal: build b.py\n"
        "Done: `b.py` (create), py_compile\n"
        "\n"
        "Notes: db.json lives beside b.py\n"
        "\n"
        "## Todos\n"
        "1. [ ] refine `b.py` — add update route\n"
    )
    assert "Goal: build b.py" in final
    assert "Done: `b.py` (create), py_compile" in final
    assert "Notes: db.json lives beside b.py" in final


def test_format_todo_board_includes_done_and_open(memory: WorkspaceMemory) -> None:
    memory.write_plan(
        "# Plan\nGoal: run tests\n\n## Todos\n"
        "1. [x] create `tests/test_db.py` — smoke\n"
        "2. [ ] run `python3 -m pytest tests/test_db.py -v` — expect success\n"
        "3. [!] run `python3 -m py_compile app.py` — expect success\n"
    )
    board = memory.format_todo_board()
    assert "Todos 1/3 (+1 skipped)" in board
    assert "1. [x] create `tests/test_db.py` — smoke" in board
    assert "2. [ ] run `python3 -m pytest tests/test_db.py -v`" in board
    assert "3. [!] run `python3 -m py_compile app.py`" in board


def test_save_plan_from_editor_keeps_hand_written_todos(
    memory: WorkspaceMemory,
) -> None:
    """Undo-after-rewrite persist path must keep hand-written todos."""
    edited = (
        "# Plan\n"
        "Goal: start the API from start_service.sh\n"
        "\n"
        "## Todos\n"
        "1. [ ] refine `start_service.sh` — run python3 src/db.py\n"
        "2. [ ] refine `src/db.py` — add update and delete routes\n"
    )
    saved = memory.save_plan_from_editor(edited)
    assert "refine `start_service.sh`" in saved
    assert "refine `src/db.py`" in saved
    assert memory.read_plan() == saved
    todos = memory.parse_todos()
    assert [t.target for t in todos] == ["start_service.sh", "src/db.py"]


def test_save_plan_from_editor_keeps_prose_when_todos_do_not_parse(
    memory: WorkspaceMemory,
) -> None:
    edited = "# Plan\nGoal: make start_service.sh actually start the app\n\nNotes: use src.db:app\n"
    saved = memory.save_plan_from_editor(edited)
    assert "make start_service.sh actually start the app" in saved
    assert "use src.db:app" in saved


def test_plan_text_equivalent_ignores_trailing_whitespace() -> None:
    assert plan_text_equivalent("Goal: x\n", "Goal: x")
    assert plan_text_equivalent("a  \n\n", "a")
    assert not plan_text_equivalent("Goal: a", "Goal: b")


def test_render_finalized_plan_does_not_write(memory: WorkspaceMemory) -> None:
    original = "# Plan\nGoal: stay put\n\n## Todos\n"
    memory.write_plan(original)
    draft = (
        "# Plan\nGoal: add a helper\n\n## Todos\n"
        "1. [ ] create `util.py` — helper\n"
        "2. [ ] run `curl localhost:8000` — expect 200\n"
    )
    rendered = memory.render_finalized_plan(draft)
    assert memory.read_plan() == original
    assert "util.py" in rendered
    assert memory.finalize_plan(draft) == rendered
    assert memory.read_plan() == rendered


def test_plan_edit_needs_review_when_standards_rewrite_changes_text(
    memory: WorkspaceMemory,
) -> None:
    draft = (
        "# Plan\nGoal: start the API\n\n## Todos\n"
        "- make start_service.sh work somehow\n"
        "please also add update routes\n"
    )
    finalized = memory.render_finalized_plan(draft)
    assert plan_edit_needs_review(draft, finalized, skip_standards=False)
    assert not plan_edit_needs_review(draft, finalized, skip_standards=True)
    assert not plan_edit_needs_review(finalized, finalized, skip_standards=False)


def test_finalize_renumbers_contiguously(memory: WorkspaceMemory) -> None:
    memory.finalize_plan(
        "# Plan\nGoal: g\n\n## Todos\n"
        "7. [ ] create `a.py` — x\n"
        "9. [ ] create `c.py` — y\n"
    )
    assert [t.number for t in memory.parse_todos()] == [1, 2, 3, 4]


def test_finalize_caps_runaway_plan(memory: WorkspaceMemory) -> None:
    lines = "\n".join(
        f"{i}. [ ] create `f{i}.py` — file {i}" for i in range(1, 40)
    )
    memory.finalize_plan(f"# Plan\nGoal: many files\n\n## Todos\n{lines}\n")
    assert len(memory.parse_todos()) <= PLAN_TODO_CAP


def test_finalize_tolerates_missing_todos_heading(memory: WorkspaceMemory) -> None:
    """Small models routinely omit `## Todos`."""
    final = memory.finalize_plan(
        "# Plan\nGoal: build b.py\n1. [ ] create `b.py` — json db\n"
    )
    assert "## Todos" in final
    assert any(t.target == "b.py" for t in memory.parse_todos())


def test_finalize_empty_text_yields_usable_skeleton(memory: WorkspaceMemory) -> None:
    final = memory.finalize_plan("")
    assert final.startswith("# Plan")
    assert "## Todos" in final
    assert memory.parse_todos() == []


# --- sanitize / dedupe / cap units ------------------------------------


def test_dedupe_keeps_one_done_and_one_open_per_target(memory: WorkspaceMemory) -> None:
    todos = [
        _todo("create", "b.py", done=True, number=1),
        _todo("create", "path/to/b.py", done=True, number=2),
        _todo("create", "b.py", number=3),
        _todo("create", "b.py", number=4),
    ]
    out = memory.dedupe_todos(todos)
    assert len(out) == 2
    assert [t.done for t in out] == [True, False]
    assert all(t.target == "b.py" for t in out)


def test_dedupe_drops_empty_targets(memory: WorkspaceMemory) -> None:
    assert memory.dedupe_todos([_todo("create", "path/to/")]) == []


def test_cap_todos_prefers_open_items(memory: WorkspaceMemory) -> None:
    done = [_todo("create", f"d{i}.py", done=True, number=i) for i in range(10)]
    open_ = [_todo("create", f"o{i}.py", number=100 + i) for i in range(5)]
    out = memory.cap_todos(done + open_, max_total=6)
    assert len(out) == 6
    assert sum(1 for t in out if not t.done) == 3  # REPLAN_OPEN_CAP


def test_sanitize_collapses_duplicate_compile_runs(memory: WorkspaceMemory) -> None:
    todos = [
        _todo("create", "b.py", number=1),
        _todo("run", "python3 -m py_compile b.py", "expect success", number=2),
        _todo("run", "python3 -m py_compile b.py", "expect success", number=3),
        _todo("run", 'python3 -c "import b"', "expect success", number=4),
        _todo("run", 'python3 -c "import b"', "expect success", number=5),
    ]
    out = memory.sanitize_plan_todos(todos, goal="build b.py")
    runs = [t for t in out if t.action == "run"]
    assert len([r for r in runs if "py_compile" in r.target]) == 1
    assert len([r for r in runs if "py_compile" not in r.target]) == 1


def test_sanitize_appends_expect_clause_when_missing(memory: WorkspaceMemory) -> None:
    out = memory.sanitize_plan_todos(
        [_todo("run", "python3 -m py_compile b.py", "compile it")], goal="g"
    )
    assert out[0].description.lower().endswith("expect success")


# --- validate_plan ----------------------------------------------------


def test_validate_plan_flags_missing_verify_and_meta(memory: WorkspaceMemory) -> None:
    issues = memory.validate_plan(
        [
            _todo("create", "b.py", "json db", number=1),
            _todo("run", "/auto-fix on", "expect success", number=2),
        ]
    )
    blob = " ".join(issues)
    assert "meta-command" in blob
    assert "py_compile" in blob


def test_validate_plan_flags_run_without_expect(memory: WorkspaceMemory) -> None:
    issues = memory.validate_plan(
        [_todo("run", "python3 -m py_compile b.py", "just compile", number=1)]
    )
    assert any("expect clause" in i for i in issues)


def test_validate_plan_clean_plan_has_no_issues(memory: WorkspaceMemory) -> None:
    assert memory.validate_plan(
        [
            _todo("create", "b.py", "json db", number=1),
            _todo("run", "python3 -m py_compile b.py", "expect success", number=2),
        ]
    ) == []
