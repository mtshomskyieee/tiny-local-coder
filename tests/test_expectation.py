"""The expect-clause matcher decides pass/fail for every run todo.

A false pass silently marks broken work done; a false fail burns the
fix/replan budget. Both directions are pinned here.
"""

from __future__ import annotations

import pytest

from tinylocalcoder.agents.execution import expectation_met, parse_expectation
from tinylocalcoder.memory.files import TodoStep


def _todo(desc: str, raw: str = "") -> TodoStep:
    return TodoStep(
        number=1,
        action="run",
        target='python3 -c "print(True)"',
        description=desc,
        done=False,
        raw_line=raw,
    )


# --- parsing the clause -----------------------------------------------


@pytest.mark.parametrize(
    ("desc", "expected"),
    [
        ("expect success", "success"),
        ("expecting True", "True"),
        ("print routes — expect /select", "/select"),
        ("expect 200.", "200"),
        ("", None),
        ("just run it", None),
    ],
)
def test_parse_expectation_from_description(desc: str, expected: str | None) -> None:
    assert parse_expectation(_todo(desc)) == expected


def test_parse_expectation_falls_back_to_raw_line() -> None:
    todo = _todo("", "1. [ ] run `python3 -c \"print(True)\"` — expect True")
    assert parse_expectation(todo) == "True"


def test_parse_expectation_ignores_skipped_tail() -> None:
    """A stale SKIPPED marker must not leak into the expected value."""
    todo = _todo("expect True — SKIPPED after failure")
    assert parse_expectation(todo) == "True"


# --- matching --------------------------------------------------------


def test_nonzero_exit_always_fails_even_with_matching_stdout() -> None:
    ok, detail = expectation_met("True", exit_code=1, stdout="True", stderr="")
    assert not ok
    assert "exit code 1" in detail


def test_missing_exit_code_fails() -> None:
    ok, _ = expectation_met("success", exit_code=None, stdout="", stderr="")
    assert not ok


def test_no_expect_clause_accepts_exit_zero() -> None:
    ok, detail = expectation_met(None, exit_code=0, stdout="", stderr="")
    assert ok
    assert "no expect clause" in detail


@pytest.mark.parametrize(
    "word",
    ["success", "ok", "exit 0", "exit=0", "exit code 0", "no error", "no errors", "succeed"],
)
def test_exit_only_expectations(word: str) -> None:
    """Multi-word phrasings must not fall through to substring matching."""
    ok, _ = expectation_met(word, exit_code=0, stdout="whatever", stderr="")
    assert ok


def test_boolean_expect_uses_last_stdout_line() -> None:
    ok, _ = expectation_met("True", exit_code=0, stdout="noise\nTrue\n", stderr="")
    assert ok


def test_boolean_expect_rejects_wrong_value() -> None:
    ok, detail = expectation_met("True", exit_code=0, stdout="False\n", stderr="")
    assert not ok
    assert "expected stdout" in detail


def test_route_path_expectation_matches_substring() -> None:
    stdout = "['/select', '/add', '/delete']"
    ok, _ = expectation_met("/select", exit_code=0, stdout=stdout, stderr="")
    assert ok


def test_expectation_strips_filler_words() -> None:
    stdout = "['/getbeer']"
    ok, _ = expectation_met(
        "a json response with /getbeer", exit_code=0, stdout=stdout, stderr=""
    )
    assert ok


def test_quoted_needle_is_preferred() -> None:
    ok, _ = expectation_met(
        "output containing `db.json`", exit_code=0, stdout="wrote db.json", stderr=""
    )
    assert ok


def test_expectation_can_match_stderr() -> None:
    """Many tools log to stderr; a successful run should still count."""
    ok, _ = expectation_met(
        "listening", exit_code=0, stdout="", stderr="INFO: listening on 8000"
    )
    assert ok


def test_unmatched_short_needle_fails() -> None:
    ok, detail = expectation_met("/select", exit_code=0, stdout="['/add']", stderr="")
    assert not ok
    assert "/select" in detail


def test_vague_long_expectation_accepts_exit_zero() -> None:
    """A 3B model writes essays as expect clauses; don't fail real work on that."""
    vague = (
        "the service should respond correctly to every endpoint and persist "
        "the records into the json database file as expected"
    )
    ok, detail = expectation_met(vague, exit_code=0, stdout="", stderr="")
    assert ok
    assert "too vague" in detail
