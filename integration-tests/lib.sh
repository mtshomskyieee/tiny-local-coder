#!/usr/bin/env bash
# Shared harness for the unattended plan → execute integration tests.
# See integration-tests/PLAN.md.
#
# A test script sources this file, sets the configuration variables below, and
# calls `run_integration_test`. Everything else — service lifecycle, workspace
# backup/restore, API polling, the approval gate, the summary and exit code —
# lives here so each test is only its own assertions.
#
# Required before calling run_integration_test:
#   TEST_NAME        human-readable name for the banner
#   PLAN_PROMPT      the /v1/plan prompt
#   CANONICAL_PLAN   fixed plan.md bytes substituted before execute, so the
#                    execute stage is identical on every run
#   TEST_ARTIFACTS   array of workspace-relative paths to delete before the run
#   assert_plan_shape      hook: 0 if the LLM's own plan has the right shape
#   assert_execute_result  hook: 0 if the workspace looks right after execute
#
# Hooks may use log/pass/fail and the WORKSPACE / PLAN_FILE variables.

API="${TLC_API:-http://localhost:8000}"
HEALTH_TIMEOUT_SEC="${TLC_HEALTH_TIMEOUT_SEC:-300}"
PLAN_TIMEOUT_SEC="${TLC_PLAN_TIMEOUT_SEC:-600}"
EXECUTE_TIMEOUT_SEC="${TLC_EXECUTE_TIMEOUT_SEC:-1200}"

WORKSPACE="$ROOT/workspace"
PLAN_FILE="$WORKSPACE/plan.md"
EMPTY_PLAN=$'# Plan\n\nGoal:\n\n## Todos\n'

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

json_obj() {
  python3 -c '
import json, sys
print(json.dumps(dict(zip(sys.argv[1::2], sys.argv[2::2]))))
' "$@"
}

run_finished() {
  case "$1" in
    ok|error|denied|failed) return 0 ;;
    *) return 1 ;;
  esac
}

# --- workspace lifecycle ----------------------------------------------------

reset_test_artifacts() {
  mkdir -p "$WORKSPACE"
  printf '%s' "$EMPTY_PLAN" >"$PLAN_FILE"
  reset_test_artifacts_keep_plan
}

# Clear what a previous run left behind, without touching plan.md — callers
# after the planner stage have only just written the canonical bytes there.
# exec.log is append-only, so a stale one makes the assertions below read a
# previous session's failures as if they were this run's.
reset_test_artifacts_keep_plan() {
  rm -f "$WORKSPACE/exec.log"
  local rel
  for rel in "${TEST_ARTIFACTS[@]+"${TEST_ARTIFACTS[@]}"}"; do
    rm -rf "${WORKSPACE:?}/$rel"
  done
}

backup_workspace() {
  BACKUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tlc-integ-workspace.XXXXXX")"
  mkdir -p "$WORKSPACE"
  if [[ -n "$(ls -A "$WORKSPACE" 2>/dev/null || true)" ]]; then
    cp -a "$WORKSPACE"/. "$BACKUP_DIR"/
  fi
  reset_test_artifacts
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

# --- shared plan.md assertions ---------------------------------------------

plan_has_todos() {
  [[ -f "$PLAN_FILE" ]] || return 1
  grep -Eq '^[0-9]+\. \[[ x!]\]' "$PLAN_FILE"
}

plan_all_todos_done() {
  [[ -f "$PLAN_FILE" ]] || return 1
  ! grep -Eq '^[0-9]+\. \[[ !]\]' "$PLAN_FILE"
}

no_junk_run_commands() {
  ! grep -q 'junk run command' "$WORKSPACE/exec.log" 2>/dev/null
}

dump_plan() {
  log "--- plan.md ---"
  cat "$PLAN_FILE" 2>/dev/null || true
  log "---------------"
}

dump_exec_log() {
  log "--- exec.log (tail) ---"
  tail -40 "$WORKSPACE/exec.log" 2>/dev/null || true
  log "-----------------------"
}

# --- stages -----------------------------------------------------------------

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
      if ! plan_has_todos; then
        fail "plan finished ($status) but plan.md has no numbered todos"
        dump_plan
        return 1
      fi
      if ! assert_plan_shape; then
        dump_plan
        return 1
      fi
      log "Writing canonical plan (same bytes every run)…"
      printf '%s' "$CANONICAL_PLAN" >"$PLAN_FILE"
      reset_test_artifacts_keep_plan
      pass "plan.md is the canonical plan"
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
        log "Approving command $command_id (allow_all): $cmd"
        api_post "/v1/execute/approve" \
          "$(json_obj command_id "$command_id" decision allow_all)"
        body="$LAST_BODY"
        if [[ "$API_HTTP_CODE" != "200" ]]; then
          fail "POST /v1/execute/approve HTTP $API_HTTP_CODE"
          return 1
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

  assert_execute_result || return 1

  if ! plan_all_todos_done; then
    fail "plan.md has unfinished or skipped todos after execute"
    dump_plan
    dump_exec_log
    return 1
  fi
  pass "every todo completed — no [!] skips"

  if ! no_junk_run_commands; then
    fail "a run command was skipped as a junk run command"
    dump_exec_log
    return 1
  fi
  pass "no run command was dismissed as junk"
  return 0
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
  local rel
  for rel in "${TEST_ARTIFACTS[@]+"${TEST_ARTIFACTS[@]}"}"; do
    if [[ -s "$WORKSPACE/$rel" ]]; then
      fail "/clear-workspace left $rel in the live workspace"
      return 1
    fi
  done
  pass "/clear-workspace archived to ${archive:-archive/}"
  return 0
}

run_integration_test() {
  trap cleanup EXIT

  log "=== TinyLocalCoder integration: plan → execute ($TEST_NAME) ==="
  backup_workspace
  log "Workspace backed up; artifacts reset"

  # The recovery ladder is off on purpose: this test asserts that the planner
  # and executor get it right on the first pass, rather than letting auto-fix
  # or auto-replan paper over a plan that was wrong to begin with.
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
  # cleanup trap prints the summary and exits 0/1
}
