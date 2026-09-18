"""Fix agent — diagnose last failure, inspect files/dirs, apply code fixes."""

from __future__ import annotations

import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from tinylocalcoder.llm import invoke_llm, message_text
from tinylocalcoder.memory.files import WorkspaceMemory


FIX_SYSTEM = """You troubleshoot a failed local workspace command.
You see: the error, the failed todo line, file names, and a few file snippets.
Propose concrete file fixes only (no long explanations).

Output EXACTLY this format:

DIAGNOSIS: <one short line>
ACTIONS:
CREATE `relative/path` 
<<<
file contents if creating a new file (may be empty)
>>>
WRITE `relative/path`
<<<
full new file contents
>>>
END

Or if the *command/todo* itself is wrong (wrong API, impossible assert) and no file edit helps:
DIAGNOSIS: <one short line>
NEEDS_REPLAN: <short reason>
ACTIONS:
END

Rules:
- Paths are workspace-relative (e.g. `src/__init__.py`). Never use leading /.
- Prefer CREATE for missing `__init__.py` only when the failing import needs that package.
- Prefer WRITE to fix imports/ports in existing files.
- When a port is already in use, prefer killing the tracked execute PID.
- If ImportError is "cannot import name X" and X is not defined in the module file,
  do NOT invent a fake X object — use NEEDS_REPLAN (the run todo is wrong).
- If AttributeError names a method the plan invented but the file is a FastAPI app,
  use NEEDS_REPLAN (verify app/routes instead of fake methods).
- Keep files short. No markdown outside the ACTION blocks.
- If nothing to change: ACTIONS: then END immediately (or NEEDS_REPLAN).
"""


_TRACE_FILE_RE = re.compile(
    r'File "([^"]+\.(?:py|md|sh|txt|json|toml))"',
    re.IGNORECASE,
)
_MODULE_RE = re.compile(
    r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_ATTR_RE = re.compile(
    r"AttributeError:\s+['\"]?[^'\"]+['\"]?\s+object has no attribute ['\"](\w+)['\"]|"
    r"AttributeError:\s+module ['\"][^'\"]+['\"] has no attribute ['\"](\w+)['\"]|"
    r"AttributeError:\s+.*['\"](\w+)['\"]",
    re.IGNORECASE,
)
_IMPORT_NAME_RE = re.compile(
    r"ImportError:\s+cannot import name ['\"](\w+)['\"]",
    re.IGNORECASE,
)
_PATH_HINT_RE = re.compile(
    r"(?:^|[\s/`])((?:src|tests|prototypes)/[\w./-]+\.(?:py|md|sh|txt))",
    re.IGNORECASE,
)
_ACTION_RE = re.compile(
    r"(CREATE|WRITE)\s+`([^`]+)`\s*<<<\s*([\s\S]*?)>>>",
    re.IGNORECASE,
)
_NEEDS_REPLAN_RE = re.compile(
    r"^NEEDS_REPLAN:\s*(.+)$", re.IGNORECASE | re.MULTILINE
)


def _norm(path: str) -> str:
    return WorkspaceMemory.normalize_workspace_path(path)


def extract_paths_from_error(text: str) -> list[str]:
    found: list[str] = []
    for m in _TRACE_FILE_RE.finditer(text or ""):
        raw = m.group(1)
        # Strip container absolute prefix down to workspace-relative if possible
        if "/workspace/" in raw.replace("\\", "/"):
            raw = raw.replace("\\", "/").split("/workspace/", 1)[1]
        p = _norm(raw)
        if p and p not in found:
            found.append(p)
    for m in _PATH_HINT_RE.finditer(text or ""):
        p = _norm(m.group(1))
        if p and p not in found:
            found.append(p)
    return found


def _list_dir(memory: WorkspaceMemory, rel_dir: str, limit: int = 40) -> str:
    try:
        root = memory.resolve_path(rel_dir) if rel_dir not in {".", ""} else memory.root
    except ValueError:
        return f"{rel_dir}: (invalid path)"
    if not root.exists():
        return f"{rel_dir}: (missing)"
    if not root.is_dir():
        return f"{rel_dir}: (not a directory)"
    names = sorted(p.name for p in root.iterdir())
    more = ""
    if len(names) > limit:
        names = names[:limit]
        more = " …"
    return f"{rel_dir or '.'}/: " + ", ".join(names) + more


def gather_fix_context(memory: WorkspaceMemory, error_text: str) -> dict:
    paths = extract_paths_from_error(error_text)
    err = error_text or ""
    # Only peek at src helpers when the failure actually mentions src/
    if "src/" in err or "src." in err or "from src" in err or "import src" in err:
        for extra in ("src/main.py", "src/api.py", "src/__init__.py"):
            if extra not in paths:
                paths.append(extra)

    files: dict[str, str] = {}
    dirs: dict[str, str] = {}
    for path in paths[:6]:
        try:
            p = memory.resolve_path(path)
        except ValueError:
            continue
        parent = str(Path(path).parent).replace("\\", "/")
        if parent not in dirs:
            dirs[parent] = _list_dir(memory, parent if parent != "." else ".")
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            if len(text) > 2000:
                text = text[:2000] + "\n# …truncated…"
            files[path] = text
        else:
            files[path] = "(file missing)"

    if "." not in dirs:
        dirs["."] = _list_dir(memory, ".")
    if ("src/" in err or "src." in err) and "src" not in dirs:
        dirs["src"] = _list_dir(memory, "src")

    return {"paths": paths, "files": files, "dirs": dirs}


_PORT_IN_USE_RE = re.compile(
    r"bind on address\s*\(\s*['\"]?[^\s,'\"]+['\"]?\s*,\s*(\d{2,5})\s*\)|"
    r"address already in use[^\n]*\b(\d{4,5})\b|"
    r":(\d{4,5})\b[^\n]*(?:address already in use|errno\s*98)|"
    r"(?:errno\s*98)[^\n]*:(\d{4,5})\b",
    re.IGNORECASE,
)


def _ports_from_error(err: str) -> list[int]:
    ports: list[int] = []
    for m in _PORT_IN_USE_RE.finditer(err or ""):
        for g in m.groups():
            if not g:
                continue
            try:
                p = int(g)
            except ValueError:
                continue
            # Ignore IPv4 first octets accidentally captured
            if p < 1024 or p > 65535:
                continue
            if p not in ports:
                ports.append(p)
    if not ports and (
        "address already in use" in (err or "").lower() or "errno 98" in (err or "").lower()
    ):
        from tinylocalcoder.exec.processes import extract_ports

        ports = [p for p in extract_ports("", err) if p >= 1024]
    return ports


def apply_heuristic_fixes(memory: WorkspaceMemory, error_text: str) -> list[str]:
    """Cheap deterministic fixes for common local failures."""
    applied: list[str] = []
    err = error_text or ""

    mod = _MODULE_RE.search(err)
    if mod:
        name = mod.group(1)
        # Missing package marker — only for the package named in the error
        if name == "src" or name.startswith("src."):
            init = "src/__init__.py"
            if not memory.prototype_exists(init):
                memory.write_prototype(init, "")
                applied.append(f"CREATE `{init}` (package marker)")
            # Script launched as python3 src/main.py cannot import `src.*`
            main = "src/main.py"
            if memory.prototype_exists(main):
                content = memory.read_prototype(main)
                if "from src.api import" in content or "import src.api" in content:
                    new = content.replace("from src.api import", "from api import")
                    new = new.replace("import src.api", "import api")
                    if new != content:
                        memory.write_prototype(main, new)
                        applied.append(
                            f"WRITE `{main}` (script-safe import: from api import …)"
                        )
        else:
            # e.g. No module named 'pkg' → CREATE pkg/__init__.py only if pkg/ exists
            top = name.split(".", 1)[0]
            if top and top.isidentifier() and top not in {"builtins", "sys", "os"}:
                pkg_dir = top
                try:
                    p = memory.resolve_path(pkg_dir)
                except ValueError:
                    p = None
                if p is not None and p.is_dir():
                    init = f"{top}/__init__.py"
                    if not memory.prototype_exists(init):
                        memory.write_prototype(init, "")
                        applied.append(f"CREATE `{init}` (package marker)")

    # Port already in use — kill tracked execute PIDs first (do not invent a new port)
    if "address already in use" in err.lower() or "errno 98" in err.lower():
        registry = memory.process_registry()
        ports = _ports_from_error(err)
        killed_any = False
        if ports:
            for port in ports:
                notes = registry.kill_port(port)
                if notes:
                    killed_any = True
                    applied.append(f"KILL processes on port {port}")
                    applied.extend(f"  {n}" for n in notes)
        if not killed_any:
            # Fall back: kill any still-running tracked server-like processes
            running = registry.list_running()
            if running:
                notes = registry.kill_all_running()
                applied.append("KILL all tracked execute processes (port busy)")
                applied.extend(f"  {n}" for n in notes)
                killed_any = True
        if not killed_any:
            # Last resort: change 8000→8888 only when that looks like the conflict source
            for path in ("src/main.py", "src/api.py"):
                if not memory.prototype_exists(path):
                    continue
                content = memory.read_prototype(path)
                if "port=8000" in content or "port = 8000" in content:
                    new = content.replace("port=8000", "port=8888").replace(
                        "port = 8000", "port = 8888"
                    )
                    if new != content:
                        memory.write_prototype(path, new)
                        applied.append(f"WRITE `{path}` (port 8000 → 8888)")

    return applied


def _parse_and_apply_llm_actions(memory: WorkspaceMemory, text: str) -> list[str]:
    applied: list[str] = []
    for m in _ACTION_RE.finditer(text or ""):
        kind = m.group(1).upper()
        path = _norm(m.group(2))
        body = m.group(3)
        # Trim common accidental fences
        body = body.strip()
        if body.startswith("```"):
            lines = body.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            body = "\n".join(lines)
        if not path or ".." in path:
            continue
        if kind == "CREATE" and memory.prototype_exists(path):
            # Still allow overwrite via CREATE when empty init etc.
            pass
        memory.write_prototype(path, body if body.endswith("\n") else body + "\n")
        applied.append(f"{kind} `{path}`")
    return applied


def parse_needs_replan(text: str) -> str | None:
    m = _NEEDS_REPLAN_RE.search(text or "")
    if not m:
        return None
    return (m.group(1) or "").strip() or "plan todo looks wrong"


def _failed_todo_line(memory: WorkspaceMemory, failure: dict | None) -> str:
    if not failure:
        return "(unknown step)"
    step = failure.get("step")
    if step is None:
        return f"command: {failure.get('command', '')}"
    try:
        num = int(step)
    except (TypeError, ValueError):
        return f"command: {failure.get('command', '')}"
    for t in memory.parse_todos():
        if t.number == num:
            return t.raw_line or (
                f"{t.number}. [ ] {t.action} `{t.target}` — {t.description}"
            )
    return f"step {num}: {failure.get('command', '')}"


def _attr_missing_in_sources(
    memory: WorkspaceMemory, attr: str, error_text: str, command: str
) -> bool:
    """True when attr appears in the failing command but not in traceback sources."""
    if not attr or attr not in (command or ""):
        return False
    paths = extract_paths_from_error(error_text)
    if not paths:
        # Import target from command: from b import app
        m = re.search(r"from\s+([\w.]+)\s+import", command or "")
        if m:
            mod = m.group(1).replace(".", "/") + ".py"
            paths = [mod, m.group(1).split(".")[-1] + ".py"]
    for path in paths[:6]:
        try:
            text = memory.read_prototype(path)
        except Exception:  # noqa: BLE001
            continue
        if not text:
            continue
        # def attr / attr = / class attr
        if re.search(rf"\bdef\s+{re.escape(attr)}\b", text):
            return False
        if re.search(rf"\b{re.escape(attr)}\s*=", text):
            return False
        if re.search(rf"\bclass\s+{re.escape(attr)}\b", text):
            return False
        if f"'{attr}'" in text or f'"{attr}"' in text:
            return False
    return True


def classify_plan_smell(
    memory: WorkspaceMemory,
    *,
    failure: dict | None,
    fixes: list[str] | None,
    needs_replan: str | None = None,
    error_blob: str = "",
) -> bool:
    """Deterministic: failure is likely a bad plan todo, not a missing file patch."""
    if needs_replan:
        return True
    failure = failure or {}
    cmd = str(failure.get("command") or "")
    err = error_blob or "\n".join(
        [
            str(failure.get("stderr") or ""),
            str(failure.get("stdout") or ""),
            str(failure.get("error") or ""),
        ]
    )
    fixes = fixes or []

    # Junk / non-python commands are skip-worthy, not "API mismatch" smell for replan
    from tinylocalcoder.memory.files import is_valid_run_command

    if cmd and not is_valid_run_command(cmd):
        return False

    attr_m = _ATTR_RE.search(err)
    if attr_m:
        attr = next((g for g in attr_m.groups() if g), "")
        if attr and _attr_missing_in_sources(memory, attr, err, cmd):
            return True

    imp = _IMPORT_NAME_RE.search(err)
    if imp:
        name = imp.group(1)
        if name and (
            name in cmd or f"import {name}" in cmd
        ) and _attr_missing_in_sources(memory, name, err, cmd):
            return True

    # Fixes only touched unrelated package markers while command is another module
    if fixes:
        unrelated = all(
            ("src/__init__" in f or "src\\__init__" in f) for f in fixes if f.strip()
        )
        if unrelated and ("from b " in cmd or "import b" in cmd or "b.py" in err):
            return True
        # All fixes are CREATE of paths never mentioned in command/error
        mentioned = (cmd + err).lower()
        only_unrelated = True
        for f in fixes:
            m = re.search(r"`([^`]+)`", f)
            if not m:
                continue
            path = m.group(1).lower()
            base = Path(path).name.lower()
            if path in mentioned or base in mentioned or path.split("/")[0] in mentioned:
                only_unrelated = False
                break
        if only_unrelated and fixes:
            return True

    if not fixes and (
        "AttributeError" in err
        or "AssertionError" in err
        or "NameError" in err
        or "ImportError" in err
    ):
        # Missing package ModuleNotFoundError for src is often a real code fix
        if "ModuleNotFoundError" in err and _MODULE_RE.search(err):
            mod = _MODULE_RE.search(err)
            if mod and (mod.group(1) == "src" or mod.group(1).startswith("src.")):
                return False
        return True

    return False


def is_junk_run_failure(failure: dict | None, error_blob: str = "") -> bool:
    """True when the failed command is not a valid verify command (e.g. bare Define)."""
    from tinylocalcoder.memory.files import is_valid_run_command

    failure = failure or {}
    cmd = str(failure.get("command") or "")
    if cmd and not is_valid_run_command(cmd):
        return True
    err = error_blob or str(failure.get("stderr") or "")
    if "not found" in err.lower() and cmd and not is_valid_run_command(cmd):
        return True
    return False


def run_fix_agent(
    memory: WorkspaceMemory,
    prompt: str = "",
    error_text: str | None = None,
) -> dict:
    """Inspect last failure (or provided error) and apply fixes."""
    failure = memory.read_last_failure()
    blob = error_text or ""
    if not blob and failure:
        blob = "\n".join(
            [
                f"command: {failure.get('command', '')}",
                f"exit: {failure.get('exit_code', '')}",
                f"stdout:\n{failure.get('stdout', '')}",
                f"stderr:\n{failure.get('stderr', '')}",
                f"error: {failure.get('error', '')}",
                f"step: {failure.get('step', '')}",
            ]
        )
    if prompt:
        blob = (blob + f"\n\nUser note:\n{prompt}").strip()

    if not blob.strip():
        # Fall back to tail of exec.log
        log = memory.read_exec_log()
        blob = log[-2500:] if log else ""
    if not blob.strip():
        return {
            "output": "No recent failure to fix. Run /execute-plan first, or pass details: /fix <error>",
            "status": "ok",
            "phase": "done",
            "fixes": [],
            "last_file": "",
            "needs_replan": None,
            "plan_smell": False,
        }

    ctx = gather_fix_context(memory, blob)
    procs = memory.process_registry().format_report()
    heuristic = apply_heuristic_fixes(memory, blob)
    failed_todo = _failed_todo_line(memory, failure)
    file_names = ", ".join(memory.list_files()[:40]) or "(none)"

    files_block = "\n\n".join(
        f"### `{path}`\n{content}" for path, content in list(ctx["files"].items())[:4]
    )
    dirs_block = "\n".join(f"- {v}" for v in ctx["dirs"].values())

    messages = [
        SystemMessage(content=FIX_SYSTEM),
        HumanMessage(
            content=(
                f"Failed todo:\n{failed_todo}\n\n"
                f"Failure report:\n{blob[:2500]}\n\n"
                f"Workspace files: {file_names}\n\n"
                f"Tracked execute processes:\n{procs[:500]}\n\n"
                f"Directory listings:\n{dirs_block}\n\n"
                f"File contents:\n{files_block}\n\n"
                f"Already applied deterministic fixes: {heuristic or ['(none)']}\n"
                "Propose any remaining ACTIONS (or NEEDS_REPLAN)."
            )
        ),
    ]
    result = invoke_llm(messages)
    raw = message_text(result)
    needs_replan = parse_needs_replan(raw)
    llm_fixes: list[str] = []
    if not needs_replan:
        llm_fixes = _parse_and_apply_llm_actions(memory, raw)

    # Re-run heuristics after LLM in case diagnosis text helps (module error still present)
    heuristic2 = apply_heuristic_fixes(memory, blob)
    for h in heuristic2:
        if h not in heuristic:
            heuristic.append(h)

    fixes = heuristic + [f for f in llm_fixes if f not in heuristic]
    if needs_replan:
        # Do not keep unrelated heuristic noise when model asked to replan
        if all("src/__init__" in f for f in fixes) and classify_plan_smell(
            memory, failure=failure, fixes=fixes, needs_replan=needs_replan, error_blob=blob
        ):
            fixes = []

    diagnosis = ""
    for line in raw.splitlines():
        if line.upper().startswith("DIAGNOSIS:"):
            diagnosis = line.split(":", 1)[-1].strip()
            break

    junk = is_junk_run_failure(failure, blob)
    if junk:
        # Junk commands (e.g. bare `Define`) — skip, do not burn replan budget
        needs_replan = None
        plan_smell = False
        fixes = []
    else:
        plan_smell = classify_plan_smell(
            memory,
            failure=failure,
            fixes=fixes,
            needs_replan=needs_replan,
            error_blob=blob,
        )
        # Deterministic escalate: import/API mismatch with no useful patch
        if plan_smell and not needs_replan and not fixes:
            needs_replan = diagnosis or "run todo does not match module exports"

    memory.append_exec_log(
        "\n".join(
            [
                "\n## FIX",
                f"diagnosis: {diagnosis or '(n/a)'}",
                f"needs_replan: {needs_replan or '(no)'}",
                f"junk_command: {junk}",
                "fixes:",
                *([f"- {f}" for f in fixes] if fixes else ["- (none)"]),
            ]
        )
    )
    if failure and not (needs_replan or plan_smell or junk):
        memory.clear_last_failure()

    summary_lines = [
        "Fix agent",
        f"Diagnosis: {diagnosis or '(see actions)'}",
    ]
    if junk:
        summary_lines.append("Junk run command — skip (not a code fix / replan).")
    if needs_replan:
        summary_lines.append(f"Needs replan: {needs_replan}")
    summary_lines.append(
        "Applied:" if fixes else "Applied: (nothing new — try a clearer /fix note)"
    )
    summary_lines.extend(f"  - {f}" for f in fixes)
    last_file = ""
    for f in reversed(fixes):
        m = re.search(r"`([^`]+)`", f)
        if m:
            last_file = m.group(1)
            break

    return {
        "output": "\n".join(summary_lines),
        "status": "ok" if fixes else "ok",
        "phase": "done",
        "fixes": fixes,
        "last_file": last_file,
        "diagnosis": diagnosis,
        "raw_model": raw[:1500],
        "needs_replan": needs_replan,
        "plan_smell": plan_smell,
        "junk_command": junk,
    }
