# Integration test for plan → execute (C hello world)

Unattended check of the documented `/plan` then `/execute-plan` use case
([docs/use-cases.md](../docs/use-cases.md)). Drive the HTTP API on `:8000`,
not the TUI.

Runner: [`run-hello-world-c.sh`](run-hello-world-c.sh)

```bash
./integration-tests/run-hello-world-c.sh
```

Safe to run again: each invocation stops any leftover suite, resets artifacts,
uses the same canonical build-and-run plan after `/plan`, then restores `./workspace`.

Not wired into `./run-tests.sh` or GitHub unit CI (needs Docker, Ollama,
~12 GB RAM, and a long runtime).

## Purpose

Protect this path:

1. Start the suite with no human input.
2. Ask for a C file `hello-world.c` that prints hello world (**coding only**).
3. Expect a real plan in `workspace/plan.md` that has, of its own accord, a
   `gcc`/`make` compile todo and a `./hello-world` run todo. Then replace it
   with the **canonical build-and-run plan** so the execute stage sees the same
   bytes every run.
4. Execute all three todos, approving the compile and run gates.
5. Expect `workspace/hello-world.c`, an executable `workspace/hello-world`,
   every todo marked `[x]` (no `[!]` skips), and no "junk run command" line in
   `exec.log`.
6. `POST /v1/workspace/clear` (same as TUI `/clear-workspace`).
7. Stop the suite that was started.
8. Exit `0` if every check passed, else `1`.

This test used to force a create-only plan and actively *deny* any `gcc` gate,
because the planner was Python-centric and could not produce a working C plan:
`gcc` was not in the run-command allowlist, so `finalize_plan` dropped every
build todo. Now that languages come from `toolchains.py`, the compile step is
the point of the test. If the planner emits no build todo, or the binary is
never produced, this test should fail.

## Sequence

```mermaid
sequenceDiagram
  participant Script
  participant Start as start-service.sh
  participant API as App_8000
  participant WS as workspace
  participant Stop as stop-service.sh
  Script->>WS: backup then blank plan
  Script->>Start: --no-prompt
  Script->>API: GET /health until ollama ok
  Script->>API: POST /v1/plan
  loop until plan or timeout
    Script->>API: GET /v1/status
    Script->>WS: assert plan.md has todos
  end
  Script->>WS: write canonical build-and-run plan
  Script->>API: POST /v1/execute
  loop until file or timeout
    Script->>API: GET /v1/status
    alt pending_approval
      Script->>API: POST /v1/execute/approve allow_all
    end
    Script->>WS: assert hello-world.c
  end
  Script->>API: POST /v1/workspace/clear
  Script->>Stop: always
  Script->>WS: restore backup keep archive
```

## API: poll and `allow_all`

`POST /v1/plan` and `POST /v1/execute` start a background run and wait about
60 seconds, then may return `status: "running"`. A second `POST` returns 409.

`GET /v1/status` returns the same `RunResponse` snapshot for the last mode so
the script can keep polling.

The compile and run todos do hit the shell gate. The script posts `POST
/v1/execute/approve` with `decision: "allow_all"` for every pending command;
earlier revisions denied anything matching `gcc|clang|compile`, which is
exactly the behaviour this test now exists to prove unnecessary.

## Script steps

1. **Backup `./workspace`** to a temp dir. Reset `plan.md` and remove leftover
   `hello-world.c` / `hello_world.c` / binaries. **Stop** any leftover suite
   so a second run does not inherit the first.
2. **Start** with `AUTO_FIX=false` and `AUTO_REPLAN=false` (autofix was
   adding a compile/run todo) then `./start-service.sh --no-prompt`. Wait until
   `GET http://localhost:8000/health` reports `ollama: true` (about 5 minutes
   for a first model pull).
3. **Plan:** `POST /v1/plan` with a **coding-only** prompt (create
   `hello-world.c`, no compile/run) and `"thinking_enabled": false`. Poll
   `GET /v1/status` until the run is **finished** (`ok` / `error` / `denied` /
   `failed`). Do not start execute while status is `running` (that is a 409).
4. **Expect a plan:** after the plan run is done, fail if `plan.md` has no
   numbered hello-world todo, **or no `gcc`/`make` compile todo and
   `./hello-world` run todo** — the planner must reach that shape unaided.
   Then write the **same canonical build-and-run `plan.md`** every run
   (create, compile, run). Writing only after the planner thread exits so it
   cannot overwrite the fixture.
5. **Execute:** `POST /v1/execute` walks all three todos, approving each gate.
   Fail immediately on HTTP 409. Poll until execute is **finished**. Do not
   treat a non-empty file as done while status is still `running`.
6. **Expect a working program:** `hello-world.c` exists, is non-empty and
   contains `hello` / `printf` / `puts`; `hello-world` exists and is
   executable; every todo is `[x]` with no `[!]` skips; and `exec.log` has no
   `junk run command` line.
7. **`/clear-workspace`:** only after execute has finished. `POST
   /v1/workspace/clear` returns 409 if a run is still alive. On success it
   archives into `workspace/archive/<timestamp>` and blanks `plan.md`. Fail if
   `hello-world.c` is still in the live workspace.
8. **Shutdown** with `./stop-service.sh` from the EXIT trap (always stop what
   we started).
9. **Exit:** print a short pass/fail summary. Restore the pre-test workspace
   backup, but keep `archive/` from `/clear-workspace`. `exit 1` if any
   check failed, else `exit 0`.

Helpers are bash plus `curl` / `python3` for JSON. Failures set `FAILED=1`
and are reported at the end; the trap always runs.

## Repeatability

Each run is meant to be interchangeable:

- Stop leftover compose first; stop again on EXIT (idempotent cleanup).
- `AUTO_FIX=false` and `AUTO_REPLAN=false` so recovery cannot add compile todos.
- After `/plan` **finishes** (not merely after `plan.md` looks valid), execute
  always sees the same canonical `plan.md`.
- Execute must finish before `/clear-workspace`. The API refuses clear while a
  run thread is alive (409).
- HTTP helpers record the status code so 409 is not treated as an in-flight
  snapshot.
- Compile/run gates (`gcc`, `clang`, `./hello-world`) are **denied**.
- End with `/clear-workspace` (`POST /v1/workspace/clear`) so test files are
  archived, not left in the live workspace.
- Workspace backup is restored on every exit so the next run starts from the
  user's files; `archive/` from `/clear-workspace` is kept.

## Isolation / safety

- Write into the live `./workspace` only during the test window; restore on EXIT.
- Do not add this runner to `.github/workflows/tests.yml`.
- Do not change `PLAN_SYSTEM` for this test.

## Timeouts

| Phase | Timeout |
| --- | --- |
| Health / Ollama ready | 5 minutes |
| Plan complete | 10 minutes |
| Execute complete | 20 minutes |

## Out of scope

- Other use cases (`/fix`, `/review`, `/test`).
- Wiring into `./run-tests.sh`.
- Changing Colima / compose RAM defaults.
