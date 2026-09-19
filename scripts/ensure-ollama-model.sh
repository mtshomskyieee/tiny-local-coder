# Source after ROOT, COMPOSE, and MODEL_NAME are set.
# Waits for the ollama service, then pulls MODEL_NAME if it is not local.

tlc_wait_for_ollama() {
  local i=0
  echo "Waiting for Ollama…"
  while ! "${COMPOSE[@]}" exec -T ollama ollama list >/dev/null 2>&1; do
    i=$((i + 1))
    if [[ "$i" -gt 60 ]]; then
      echo "error: Ollama did not become ready" >&2
      return 1
    fi
    sleep 1
  done
}

tlc_ollama_has_model() {
  local name="$1"
  "${COMPOSE[@]}" exec -T ollama ollama show "$name" >/dev/null 2>&1
}

tlc_docker_ram_gb() {
  local bytes
  bytes="$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
  if [[ -z "$bytes" || "$bytes" == "0" ]]; then
    echo 0
    return 0
  fi
  echo $((bytes / 1024 / 1024 / 1024))
}

tlc_check_docker_ram() {
  local have need
  need="${MIN_RAM_GB:-0}"
  if [[ -z "$need" || "$need" -le 0 ]]; then
    return 0
  fi
  have="$(tlc_docker_ram_gb)"
  if [[ -z "$have" || "$have" -le 0 ]]; then
    echo "warning: could not read Docker RAM; $MODEL_NAME wants ~${need} GB" >&2
    return 0
  fi
  if [[ "$have" -lt "$need" ]]; then
    cat >&2 <<EOF
error: Docker has ${have} GB RAM; $MODEL_NAME needs about ${need} GB to load.

That 500 / CPU_REPACK error means the VM ran out of memory (Colima
defaults to 2 GB). Give Docker more RAM, then start again:

  ./colima-start-stop-on-mac.sh
  ./start-service.sh

Or choose qwen2.5 at the picker (~4 GB).
EOF
    return 1
  fi
  echo "Docker RAM: ${have} GB (need ~${need} GB for $MODEL_NAME)"
}

tlc_ensure_ollama_model() {
  if [[ -z "${MODEL_NAME:-}" ]]; then
    echo "error: MODEL_NAME is not set" >&2
    return 1
  fi
  tlc_check_docker_ram
  tlc_wait_for_ollama
  if tlc_ollama_has_model "$MODEL_NAME"; then
    echo "Model $MODEL_NAME is already installed."
    return 0
  fi
  echo
  echo "Model $MODEL_NAME is not installed locally."
  echo "Pulling from https://ollama.com/library/${MODEL_NAME}  (first time is slow)…"
  echo
  if ! "${COMPOSE[@]}" exec -T ollama ollama pull "$MODEL_NAME"; then
    echo "error: ollama pull $MODEL_NAME failed" >&2
    return 1
  fi
  if ! tlc_ollama_has_model "$MODEL_NAME"; then
    echo "error: $MODEL_NAME is still not available after pull" >&2
    return 1
  fi
  echo "Model $MODEL_NAME is ready."
}
