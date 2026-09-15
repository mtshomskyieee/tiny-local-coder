"""Token usage accounting for local LLM calls (no dollar cost)."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tinylocalcoder.config import get_settings


@dataclass
class UsageSnapshot:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    def add(self, prompt: int, completion: int) -> None:
        self.prompt_tokens += max(0, prompt)
        self.completion_tokens += max(0, completion)
        self.total_tokens = self.prompt_tokens + self.completion_tokens
        self.calls += 1


@dataclass
class UsageTracker:
    """Process-wide + workspace-persisted token counters."""

    session: UsageSnapshot = field(default_factory=UsageSnapshot)
    lifetime: UsageSnapshot = field(default_factory=UsageSnapshot)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _path: Path | None = None

    def _store_path(self) -> Path:
        if self._path is None:
            root = Path(get_settings().workspace_dir)
            root.mkdir(parents=True, exist_ok=True)
            idx = root / ".index"
            idx.mkdir(parents=True, exist_ok=True)
            self._path = idx / "usage.json"
        return self._path

    def load(self) -> None:
        path = self._store_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            life = data.get("lifetime") or data
            self.lifetime = UsageSnapshot(
                prompt_tokens=int(life.get("prompt_tokens", 0)),
                completion_tokens=int(life.get("completion_tokens", 0)),
                total_tokens=int(life.get("total_tokens", 0)),
                calls=int(life.get("calls", 0)),
            )
            if self.lifetime.total_tokens == 0:
                self.lifetime.total_tokens = (
                    self.lifetime.prompt_tokens + self.lifetime.completion_tokens
                )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    def save(self) -> None:
        path = self._store_path()
        payload = {"lifetime": asdict(self.lifetime)}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def record(self, prompt_tokens: int, completion_tokens: int) -> None:
        with self._lock:
            self.session.add(prompt_tokens, completion_tokens)
            self.lifetime.add(prompt_tokens, completion_tokens)
            self.save()

    def reset_session(self) -> None:
        with self._lock:
            self.session = UsageSnapshot()

    def format_report(self) -> str:
        with self._lock:
            s, l = self.session, self.lifetime
        return "\n".join(
            [
                "[b]Usage[/b] (tokens only — local model, no $ cost)",
                (
                    f"  session:  prompt={s.prompt_tokens}  "
                    f"completion={s.completion_tokens}  "
                    f"total={s.total_tokens}  calls={s.calls}"
                ),
                (
                    f"  lifetime: prompt={l.prompt_tokens}  "
                    f"completion={l.completion_tokens}  "
                    f"total={l.total_tokens}  calls={l.calls}"
                ),
            ]
        )


_tracker: UsageTracker | None = None
_tracker_lock = threading.Lock()


def get_usage_tracker() -> UsageTracker:
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = UsageTracker()
            _tracker.load()
        return _tracker


def extract_token_counts(message: Any) -> tuple[int, int]:
    """Best-effort prompt/completion counts from a LangChain AIMessage."""
    prompt = 0
    completion = 0

    usage = getattr(message, "usage_metadata", None) or {}
    if isinstance(usage, dict):
        prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        completion = int(
            usage.get("output_tokens") or usage.get("completion_tokens") or 0
        )

    meta = getattr(message, "response_metadata", None) or {}
    if isinstance(meta, dict):
        if not prompt:
            prompt = int(
                meta.get("prompt_eval_count")
                or meta.get("prompt_tokens")
                or (meta.get("token_usage") or {}).get("prompt_tokens")
                or 0
            )
        if not completion:
            completion = int(
                meta.get("eval_count")
                or meta.get("completion_tokens")
                or (meta.get("token_usage") or {}).get("completion_tokens")
                or 0
            )

    return prompt, completion


def record_from_message(message: Any) -> tuple[int, int]:
    prompt, completion = extract_token_counts(message)
    if prompt or completion:
        get_usage_tracker().record(prompt, completion)
    return prompt, completion
