#!/usr/bin/env bash
# Stop the TinyLocalCoder suite and clear leftover containers that hold ports.
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

REMOVE_VOLUMES=0
for arg in "$@"; do
  case "$arg" in
    --volumes|-v)
      REMOVE_VOLUMES=1
      ;;
    -h|--help)
      echo "Usage: $0 [--volumes|-v]"
      echo "  Stops TinyLocalCoder compose services and leftover run containers."
      echo "  --volumes also removes anonymous/project volumes (NOT the shared model volume)."
      exit 0
      ;;
  esac
done

echo "Stopping TinyLocalCoder compose project…"
# Project name is set in docker-compose.yml (name: tinylocalcoder)
if [[ "$REMOVE_VOLUMES" -eq 1 ]]; then
  "${COMPOSE[@]}" down --remove-orphans -v || true
else
  "${COMPOSE[@]}" down --remove-orphans || true
fi

# Older project name before the TinyLocalCoder rename
if docker compose -p crew_pipeline ps -q 2>/dev/null | grep -q .; then
  echo "Stopping legacy crew_pipeline project…"
  docker compose -p crew_pipeline down --remove-orphans || true
fi

# One-off TUI / run containers that can keep ports or networks busy
echo "Cleaning leftover TinyLocalCoder / crew_pipeline run containers…"
docker ps -aq --filter "name=tinylocalcoder" --filter "name=crew_pipeline" \
  | while read -r id; do
      [[ -z "$id" ]] && continue
      docker stop "$id" >/dev/null 2>&1 || true
      docker rm "$id" >/dev/null 2>&1 || true
    done

echo
echo "Remaining related containers:"
docker ps -a --filter "name=tinylocalcoder" --filter "name=crew_pipeline" \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' \
  || true

echo
echo "Ports 8000 / 11434 should now be free."
echo "Model weights kept in volume: crew_pipeline_ollama_data"
echo "Start again with: ./start-service.sh"
