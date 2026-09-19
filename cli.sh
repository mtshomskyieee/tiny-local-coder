#!/usr/bin/env bash
# Launch the TinyLocalCoder interactive TUI (connects to the running suite).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# shellcheck source=scripts/compose-env.sh
source "$ROOT/scripts/compose-env.sh"

COMPOSE=(docker compose)
if ! docker compose version >/dev/null 2>&1; then
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    echo "error: need 'docker compose' or 'docker-compose'" >&2
    exit 1
  fi
fi

AUTO_SKIP=""
AUTO_FIX=""

usage() {
  cat <<EOF
Usage: $0 [options]

Opens the TinyLocalCoder TUI against the compose stack.
Starts the suite first if Ollama/API are not already up.

Options:
  --auto-skip on|off     Start with auto-skip enabled or disabled (default: on)
  --auto-fix on|off      Start with auto-fix enabled or disabled (default: on)
  -h, --help             Show this help

Inside the TUI you can also toggle at runtime:
  /auto-skip on|off
  /auto-fix on|off
EOF
}

parse_on_off() {
  local name="$1"
  local val="${2:-}"
  case "$val" in
    on|true|1|enable) echo true ;;
    off|false|0|disable) echo false ;;
    *)
      echo "error: $name requires on or off (got: ${val:-<missing>})" >&2
      exit 1
      ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --auto-skip=*)
      AUTO_SKIP="$(parse_on_off --auto-skip "${1#*=}")"
      shift
      ;;
    --auto-skip)
      shift
      AUTO_SKIP="$(parse_on_off --auto-skip "${1:-}")"
      shift
      ;;
    --auto-skip-on)
      AUTO_SKIP=true
      shift
      ;;
    --auto-skip-off)
      AUTO_SKIP=false
      shift
      ;;
    --auto-fix=*)
      AUTO_FIX="$(parse_on_off --auto-fix "${1#*=}")"
      shift
      ;;
    --auto-fix)
      shift
      AUTO_FIX="$(parse_on_off --auto-fix "${1:-}")"
      shift
      ;;
    --auto-fix-on)
      AUTO_FIX=true
      shift
      ;;
    --auto-fix-off)
      AUTO_FIX=false
      shift
      ;;
    *)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

# Ensure backend services are running (Ollama must be reachable from the TUI container).
need_start=0
if ! "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx ollama; then
  need_start=1
elif ! "${COMPOSE[@]}" ps --format '{{.Service}} {{.Status}}' 2>/dev/null | grep -qE '^ollama .*healthy'; then
  # Running but not healthy yet (or unmarked) — still try to bring stack up
  need_start=1
fi
if [[ "$need_start" -eq 1 ]]; then
  echo "Suite not running or Ollama unhealthy — starting with ./start-service.sh …"
  ./start-service.sh
fi

# Hard check: TUI talks to http://ollama:11434 — fail fast if DNS/connect breaks.
if ! "${COMPOSE[@]}" exec -T ollama ollama list >/dev/null 2>&1; then
  echo "error: Ollama is not responding. Try: ./start-service.sh && docker compose logs ollama" >&2
  exit 1
fi
if ! "${COMPOSE[@]}" run --rm --no-deps app python -c \
  "import urllib.request; urllib.request.urlopen('http://ollama:11434/api/tags', timeout=5).read()" \
  >/dev/null 2>&1; then
  echo "error: TUI container cannot reach http://ollama:11434 (name resolution / network)." >&2
  echo "  Fix: ./start-service.sh   then retry ./cli.sh" >&2
  exit 1
fi

RUN_ENV=()
if [[ -n "$AUTO_SKIP" ]]; then
  RUN_ENV+=(-e "AUTO_SKIP=$AUTO_SKIP")
fi
if [[ -n "$AUTO_FIX" ]]; then
  RUN_ENV+=(-e "AUTO_FIX=$AUTO_FIX")
fi

echo "Connecting to TinyLocalCoder TUI…"
echo "  /plan  /code  /execute-plan  /ask   |  F5 = execute-plan  |  Ctrl+C = quit"
if [[ -n "$AUTO_SKIP" || -n "$AUTO_FIX" ]]; then
  echo "  session: auto-skip=${AUTO_SKIP:-default}  auto-fix=${AUTO_FIX:-default}"
fi
echo "  toggle: /auto-skip on|off   /auto-fix on|off"
echo "  ollama: ok (http://ollama:11434)"
# Do not publish host ports — the long-running app service already owns :8000.
# Never expand an empty array under `set -u` (bash 3.2 through 5.x).
if ((${#RUN_ENV[@]})); then
  exec "${COMPOSE[@]}" run --rm -it --no-deps "${RUN_ENV[@]}" app tui
else
  exec "${COMPOSE[@]}" run --rm -it --no-deps app tui
fi
