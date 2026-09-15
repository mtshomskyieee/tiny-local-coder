"""TUI screens."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from tinylocalcoder.exec.gate import Decision, PendingCommand


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


class PlanEditScreen(ModalScreen[str | None]):
    """Full-bleed editor for plan.md — Save returns text, Cancel returns None."""

    BINDINGS = [
        Binding("ctrl+s", "save_plan", "Save", show=True),
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
    #plan-editor {
        height: 1fr;
        border: solid $accent;
    }
    #plan-edit-buttons {
        height: auto;
        margin-top: 1;
        margin-bottom: 1;
        align: left middle;
    }
    #plan-edit-buttons Button {
        margin-right: 2;
        min-width: 12;
    }
    """

    def __init__(self, initial: str = "") -> None:
        super().__init__()
        self._initial = initial or ""

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-edit-root"):
            yield Static("[b]Edit plan.md[/b]", id="plan-edit-title")
            yield Static(
                "[dim]Ctrl+S save · Esc cancel · Save / Cancel buttons below[/dim]",
                id="plan-edit-hint",
            )
            yield TextArea(
                self._initial,
                id="plan-editor",
                language="markdown",
                soft_wrap=True,
            )
            with Horizontal(id="plan-edit-buttons"):
                yield Button("Save", variant="success", id="save")
                yield Button("Cancel", variant="error", id="cancel")

    def on_mount(self) -> None:
        editor = self.query_one("#plan-editor", TextArea)
        editor.focus()

    def action_save_plan(self) -> None:
        self._save()

    def action_cancel_plan(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        text = self.query_one("#plan-editor", TextArea).text
        self.dismiss(text)

    @on(Button.Pressed, "#save")
    def save_pressed(self) -> None:
        self._save()

    @on(Button.Pressed, "#cancel")
    def cancel_pressed(self) -> None:
        self.dismiss(None)
