#!/usr/bin/env bash
# Start the TinyLocalCoder suite (Ollama + API app).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

usage() {
  cat <<EOF
Usage: $0 [options]

Starts TinyLocalCoder (ollama + app API on :8000).
Always prompts for qwen2.5 or qwen3.5; the saved default is pre-selected
(press Return to keep it). The choice is written to config.toml.

Options:
  --model KEY          Use this catalog key and save it (skip the picker)
  --memory PROFILE     Use this memory profile (laptop | workstation | unbounded)
  --ctx N              Use this working context (2048 | 4096 | 8192 | 10240 | …)
  --no-probe           Skip the timed model probe after startup
  --no-prompt          Keep the saved settings; skip the pickers
  --build, -b          Rebuild the app image before starting
  --no-build           Skip rebuild (default)
  --foreground, -f     Run compose in the foreground
  -h, --help           Show this help

TUI:  ./cli.sh
Stop: ./stop-service.sh
EOF
}

COMPOSE=(docker compose)
if ! docker compose version >/dev/null 2>&1; then
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    echo "error: need 'docker compose' or 'docker-compose'" >&2
    exit 1
  fi
fi

if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

# shellcheck source=scripts/select-model.sh
source "$ROOT/scripts/select-model.sh"
# shellcheck source=scripts/check-memory-cgroup.sh
source "$ROOT/scripts/check-memory-cgroup.sh"
# shellcheck source=scripts/select-memory.sh
source "$ROOT/scripts/select-memory.sh"
# shellcheck source=scripts/select-ctx.sh
source "$ROOT/scripts/select-ctx.sh"

BUILD=0
DETACH=1
MODEL_KEY=""
MEMORY_PROFILE=""
CTX_SIZE=""
PROBE=1
SELECT_MODEL=yes
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)
      MODEL_KEY="${2:-}"
      if [[ -z "$MODEL_KEY" ]]; then
        echo "error: --model requires a catalog key (qwen2.5 | qwen3.5)" >&2
        exit 1
      fi
      shift 2
      ;;
    --model=*)
      MODEL_KEY="${1#*=}"
      shift
      ;;
    --memory)
      MEMORY_PROFILE="${2:-}"
      if [[ -z "$MEMORY_PROFILE" ]]; then
        echo "error: --memory requires a profile (laptop | workstation | unbounded)" >&2
        exit 1
      fi
      shift 2
      ;;
    --memory=*)
      MEMORY_PROFILE="${1#*=}"
      shift
      ;;
    --ctx)
      CTX_SIZE="${2:-}"
      if [[ -z "$CTX_SIZE" ]]; then
        echo "error: --ctx requires a token count (e.g. 8192)" >&2
        exit 1
      fi
      shift 2
      ;;
    --ctx=*)
      CTX_SIZE="${1#*=}"
      shift
      ;;
    --no-probe) PROBE=0; shift ;;
    --select|--select-model)
      SELECT_MODEL=yes
      shift
      ;;
    --no-prompt|--keep-model)
      SELECT_MODEL=no
      shift
      ;;
    --build|-b) BUILD=1; shift ;;
    --no-build) BUILD=0; shift ;;
    --foreground|-f) DETACH=0; shift ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -n "$MODEL_KEY" ]]; then
  tlc_save_model "$MODEL_KEY"
  echo "Saved model = \"$MODEL_KEY\" to config.toml"
elif [[ "$SELECT_MODEL" == "yes" ]]; then
  if [[ -t 0 || -r /dev/tty ]]; then
    tlc_prompt_model
  else
    echo "No terminal for the model picker — using saved default." >&2
  fi
fi

if [[ -n "$MEMORY_PROFILE" ]]; then
  tlc_save_memory_profile "$MEMORY_PROFILE"
  echo "Saved memory profile \"$MEMORY_PROFILE\" to .env"
elif [[ "$SELECT_MODEL" == "yes" ]]; then
  if [[ -t 0 || -r /dev/tty ]]; then
    tlc_prompt_memory
  fi
fi

# shellcheck source=scripts/compose-env.sh
source "$ROOT/scripts/compose-env.sh"

# The ctx picker prices each option from the model's kv_bytes_per_token, so it
# has to run after compose-env; compose-env then re-runs to pick up the change.
if [[ -n "$CTX_SIZE" ]]; then
  tlc_save_ctx "$CTX_SIZE"
  echo "Saved num_ctx = $CTX_SIZE to config.toml"
elif [[ "$SELECT_MODEL" == "yes" ]]; then
  if [[ -t 0 || -r /dev/tty ]]; then
    tlc_prompt_ctx
  fi
fi
source "$ROOT/scripts/compose-env.sh"

echo "Model: $TLC_MODEL_KEY → $MODEL_NAME (num_ctx=$NUM_CTX)"

# MIN_RAM_GB comes from compose-env, so the fit check runs after it.
tlc_memory_summary
tlc_warn_memory_cgroup

# shellcheck source=scripts/probe-model.sh
source "$ROOT/scripts/probe-model.sh"

# shellcheck source=scripts/ensure-ollama-model.sh
source "$ROOT/scripts/ensure-ollama-model.sh"

# Ensure the named Ollama model volume exists (preserves pulls across restarts).
VOLUME_NAME="crew_pipeline_ollama_data"
if ! docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1; then
  echo "Creating Docker volume $VOLUME_NAME"
  docker volume create "$VOLUME_NAME" >/dev/null
fi

echo "Starting TinyLocalCoder…"
# Toolchains the agent apt-installed at runtime are recorded here; feed them
# back into the image build so a rebuild does not lose them.
if [[ -s workspace/.toolchains ]]; then
  EXTRA_APT_PACKAGES="$(tr -s '\n' ' ' < workspace/.toolchains | xargs)"
  export EXTRA_APT_PACKAGES
  echo "Baking in recorded toolchains: $EXTRA_APT_PACKAGES"
fi

if [[ "$BUILD" -eq 1 ]]; then
  echo "Rebuilding app image…"
  "${COMPOSE[@]}" build
fi

# Bring Ollama up first so we can check/pull the selected model before the app.
echo "Starting Ollama…"
"${COMPOSE[@]}" up -d ollama
tlc_ensure_ollama_model

if [[ "$PROBE" -eq 1 ]]; then
  echo
  tlc_probe_model
fi

if [[ "$DETACH" -eq 1 ]]; then
  "${COMPOSE[@]}" up -d --force-recreate --no-deps app
  echo
  "${COMPOSE[@]}" ps
  echo
  echo "API:    http://localhost:8000/health"
  echo "Ollama: http://localhost:11434"
  echo "TUI:    ./cli.sh"
  echo "Stop:   ./stop-service.sh"
else
  "${COMPOSE[@]}" up --force-recreate --no-deps app
fi
