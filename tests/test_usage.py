"""Token accounting — the only feedback the user gets on a local model's cost.

`extract_token_counts` must cope with whichever metadata shape the Ollama
wrapper hands back; a silent miss makes the /usage report lie.
"""

from __future__ import annotations

import json
from pathlib import Path

from tinylocalcoder.usage import (
    UsageSnapshot,
    UsageTracker,
    extract_token_counts,
    record_from_message,
)


class _Msg:
    def __init__(self, usage_metadata=None, response_metadata=None) -> None:
        if usage_metadata is not None:
            self.usage_metadata = usage_metadata
        if response_metadata is not None:
            self.response_metadata = response_metadata


def test_snapshot_add_keeps_total_and_call_count() -> None:
    snap = UsageSnapshot()
    snap.add(10, 5)
    snap.add(1, 2)
    assert (snap.prompt_tokens, snap.completion_tokens) == (11, 7)
    assert snap.total_tokens == 18
    assert snap.calls == 2


def test_snapshot_add_clamps_negatives() -> None:
    snap = UsageSnapshot()
    snap.add(-5, 3)
    assert snap.prompt_tokens == 0
    assert snap.total_tokens == 3


def test_extract_prefers_usage_metadata() -> None:
    msg = _Msg(usage_metadata={"input_tokens": 100, "output_tokens": 20})
    assert extract_token_counts(msg) == (100, 20)


def test_extract_accepts_legacy_key_names() -> None:
    msg = _Msg(usage_metadata={"prompt_tokens": 7, "completion_tokens": 3})
    assert extract_token_counts(msg) == (7, 3)


def test_extract_falls_back_to_ollama_response_metadata() -> None:
    """Ollama reports prompt_eval_count / eval_count, not input/output tokens."""
    msg = _Msg(response_metadata={"prompt_eval_count": 512, "eval_count": 64})
    assert extract_token_counts(msg) == (512, 64)


def test_extract_falls_back_to_nested_token_usage() -> None:
    msg = _Msg(
        response_metadata={"token_usage": {"prompt_tokens": 9, "completion_tokens": 4}}
    )
    assert extract_token_counts(msg) == (9, 4)


def test_extract_fills_only_missing_half() -> None:
    msg = _Msg(
        usage_metadata={"input_tokens": 11, "output_tokens": 0},
        response_metadata={"eval_count": 22},
    )
    assert extract_token_counts(msg) == (11, 22)


def test_extract_handles_missing_and_malformed_metadata() -> None:
    assert extract_token_counts(object()) == (0, 0)
    assert extract_token_counts(_Msg(usage_metadata=None)) == (0, 0)
    assert extract_token_counts(_Msg(usage_metadata="not-a-dict")) == (0, 0)


def test_record_persists_lifetime_and_survives_reload(tmp_path: Path) -> None:
    tracker = UsageTracker()
    tracker.record(100, 50)
    tracker.record(10, 5)

    store = tmp_path / ".index" / "usage.json"
    assert store.exists(), "lifetime usage must be written to the workspace"
    payload = json.loads(store.read_text(encoding="utf-8"))
    assert payload["lifetime"]["total_tokens"] == 165

    # A fresh process keeps lifetime but starts a clean session
    reloaded = UsageTracker()
    reloaded.load()
    assert reloaded.lifetime.total_tokens == 165
    assert reloaded.lifetime.calls == 2
    assert reloaded.session.total_tokens == 0


def test_reset_session_keeps_lifetime() -> None:
    tracker = UsageTracker()
    tracker.record(100, 50)
    tracker.reset_session()
    assert tracker.session.total_tokens == 0
    assert tracker.lifetime.total_tokens == 150


def test_load_tolerates_corrupt_store(tmp_path: Path) -> None:
    idx = tmp_path / ".index"
    idx.mkdir(parents=True, exist_ok=True)
    (idx / "usage.json").write_text("{not json", encoding="utf-8")
    tracker = UsageTracker()
    tracker.load()  # must not raise
    assert tracker.lifetime.total_tokens == 0


def test_load_recomputes_missing_total(tmp_path: Path) -> None:
    idx = tmp_path / ".index"
    idx.mkdir(parents=True, exist_ok=True)
    (idx / "usage.json").write_text(
        json.dumps({"lifetime": {"prompt_tokens": 4, "completion_tokens": 6}}),
        encoding="utf-8",
    )
    tracker = UsageTracker()
    tracker.load()
    assert tracker.lifetime.total_tokens == 10


def test_record_from_message_skips_empty_usage(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    tracker = UsageTracker()
    monkeypatch.setattr(tracker, "record", lambda p, c: calls.append((p, c)))
    monkeypatch.setattr("tinylocalcoder.usage.get_usage_tracker", lambda: tracker)

    assert record_from_message(_Msg(usage_metadata={})) == (0, 0)
    assert calls == [], "a metadata-less reply must not count as a call"
    assert record_from_message(_Msg(usage_metadata={"input_tokens": 3})) == (3, 0)
    assert calls == [(3, 0)]


def test_format_report_shows_session_and_lifetime() -> None:
    tracker = UsageTracker()
    tracker.record(100, 50)
    report = tracker.format_report()
    assert "session" in report and "lifetime" in report
    assert "total=150" in report
