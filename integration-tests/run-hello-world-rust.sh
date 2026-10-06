#!/usr/bin/env bash
# Unattended plan → execute check: Rust (hello-world.rs, compiled with rustc and run).
# See integration-tests/PLAN.md
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=integration-tests/lib.sh
source "$ROOT/integration-tests/lib.sh"

TEST_NAME="hello-world.rs"
PLAN_PROMPT='Write hello-world.rs in Rust that prints hello world, then compile it with rustc and run it.'

HELLO_FILE="$WORKSPACE/hello-world.rs"
HELLO_BIN="$WORKSPACE/hello-world"
TEST_ARTIFACTS=(hello-world.rs hello_world.rs hello-world hello_world main.rs main)

# The LLM's own wording varies too much to assert against, so the execute stage
# runs these fixed bytes. assert_plan_shape checks the real plan first.
CANONICAL_PLAN=$'# Plan\nGoal: Create hello-world.rs, compile it with rustc and run it\n\n## Todos\n1. [ ] create `hello-world.rs` — prints hello world\n2. [ ] run `rustc -o hello-world hello-world.rs` — expect success\n3. [ ] run `./hello-world` — expect hello\n'

# The planner must reach create → rustc → run from the toolchain registry.
# A missing rustc is an apt install at execute time; this hook only checks
# that the plan itself is a Rust build, not a Python or C one.
assert_plan_shape() {
  if ! grep -Eqi 'hello[-_]world\.rs' "$PLAN_FILE"; then
    fail "plan.md does not mention hello-world.rs"
    return 1
  fi
  pass "plan.md has numbered todos and mentions hello-world.rs"

  if ! grep -Eqi '^[0-9]+\. \[[ x!]\] run `(rustc|cargo)' "$PLAN_FILE"; then
    fail "plan.md has no rustc/cargo compile step"
    return 1
  fi
  if ! grep -Eqi '^[0-9]+\. \[[ x!]\] run `\./hello-world' "$PLAN_FILE"; then
    fail "plan.md has no ./hello-world run step"
    return 1
  fi
  pass "plan.md has a compile step and a run step of its own"

  if grep -Eqi '^[0-9]+\. \[[ x!]\] run `(gcc|g\+\+|python3|py_compile)' "$PLAN_FILE"; then
    fail "plan.md has a non-Rust build command"
    return 1
  fi
  pass "plan.md has no foreign build commands"
  return 0
}

assert_execute_result() {
  if [[ ! -s "$HELLO_FILE" ]]; then
    fail "hello-world.rs missing or empty after execute"
    dump_exec_log
    return 1
  fi
  if ! grep -Eqi 'fn[[:space:]]+main|println!|hello' "$HELLO_FILE"; then
    fail "hello-world.rs does not look like a Rust hello-world program"
    log "--- hello-world.rs ---"
    cat "$HELLO_FILE"
    log "----------------------"
    return 1
  fi
  pass "hello-world.rs exists and looks like a hello-world program"

  if [[ ! -x "$HELLO_BIN" ]]; then
    fail "hello-world binary was not produced — the rustc step did not run or did not succeed"
    dump_plan
    dump_exec_log
    return 1
  fi
  pass "hello-world binary was compiled"
  return 0
}

run_integration_test
