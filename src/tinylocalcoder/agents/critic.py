"""Optional end-of-graph critic — one completion check on current step only."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from tinylocalcoder.llm import invoke_llm, message_text
from tinylocalcoder.memory.files import WorkspaceMemory


CRITIC_SYSTEM = """You decide if work should stop for now.
Reply with exactly one line:
CONTINUE: <short reason>
or
DONE: <short reason>

Rules:
- If unchecked todos remain, NEVER claim they are finished.
- Prefer DONE after a plan rewrite (plan is ready; user will /execute-plan).
- Prefer DONE after a successful step unless the next unchecked todo is obviously needed right now.
- Prefer CONTINUE only when recent output failed and an unchecked todo still needs work.
"""


def run_critic(
    memory: WorkspaceMemory,
    prompt: str,
    recent_output: str,
    mode: str = "",
    replan_attempts: int = 0,
) -> dict:
    progress = memory.progress_summary()
    nxt = memory.next_todo()
    pending = progress.get("todos_total", 0) - progress.get("todos_done", 0)

    # Plan mode: never invent "all todos complete" — the plan just got written.
    if mode == "plan":
        text = (
            f"DONE: Plan ready with {pending} open todo(s)."
            if pending
            else "DONE: Plan ready."
        )
        return {
            "output": text,
            "critic_continue": False,
            "critic_used": True,
        }

    if mode == "fix":
        return {
            "output": "DONE: Fix pass finished. Run /execute-plan to retry.",
            "critic_continue": False,
            "critic_used": True,
        }

    if replan_attempts > 0 and mode == "execute" and "Replan agent" in (recent_output or ""):
        text = (
            f"DONE: Plan adjusted after failure; {pending} open todo(s) — continue /execute-plan."
            if pending
            else "DONE: Plan adjusted after failure."
        )
        return {
            "output": text,
            "critic_continue": False,
            "critic_used": True,
        }

    # Deterministic: if execute/code thinks everything is done, don't ask the LLM.
    if not nxt and mode in {"execute", "code"}:
        return {
            "output": "DONE: No remaining todos.",
            "critic_continue": False,
            "critic_used": True,
        }

    # Code mode with only run/test left — don't claim the plan is finished.
    if mode == "code" and nxt and nxt.action in {"run", "test"}:
        return {
            "output": (
                f"DONE: Coding idle; next todo is run/test step {nxt.number}. "
                "Use /execute-plan (or /code with an explicit file edit)."
            ),
            "critic_continue": False,
            "critic_used": True,
        }

    step_line = memory.current_step_prompt(nxt) if nxt else "(no remaining todos)"
    messages = [
        SystemMessage(content=CRITIC_SYSTEM),
        HumanMessage(
            content=(
                f"Mode: {mode or 'unknown'}\n"
                f"User request: {prompt}\n"
                f"Todos done/total: {progress.get('todos_done', 0)}/"
                f"{progress.get('todos_total', 0)} "
                f"(open={pending}; files {progress['files_done']}/{progress['files_total']}, "
                f"cmds {progress['cmds_done']}/{progress['cmds_total']})\n"
                f"Next unchecked step if any:\n{step_line}\n"
                f"Recent output:\n{(recent_output or '')[:800]}\n"
            )
        ),
    ]
    result = invoke_llm(messages)
    text = message_text(result)
    # Guard: if todos remain, refuse a false "all complete" DONE from the model.
    if pending > 0 and text.upper().startswith("DONE") and "all" in text.lower() and (
        "complete" in text.lower() or "finished" in text.lower()
    ):
        text = f"DONE: Stop for now; {pending} todo(s) still open — use /execute-plan."
    should_continue = text.upper().startswith("CONTINUE")
    return {
        "output": text,
        "critic_continue": should_continue,
        "critic_used": True,
    }
