"""The execute recovery ladder: junk-skip → auto-fix → auto-replan → skip.

Each rung is a deterministic classifier. Misrouting wastes the single fix or
replan attempt the budget allows, so the branch conditions are pinned here.
"""

from __future__ import annotations

from tinylocalcoder.agents.fixing import (
    apply_heuristic_fixes,
    classify_failure,
    classify_plan_smell,
    extract_paths_from_error,
    gather_fix_context,
    is_junk_run_failure,
    missing_toolchain_binaries,
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


def test_stub_start_script_is_rewritten_from_hint(memory: WorkspaceMemory) -> None:
    memory.write_prototype("src/db.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    memory.write_prototype(
        "start_service.sh",
        "#!/usr/bin/env bash\nset -e\ncd \"$(dirname \"$0\")\"\n",
    )
    applied = apply_heuristic_fixes(
        memory, "User note:\nstart_service.sh should execute src/db.py"
    )
    body = memory.read_prototype("start_service.sh")
    assert "from src.db import app" in body
    assert "uvicorn" not in body
    assert any("start_service.sh" in note for note in applied)


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


class TestBuildFailureRecovery:
    """A failed `make` must reach the fix agent with the offending file in hand.

    Previously `make` was not in the run-command allowlist, so it was
    classified as a junk command and skipped outright; and even when a build
    error did reach the fix agent, no path regex matched a `.cpp` or a
    `Makefile`, so it was handed an error with nothing to look at.
    """

    def test_make_is_no_longer_a_junk_command(self) -> None:
        assert not is_junk_run_failure({"command": "make"})
        assert not is_junk_run_failure({"command": "g++ -o app main.cpp"})
        assert not is_junk_run_failure({"command": "./square_root 9"})
        # Real prose is still junk
        assert is_junk_run_failure({"command": "Define the struct"})

    def test_missing_build_tool_is_an_environment_failure(
        self, monkeypatch
    ) -> None:
        import tinylocalcoder.toolchains as toolchains

        monkeypatch.setattr(toolchains.shutil, "which", lambda name: None)
        failure = {
            "command": "make",
            "exit_code": 127,
            "stderr": "/bin/sh: 1: make: not found\n",
        }
        assert classify_failure(failure) == "environment"
        assert missing_toolchain_binaries(failure) == ["make"]

    def test_a_tool_that_is_installed_is_a_code_failure(self) -> None:
        """"not found" in a tool's own output is not a missing interpreter."""
        failure = {
            "command": "python3 -m pytest",
            "exit_code": 4,
            "stderr": "ERROR: file or directory not found: tests\n",
        }
        assert classify_failure(failure) == "code"

    def test_missing_built_binary_is_not_an_environment_failure(self) -> None:
        """`./square_root: not found` means the build failed, not that apt is needed."""
        failure = {
            "command": "./square_root",
            "exit_code": 127,
            "stderr": "/bin/sh: 1: ./square_root: not found\n",
        }
        assert missing_toolchain_binaries(failure) == []
        assert classify_failure(failure) == "code"

    def test_compile_error_is_a_code_failure(self) -> None:
        failure = {
            "command": "make",
            "exit_code": 2,
            "stderr": "src/square_root.cpp:12:5: error: 'sqrt' was not declared\n",
        }
        assert classify_failure(failure) == "code"

    def test_gcc_diagnostic_yields_the_offending_source_file(self) -> None:
        paths = extract_paths_from_error(
            "src/square_root.cpp:12:5: error: 'sqrt' was not declared in this scope\n"
            "make: *** [Makefile:7: square_root] Error 1\n"
        )
        assert "src/square_root.cpp" in paths

    def test_make_diagnostic_yields_the_makefile(self) -> None:
        paths = extract_paths_from_error("Makefile:4: *** missing separator.  Stop.\n")
        assert "Makefile" in paths

    def test_make_failure_pulls_in_the_sources_it_builds(
        self, memory: WorkspaceMemory
    ) -> None:
        memory.write_prototype("Makefile", "all:\n\tg++ -o app a.cpp\n")
        memory.write_prototype("a.cpp", "int main() { return 0; }\n")
        ctx = gather_fix_context(memory, "Makefile:4: *** missing separator.  Stop.\n")
        assert "Makefile" in ctx["files"]
        assert "a.cpp" in ctx["files"]

    def test_makefile_missing_separator_is_fixed_without_the_model(
        self, memory: WorkspaceMemory
    ) -> None:
        memory.write_prototype(
            "Makefile",
            "CC = g++\n\napp: a.cpp\n    $(CC) -o app a.cpp\n\nclean:\n    rm -f app\n",
        )
        applied = apply_heuristic_fixes(
            memory, "Makefile:4: *** missing separator.  Stop.\n"
        )
        assert any("TAB" in note for note in applied)
        text = memory.read_prototype("Makefile")
        assert "\tg++ -o app a.cpp" in text.replace("$(CC)", "g++")
        assert "\trm -f app" in text
        # Variable assignments and targets must not be indented
        assert text.startswith("CC = g++")
        assert "\napp: a.cpp\n" in text

    def test_well_formed_makefile_is_left_alone(self, memory: WorkspaceMemory) -> None:
        original = "app: a.cpp\n\tg++ -o app a.cpp\n"
        memory.write_prototype("Makefile", original)
        applied = apply_heuristic_fixes(memory, "Makefile:1: *** missing separator.\n")
        assert not any("TAB" in note for note in applied)
        assert memory.read_prototype("Makefile") == original


class TestSynthesizedSmokeRun:
    """`augment_plan_todos` can only synthesize `./binary` with no arguments.

    Nothing deterministic knows what the program expects, so when that run
    fails with no compiler diagnostics the todo is wrong, not the code —
    replan can rewrite it. Skipping instead leaves a successful build [!].
    """

    def test_bare_binary_failure_is_a_plan_smell(
        self, memory: WorkspaceMemory
    ) -> None:
        failure = _failure("./square_root", stderr="")
        failure["exit_code"] = 1
        assert classify_plan_smell(memory, failure=failure, fixes=[])

    def test_a_run_with_arguments_is_not(self, memory: WorkspaceMemory) -> None:
        """The model supplied the arguments, so the todo is not the suspect."""
        failure = _failure("./square_root 9", stderr="")
        assert not classify_plan_smell(memory, failure=failure, fixes=[])

    def test_a_compiler_diagnostic_is_a_code_problem_not_a_plan_one(
        self, memory: WorkspaceMemory
    ) -> None:
        failure = _failure(
            "./square_root", stderr="square_root.cpp:3:1: error: boom\n"
        )
        assert not classify_plan_smell(memory, failure=failure, fixes=[])

    def test_an_applied_fix_means_the_code_was_the_suspect(
        self, memory: WorkspaceMemory
    ) -> None:
        """The fix agent patched the file the failure named — retry it, don't replan."""
        failure = _failure("./square_root", stderr="square_root.cpp: bad output\n")
        assert not classify_plan_smell(
            memory, failure=failure, fixes=["WRITE `square_root.cpp`"]
        )
