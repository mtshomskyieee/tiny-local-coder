# Source after ROOT is set. Catalog helpers for config.toml.
# tlc_current_model_key  tlc_list_models  tlc_save_model  tlc_prompt_model

if [[ -z "${ROOT:-}" ]]; then
  echo "error: ROOT must be set before sourcing scripts/select-model.sh" >&2
  return 1 2>/dev/null || exit 1
fi

tlc_config_path() {
  echo "$ROOT/config.toml"
}

tlc_current_model_key() {
  awk '
    function trim(s) {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
      gsub(/^"+|"+$/, "", s)
      return s
    }
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*model[[:space:]]*=/ {
      sub(/^[^=]*=/, "")
      print trim($0)
      exit
    }
  ' "$(tlc_config_path)"
}

# Prints: key|ollama|url  one catalog entry per line (order preserved).
tlc_list_models() {
  awk '
    function trim(s) {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
      gsub(/^"+|"+$/, "", s)
      return s
    }
    function flush() {
      if (cur != "") print cur "|" ollama "|" url "|" ram
      cur = ""; ollama = ""; url = ""; ram = ""
    }
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    /^\[models\./ {
      flush()
      sect = trim($0)
      key = sect
      sub(/^\[models\."?/, "", key)
      sub(/"?\]$/, "", key)
      cur = key
      next
    }
    /^\[/ { flush(); next }
    cur != "" {
      eq = index($0, "=")
      if (eq == 0) next
      k = trim(substr($0, 1, eq - 1))
      v = trim(substr($0, eq + 1))
      if (k == "ollama") ollama = v
      if (k == "url") url = v
      if (k == "min_ram_gb") ram = v
    }
    END { flush() }
  ' "$(tlc_config_path)"
}

tlc_model_exists() {
  local key="$1"
  local line
  while IFS= read -r line; do
    [[ "${line%%|*}" == "$key" ]] && return 0
  done < <(tlc_list_models)
  return 1
}

tlc_save_model() {
  local key="$1"
  local config tmp
  if [[ -z "$key" ]]; then
    echo "error: model key is required" >&2
    return 1
  fi
  if ! tlc_model_exists "$key"; then
    echo "error: unknown model '$key'. Choose one of:" >&2
    tlc_list_models | awk -F'|' '{ print "  " $1 "  →  " $2 }' >&2
    return 1
  fi
  config="$(tlc_config_path)"
  tmp="$(mktemp "${TMPDIR:-/tmp}/tlc-config.XXXXXX")"
  awk -v key="$key" '
    BEGIN { done = 0 }
    /^[[:space:]]*model[[:space:]]*=/ && !done {
      print "model = \"" key "\""
      done = 1
      next
    }
    { print }
    END {
      if (!done) print "model = \"" key "\""
    }
  ' "$config" > "$tmp"
  mv "$tmp" "$config"
}

# Interactive picker. Writes the chosen key to config.toml.
# Return accepts the saved default (already selected).
tlc_prompt_model() {
  local current choice i key ollama url n default_n
  current="$(tlc_current_model_key)"
  if [[ -z "$current" ]]; then
    current="qwen2.5"
  fi

  echo
  echo "Select the local Ollama model (saved in config.toml)"
  i=0
  default_n=1
  while IFS='|' read -r key ollama url ram; do
    i=$((i + 1))
    ram_note=""
    if [[ -n "$ram" && "$ram" != "0" ]]; then
      ram_note="  (~${ram} GB Docker RAM)"
    fi
    if [[ "$key" == "$current" ]]; then
      default_n="$i"
      printf '  %d) %s  →  %s%s  [default]\n' "$i" "$key" "$ollama" "$ram_note"
    else
      printf '  %d) %s  →  %s%s\n' "$i" "$key" "$ollama" "$ram_note"
    fi
    if [[ -n "$url" ]]; then
      printf '       %s\n' "$url"
    fi
  done < <(tlc_list_models)
  n="$i"
  echo
  printf 'Choice [%d] %s (Return to accept): ' "$default_n" "$current"

  if [[ -r /dev/tty ]]; then
    IFS= read -r choice < /dev/tty || true
  else
    IFS= read -r choice || true
  fi
  choice="${choice#"${choice%%[![:space:]]*}"}"
  choice="${choice%"${choice##*[![:space:]]}"}"

  if [[ -z "$choice" ]]; then
    tlc_save_model "$current"
    echo "Using default $current"
    return 0
  fi

  if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= n )); then
    i=0
    while IFS='|' read -r key ollama url; do
      i=$((i + 1))
      if [[ "$i" -eq "$choice" ]]; then
        tlc_save_model "$key"
        echo "Saved model = \"$key\" → $ollama"
        return 0
      fi
    done < <(tlc_list_models)
  fi

  if tlc_model_exists "$choice"; then
    tlc_save_model "$choice"
    echo "Saved model = \"$choice\""
    return 0
  fi

  echo "error: '$choice' is not a catalog model" >&2
  tlc_list_models | awk -F'|' '{ print "  " $1 }' >&2
  return 1
}
