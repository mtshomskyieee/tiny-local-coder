"""Tests for deterministic manifest inventory tool."""

from __future__ import annotations

from pathlib import Path

from tinylocalcoder.config import Settings
from tinylocalcoder.memory.files import TodoStep, WorkspaceMemory
from tinylocalcoder.tools.manifest import (
    build_manifest,
    collect_manifest_paths,
    fulfill_manifest_todo,
    is_manifest_todo,
    parse_focus_prefixes,
    write_manifest,
)


def _memory(tmp: Path) -> WorkspaceMemory:
    settings = Settings(workspace_dir=str(tmp))
    return WorkspaceMemory(settings)


def test_collect_skips_meta_and_archive(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x=1\n", encoding="utf-8")
    (tmp_path / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (tmp_path / "ask.md").write_text("# Ask\n", encoding="utf-8")
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "old.py").write_text("y=2\n", encoding="utf-8")
    (tmp_path / ".index").mkdir()
    (tmp_path / ".index" / "x.json").write_text("[]\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")

    paths = collect_manifest_paths(tmp_path)
    assert paths == ["README.md", "src/main.py"]


def test_focus_prefix(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "tests" / "t.py").write_text("t\n", encoding="utf-8")

    assert collect_manifest_paths(tmp_path, focus_prefixes=["src/"]) == ["src/a.py"]
    assert parse_focus_prefixes("focus on src/") == ["src/"]


def test_write_manifest_and_coding_hook(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    (tmp_path / "utils.py").write_text("ok\n", encoding="utf-8")

    path, paths = write_manifest(mem)
    assert path == "manifest.txt"
    assert paths == ["utils.py"]
    assert mem.read_prototype("manifest.txt").strip() == "utils.py"

    todo = TodoStep(
        number=1,
        action="create",
        target="manifest.txt",
        description="one relative path per line — focus on src/",
        done=False,
        raw_line="1. [ ] create `manifest.txt` — …",
    )
    assert is_manifest_todo(todo)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "b.py").write_text("b\n", encoding="utf-8")
    result = fulfill_manifest_todo(mem, todo)
    assert result["status"] == "ok"
    assert result["tool"] == "manifest"
    assert "src/b.py" in mem.read_prototype("manifest.txt")
    assert "utils.py" not in mem.read_prototype("manifest.txt")


def test_build_manifest_via_memory(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    (tmp_path / "a.md").write_text("a\n", encoding="utf-8")
    assert build_manifest(mem) == ["a.md"]


# --- focus hints come from LLM prose, so they must be read conservatively ---


def test_focus_prefix_ignores_plain_english(tmp_path: Path) -> None:
    """Regression: `in manifest` is prose, not a directory prefix.

    The bare `in`/`only` keywords used to turn any English phrase into a
    path prefix, which silently filtered the inventory down to nothing.
    """
    assert parse_focus_prefixes("one relative path per line for files in manifest") == []
    assert parse_focus_prefixes("only the important modules") == []
    assert parse_focus_prefixes("notes for files in manifest") == []
    # Real directory syntax still works
    assert parse_focus_prefixes("focus on src/") == ["src/"]
    assert parse_focus_prefixes("only tests/unit/") == ["tests/unit/"]
    assert parse_focus_prefixes("focus on src/api.py") == ["src/api.py"]


def test_focus_hint_that_matches_nothing_fails_open(tmp_path: Path) -> None:
    """An inventory of zero files is never the useful answer."""
    mem = _memory(tmp_path)
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    assert build_manifest(mem, focus_prefixes=["nope/"]) == ["a.py"]


def test_collect_includes_cpp_makefile_and_popular_langs(tmp_path: Path) -> None:
    """C/C++, Makefile, Go, Rust, Ruby must be inventoriable for /review."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.cpp").write_text("int main(){}\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("all:\n\techo ok\n", encoding="utf-8")
    (tmp_path / "main.go").write_text("package main\n", encoding="utf-8")
    (tmp_path / "lib.rs").write_text("fn main() {}\n", encoding="utf-8")
    (tmp_path / "app.rb").write_text("puts 'hi'\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example\n", encoding="utf-8")
    (tmp_path / "noise.png").write_bytes(b"\x89PNG")

    paths = collect_manifest_paths(tmp_path)
    assert "src/foo.cpp" in paths
    assert "Makefile" in paths
    assert "main.go" in paths
    assert "lib.rs" in paths
    assert "app.rb" in paths
    assert "go.mod" in paths
    assert "noise.png" not in paths

    mem = _memory(tmp_path)
    _, written = write_manifest(mem)
    assert written == paths
    body = mem.read_prototype("manifest.txt")
    assert "src/foo.cpp" in body
    assert "Makefile" in body


def test_manifest_skips_build_products(memory: WorkspaceMemory) -> None:
    """Compiling in the workspace must not pollute the review manifest."""
    for rel, body in (
        ("square_root.cpp", "int main(){return 0;}\n"),
        ("Makefile", "all:\n\tg++ -o square_root square_root.cpp\n"),
        ("square_root", "ELF-ish binary\n"),
        ("square_root.o", "object\n"),
        ("a.out", "binary\n"),
    ):
        memory.write_prototype(rel, body)
    assert collect_manifest_paths(memory.root) == ["Makefile", "square_root.cpp"]
