"""JSON indexes over workspace files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tinylocalcoder.memory.chunking import Chunk, split_lines_into_chunks

# Directory names that must never be walked into when building an index.
#
# `archive/` is the load-bearing one. Without it every archived snapshot is
# re-ingested on each reindex — including that snapshot's own
# .index/prototypes.json — and /clear-workspace then moves the fattened index
# into the next archive/<stamp>/. Each cycle indexes the previous cycle's
# index, so the file squares in size (8 MB -> 21 -> 82 -> 207 -> 531 -> 1.4 GB
# was observed) until the walk exhausts RAM and swaps the host to a standstill.
SKIP_DIRS = frozenset({"archive", ".index", "__pycache__", ".git", "node_modules"})

# Anything the model could not usefully read a chunk of. Compiled output lands
# in the workspace next to its source, and chunking an ELF is pure waste.
BINARY_SUFFIXES = frozenset(
    {".o", ".a", ".so", ".pyc", ".bin", ".exe", ".class", ".jar", ".wasm",
     ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".tar"}
)


def is_skipped_path(rel: Path) -> bool:
    """True when `rel` (workspace-relative) lives under a directory we skip."""
    return any(part in SKIP_DIRS for part in rel.parts)


def looks_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(8192)
    except OSError:
        return True


class FileIndex:
    def __init__(self, index_dir: Path, max_chunk_chars: int = 1200) -> None:
        self.index_dir = index_dir
        self.max_chunk_chars = max_chunk_chars
        self.index_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, name: str) -> Path:
        return self.index_dir / f"{name}.json"

    def rebuild(self, name: str, file_path: Path) -> list[dict[str, Any]]:
        text = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
        chunks = split_lines_into_chunks(text, self.max_chunk_chars)
        payload = [
            {
                "chunk_id": c.chunk_id,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "text": c.text,
                "path": str(file_path),
            }
            for c in chunks
        ]
        self.path_for(name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def load(self, name: str) -> list[dict[str, Any]]:
        p = self.path_for(name)
        if not p.exists():
            return []
        return json.loads(p.read_text(encoding="utf-8"))

    def as_chunks(self, name: str) -> list[Chunk]:
        return [
            Chunk(
                chunk_id=row["chunk_id"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                text=row["text"],
            )
            for row in self.load(name)
        ]

    def rebuild_prototypes(self, prototypes_dir: Path) -> list[dict[str, Any]]:
        entries = build_chunk_entries(prototypes_dir, prototypes_dir.parent)
        self.path_for("prototypes").write_text(json.dumps(entries), encoding="utf-8")
        return entries


def build_chunk_entries(
    root: Path,
    rel_to: Path,
    max_chunk_chars: int = 1200,
    max_file_bytes: int = 262_144,
    max_total_chars: int = 4_194_304,
    skip_top_level: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Chunk every indexable text file under `root`, paths relative to `rel_to`.

    Bounded on three axes so a large workspace cannot blow up memory: skipped
    directories (see SKIP_DIRS), a per-file byte ceiling, and a total character
    budget after which indexing stops.
    """
    entries: list[dict[str, Any]] = []
    if not root.exists():
        return entries

    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(rel_to)
        if is_skipped_path(rel):
            continue
        if len(rel.parts) == 1 and rel.name in skip_top_level:
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
        except OSError:
            continue
        if looks_binary(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for c in split_lines_into_chunks(text, max_chunk_chars):
            entries.append(
                {
                    "chunk_id": f"{rel}:{c.chunk_id}",
                    "start_line": c.start_line,
                    "end_line": c.end_line,
                    "text": c.text,
                    "path": str(rel),
                }
            )
            total += len(c.text)
        if total >= max_total_chars:
            break

    return entries
