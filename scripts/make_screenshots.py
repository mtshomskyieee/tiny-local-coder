#!/usr/bin/env python3
"""Capture README screenshots from the real TUI driven by Textual's Pilot.

Host-only dev tool. Requires a reachable Ollama (`docker compose up -d ollama`).
Nothing is stubbed: the plan and code shown in the images are genuine
qwen2.5:3b output. Writes SVG; convert to PNG with ImageMagick afterwards.

    PYTHONPATH=src python3 scripts/make_screenshots.py --out docs/img
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path


def _prepare_env(workspace: Path, ollama: str) -> None:
    """Settings are lru_cache'd, so the environment must be set before import."""
    os.environ["WORKSPACE_DIR"] = str(workspace)
    os.environ["OLLAMA_BASE_URL"] = ollama
    os.environ["THINKING_ENABLED"] = "false"
    os.environ["AUTO_FIX"] = "true"
    os.environ["AUTO_SKIP"] = "true"
    os.environ["AUTO_REPLAN"] = "true"


async def _wait_for(pilot, predicate, timeout: float, label: str) -> bool:
    """Poll the running app until predicate() is true. CPU inference is slow."""
    waited = 0.0
    while waited < timeout:
        await pilot.pause()
        if predicate():
            return True
        await asyncio.sleep(0.5)
        waited += 0.5
    print(f"  ! timed out after {timeout:.0f}s waiting for {label}", file=sys.stderr)
    return False


def _log_lines(app) -> int:
    from textual.widgets import RichLog

    return len(app.query_one("#log", RichLog).lines)


async def _shot_plan_and_execute(out: Path, size: tuple[int, int], timeout: float) -> None:
    """01 plan mode, 02 execute mid-run, 03 the approval gate."""
    from tinylocalcoder.tui.app import TinyLocalCoderTui
    from tinylocalcoder.tui.screens import ApprovalScreen

    app = TinyLocalCoderTui()
    async with app.run_test(size=size) as pilot:
        await pilot.press(*"/plan", "enter")
        await pilot.pause()
        await pilot.press(*"create and run hello_world.py", "enter")

        print("  waiting for the model to write plan.md …")
        await _wait_for(pilot, lambda: not app._busy and _log_lines(app) > 6, timeout, "plan")
        await pilot.pause()
        app.save_screenshot(filename="01-plan-mode.svg", path=str(out))
        print("  01-plan-mode.svg")

        await pilot.press(*"/execute-plan", "enter")
        await _wait_for(pilot, lambda: app._busy, 60, "execute to start")
        # Let a couple of todos land in the log before shooting.
        for _ in range(16):
            await pilot.pause()
            await asyncio.sleep(0.5)
            if isinstance(app.screen, ApprovalScreen):
                break
        if not isinstance(app.screen, ApprovalScreen):
            app.save_screenshot(filename="02-execute.svg", path=str(out))
            print("  02-execute.svg")

        print("  waiting for a command to hit the approval gate …")
        if await _wait_for(
            pilot, lambda: isinstance(app.screen, ApprovalScreen), timeout, "approval gate"
        ):
            await pilot.press("tab")
            await pilot.pause()
            app.save_screenshot(filename="03-approval-gate.svg", path=str(out))
            print("  03-approval-gate.svg")
            if not (out / "02-execute.svg").exists():
                print("  ! 02-execute.svg was skipped (gate arrived first)", file=sys.stderr)
        app.exit()


async def _shot_help(out: Path, timeout: float) -> None:
    """04 the command reference; /help is long, so use a taller terminal."""
    from tinylocalcoder.tui.app import TinyLocalCoderTui

    app = TinyLocalCoderTui()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.press(*"/help", "enter")
        await _wait_for(pilot, lambda: _log_lines(app) > 20, 30, "help output")
        await pilot.pause()
        app.save_screenshot(filename="04-help.svg", path=str(out))
        print("  04-help.svg")
        app.exit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="docs/img", type=Path)
    ap.add_argument("--size", default="120x34", help="terminal size, WIDTHxHEIGHT")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--timeout", type=float, default=600.0, help="per-wait seconds")
    args = ap.parse_args()

    width, _, height = args.size.partition("x")
    size = (int(width), int(height))
    args.out.mkdir(parents=True, exist_ok=True)

    workspace = Path(tempfile.mkdtemp(prefix="tlc-shots-"))
    _prepare_env(workspace, args.ollama)
    print(f"workspace: {workspace}")

    try:
        asyncio.run(_shot_plan_and_execute(args.out, size, args.timeout))
        asyncio.run(_shot_help(args.out, args.timeout))
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    print(f"\nwrote SVGs to {args.out}; convert with:")
    print(f"  for f in {args.out}/*.svg; do magick -background none -density 200 "
          '"$f" "${f%.svg}.png"; done')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
