"""The 3B model only sees the current todo, so the prompt must name the language.

Showing a Python example for a C++ request is how plans ended up with no
compile step. The example block is swapped, not added to — at 2048 ctx there
is no room for a menu of languages.
"""

from __future__ import annotations

from pathlib import Path

from tinylocalcoder.agents.coding import (
    _coding_system,
    _language_note,
    _paths_for_todo,
    source_slice_for_test,
)
from tinylocalcoder.agents.thinking import TEST_GUIDANCE_SYSTEM
from tinylocalcoder.agents.replan import _fallback_verify_todo
from tinylocalcoder.agents.thinking import build_plan_system
from tinylocalcoder.agents.workflows import _list_workspace_tests
from tinylocalcoder.memory.files import TodoStep, WorkspaceMemory


def _todo(target: str, desc: str = "") -> TodoStep:
    return TodoStep(
        number=1,
        action="create",
        target=target,
        description=desc,
        done=False,
        raw_line="",
    )


class TestPlanPrompt:
    def test_cpp_request_gets_a_cpp_example(self) -> None:
        text = build_plan_system("write a C++ square root program with a Makefile")
        assert "g++ -Wall -std=c++17" in text
        assert "py_compile" not in text
        # Python-only rules are dropped, not just outvoted
        assert "PYTHONPATH" not in text
        assert "__init__.py" not in text

    def test_python_request_is_unchanged(self) -> None:
        text = build_plan_system("a FastAPI beer endpoint")
        assert "python3 -m py_compile" in text
        assert 'python3 app.py --dry-run && python3 -c "import app"' in text
        assert "PYTHONPATH" in text
        assert "app.routes" in text

    def test_unknown_request_falls_back_to_python(self) -> None:
        assert "py_compile" in build_plan_system("do something vague")

    def test_prompt_does_not_grow_with_the_registry(self) -> None:
        """One example block either way — the context budget is 2048 tokens."""
        cpp = build_plan_system("a C++ program")
        py = build_plan_system("a python module")
        assert abs(len(cpp) - len(py)) < len(py) / 2
        assert "rustc" not in cpp and "cargo" not in cpp


class TestCodingAgent:
    def test_source_paths_of_any_language_are_recognized(self) -> None:
        assert _paths_for_todo(_todo("src/square_root.cpp")) == ["src/square_root.cpp"]
        assert _paths_for_todo(_todo("Makefile")) == ["Makefile"]
        assert _paths_for_todo(_todo("hello.c")) == ["hello.c"]
        assert _paths_for_todo(_todo("app.py")) == ["app.py"]

    def test_test_guidance_names_no_language_or_runner(self) -> None:
        lowered = TEST_GUIDANCE_SYSTEM.lower()
        for word in ("python", "pytest", "unittest", "cargo", "javac", "npm"):
            assert word not in lowered

    def test_test_file_step_sees_the_source_slice(self, memory: WorkspaceMemory) -> None:
        (memory.root / "calculator.py").write_text(
            "def rpn(expr):\n    return 1\n",
            encoding="utf-8",
        )
        note = source_slice_for_test(memory, "tests/test_calculator.py")
        assert "calculator.py" in note
        assert "def rpn" in note
        assert source_slice_for_test(memory, "calculator.py") == ""

    def test_dry_run_sentence_is_added_only_when_the_step_mentions_it(self) -> None:
        plain = _coding_system("app.py", "1. [ ] create `app.py` — does a thing", refine=False)
        assert "Accept --dry-run" not in plain
        noted = _coding_system(
            "app.py",
            "1. [ ] create `app.py` — supports --dry-run: print the action and exit 0",
            refine=False,
        )
        assert "Accept --dry-run: print the action, exit 0" in noted

    def test_language_note_names_the_language(self) -> None:
        assert _language_note("a.cpp") == "Write C++.\n"
        assert _language_note("a.c") == "Write C.\n"
        assert _language_note("a.py") == "Write Python.\n"
        assert _language_note("README.md") == ""

    def test_makefile_note_states_the_tab_rule(self) -> None:
        """The one Makefile mistake a 3B model makes every time."""
        note = _language_note("Makefile")
        assert "TAB" in note
        assert "target" in note


class TestReplanFallback:
    def test_cpp_workspace_gets_a_cpp_verify(self, memory: WorkspaceMemory) -> None:
        memory.write_prototype("square_root.cpp", "int main(){return 0;}\n")
        todo = _fallback_verify_todo(memory, "C++ square root")
        assert todo is not None
        assert todo.target.startswith("g++")
        assert "python3" not in todo.target

    def test_python_workspace_still_gets_an_import_check(
        self, memory: WorkspaceMemory
    ) -> None:
        memory.write_prototype("app.py", "x = 1\n")
        todo = _fallback_verify_todo(memory, "a module")
        assert todo is not None
        assert "python3 -c" in todo.target

    def test_empty_workspace_has_no_fallback(self, memory: WorkspaceMemory) -> None:
        assert _fallback_verify_todo(memory, "anything") is None


class TestTestDiscovery:
    def test_tests_in_any_language_are_found(self, memory: WorkspaceMemory) -> None:
        memory.write_prototype("tests/test_a.py", "")
        memory.write_prototype("test_square_root.cpp", "")
        memory.write_prototype("square_root.cpp", "")
        memory.write_prototype("README.md", "")
        found = set(_list_workspace_tests(memory))
        assert "tests/test_a.py" in found
        assert "test_square_root.cpp" in found
        assert "square_root.cpp" not in found
        assert "README.md" not in found
