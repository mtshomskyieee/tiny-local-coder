# Source after ROOT is set. Memory profile helpers over .env.
# tlc_memory_profiles  tlc_current_memory_profile  tlc_save_memory_profile
# tlc_prompt_memory    tlc_memory_summary
#
# .env stays the source of truth: a profile is just a named set of values that
# gets written into it, and the summary always reports what .env actually says
# so a hand-edited value is never misrepresented as a profile.

if [[ -z "${ROOT:-}" ]]; then
  echo "error: ROOT must be set before sourcing scripts/select-memory.sh" >&2
  return 1 2>/dev/null || exit 1
fi

TLC_MEM_KEYS=(
  OLLAMA_MAX_LOADED_MODELS
  OLLAMA_NUM_PARALLEL
  OLLAMA_MAX_QUEUE
  OLLAMA_KEEP_ALIVE
  OLLAMA_MEM_LIMIT
  APP_MEM_LIMIT
)

# name|loaded|parallel|queue|keep_alive|ollama_cap|app_cap|blurb
# 0 means "Ollama's own auto behaviour" for the first three, and "no cgroup
# limit" for the caps.
tlc_memory_profiles() {
  cat <<'EOF'
laptop|1|1|1|5m|5g|1g|one resident copy, one request — fits 8-16 GB hosts
workstation|1|2|2|30m|12g|2g|two concurrent requests, longer warm — 32 GB+
unbounded|0|0|512|5m|0|0|Ollama defaults; up to 3 copies and 4 slots
EOF
}

tlc_env_path() {
  echo "$ROOT/.env"
}

# Read one key out of .env (last assignment wins, as dotenv does).
tlc_env_get() {
  local key="$1" env_file
  env_file="$(tlc_env_path)"
  [[ -f "$env_file" ]] || return 0
  awk -F= -v k="$key" '
    /^[[:space:]]*#/ { next }
    {
      key = $1
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
      if (key != k) next
      sub(/^[^=]*=/, "")
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0)
      gsub(/^"+|"+$/, "", $0)
      val = $0
      found = 1
    }
    END { if (found) print val }
  ' "$env_file"
}

# Set KEY=VALUE in .env, preserving surrounding comments and ordering.
tlc_env_set() {
  local key="$1" value="$2" env_file tmp
  env_file="$(tlc_env_path)"
  touch "$env_file"
  tmp="$(mktemp "${TMPDIR:-/tmp}/tlc-env.XXXXXX")"
  awk -v k="$key" -v v="$value" '
    BEGIN { done = 0 }
    {
      key = $0
      sub(/=.*$/, "", key)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
      if ($0 !~ /^[[:space:]]*#/ && key == k) {
        if (!done) { print k "=" v; done = 1 }
        next
      }
      print
    }
    END { if (!done) print k "=" v }
  ' "$env_file" > "$tmp"
  mv "$tmp" "$env_file"
}

# Name of the profile whose values match .env exactly, else "custom".
tlc_current_memory_profile() {
  local name loaded parallel queue keep ocap acap blurb
  while IFS='|' read -r name loaded parallel queue keep ocap acap blurb; do
    [[ "$(tlc_env_get OLLAMA_MAX_LOADED_MODELS)" == "$loaded"   ]] || continue
    [[ "$(tlc_env_get OLLAMA_NUM_PARALLEL)"      == "$parallel" ]] || continue
    [[ "$(tlc_env_get OLLAMA_MAX_QUEUE)"         == "$queue"    ]] || continue
    [[ "$(tlc_env_get OLLAMA_KEEP_ALIVE)"        == "$keep"     ]] || continue
    [[ "$(tlc_env_get OLLAMA_MEM_LIMIT)"         == "$ocap"     ]] || continue
    [[ "$(tlc_env_get APP_MEM_LIMIT)"            == "$acap"     ]] || continue
    echo "$name"
    return 0
  done < <(tlc_memory_profiles)
  echo "custom"
}

tlc_memory_profile_exists() {
  local want="$1" line
  while IFS= read -r line; do
    [[ "${line%%|*}" == "$want" ]] && return 0
  done < <(tlc_memory_profiles)
  return 1
}

tlc_save_memory_profile() {
  local want="$1" name loaded parallel queue keep ocap acap blurb
  if [[ "$want" == "custom" ]]; then
    return 0   # keep whatever .env already says
  fi
  if ! tlc_memory_profile_exists "$want"; then
    echo "error: unknown memory profile '$want'. Choose one of:" >&2
    tlc_memory_profiles | awk -F'|' '{ print "  " $1 }' >&2
    return 1
  fi
  while IFS='|' read -r name loaded parallel queue keep ocap acap blurb; do
    [[ "$name" == "$want" ]] || continue
    tlc_env_set OLLAMA_MAX_LOADED_MODELS "$loaded"
    tlc_env_set OLLAMA_NUM_PARALLEL "$parallel"
    tlc_env_set OLLAMA_MAX_QUEUE "$queue"
    tlc_env_set OLLAMA_KEEP_ALIVE "$keep"
    tlc_env_set OLLAMA_MEM_LIMIT "$ocap"
    tlc_env_set APP_MEM_LIMIT "$acap"
    return 0
  done < <(tlc_memory_profiles)
}

# Total host RAM in whole GB, or empty when it cannot be determined.
tlc_host_ram_gb() {
  local kb bytes
  if [[ -r /proc/meminfo ]]; then
    kb="$(awk '/^MemTotal:/ { print $2; exit }' /proc/meminfo)"
    [[ -n "$kb" ]] && echo $(( kb / 1024 / 1024 )) && return 0
  fi
  if command -v sysctl >/dev/null 2>&1; then
    bytes="$(sysctl -n hw.memsize 2>/dev/null || true)"
    [[ -n "$bytes" ]] && echo $(( bytes / 1024 / 1024 / 1024 )) && return 0
  fi
  echo ""
}

tlc_swap_gb() {
  local kb
  if [[ -r /proc/meminfo ]]; then
    kb="$(awk '/^SwapTotal:/ { print $2; exit }' /proc/meminfo)"
    [[ -n "$kb" ]] && echo $(( kb / 1024 / 1024 )) && return 0
  fi
  echo ""
}

# "5g" / "512m" -> whole GB, rounded up. "0" and "" -> 0.
tlc_size_to_gb() {
  local v="${1:-0}" num unit
  v="$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')"
  [[ "$v" == "0" || -z "$v" ]] && { echo 0; return 0; }
  num="${v%[gmk]}"
  unit="${v#"$num"}"
  [[ "$num" =~ ^[0-9]+$ ]] || { echo 0; return 0; }
  case "$unit" in
    g|"") echo "$num" ;;
    m)    echo $(( (num + 1023) / 1024 )) ;;
    k)    echo $(( (num + 1048575) / 1048576 )) ;;
    *)    echo 0 ;;
  esac
}

# Resolved settings, the fit against this host, and whether the caps are real.
tlc_memory_summary() {
  local profile host swap loaded parallel queue keep ocap acap
  local per_copy budget enforced note

  profile="$(tlc_current_memory_profile)"
  host="$(tlc_host_ram_gb)"
  swap="$(tlc_swap_gb)"
  loaded="$(tlc_env_get OLLAMA_MAX_LOADED_MODELS)"
  parallel="$(tlc_env_get OLLAMA_NUM_PARALLEL)"
  queue="$(tlc_env_get OLLAMA_MAX_QUEUE)"
  keep="$(tlc_env_get OLLAMA_KEEP_ALIVE)"
  ocap="$(tlc_env_get OLLAMA_MEM_LIMIT)"
  acap="$(tlc_env_get APP_MEM_LIMIT)"

  enforced=no
  if declare -F tlc_memory_cgroup_delegated >/dev/null 2>&1; then
    tlc_memory_cgroup_delegated && enforced=yes
  else
    enforced=unknown
  fi

  echo
  if [[ -n "$host" ]]; then
    printf 'Memory (host: %s GB RAM, %s GB swap) — profile "%s"\n' \
      "$host" "${swap:-?}" "$profile"
  else
    printf 'Memory — profile "%s"\n' "$profile"
  fi

  printf '  loaded models  %-8s %s\n' "${loaded:-auto}" \
    "$([[ "${loaded:-0}" == "0" ]] && echo 'auto — up to 3 resident copies' || echo 'resident copies of the weights')"
  printf '  parallel slots %-8s %s\n' "${parallel:-auto}" \
    "$([[ "${parallel:-0}" == "0" ]] && echo 'auto — up to 4 KV caches' || echo "KV cache(s) at num_ctx=${NUM_CTX:-?}")"
  printf '  queue depth    %s\n' "${queue:-auto}"
  printf '  keep alive     %-8s %s\n' "${keep:-auto}" 'warm between graph nodes'

  # The context window's whole memory cost, and the reason 2048 was the floor.
  local kv_mb=""
  if declare -F tlc_kv_mb >/dev/null 2>&1; then
    kv_mb="$(tlc_kv_mb "${NUM_CTX:-2048}")"
  fi
  if [[ -n "$kv_mb" ]]; then
    printf '  context        %-8s %s MB KV cache\n' "${NUM_CTX:-?}" "$kv_mb"
  else
    printf '  context        %-8s\n' "${NUM_CTX:-?}"
  fi

  note=''
  [[ "$enforced" == "no" ]] && note='NOT ENFORCED (no memory cgroup)'
  [[ "$enforced" == "unknown" ]] && note='enforcement unknown'
  if [[ "${ocap:-0}" == "0" ]]; then
    printf '  ollama cap     %-8s %s\n' 'none' 'no limit requested'
  else
    printf '  ollama cap     %-8s %s\n' "$ocap" "$note"
  fi
  if [[ "${acap:-0}" == "0" ]]; then
    printf '  app cap        %-8s %s\n' 'none' 'no limit requested'
  else
    printf '  app cap        %-8s %s\n' "$acap" "$note"
  fi

  # Budget, not a measurement: the model's documented per-copy requirement
  # times the number of copies we allow, plus the app. Parallel slots add KV
  # caches on top, which num_ctx keeps small.
  per_copy="${MIN_RAM_GB:-0}"
  if [[ "$per_copy" =~ ^[0-9]+$ ]] && (( per_copy > 0 )); then
    local copies="${loaded:-0}"
    [[ "$copies" =~ ^[0-9]+$ ]] || copies=0
    (( copies == 0 )) && copies=3     # auto: Ollama's own ceiling
    local kv_gb=0
    [[ -n "$kv_mb" ]] && kv_gb=$(( (kv_mb + 1023) / 1024 ))
    budget=$(( per_copy * copies + $(tlc_size_to_gb "$acap") + kv_gb ))
    printf '  ----------------------------------------------\n'
    if [[ -n "$host" ]]; then
      printf '  budget         ~%s GB of %s GB  (%s x %s GB/copy + KV + app)\n' \
        "$budget" "$host" "$copies" "$per_copy"
      if (( budget > host )); then
        echo
        printf 'warning: this profile budgets more memory (~%s GB) than the host has (%s GB).\n' \
          "$budget" "$host" >&2
        if [[ -n "$swap" ]] && (( swap > 0 )) && [[ "$enforced" != "yes" ]]; then
          printf '         With %s GB of swap and no enforceable cap, an overrun thrashes\n' "$swap" >&2
          printf '         the host instead of being OOM-killed. Pick a smaller profile.\n' >&2
        fi
      fi
    else
      printf '  budget         ~%s GB  (%s x %s GB/copy + KV + app)\n' \
        "$budget" "$copies" "$per_copy"
    fi
  fi
  echo
}

# Interactive picker. Writes the chosen profile's values into .env.
tlc_prompt_memory() {
  local current choice i name loaded parallel queue keep ocap acap blurb n default_n
  current="$(tlc_current_memory_profile)"

  echo
  echo "Select the memory profile (written to .env)"
  i=0
  default_n=1
  while IFS='|' read -r name loaded parallel queue keep ocap acap blurb; do
    i=$((i + 1))
    if [[ "$name" == "$current" ]]; then
      default_n="$i"
      printf '  %d) %-12s %s  [default]\n' "$i" "$name" "$blurb"
    else
      printf '  %d) %-12s %s\n' "$i" "$name" "$blurb"
    fi
  done < <(tlc_memory_profiles)
  n="$i"
  if [[ "$current" == "custom" ]]; then
    i=$((i + 1))
    default_n="$i"
    printf '  %d) %-12s %s  [default]\n' "$i" "custom" "keep the values already in .env"
    n="$i"
  fi
  echo
  printf 'Choice [%d] %s (Return to accept): ' "$default_n" "$current"

  # /dev/tty can be readable by mode yet fail to open (no controlling
  # terminal), so fall through to stdin rather than silently taking the
  # default on a read that never happened.
  choice=""
  if ! { [[ -r /dev/tty ]] && IFS= read -r choice 2>/dev/null < /dev/tty; }; then
    IFS= read -r choice || true
  fi
  choice="${choice#"${choice%%[![:space:]]*}"}"
  choice="${choice%"${choice##*[![:space:]]}"}"

  if [[ -z "$choice" ]]; then
    tlc_save_memory_profile "$current"
    echo "Using $current"
    return 0
  fi

  if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= n )); then
    if [[ "$current" == "custom" && "$choice" -eq "$n" ]]; then
      echo "Keeping the custom values in .env"
      return 0
    fi
    i=0
    while IFS='|' read -r name loaded parallel queue keep ocap acap blurb; do
      i=$((i + 1))
      if [[ "$i" -eq "$choice" ]]; then
        tlc_save_memory_profile "$name"
        echo "Saved memory profile \"$name\" to .env"
        return 0
      fi
    done < <(tlc_memory_profiles)
  fi

  if [[ "$choice" == "custom" ]]; then
    echo "Keeping the custom values in .env"
    return 0
  fi
  if tlc_memory_profile_exists "$choice"; then
    tlc_save_memory_profile "$choice"
    echo "Saved memory profile \"$choice\" to .env"
    return 0
  fi

  echo "error: '$choice' is not a memory profile" >&2
  tlc_memory_profiles | awk -F'|' '{ print "  " $1 }' >&2
  return 1
}
