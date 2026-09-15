#!/usr/bin/env bash
# Start the TinyLocalCoder suite (Ollama + API app).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

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

# Ensure the named Ollama model volume exists (preserves qwen2.5:3b across restarts).
VOLUME_NAME="crew_pipeline_ollama_data"
if ! docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1; then
  echo "Creating Docker volume $VOLUME_NAME"
  docker volume create "$VOLUME_NAME" >/dev/null
fi

BUILD=0
DETACH=1
for arg in "$@"; do
  case "$arg" in
    --build|-b) BUILD=1 ;;
    --no-build) BUILD=0 ;;
    --foreground|-f) DETACH=0 ;;
    -h|--help)
      echo "Usage: $0 [--no-build] [--foreground|-f]"
      echo "  Starts TinyLocalCoder (ollama + app API on :8000)."
      echo "  Rebuilds the app image by default (use --no-build to skip)."
      echo "  TUI:  ./cli.sh"
      exit 0
      ;;
  esac
done

echo "Starting TinyLocalCoder…"
if [[ "$BUILD" -eq 1 ]]; then
  echo "Rebuilding app image…"
  "${COMPOSE[@]}" build
fi

if [[ "$DETACH" -eq 1 ]]; then
  "${COMPOSE[@]}" up -d
  echo
  "${COMPOSE[@]}" ps
  echo
  echo "API:    http://localhost:8000/health"
  echo "Ollama: http://localhost:11434"
  echo "TUI:    ./cli.sh"
  echo "Stop:   ./stop-service.sh"
else
  "${COMPOSE[@]}" up
fi
