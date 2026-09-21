#!/usr/bin/env bash
# Unattended plan → execute check: Python (greet.py, byte-compiled and imported).
# The mirror of run-hello-world-c.sh — the toolchain registry must not have
# regressed the Python path it was factored out of.
# See integration-tests/PLAN.md
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck source=integration-tests/lib.sh
source "$ROOT/integration-tests/lib.sh"

TEST_NAME="greet.py"
PLAN_PROMPT='Write greet.py in Python with a greet(name) function that returns a greeting string, then byte-compile it and import it to check it works.'

GREET_FILE="$WORKSPACE/greet.py"
PYCACHE_DIR="$WORKSPACE/__pycache__"
TEST_ARTIFACTS=(greet.py __pycache__ greet.pyc)

CANONICAL_PLAN=$'# Plan\nGoal: Create greet.py with a greet(name) function, byte-compile it and import it\n\n## Todos\n1. [ ] create `greet.py` — defines greet(name) returning a greeting containing the name\n2. [ ] run `python3 -m py_compile greet.py` — expect success\n3. [ ] run `python3 -c "from greet import greet; print(greet(\'world\'))"` — expect world\n'

# The Python shape must be exactly what it always was: create, ONE batched
# py_compile, ONE short behavioural `python3 -c`. The registry made the
# augmentation language-driven, and this is the guard that it still picks the
# Python builders for a Python plan.
assert_plan_shape() {
  if ! grep -Eqi 'greet\.py' "$PLAN_FILE"; then
    fail "plan.md does not mention greet.py"
    return 1
  fi
  pass "plan.md has numbered todos and mentions greet.py"

  if ! grep -Eq '^[0-9]+\. \[[ x!]\] run `[^`]*py_compile' "$PLAN_FILE"; then
    fail "plan.md has no py_compile step"
    return 1
  fi
  if ! grep -Eq '^[0-9]+\. \[[ x!]\] run `[^`]*python3 -c' "$PLAN_FILE"; then
    fail "plan.md has no python3 -c behavioural step"
    return 1
  fi
  pass "plan.md has a py_compile step and a python3 -c step of its own"

  # A Python plan must never be handed another language's build command.
  if grep -Eqi '^[0-9]+\. \[[ x!]\] run `(gcc|g\+\+|make|cargo|go|rustc|javac) ' "$PLAN_FILE"; then
    fail "plan.md has a non-Python build command"
    return 1
  fi
  pass "plan.md has no foreign build commands"

  if grep -q 'apt-get' "$PLAN_FILE"; then
    fail "plan.md wants to install a toolchain — python3 is already present"
    return 1
  fi
  pass "no install step — the Python toolchain is already present"
  return 0
}

assert_execute_result() {
  if [[ ! -s "$GREET_FILE" ]]; then
    fail "greet.py missing or empty after execute"
    dump_exec_log
    return 1
  fi
  if ! grep -Eq 'def[[:space:]]+greet' "$GREET_FILE"; then
    fail "greet.py does not define a greet() function"
    log "--- greet.py ---"
    cat "$GREET_FILE"
    log "----------------"
    return 1
  fi
  pass "greet.py exists and defines greet()"

  # __pycache__ is the Python equivalent of the C test's compiled binary:
  # proof the compile step actually ran rather than being skipped.
  if [[ ! -d "$PYCACHE_DIR" ]] || ! compgen -G "$PYCACHE_DIR/greet.*.pyc" >/dev/null; then
    fail "no __pycache__/greet.*.pyc — the py_compile step did not run or did not succeed"
    dump_plan
    dump_exec_log
    return 1
  fi
  pass "greet.py was byte-compiled"

  # The behavioural step must have actually imported and called greet().
  if ! grep -q 'from greet import greet' "$WORKSPACE/exec.log" 2>/dev/null; then
    fail "exec.log has no record of the import check running"
    dump_exec_log
    return 1
  fi
  pass "the import check ran"
  return 0
}

run_integration_test
