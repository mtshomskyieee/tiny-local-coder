"""Lexical chunking and retrieval (no embeddings — save RAM)."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Chunk:
    chunk_id: str
    start_line: int
    end_line: int
    text: str


def split_lines_into_chunks(text: str, max_chars: int = 1200) -> list[Chunk]:
    lines = text.splitlines(keepends=True)
    if not lines:
        return []

    chunks: list[Chunk] = []
    buf: list[str] = []
    start = 1
    size = 0
    line_no = 0

    for line in lines:
        line_no += 1
        if buf and size + len(line) > max_chars:
            body = "".join(buf)
            chunks.append(
                Chunk(
                    chunk_id=f"L{start}-{line_no - 1}",
                    start_line=start,
                    end_line=line_no - 1,
                    text=body,
                )
            )
            buf = [line]
            start = line_no
            size = len(line)
        else:
            buf.append(line)
            size += len(line)

    if buf:
        body = "".join(buf)
        chunks.append(
            Chunk(
                chunk_id=f"L{start}-{line_no}",
                start_line=start,
                end_line=line_no,
                text=body,
            )
        )
    return chunks


def _tokenize(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-zA-Z0-9_./-]+", text.lower()) if len(t) > 1}


def retrieve_chunks(chunks: list[Chunk], query: str, top_k: int = 3) -> list[Chunk]:
    if not chunks:
        return []
    q = _tokenize(query)
    if not q:
        return chunks[:top_k]

    scored: list[tuple[float, Chunk]] = []
    for ch in chunks:
        tokens = _tokenize(ch.text)
        overlap = len(q & tokens)
        scored.append((float(overlap), ch))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:top_k]]
