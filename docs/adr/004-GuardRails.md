# ADR 004: Unit tests always, integration tests when the path changes

Status: Accepted

## Context

Unit tests lock the Python that shapes a plan. They never start Docker or the model, so a green unit run can still hide a broken `/plan` then `/execute-plan`: wrong compile line, missing binary, skipped todo. The path that produces a real program needs a real container, a real compiler, and the approval gate.

## Decision

Two layers, two duties:

- **Unit tests** in [`tests/`](../../tests/) run on every change. They cover plan finalize, toolchains, workflows, provision, and the rest of the deterministic Python. [`./run-tests.sh`](../../run-tests.sh) bootstraps a venv and runs pytest. pytest stays out of the app image and out of `requirements.txt`.
- **Integration tests** in [`integration-tests/`](../../integration-tests/) run when a change touches a language in the toolchain table or the plan-then-execute path. Each runner (C, Python, Rust today) drives the HTTP API: it checks the model's own plan shape, substitutes the canonical `plan.md`, then executes. The shared harness in `lib.sh` requires every todo to end `[x]`, with no `[!]` and no junk run command. The harness approves parked commands with `allow_all` so the run can finish unattended. That approval is part of the test. The product default remains the gate in [007](007-ApprovalGate.md). The contract is [integration-tests/PLAN.md](../../integration-tests/PLAN.md).

These runners stay outside `./run-tests.sh` and GitHub unit CI. They need Docker, Ollama, on the order of 12 GB RAM, and a long runtime. Run one at a time; each owns `./workspace` and the compose suite until it exits. The author of a language or execute-path change runs the matching runner and keeps it passing. Adding a language includes a new runner of about 60 lines.

## Consequences

- Finish a normal Python change with `./run-tests.sh`.
- Finish a toolchain or execute-path change only after the matching integration runner passes. A failure means fix the product, or update the canonical plan and the assertions together and say why.
- Integration coverage is the languages that have a runner. A registered language without a runner is unguarded on the live path.
