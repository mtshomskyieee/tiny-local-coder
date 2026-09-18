"""Deterministic code review from ``manifest.txt`` → ``review.md`` (no LLM)."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from tinylocalcoder.tools.manifest import (
    MANIFEST_NAME,
    REVIEW_ARTIFACTS,
    write_manifest,
)

if TYPE_CHECKING:
    from tinylocalcoder.memory.files import TodoStep, WorkspaceMemory

REVIEW_NAME = "review.md"
Severity = Literal["high", "medium", "low", "info"]

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

_TODO_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
_PRINT_RE = re.compile(r"^\s*print\s*\(", re.MULTILINE)
_BARE_EXCEPT_RE = re.compile(r"except\s*:")
_STAR_IMPORT_RE = re.compile(r"from\s+\S+\s+import\s+\*")
_ON_EVENT_RE = re.compile(r"@\w*\.on_event\s*\(\s*[\"'](?:startup|shutdown)[\"']\s*\)")
_GLOBAL_RE = re.compile(r"^\s*global\s+\w+", re.MULTILINE)
_HARDCODED_JSON_RE = re.compile(r"""open\s*\(\s*["'][^"']+\.json["']""")
_ASSERT_TRUE_RE = re.compile(r"assertTrue\s*\(\s*True\s*\)|assert\s+True\b")


@dataclass
class Finding:
    severity: Severity
    message: str
    line: int | None = None

    def format(self) -> str:
        loc = f" (L{self.line})" if self.line else ""
        return f"- **{self.severity}**{loc}: {self.message}"


@dataclass
class FileReview:
    path: str
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""

    @property
    def worst(self) -> Severity | None:
        if not self.findings:
            return None
        return min(self.findings, key=lambda f: _SEVERITY_ORDER[f.severity]).severity


@dataclass
class ReviewIssue:
    """One actionable line from ``review.md`` (or a live heuristic scan)."""

    path: str
    severity: Severity
    message: str
    line: int | None = None

    def format_plan_line(self) -> str:
        loc = f" L{self.line}" if self.line else ""
        return f"- `{self.path}`{loc} [{self.severity}]: {self.message}"


_FILE_HEAD_RE = re.compile(r"^###\s+`([^`]+)`\s*$")
_FINDING_LINE_RE = re.compile(
    r"^-\s+\*\*(high|medium|low|info)\*\*(?:\s+\(L(\d+)\))?:\s+(.+)$",
    re.IGNORECASE,
)


def parse_review_issues(text: str) -> list[ReviewIssue]:
    """Parse findings from a tool-written (or similarly shaped) review.md."""
    issues: list[ReviewIssue] = []
    path = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        head = _FILE_HEAD_RE.match(line)
        if head:
            path = head.group(1).strip()
            continue
        hit = _FINDING_LINE_RE.match(line)
        if not hit or not path:
            continue
        sev = hit.group(1).lower()
        if sev not in _SEVERITY_ORDER:
            continue
        lineno = int(hit.group(2)) if hit.group(2) else None
        issues.append(
            ReviewIssue(path=path, severity=sev, message=hit.group(3).strip(), line=lineno)
        )
    return issues


def actionable_issues(issues: list[ReviewIssue]) -> list[ReviewIssue]:
    """Drop info-only notes that are not worth a fix todo."""
    return [i for i in issues if i.severity in {"high", "medium", "low"}]


def format_issues_for_plan(issues: list[ReviewIssue]) -> str:
    if not issues:
        return "(no actionable findings)"
    return "\n".join(i.format_plan_line() for i in issues)


def issues_from_review(memory: WorkspaceMemory) -> list[ReviewIssue]:
    """Load issues from review.md; live-scan only when that file is empty."""
    text = ""
    if memory.prototype_exists(REVIEW_NAME):
        text = memory.read_prototype(REVIEW_NAME)
    parsed = parse_review_issues(text)
    if parsed:
        return actionable_issues(parsed)
    if text.strip():
        return []
    live: list[ReviewIssue] = []
    for rev in build_reviews(memory):
        for finding in rev.findings:
            if finding.severity == "info":
                continue
            live.append(
                ReviewIssue(
                    path=rev.path,
                    severity=finding.severity,
                    message=finding.message,
                    line=finding.line,
                )
            )
    return live


def is_review_todo(todo: TodoStep) -> bool:
    """True when a create/refine todo targets ``review.md``."""
    if todo.action not in {"create", "write", "refine"}:
        return False
    target = (todo.target or "").replace("\\", "/").strip().lstrip("./")
    if target == REVIEW_NAME or target.endswith(f"/{REVIEW_NAME}"):
        return True
    blob = f"{todo.raw_line} {todo.description}".lower()
    return "review.md" in blob and todo.action in {"create", "write", "refine"}


def read_manifest_paths(memory: WorkspaceMemory) -> list[str]:
    """Load paths from manifest.txt; rebuild if missing/empty."""
    raw = ""
    if memory.prototype_exists(MANIFEST_NAME):
        raw = memory.read_prototype(MANIFEST_NAME)
    paths = [
        ln.strip().replace("\\", "/")
        for ln in (raw or "").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    paths = [p for p in paths if p not in REVIEW_ARTIFACTS]
    if paths:
        return paths
    _, rebuilt = write_manifest(memory)
    return [p for p in rebuilt if p not in REVIEW_ARTIFACTS]


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _review_python(path: str, text: str, *, has_tests: bool) -> FileReview:
    rev = FileReview(path=path)
    lines = text.splitlines()
    n = len(lines)

    # Package markers are checked first: an empty __init__.py is correct, and
    # the fix agent creates exactly that file to repair imports.
    if path.endswith("__init__.py") and len(text.strip()) < 40:
        rev.findings.append(
            Finding("info", "Package marker only — fine if intentional.")
        )
        rev.summary = "Package init."
        return rev

    if not text.strip():
        rev.findings.append(Finding("medium", "File is empty."))
        rev.summary = "Empty module."
        return rev

    def add(sev: Severity, msg: str, line: int | None = None) -> None:
        # One note per message — avoid spamming every `global` line.
        if any(f.message == msg for f in rev.findings):
            return
        rev.findings.append(Finding(sev, msg, line))

    # Regex / line heuristics (fast, opinionated)
    for m in _TODO_RE.finditer(text):
        add("low", f"Leftover marker `{m.group(1)}`.", _line_of(text, m.start()))
    for m in _PRINT_RE.finditer(text):
        add("low", "Debug `print` left in source.", _line_of(text, m.start()))
    for m in _BARE_EXCEPT_RE.finditer(text):
        add(
            "high",
            "Bare `except:` swallows all errors — catch a specific type.",
            _line_of(text, m.start()),
        )
    for m in _STAR_IMPORT_RE.finditer(text):
        add("medium", "Star import hurts clarity and tooling.", _line_of(text, m.start()))
    if _ON_EVENT_RE.search(text):
        m = _ON_EVENT_RE.search(text)
        add(
            "medium",
            "Deprecated FastAPI `@app.on_event` — prefer lifespan handlers.",
            _line_of(text, m.start()) if m else None,
        )
    if _GLOBAL_RE.search(text):
        m = _GLOBAL_RE.search(text)
        add(
            "medium",
            "Mutable module `global` state — prefer app.state / dependency injection.",
            _line_of(text, m.start()) if m else None,
        )
    if _HARDCODED_JSON_RE.search(text) and "Path(" not in text:
        add(
            "medium",
            "Hardcoded JSON path string — prefer pathlib + configurable location.",
        )

    # Trivial / missing tests
    is_test = (
        Path(path).name.startswith("test_")
        or "/tests/" in f"/{path.replace(chr(92), '/')}/"
        or path.startswith("test")
    )
    if is_test and not path.endswith("__init__.py") and _ASSERT_TRUE_RE.search(text):
        add("high", "Smoke assertion is a no-op (`assert True`) — test real behavior.")
    if (
        path.endswith(".py")
        and not is_test
        and not path.endswith("__init__.py")
        and not has_tests
        and ("src/" in path.replace("\\", "/") or path.count("/") == 0)
    ):
        add("medium", "No obvious unit test covering this module.")

    # AST-backed checks
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        add("high", f"Syntax error: {exc.msg}", getattr(exc, "lineno", None))
        rev.summary = "Does not parse."
        return rev

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in list(node.args.defaults) + list(node.args.kw_defaults):
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    add(
                        "high",
                        f"`{node.name}` uses a mutable default argument.",
                        getattr(node, "lineno", None),
                    )
            body_lines = 0
            if hasattr(node, "end_lineno") and node.end_lineno and node.lineno:
                body_lines = int(node.end_lineno) - int(node.lineno)
            if body_lines > 60:
                add(
                    "low",
                    f"`{node.name}` is long (~{body_lines} lines) — consider splitting.",
                    getattr(node, "lineno", None),
                )
            src_seg = ast.get_source_segment(text, node) or ""
            if re.search(r"max\s*\(\s*\w+\.keys\s*\(", src_seg):
                add(
                    "high",
                    f"`{node.name}` uses `max(...keys())` for ids — after "
                    "`json.load`, object keys are strings (and empty `.keys()` "
                    "is truthy, so `keys() or [0]` never falls back).",
                    getattr(node, "lineno", None),
                )

    if "json.dump" in text and "json.load" in text and "Lock" not in text and not is_test:
        add(
            "low",
            "JSON read/write without locking — concurrent requests can corrupt data.",
        )

    if n > 250:
        add("low", f"Large file ({n} lines) — harder to review and test.")

    if not rev.findings:
        add("info", "No heuristic issues flagged — still skim for API/contract fit.")
        rev.summary = "Clean under heuristic checks."
    else:
        counts: dict[str, int] = {}
        for f in rev.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        bits = [f"{counts[s]} {s}" for s in ("high", "medium", "low", "info") if s in counts]
        rev.summary = ", ".join(bits)
    return rev


def _review_markdown(path: str, text: str) -> FileReview:
    rev = FileReview(path=path)
    if not text.strip():
        rev.findings.append(Finding("medium", "Markdown file is empty."))
    elif len(text.strip()) < 40:
        rev.findings.append(Finding("low", "Very short doc — may need more context."))
    if text.strip() and not re.search(r"^#\s+", text, re.MULTILINE):
        rev.findings.append(Finding("low", "No top-level `#` heading."))
    if not rev.findings:
        rev.findings.append(Finding("info", "Doc looks structurally fine."))
        rev.summary = "OK"
    else:
        rev.summary = f"{len(rev.findings)} note(s)"
    return rev


def _review_json(path: str, text: str) -> FileReview:
    rev = FileReview(path=path)
    import json

    try:
        data = json.loads(text or "null")
    except json.JSONDecodeError as exc:
        rev.findings.append(Finding("high", f"Invalid JSON: {exc}"))
        rev.summary = "Invalid JSON"
        return rev
    if isinstance(data, dict) and data and all(isinstance(k, str) and k.isdigit() for k in data):
        rev.findings.append(
            Finding(
                "medium",
                "Object keys look like numeric ids stored as strings — keep API types consistent.",
            )
        )
    if not rev.findings:
        rev.findings.append(Finding("info", "JSON parses."))
        rev.summary = "OK"
    else:
        rev.summary = f"{len(rev.findings)} note(s)"
    return rev


def _review_generic(path: str, text: str) -> FileReview:
    rev = FileReview(path=path)
    if not text.strip():
        rev.findings.append(Finding("medium", "File is empty."))
        rev.summary = "Empty"
    else:
        rev.findings.append(
            Finding("info", f"{len(text.splitlines())} lines — no language-specific rules applied.")
        )
        rev.summary = "Listed for awareness"
    return rev


def _test_paths(all_paths: list[str]) -> set[str]:
    return {
        p.replace("\\", "/")
        for p in all_paths
        if Path(p).name.startswith("test_")
        or "/tests/" in f"/{p.replace(chr(92), '/')}/"
        or p.startswith("tests/")
    }


def _module_covered(path: str, test_paths: set[str]) -> bool:
    stem = Path(path).stem
    if stem in {"__init__", "conftest"}:
        return True
    needle = stem.lower()
    for t in test_paths:
        name = Path(t).name.lower()
        body = t.lower()
        if needle in name or needle in body:
            return True
    return False


def review_file(path: str, text: str, *, all_paths: list[str]) -> FileReview:
    """Opinionated review for one workspace-relative file."""
    tests = _test_paths(all_paths)
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return _review_python(path, text, has_tests=_module_covered(path, tests))
    if suffix in {".md", ".rst"}:
        return _review_markdown(path, text)
    if suffix == ".json":
        return _review_json(path, text)
    return _review_generic(path, text)


def render_review_md(reviews: list[FileReview], *, manifest_count: int) -> str:
    """Render a stable, skimmable review.md."""
    highs = sum(1 for r in reviews for f in r.findings if f.severity == "high")
    meds = sum(1 for r in reviews for f in r.findings if f.severity == "medium")
    lows = sum(1 for r in reviews for f in r.findings if f.severity == "low")

    lines: list[str] = [
        "# Code review",
        "",
        f"Generated by the review tool from `{MANIFEST_NAME}` "
        f"({manifest_count} file{'s' if manifest_count != 1 else ''}).",
        "",
        "## Summary",
        "",
        f"- Findings: **{highs} high**, **{meds} medium**, **{lows} low** "
        f"across {len(reviews)} files.",
    ]
    hot = [r for r in reviews if r.worst in {"high", "medium"}]
    if hot:
        lines.append("- Priority files:")
        for r in sorted(hot, key=lambda x: _SEVERITY_ORDER.get(x.worst or "info", 9)):
            lines.append(f"  - `{r.path}` — {r.summary or r.worst}")
    else:
        lines.append("- No high/medium issues flagged by heuristics.")
    lines.extend(["", "## Files", ""])

    for r in reviews:
        lines.append(f"### `{r.path}`")
        lines.append("")
        if r.summary:
            lines.append(f"_{r.summary}_")
            lines.append("")
        if r.findings:
            for f in sorted(r.findings, key=lambda x: _SEVERITY_ORDER[x.severity]):
                lines.append(f.format())
        else:
            lines.append("- **info**: No heuristic issues flagged.")
        lines.append("")

    lines.append("---")
    lines.append("_Heuristic / opinionated pass — not a substitute for human review._")
    lines.append("")
    return "\n".join(lines)


def build_reviews(memory: WorkspaceMemory, paths: list[str] | None = None) -> list[FileReview]:
    paths = list(paths if paths is not None else read_manifest_paths(memory))
    reviews: list[FileReview] = []
    for rel in paths:
        if rel in REVIEW_ARTIFACTS:
            continue
        if not memory.prototype_exists(rel):
            reviews.append(
                FileReview(
                    path=rel,
                    findings=[Finding("high", "Listed in manifest but file is missing.")],
                    summary="Missing file",
                )
            )
            continue
        text = memory.read_prototype(rel)
        # Cap huge files for heuristic scan
        if len(text) > 80_000:
            text = text[:80_000]
        reviews.append(review_file(rel, text, all_paths=paths))
    return reviews


def write_review(
    memory: WorkspaceMemory,
    *,
    focus_hint: str = "",
    out_name: str = REVIEW_NAME,
) -> tuple[str, list[FileReview]]:
    """Ensure manifest, review each path, write ``review.md``."""
    # Rebuild inventory when empty so review never targets the wrong artifacts
    paths = read_manifest_paths(memory)
    if focus_hint.strip():
        from tinylocalcoder.tools.manifest import parse_focus_prefixes

        prefixes = parse_focus_prefixes(focus_hint)
        if prefixes:
            focused = [
                p
                for p in paths
                if any(p == pref.rstrip("/") or p.startswith(pref) for pref in prefixes)
            ]
            # Fail open: never review zero files because a hint matched nothing
            if focused:
                paths = focused
    reviews = build_reviews(memory, paths)
    body = render_review_md(reviews, manifest_count=len(paths))
    memory.write_prototype(out_name, body)
    return out_name, reviews


def fulfill_review_todo(
    memory: WorkspaceMemory,
    todo: TodoStep,
    *,
    focus_hint: str = "",
) -> dict:
    """Tool path for a create/refine ``review.md`` todo."""
    hint = " ".join(p for p in (focus_hint, todo.description, todo.raw_line) if p)
    path, reviews = write_review(memory, focus_hint=hint)
    memory.mark_todo_done(todo.number)
    # Collapse duplicate refine/create review todos
    for other in list(memory.pending_todos()):
        if other.number != todo.number and is_review_todo(other):
            memory.mark_todo_done(other.number)
    highs = sum(1 for r in reviews for f in r.findings if f.severity == "high")
    return {
        "last_file": path,
        "output": (
            f"Step {todo.number}: wrote `{path}` via tool "
            f"({len(reviews)} files, {highs} high)"
        ),
        "phase": "coding",
        "status": "ok",
        "pending_initial_files": [
            t.target
            for t in memory.parse_todos()
            if not t.done and t.action in {"create", "write", "refine"}
        ],
        "tool": "review",
        "review_files": len(reviews),
        "review_high": highs,
    }


def fulfill_open_review_todos(
    memory: WorkspaceMemory,
    *,
    focus_hint: str = "",
) -> list[dict]:
    """Complete every open create/refine review.md todo with the tool."""
    results: list[dict] = []
    for todo in list(memory.pending_todos()):
        if is_review_todo(todo):
            results.append(fulfill_review_todo(memory, todo, focus_hint=focus_hint))
            break  # fulfill_review_todo marks siblings done
    return results
