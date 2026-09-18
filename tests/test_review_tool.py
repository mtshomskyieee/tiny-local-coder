"""Tests for deterministic review tool."""

from __future__ import annotations

from pathlib import Path

from tinylocalcoder.config import Settings
from tinylocalcoder.memory.files import TodoStep, WorkspaceMemory
from tinylocalcoder.tools.manifest import write_manifest
from tinylocalcoder.tools.review import (
    actionable_issues,
    fulfill_review_todo,
    is_review_todo,
    parse_review_issues,
    write_review,
)


def _memory(tmp: Path) -> WorkspaceMemory:
    return WorkspaceMemory(Settings(workspace_dir=str(tmp)))


def test_review_covers_manifest_files(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "api.py").write_text(
        "import json\n"
        "db = {}\n"
        "def add():\n"
        "    global db\n"
        "    with open('src/db.json') as f:\n"
        "        db = json.load(f)\n"
        "    id = max(db.keys() or [0]) + 1\n"
        "    return id\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_smoke.py").write_text(
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_smoke(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    write_manifest(mem)
    path, reviews = write_review(mem)
    assert path == "review.md"
    body = mem.read_prototype("review.md")
    assert "### `src/api.py`" in body
    assert "### `tests/test_smoke.py`" in body
    assert "manifest.txt" not in body.split("## Files")[-1] or "### `manifest.txt`" not in body
    assert any(r.path == "src/api.py" for r in reviews)
    api = next(r for r in reviews if r.path == "src/api.py")
    assert any(f.severity == "high" for f in api.findings)


def test_coding_hook_marks_done(tmp_path: Path) -> None:
    mem = _memory(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    write_manifest(mem)
    todo = TodoStep(
        number=2,
        action="create",
        target="review.md",
        description="notes for files in manifest",
        done=False,
        raw_line="2. [ ] create `review.md` — notes",
    )
    assert is_review_todo(todo)
    result = fulfill_review_todo(mem, todo)
    assert result["tool"] == "review"
    assert result["status"] == "ok"
    assert "### `a.py`" in mem.read_prototype("review.md")


def test_review_never_empties_itself_on_a_prose_hint(tmp_path: Path) -> None:
    """Regression: a todo description mentioning "manifest" emptied the review."""
    mem = _memory(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    write_manifest(mem)
    path, reviews = write_review(mem, focus_hint="review every file in manifest")
    assert [r.path for r in reviews] == ["a.py"]
    assert "### `a.py`" in mem.read_prototype(path)


def test_parse_review_issues_skips_info(tmp_path: Path) -> None:
    text = (
        "# Code review\n\n"
        "### `src/db.py`\n\n"
        "- **high** (L12): Bare `except:` swallows all errors.\n"
        "- **low**: Debug `print` left in source.\n"
        "### `src/__init__.py`\n\n"
        "- **info**: No heuristic issues flagged — still skim for API/contract fit.\n"
    )
    parsed = parse_review_issues(text)
    assert [(i.path, i.severity, i.line) for i in parsed] == [
        ("src/db.py", "high", 12),
        ("src/db.py", "low", None),
        ("src/__init__.py", "info", None),
    ]
    action = actionable_issues(parsed)
    assert [i.path for i in action] == ["src/db.py", "src/db.py"]
