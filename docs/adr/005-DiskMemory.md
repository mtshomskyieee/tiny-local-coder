# ADR 005: Disk is the memory

Status: Accepted

## Context

A small model cannot see the whole plan, the whole tree, or a long message list in one call. If the goal and the todos lived only in that call, a restart would lose them and the next call would repeat work. LangGraph still carries a message channel. The session that survives a call is the workspace.

## Decision

Nodes share state through files under `workspace/`, owned by [`memory/files.py`](../../src/tinylocalcoder/memory/files.py).

| File | Role |
| --- | --- |
| `plan.md` | Goal and numbered todos (`[ ]` open, `[x]` done, `[!]` skipped) |
| sources the plan names | Generated code, at the workspace root or under a path the plan gives |
| `exec.log` | Gated command results |
| `ask.md` | Q&A transcript |
| `session.md` | Plan after `/compaction` |
| `manifest.txt`, `review.md` | Inventory and per-file notes from `/review` |
| `.index/` | Chunk indexes for retrieval |

Each model call sees the current todo plus a small slice: a goal line, file names, one failed todo, a short snippet. Raising `NUM_CTX` enlarges that slice. The call still does not receive the whole plan.

`workspace/archive/` holds past sessions. Manifest, review, list/read tools, and the chunk indexer skip it. The indexer skip is load-bearing: indexing an archived `.index/prototypes.json` squares the index on every clear-workspace cycle.

## Consequences

- The artifact of a session is the workspace. A restart continues from `plan.md`.
- A prompt that pastes the whole plan, the whole tree, or the chat so far breaks this decision, including when the context window could fit them.
- A caller that needs the plan reads `plan.md` (`read_file` or `GET /v1/workspace/file`).
- Archive is history. Tools that list or read the live workspace omit it.
