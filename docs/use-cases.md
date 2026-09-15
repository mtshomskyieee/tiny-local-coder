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

- `/plan-edit` — full-screen edit of `plan.md` (Save / Cancel · Ctrl+S / Esc)  
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

1. Fixed review prompt drives `/plan` (expect todos for `manifest.txt` and `review.md`).
2. A **tool** writes `manifest.txt` (filesystem walk — no LLM inventory).
3. A **tool** writes `review.md` (walks each manifest path; opinionated heuristic findings).
4. Immediately runs `/execute-plan` on any remaining todos (usually none for coding).
5. Artifacts: `workspace/manifest.txt`, `workspace/review.md`.

Inspect with `/show-plan` or `/code show review.md`. After `/review`, the TUI prints `review.md` automatically.

---

## 4. Testing the code

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

1. Fixed test prompt drives `/plan` — find existing tests (or add a tiny smoke test) and add `run` todos (`pytest` / `unittest`).
2. That plan **is** `plan.md`; `/execute-plan` runs immediately.
3. Approve test commands at the gate; check `exec.log` and the TUI log for results.

If tests were skipped (`[!]`):

```text
/reset-all-skipped
/execute-plan
```
