# Guide for contributors

People and coding agents follow the same rules. Read this file, then the accepted records in this directory, before changing the product.

## License

TinyLocalCoder is MIT licensed. Copyright (c) 2026 Michael Shomsky.

The text that controls is [LICENSE](../../LICENSE): you may use, copy, modify, merge, publish, distribute, sublicense, and sell copies, provided the copyright notice and the permission notice stay with those copies. The software is provided as is, without warranty.

## Before you change anything

- [Architecture](../Architecture.md) — the constraint and the component map
- [Orchestration](../orchestration.md) — nodes and edges
- [Use cases](../use-cases.md) — CLI walks
- [llm.txt](../llm.txt) — REST and MCP contract, when you are calling the running service
- ADRs [001](001-Architecture.md) through [007](007-ApprovalGate.md)

Match the accepted records. A new decision is a new file here (`008-ShortName.md`) with Status, Context, Decision, and Consequences. Start it as Proposed. Mark it Accepted when the change lands.

## What a change has to respect

Each record owns one rule. Follow the record; do not re-implement a second copy of it in a prompt or a new module.

| Rule | Record |
| --- | --- |
| Small local model, containerized, local MCP | [001](001-Architecture.md) |
| Linux and apt supply compilers | [002](002-ContainerFirst.md) |
| CLI, MCP, and REST share one session | [003](003-Interoperability.md) |
| Unit tests on every change; integration tests when plan-then-execute changes | [004](004-GuardRails.md) |
| `workspace/` files are the memory | [005](005-DiskMemory.md) |
| Python owns plan shape; the model rewrites a scrap of text | [006](006-DeterministicStructure.md) |
| Shell commands stop at the approval gate | [007](007-ApprovalGate.md) |

Code style that is not a product decision: modules start with `from __future__ import annotations`, and settings come from `config.py`.

## Tests

- Unit tests live in [`tests/`](../../tests/) and do not call the model. Run [`./run-tests.sh`](../../run-tests.sh) before you finish. pytest is not an app dependency.
- When the change touches a language table entry or the plan-then-execute path, also run the matching runner under [`integration-tests/`](../../integration-tests/). See [ADR 004](004-GuardRails.md).

## Commits

Imperative subject line. A body, when you need one, says why.

The human author is the only author of record. Commit messages and pull request descriptions carry no `Co-Authored-By` trailer for an AI tool and no "Generated with" footer.

Do not commit secrets (`.env`, credentials). `workspace/` stays untracked.

## For coding agents

- Treat this guide and the ADRs as constraints on the patch, not as background color.
- Keep prompts narrow ([005](005-DiskMemory.md), [006](006-DeterministicStructure.md)). A larger context window is not a reason to paste the whole plan into a prompt.
- Add a language as one `Toolchain` entry ([002](002-ContainerFirst.md)), plus an integration runner when you want that language guarded ([004](004-GuardRails.md)).
- Leave shell execution on the approval gate ([007](007-ApprovalGate.md)).
- Run `./run-tests.sh` and report the result.
