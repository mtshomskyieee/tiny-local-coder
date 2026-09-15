# TinyLocalCoder

File-backed LangGraph agents for memory-compromised hosts. Runs Qwen2.5 3B via Ollama (CPU) with plan / code / execute / ask modes.

See **[docs/Architecture.md](docs/Architecture.md)** for plan → execute → fix → replan design (small-model constraints, test-by-default plans, plan-smell recovery), **[docs/orchestration.md](docs/orchestration.md)** for LangGraph agent wiring, and **[docs/use-cases.md](docs/use-cases.md)** for CLI walks (plan/code, fix, review, test).

## Requirements

- Docker + Docker Compose
- ~12GB RAM (CPU-only is fine)
- No GPU required

## Quick start

```bash
cp .env.example .env   # once
./start-service.sh     # rebuild app image, then start Ollama + API
./start-service.sh --no-build   # start without rebuilding
./stop-service.sh      # stop the suite (use this instead of bare docker compose down)
```

API listens on `http://localhost:8000`.

Interactive TUI:

```bash
./cli.sh
# equivalent: docker compose run --rm -it app tui
```

First boot pulls `qwen2.5:3b` into the Ollama volume (slow once). The shared model volume is `crew_pipeline_ollama_data` and is kept across `./stop-service.sh`.

## Modes

| Mode | Role |
|------|------|
| `/plan` | Build `workspace/plan.md` as a **numbered todo list** (Goal + `1. [ ] create …` / `run …`) |
| `/code` | Run the next create/refine todo only (small context) |
| `/execute-plan` | Walk todos one-by-one; each LLM call sees **only the current step** |
| `/ask` | Q&A written to `workspace/ask.md` |
| `/review` | Tool-built `manifest.txt` + opinionated `review.md`, then `/execute-plan` |
| `/test` | Plan a runnable test plan into `plan.md`, then run `/execute-plan` |

Plans use small steps so a 3B model never has to hold the whole plan in context—only `Goal` + the active todo.

**Default plan shape (enforced after `/plan`):** a few `create`/`refine` steps → **one** batched `py_compile` → **one** short `python3 -c` smoke check. Prefer ≤8 todos. Meta commands (`reset-todo`, …) are stripped if they leak into the plan.

On execute failures: **auto-fix** (one code patch + retry) → if the todo itself looks wrong (**plan smell**), **auto-replan** rewrites remaining open todos once → otherwise **auto-skip**.

After `/plan` finishes, the TUI prints a next-step hint. While agents work, a **thinking ...** line is shown and cleared when the reply arrives.

Session meta commands (Claude/Codex-aligned subset):

| Command | Role |
|---------|------|
| `/quit` (`/exit`) | Leave the TUI |
| `/show-plan` (`/plan` or `/plan show`) | Print current `plan.md` |
| `/plan-edit` (`/edit-plan`, `/plan edit`) | Full-screen edit of `plan.md` (Save / Cancel) |
| `/clear-plan` (`/plan clear`) | Reset `plan.md` to empty Goal/Todos (keeps code files) |
| `/archive-plan` (`/plan archive`) | Save `plan.md` under `workspace/archives/` then clear |
| `/clear` (`/new`) | Reset ask/exec session logs; keeps plan + code; resets session token counter |
| `/model` | Show current model and how to set `MODEL_NAME` |
| `/usage` | Session + lifetime token counts (no $ cost) |
| `/auto-fix on|off` | Enable/disable automatic repair+retry on failed steps |
| `/auto-skip on|off` | Skip failed todos after fix and continue plan |
| `/auto-replan on|off` | Rewrite open todos when a fix cannot repair a bad plan step |
| `/skip-todo N` | Mark todo N as skipped (`[!]`) so execute can move on |
| `/reset-todo N` | Reopen skipped todo N (`[!]` → `[ ]`) |
| `/help` | List commands |

### Demo: create and run hello_world.py

1. In TUI: `/plan` then ask: `create and run hello_world.py`
2. Run `/execute-plan` (or press **F5**)
3. Approve `python hello_world.py` when prompted (Allow / Deny / Allow all)

## File memory

Because context is small (`NUM_CTX=2048`), agents use disk as long-term memory:

- `workspace/plan.md` — plan + checkboxes
- `workspace/prototypes/` — generated code
- `workspace/ask.md` — ask transcript
- `workspace/exec.log` — gated command results
- `workspace/.index/` — chunk indexes for retrieval

## Gated execution

No shell command runs without approval:

- **Allow** — this command once
- **Deny** — skip and log
- **Allow all** — auto-approve for the rest of the session

API flow:

- `POST /v1/execute` may return `pending_approval`
- `POST /v1/execute/approve` with `{ "command_id", "decision": "allow"|"deny"|"allow_all" }`

## Thinking critic

End-of-graph node asks once whether the task is complete. Disable with:

```bash
THINKING_ENABLED=false
```

## Configuration

See `.env.example`. Important knobs:

- `MODEL_NAME=qwen2.5:3b`
- `NUM_CTX=2048` (raise to 4096 only if you have headroom)
- `AUTO_FIX` / `AUTO_FIX_MAX` — one code repair+retry per failed run step
- `AUTO_REPLAN` — one open-todo rewrite when the failing step looks like a bad plan
- `AUTO_SKIP` — mark failed steps `[!]` and continue
- `WORKSPACE_DIR=/workspace`
- `EXEC_TIMEOUT_SEC=60`

## API

- `GET /health`
- `POST /v1/plan` `{ "prompt": "..." }`
- `POST /v1/code` `{ "prompt": "..." }`
- `POST /v1/execute` `{ "prompt": "..." }`
- `POST /v1/execute/approve` `{ "command_id": "...", "decision": "allow" }`
- `POST /v1/ask` `{ "prompt": "..." }`
- `POST /v1/review` `{ "prompt": "..." }` — plan then execute review workflow
- `POST /v1/test` `{ "prompt": "..." }` — plan then execute test workflow
- `GET /v1/workspace/files`
- `GET /v1/workspace/file?path=plan.md`
