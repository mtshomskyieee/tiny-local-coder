# Tiny Local Coder: a coding agent that fits in a small box

Most coding agents assume a large model, a large context window, and a network path to somewhere else’s GPUs. Tiny Local Coder (TLC) starts from the opposite bet: a 3B model, about 2048 tokens of context, CPU-only inference through Ollama, and no API key. Nothing leaves your machine.

That sounds like a handicap. It is — if you treat the model as the whole product. TLC is not a smaller Copilot/Claude. It is a system that moves memory and structure *off* the model and onto disk and plain Python, so a tiny brain can still do useful work one step at a time.

## Designing around low memory usage

A 3B model at a short context length cannot see your whole plan, your whole tree, or a long chat history. Pretending otherwise just produces confident garbage. TLC leans into the limit:

- **Disk is long-term memory.** The goal and todos live in `workspace/plan.md`. Generated code lands under `workspace/`. Command results go to `exec.log`. Q&A accumulates in `ask.md`. Between agent turns, the shared state is files — not a ballooning message list.
- **Each LLM call sees only the current todo.** Plus tiny slices: a goal line, a few file names, a failed step. Never the whole plan.
- **Deterministic tools own the structure.** After planning, Python validates and rewrites the todo list: strips leaked meta-commands, caps verify steps, drops junk run targets. Review and inventory are filesystem walks and heuristics with no model in the loop. Path fixes try the obvious repair before asking the LLM for a scrap of text.
- **The shell never runs unapproved.** Every command stops at a gate: Allow, Deny, or Allow all for the rest of the session.

“More rigorous” here means harder-to-skip checks and shorter plans — not longer prompts.

## A sample afternoon: hello world

Assume Docker is up and you’ve started the suite once (first boot pulls the Ollama model into a volume that survives later stops). From the repo root you launch the TUI:

```bash
./cli.sh
```

You enter plan mode and describe something small:

```text
/plan
create and run hello_world.py
```

TLC rewrites `workspace/plan.md` into a short numbered list: a create step or two, then one compile check, then one short smoke run. The shape is intentional. After the model drafts a plan, Python normalizes it — so you get a few concrete steps and tight verifies, not a novel of aspirational checkboxes. You can open the result with `/show-plan` or edit it by hand with `/plan-edit`. Prefer a handful of todos: enough to be honest, not so many that a tiny model drowns.

When the list looks right, you run it:

```text
/execute-plan
```

The agent walks the todos one at a time. For a create step it writes a file under the workspace. For a run step it proposes a shell command — and then everything stops. You see the command and choose Allow, Deny, or Allow all. Nothing executes behind your back. That pause is not ceremony; on a local agent it is the difference between “helpful” and “surprising.”

If a verify step fails, TLC climbs a short recovery ladder: skip obvious junk commands immediately; try one code repair and retry; if the *todo itself* looks wrong, rewrite the remaining open todos once; otherwise mark the step skipped and continue. You can reopen a skipped item later and run the plan again. The defaults are conservative on purpose — one fix, one replan, then move on — because a small model looping forever is worse than an honest `[!]`.

When you finish, the interesting artifact is not a chat transcript. It is the workspace: checkboxes in `plan.md`, code on disk, and a log of what you approved. That is the point of file-backed agents — the session survives because the memory was never trapped in the model’s context window.

For fixing, reviewing, and inventing a tiny test plan, the CLI walks in [use-cases.md](use-cases.md) go further without changing the same idea: plan on disk, execute one step, approve the shell.

## When to use TLC — and when not to

TLC sits in a crowded field. The useful question is not “which is best?” but “what are you optimizing for?”

### Cloud IDE agents and copilots

Tools like Cursor, GitHub Copilot, Claude Code, and Devin-class agents are built around capable cloud models, large context, and deep editor integration. Reach for them when you want speed on multi-file refactors, strong reasoning over a big repo, and you are comfortable with cloud models and whatever that costs in money and data leaving the box. They optimize for “get the change done” with a frontier brain in the loop.

Prefer TLC when those tradeoffs go the other way: code or prompts must stay local; you have no GPU and a modest RAM budget; you want a hard cost floor (your electricity, not tokens); or you care about *seeing* how an agent is constrained — plan file, approval gate, recovery ladder — more than raw model horsepower. TLC will not out-reason a cloud agent on a sprawling codebase. It will stay on your machine and refuse to invent structure it cannot maintain.

### Local and open coding tools

Aider, Continue, Open Interpreter–style tools, and similar projects shine when you already have a strong local or remote model and you want chat-driven edits or repo-aware assist inside a familiar workflow. They are excellent companions to a model that can actually hold a large problem in its head — and many of them are happier the stronger that model gets.

Prefer TLC when the model is *tiny* — or must stay tiny — and you need the scaffolding that makes that viable: an explicit plan on disk, todos walked one by one, structure enforced in Python, and a human on every shell command. If your local setup assumes a much larger model and a long context, TLC’s discipline may feel redundant. If you are stuck on a 3B CPU box, that discipline is the product: the interesting engineering is around the model, not inside it.

### A short checklist

**Reach for TLC when:**

- Privacy or air-gap matters more than frontier competence
- You are on CPU-only hardware and a small Ollama model
- You want human-in-the-loop shell with a clear plan you can edit
- You want a teachable, inspectable agent architecture (disk memory, short prompts, tools first)

**Reach for something else when:**

- You need large greenfield refactors or deep multi-file reasoning today
- You depend on heavy test frameworks and long-running verify suites as the default path
- You need the model to “just figure it out” across a wide context window

Those are not moral rankings. They are different machines for different constraints.

## Small modern models are impactful early

Shrinking context and leaning on one-shot prompts for code early is not a downgrade, it is how you get something real on disk to refine. Compared with sprawling agent loops in larger systems (Claude, Codex, etc.), there is less haphazard “think until it works” behavior; in its place sits a rigid workflow that guarantees code you can iterate on. Simpler prompting, memory on disk, one step in view, tools for structure, and a person at the shell to verify items can be run are methods that enable our small context window to be more.

That is Tiny Local Coder. If you want the internals constraints, recovery ladder, and why plans stay short, then start with [Architecture.md](Architecture.md). For hands-on CLI flows, see [use-cases.md](use-cases.md). For how the LangGraph nodes wire together, see [orchestration.md](orchestration.md).
