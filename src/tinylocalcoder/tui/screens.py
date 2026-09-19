"""TUI screens."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from tinylocalcoder.exec.gate import Decision, PendingCommand
from tinylocalcoder.memory.files import plan_text_equivalent


class ApprovalScreen(ModalScreen[Decision]):
    """Modal: Allow / Deny / Allow all."""

    CSS = """
    #approve-dialog {
        width: 80%;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $warning;
        margin: 4 0;
    }
    #approve-cmd {
        color: $success;
        margin: 1 0;
    }
    #approve-buttons {
        height: auto;
        margin-top: 1;
    }
    """

    def __init__(self, pending: PendingCommand) -> None:
        super().__init__()
        self.pending = pending

    def compose(self) -> ComposeResult:
        with Vertical(id="approve-dialog"):
            yield Static("[b]Approve command?[/b]", id="approve-title")
            yield Static(f"cwd: {self.pending.cwd}")
            yield Static(f"$ {self.pending.command}", id="approve-cmd")
            if self.pending.reason:
                yield Static(f"reason: {self.pending.reason}")
            with Horizontal(id="approve-buttons"):
                yield Button("Allow", variant="success", id="allow")
                yield Button("Deny", variant="error", id="deny")
                yield Button("Allow all", variant="warning", id="allow_all")

    @on(Button.Pressed, "#allow")
    def allow(self) -> None:
        self.dismiss(Decision.ALLOW)

    @on(Button.Pressed, "#deny")
    def deny(self) -> None:
        self.dismiss(Decision.DENY)

    @on(Button.Pressed, "#allow_all")
    def allow_all(self) -> None:
        self.dismiss(Decision.ALLOW_ALL)


@dataclass(frozen=True)
class PlanEditResult:
    """Text to persist after plan-edit. skip_standards means the user undid auto-fix."""

    text: str
    skip_standards: bool = False


def plan_edit_needs_review(
    draft: str, finalized: str, *, skip_standards: bool
) -> bool:
    """True when Save should keep the editor open to review an automated rewrite."""
    if skip_standards:
        return False
    return not plan_text_equivalent(draft, finalized)


class PlanEditScreen(ModalScreen[PlanEditResult | None]):
    """Full-bleed editor for plan.md — Save returns text, Cancel returns None."""

    BINDINGS = [
        Binding("ctrl+s", "save_plan", "Save", show=True),
        Binding("ctrl+u", "undo_fix", "Undo fix", show=True),
        Binding("escape", "cancel_plan", "Cancel", show=True),
    ]

    CSS = """
    #plan-edit-root {
        width: 100%;
        height: 100%;
        background: $surface;
        padding: 1 1 0 1;
    }
    #plan-edit-title {
        height: 1;
        margin-bottom: 1;
    }
    #plan-edit-hint {
        height: 1;
        color: $text-muted;
        margin-bottom: 1;
    }
    #plan-edit-warn {
        height: auto;
        color: $warning;
        background: $warning 15%;
        border: tall $warning;
        padding: 0 1;
        display: none;
        margin-bottom: 1;
    }
    #plan-editor {
        height: 1fr;
        border: solid $accent;
    }
    #plan-edit-buttons {
        dock: bottom;
        height: 3;
        width: 100%;
        align: left middle;
    }
    #plan-edit-buttons Button {
        margin-right: 2;
        min-width: 12;
    }
    """

    def __init__(
        self,
        initial: str = "",
        *,
        finalize: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__()
        self._initial = initial or ""
        self._finalize = finalize
        self._undo_buffer: str | None = None
        self._skip_standards = False

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-edit-root"):
            yield Static("[b]Edit plan.md[/b]", id="plan-edit-title")
            yield Static(
                "[dim]Ctrl+S save · Ctrl+U undo automated fix · Esc cancel[/dim]",
                id="plan-edit-hint",
            )
            yield Static("", id="plan-edit-warn")
            yield TextArea(
                self._initial,
                id="plan-editor",
                language="markdown",
                soft_wrap=True,
            )
            with Horizontal(id="plan-edit-buttons"):
                yield Button("Save", variant="success", id="save")
                yield Button("Undo fix", variant="warning", id="undo", disabled=True)
                yield Button("Cancel", variant="error", id="cancel")

    def on_mount(self) -> None:
        editor = self.query_one("#plan-editor", TextArea)
        editor.focus()

    def action_save_plan(self) -> None:
        self._save()

    def action_undo_fix(self) -> None:
        self._undo_automated_fix()

    def action_cancel_plan(self) -> None:
        self.dismiss(None)

    def _editor_text(self) -> str:
        editor = self.query_one("#plan-editor", TextArea)
        doc = getattr(editor, "document", None)
        if doc is not None:
            text = getattr(doc, "text", None)
            if text is not None:
                return str(text)
        return editor.text or ""

    def _set_editor_text(self, text: str) -> None:
        editor = self.query_one("#plan-editor", TextArea)
        load = getattr(editor, "load_text", None)
        if callable(load):
            load(text)
            return
        editor.text = text

    def _set_warning(self, message: str | None) -> None:
        warn = self.query_one("#plan-edit-warn", Static)
        if message:
            warn.update(f"[b]⚠ !!!! {message}[/]")
            warn.display = True
        else:
            warn.update("")
            warn.display = False
        undo = self.query_one("#undo", Button)
        undo.disabled = not bool(message)

    def _save(self) -> None:
        draft = self._editor_text()
        if self._finalize is None or self._skip_standards:
            self.dismiss(PlanEditResult(draft, skip_standards=self._skip_standards))
            return
        finalized = self._finalize(draft)
        if not plan_edit_needs_review(
            draft, finalized, skip_standards=self._skip_standards
        ):
            self.dismiss(PlanEditResult(finalized, skip_standards=False))
            return
        self._undo_buffer = draft
        self._set_editor_text(finalized)
        self._set_warning(
            "This plan didn't fit standards and has been fixed. "
            "Review the rewrite — Save to keep it, or Undo the automated fix."
        )
        self.query_one("#plan-editor", TextArea).focus()

    def _undo_automated_fix(self) -> None:
        if self._undo_buffer is None:
            return
        restored = self._undo_buffer
        self._undo_buffer = None
        self._skip_standards = True
        self._set_editor_text(restored)
        self._set_warning(None)
        self.query_one("#plan-editor", TextArea).focus()

    @on(Button.Pressed, "#save")
    def save_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._save()

    @on(Button.Pressed, "#undo")
    def undo_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._undo_automated_fix()

    @on(Button.Pressed, "#cancel")
    def cancel_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)
