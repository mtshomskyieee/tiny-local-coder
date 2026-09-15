"""User approval gate for shell commands."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ALLOW_ALL = "allow_all"


@dataclass
class PendingCommand:
    command_id: str
    command: str
    cwd: str
    reason: str = ""


class ApprovalCallback(Protocol):
    def __call__(self, pending: PendingCommand) -> Decision: ...


@dataclass
class ApprovalGate:
    """Blocks until the host (TUI/API) supplies a decision."""

    callback: ApprovalCallback | None = None
    allow_all: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _pending: dict[str, PendingCommand] = field(default_factory=dict)
    _events: dict[str, threading.Event] = field(default_factory=dict)
    _decisions: dict[str, Decision] = field(default_factory=dict)

    def set_callback(self, callback: ApprovalCallback | None) -> None:
        self.callback = callback

    def reset_session(self) -> None:
        with self._lock:
            self.allow_all = False

    def request(self, command: str, cwd: str, reason: str = "") -> Decision:
        if self.allow_all:
            return Decision.ALLOW_ALL

        pending = PendingCommand(
            command_id=str(uuid.uuid4()),
            command=command,
            cwd=cwd,
            reason=reason,
        )

        if self.callback is not None:
            decision = self.callback(pending)
            if decision == Decision.ALLOW_ALL:
                self.allow_all = True
            return decision

        # Async/API path: park until approve() is called
        event = threading.Event()
        with self._lock:
            self._pending[pending.command_id] = pending
            self._events[pending.command_id] = event

        # Expose id on the pending object for callers that inspect state
        self.last_pending = pending  # type: ignore[attr-defined]
        event.wait()
        with self._lock:
            decision = self._decisions.pop(pending.command_id, Decision.DENY)
            self._pending.pop(pending.command_id, None)
            self._events.pop(pending.command_id, None)
        if decision == Decision.ALLOW_ALL:
            self.allow_all = True
        return decision

    def get_pending(self, command_id: str) -> PendingCommand | None:
        with self._lock:
            return self._pending.get(command_id)

    def list_pending(self) -> list[PendingCommand]:
        with self._lock:
            return list(self._pending.values())

    def approve(self, command_id: str, decision: Decision | str) -> bool:
        if isinstance(decision, str):
            decision = Decision(decision)
        with self._lock:
            event = self._events.get(command_id)
            if event is None:
                return False
            self._decisions[command_id] = decision
            event.set()
            return True

    def create_pending_for_api(self, command: str, cwd: str, reason: str = "") -> PendingCommand:
        """Park a command and return it without blocking (API interrupt style)."""
        pending = PendingCommand(
            command_id=str(uuid.uuid4()),
            command=command,
            cwd=cwd,
            reason=reason,
        )
        event = threading.Event()
        with self._lock:
            self._pending[pending.command_id] = pending
            self._events[pending.command_id] = event
        self.last_pending = pending  # type: ignore[attr-defined]
        return pending
