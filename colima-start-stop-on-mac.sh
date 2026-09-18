#!/usr/bin/env bash
# Stop and start Colima with enough RAM for qwen3.5:4b.
# Default Colima is 2 CPU / 2 GB — that is why Ollama hits CPU_REPACK OOM.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

CPU="${TLC_COLIMA_CPU:-4}"
MEMORY="${TLC_COLIMA_MEMORY:-12}"
DISK="${TLC_COLIMA_DISK:-60}"
STOP_SUITE=1
ACTION="restart"

usage() {
  cat <<EOF
Usage: $0 [start|stop|restart|status] [options]

Mac helper for Colima. Default action is restart: stop the TLC suite,
stop Colima, then start Colima with 4 CPUs / 12 GB RAM / 60 GB disk
so qwen3.5:4b can load.

Commands:
  restart   Stop suite + Colima, then start Colima (default)
  start     Start (or restart) Colima with the sized VM
  stop      Stop the TLC suite and Colima
  status    Show colima list / docker memory

Options:
  --cpu N          vCPUs (default: $CPU)
  --memory N       RAM in GB (default: $MEMORY)
  --disk N         Disk in GB (default: $DISK)
  --no-stop-suite  Do not run ./stop-service.sh first
  -h, --help       Show this help

After a successful start:
  ./start-service.sh
EOF
}

die() {
  echo "error: $*" >&2
  exit 1
}

require_macos() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    die "this script is for macOS (uname=$(uname -s))"
  fi
}

require_colima() {
  if ! command -v colima >/dev/null 2>&1; then
    die "colima is not on PATH. Install with: brew install colima docker docker-compose"
  fi
}

stop_suite() {
  if [[ "$STOP_SUITE" -ne 1 ]]; then
    return 0
  fi
  if [[ -x "$ROOT/stop-service.sh" ]]; then
    echo "Stopping TinyLocalCoder…"
    "$ROOT/stop-service.sh" || true
  fi
}

cmd_status() {
  echo "Colima:"
  colima list || true
  echo
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    local bytes gb
    bytes="$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
    gb=$((bytes / 1024 / 1024 / 1024))
    echo "Docker RAM: ${gb} GB"
  else
    echo "Docker is not reachable (Colima is probably stopped)."
  fi
}

cmd_stop() {
  stop_suite
  echo "Stopping Colima…"
  colima stop || true
  colima list || true
}

cmd_start() {
  echo "Starting Colima with --cpu $CPU --memory $MEMORY --disk $DISK…"
  colima start --cpu "$CPU" --memory "$MEMORY" --disk "$DISK"
  echo
  cmd_status
  echo
  echo "Colima is up. Start the suite with: ./start-service.sh"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    start|stop|restart|status)
      ACTION="$1"
      shift
      ;;
    --cpu)
      CPU="${2:-}"
      [[ -n "$CPU" ]] || die "--cpu requires a number"
      shift 2
      ;;
    --memory|--mem)
      MEMORY="${2:-}"
      [[ -n "$MEMORY" ]] || die "--memory requires GB"
      shift 2
      ;;
    --disk)
      DISK="${2:-}"
      [[ -n "$DISK" ]] || die "--disk requires GB"
      shift 2
      ;;
    --no-stop-suite)
      STOP_SUITE=0
      shift
      ;;
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

require_macos
require_colima

case "$ACTION" in
  status) cmd_status ;;
  stop) cmd_stop ;;
  start)
    stop_suite
    # A running VM with the old 2 GB size will not pick up --memory unless we stop first.
    if colima status >/dev/null 2>&1; then
      echo "Colima is already running — stopping so RAM/CPU changes apply…"
      colima stop
    fi
    cmd_start
    ;;
  restart)
    cmd_stop
    cmd_start
    ;;
esac
