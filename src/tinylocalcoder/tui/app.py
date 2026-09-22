"""Textual TUI: /plan /code /execute /ask with gated command approval."""

from __future__ import annotations

import threading
import time
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Header, Input, RichLog, Static

from tinylocalcoder.config import get_settings
from tinylocalcoder.exec.gate import ApprovalGate, Decision, PendingCommand
from tinylocalcoder.graph.builder import build_pipeline
from tinylocalcoder.memory.files import META_TODO_NAMES, meta_command_name
from tinylocalcoder.model_config import iter_models, load_model_choice
from tinylocalcoder.tui.screens import ApprovalScreen, PlanEditResult, PlanEditScreen
from tinylocalcoder.usage import get_usage_tracker

_MODE_ALIASES = {
    "plan": "plan",
    "code": "code",
    "execute": "execute",
    "ask": "ask",
    "fix": "fix",
    "execute-plan": "execute",
    "exec-plan": "execute",
    "run-plan": "execute",
}

_WORKFLOW_COMMANDS = frozenset(
    {"review", "test", "review-fix", "fix-plan", "soup-to-nuts"}
)

_META_COMMANDS = set(META_TODO_NAMES) | {
    "help",
    "clear",
    "new",
    "reset",
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
    "skip-todo",
    "reset-todo",
    "reset-all-skipped",
    "reset-skipped",
    "review",
    "review-fix",
    "fix-plan",
    "test",
    "soup-to-nuts",
    "procs",
    "processes",
    "kill-procs",
    "auto-replan",
    "autoreplan",
    "auto-install",
    "autoinstall",
}

# Mode words that are also meta when used as /commands, but must NOT steal
# freeform plan prompts like "plan a flask app" when typed without /.
_META_ONLY_WITH_SLASH = frozenset(
    {
        "plan",
        "code",
        "ask",
        "execute",
        "fix",
        "reset",
        "review",
        "review-fix",
        "fix-plan",
        "test",
        "soup-to-nuts",
    }
)


def _unslashed_meta_name(raw: str) -> str | None:
    """Detect bare meta commands (e.g. reset-todo 8) that must not hit /plan."""
    name = meta_command_name(raw)
    if not name:
        return None
    if name in _META_ONLY_WITH_SLASH:
        return None
    if name in _MODE_ALIASES:
        return None
    return name

THINKING_TEXT = "thinking …"
_THINKING_PREFIXES = (
    "thinking … ",
    "thinking ... ",
    "thinking… ",
    "thinking... ",
)


def _format_elapsed(seconds: float) -> str:
    """Compact elapsed time: 12s, 1m05s, 1h02m."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"


def _thinking_body(message: str) -> str:
    """Strip leading 'thinking …' so elapsed can be inserted after it."""
    text = (message or "").strip() or "working"
    for prefix in _THINKING_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):].strip() or "working"
    if text in {"thinking …", "thinking...", "thinking…", "thinking ..."}:
        return "working"
    return text


def _thinking_for_mode(mode: str, prompt: str, memory) -> str:
    """Build a short status line for the thinking bar before work starts."""
    prompt = (prompt or "").strip()
    if mode == "review":
        return "thinking … review: 1/4 writing manifest.txt"
    if mode == "review-fix":
        return "thinking … review-fix: 1/3 loading review.md"
    if mode == "fix-plan":
        return "thinking … fix-plan: 1/3 reading requirement and plan.md"
    if mode == "test":
        return "thinking … test: 1/3 looking for existing tests"
    if mode == "soup-to-nuts":
        return "thinking … soup-to-nuts: consent (allow-all)"
    if mode in _WORKFLOW_COMMANDS:
        return f"thinking … {mode}: planning"
    if mode == "plan":
        return f"thinking … planning: {prompt[:56]}" if prompt else "thinking … writing plan"
    if mode == "ask":
        return f"thinking … asking: {prompt[:56]}" if prompt else "thinking … asking"
    if mode == "fix":
        failure = memory.read_last_failure() or {}
        cmd = (failure.get("command") or "").strip()
        step = failure.get("step")
        if step and cmd:
            return f"thinking … fixing step {step}: `{cmd[:48]}`"
        if cmd:
            return f"thinking … fixing: `{cmd[:56]}`"
        return f"thinking … fixing: {prompt[:56]}" if prompt else "thinking … diagnosing failure"
    if mode in {"execute", "code"}:
        todo = memory.next_todo()
        if todo:
            total = memory.progress_summary().get("todos_total", "?")
            target = (todo.target or "")[:56]
            return f"thinking … step {todo.number}/{total}: {todo.action} `{target}`"
        if prompt:
            return f"thinking … {mode}: {prompt[:56]}"
        return f"thinking … {mode}"
    return THINKING_TEXT


class PromptInput(Input):
    """Input with up/down arrow command history (bash-style)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        *Input.BINDINGS,
        Binding("up", "history_older", "History older", show=False),
        Binding("down", "history_newer", "History newer", show=False),
    ]

    def __init__(self, *args, history: list[str] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._history: list[str] = history if history is not None else []
        self._history_index: int | None = None
        self._draft = ""

    def remember(self, value: str) -> None:
        """Push a submitted line onto history (no consecutive duplicates)."""
        text = (value or "").strip()
        if not text:
            return
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_index = None
        self._draft = ""

    def action_history_older(self) -> None:
        if not self._history:
            return
        if self._history_index is None:
            self._draft = self.value
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return
        self.value = self._history[self._history_index]
        self.cursor_position = len(self.value)

    def action_history_newer(self) -> None:
        if self._history_index is None:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.value = self._history[self._history_index]
        else:
            self._history_index = None
            self.value = self._draft
        self.cursor_position = len(self.value)


_HELP = """[b]Commands[/b]
  [cyan]/plan[/] [green]/code[/] [yellow]/execute[/] (/execute-plan) [magenta]/ask[/]
  [blue]/review[/]      — inventory → per-file notes in review.md (shown in the log)
  [blue]/review-fix[/]  — plan fixes from review.md, then execute that plan
  [blue]/fix-plan[/]    — compare requirement vs plan.md and rewrite the todos
  [blue]/test[/]        — find tests, show the plan, run each step (live in the log)
  [blue]/soup-to-nuts[/] — plan → execute → review → review-fix → test (one allow-all consent)
  [red]/fix[/]         — last failure or a file hint (e.g. /fix start_service.sh)
  [b]/procs[/]         — list PIDs tracked from /execute (ports, status)
  [b]/kill-procs[/]    — kill all tracked execute processes
  [b]/auto-fix[/]      — show auto-fix status (on by default after failures)
  [b]/auto-fix on|off[/] — enable/disable automatic repair+retry on failed steps
  [b]/auto-skip[/]       — show auto-skip status (on by default)
  [b]/auto-skip on|off[/] — skip failed todos after fix and continue plan
  [b]/auto-replan[/]     — show auto-replan status (on by default)
  [b]/auto-replan on|off[/] — rewrite open todos when a fix cannot repair the plan
  [b]/auto-install[/]    — show auto-install status (on by default)
  [b]/auto-install on|off[/] — apt-get a missing language toolchain, then retry
  [b]/skip-todo N[/]     — mark todo N as skipped ([!]) so execute can move on
  [b]/reset-todo N[/]    — reopen skipped todo N ([!] → [ ])
  [b]/reset-all-skipped[/] — reopen every skipped todo ([!] → [ ])
  [b]/code show path[/] — print a workspace file (e.g. /code show src/main.py)
  [b]/code update …[/] — edit a named file from your prompt (writes to disk)
  [b]/show-plan[/]     — print current plan.md
  [b]/plan-edit[/]     — full-screen edit plan.md (Save applies standards; Undo if rewritten)
  [b]/clear-plan[/]    — reset plan.md to empty todos (code files kept)
  [b]/archive-plan[/]  — save plan.md under workspace/archives/ then clear
  [b]/archive name[/]  — copy entire workspace into workspace/archive/<name>
  [b]/clear-workspace[/] — move everything (except archive/) into archive/<timestamp>, blank plan/ask
  [b]/plan show|clear|archive|edit[/] — same as above
  [b]/clear[/] (/new)  — reset ask/exec session logs (keeps plan + code)
  [b]/model[/]         — show current model + how to change it (config.toml)
  [b]/usage[/]         — token usage (session + lifetime; no $ cost)
  [b]/help[/]          — this list
  [b]/quit[/] (/exit)  — leave the TUI
  [dim]↑/↓[/]            — recall / edit previous prompts
"""


class TinyLocalCoderTui(App[None]):
    TITLE = "TinyLocalCoder"
    SUB_TITLE = "plan · code · execute · ask"

    CSS = """
    Screen {
        layout: vertical;
    }
    #status {
        height: 3;
        padding: 0 1;
        background: $surface;
        border: solid $accent;
    }
    #thinking {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-style: italic;
    }
    #thinking.active {
        color: $warning;
        text-style: bold italic;
    }
    #log {
        height: 1fr;
        border: solid $primary;
    }
    #input {
        dock: bottom;
        margin: 0 0 1 0;
    }
    """

    BINDINGS = [
        Binding("f1", "mode_plan", "Plan", show=True),
        Binding("f2", "mode_code", "Code", show=True),
        Binding("f3", "mode_execute", "Execute", show=True),
        Binding("f5", "execute_plan", "ExecPlan", show=True),
        Binding("f4", "mode_ask", "Ask", show=True),
        Binding("ctrl+c", "quit", "Quit", show=True),
    ]

    def action_quit(self) -> None:
        self.exit()

    def __init__(self) -> None:
        super().__init__()
        self.settings = get_settings()
        self.gate = ApprovalGate()
        self.gate.set_callback(self._sync_approve)
        self.pipeline = build_pipeline(gate=self.gate, settings=self.settings)
        self.pipeline.set_progress_callback(self._on_pipeline_progress)
        self.mode = "plan"
        self._busy = False
        self._session_started_at = time.monotonic()
        self._thinking_started_at: float | None = None
        self._thinking_body = ""
        self._input_history: list[str] = []
        get_usage_tracker().load()
        # Show startup automation flags when non-default or explicitly set via CLI/env.
        self._startup_automation_note = (
            f"autofix={'on' if self.settings.auto_fix else 'off'}  "
            f"autoskip={'on' if self.settings.auto_skip else 'off'}  "
            f"autoreplan={'on' if self.settings.auto_replan else 'off'}  "
            f"autoinstall={'on' if self.settings.auto_install else 'off'}"
        )

    def _on_pipeline_progress(self, message: str) -> None:
        """Update thinking bar from a worker thread during pipeline steps."""
        self.call_from_thread(self._show_thinking, message)
        if (
            message.startswith("review »")
            or message.startswith("review-fix »")
            or message.startswith("test »")
            or message.startswith("fix-plan »")
            or message.startswith("soup-to-nuts »")
        ):
            self.call_from_thread(self._append_log, f"[blue]{message}[/]")
        if message.startswith("review » manifest.txt ready"):
            self.call_from_thread(self._show_manifest)
        if message.startswith("review » review.md saved"):
            self.call_from_thread(self._show_review_md)
        if message.startswith("review-fix » review.md ready"):
            self.call_from_thread(self._show_review_md)
        if message.startswith("review-fix » plan.md ready"):
            self.call_from_thread(self._show_current_plan)
        if message.startswith("test » plan.md ready"):
            self.call_from_thread(self._show_current_plan)
        if message.startswith("fix-plan » plan.md ready"):
            self.call_from_thread(self._show_current_plan)
        if message.startswith("soup-to-nuts » plan.md ready"):
            self.call_from_thread(self._show_current_plan)
        if message.startswith("todos »"):
            self.call_from_thread(self._show_todo_board)

    def _append_log(self, markup: str) -> None:
        self.query_one("#log", RichLog).write(markup)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._status_text(), id="status")
        yield Static("", id="thinking")
        yield RichLog(id="log", highlight=True, markup=True)
        yield PromptInput(
            placeholder="Prompt, or /show-plan /clear-plan /archive-plan /execute /quit …",
            id="input",
            history=self._input_history,
        )
        yield Footer()

    def on_mount(self) -> None:
        self._session_started_at = time.monotonic()
        self.set_interval(1.0, self._tick_clocks)
        self._print_banner()
        self.query_one("#input", PromptInput).focus()

    def _reset_session_clock(self) -> None:
        self._session_started_at = time.monotonic()

    def _tick_clocks(self) -> None:
        """Refresh status (session elapsed) and thinking elapsed once per second."""
        self._refresh_status()
        if self._busy and self._thinking_started_at is not None:
            self._render_thinking()

    def _print_banner(self) -> None:
        log = self.query_one("#log", RichLog)
        log.write("[b]TinyLocalCoder[/b] — file-backed agents on local Ollama")
        log.write(
            "Modes: [cyan]/plan[/] [green]/code[/] "
            "[yellow]/execute-plan[/] [red]/fix[/] [magenta]/ask[/]  ·  "
            "flows: [blue]/review /review-fix /fix-plan /test /soup-to-nuts[/]  ·  "
            "plan: [b]/show-plan /plan-edit /clear-plan /archive /clear-workspace[/b]  ·  "
            "meta: [b]/auto-fix /auto-skip /skip-todo /reset-todo "
            "/reset-all-skipped /clear /model /usage /help /quit[/b]"
        )
        log.write(
            "Flow: [cyan]/plan[/] → [yellow]/execute-plan[/] "
            "(or F5) → approve · failures auto-fix then auto-skip when enabled"
        )
        log.write(f"[dim]{self._startup_automation_note}[/dim]")

    def _status_text(self) -> str:
        p = self.pipeline.memory.progress_summary()
        u = get_usage_tracker().session
        todos = f"{p.get('todos_done', 0)}/{p.get('todos_total', 0)}"
        skip = p.get("todos_skipped", 0)
        if skip:
            todos = f"{todos}+{skip}skip"
        elapsed = _format_elapsed(time.monotonic() - self._session_started_at)
        return (
            f"mode=[b]{self.mode}[/b]  "
            f"todos {todos}  "
            f"files {p['files_done']}/{p['files_total']}  "
            f"cmds {p['cmds_done']}/{p['cmds_total']}  "
            f"model={self.settings.model_name}  "
            f"tok={u.total_tokens}  "
            f"t={elapsed}  "
            f"autofix={'on' if self.settings.auto_fix else 'off'}  "
            f"autoskip={'on' if self.settings.auto_skip else 'off'}  "
            f"autoreplan={'on' if self.settings.auto_replan else 'off'}  "
            f"autoinstall={'on' if self.settings.auto_install else 'off'}  "
            f"thinking={'on' if self.settings.thinking_enabled else 'off'}"
        )

    def _refresh_status(self) -> None:
        self.query_one("#status", Static).update(self._status_text())

    def _render_thinking(self) -> None:
        started = self._thinking_started_at
        if started is None:
            return
        elapsed = _format_elapsed(time.monotonic() - started)
        body = self._thinking_body or "working"
        message = f"thinking … {elapsed} · {body}"
        widget = self.query_one("#thinking", Static)
        widget.update(message)
        self.sub_title = message

    def _show_thinking(self, message: str = THINKING_TEXT) -> None:
        if not self._busy or self._thinking_started_at is None:
            self._thinking_started_at = time.monotonic()
        self._busy = True
        self._thinking_body = _thinking_body(message)
        widget = self.query_one("#thinking", Static)
        widget.add_class("active")
        self._render_thinking()

    def _clear_thinking(self) -> None:
        self._busy = False
        self._thinking_started_at = None
        self._thinking_body = ""
        widget = self.query_one("#thinking", Static)
        widget.remove_class("active")
        widget.update("")
        self.sub_title = "plan · code · execute · ask"

    def _show_current_plan(self) -> None:
        log = self.query_one("#log", RichLog)
        text = self.pipeline.memory.read_plan().strip() or "(empty plan)"
        p = self.pipeline.memory.progress_summary()
        log.write(
            f"[b]Current plan[/b] "
            f"(todos {p.get('todos_done', 0)}/{p.get('todos_total', 0)})"
        )
        # Cap display so a huge plan doesn't flood the TUI
        if len(text) > 6000:
            text = text[:6000] + "\n…[truncated]"
        log.write(text)

    def _show_todo_board(self) -> None:
        """Reprint every todo after a step completes, including [x] / [!]."""
        from rich.markup import escape

        log = self.query_one("#log", RichLog)
        memory = self.pipeline.memory
        p = memory.progress_summary()
        skip = p.get("todos_skipped", 0)
        extra = f"  +{skip} skipped" if skip else ""
        log.write(
            f"[b]Todos[/b] {p.get('todos_done', 0)}/{p.get('todos_total', 0)}{extra}"
        )
        todos = memory.parse_todos()
        if not todos:
            log.write("[dim](no todos)[/]")
            return
        for todo in todos:
            line = escape(memory.format_todo_line(todo))
            if todo.done:
                log.write(f"[green]{line}[/]")
            elif todo.skipped:
                log.write(f"[yellow]{line}[/]")
            else:
                log.write(f"[dim]{line}[/]")

    def _ensure_review_md(self) -> str:
        """Write review.md from the manifest if it is missing or blank."""
        from tinylocalcoder.tools.review import write_review

        memory = self.pipeline.memory
        body = memory.read_prototype("review.md").strip()
        if body:
            return body
        write_review(memory)
        return memory.read_prototype("review.md").strip()

    def _show_manifest(self, text: str = "") -> None:
        from rich.markup import escape

        log = self.query_one("#log", RichLog)
        body = (text or self.pipeline.memory.read_prototype("manifest.txt")).strip()
        log.write("[b cyan]manifest.txt[/]")
        if not body:
            log.write("[dim](no source files inventoried)[/]")
            return
        log.write(escape(body))

    def _show_review_md(self, text: str | None = None) -> None:
        """Display review.md in the log (review workflow deliverable)."""
        from rich.markup import escape

        log = self.query_one("#log", RichLog)
        if text is None:
            body = self._ensure_review_md()
        else:
            body = text.strip() or self._ensure_review_md()
        if not body:
            log.write("[yellow]review.md[/] is empty — no source files to review.")
            return
        log.write("[b green]review.md[/]")
        if len(body) > 12000:
            body = body[:12000] + "\n…[truncated]"
        log.write(escape(body))

    def _handle_meta(self, cmd: str, rest: str = "") -> bool:
        """Handle session meta commands. Returns True if consumed."""
        log = self.query_one("#log", RichLog)
        if cmd in {"exit", "quit", "q"}:
            self.exit()
            return True
        if cmd in _WORKFLOW_COMMANDS:
            if self._busy:
                log.write("[dim]Still thinking ... please wait (or /quit).[/dim]")
                return True
            label = {
                "review": "write manifest.txt, plan from it, write review.md, then execute",
                "review-fix": "plan fixes from review.md, then execute",
                "fix-plan": "compare requirement vs plan.md and rewrite todos",
                "test": "find tests, plan how to run them, then execute each step",
                "soup-to-nuts": (
                    "plan → execute → review → review-fix → test "
                    "(one allow-all consent up front)"
                ),
            }[cmd]
            log.write(f"[blue]/{cmd}[/] — {label}")
            self._start_workflow(cmd, rest)
            return True
        if cmd == "help":
            log.write(_HELP)
            return True
        if cmd in {"show-plan", "plan-show"}:
            self._show_current_plan()
            return True
        if cmd in {"plan-edit", "edit-plan"}:
            if self._busy:
                log.write("[dim]Still thinking ... please wait (or /quit).[/dim]")
                return True
            initial = self.pipeline.memory.read_plan()

            def on_edit_done(result: PlanEditResult | None) -> None:
                log = self.query_one("#log", RichLog)
                if result is None:
                    log.write("[dim]/plan-edit[/] — cancelled; plan.md unchanged.")
                elif result.skip_standards:
                    self.pipeline.memory.save_plan_from_editor(result.text)
                    log.write(
                        "[green]/plan-edit[/] — saved plan.md "
                        "(kept your text after undoing the automated fix)."
                    )
                    self._show_current_plan()
                    self._refresh_status()
                else:
                    self.pipeline.memory.finalize_plan(result.text)
                    log.write("[green]/plan-edit[/] — saved plan.md.")
                    self._show_current_plan()
                    self._refresh_status()
                try:
                    self.query_one("#input", PromptInput).focus()
                except Exception:  # noqa: BLE001
                    pass

            self.push_screen(
                PlanEditScreen(
                    initial,
                    finalize=self.pipeline.memory.render_finalized_plan,
                ),
                on_edit_done,
            )
            return True
        if cmd in {"clear-plan", "plan-clear"}:
            self.pipeline.memory.clear_plan()
            log.write("[green]/clear-plan[/] — plan.md reset to empty Goal/Todos template.")
            self._refresh_status()
            return True
        if cmd in {"archive-plan", "plan-archive"}:
            dest = self.pipeline.memory.archive_plan()
            rel = dest.relative_to(self.pipeline.memory.root)
            log.write(
                f"[green]/archive-plan[/] — saved to [cyan]{rel}[/] and cleared plan.md."
            )
            self._refresh_status()
            return True
        if cmd == "archive":
            name = (rest or "").strip()
            if not name:
                log.write("[red]Usage:[/] /archive <name>")
                log.write(
                    "[dim]Copies the whole workspace into "
                    "[cyan]archive/<name>[/] (live files kept).[/dim]"
                )
                return True
            try:
                dest = self.pipeline.memory.archive_workspace(name)
            except ValueError as exc:
                log.write(f"[red]/archive[/] — {exc}")
                return True
            except FileExistsError as exc:
                log.write(f"[red]/archive[/] — {exc}")
                return True
            except OSError as exc:
                log.write(f"[red]/archive[/] failed: {exc}")
                return True
            rel = dest.relative_to(self.pipeline.memory.root)
            # Count copied entries (exclude ARCHIVE.md)
            n_files = sum(1 for p in dest.rglob("*") if p.is_file())
            log.write(
                f"[green]/archive[/] — workspace copied to [cyan]{rel}/[/] "
                f"({n_files} files). Live workspace unchanged."
            )
            return True
        if cmd in {"clear-workspace", "workspace-clear"}:
            try:
                dest = self.pipeline.memory.clear_workspace()
            except OSError as exc:
                log.write(f"[red]/clear-workspace[/] failed: {exc}")
                return True
            self.gate.reset_session()
            get_usage_tracker().reset_session()
            self._reset_session_clock()
            rel = dest.relative_to(self.pipeline.memory.root)
            n_files = sum(1 for p in dest.rglob("*") if p.is_file())
            log.clear()
            self._print_banner()
            log.write(
                f"[green]/clear-workspace[/] — moved prior work to [cyan]{rel}/[/] "
                f"({n_files} files). Fresh [cyan]plan.md[/], [cyan]ask.md[/], "
                f"and [cyan]session.md[/] ready."
            )
            self._refresh_status()
            return True
        if cmd in {"clear", "new", "reset"}:
            self.pipeline.memory.clear_session_logs()
            self.gate.reset_session()
            get_usage_tracker().reset_session()
            self._reset_session_clock()
            log.clear()
            self._print_banner()
            log.write(
                "[green]/clear[/] — session ask/exec/session logs reset; "
                "plan.md and code files kept. Session token counter reset."
            )
            self._refresh_status()
            return True
        if cmd == "model":
            choice = load_model_choice()
            log.write(
                f"[b]Current model:[/b] {choice.key} → {self.settings.model_name}"
            )
            log.write("Edit [cyan]config.toml[/] then restart the suite:")
            log.write('  [cyan]model = "qwen3.5"[/]   # or "qwen2.5"')
            log.write("  then [cyan]./start-service.sh[/]")
            log.write("[b]Catalog:[/b]")
            for spec in iter_models():
                mark = " [green](active)[/]" if spec.key == choice.key else ""
                log.write(f"  {spec.key}: {spec.ollama}{mark}")
                log.write(f"    {spec.url}")
            log.write(f"  Ollama base URL: {self.settings.ollama_base_url}")
            return True
        if cmd == "usage":
            log.write(get_usage_tracker().format_report())
            return True
        if cmd in {"auto-fix", "autofix"}:
            arg = (rest or "").strip().lower()
            if arg in {"on", "enable", "true", "1"}:
                self.settings.auto_fix = True
                log.write("[green]/auto-fix on[/] — failures will repair + retry once.")
            elif arg in {"off", "disable", "false", "0"}:
                self.settings.auto_fix = False
                log.write("[yellow]/auto-fix off[/] — use /fix manually after failures.")
            elif arg in {"", "status"}:
                state = "on" if self.settings.auto_fix else "off"
                log.write(
                    f"[b]auto-fix:[/b] {state}  "
                    f"(max {self.settings.auto_fix_max} auto attempt per execute invoke)"
                )
                log.write("Toggle: [cyan]/auto-fix on[/] | [cyan]/auto-fix off[/]")
            else:
                log.write("[red]Usage:[/] /auto-fix [on|off|status]")
            self._refresh_status()
            return True
        if cmd in {"auto-skip", "autoskip"}:
            arg = (rest or "").strip().lower()
            if arg in {"on", "enable", "true", "1"}:
                self.settings.auto_skip = True
                log.write(
                    "[green]/auto-skip on[/] — failed steps marked [!] and plan continues."
                )
            elif arg in {"off", "disable", "false", "0"}:
                self.settings.auto_skip = False
                log.write(
                    "[yellow]/auto-skip off[/] — failed steps stop the execute pass."
                )
            elif arg in {"", "status"}:
                state = "on" if self.settings.auto_skip else "off"
                log.write(f"[b]auto-skip:[/b] {state}")
                log.write("Toggle: [cyan]/auto-skip on[/] | [cyan]/auto-skip off[/]")
            else:
                log.write("[red]Usage:[/] /auto-skip [on|off|status]")
            self._refresh_status()
            return True
        if cmd in {"auto-replan", "autoreplan"}:
            arg = (rest or "").strip().lower()
            if arg in {"on", "enable", "true", "1"}:
                self.settings.auto_replan = True
                log.write(
                    "[green]/auto-replan on[/] — after a failed fix, rewrite open todos once."
                )
            elif arg in {"off", "disable", "false", "0"}:
                self.settings.auto_replan = False
                log.write(
                    "[yellow]/auto-replan off[/] — will not rewrite the plan after fix fails."
                )
            elif arg in {"", "status"}:
                state = "on" if self.settings.auto_replan else "off"
                log.write(f"[b]auto-replan:[/b] {state}")
                log.write("Toggle: [cyan]/auto-replan on[/] | [cyan]/auto-replan off[/]")
            else:
                log.write("[red]Usage:[/] /auto-replan [on|off|status]")
            self._refresh_status()
            return True
        if cmd in {"auto-install", "autoinstall"}:
            arg = (rest or "").strip().lower()
            if arg in {"on", "enable", "true", "1"}:
                self.settings.auto_install = True
                log.write(
                    "[green]/auto-install on[/] — apt-get a missing toolchain and retry."
                )
            elif arg in {"off", "disable", "false", "0"}:
                self.settings.auto_install = False
                log.write(
                    "[yellow]/auto-install off[/] — a missing compiler will be reported, not installed."
                )
            elif arg in {"", "status"}:
                state = "on" if self.settings.auto_install else "off"
                log.write(f"[b]auto-install:[/b] {state}")
                log.write("Toggle: [cyan]/auto-install on[/] | [cyan]/auto-install off[/]")
            else:
                log.write("[red]Usage:[/] /auto-install [on|off|status]")
            self._refresh_status()
            return True
        if cmd == "skip-todo":
            arg = (rest or "").strip()
            try:
                number = int(arg)
                if number < 1:
                    raise ValueError
            except ValueError:
                log.write("[red]Usage:[/] /skip-todo <number>")
                return True
            todos = {t.number: t for t in self.pipeline.memory.parse_todos()}
            if number not in todos:
                log.write(f"[red]/skip-todo[/] — no todo {number} in plan.md")
                return True
            self.pipeline.memory.mark_todo_skipped(number, reason="manual skip")
            log.write(f"[yellow]/skip-todo {number}[/] — marked [!]")
            self._refresh_status()
            return True
        if cmd == "reset-todo":
            arg = (rest or "").strip()
            try:
                number = int(arg)
                if number < 1:
                    raise ValueError
            except ValueError:
                log.write("[red]Usage:[/] /reset-todo <number>")
                return True
            todos = {t.number: t for t in self.pipeline.memory.parse_todos()}
            if number not in todos:
                log.write(f"[red]/reset-todo[/] — no todo {number} in plan.md")
                return True
            if not todos[number].skipped:
                log.write(f"[dim]/reset-todo {number}[/] — todo is not skipped ([!])")
                return True
            self.pipeline.memory.clear_todo_skip(number)
            log.write(f"[green]/reset-todo {number}[/] — reopened as [ ]")
            self._refresh_status()
            return True
        if cmd in {"reset-all-skipped", "reset-skipped"}:
            cleared = self.pipeline.memory.clear_all_todo_skips()
            if cleared:
                nums = ", ".join(str(n) for n in cleared)
                log.write(
                    f"[green]/reset-all-skipped[/] — reopened {len(cleared)} "
                    f"todo(s) as [ ]: {nums}"
                )
            else:
                log.write("[dim]/reset-all-skipped[/] — no skipped ([!]) todos")
            self._refresh_status()
            return True
        if cmd in {"procs", "processes"}:
            log.write(self.pipeline.memory.process_registry().format_report())
            return True
        if cmd == "kill-procs":
            notes = self.pipeline.memory.process_registry().kill_all_running()
            if notes:
                for n in notes:
                    log.write(f"[yellow]{n}[/]")
            else:
                log.write("[dim]No running tracked processes to kill.[/dim]")
            return True
        return False

    def _sync_approve(self, pending: PendingCommand) -> Decision:
        event = threading.Event()
        box: dict[str, Decision] = {}

        def on_dismiss(decision: Decision | None) -> None:
            box["d"] = decision or Decision.DENY
            event.set()

        def open_modal() -> None:
            self._clear_thinking()
            self.push_screen(ApprovalScreen(pending), on_dismiss)

        self.call_from_thread(open_modal)
        event.wait(timeout=600)
        cmd = (pending.command or "").replace("\n", " ").strip()
        if len(cmd) > 56:
            cmd = cmd[:55] + "…"
        self.call_from_thread(
            self._show_thinking, f"thinking … running: `{cmd}`"
        )
        return box.get("d", Decision.DENY)

    def action_mode_plan(self) -> None:
        self.mode = "plan"
        self._refresh_status()

    def action_mode_code(self) -> None:
        self.mode = "code"
        self._refresh_status()

    def action_mode_execute(self) -> None:
        self.mode = "execute"
        self._refresh_status()

    def action_mode_ask(self) -> None:
        self.mode = "ask"
        self._refresh_status()

    def action_execute_plan(self) -> None:
        if self._busy:
            return
        self.mode = "execute"
        self._refresh_status()
        self.pipeline.memory.append_session("user", "/execute-plan")
        self.query_one("#log", RichLog).write(
            "[yellow]/execute-plan[/] — creating files then gated Run/Test steps"
        )
        self._start_pipeline("execute", "")

    def _start_pipeline(self, mode: str, prompt: str) -> None:
        if self._busy:
            self.query_one("#log", RichLog).write(
                "[dim]Still thinking ... please wait (or /quit).[/dim]"
            )
            return
        log = self.query_one("#log", RichLog)
        log.write(f"\n[b]→ /{mode}[/b] {prompt or '(continue current plan)'}")
        self._show_thinking(_thinking_for_mode(mode, prompt, self.pipeline.memory))
        self.run_pipeline(mode, prompt)

    def _start_workflow(self, kind: str, prompt: str = "") -> None:
        """Run fixed /plan prompt then /execute-plan in one busy session."""
        if self._busy:
            self.query_one("#log", RichLog).write(
                "[dim]Still thinking ... please wait (or /quit).[/dim]"
            )
            return
        self.mode = kind
        self._refresh_status()
        log = self.query_one("#log", RichLog)
        if kind == "review":
            log.write(
                f"\n[b]→ /review[/b] {prompt or ''}".rstrip()
                + "\n[dim]workflow: inventory → plan → per-file review → save[/]"
            )
        elif kind == "review-fix":
            log.write(
                f"\n[b]→ /review-fix[/b] {prompt or ''}".rstrip()
                + "\n[dim]workflow: load review.md → plan fixes → execute[/]"
            )
        elif kind == "fix-plan":
            log.write(
                f"\n[b]→ /fix-plan[/b] {prompt or ''}".rstrip()
                + "\n[dim]workflow: requirement vs plan.md → rewrite todos (no execute)[/]"
            )
        elif kind == "test":
            log.write(
                f"\n[b]→ /test[/b] {prompt or ''}".rstrip()
                + "\n[dim]workflow: find tests → plan run steps → execute each[/]"
            )
        elif kind == "soup-to-nuts":
            log.write(
                f"\n[b]→ /soup-to-nuts[/b] {prompt or ''}".rstrip()
                + "\n[dim]workflow: consent → plan → execute → review → "
                "review-fix → test[/]"
            )
        else:
            log.write(
                f"\n[b]→ /{kind}[/b] {prompt or '(plan → execute workflow)'}"
            )
        self._show_thinking(_thinking_for_mode(kind, prompt, self.pipeline.memory))
        self.run_pipeline(kind, prompt, workflow=kind)

    @on(Input.Submitted, "#input")
    def handle_input(self, event: Input.Submitted) -> None:
        raw = (event.value or "").strip()
        event.input.value = ""
        if isinstance(event.input, PromptInput):
            event.input.remember(raw)
        elif raw:
            # Fallback if a plain Input is used in tests
            if not self._input_history or self._input_history[-1] != raw:
                self._input_history.append(raw)

        if raw:
            self.pipeline.memory.append_session("user", raw)

        if raw.startswith("/"):
            parts = raw.split(maxsplit=1)
            cmd = parts[0][1:].lower()
            rest = parts[1] if len(parts) > 1 else ""
            if cmd in _META_COMMANDS:
                if self._handle_meta(cmd, rest):
                    return
            if self._busy:
                self.query_one("#log", RichLog).write(
                    "[dim]Still thinking ... please wait (or /quit).[/dim]"
                )
                return
            # /plan show|clear|archive|edit — plan management without LLM
            if cmd == "plan" and rest:
                sub = rest.split(maxsplit=1)[0].lower()
                if sub in {"show", "clear", "archive", "edit"}:
                    alias = {
                        "show": "show-plan",
                        "clear": "clear-plan",
                        "archive": "archive-plan",
                        "edit": "plan-edit",
                    }[sub]
                    self._handle_meta(alias)
                    return
            if cmd in _MODE_ALIASES:
                mode = _MODE_ALIASES[cmd]
                self.mode = mode
                self._refresh_status()
                force_run = cmd in {
                    "execute-plan",
                    "exec-plan",
                    "run-plan",
                    "execute",
                    "code",
                    "fix",
                }
                if rest or force_run:
                    if cmd in {"execute-plan", "exec-plan", "run-plan"}:
                        self.query_one("#log", RichLog).write(
                            "[yellow]/execute-plan[/] — creating files then gated Run/Test steps"
                        )
                    if cmd == "fix":
                        self.query_one("#log", RichLog).write(
                            "[red]/fix[/] — diagnosing last failure and applying repairs"
                        )
                    self._start_pipeline(mode, rest)
                elif cmd == "plan":
                    # Bare /plan → show current plan and stay in plan mode
                    self._show_current_plan()
                    self.query_one("#log", RichLog).write(
                        "[dim]Plan mode. Type a request to rewrite the plan, "
                        "or /execute-plan to continue.[/dim]"
                    )
                else:
                    self.query_one("#log", RichLog).write(
                        f"[dim]Switched to /{mode}[/dim]"
                    )
                return
            self.query_one("#log", RichLog).write(
                f"[red]Unknown command: {cmd}[/red]  try /help"
            )
            return

        if self._busy:
            self.query_one("#log", RichLog).write(
                "[dim]Still thinking ... please wait (or /quit).[/dim]"
            )
            return
        if not raw:
            self.pipeline.memory.append_session("user", f"/{self.mode}")
            self._start_pipeline(self.mode, "")
            return

        # Bare meta commands (reset-todo 8) must not become plan todos
        bare = _unslashed_meta_name(raw)
        if bare:
            rest = raw.split(maxsplit=1)[1] if len(raw.split(maxsplit=1)) > 1 else ""
            log = self.query_one("#log", RichLog)
            log.write(
                f"[dim]Treating as [cyan]/{bare}[/] (meta commands need a leading /).[/dim]"
            )
            if self._handle_meta(bare, rest):
                return

        self._start_pipeline(self.mode, raw)

    @work(thread=True, exclusive=True)
    def run_pipeline(
        self, mode: str, prompt: str, *, workflow: str | None = None
    ) -> None:
        log = self.query_one("#log", RichLog)
        memory = self.pipeline.memory
        try:
            if workflow:
                result = self.pipeline.invoke_workflow(workflow, prompt)  # type: ignore[arg-type]
            else:
                result = self.pipeline.invoke(mode, prompt)
        except Exception as exc:  # noqa: BLE001

            def fail() -> None:
                self._clear_thinking()
                text = str(exc).strip() or "unknown error"
                log.write("[red]Error:[/red]")
                for line in text.splitlines() or [text]:
                    log.write(f"[red]{line}[/red]")
                memory.append_session("assistant", f"Error: {text}")
                self._refresh_status()

            self.call_from_thread(fail)
            return

        def show() -> None:
            self._clear_thinking()
            out = str(result.get("output") or "")
            session_bits = [out] if out.strip() else []
            if result.get("last_file"):
                session_bits.append(f"file: {result['last_file']}")
            if result.get("last_command"):
                session_bits.append(f"cmd: {result['last_command']}")
            if result.get("critic_used") and result.get("error"):
                session_bits.append(f"critic: {result.get('error')}")
            if session_bits:
                memory.append_session("assistant", "\n".join(session_bits))

            # /review: show steps, manifest, then the written review.md
            if (workflow or result.get("workflow")) == "review":
                log.write("[b]/review[/] — inventory → plan → per-file review → save")
                self._show_manifest(str(result.get("manifest_text") or ""))
                review_body = str(result.get("review_markdown") or "").strip()
                self._show_review_md(review_body or None)
                log.write("[green]saved:[/green] manifest.txt  review.md")
                self._refresh_status()
                log.write("[b green]/review complete.[/]")
                return

            if (workflow or result.get("workflow")) == "review-fix":
                log.write("[b]/review-fix[/] — load review.md → plan fixes → execute")
                issues = str(result.get("issues_text") or "").strip()
                if issues:
                    log.write("[b cyan]findings[/]")
                    log.write(issues)
                self._show_current_plan()
                if result.get("skipped_execute"):
                    log.write("[dim]No fix todos to execute.[/]")
                log.write("[green]saved:[/green] plan.md")
                self._refresh_status()
                log.write("[b green]/review-fix complete.[/]")
                return

            if (workflow or result.get("workflow")) == "fix-plan":
                log.write("[b]/fix-plan[/] — requirement vs plan.md → rewrite todos")
                req = str(result.get("requirement") or "").strip()
                if req:
                    log.write("[b cyan]requirement[/]")
                    log.write(req[:800] + ("…" if len(req) > 800 else ""))
                gaps = result.get("gaps") or []
                if gaps:
                    log.write("[b yellow]gaps[/]")
                    for gap in gaps:
                        log.write(f"  • {gap}")
                self._show_current_plan()
                log.write("[green]saved:[/green] plan.md  (not executed)")
                log.write("[dim]Next: [yellow]/execute-plan[/] or press F5.[/]")
                self._refresh_status()
                log.write("[b green]/fix-plan complete.[/]")
                return

            if (workflow or result.get("workflow")) == "test":
                log.write("[b]/test[/] — find tests → plan run steps → execute")
                found = result.get("test_files") or []
                if found:
                    log.write("[b cyan]test files[/]")
                    for path in found:
                        log.write(f"  {path}")
                self._show_current_plan()
                if result.get("last_command"):
                    log.write(f"[yellow]last cmd:[/yellow] {result['last_command']}")
                log.write("[green]saved:[/green] plan.md")
                self._refresh_status()
                log.write("[b green]/test complete.[/]")
                return

            if (workflow or result.get("workflow")) == "soup-to-nuts":
                if result.get("status") == "denied":
                    log.write("[b red]/soup-to-nuts[/] — consent denied; nothing ran.")
                    self._refresh_status()
                    return
                log.write(
                    "[b]/soup-to-nuts[/] — plan → execute → review → "
                    "review-fix → test"
                )
                review_body = str(result.get("review_markdown") or "").strip()
                if review_body:
                    self._show_review_md(review_body)
                found = result.get("test_files") or []
                if found:
                    log.write("[b cyan]test files[/]")
                    for path in found:
                        log.write(f"  {path}")
                self._show_current_plan()
                self._refresh_status()
                log.write("[b green]/soup-to-nuts complete.[/]")
                return

            if len(out) > 4000:
                out = out[:4000] + "\n…[truncated]"
            log.write(out)
            if result.get("last_file"):
                log.write(f"[green]file:[/green] {result['last_file']}")
            if result.get("last_command"):
                log.write(f"[yellow]cmd:[/yellow] {result['last_command']}")
            if result.get("critic_used") and result.get("error"):
                log.write(f"[dim]critic: {result.get('error')}[/dim]")
            self._refresh_status()
            effective = "execute" if (workflow or result.get("workflow")) else mode
            if mode == "plan" and not workflow:
                p = self.pipeline.memory.progress_summary()
                log.write(
                    f"[b green]Plan ready[/] "
                    f"(files {p['files_total']}, cmds {p['cmds_total']}). "
                    f"Next: type [yellow]/execute-plan[/] or press [b]F5[/b]."
                )
            elif mode == "fix":
                fixes = result.get("fixes") or []
                if fixes:
                    log.write(
                        "[green]Fix applied.[/] Next: [yellow]/execute-plan[/] to retry."
                    )
                else:
                    log.write(
                        "[dim]No file changes. Name the file: "
                        "[red]/fix start_service.sh should import src.db[/] "
                        "or rewrite todos with [blue]/fix-plan[/].[/dim]"
                    )
            elif effective == "execute":
                p = self.pipeline.memory.progress_summary()
                if result.get("fixes"):
                    log.write(
                        "[dim]Auto-fix ran during this execute pass.[/dim]"
                    )
                skipped = p.get("todos_skipped", 0)
                pending = (
                    p["files_done"] < p["files_total"]
                    or p["cmds_done"] < p["cmds_total"]
                    or p.get("todos_done", 0) + skipped < p.get("todos_total", 0)
                )
                if pending:
                    if result.get("status") == "failed":
                        log.write(
                            "[dim]Still failing — try [red]/fix[/], "
                            "[cyan]/auto-fix on[/], or [cyan]/auto-skip on[/].[/dim]"
                        )
                    elif skipped:
                        log.write(
                            f"[yellow]{skipped} step(s) skipped[/] — "
                            "run [yellow]/execute-plan[/] to continue."
                        )
                    else:
                        log.write(
                            "[dim]Plan not finished — run [yellow]/execute-plan[/] again "
                            "to continue.[/dim]"
                        )
                else:
                    if skipped:
                        log.write(
                            f"[b green]Plan finished[/] "
                            f"([yellow]{skipped} step(s) skipped[/])."
                        )
                    else:
                        label = f"/{workflow} " if workflow else ""
                        log.write(f"[b green]{label}Plan execution complete.[/]")

        self.call_from_thread(show)


def run_tui() -> None:
    TinyLocalCoderTui().run()
