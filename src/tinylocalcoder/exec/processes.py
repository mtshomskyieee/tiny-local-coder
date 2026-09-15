"""Track PIDs of processes started by the execute agent."""

from __future__ import annotations

import json
import os
import re
import signal
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


_PORT_RE = re.compile(
    r"(?:--port[= ]+|-p[= ]+|port\s*=\s*|:(?!//))(\d{2,5})",
    re.IGNORECASE,
)
_PORT_SIMPLE_RE = re.compile(r"\b(8\d{3}|9\d{3}|3000|5000|8000|8080|8888)\b")
_UVICORN_PID_RE = re.compile(
    r"Started server process \[(\d+)\]",
    re.IGNORECASE,
)


def extract_ports(command: str, output: str = "") -> list[int]:
    ports: list[int] = []
    for blob in (command or "", output or ""):
        for m in _PORT_RE.finditer(blob):
            try:
                p = int(m.group(1))
            except ValueError:
                continue
            if 1 <= p <= 65535 and p not in ports:
                ports.append(p)
        for m in _PORT_SIMPLE_RE.finditer(blob):
            try:
                p = int(m.group(1))
            except ValueError:
                continue
            if p not in ports:
                ports.append(p)
    return ports


def extract_child_pids(output: str) -> list[int]:
    found: list[int] = []
    for m in _UVICORN_PID_RE.finditer(output or ""):
        pid = int(m.group(1))
        if pid not in found:
            found.append(pid)
    return found


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    # Prefer /proc so zombies (state Z) count as dead for our purposes
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        try:
            # format: pid (comm) state ...
            body = stat_path.read_text(encoding="utf-8", errors="replace")
            close = body.rfind(")")
            if close != -1:
                state = body[close + 2 : close + 3]
                if state in {"Z", "X"}:
                    return False
            return True
        except OSError:
            pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class TrackedProcess:
    pid: int
    command: str
    ports: list[int] = field(default_factory=list)
    pgid: int | None = None
    children: list[int] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    status: str = "running"  # running | exited | killed | timed_out
    exit_code: int | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrackedProcess:
        return cls(
            pid=int(data.get("pid") or 0),
            command=str(data.get("command") or ""),
            ports=[int(p) for p in (data.get("ports") or [])],
            pgid=data.get("pgid"),
            children=[int(c) for c in (data.get("children") or [])],
            started_at=float(data.get("started_at") or time.time()),
            status=str(data.get("status") or "running"),
            exit_code=data.get("exit_code"),
            reason=str(data.get("reason") or ""),
        )


class ProcessRegistry:
    """Persisted list of execute-spawned processes under workspace/.index/."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[TrackedProcess]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        items = raw.get("processes") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        return [TrackedProcess.from_dict(x) for x in items if isinstance(x, dict)]

    def _save(self, items: list[TrackedProcess]) -> None:
        # Keep recent history capped
        items = items[-40:]
        payload = {"processes": [p.to_dict() for p in items]}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def refresh(self) -> list[TrackedProcess]:
        items = self._load()
        changed = False
        for p in items:
            if p.status == "running" and not pid_alive(p.pid):
                # If any child still alive, keep as running under child
                live_children = [c for c in p.children if pid_alive(c)]
                if live_children:
                    p.children = live_children
                    # Promote first live child as primary if shell died
                    if not pid_alive(p.pid):
                        p.pid = live_children[0]
                    continue
                p.status = "exited"
                changed = True
        if changed:
            self._save(items)
        return items

    def register(
        self,
        pid: int,
        command: str,
        *,
        ports: list[int] | None = None,
        pgid: int | None = None,
        reason: str = "",
    ) -> TrackedProcess:
        items = self.refresh()
        proc = TrackedProcess(
            pid=pid,
            command=command,
            ports=ports or extract_ports(command),
            pgid=pgid,
            reason=reason,
            status="running",
        )
        items.append(proc)
        self._save(items)
        return proc

    def update(
        self,
        pid: int,
        *,
        status: str | None = None,
        exit_code: int | None = None,
        children: list[int] | None = None,
        ports: list[int] | None = None,
    ) -> None:
        items = self._load()
        for p in items:
            if p.pid == pid or pid in p.children:
                if status:
                    p.status = status
                if exit_code is not None:
                    p.exit_code = exit_code
                if children:
                    for c in children:
                        if c not in p.children and c != p.pid:
                            p.children.append(c)
                if ports:
                    for port in ports:
                        if port not in p.ports:
                            p.ports.append(port)
                break
        self._save(items)

    def list_running(self) -> list[TrackedProcess]:
        return [p for p in self.refresh() if p.status == "running" and pid_alive(p.pid)]

    def find_by_port(self, port: int) -> list[TrackedProcess]:
        return [p for p in self.list_running() if port in p.ports]

    def kill_process(self, proc: TrackedProcess, *, sig: int = signal.SIGTERM) -> list[str]:
        notes: list[str] = []
        targets: list[int] = []
        if proc.pgid and proc.pgid > 0:
            targets.append(-proc.pgid)  # process group
        targets.append(proc.pid)
        targets.extend(proc.children)

        seen: set[int] = set()
        for target in targets:
            if target in seen or target == 0:
                continue
            seen.add(target)
            try:
                os.kill(target, sig)
                label = "pgid" if target < 0 else "pid"
                notes.append(f"sent signal {sig} to {label} {abs(target)}")
            except ProcessLookupError:
                notes.append(f"pid {abs(target)} already gone")
            except PermissionError:
                notes.append(f"no permission to kill pid {abs(target)}")
            except OSError as exc:
                # e.g. not a process-group leader when using -pgid
                notes.append(f"kill {target} failed: {exc}")
                if target < 0 and proc.pid not in seen:
                    # Fall through to direct pid kill below
                    continue
        # Escalate if still alive
        time.sleep(0.3)
        still = [pid for pid in [proc.pid, *proc.children] if pid_alive(pid)]
        if still:
            for pid in still:
                try:
                    os.kill(pid, signal.SIGKILL)
                    notes.append(f"SIGKILL pid {pid}")
                except OSError:
                    pass
            if proc.pgid:
                try:
                    os.kill(-proc.pgid, signal.SIGKILL)
                except OSError:
                    pass

        self.update(proc.pid, status="killed")
        # Reap if this process is our child (best-effort)
        for reap in {proc.pid, *proc.children}:
            try:
                os.waitpid(reap, os.WNOHANG)
            except ChildProcessError:
                pass
            except OSError:
                pass
        return notes

    def kill_port(self, port: int) -> list[str]:
        notes: list[str] = []
        for proc in self.find_by_port(port):
            notes.append(f"killing tracked pid {proc.pid} for port {port}: `{proc.command}`")
            notes.extend(self.kill_process(proc))
        return notes

    def kill_all_running(self) -> list[str]:
        notes: list[str] = []
        for proc in self.list_running():
            notes.append(f"killing tracked pid {proc.pid}: `{proc.command}`")
            notes.extend(self.kill_process(proc))
        return notes

    def format_report(self) -> str:
        items = self.refresh()
        if not items:
            return "No tracked execute processes."
        lines = ["Tracked execute processes:"]
        for p in items[-15:]:
            alive = "alive" if pid_alive(p.pid) else "dead"
            ports = ",".join(str(x) for x in p.ports) or "-"
            kids = ",".join(str(c) for c in p.children) or "-"
            lines.append(
                f"  pid={p.pid} [{p.status}/{alive}] ports={ports} "
                f"children={kids} cmd=`{p.command[:80]}`"
            )
        return "\n".join(lines)
