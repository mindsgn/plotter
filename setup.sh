#!/usr/bin/env bash
# Install or run the LY Drawbot terminal plotter.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

usage() {
  echo "Usage: $0 [install|run|test]"
  echo "  install  Create .venv and install dependencies (default)"
  echo "  run      Install if needed, then start the TUI"
  echo "  test     Install if needed, then run unit tests"
}

ensure_venv() {
  if [[ ! -d "$ROOT/.venv" ]]; then
    python3 -m venv "$ROOT/.venv"
  fi
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
  python -m pip install --upgrade pip
  python -m pip install -r "$ROOT/requirements.txt"
}

cmd="${1:-install}"
case "$cmd" in
  -h|--help|help)
    usage
    ;;
  install)
    ensure_venv
    echo "Installed. Run: $0 run"
    ;;
  run)
    ensure_venv
    exec python -m lyplotter
    ;;
  test)
    ensure_venv
    exec python -m pytest "$ROOT/tests" -q
    ;;
  *)
    usage
    exit 1
    ;;
esac
