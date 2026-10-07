# ADR 006: Python owns structure

Status: Accepted

## Context

Asked to manage a plan, a small model leaks meta commands, invents junk run lines, and loops when a command fails. Those are structural jobs. Python can do them the same way every time. The model is for a short rewrite when the text itself has to change.

## Decision

Deterministic Python owns structure. The model rewrites a small scrap of text, and Python decides whether to keep it.

Invariants, enforced in code (the current steps are in [Architecture](../Architecture.md)):

- [`finalize_plan()`](../../src/tinylocalcoder/memory/files.py) normalizes every plan before it is stored: strip leaked meta commands, drop run lines whose first token is not a registered tool, dedupe, cap the list. Open run steps stay at most one provision, one compile, and one behavioral run, in that order. A missing compile or smoke step is filled from the toolchain registry.
- Inventory and review ([`tools/manifest.py`](../../src/tinylocalcoder/tools/manifest.py), [`tools/review.py`](../../src/tinylocalcoder/tools/review.py)) are filesystem walks. No model call.
- The plan prompt includes one language's example, chosen from the registry ([002](002-ContainerFirst.md)). Python-only rules are omitted for other languages.
- On a failed run todo the ladder is bounded. Classify first (junk, environment, or code). Junk is skipped immediately. Environment installs through the gate and retries. Code gets one fix and one retry. A plan-smelling failure gets one replan of the remaining open todos. Otherwise the step is marked `[!]` and execution continues. A path or import mismatch is rewritten with no model call. When replan does call the model, it asks for one or two open lines; Python assembles Goal, Done, and Todos.

"More rigorous" means a check that is harder to skip, not a longer plan.

Compound workflows (`/review`, `/test`, and the rest in [`agents/workflows.py`](../../src/tinylocalcoder/agents/workflows.py)) are two invokes of the existing plan and execute paths. They are not new graph nodes.

## Consequences

- A model draft is not stored as written. `plan.md` is whatever `finalize_plan()` accepted.
- A new check belongs in a tool or in `finalize_plan()`, not in a longer prompt or an extra critic round.
- Repair inside one step stops after the ladder above. A skip is the outcome when that budget is spent.
