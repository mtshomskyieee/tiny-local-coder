# ADR 007: Shell commands stop at the approval gate

Status: Accepted

## Context

The agent proposes shell commands: compiles, smoke runs, and `apt-get` when a compiler is missing. Those commands change the workspace and the container. File writes from a create or refine step are a different action; this decision is about the shell.

## Decision

[`exec/gate.py`](../../src/tinylocalcoder/exec/gate.py) parks every shell command. [`exec/runner.py`](../../src/tinylocalcoder/exec/runner.py) starts it only after a decision.

- `allow` — this command once
- `deny` — skip it and log the denial
- `allow_all` — this command and every later shell command in the session

The TUI asks at the gate. The API returns `pending_approval` (`command_id`, `command`, `cwd`) and waits for `POST /v1/execute/approve`. MCP uses the `approve` tool on the same object. Provisioning uses this gate; an install is never silent.

`allow_all` is off at the start of a session. Something has to set it: a person, an API or MCP client, or the one-time consent command at the start of `soup-to-nuts`. The integration harness sets it so a live run can finish unattended ([004](004-GuardRails.md)). That is the test harness, not the default for interactive use.

A second call while a run is in progress fails. The session has one pipeline and one gate.

## Consequences

- Create and refine steps write files without this gate. Only shell commands stop.
- Review, test, and execute can all return `pending_approval`. The caller handles that result, then the next one.
- `allow_all` lasts for the session and survives later commands with no further prompt. `reset_session` clears it.
- The only shell entry is the gate inside execute and the workflows that call execute. Clients have no generic terminal endpoint.
