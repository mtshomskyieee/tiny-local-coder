# ADR 003: One session, three standard surfaces

Status: Accepted

## Context

A person has to drive TinyLocalCoder from a terminal, and another agent has to drive it through a protocol that agent already speaks. A private SDK, or a second process on the same workspace, would split the plan and the approval gate.

## Decision

One process owns one pipeline, one workspace, and one approval gate. Three surfaces talk to that process. Roles differ; session state does not.

- **CLI.** [`cli.sh`](../../cli.sh) starts the Textual TUI, and starts the suite first when Ollama is not healthy. Meta commands need a leading `/` (`/plan`, `/execute-plan`, `/ask`, `/review`, `/test`). This is the surface for a person at the keyboard.
- **MCP.** Streamable HTTP at `http://127.0.0.1:8000/mcp`, contract in [llm.txt](../llm.txt). Tools: `plan`, `code`, `execute`, `ask`, `review`, `review_fix`, `fix_plan`, `test`, `status`, `approve`, plus workspace reads. This is the surface for an agent that already speaks MCP (Claude Code, OpenCode). A stdio-only client bridges to that URL.
- **REST** on `:8000` is the same session, for curl and scripts. `soup-to-nuts` and workspace clear exist only here.

Shell approval is the same gate on all three ([007](007-ApprovalGate.md)). One run at a time: a second call while a run is in progress fails (HTTP 409, or that same text as an MCP tool error).

## Consequences

- An MCP client drives plan and execute without learning the TUI. It still has to handle `pending_approval` and `running`.
- Scripts use REST and the same wait contract: the call returns `pending_approval`, `ok`, `error`, `denied`, or `running` after about 60 seconds, then the caller polls `status`.
- There is no auth. Compose publishes port 8000 on the host. Use it on localhost. Putting that port on a shared network exposes an unauthenticated shell gate.
- There is no second MCP server process and no HTTP `/v1/fix`. Recovery during execute stays inside the pipeline.
