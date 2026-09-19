#!/usr/bin/env bash
# Unattended plan → execute check: hello-world.c
# See integration-tests/PLAN.md
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

API="${TLC_API:-http://localhost:8000}"
HEALTH_TIMEOUT_SEC="${TLC_HEALTH_TIMEOUT_SEC:-300}"
PLAN_TIMEOUT_SEC="${TLC_PLAN_TIMEOUT_SEC:-600}"
EXECUTE_TIMEOUT_SEC="${TLC_EXECUTE_TIMEOUT_SEC:-1200}"

PLAN_PROMPT='Create only hello-world.c that prints hello world. One create todo. Do not compile, link, run gcc, or add any run or test todos. Coding only — stop after the source file exists.'
WORKSPACE="$ROOT/workspace"
PLAN_FILE="$WORKSPACE/plan.md"
HELLO_FILE="$WORKSPACE/hello-world.c"
EMPTY_PLAN=$'# Plan\n\nGoal:\n\n## Todos\n'
# Same plan.md every run after the LLM produces any hello-world create todo.
CANONICAL_PLAN=$'# Plan\nGoal: Create hello-world.c that prints hello world (coding only; no compile or run)\n\n## Todos\n1. [ ] create `hello-world.c` — prints hello world\n'

FAILED=0
FAILURES=()
STARTED=0
CLEANED=0
BACKUP_DIR=""

log() { printf '%s\n' "$*"; }

fail() {
  FAILED=1
  FAILURES+=("$*")
  log "FAIL: $*"
}

pass() { log "PASS: $*"; }

json_field() {
  local json="$1"
  local field="$2"
  python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    sys.exit(2)
val = data
for part in sys.argv[1].split("."):
    if isinstance(val, dict) and part in val:
        val = val[part]
    else:
        sys.exit(1)
if val is None:
    sys.exit(1)
if isinstance(val, bool):
    print("true" if val else "false")
else:
    print(val)
' "$field" <<<"$json"
}

API_HTTP_CODE=0
LAST_BODY=""

# Do not wrap these in $(...); command substitution is a subshell and would
# drop API_HTTP_CODE. Callers read LAST_BODY / API_HTTP_CODE after return.
api_get() {
  local tmp
  tmp="$(mktemp)"
  API_HTTP_CODE="$(curl -sS --max-time 30 -o "$tmp" -w '%{http_code}' "$1" || printf '000')"
  LAST_BODY="$(cat "$tmp")"
  rm -f "$tmp"
}

api_post() {
  local path="$1"
  local req="$2"
  local tmp
  tmp="$(mktemp)"
  API_HTTP_CODE="$(curl -sS --max-time 90 \
    -o "$tmp" -w '%{http_code}' \
    -H "Content-Type: application/json" \
    -d "$req" \
    "${API}${path}" || printf '000')"
  LAST_BODY="$(cat "$tmp")"
  rm -f "$tmp"
}

run_finished() {
  case "$1" in
    ok|error|denied|failed) return 0 ;;
    *) return 1 ;;
  esac
}

cleanup() {
  local rc=$?
  if [[ "$CLEANED" -eq 1 ]]; then
    return 0
  fi
  CLEANED=1
  if [[ "$STARTED" -eq 1 ]]; then
    log "Stopping suite…"
    "$ROOT/stop-service.sh" || true
  fi
  if [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]]; then
    local archive_keep=""
    if [[ -d "$WORKSPACE/archive" ]]; then
      archive_keep="$(mktemp -d "${TMPDIR:-/tmp}/tlc-integ-archive.XXXXXX")"
      cp -a "$WORKSPACE/archive"/. "$archive_keep"/
    fi
    log "Restoring workspace…"
    rm -rf "$WORKSPACE"
    mkdir -p "$WORKSPACE"
    # Backup may be empty if workspace was empty.
    if [[ -n "$(ls -A "$BACKUP_DIR" 2>/dev/null || true)" ]]; then
      cp -a "$BACKUP_DIR"/. "$WORKSPACE"/
    fi
    if [[ -n "$archive_keep" ]]; then
      mkdir -p "$WORKSPACE/archive"
      cp -a "$archive_keep"/. "$WORKSPACE/archive"/
      rm -rf "$archive_keep"
    fi
    rm -rf "$BACKUP_DIR"
  fi
  if [[ "$FAILED" -ne 0 ]]; then
    log ""
    log "=== Integration test FAILED ==="
    local item
    for item in "${FAILURES[@]+"${FAILURES[@]}"}"; do
      log "  - $item"
    done
    exit 1
  fi
  if [[ "$rc" -ne 0 ]]; then
    log "=== Integration test FAILED (command exit $rc) ==="
    exit 1
  fi
  log ""
  log "=== Integration test PASSED ==="
  exit 0
}

trap cleanup EXIT

backup_workspace() {
  BACKUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tlc-integ-workspace.XXXXXX")"
  mkdir -p "$WORKSPACE"
  if [[ -n "$(ls -A "$WORKSPACE" 2>/dev/null || true)" ]]; then
    cp -a "$WORKSPACE"/. "$BACKUP_DIR"/
  fi
  reset_test_artifacts
}

reset_test_artifacts() {
  mkdir -p "$WORKSPACE"
  printf '%s' "$EMPTY_PLAN" >"$PLAN_FILE"
  rm -f "$HELLO_FILE" \
    "$WORKSPACE/hello_world.c" \
    "$WORKSPACE/hello-world" \
    "$WORKSPACE/hello_world" \
    "$WORKSPACE/a.out"
}

wait_healthy() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT_SEC))
  local body
  log "Waiting for $API/health (ollama ready, ${HEALTH_TIMEOUT_SEC}s)…"
  while ((SECONDS < deadline)); do
    api_get "$API/health"
    body="$LAST_BODY"
    if [[ "$API_HTTP_CODE" == "200" ]]; then
      if [[ "$(json_field "$body" "ollama" 2>/dev/null || true)" == "true" ]]; then
        pass "API health: ollama ready"
        return 0
      fi
    fi
    sleep 3
  done
  fail "API did not become healthy with ollama ready within ${HEALTH_TIMEOUT_SEC}s"
  return 1
}

plan_has_todos() {
  [[ -f "$PLAN_FILE" ]] || return 1
  grep -Eq '^[0-9]+\. \[[ x!]\]' "$PLAN_FILE"
}

plan_mentions_hello() {
  [[ -f "$PLAN_FILE" ]] || return 1
  grep -Eqi 'hello[-_]world\.c' "$PLAN_FILE"
}

plan_has_run_todos() {
  [[ -f "$PLAN_FILE" ]] || return 1
  grep -Eqi '^[0-9]+\. \[[ x!]\] (run|test) ' "$PLAN_FILE" \
    || grep -Eqi '^[0-9]+\. \[[ x!]\] .*(gcc|clang|compile|\./hello-world)' "$PLAN_FILE"
}

# Fixed execute input every run (LLM wording / compile todos must not leak).
write_canonical_plan() {
  printf '%s' "$CANONICAL_PLAN" >"$PLAN_FILE"
}

wait_for_plan() {
  local deadline=$((SECONDS + PLAN_TIMEOUT_SEC))
  local body status
  log "Planning: $PLAN_PROMPT"
  api_post "/v1/plan" "$(python3 -c '
import json, sys
print(json.dumps({"prompt": sys.argv[1], "thinking_enabled": False}))
' "$PLAN_PROMPT")"
  body="$LAST_BODY"
  if [[ "$API_HTTP_CODE" != "200" ]]; then
    fail "POST /v1/plan HTTP $API_HTTP_CODE: $body"
    return 1
  fi
  status="$(json_field "$body" "status" 2>/dev/null || true)"
  log "POST /v1/plan status=$status"

  while ((SECONDS < deadline)); do
    if [[ "$status" == "error" ]]; then
      fail "plan run error: $(json_field "$body" "error" 2>/dev/null || echo "$body")"
      return 1
    fi
    if run_finished "$status"; then
      if ! plan_has_todos || ! plan_mentions_hello; then
        fail "plan finished ($status) but plan.md has no hello-world create todo"
        return 1
      fi
      pass "plan.md has numbered todos and mentions hello-world.c"
      log "Writing canonical create-only plan (same bytes every run)…"
      write_canonical_plan
      rm -f "$HELLO_FILE" "$WORKSPACE/hello_world.c"
      if plan_has_run_todos; then
        fail "plan.md still has compile/run todos after create-only rewrite"
        return 1
      fi
      if ! plan_has_todos || ! plan_mentions_hello; then
        fail "canonical plan lost the hello-world.c create todo"
        return 1
      fi
      pass "plan.md is the canonical create-only plan"
      return 0
    fi
    api_get "$API/v1/status"
    body="$LAST_BODY"
    status="$(json_field "$body" "status" 2>/dev/null || true)"
    sleep 2
  done

  fail "plan still $status after ${PLAN_TIMEOUT_SEC}s (need ok/error before execute)"
  return 1
}

hello_file_ok() {
  [[ -s "$HELLO_FILE" ]] || return 1
  grep -Eqi 'hello|printf|puts' "$HELLO_FILE"
}

wait_for_execute() {
  local deadline=$((SECONDS + EXECUTE_TIMEOUT_SEC))
  local body status command_id last_id=""
  log "Executing plan…"
  api_post "/v1/execute" '{"prompt":"","thinking_enabled":false}'
  body="$LAST_BODY"
  if [[ "$API_HTTP_CODE" == "409" ]]; then
    fail "POST /v1/execute HTTP 409 (plan still running?): $body"
    return 1
  fi
  if [[ "$API_HTTP_CODE" != "200" ]]; then
    fail "POST /v1/execute HTTP $API_HTTP_CODE: $body"
    return 1
  fi
  status="$(json_field "$body" "status" 2>/dev/null || true)"
  log "POST /v1/execute status=$status"

  while ((SECONDS < deadline)); do
    if [[ "$status" == "pending_approval" ]]; then
      command_id="$(json_field "$body" "pending_approval.command_id" 2>/dev/null || true)"
      if [[ -n "$command_id" && "$command_id" != "$last_id" ]]; then
        local cmd
        cmd="$(json_field "$body" "pending_approval.command" 2>/dev/null || true)"
        if echo "$cmd" | grep -Eqi 'gcc|clang|compile|\./hello-world'; then
          log "Denying compile/run gate: $cmd"
          api_post "/v1/execute/approve" "$(python3 -c '
import json, sys
print(json.dumps({"command_id": sys.argv[1], "decision": "deny"}))
' "$command_id")"
          body="$LAST_BODY"
          if [[ "$API_HTTP_CODE" != "200" ]]; then
            fail "POST /v1/execute/approve (deny compile) HTTP $API_HTTP_CODE"
            return 1
          fi
        else
          log "Approving command $command_id (allow_all): $cmd"
          api_post "/v1/execute/approve" "$(python3 -c '
import json, sys
print(json.dumps({"command_id": sys.argv[1], "decision": "allow_all"}))
' "$command_id")"
          body="$LAST_BODY"
          if [[ "$API_HTTP_CODE" != "200" ]]; then
            fail "POST /v1/execute/approve HTTP $API_HTTP_CODE"
            return 1
          fi
        fi
        last_id="$command_id"
        status="$(json_field "$body" "status" 2>/dev/null || true)"
        continue
      fi
    fi
    if [[ "$status" == "error" ]]; then
      fail "execute run error: $(json_field "$body" "error" 2>/dev/null || echo "$body")"
      return 1
    fi
    if run_finished "$status"; then
      break
    fi
    api_get "$API/v1/status"
    body="$LAST_BODY"
    status="$(json_field "$body" "status" 2>/dev/null || true)"
    sleep 2
  done

  if ! run_finished "$status"; then
    fail "execute still $status after ${EXECUTE_TIMEOUT_SEC}s (not clearing while coder may write)"
    return 1
  fi

  if hello_file_ok; then
    pass "hello-world.c exists and looks like a hello-world program"
    return 0
  fi
  if [[ ! -s "$HELLO_FILE" ]]; then
    fail "hello-world.c missing or empty after execute (${EXECUTE_TIMEOUT_SEC}s, last status=$status)"
  else
    fail "hello-world.c does not contain hello / printf / puts"
  fi
  return 1
}

clear_workspace_via_api() {
  local body status archive
  log "POST /v1/workspace/clear (/clear-workspace)"
  api_post "/v1/workspace/clear" '{}'
  body="$LAST_BODY"
  if [[ "$API_HTTP_CODE" == "409" ]]; then
    fail "POST /v1/workspace/clear HTTP 409 (run still in progress): $body"
    return 1
  fi
  if [[ "$API_HTTP_CODE" != "200" ]]; then
    fail "POST /v1/workspace/clear HTTP $API_HTTP_CODE: $body"
    return 1
  fi
  status="$(json_field "$body" "status" 2>/dev/null || true)"
  archive="$(json_field "$body" "archive" 2>/dev/null || true)"
  if [[ "$status" != "ok" ]]; then
    fail "/clear-workspace failed: $body"
    return 1
  fi
  if [[ -s "$HELLO_FILE" ]]; then
    fail "/clear-workspace left hello-world.c in the live workspace"
    return 1
  fi
  pass "/clear-workspace archived to ${archive:-archive/}"
  return 0
}

log "=== TinyLocalCoder integration: plan → execute (hello-world.c) ==="
backup_workspace
log "Workspace backed up; artifacts reset"

# Autofix / replan will invent compile+run todos for C; this test is coding-only.
export AUTO_FIX=false
export AUTO_REPLAN=false
log "Stopping any leftover suite so this run starts clean…"
"$ROOT/stop-service.sh" || true
log "Starting suite (./start-service.sh --no-prompt, AUTO_FIX=false AUTO_REPLAN=false)…"
if ! "$ROOT/start-service.sh" --no-prompt; then
  fail "start-service.sh --no-prompt failed"
else
  STARTED=1
  pass "service start requested"
  wait_healthy || true
fi

if [[ "$FAILED" -eq 0 ]]; then
  wait_for_plan || true
fi
if [[ "$FAILED" -eq 0 ]]; then
  wait_for_execute || true
fi
if [[ "$STARTED" -eq 1 ]]; then
  clear_workspace_via_api || true
fi

if [[ "$FAILED" -eq 0 ]]; then
  pass "all checks"
fi
# cleanup trap prints summary and exits 0/1
