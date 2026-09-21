"""Live workspace only: archived sessions are not the working tree.

/test walked workspace/archive/ looking for test files, so a plan built from
it targeted files from finished sessions. list_files() returned the archive
and left the exclusion to each caller; six remembered and three did not.
"""

from __future__ import annotations

from pathlib import Path

from tinylocalcoder.agents.workflows import _list_workspace_tests, plan_requirement_gaps
from tinylocalcoder.memory.files import WorkspaceMemory


def _seed(root: Path) -> None:
    """One live test file, and an archived session full of decoys."""
    (root / "test_live.py").write_text("def test_live(): pass\n", encoding="utf-8")
    (root / "app.py").write_text("print('live')\n", encoding="utf-8")

    old = root / "archive" / "20260101-000000"
    (old / "tests").mkdir(parents=True)
    (old / "test_archived.py").write_text("def test_old(): pass\n", encoding="utf-8")
    (old / "tests" / "test_nested.py").write_text("def test_n(): pass\n", encoding="utf-8")
    (old / "legacy.py").write_text("print('old')\n", encoding="utf-8")


def test_list_files_excludes_archive(memory: WorkspaceMemory, tmp_path: Path) -> None:
    _seed(tmp_path)
    files = memory.list_files()

    assert "test_live.py" in files
    assert not any(f.startswith("archive") for f in files)


def test_list_files_can_opt_into_archive(memory: WorkspaceMemory, tmp_path: Path) -> None:
    _seed(tmp_path)
    files = memory.list_files(include_archive=True)

    assert any(f.startswith("archive") for f in files)


def test_test_workflow_ignores_archived_tests(
    memory: WorkspaceMemory, tmp_path: Path
) -> None:
    _seed(tmp_path)
    found = _list_workspace_tests(memory)

    assert found == ["test_live.py"]
    assert not any("archive" in f for f in found)


def test_requirement_gaps_ignore_archive(
    memory: WorkspaceMemory, tmp_path: Path
) -> None:
    _seed(tmp_path)
    # Names a file that exists only in the archive; it must not count as present.
    gaps = plan_requirement_gaps(memory, "legacy.py must exist")

    assert all("archive" not in g for g in gaps)


def test_find_module_py_ignores_archive(
    memory: WorkspaceMemory, tmp_path: Path
) -> None:
    _seed(tmp_path)
    assert memory.find_module_py("legacy") is None
    assert memory.find_module_py("app") == "app.py"
