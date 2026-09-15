"""Workspace file operations and numbered todo plan parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from tinylocalcoder.config import Settings, get_settings
from tinylocalcoder.memory.chunking import retrieve_chunks
from tinylocalcoder.memory.index import FileIndex


# Legacy checkbox lines under Deliverables / Run / Test
_CHECKBOX_RE = re.compile(
    r"^- \[([ xX])\]\s+(?:`([^`]+)`|(\S+))\s*(?:—|--|-)?\s*(.*)$"
)

# Numbered todos: 1. [ ] create … | 1. [x] done | 1. [!] skipped/failed
_TODO_RE = re.compile(
    r"^(\d+)\.\s+\[([ xX!])\]\s+"
    r"(?:(create|write|refine|update|fix|run|test)\s+)?"
    r"(?:`([^`]+)`|(\S+))\s*"
    r"(?:—|--|-)?\s*(.*)$",
    re.IGNORECASE,
)

_GOAL_RE = re.compile(r"^(?:#\s*)?Goal:\s*(.+)$", re.IGNORECASE)
_DONE_RE = re.compile(r"^Done:\s*(.+)$", re.IGNORECASE)

# Max numbered todos after finalize (tiny-model safety net)
PLAN_TODO_CAP = 12
REPLAN_OPEN_CAP = 3

# TUI meta commands that must never become plan todos
META_TODO_NAMES = frozenset(
    {
        "help",
        "clear",
        "new",
        "model",
        "usage",
        "exit",
        "quit",
        "q",
        "show-plan",
        "plan-show",
        "clear-plan",
        "plan-clear",
        "archive-plan",
        "plan-archive",
        "plan-edit",
        "edit-plan",
        "archive",
        "clear-workspace",
        "workspace-clear",
        "auto-fix",
        "autofix",
        "auto-skip",
        "autoskip",
        "auto-replan",
        "autoreplan",
        "skip-todo",
        "reset-todo",
        "reset-all-skipped",
        "reset-skipped",
        "review",
        "test",
        "procs",
        "processes",
        "kill-procs",
        "execute-plan",
        "exec-plan",
        "run-plan",
    }
)


def meta_command_name(text: str) -> str | None:
    """Return meta command name if text is (or starts with) a known meta cmd."""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("/"):
        raw = raw[1:]
    first = raw.split(maxsplit=1)[0].lower()
    if first in META_TODO_NAMES:
        return first
    return None


def is_meta_todo_target(target: str) -> bool:
    """True when a todo target looks like a TUI meta-command, not a run/create."""
    t = (target or "").strip().lstrip("/")
    if not t:
        return False
    first = t.split(maxsplit=1)[0].lower()
    if first in META_TODO_NAMES:
        return True
    # Follow-on files / imports invented after meta leakage (reset_todo_8.py)
    if re.search(r"\breset_todo_\d+\b", t, re.IGNORECASE):
        return True
    if re.search(r"\bskip_todo_\d+\b", t, re.IGNORECASE):
        return True
    return False


_FROM_MOD_IMPORT_MOD = re.compile(
    r"""from\s+(\w+)\s+import\s+\1\b""",
    re.IGNORECASE,
)


def is_valid_run_command(command: str) -> bool:
    """True when a run todo looks like an allowed short shell command."""
    c = (command or "").strip()
    if not c:
        return False
    if c.startswith("PYTHONPATH="):
        return "python3" in c or "python " in c or "pytest" in c
    first = c.split(maxsplit=1)[0]
    return first in {"python3", "python", "pytest", "bash", "sh"} or first.startswith(
        "./"
    )


def strip_placeholder_path(path: str) -> str:
    """Remove LLM placeholder prefixes like path/to/ from workspace paths."""
    p = (path or "").strip().lstrip("/").lstrip("./").replace("\\", "/")
    while True:
        if p.startswith("path/to/"):
            p = p[len("path/to/") :]
            continue
        if p.startswith("./path/to/"):
            p = p[len("./path/to/") :]
            continue
        break
    return p


def strip_placeholders_in_command(command: str) -> str:
    """Strip path/to/ placeholders inside shell commands."""
    c = command or ""
    c = c.replace("./path/to/", "").replace("path/to/", "")
    return c


def looks_fastapi_goal(goal: str, todos: list | None = None) -> bool:
    blob = (goal or "").lower()
    if any(k in blob for k in ("fastapi", "rest", "endpoint", "uvicorn")):
        return True
    for t in todos or []:
        text = f"{t.target} {t.description}".lower()
        if "fastapi" in text or "endpoint" in text:
            return True
    return False


@dataclass
class PlanItem:
    """Legacy item (deliverable / run_test)."""

    kind: str
    path_or_cmd: str
    description: str
    done: bool
    raw_line: str


@dataclass
class TodoStep:
    """Single numbered plan step for small-context execution."""

    number: int
    action: str  # create | refine | run
    target: str  # file path or shell command
    description: str
    done: bool
    raw_line: str
    skipped: bool = False

    @property
    def kind(self) -> str:
        if self.action in {"run", "test"}:
            return "run_test"
        return "deliverable"

    @property
    def path_or_cmd(self) -> str:
        return self.target


class WorkspaceMemory:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.root = Path(self.settings.workspace_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.prototypes_dir = self.root / "prototypes"
        self.prototypes_dir.mkdir(parents=True, exist_ok=True)
        self.index = FileIndex(self.root / ".index", self.settings.max_chunk_chars)
        self.plan_path = self.root / "plan.md"
        self.ask_path = self.root / "ask.md"
        self.exec_log_path = self.root / "exec.log"
        self.session_path = self.root / "session.md"
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        if not self.plan_path.exists():
            self.plan_path.write_text(
                "# Plan\n\nGoal:\n\n## Todos\n",
                encoding="utf-8",
            )
            self.reindex_plan()
        if not self.ask_path.exists():
            self.ask_path.write_text("# Ask\n\n", encoding="utf-8")
            self.reindex_ask()
        if not self.exec_log_path.exists():
            self.exec_log_path.write_text("# Exec log\n\n", encoding="utf-8")
            self.reindex_exec()
        if not self.session_path.exists():
            self.session_path.write_text("# Session\n\n", encoding="utf-8")

    def reindex_plan(self) -> None:
        self.index.rebuild("plan", self.plan_path)

    def reindex_ask(self) -> None:
        self.index.rebuild("ask", self.ask_path)

    def reindex_exec(self) -> None:
        self.index.rebuild("exec", self.exec_log_path)

    def reindex_prototypes(self) -> None:
        entries: list[dict] = []
        skip = {".index", "plan.md", "ask.md", "exec.log", "session.md"}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root)
            if rel.parts and rel.parts[0] == ".index":
                continue
            if rel.name in skip and len(rel.parts) == 1:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            from tinylocalcoder.memory.chunking import split_lines_into_chunks

            for c in split_lines_into_chunks(text, self.settings.max_chunk_chars):
                entries.append(
                    {
                        "chunk_id": f"{rel}:{c.chunk_id}",
                        "start_line": c.start_line,
                        "end_line": c.end_line,
                        "text": c.text,
                        "path": str(rel),
                    }
                )
        self.index.path_for("prototypes").write_text(
            __import__("json").dumps(entries, indent=2), encoding="utf-8"
        )

    def read_plan(self) -> str:
        return self.plan_path.read_text(encoding="utf-8")

    def write_plan(self, content: str) -> None:
        self.plan_path.write_text(content, encoding="utf-8")
        self.reindex_plan()

    def append_ask(self, role: str, text: str) -> None:
        with self.ask_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n### {role}\n{text.strip()}\n")
        self.reindex_ask()

    def append_session(self, role: str, text: str) -> None:
        """Append a turn to session.md (full TUI input/response transcript)."""
        from datetime import datetime

        body = (text or "").strip()
        if not body:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.session_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n### {role} · {stamp}\n{body}\n")

    def clear_session(self) -> None:
        self.session_path.write_text("# Session\n\n", encoding="utf-8")

    def clear_ask(self) -> None:
        self.ask_path.write_text("# Ask\n\n", encoding="utf-8")
        self.reindex_ask()

    def clear_session_logs(self) -> None:
        self.clear_ask()
        self.exec_log_path.write_text("# Exec log\n\n", encoding="utf-8")
        self.reindex_exec()
        self.clear_session()

    def empty_plan_template(self) -> str:
        return "# Plan\n\nGoal:\n\n## Todos\n"

    def clear_plan(self) -> None:
        """Reset plan.md to an empty numbered-todo template."""
        self.write_plan(self.empty_plan_template())

    def archive_plan(self) -> Path:
        """Copy current plan to workspace/archives/ and reset plan.md.

        Returns the archive file path (relative-friendly Path object).
        """
        from datetime import datetime

        archives = self.root / "archives"
        archives.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        goal = self.plan_goal() or "plan"
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in goal.lower())[:40].strip("-")
        name = f"{stamp}-{safe or 'plan'}.md"
        dest = archives / name
        dest.write_text(self.read_plan(), encoding="utf-8")
        self.clear_plan()
        return dest

    @staticmethod
    def sanitize_archive_name(name: str) -> str:
        """Safe single-segment folder name for /archive <name>."""
        raw = (name or "").strip().strip("/").strip()
        if not raw or raw in {".", ".."} or "/" in raw or "\\" in raw:
            raise ValueError("archive name must be a single folder name (no slashes)")
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in raw)
        safe = safe.strip(".-") or "workspace"
        return safe[:80]

    def archive_workspace(self, name: str) -> Path:
        """Copy all workspace files/dirs into workspace/archive/<name>.

        Skips the archive/ tree itself to avoid recursive copies.
        Does not delete or reset the live workspace.
        """
        import shutil

        safe = self.sanitize_archive_name(name)
        archive_root = self.root / "archive"
        dest = archive_root / safe
        if dest.exists():
            raise FileExistsError(
                f"archive/{safe} already exists — pick another name or remove it"
            )

        archive_root.mkdir(parents=True, exist_ok=True)
        dest.mkdir(parents=True, exist_ok=False)

        skip_names = {"archive"}
        copied = 0
        for item in sorted(self.root.iterdir()):
            if item.name in skip_names:
                continue
            target = dest / item.name
            if item.is_dir():
                shutil.copytree(
                    item,
                    target,
                    symlinks=False,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            elif item.is_file():
                shutil.copy2(item, target)
            else:
                continue
            copied += 1

        # Small manifest for later browsing
        manifest = (
            f"# Workspace archive\n"
            f"name: {safe}\n"
            f"source: {self.root}\n"
            f"entries: {copied}\n"
        )
        (dest / "ARCHIVE.md").write_text(manifest, encoding="utf-8")
        return dest

    def clear_workspace(self) -> Path:
        """Move all workspace content (except archive/) into archive/<timestamp>,
        then start fresh with blank plan.md, ask.md, exec.log, and session.md.
        """
        from datetime import datetime
        import shutil

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_root = self.root / "archive"
        archive_root.mkdir(parents=True, exist_ok=True)
        dest = archive_root / stamp
        n = 0
        while dest.exists():
            n += 1
            dest = archive_root / f"{stamp}-{n}"

        dest.mkdir(parents=True, exist_ok=False)

        moved = 0
        for item in sorted(self.root.iterdir()):
            if item.name == "archive":
                continue
            target = dest / item.name
            shutil.move(str(item), str(target))
            moved += 1

        goal = ""
        try:
            plan_text = (dest / "plan.md").read_text(encoding="utf-8")
            for line in plan_text.splitlines():
                m = _GOAL_RE.match(line.strip())
                if m:
                    goal = m.group(1).strip()
                    break
        except OSError:
            pass

        manifest = (
            f"# Workspace archive (cleared)\n"
            f"name: {dest.name}\n"
            f"moved: {moved}\n"
            f"goal: {goal or '(none)'}\n"
        )
        (dest / "ARCHIVE.md").write_text(manifest, encoding="utf-8")

        # Fresh top-level workspace
        self.plan_path.write_text(self.empty_plan_template(), encoding="utf-8")
        self.ask_path.write_text("# Ask\n\n", encoding="utf-8")
        self.exec_log_path.write_text("# Exec log\n\n", encoding="utf-8")
        self.session_path.write_text("# Session\n\n", encoding="utf-8")
        self.index.index_dir.mkdir(parents=True, exist_ok=True)
        self.reindex_plan()
        self.reindex_ask()
        self.reindex_exec()
        self.clear_last_failure()
        return dest

    def append_exec_log(self, text: str) -> None:
        with self.exec_log_path.open("a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n")
        self.reindex_exec()

    def read_exec_log(self) -> str:
        if not self.exec_log_path.exists():
            return ""
        return self.exec_log_path.read_text(encoding="utf-8")

    @property
    def last_failure_path(self) -> Path:
        return self.root / ".index" / "last_failure.json"

    @property
    def processes_path(self) -> Path:
        return self.root / ".index" / "processes.json"

    def process_registry(self):
        from tinylocalcoder.exec.processes import ProcessRegistry

        return ProcessRegistry(self.processes_path)

    def write_last_failure(self, payload: dict) -> None:
        self.last_failure_path.parent.mkdir(parents=True, exist_ok=True)
        self.last_failure_path.write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

    def read_last_failure(self) -> dict | None:
        path = self.last_failure_path
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, OSError):
            return None

    def clear_last_failure(self) -> None:
        path = self.last_failure_path
        if path.exists():
            path.unlink()

    def resolve_path(self, rel_path: str) -> Path:
        # "/src/api.py" from the LLM must become workspace-relative "src/api.py"
        clean = rel_path.strip().lstrip("/")
        clean = clean.lstrip("./")
        path = (self.root / clean).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError(f"Path escapes workspace: {rel_path}")
        return path

    @staticmethod
    def normalize_workspace_path(path: str) -> str:
        """Strip leading slashes so paths stay under the workspace."""
        return path.strip().lstrip("/").lstrip("./")

    @staticmethod
    def normalize_run_command(command: str) -> str:
        """Rewrite absolute-looking workspace paths inside shell commands."""
        import re

        # /src/foo.py → src/foo.py  (common LLM mistake)
        cmd = re.sub(r"(?<![\w])/(src|prototypes|tests)/", r"\1/", command)
        cmd = re.sub(r"(?<![\w])/([\w.-]+\.py)\b", r"\1", cmd)
        return cmd

    def write_prototype(self, rel_path: str, content: str) -> Path:
        path = self.resolve_path(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.reindex_prototypes()
        return path

    def read_prototype(self, rel_path: str) -> str:
        path = self.resolve_path(rel_path)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def prototype_exists(self, rel_path: str) -> bool:
        return self.resolve_path(rel_path).exists()

    def list_files(self) -> list[str]:
        files: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and ".index" not in path.parts:
                files.append(str(path.relative_to(self.root)))
        return files

    def read_workspace_file(self, rel_path: str) -> str:
        return self.resolve_path(rel_path).read_text(encoding="utf-8")

    def plan_goal(self) -> str:
        for line in self.read_plan().splitlines():
            m = _GOAL_RE.match(line.strip())
            if m:
                return m.group(1).strip()
        return ""

    @staticmethod
    def _action_from_match(action_raw: str, target: str) -> str:
        action_raw = (action_raw or "").lower()
        if action_raw in {"run", "test"}:
            return "run"
        if action_raw in {"refine", "update", "fix"}:
            return "refine"
        if action_raw in {"create", "write"}:
            return "create"
        # Infer when the model omits the verb
        if target.startswith(("python", "pytest", "curl", "bash", "sh ", "./")):
            return "run"
        if " " in target or target.startswith(("http://", "https://")):
            return "run"
        if "/" in target or target.endswith(
            (".py", ".md", ".txt", ".json", ".toml", ".sh")
        ):
            return "create"
        return "run"

    def _todos_from_lines(
        self, lines: list[str], *, require_section: bool
    ) -> list[TodoStep]:
        todos: list[TodoStep] = []
        in_todos = not require_section
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("## "):
                in_todos = "todo" in stripped.lower()
                continue
            if not in_todos:
                continue
            m = _TODO_RE.match(stripped)
            if not m:
                continue
            number = int(m.group(1))
            mark = m.group(2).lower()
            done = mark in {"x", "!"}
            skipped = mark == "!"
            action_raw = (m.group(3) or "").lower()
            target = (m.group(4) or m.group(5) or "").strip()
            desc = (m.group(6) or "").strip()
            action = self._action_from_match(action_raw, target)
            if action in {"create", "write", "refine"}:
                target = WorkspaceMemory.normalize_workspace_path(target)
            else:
                target = WorkspaceMemory.normalize_run_command(target)
            todos.append(
                TodoStep(
                    number=number,
                    action=action,
                    target=target,
                    description=desc,
                    done=done,
                    raw_line=stripped,
                    skipped=skipped,
                )
            )
        return todos

    def parse_todos(self) -> list[TodoStep]:
        """Parse numbered todos; tolerate missing ## Todos heading from the LLM."""
        text = self.read_plan()
        lines = text.splitlines()
        todos = self._todos_from_lines(lines, require_section=True)
        if not todos:
            # Models often omit "## Todos" and list 1. [ ] … under # Plan
            todos = self._todos_from_lines(lines, require_section=False)
        if todos:
            return todos
        return self._legacy_items_as_todos()

    def normalize_plan_markdown(self, text: str) -> str:
        """Ensure Goal + ## Todos so parsers and the TUI stay in sync."""
        text = text.strip()
        if not text:
            return "# Plan\nGoal: (none)\n\n## Todos\n"
        lines = text.splitlines()
        has_todos_heading = any(
            ln.strip().lower().startswith("## ") and "todo" in ln.strip().lower()
            for ln in lines
        )
        if has_todos_heading:
            # Drop meta-command todo lines in place; keep numbering as-is for now
            filtered: list[str] = []
            for ln in lines:
                stripped = ln.strip()
                m = _TODO_RE.match(stripped)
                if m:
                    target = (m.group(4) or m.group(5) or "").strip()
                    if is_meta_todo_target(target):
                        continue
                filtered.append(ln)
            out = "\n".join(filtered)
            return out + ("\n" if not out.endswith("\n") else "")

        goal = ""
        body: list[str] = []
        numbered: list[str] = []
        for ln in lines:
            stripped = ln.strip()
            if _GOAL_RE.match(stripped):
                goal = _GOAL_RE.match(stripped).group(1).strip()  # type: ignore[union-attr]
                continue
            if stripped.lower() in {"# plan", "#plan"}:
                continue
            if _TODO_RE.match(stripped):
                target = (
                    _TODO_RE.match(stripped).group(4)  # type: ignore[union-attr]
                    or _TODO_RE.match(stripped).group(5)  # type: ignore[union-attr]
                    or ""
                ).strip()
                if is_meta_todo_target(target):
                    continue
                numbered.append(stripped)
                continue
            if stripped:
                body.append(stripped)

        if not goal and body:
            goal = body[0][:200]
            body = body[1:]
        if not goal:
            goal = "(see todos)"

        out = ["# Plan", f"Goal: {goal}", "", "## Todos"]
        if numbered:
            out.extend(numbered)
        elif body:
            out.extend(body)
        return "\n".join(out) + "\n"

    @staticmethod
    def plan_goal_from_text(text: str) -> str:
        for line in (text or "").splitlines():
            m = _GOAL_RE.match(line.strip())
            if m:
                return m.group(1).strip()
        return ""

    @staticmethod
    def plan_done_from_text(text: str) -> str:
        for line in (text or "").splitlines():
            m = _DONE_RE.match(line.strip())
            if m:
                return m.group(1).strip()
        return ""

    @staticmethod
    def plan_extra_from_text(text: str) -> str:
        """Preserve one middle block between Goal/Done and ## Todos (e.g. ASCII art).

        Keeps everything after the header lines (# Plan, Goal:, Done:) and before
        the Todos heading. Empty if there is no such content.
        """
        lines = (text or "").splitlines()
        todos_idx: int | None = None
        for i, ln in enumerate(lines):
            stripped = ln.strip().lower()
            if stripped.startswith("## ") and "todo" in stripped:
                todos_idx = i
                break
        if todos_idx is None:
            return ""

        start = 0
        i = 0
        while i < todos_idx:
            stripped = lines[i].strip()
            if (
                not stripped
                or stripped.lower() in {"# plan", "#plan"}
                or _GOAL_RE.match(stripped)
                or _DONE_RE.match(stripped)
            ):
                i += 1
                start = i
                continue
            break

        extra = lines[start:todos_idx]
        while extra and not extra[0].strip():
            extra = extra[1:]
        while extra and not extra[-1].strip():
            extra = extra[:-1]
        return "\n".join(extra).strip()

    @staticmethod
    def _module_from_py_path(path: str) -> str:
        p = strip_placeholder_path(path)
        if p.endswith(".py"):
            p = p[:-3]
        return p.replace("/", ".")

    def build_done_summary(self, todos: list[TodoStep] | None = None) -> str:
        """One-line narrative of completed work (for replan; not executed)."""
        todos = list(todos if todos is not None else self.parse_todos())
        parts: list[str] = []
        seen: set[tuple[str, str]] = set()
        for t in todos:
            if not (t.done and not t.skipped):
                continue
            action = (
                "run"
                if t.action in {"run", "test"}
                else ("refine" if t.action in {"refine", "update", "fix"} else "create")
            )
            target = (
                strip_placeholders_in_command(t.target)
                if action == "run"
                else strip_placeholder_path(t.target)
            )
            key = (action, target)
            if key in seen:
                continue
            seen.add(key)
            if action == "run" and "py_compile" in target:
                parts.append("py_compile")
            elif action == "run":
                parts.append("verify")
            else:
                parts.append(f"`{target}` ({action})")
            if len(parts) >= 8:
                break
        return ", ".join(parts)

    def find_module_py(self, module_name: str) -> str | None:
        """Locate module_name.py under the workspace (skip archive/.index)."""
        name = (module_name or "").split(".", 1)[0].strip()
        if not name or not name.isidentifier():
            return None
        direct = f"{name}.py"
        if self.prototype_exists(direct):
            return direct
        hits: list[str] = []
        for rel in self.list_files():
            rel_n = rel.replace("\\", "/")
            if rel_n.startswith("archive/") or "/.index/" in f"/{rel_n}/":
                continue
            if rel_n == direct or rel_n.endswith(f"/{name}.py"):
                hits.append(rel_n)
        if not hits:
            return None
        # Prefer shorter / shallower paths
        hits.sort(key=lambda p: (p.count("/"), len(p)))
        return hits[0]

    def validate_plan(self, todos: list[TodoStep] | None = None) -> list[str]:
        """Small checklist for tiny-model plans. Returns human-readable issues."""
        todos = list(todos if todos is not None else self.parse_todos())
        issues: list[str] = []
        creates = [t for t in todos if t.action in {"create", "write", "refine"}]
        runs = [t for t in todos if t.action in {"run", "test"}]
        for t in todos:
            if is_meta_todo_target(t.target):
                issues.append(f"todo {t.number}: meta-command `{t.target}`")
        if creates and not runs:
            issues.append("no run todos (need at least one short verify)")
        py_creates = [
            t
            for t in creates
            if t.target.endswith(".py") and not t.target.endswith("__init__.py")
        ]
        has_compile = any("py_compile" in (t.target or "") for t in runs)
        if py_creates and not has_compile:
            issues.append("missing py_compile for created .py files")
        for t in runs:
            blob = f"{t.target} {t.description}".lower()
            if "expect" not in blob:
                issues.append(f"todo {t.number}: run missing expect clause")
        return issues

    def _format_todo_line(self, number: int, todo: TodoStep) -> str:
        mark = "!" if todo.skipped else ("x" if todo.done else " ")
        if todo.action in {"run", "test"}:
            action = "run"
        elif todo.action in {"refine", "update", "fix"}:
            action = "refine"
        else:
            action = "create"
        desc = f" — {todo.description}" if todo.description else ""
        return f"{number}. [{mark}] {action} `{todo.target}`{desc}"

    def _norm_todo_key(self, todo: TodoStep) -> tuple[str, str]:
        if todo.action in {"run", "test"}:
            return ("run", strip_placeholders_in_command(todo.target).strip())
        if todo.action in {"refine", "update", "fix"}:
            return ("refine", strip_placeholder_path(todo.target))
        return ("create", strip_placeholder_path(todo.target))

    def dedupe_todos(self, todos: list[TodoStep]) -> list[TodoStep]:
        """Keep one done and one open entry per (action, target). Strip path/to/."""
        seen_done: set[tuple[str, str]] = set()
        seen_open: set[tuple[str, str]] = set()
        out: list[TodoStep] = []
        for t in todos:
            action, target = self._norm_todo_key(t)
            key = (action, target)
            if not target:
                continue
            is_done = t.done and not t.skipped
            if is_done:
                if key in seen_done:
                    continue
                seen_done.add(key)
            else:
                if key in seen_open:
                    continue
                seen_open.add(key)
            out.append(
                TodoStep(
                    number=t.number,
                    action=action,
                    target=target,
                    description=t.description,
                    done=t.done,
                    raw_line=t.raw_line,
                    skipped=t.skipped,
                )
            )
        return out

    def cap_todos(
        self, todos: list[TodoStep], *, max_total: int = PLAN_TODO_CAP
    ) -> list[TodoStep]:
        """Hard-cap todo count; prefer keeping open items."""
        if len(todos) <= max_total:
            return todos
        open_t = [t for t in todos if not t.done or t.skipped][:REPLAN_OPEN_CAP]
        done_t = [t for t in todos if t.done and not t.skipped]
        room = max(0, max_total - len(open_t))
        return done_t[:room] + open_t

    def _rewrite_plan_from_todos(
        self,
        goal: str,
        todos: list[TodoStep],
        *,
        strip_meta: bool = True,
        done_summary: str = "",
        extra_section: str = "",
    ) -> str:
        kept: list[TodoStep] = []
        for t in todos:
            if strip_meta and is_meta_todo_target(t.target):
                continue
            kept.append(t)
        lines = ["# Plan", f"Goal: {goal or '(see todos)'}"]
        if done_summary.strip():
            lines.append(f"Done: {done_summary.strip()}")
        extra = (extra_section or "").strip()
        if extra:
            lines.append("")
            lines.append(extra)
        lines.extend(["", "## Todos"])
        for i, t in enumerate(kept, start=1):
            lines.append(self._format_todo_line(i, t))
        return "\n".join(lines) + "\n"

    def write_slim_replan_plan(
        self,
        goal: str,
        done_summary: str,
        open_todos: list[TodoStep],
    ) -> str:
        """Write Goal + Done narrative + open todos only; then finalize."""
        opens: list[TodoStep] = []
        for t in open_todos[:REPLAN_OPEN_CAP]:
            opens.append(
                TodoStep(
                    number=len(opens) + 1,
                    action=t.action,
                    target=t.target,
                    description=t.description or "expect success",
                    done=False,
                    raw_line="",
                    skipped=False,
                )
            )
        extra = self.plan_extra_from_text(self.read_plan())
        draft = self._rewrite_plan_from_todos(
            goal,
            opens,
            strip_meta=True,
            done_summary=done_summary,
            extra_section=extra,
        )
        return self.finalize_plan(draft)

    def sanitize_plan_todos(
        self, todos: list[TodoStep], goal: str = ""
    ) -> list[TodoStep]:
        """Drop junk/meta, strip path/to/, rewrite bad FastAPI checks, cap runs."""
        fastapi = looks_fastapi_goal(goal, todos)
        out: list[TodoStep] = []
        compile_runs: list[TodoStep] = []
        behavior_runs: list[TodoStep] = []

        for t in todos:
            if is_meta_todo_target(t.target):
                continue
            if t.action in {"run", "test"}:
                cmd = strip_placeholders_in_command(t.target)
                if not is_valid_run_command(cmd):
                    continue
                desc = t.description
                m = _FROM_MOD_IMPORT_MOD.search(cmd)
                if m:
                    mod = m.group(1)
                    py_mods = {
                        Path(strip_placeholder_path(x.target)).stem
                        for x in todos
                        if x.action in {"create", "write", "refine"}
                        and strip_placeholder_path(x.target).endswith(".py")
                    }
                    if fastapi or mod in py_mods:
                        if fastapi:
                            inner = (
                                f"from {mod} import app; "
                                "print([getattr(r,'path',None) for r in app.routes])"
                            )
                            cmd = f'python3 -c "{inner}"'
                            if t.target.strip().startswith("PYTHONPATH="):
                                cmd = "PYTHONPATH=. " + cmd
                            desc = "expect success"
                        else:
                            cmd = _FROM_MOD_IMPORT_MOD.sub(
                                rf"from {mod} import app", cmd, count=1
                            )
                            desc = desc or "expect success"
                step = TodoStep(
                    number=t.number,
                    action="run",
                    target=cmd,
                    description=desc if "expect" in (desc or "").lower() else (
                        f"{desc} — expect success" if desc else "expect success"
                    ),
                    done=t.done,
                    raw_line=t.raw_line,
                    skipped=t.skipped,
                )
                if "py_compile" in cmd:
                    compile_runs.append(step)
                else:
                    behavior_runs.append(step)
                continue
            target = strip_placeholder_path(t.target)
            out.append(
                TodoStep(
                    number=t.number,
                    action=t.action,
                    target=target,
                    description=t.description,
                    done=t.done,
                    raw_line=t.raw_line,
                    skipped=t.skipped,
                )
            )

        def _one_each(runs: list[TodoStep]) -> list[TodoStep]:
            """At most one done + one open per unique target."""
            kept: list[TodoStep] = []
            seen_done: set[str] = set()
            open_kept = False
            for r in runs:
                tgt = r.target
                if r.done and not r.skipped:
                    if tgt in seen_done:
                        continue
                    seen_done.add(tgt)
                    kept.append(r)
                    continue
                if not open_kept:
                    kept.append(r)
                    open_kept = True
            return kept

        merged = out + _one_each(compile_runs) + _one_each(behavior_runs)
        return self.cap_todos(self.dedupe_todos(merged))

    def augment_plan_todos(
        self, todos: list[TodoStep], goal: str = ""
    ) -> list[TodoStep]:
        """Sanitize, then append at most one py_compile and one smoke run if missing."""
        cleaned = self.sanitize_plan_todos(todos, goal=goal)
        creates = [t for t in cleaned if t.action in {"create", "write", "refine"}]
        runs = [t for t in cleaned if t.action in {"run", "test"}]
        py_files = [
            t.target
            for t in creates
            if t.target.endswith(".py") and not t.target.endswith("__init__.py")
        ]
        has_compile = any("py_compile" in (t.target or "") for t in runs)
        has_behavior = any("py_compile" not in (t.target or "") for t in runs)
        fastapi = looks_fastapi_goal(goal, cleaned)

        out = list(cleaned)
        next_num = (max((t.number for t in out), default=0) + 1) if out else 1

        if py_files and not has_compile:
            files = " ".join(dict.fromkeys(py_files))
            out.append(
                TodoStep(
                    number=next_num,
                    action="run",
                    target=f"python3 -m py_compile {files}",
                    description="expect success",
                    done=False,
                    raw_line="",
                )
            )
            next_num += 1

        if py_files and not has_behavior:
            mod_path = py_files[0]
            mod = self._module_from_py_path(mod_path)
            parent = str(Path(mod_path).parent).replace("\\", "/")
            if fastapi:
                inner = (
                    f"from {Path(mod_path).stem} import app; "
                    "print([getattr(r,'path',None) for r in app.routes])"
                )
                cmd = f'python3 -c "{inner}"'
                if parent not in {".", ""}:
                    cmd = f"PYTHONPATH={parent} " + cmd
            elif parent not in {".", ""}:
                cmd = f'PYTHONPATH={parent} python3 -c "import {Path(mod_path).stem}"'
            else:
                cmd = f'python3 -c "import {mod}"'
            out.append(
                TodoStep(
                    number=next_num,
                    action="run",
                    target=cmd,
                    description="expect success",
                    done=False,
                    raw_line="",
                )
            )
        return self.cap_todos(self.dedupe_todos(out))

    def finalize_plan(self, text: str | None = None) -> str:
        """Normalize, strip meta/junk, dedupe/cap, rewrite bad verifies, write plan.md."""
        raw = text if text is not None else self.read_plan()
        normalized = self.normalize_plan_markdown(raw)
        goal = self.plan_goal_from_text(normalized) or "(see todos)"
        done_summary = self.plan_done_from_text(normalized)
        # Prefer middle section from normalized text; fall back to raw (plan-edit)
        extra = self.plan_extra_from_text(normalized) or self.plan_extra_from_text(raw)
        todos = self._todos_from_lines(
            normalized.splitlines(), require_section=True
        ) or self._todos_from_lines(normalized.splitlines(), require_section=False)
        augmented = self.augment_plan_todos(todos, goal=goal)
        final = self._rewrite_plan_from_todos(
            goal,
            augmented,
            strip_meta=True,
            done_summary=done_summary,
            extra_section=extra,
        )
        self.write_plan(final)
        return final

    def skip_clone_run_todos(
        self, failed_command: str, *, reason: str = "same failing import pattern"
    ) -> list[int]:
        """Mark remaining open run todos that share the failed import prefix as [!]."""
        cmd = (failed_command or "").strip()
        prefix = ""
        m = re.search(r"from\s+([\w.]+)\s+import\s+(\w+)", cmd)
        if m:
            prefix = f"from {m.group(1)} import {m.group(2)}"
        if not prefix:
            return []
        skipped: list[int] = []
        for t in self.parse_todos():
            if t.done or t.action not in {"run", "test"}:
                continue
            if prefix in (t.target or ""):
                self.mark_todo_skipped(t.number, reason)
                skipped.append(t.number)
        return skipped

    def _legacy_items_as_todos(self) -> list[TodoStep]:
        items = self._parse_legacy_items()
        out: list[TodoStep] = []
        n = 1
        for item in items:
            action = "run" if item.kind == "run_test" else "create"
            out.append(
                TodoStep(
                    number=n,
                    action=action,
                    target=item.path_or_cmd,
                    description=item.description,
                    done=item.done,
                    raw_line=item.raw_line,
                )
            )
            n += 1
        return out

    def _parse_legacy_items(self) -> list[PlanItem]:
        text = self.read_plan()
        section: str | None = None
        items: list[PlanItem] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                lower = stripped.lower()
                if "deliverable" in lower:
                    section = "deliverable"
                elif "run" in lower and "test" in lower:
                    section = "run_test"
                else:
                    section = None
                continue
            if section is None:
                continue
            m = _CHECKBOX_RE.match(stripped)
            if not m:
                continue
            done = m.group(1).lower() == "x"
            path_or_cmd = (m.group(2) or m.group(3) or "").strip()
            desc = (m.group(4) or "").strip()
            items.append(
                PlanItem(
                    kind=section,
                    path_or_cmd=path_or_cmd,
                    description=desc,
                    done=done,
                    raw_line=stripped,
                )
            )
        return items

    def parse_plan_items(self) -> list[PlanItem]:
        """Compatibility: map todos (or legacy sections) to PlanItem list."""
        todos = self.parse_todos()
        return [
            PlanItem(
                kind=t.kind,
                path_or_cmd=t.target,
                description=t.description,
                done=t.done,
                raw_line=t.raw_line,
            )
            for t in todos
        ]

    def next_todo(self) -> TodoStep | None:
        """Next unchecked todo.

        If a create targets a file that already exists, return it as refine
        instead of silently marking it done (that skipped real updates).
        """
        for t in self.parse_todos():
            if t.done:
                continue
            if t.action in {"create", "write"} and self.prototype_exists(t.target):
                return TodoStep(
                    number=t.number,
                    action="refine",
                    target=t.target,
                    description=t.description or "update existing file",
                    done=False,
                    raw_line=t.raw_line,
                )
            return t
        return None

    def pending_todos(self) -> list[TodoStep]:
        return [t for t in self.parse_todos() if not t.done]

    def pending_deliverables(self) -> list[PlanItem]:
        return [
            PlanItem(
                kind="deliverable",
                path_or_cmd=t.target,
                description=t.description,
                done=False,
                raw_line=t.raw_line,
            )
            for t in self.parse_todos()
            if not t.done
            and t.action in {"create", "write", "refine"}
            and not (t.action != "refine" and self.prototype_exists(t.target))
        ]

    def pending_run_tests(self) -> list[PlanItem]:
        return [
            PlanItem(
                kind="run_test",
                path_or_cmd=t.target,
                description=t.description,
                done=False,
                raw_line=t.raw_line,
            )
            for t in self.parse_todos()
            if not t.done and t.action in {"run", "test"}
        ]

    def current_step_prompt(self, todo: TodoStep) -> str:
        """Minimal context for a small LLM: goal + this step only."""
        goal = self.plan_goal() or "(see step)"
        return (
            f"Goal: {goal}\n"
            f"Work on ONLY this step (ignore all other plan steps):\n"
            f"{todo.number}. [ ] {todo.action} `{todo.target}` — {todo.description}\n"
        )

    def mark_todo_done(self, number: int) -> None:
        lines = self.read_plan().splitlines()
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            m = _TODO_RE.match(stripped)
            if m and int(m.group(1)) == number and m.group(2).lower() not in {"x", "!"}:
                indent = line[: len(line) - len(line.lstrip())]
                out.append(indent + stripped.replace("[ ]", "[x]", 1))
                continue
            out.append(line)
        self.write_plan("\n".join(out) + ("\n" if out else ""))

    def mark_todo_skipped(self, number: int, reason: str = "") -> None:
        """Mark a todo as failed/skipped ([!]) so execution can move on.

        Only flips the checkbox to [!]. Skip reason goes to exec.log — never
        append onto the expect clause (that broke stdout checks on retry).
        """
        lines = self.read_plan().splitlines()
        out: list[str] = []
        note = (reason or "auto-skipped after failure").strip()
        for line in lines:
            stripped = line.strip()
            m = _TODO_RE.match(stripped)
            if m and int(m.group(1)) == number and m.group(2).lower() != "!":
                indent = line[: len(line) - len(line.lstrip())]
                new = stripped
                # Drop any prior SKIPPED tail so expect text stays clean
                new = re.sub(
                    r"\s+[—-]\s+SKIPPED\b.*$", "", new, flags=re.IGNORECASE
                )
                if "[ ]" in new:
                    new = new.replace("[ ]", "[!]", 1)
                elif "[x]" in new.lower():
                    new = re.sub(r"\[[xX]\]", "[!]", new, count=1)
                out.append(indent + new)
                continue
            out.append(line)
        self.write_plan("\n".join(out) + ("\n" if out else ""))
        self.append_exec_log(f"\n## SKIP todo {number}\n{note}\n")

    def clear_todo_skip(self, number: int) -> None:
        """Re-open a skipped todo ([!] → [ ]) for another attempt."""
        lines = self.read_plan().splitlines()
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            m = _TODO_RE.match(stripped)
            if m and int(m.group(1)) == number and m.group(2) == "!":
                indent = line[: len(line) - len(line.lstrip())]
                new = stripped.replace("[!]", "[ ]", 1)
                new = re.sub(
                    r"\s+[—-]\s+SKIPPED\b.*$", "", new, flags=re.IGNORECASE
                )
                out.append(indent + new)
                continue
            out.append(line)
        self.write_plan("\n".join(out) + ("\n" if out else ""))

    def clear_all_todo_skips(self) -> list[int]:
        """Re-open every skipped todo ([!] → [ ]). Returns cleared numbers."""
        lines = self.read_plan().splitlines()
        out: list[str] = []
        cleared: list[int] = []
        for line in lines:
            stripped = line.strip()
            m = _TODO_RE.match(stripped)
            if m and m.group(2) == "!":
                number = int(m.group(1))
                indent = line[: len(line) - len(line.lstrip())]
                new = stripped.replace("[!]", "[ ]", 1)
                new = re.sub(
                    r"\s+[—-]\s+SKIPPED\b.*$", "", new, flags=re.IGNORECASE
                )
                out.append(indent + new)
                cleared.append(number)
                continue
            out.append(line)
        if cleared:
            self.write_plan("\n".join(out) + ("\n" if out else ""))
        return cleared

    def mark_item_done(self, path_or_cmd: str) -> None:
        """Mark first matching unchecked todo/legacy item with this target."""
        for t in self.parse_todos():
            if not t.done and t.target == path_or_cmd:
                # Prefer numbered mark when present in file
                if _TODO_RE.match(t.raw_line):
                    self.mark_todo_done(t.number)
                    return
                break
        lines = self.read_plan().splitlines()
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            m = _CHECKBOX_RE.match(stripped)
            if m:
                target = (m.group(2) or m.group(3) or "").strip()
                if target == path_or_cmd and m.group(1).lower() != "x":
                    indent = line[: len(line) - len(line.lstrip())]
                    out.append(indent + stripped.replace("[ ]", "[x]", 1))
                    continue
            m2 = _TODO_RE.match(stripped)
            if m2:
                target = (m2.group(4) or m2.group(5) or "").strip()
                if target == path_or_cmd and m2.group(2).lower() != "x":
                    indent = line[: len(line) - len(line.lstrip())]
                    out.append(indent + stripped.replace("[ ]", "[x]", 1))
                    continue
            out.append(line)
        self.write_plan("\n".join(out) + ("\n" if out else ""))

    def context_slices(self, index_name: str, query: str) -> str:
        if index_name == "prototypes":
            self.reindex_prototypes()
        chunks = self.index.as_chunks(index_name)
        selected = retrieve_chunks(chunks, query, self.settings.retrieve_top_k)
        if not selected:
            return "(no indexed context)"
        return "\n\n".join(f"[{c.chunk_id}]\n{c.text.strip()}" for c in selected)

    def progress_summary(self) -> dict[str, int]:
        todos = self.parse_todos()
        creates = [t for t in todos if t.action in {"create", "write", "refine"}]
        runs = [t for t in todos if t.action in {"run", "test"}]
        files_done = sum(
            1
            for t in creates
            if t.done or (t.action != "refine" and self.prototype_exists(t.target))
        )
        return {
            "files_done": files_done,
            "files_total": len(creates),
            "cmds_done": sum(1 for t in runs if t.done),
            "cmds_total": len(runs),
            "todos_done": sum(1 for t in todos if t.done and not t.skipped),
            "todos_skipped": sum(1 for t in todos if t.skipped),
            "todos_total": len(todos),
        }
