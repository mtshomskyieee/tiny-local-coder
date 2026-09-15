"""Deterministic workspace inventory → ``manifest.txt`` (no LLM)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tinylocalcoder.memory.files import TodoStep, WorkspaceMemory

MANIFEST_NAME = "manifest.txt"
REVIEW_ARTIFACTS = frozenset({"manifest.txt", "review.md"})

# Match /review prompt: skip archive/, .index/, plan.md, ask.md, exec.log, session.md
SKIP_DIR_NAMES = frozenset(
    {
        "archive",
        ".index",
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        ".eggs",
    }
)
SKIP_ROOT_FILES = frozenset(
    {
        "plan.md",
        "ask.md",
        "exec.log",
        "session.md",
        "manifest.txt",
        "review.md",
    }
)
# Prefer reviewable text; empty means accept any non-skipped file
SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".md",
        ".txt",
        ".toml",
        ".json",
        ".yml",
        ".yaml",
        ".sh",
        ".cfg",
        ".ini",
        ".rst",
        ".css",
        ".html",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
    }
)

_FOCUS_RE = re.compile(
    r"(?:focus(?:\s+on)?|only|under|in)\s+[`'\"]?([\w./-]+/?)[`'\"]?",
    re.IGNORECASE,
)


def parse_focus_prefixes(text: str) -> list[str]:
    """Extract path prefixes from notes like 'focus on src/' or 'only tests/'."""
    found: list[str] = []
    for m in _FOCUS_RE.finditer(text or ""):
        prefix = (m.group(1) or "").strip().lstrip("./")
        if not prefix:
            continue
        if not prefix.endswith("/") and "." not in Path(prefix).name:
            prefix = prefix + "/"
        if prefix not in found:
            found.append(prefix)
    return found


def is_manifest_todo(todo: TodoStep) -> bool:
    """True when a create/refine todo targets ``manifest.txt``."""
    if todo.action not in {"create", "write", "refine"}:
        return False
    target = (todo.target or "").replace("\\", "/").strip().lstrip("./")
    if target == MANIFEST_NAME or target.endswith(f"/{MANIFEST_NAME}"):
        return True
    # Path buried only in description/raw line
    blob = f"{todo.raw_line} {todo.description}".lower()
    return "manifest.txt" in blob and todo.action in {"create", "write", "refine"}


def _should_skip(rel: Path) -> bool:
    parts = rel.parts
    if any(p in SKIP_DIR_NAMES for p in parts):
        return True
    # Skip hidden directories (e.g. .cache/) but allow root dotfiles if source-like
    if any(p.startswith(".") for p in parts[:-1]):
        return True
    name = rel.name
    if name in SKIP_ROOT_FILES and len(parts) == 1:
        return True
    if name in REVIEW_ARTIFACTS:
        return True
    if name.endswith((".pyc", ".pyo", ".so", ".dylib", ".egg", ".whl")):
        return True
    if name == ".DS_Store":
        return True
    return False


def _is_source_like(rel: Path) -> bool:
    if not SOURCE_SUFFIXES:
        return True
    suffix = rel.suffix.lower()
    if suffix in SOURCE_SUFFIXES:
        return True
    # extensionless scripts sometimes appear; skip by default
    return False


def collect_manifest_paths(
    root: Path,
    *,
    focus_prefixes: list[str] | None = None,
) -> list[str]:
    """Return sorted relative paths for a review inventory."""
    root = root.resolve()
    focus = [p.replace("\\", "/").lstrip("./") for p in (focus_prefixes or []) if p]
    out: list[str] = []
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if _should_skip(rel):
            continue
        if not _is_source_like(rel):
            continue
        rel_s = rel.as_posix()
        if focus and not any(
            rel_s == p.rstrip("/") or rel_s.startswith(p) for p in focus
        ):
            continue
        out.append(rel_s)
    return out


def build_manifest(
    memory: WorkspaceMemory,
    *,
    focus_prefixes: list[str] | None = None,
    focus_hint: str = "",
) -> list[str]:
    """Inventory workspace files; does not write disk."""
    prefixes = list(focus_prefixes or [])
    for p in parse_focus_prefixes(focus_hint):
        if p not in prefixes:
            prefixes.append(p)
    return collect_manifest_paths(memory.root, focus_prefixes=prefixes or None)


def write_manifest(
    memory: WorkspaceMemory,
    *,
    focus_prefixes: list[str] | None = None,
    focus_hint: str = "",
    out_name: str = MANIFEST_NAME,
) -> tuple[str, list[str]]:
    """Write ``manifest.txt`` (one path per line). Returns (path, paths)."""
    paths = build_manifest(
        memory, focus_prefixes=focus_prefixes, focus_hint=focus_hint
    )
    body = "\n".join(paths) + ("\n" if paths else "")
    memory.write_prototype(out_name, body)
    return out_name, paths


def fulfill_manifest_todo(
    memory: WorkspaceMemory,
    todo: TodoStep,
    *,
    focus_hint: str = "",
) -> dict:
    """Tool path for a create/refine ``manifest.txt`` todo."""
    hint = " ".join(
        p for p in (focus_hint, todo.description, todo.raw_line) if p
    )
    path, paths = write_manifest(memory, focus_hint=hint)
    memory.mark_todo_done(todo.number)
    n = len(paths)
    return {
        "last_file": path,
        "output": f"Step {todo.number}: wrote `{path}` via tool ({n} path{'s' if n != 1 else ''})",
        "phase": "coding",
        "status": "ok",
        "pending_initial_files": [
            t.target
            for t in memory.parse_todos()
            if not t.done and t.action in {"create", "write", "refine"}
        ],
        "tool": "manifest",
        "manifest_count": n,
    }


def fulfill_open_manifest_todos(
    memory: WorkspaceMemory,
    *,
    focus_hint: str = "",
) -> list[dict]:
    """Complete every open create/refine manifest todo with the tool."""
    results: list[dict] = []
    for todo in list(memory.pending_todos()):
        if is_manifest_todo(todo):
            results.append(
                fulfill_manifest_todo(memory, todo, focus_hint=focus_hint)
            )
    return results
