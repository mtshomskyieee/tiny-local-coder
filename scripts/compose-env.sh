# Source after ROOT is set. Exports MODEL_NAME, NUM_CTX, TLC_MODEL_KEY
# from the repo-root config.toml (qwen2.5 | qwen3.5). POSIX awk only —
# macOS /usr/bin/python3 is often too old for tomllib.
if [[ -z "${ROOT:-}" ]]; then
  echo "error: ROOT must be set before sourcing scripts/compose-env.sh" >&2
  return 1 2>/dev/null || exit 1
fi

config="$ROOT/config.toml"
if [[ ! -f "$config" ]]; then
  echo "error: $config not found" >&2
  return 1 2>/dev/null || exit 1
fi

eval "$(
  awk '
    function trim(s) {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", s)
      gsub(/^"+|"+$/, "", s)
      return s
    }
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    /^[[:space:]]*model[[:space:]]*=/ && key == "" {
      sub(/^[^=]*=/, "")
      key = trim($0)
      next
    }
    /^\[/ {
      sect = trim($0)
      quoted = "[models.\"" key "\"]"
      plain = "[models." key "]"
      in_active = (key != "" && (sect == quoted || sect == plain))
      next
    }
    in_active {
      eq = index($0, "=")
      if (eq == 0) next
      k = trim(substr($0, 1, eq - 1))
      v = trim(substr($0, eq + 1))
      if (k == "ollama") ollama = v
      if (k == "num_ctx") num_ctx = v
      if (k == "min_ram_gb") min_ram = v
    }
    END {
      if (key == "") {
        print "error: config.toml is missing model =" > "/dev/stderr"
        exit 1
      }
      if (ollama == "") {
        print "error: unknown model \"" key "\" in config.toml (need [models.\"" key "\"])" > "/dev/stderr"
        exit 1
      }
      if (num_ctx == "") num_ctx = 2048
      if (min_ram == "") min_ram = 0
      print "MODEL_NAME=" ollama
      print "NUM_CTX=" num_ctx
      print "TLC_MODEL_KEY=" key
      print "MIN_RAM_GB=" min_ram
    }
  ' "$config"
)"
export MODEL_NAME NUM_CTX TLC_MODEL_KEY MIN_RAM_GB
