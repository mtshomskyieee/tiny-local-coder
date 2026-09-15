"""CLI entrypoints: tui | api."""

from __future__ import annotations

import argparse
import os
import sys


def _apply_env_flags(args: argparse.Namespace) -> None:
    from tinylocalcoder.config import get_settings

    if getattr(args, "auto_skip", None) is not None:
        os.environ["AUTO_SKIP"] = "true" if args.auto_skip else "false"
    if getattr(args, "auto_fix", None) is not None:
        os.environ["AUTO_FIX"] = "true" if args.auto_fix else "false"
    if getattr(args, "auto_skip", None) is not None or getattr(args, "auto_fix", None) is not None:
        get_settings.cache_clear()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tinylocalcoder")
    sub = parser.add_subparsers(dest="command", required=True)

    tui_p = sub.add_parser("tui", help="Run the Textual TUI")
    tui_p.add_argument(
        "--auto-skip",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Skip failed todos after auto-fix and continue (default: on)",
    )
    tui_p.add_argument(
        "--auto-fix",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Auto-repair and retry failed execute steps (default: on)",
    )

    api_p = sub.add_parser("api", help="Run the FastAPI server")
    api_p.add_argument("--host", default=None)
    api_p.add_argument("--port", type=int, default=None)

    args = parser.parse_args(argv)

    if args.command == "api":
        import uvicorn

        from tinylocalcoder.config import get_settings

        settings = get_settings()
        host = args.host or settings.host
        port = args.port or settings.port
        uvicorn.run(
            "tinylocalcoder.api.app:app",
            host=host,
            port=port,
            reload=False,
        )
        return

    _apply_env_flags(args)
    from tinylocalcoder.tui.app import run_tui

    run_tui()


if __name__ == "__main__":
    main(sys.argv[1:])
