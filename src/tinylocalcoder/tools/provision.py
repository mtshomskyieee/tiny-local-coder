"""Toolchain provisioning — install a missing compiler into the container.

Deterministic, no LLM, in the spirit of `tools/manifest.py` and
`tools/review.py`. The registry already knows which apt packages back which
language; this module only decides *when* to install and records what it did.

Installs into a running container are ephemeral — they are lost on the next
image rebuild. Every install is therefore also appended to
`workspace/.toolchains`, which `build-service.sh` feeds to the Dockerfile's
`EXTRA_APT_PACKAGES` build arg so a rebuild bakes in what the agent learned it
needed, instead of re-installing every session.
"""

from __future__ import annotations

from pathlib import Path

from tinylocalcoder.memory.files import WorkspaceMemory
from tinylocalcoder.toolchains import (
    Toolchain,
    detect_toolchains,
    install_command,
    toolchain_for_binary,
)

# apt-get on a cold cache is far slower than a verify step; the default
# exec timeout (60s) would kill it midway and leave dpkg half-configured.
PROVISION_TIMEOUT_SEC = 600

RECORD_FILE = ".toolchains"


def missing_toolchains(memory: WorkspaceMemory) -> list[Toolchain]:
    """Toolchains the current plan needs that are not installed."""
    todos = memory.parse_todos()
    created = [t.target for t in todos if t.action in {"create", "write", "refine"}]
    runs = [t.target for t in todos if t.action in {"run", "test"}]
    return [tc for tc in detect_toolchains(created, runs) if tc.missing_probes()]


def toolchains_for_missing_binaries(names: list[str]) -> list[Toolchain]:
    """Map absent binaries (`make`, `g++`) onto the toolchains that install them."""
    out: list[Toolchain] = []
    for name in names:
        tc = toolchain_for_binary(name)
        if tc is not None and tc not in out:
            out.append(tc)
    return out


def record_packages(memory: WorkspaceMemory, toolchains: list[Toolchain]) -> list[str]:
    """Append installed package names to workspace/.toolchains, deduped."""
    packages: list[str] = []
    for tc in toolchains:
        packages.extend(tc.apt_packages)
    if not packages:
        return []
    path = Path(memory.root) / RECORD_FILE
    existing = (
        path.read_text(encoding="utf-8").split() if path.exists() else []
    )
    merged = list(dict.fromkeys(existing + packages))
    path.write_text(" ".join(merged) + "\n", encoding="utf-8")
    return merged


def provision(
    memory: WorkspaceMemory, runner, toolchains: list[Toolchain]
) -> tuple[bool, str]:
    """Install the given toolchains. Returns (ok, human-readable note).

    The command goes through the normal `CommandRunner`, so the approval gate
    still governs it — provisioning is never silent.
    """
    command = install_command(toolchains)
    if not command:
        return False, "nothing to install"
    names = ", ".join(tc.name for tc in toolchains)
    result = runner.run(
        command,
        reason=f"install missing {names} toolchain",
        timeout=PROVISION_TIMEOUT_SEC,
    )
    if not result.allowed:
        return False, f"install of {names} was {result.decision}"
    if result.exit_code != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else f"exit {result.exit_code}"
        return False, f"install of {names} failed: {detail}"
    record_packages(memory, toolchains)
    still_missing = [p for tc in toolchains for p in tc.missing_probes()]
    if still_missing:
        return False, f"installed {names} but {', '.join(still_missing)} still absent"
    return True, f"installed {names} toolchain ({command})"
