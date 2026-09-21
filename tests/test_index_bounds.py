"""The prototypes index must stay bounded — no archive re-ingestion.

Regression guard for a compounding loop: reindex_prototypes() used to walk the
whole workspace, so it read each archived snapshot's own .index/prototypes.json
back in, and /clear-workspace then moved the fattened index into the next
archive/<stamp>/. Every cycle indexed the previous cycle's index and the file
squared in size until the walk exhausted RAM.
"""

from __future__ import annotations

import json
from pathlib import Path

from tinylocalcoder.memory.files import WorkspaceMemory


def _index_paths(memory: WorkspaceMemory) -> set[str]:
    raw = memory.index.path_for("prototypes").read_text(encoding="utf-8")
    return {row["path"] for row in json.loads(raw)}


def test_reindex_skips_archive_tree(memory: WorkspaceMemory, tmp_path: Path) -> None:
    (tmp_path / "prototypes").mkdir(exist_ok=True)
    (tmp_path / "prototypes" / "live.py").write_text("print('live')\n", encoding="utf-8")

    old = tmp_path / "archive" / "20260101-000000"
    (old / ".index").mkdir(parents=True)
    (old / "prototypes").mkdir()
    (old / "prototypes" / "stale.py").write_text("print('stale')\n", encoding="utf-8")
    (old / ".index" / "prototypes.json").write_text("x" * 50_000, encoding="utf-8")

    memory.reindex_prototypes()
    paths = _index_paths(memory)

    assert "prototypes/live.py" in paths
    assert not any(p.startswith("archive") for p in paths)
    assert not any(".index" in p for p in paths)


def test_reindex_skips_oversized_and_binary_files(
    memory: WorkspaceMemory, tmp_path: Path
) -> None:
    (tmp_path / "prototypes").mkdir(exist_ok=True)
    (tmp_path / "prototypes" / "small.c").write_text("int main(){}\n", encoding="utf-8")
    (tmp_path / "prototypes" / "huge.json").write_text(
        "y" * (memory.settings.max_index_file_bytes + 1), encoding="utf-8"
    )
    (tmp_path / "prototypes" / "a.out").write_bytes(b"\x7fELF\x00\x00binary")

    memory.reindex_prototypes()
    paths = _index_paths(memory)

    assert "prototypes/small.c" in paths
    assert "prototypes/huge.json" not in paths
    assert "prototypes/a.out" not in paths


def test_clear_workspace_does_not_archive_the_index(
    memory: WorkspaceMemory, tmp_path: Path
) -> None:
    (tmp_path / "prototypes").mkdir(exist_ok=True)
    (tmp_path / "prototypes" / "app.py").write_text("print(1)\n", encoding="utf-8")
    memory.reindex_prototypes()

    dest = memory.clear_workspace()

    assert not (dest / ".index").exists()
    assert (tmp_path / ".index").exists()
    assert _index_paths(memory) == set()


def test_repeated_clear_cycles_do_not_grow_the_index(
    memory: WorkspaceMemory, tmp_path: Path
) -> None:
    sizes = []
    for i in range(4):
        (tmp_path / "prototypes").mkdir(exist_ok=True)
        (tmp_path / "prototypes" / f"m{i}.py").write_text(
            "print('x')\n" * 200, encoding="utf-8"
        )
        memory.reindex_prototypes()
        sizes.append(memory.index.path_for("prototypes").stat().st_size)
        memory.clear_workspace()

    # Each cycle indexes one same-sized file; without the archive skip these
    # would compound instead of staying flat.
    assert max(sizes) < 2 * min(sizes)
