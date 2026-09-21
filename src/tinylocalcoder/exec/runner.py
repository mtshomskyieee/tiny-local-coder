"""Subprocess runner confined to the workspace — tracks PIDs for /fix."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from tinylocalcoder.config import Settings, get_settings
from tinylocalcoder.exec.gate import ApprovalGate, Decision
from tinylocalcoder.exec.processes import (
    ProcessRegistry,
    extract_child_pids,
    extract_ports,
)
from tinylocalcoder.memory.files import WorkspaceMemory


@dataclass
class RunResult:
    command: str
    allowed: bool
    decision: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    pid: int | None = None


class CommandRunner:
    def __init__(
        self,
        memory: WorkspaceMemory,
        gate: ApprovalGate,
        settings: Settings | None = None,
    ) -> None:
        self.memory = memory
        self.gate = gate
        self.settings = settings or get_settings()
        self.cwd = Path(self.settings.workspace_dir)
        self.processes = ProcessRegistry(self.memory.processes_path)

    def run(
        self, command: str, reason: str = "", timeout: int | None = None
    ) -> RunResult:
        """Run one command. `timeout` overrides the default for slow steps
        such as `apt-get install`, which a 60s verify budget would kill."""
        limit = timeout or self.settings.exec_timeout_sec
        decision = self.gate.request(command, str(self.cwd), reason=reason)
        if decision == Decision.DENY:
            self.memory.append_exec_log(
                f"\n## DENIED\ncmd: `{command}`\nreason: {reason}\n"
            )
            return RunResult(command=command, allowed=False, decision=decision.value)

        ports = extract_ports(command)
        proc: subprocess.Popen[str] | None = None
        pid: int | None = None
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(self.cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,  # own process group for clean kill
            )
            pid = proc.pid
            try:
                pgid = os.getpgid(pid)
            except OSError:
                pgid = pid
            self.processes.register(
                pid,
                command,
                ports=ports,
                pgid=pgid,
                reason=reason,
            )

            try:
                stdout, stderr = proc.communicate(timeout=limit)
            except subprocess.TimeoutExpired:
                # Long-running server / hung command — kill the group but keep
                # registry entry so /fix can free the port later if needed.
                self._terminate(proc, pgid)
                stdout_b, stderr_b = proc.communicate(timeout=5)
                stdout = stdout_b or ""
                stderr = stderr_b or ""
                # Uvicorn may have logged a child PID before timeout
                out_all = f"{stdout}\n{stderr}"
                children = extract_child_pids(out_all)
                more_ports = extract_ports(command, out_all)
                # If children survived our kill, keep them tracked as running
                live_children = []
                for child in children:
                    try:
                        os.kill(child, 0)
                        live_children.append(child)
                    except OSError:
                        pass
                if live_children:
                    self.processes.update(
                        pid,
                        status="running",
                        children=live_children,
                        ports=more_ports,
                    )
                    # Re-register under child so port lookup works
                    for child in live_children:
                        self.processes.register(
                            child,
                            command,
                            ports=more_ports or ports,
                            pgid=pgid,
                            reason=f"orphan after timeout of pid {pid}",
                        )
                else:
                    self.processes.update(
                        pid,
                        status="timed_out",
                        exit_code=None,
                        children=children,
                        ports=more_ports,
                    )
                result = RunResult(
                    command=command,
                    allowed=True,
                    decision=decision.value,
                    exit_code=None,
                    stdout=stdout[-4000:],
                    stderr=stderr[-4000:],
                    error=f"timeout after {limit}s",
                    pid=pid,
                )
            else:
                out_all = f"{stdout}\n{stderr}"
                children = extract_child_pids(out_all)
                more_ports = extract_ports(command, out_all)
                self.processes.update(
                    pid,
                    status="exited",
                    exit_code=proc.returncode,
                    children=children,
                    ports=more_ports,
                )
                # If the shell exited but a server child is still alive, keep it
                for child in children:
                    try:
                        os.kill(child, 0)
                    except OSError:
                        continue
                    self.processes.register(
                        child,
                        command,
                        ports=more_ports or ports,
                        reason=f"server child of pid {pid}",
                    )
                result = RunResult(
                    command=command,
                    allowed=True,
                    decision=decision.value,
                    exit_code=proc.returncode,
                    stdout=(stdout or "")[-4000:],
                    stderr=(stderr or "")[-4000:],
                    pid=pid,
                )
        except OSError as exc:
            if pid is not None:
                self.processes.update(pid, status="exited")
            result = RunResult(
                command=command,
                allowed=True,
                decision=decision.value,
                error=str(exc),
                pid=pid,
            )

        self.memory.append_exec_log(
            "\n".join(
                [
                    f"\n## RUN ({result.decision})",
                    f"cmd: `{command}`",
                    f"pid: {result.pid}",
                    f"exit: {result.exit_code}",
                    f"stdout:\n```\n{result.stdout}\n```",
                    f"stderr:\n```\n{result.stderr}\n```",
                    f"error: {result.error}" if result.error else "",
                ]
            )
        )
        return result

    @staticmethod
    def _terminate(proc: subprocess.Popen[str], pgid: int) -> None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + 2
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
