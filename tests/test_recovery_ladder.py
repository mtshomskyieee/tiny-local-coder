"""The execute recovery ladder: junk-skip → auto-fix → auto-replan → skip.

Each rung is a deterministic classifier. Misrouting wastes the single fix or
replan attempt the budget allows, so the branch conditions are pinned here.
"""

from __future__ import annotations

from tinylocalcoder.agents.fixing import (
    apply_heuristic_fixes,
    classify_plan_smell,
    extract_paths_from_error,
    is_junk_run_failure,
    parse_needs_replan,
)
from tinylocalcoder.agents.replan import try_deterministic_replan
from tinylocalcoder.memory.files import WorkspaceMemory


def _failure(command: str, stderr: str = "", stdout: str = "") -> dict:
    return {"command": command, "stderr": stderr, "stdout": stdout}


# --- rung 0: junk commands skip immediately, spending no budget --------


def test_prose_command_is_junk() -> None:
    assert is_junk_run_failure(_failure("Define the endpoints", "not found"))


def test_real_command_is_not_junk() -> None:
    assert not is_junk_run_failure(
        _failure("python3 -m py_compile b.py", "SyntaxError: invalid syntax")
    )


def test_junk_check_tolerates_missing_failure() -> None:
    assert is_junk_run_failure(None) is False


def test_junk_command_never_counts_as_plan_smell(memory: WorkspaceMemory) -> None:
    """Junk belongs on the skip rung, not the replan rung."""
    assert not classify_plan_smell(
        memory,
        failure=_failure("Define the endpoints", "not found"),
        fixes=[],
    )


# --- error parsing ----------------------------------------------------


def test_extract_paths_from_traceback() -> None:
    err = (
        'Traceback (most recent call last):\n'
        '  File "/workspace/src/api.py", line 12, in <module>\n'
        "    from src.db import load\n"
        "ModuleNotFoundError: No module named 'src.db'\n"
    )
    assert "src/api.py" in extract_paths_from_error(err)


def test_extract_paths_dedupes_and_handles_empty() -> None:
    err = 'File "b.py", line 1\nFile "b.py", line 9\n'
    assert extract_paths_from_error(err) == ["b.py"]
    assert extract_paths_from_error("") == []


# --- rung 1: deterministic heuristic fixes -----------------------------


def test_missing_src_package_marker_is_created(memory: WorkspaceMemory) -> None:
    memory.write_prototype("src/api.py", "x = 1\n")
    applied = apply_heuristic_fixes(memory, "ModuleNotFoundError: No module named 'src'")
    assert memory.prototype_exists("src/__init__.py")
    assert any("__init__.py" in note for note in applied)


def test_script_safe_import_rewrite(memory: WorkspaceMemory) -> None:
    """`python3 src/main.py` cannot import `src.api`; drop the package prefix."""
    memory.write_prototype("src/main.py", "from src.api import app\n")
    applied = apply_heuristic_fixes(memory, "ModuleNotFoundError: No module named 'src'")
    assert memory.read_prototype("src/main.py") == "from api import app\n"
    assert any("script-safe import" in note for note in applied)


def test_no_marker_invented_for_nonexistent_package(memory: WorkspaceMemory) -> None:
    applied = apply_heuristic_fixes(
        memory, "ModuleNotFoundError: No module named 'requests'"
    )
    assert not memory.prototype_exists("requests/__init__.py")
    assert applied == []


def test_unrelated_error_applies_nothing(memory: WorkspaceMemory) -> None:
    assert apply_heuristic_fixes(memory, "SyntaxError: invalid syntax") == []


def test_port_conflict_falls_back_to_rewriting_the_port(memory: WorkspaceMemory) -> None:
    memory.write_prototype("src/main.py", "uvicorn.run(app, port=8000)\n")
    applied = apply_heuristic_fixes(
        memory, "OSError: [Errno 98] Address already in use"
    )
    assert "port=8888" in memory.read_prototype("src/main.py")
    assert any("8888" in note for note in applied)


# --- rung 2: is the todo itself wrong? ---------------------------------


def test_parse_needs_replan_reads_the_llm_marker() -> None:
    assert parse_needs_replan("NEEDS_REPLAN: the verify targets a missing module")
    assert parse_needs_replan("all good") is None


def test_explicit_needs_replan_wins(memory: WorkspaceMemory) -> None:
    assert classify_plan_smell(
        memory, failure=_failure("python3 -c 'import b'"), fixes=[], needs_replan="bad todo"
    )


def test_verify_for_attribute_absent_from_sources_is_plan_smell(
    memory: WorkspaceMemory,
) -> None:
    """The plan asks for `app`, but b.py has no app — patching code won't help."""
    memory.write_prototype("b.py", "def select():\n    return []\n")
    assert classify_plan_smell(
        memory,
        failure=_failure(
            'python3 -c "from b import app"',
            "ImportError: cannot import name 'app' from 'b'",
        ),
        fixes=[],
    )


def test_attribute_present_in_sources_is_not_plan_smell(memory: WorkspaceMemory) -> None:
    memory.write_prototype("b.py", "app = 1\n")
    assert not classify_plan_smell(
        memory,
        failure=_failure(
            'python3 -c "from b import app"',
            'File "b.py", line 1\nImportError: cannot import name \'app\' from \'b\'',
        ),
        fixes=["WRITE `b.py` (patch)"],
    )


def test_missing_src_package_is_a_code_fix_not_a_replan(memory: WorkspaceMemory) -> None:
    assert not classify_plan_smell(
        memory,
        failure=_failure(
            'python3 -c "import src.api"', "ModuleNotFoundError: No module named 'src'"
        ),
        fixes=[],
    )


# --- deterministic replan (no LLM) ------------------------------------


def test_deterministic_replan_refines_file_missing_app(memory: WorkspaceMemory) -> None:
    memory.write_prototype("b.py", "def select():\n    return []\n")
    todos = try_deterministic_replan(
        memory,
        failure=_failure(
            'python3 -c "from b import app"',
            "ImportError: cannot import name 'app' from 'b'",
        ),
        goal="Build a FastAPI service in b.py",
    )
    assert todos is not None
    assert todos[0].action == "refine"
    assert todos[0].target == "b.py"
    assert "expect" in todos[0].description.lower()


def test_deterministic_replan_retargets_verify_to_real_module(
    memory: WorkspaceMemory,
) -> None:
    """The plan verified `db`, but the file on disk is b.py — point at b.py."""
    memory.write_prototype("b.py", "app = 1\n")
    todos = try_deterministic_replan(
        memory,
        failure=_failure(
            'python3 -c "from b import app"',
            "ModuleNotFoundError: No module named 'b'",
        ),
        goal="Build a FastAPI service",
    )
    assert todos is not None
    assert todos[0].action == "run"
    assert "b" in todos[0].target
    assert "expect" in todos[0].description.lower()


def test_deterministic_replan_gives_up_when_nothing_matches(
    memory: WorkspaceMemory,
) -> None:
    assert (
        try_deterministic_replan(
            memory,
            failure=_failure("python3 -m py_compile b.py", "SyntaxError: bad"),
            goal="build b.py",
        )
        is None
    )


def test_deterministic_replan_ignores_archived_files(memory: WorkspaceMemory) -> None:
    memory.write_prototype("archive/old/b.py", "app = 1\n")
    assert (
        try_deterministic_replan(
            memory,
            failure=_failure(
                'python3 -c "import nope"', "ModuleNotFoundError: No module named 'nope'"
            ),
            goal="build something",
        )
        is None
    )
