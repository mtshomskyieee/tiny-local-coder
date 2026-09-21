# Source after ROOT is set, and after select-memory.sh (for tlc_host_ram_gb
# and tlc_size_to_gb). Context-window helpers over config.toml.
# tlc_ctx_options  tlc_current_ctx  tlc_save_ctx  tlc_prompt_ctx  tlc_kv_mb
#
# num_ctx lives in config.toml beside the model it belongs to, because the KV
# cost of a token is a property of the architecture. .env owns the memory
# caps; config.toml owns the model and its context.

if [[ -z "${ROOT:-}" ]]; then
  echo "error: ROOT must be set before sourcing scripts/select-ctx.sh" >&2
  return 1 2>/dev/null || exit 1
fi

# 2048 is the floor the agent prompts were sized against; the rest are the
# sizes worth trying on a CPU-only host before the KV cache starts to matter.
tlc_ctx_options() {
  printf '%s\n' 2048 4096 8192 10240 16384 32768
}

tlc_current_ctx() {
  local v
  v="$(tlc_config_get_active num_ctx)"
  echo "${v:-2048}"
}

# Read a key from the active [models."<key>"] table in config.toml.
tlc_config_get_active() {
  local want="$1"
  awk -v want="$want" '
    function trim(s) {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
      gsub(/^"+|"+$/, "", s)
      return s
    }
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    /^[[:space:]]*model[[:space:]]*=/ && key == "" {
      sub(/^[^=]*=/, ""); key = trim($0); next
    }
    /^\[/ {
      sect = trim($0)
      in_active = (key != "" && (sect == "[models.\"" key "\"]" || sect == "[models." key "]"))
      next
    }
    in_active {
      eq = index($0, "=")
      if (eq == 0) next
      if (trim(substr($0, 1, eq - 1)) == want) { print trim(substr($0, eq + 1)); exit }
    }
  ' "$ROOT/config.toml"
}

# Write num_ctx into the active model's table (only that table).
tlc_save_ctx() {
  local value="$1" config tmp
  if ! [[ "$value" =~ ^[0-9]+$ ]] || (( value < 512 )); then
    echo "error: --ctx wants a token count of at least 512 (got '$value')" >&2
    return 1
  fi
  local ceiling="${MAX_CTX:-0}"
  if [[ "$ceiling" =~ ^[0-9]+$ ]] && (( ceiling > 0 && value > ceiling )); then
    echo "error: $value exceeds this model's max_ctx of $ceiling" >&2
    return 1
  fi
  config="$ROOT/config.toml"
  tmp="$(mktemp "${TMPDIR:-/tmp}/tlc-ctx.XXXXXX")"
  awk -v v="$value" '
    function trim(s) {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
      gsub(/^"+|"+$/, "", s)
      return s
    }
    /^[[:space:]]*model[[:space:]]*=/ && key == "" && !/^[[:space:]]*#/ {
      line = $0; sub(/^[^=]*=/, "", line); key = trim(line); print; next
    }
    /^\[/ {
      sect = trim($0)
      in_active = (key != "" && (sect == "[models.\"" key "\"]" || sect == "[models." key "]"))
      print; next
    }
    in_active && /^[[:space:]]*num_ctx[[:space:]]*=/ && !/^[[:space:]]*#/ {
      print "num_ctx = " v; done = 1; next
    }
    { print }
  ' "$config" > "$tmp"
  mv "$tmp" "$config"
}

# KV cache megabytes for a context size across the configured parallel slots.
tlc_kv_mb() {
  local ctx="$1" per_token slots bytes
  per_token="${KV_BYTES_PER_TOKEN:-0}"
  [[ "$per_token" =~ ^[0-9]+$ ]] || per_token=0
  (( per_token == 0 )) && { echo ""; return 0; }
  slots="$(tlc_env_get OLLAMA_NUM_PARALLEL 2>/dev/null || echo 1)"
  [[ "$slots" =~ ^[0-9]+$ ]] || slots=1
  (( slots == 0 )) && slots=4      # auto: Ollama's own ceiling
  bytes=$(( ctx * per_token * slots ))
  echo $(( bytes / 1024 / 1024 ))
}

# Interactive picker. Writes num_ctx into config.toml for the active model.
tlc_prompt_ctx() {
  local current choice i opt n default_n kv host budget per_copy loaded acap ceiling

  current="$(tlc_current_ctx)"
  host="$(tlc_host_ram_gb)"
  ceiling="${MAX_CTX:-0}"
  per_copy="${MIN_RAM_GB:-0}"
  loaded="$(tlc_env_get OLLAMA_MAX_LOADED_MODELS 2>/dev/null || echo 1)"
  [[ "$loaded" =~ ^[0-9]+$ ]] || loaded=1
  (( loaded == 0 )) && loaded=3
  acap="$(tlc_size_to_gb "$(tlc_env_get APP_MEM_LIMIT 2>/dev/null || echo 0)")"

  echo
  echo "Select the working context (num_ctx, saved in config.toml)"
  echo "  2048 is the floor the agent prompts were sized against; larger lets a"
  echo "  todo carry more of its file with it. The cost is the KV cache."
  echo
  i=0
  default_n=1
  while IFS= read -r opt; do
    if [[ "$ceiling" =~ ^[0-9]+$ ]] && (( ceiling > 0 && opt > ceiling )); then
      continue
    fi
    i=$((i + 1))
    kv="$(tlc_kv_mb "$opt")"
    budget=""
    if [[ "$per_copy" =~ ^[0-9]+$ ]] && (( per_copy > 0 )) && [[ -n "$kv" ]]; then
      budget="$(( per_copy * loaded + acap ))"
      budget="$(printf '~%s GB + %s MB KV' "$budget" "$kv")"
      if [[ -n "$host" ]]; then
        budget="$budget of ${host} GB"
      fi
    fi
    if [[ "$opt" == "$current" ]]; then
      default_n="$i"
      printf '  %d) %-6s %-30s [current]\n' "$i" "$opt" "$budget"
    else
      printf '  %d) %-6s %s\n' "$i" "$opt" "$budget"
    fi
  done < <(tlc_ctx_options)
  n="$i"
  echo
  printf 'Choice [%d] %s (Return to accept): ' "$default_n" "$current"

  choice=""
  if ! { [[ -r /dev/tty ]] && IFS= read -r choice 2>/dev/null < /dev/tty; }; then
    IFS= read -r choice || true
  fi
  choice="${choice#"${choice%%[![:space:]]*}"}"
  choice="${choice%"${choice##*[![:space:]]}"}"

  if [[ -z "$choice" ]]; then
    echo "Using num_ctx = $current"
    return 0
  fi

  # A small number is a menu index; anything else is a literal token count.
  if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= n )); then
    i=0
    while IFS= read -r opt; do
      if [[ "$ceiling" =~ ^[0-9]+$ ]] && (( ceiling > 0 && opt > ceiling )); then
        continue
      fi
      i=$((i + 1))
      if (( i == choice )); then
        tlc_save_ctx "$opt" && echo "Saved num_ctx = $opt to config.toml"
        return $?
      fi
    done < <(tlc_ctx_options)
  fi

  if [[ "$choice" =~ ^[0-9]+$ ]]; then
    tlc_save_ctx "$choice" && echo "Saved num_ctx = $choice to config.toml"
    return $?
  fi

  echo "error: '$choice' is not a context size" >&2
  return 1
}
