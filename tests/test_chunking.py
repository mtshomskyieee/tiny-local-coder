"""Lexical chunking + retrieval — how a 2048-token model sees big files.

Chunk boundaries must stay line-accurate (they are reported to the model as
L<start>-<end>) and retrieval must never return more than top_k.
"""

from __future__ import annotations

from tinylocalcoder.memory.chunking import (
    Chunk,
    retrieve_chunks,
    split_lines_into_chunks,
)


def test_empty_text_has_no_chunks() -> None:
    assert split_lines_into_chunks("") == []


def test_small_text_is_one_chunk_covering_every_line() -> None:
    chunks = split_lines_into_chunks("a\nb\nc\n")
    assert len(chunks) == 1
    assert (chunks[0].start_line, chunks[0].end_line) == (1, 3)
    assert chunks[0].chunk_id == "L1-3"
    assert chunks[0].text == "a\nb\nc\n"


def test_split_respects_max_chars_and_keeps_lines_contiguous() -> None:
    text = "".join(f"line {i}\n" for i in range(1, 51))
    chunks = split_lines_into_chunks(text, max_chars=40)

    assert len(chunks) > 1
    # No gaps and no overlap between consecutive chunks
    assert chunks[0].start_line == 1
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start_line == prev.end_line + 1
    assert chunks[-1].end_line == 50
    # Reassembling the chunks reproduces the file exactly
    assert "".join(c.text for c in chunks) == text
    # chunk_id always matches the line range it reports
    assert all(c.chunk_id == f"L{c.start_line}-{c.end_line}" for c in chunks)


def test_a_single_overlong_line_is_not_dropped() -> None:
    text = "x" * 5000 + "\n"
    chunks = split_lines_into_chunks(text, max_chars=100)
    assert len(chunks) == 1
    assert chunks[0].text == text


def test_retrieve_returns_empty_for_no_chunks() -> None:
    assert retrieve_chunks([], "anything") == []


def test_retrieve_without_query_returns_head() -> None:
    chunks = split_lines_into_chunks("".join(f"l{i}\n" for i in range(20)), max_chars=10)
    assert retrieve_chunks(chunks, "", top_k=2) == chunks[:2]


def test_retrieve_ranks_by_token_overlap() -> None:
    chunks = [
        Chunk("L1-1", 1, 1, "unrelated helper code\n"),
        Chunk("L2-2", 2, 2, "def select(): return db.json records\n"),
        Chunk("L3-3", 3, 3, "print('hello')\n"),
    ]
    best = retrieve_chunks(chunks, "select db.json", top_k=1)
    assert [c.chunk_id for c in best] == ["L2-2"]


def test_retrieve_caps_at_top_k() -> None:
    chunks = [Chunk(f"L{i}-{i}", i, i, "select db.json\n") for i in range(1, 10)]
    assert len(retrieve_chunks(chunks, "select", top_k=3)) == 3


def test_retrieve_ignores_one_character_tokens() -> None:
    """Single chars are noise at 2048 ctx — they must not drive ranking."""
    chunks = [
        Chunk("L1-1", 1, 1, "a b c\n"),
        Chunk("L2-2", 2, 2, "database records\n"),
    ]
    assert retrieve_chunks(chunks, "a b c", top_k=1)[0].chunk_id == "L1-1"
    assert retrieve_chunks(chunks, "records", top_k=1)[0].chunk_id == "L2-2"


def test_retrieve_is_case_insensitive() -> None:
    chunks = [
        Chunk("L1-1", 1, 1, "nothing here\n"),
        Chunk("L2-2", 2, 2, "def SelectAll(): ...\n"),
    ]
    assert retrieve_chunks(chunks, "selectall", top_k=1)[0].chunk_id == "L2-2"
