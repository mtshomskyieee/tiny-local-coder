"""Review heuristics — the no-LLM code critic that a 3B model cannot do.

Each check here exists because the small model shipped the corresponding bug,
so both the hit and the no-false-positive case matter.
"""

from __future__ import annotations

from tinylocalcoder.tools.review import review_file


def _messages(path: str, text: str, all_paths: list[str] | None = None) -> str:
    rev = review_file(path, text, all_paths=all_paths or [path])
    return " ".join(f.message for f in rev.findings)


def _severities(path: str, text: str, all_paths: list[str] | None = None) -> set[str]:
    rev = review_file(path, text, all_paths=all_paths or [path])
    return {f.severity for f in rev.findings}


def test_syntax_error_is_high_and_stops_further_checks() -> None:
    rev = review_file("b.py", "def broken(:\n", all_paths=["b.py"])
    assert rev.worst == "high"
    assert rev.summary == "Does not parse."
    assert any("Syntax error" in f.message for f in rev.findings)


def test_bare_except_is_high() -> None:
    text = "try:\n    x = 1\nexcept:\n    pass\n"
    assert "high" in _severities("b.py", text)
    assert "Bare `except:`" in _messages("b.py", text)


def test_mutable_default_argument_is_high() -> None:
    assert "mutable default argument" in _messages(
        "b.py", "def add(items=[]):\n    return items\n"
    )


def test_max_keys_id_bug_is_flagged() -> None:
    """The model's favourite json-db bug: max(db.keys() or [0]) + 1."""
    text = (
        "import json\n"
        "def add(rec):\n"
        "    db = json.load(open('db.json'))\n"
        "    new_id = max(db.keys() or [0]) + 1\n"
        "    return new_id\n"
    )
    msgs = _messages("b.py", text)
    assert "max(...keys())" in msgs
    assert "high" in _severities("b.py", text)


def test_no_op_smoke_assertion_in_a_test_is_high() -> None:
    text = "def test_smoke():\n    assert True\n"
    assert "no-op" in _messages("tests/test_x.py", text, ["tests/test_x.py"])


def test_missing_test_coverage_is_flagged_and_cleared_by_a_test_file() -> None:
    src = "def select():\n    return []\n"
    assert "No obvious unit test" in _messages("src/api.py", src, ["src/api.py"])
    covered = _messages(
        "src/api.py", src, ["src/api.py", "tests/test_api.py"]
    )
    assert "No obvious unit test" not in covered


def test_deprecated_on_event_is_flagged_once() -> None:
    text = (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.on_event('startup')\n"
        "def a():\n    pass\n"
        "@app.on_event('shutdown')\n"
        "def b():\n    pass\n"
    )
    rev = review_file("b.py", text, all_paths=["b.py", "tests/test_b.py"])
    hits = [f for f in rev.findings if "on_event" in f.message]
    assert len(hits) == 1, "one note per issue, not one per occurrence"


def test_clean_module_reports_info_only() -> None:
    text = (
        "from pathlib import Path\n\n\n"
        "def read(path: Path) -> str:\n"
        '    return path.read_text(encoding="utf-8")\n'
    )
    rev = review_file("src/io.py", text, all_paths=["src/io.py", "tests/test_io.py"])
    assert rev.worst == "info"
    assert rev.summary == "Clean under heuristic checks."


def test_package_marker_is_not_nagged() -> None:
    rev = review_file("src/__init__.py", "", all_paths=["src/__init__.py"])
    assert rev.summary == "Package init."
    assert rev.worst == "info"


def test_empty_module_is_medium() -> None:
    rev = review_file("b.py", "   \n", all_paths=["b.py"])
    assert rev.worst == "medium"


def test_markdown_without_heading_is_flagged() -> None:
    assert "No top-level" in _messages("docs/notes.md", "some prose " * 10)


def test_invalid_json_is_flagged() -> None:
    assert _severities("db.json", "{not json") & {"high", "medium"}


def test_worst_orders_severities() -> None:
    rev = review_file(
        "b.py",
        "import os  # TODO: cleanup\ntry:\n    pass\nexcept:\n    pass\n",
        all_paths=["b.py", "tests/test_b.py"],
    )
    assert rev.worst == "high", "a high finding must outrank the low TODO note"


class TestCppReview:
    """`_review_cpp` was a TODO-marker scan; the C++ session it was meant to
    help got a review with zero findings."""

    def _messages(self, path: str, text: str) -> list[str]:
        return [f.message for f in review_file(path, text, all_paths=[]).findings]

    def test_unchecked_argv_is_high(self) -> None:
        msgs = self._messages(
            "square_root.cpp",
            "#include <cmath>\n"
            "int main(int argc, char** argv) {\n"
            "    double x = atof(argv[1]);\n"
            "    return 0;\n"
            "}\n",
        )
        assert any("argv[1]" in m and "argc" in m for m in msgs)

    def test_argc_check_silences_it(self) -> None:
        msgs = self._messages(
            "square_root.cpp",
            "#include <cmath>\n"
            "int main(int argc, char** argv) {\n"
            "    if (argc < 2) return 1;\n"
            "    double x = atof(argv[1]);\n"
            "    return 0;\n"
            "}\n",
        )
        assert not any("argc" in m for m in msgs)

    def test_missing_cmath_include(self) -> None:
        msgs = self._messages(
            "square_root.cpp", "int main() {\n    return sqrt(4.0);\n}\n"
        )
        assert any("cmath" in m for m in msgs)

    def test_header_without_include_guard(self) -> None:
        msgs = self._messages("util.hpp", "int f();\n")
        assert any("include guard" in m for m in msgs)
        assert "pragma once" not in " ".join(self._messages("ok.hpp", "#pragma once\nint f();\n"))

    def test_using_namespace_in_a_header_is_high(self) -> None:
        rev = review_file(
            "util.hpp", "#pragma once\nusing namespace std;\nint f();\n", all_paths=[]
        )
        assert any(
            f.severity == "high" and "using namespace" in f.message
            for f in rev.findings
        )

    def test_using_namespace_in_a_source_file_is_fine(self) -> None:
        msgs = self._messages(
            "main.cpp", "#include <cmath>\nusing namespace std;\nint main(){return 0;}\n"
        )
        assert not any("using namespace" in m for m in msgs)

    def test_unbounded_string_functions(self) -> None:
        msgs = self._messages("a.c", "int main(){ char b[8]; gets(b); return 0; }\n")
        assert any("unbounded" in m for m in msgs)

    def test_clean_file_gets_an_info_note_only(self) -> None:
        rev = review_file(
            "ok.cpp", "#include <cmath>\nint main() { return 0; }\n", all_paths=[]
        )
        assert [f.severity for f in rev.findings] == ["info"]
