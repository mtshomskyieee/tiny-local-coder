"""JSON indexes over workspace files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tinylocalcoder.memory.chunking import Chunk, split_lines_into_chunks


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
        entries: list[dict[str, Any]] = []
        if prototypes_dir.exists():
            for path in sorted(prototypes_dir.rglob("*")):
                if path.is_file():
                    rel = str(path.relative_to(prototypes_dir.parent))
                    text = path.read_text(encoding="utf-8", errors="replace")
                    for c in split_lines_into_chunks(text, self.max_chunk_chars):
                        entries.append(
                            {
                                "chunk_id": f"{rel}:{c.chunk_id}",
                                "start_line": c.start_line,
                                "end_line": c.end_line,
                                "text": c.text,
                                "path": rel,
                            }
                        )
        self.path_for("prototypes").write_text(json.dumps(entries, indent=2), encoding="utf-8")
        return entries
