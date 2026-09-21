#!/usr/bin/env bash
# Unattended plan → execute check: C (hello-world.c, compiled and run).
# See integration-tests/PLAN.md
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=integration-tests/lib.sh
source "$ROOT/integration-tests/lib.sh"

TEST_NAME="hello-world.c"
PLAN_PROMPT='Write hello-world.c in C that prints hello world, then compile it with gcc and run it.'

HELLO_FILE="$WORKSPACE/hello-world.c"
HELLO_BIN="$WORKSPACE/hello-world"
TEST_ARTIFACTS=(hello-world.c hello_world.c hello-world hello_world a.out)

# The LLM's own wording varies too much to assert against, so the execute stage
# runs these fixed bytes. assert_plan_shape checks the real plan first.
CANONICAL_PLAN=$'# Plan\nGoal: Create hello-world.c, compile it with gcc and run it\n\n## Todos\n1. [ ] create `hello-world.c` — prints hello world\n2. [ ] run `gcc -Wall -std=c11 -o hello-world hello-world.c` — expect success\n3. [ ] run `./hello-world` — expect hello\n'

# The planner must reach create → compile → run unaided. Before the toolchain
# registry it could not: `gcc …` was not in the run-command allowlist, so
# finalize_plan dropped every build todo and the plan ended at the create step.
assert_plan_shape() {
  if ! grep -Eqi 'hello[-_]world\.c' "$PLAN_FILE"; then
    fail "plan.md does not mention hello-world.c"
    return 1
  fi
  pass "plan.md has numbered todos and mentions hello-world.c"

  if ! grep -Eqi '^[0-9]+\. \[[ x!]\] run `(gcc|cc|clang|make)' "$PLAN_FILE"; then
    fail "plan.md has no gcc/make compile step"
    return 1
  fi
  if ! grep -Eqi '^[0-9]+\. \[[ x!]\] run `\./hello-world' "$PLAN_FILE"; then
    fail "plan.md has no ./hello-world run step"
    return 1
  fi
  pass "plan.md has a compile step and a run step of its own"
  return 0
}

assert_execute_result() {
  if [[ ! -s "$HELLO_FILE" ]]; then
    fail "hello-world.c missing or empty after execute"
    dump_exec_log
    return 1
  fi
  if ! grep -Eqi 'hello|printf|puts' "$HELLO_FILE"; then
    fail "hello-world.c does not contain hello / printf / puts"
    return 1
  fi
  pass "hello-world.c exists and looks like a hello-world program"

  if [[ ! -x "$HELLO_BIN" ]]; then
    fail "hello-world binary was not produced — the compile step did not run or did not succeed"
    dump_plan
    dump_exec_log
    return 1
  fi
  pass "hello-world binary was compiled"
  return 0
}

run_integration_test
