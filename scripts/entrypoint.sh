#!/bin/sh
set -e

case "${1:-api}" in
  api)
    exec python -m tinylocalcoder.main api
    ;;
  tui)
    exec python -m tinylocalcoder.main tui
    ;;
  *)
    exec "$@"
    ;;
esac
