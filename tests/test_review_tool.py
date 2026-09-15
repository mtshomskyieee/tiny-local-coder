"""Tests for deterministic review tool."""

from __future__ import annotations

from pathlib import Path

from tinylocalcoder.config import Settings
from tinylocalcoder.memory.files import TodoStep, WorkspaceMemory
from tinylocalcoder.tools.manifest import write_manifest
from tinylocalcoder.tools.review import (
    fulfill_review_todo,
    is_review_todo,
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
