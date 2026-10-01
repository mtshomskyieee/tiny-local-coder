# Source after LLM_BASE_URL is set to the host OpenAI base (…/v1).
# tlc_lmstudio_discover  — require a loaded model; sets MODEL_NAME
# tlc_lmstudio_docker_url — sets LLM_BASE_URL_DOCKER for the app container

tlc_lmstudio_discover() {
  local base json id
  base="${LLM_BASE_URL:-http://127.0.0.1:1234/v1}"
  base="${base%/}"
  if ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    echo "error: curl and python3 are required to reach LM Studio" >&2
    return 1
  fi
  json="$(curl -fsS --max-time 5 "$base/models" 2>/dev/null || true)"
  if [[ -z "$json" ]]; then
    echo "error: LM Studio is not answering at ${base%/v1}" >&2
    echo "Start LM Studio, load a model, and enable the local server." >&2
    return 1
  fi
  if ! id="$(printf '%s' "$json" | python3 -c '
import json, sys
data = json.load(sys.stdin)
models = data.get("data") or []
if not models:
    sys.exit(2)
first = models[0]
print(first if isinstance(first, str) else (first.get("id") or ""))
')"; then
    echo "error: LM Studio at $base has no model loaded." >&2
    echo "Load a model in LM Studio, then start again." >&2
    return 1
  fi
  if [[ -z "$id" ]]; then
    echo "error: LM Studio at $base has no model loaded." >&2
    echo "Load a model in LM Studio, then start again." >&2
    return 1
  fi
  MODEL_NAME="$id"
  export MODEL_NAME
  echo "LM Studio model: $MODEL_NAME"
}

tlc_lmstudio_docker_url() {
  local base="${LLM_BASE_URL:-http://127.0.0.1:1234/v1}"
  base="${base//127.0.0.1/host.docker.internal}"
  base="${base//localhost/host.docker.internal}"
  LLM_BASE_URL_DOCKER="$base"
  export LLM_BASE_URL_DOCKER
}
