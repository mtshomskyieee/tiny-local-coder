# TinyLocalCoder use cases (CLI)

Start the TUI from the repo root:

```bash
./cli.sh
```

Approve shell commands when the gate appears (**Allow** / **Deny** / **Allow all**). Use `/show-plan` anytime to inspect `workspace/plan.md`. Full design notes: [`Architecture.md`](Architecture.md).

---

## 1. Planning → create code

**Goal:** Turn a short request into a numbered plan, then create/run the code.

**CLI**

```text
/plan
create and run hello_world.py
```

When the plan is ready:

```text
/execute-plan
```

(or press **F5**)

**What happens**

1. `/plan` rewrites `workspace/plan.md` (Goal + `create` / `run` todos).
2. `/execute-plan` walks todos: coding agent writes files, then gated shell runs verifies.
3. Failures may auto-fix once, then auto-replan or auto-skip depending on toggles.

**Useful follow-ups**

- `/plan-edit` — full-screen edit of `plan.md` (Save applies standards; Undo if rewritten · Ctrl+S / Ctrl+U / Esc)  
- `/code` — next create/refine todo only  
- `/code show hello_world.py` — print a file  
- `/clear-plan` — wipe todos but keep code  

---

## 2. Fixing an issue

**Goal:** Repair the last failed execute step (or apply a hinted fix) without rewriting the whole product.

**CLI — after a failed `/execute-plan`**

```text
/fix
```

Then retry:

```text
/execute-plan
```

**CLI — with a hint**

```text
/fix create src/__init__.py
```

**What happens**

1. `/fix` reads the last failure from `exec.log`, applies heuristics and/or an LLM CREATE/WRITE.
2. Manual `/fix` does **not** auto-replan; re-run `/execute-plan` (with `/auto-replan on` if the todo itself looks wrong).
3. Skipped steps: `/reset-todo N` or `/reset-all-skipped`, then `/execute-plan` again.

**Useful toggles**

```text
/auto-fix on
/auto-skip on
/auto-replan on
```

Name the file when `/fix` has no last failure, or the last error is the wrong problem:

```text
/fix start_service.sh should import src.db and print routes
```

---

## Repairing the plan

**Goal:** Check the original requirement against `plan.md` and rewrite todos that miss files, endpoints, or start scripts — without executing yet.

**CLI**

```text
/fix-plan
```

Optional note (merged into the requirement):

```text
/fix-plan start_service.sh must start src/db.py
```

**What happens**

The TUI prints `fix-plan » N/3 …`.

1. **1/3** — load the requirement (your note, current Goal, last non-command session turn) and `plan.md`.
2. **2/3** — list structural gaps (stub `start_*.sh`, required endpoints missing from source, empty Goal).
3. **3/3** — `/plan` rewrites `plan.md`. The TUI prints the new todos. Nothing is executed.

Then run `/execute-plan` (or F5).

---

## 3. Reviewing the code

**Goal:** Inventory the workspace and produce review comments — without manually `/plan` then `/execute-plan`.

**CLI**

```text
/review
```

Optional focus:

```text
/review focus on src/
```

**What happens**

The TUI prints each stage as `review » N/4 …` so you can see where the workflow is.

1. **1/4** — a **tool** writes `manifest.txt` (filesystem walk — no LLM inventory). The TUI prints the file.
2. **2/4** — `/plan` runs with that inventory in the prompt (todos for `review.md`, not invented paths).
3. **3/4** — a **tool** reviews **each** manifest path and always writes `review.md` (does not depend on how the LLM worded the todos). The TUI prints the file.
4. **4/4** — `/execute-plan` runs any remaining todos; if execute wipes `review.md`, the tool restores it.
5. Artifacts: `workspace/manifest.txt`, `workspace/review.md`.

Inspect with `/show-plan` or `/code show review.md`. Typical next step: `/review-fix`.

---

## 4. Fixing from a review

**Goal:** Turn `review.md` findings into a fix plan and execute it — without manually `/plan` then `/execute-plan`.

**CLI**

```text
/review-fix
```

Optional focus:

```text
/review-fix only high
```

**What happens**

The TUI prints each stage as `review-fix » N/3 …`.

1. **1/3** — load `review.md` (write it from the manifest if missing/empty). High/medium/low findings are listed; info-only notes are skipped. The TUI prints `review.md`.
2. **2/3** — `/plan` writes refine/fix todos from those findings into `plan.md`. The TUI prints the plan.
3. **3/3** — `/execute-plan` runs that plan. If nothing is actionable, execute does not run (leftover todos from an earlier plan are left alone).

Inspect with `/show-plan`. After `/review-fix`, the TUI prints the findings and `plan.md` again.

---

## 5. Testing the code

**Goal:** Build a **runnable test plan** as `plan.md` and execute it in one command.

**CLI**

```text
/test
```

Optional note:

```text
/test only unit
```

**What happens**

The TUI prints each stage as `test » N/3 …`, then a line when each todo starts and finishes.

1. **1/3** — list existing `test_*.py` / `tests/` files (or note that a smoke test may be added).
2. **2/3** — `/plan` writes run (and optional create) todos. The TUI prints `plan.md` and each step.
3. **3/3** — `/execute-plan` walks those todos. Approve pytest/unittest at the gate. Each step logs `test » running …` then `test » done … [ok|failed]`.
4. Afterward the TUI reprints `plan.md` with `[x]` / `[!]` so you can see what completed.

If tests were skipped (`[!]`):

```text
/reset-all-skipped
/execute-plan
```
