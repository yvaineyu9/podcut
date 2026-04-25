#!/usr/bin/env bash
# One-command launcher for podcast-cutter.
# Usage:
#   bash ~/.claude/skills/podcast-cutter/scripts/start.sh <path-to-video>

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$SKILL_DIR/.venv/bin/python"

# Video is optional — if not provided, pick one in the browser via the UI.
VIDEO_ABS=""
if [[ $# -ge 1 && -n "$1" ]]; then
  VIDEO="$1"
  if [[ ! -f "$VIDEO" ]]; then
    echo "✗ video not found: $VIDEO" >&2
    exit 1
  fi
  VIDEO_ABS="$(cd "$(dirname "$VIDEO")" && pwd)/$(basename "$VIDEO")"
fi

if [[ ! -x "$VENV_PY" ]]; then
  echo "⚠ venv not set up yet."
  echo "  Run setup first:"
  echo "     bash $SKILL_DIR/scripts/setup.sh"
  echo
  echo "  Starting server with system python anyway — transcribe/cut will fail until setup is done."
  VENV_PY="$(command -v python3)"
fi

PORT="${PORT:-8787}"

if [[ -n "$VIDEO_ABS" ]]; then
  exec "$VENV_PY" "$SKILL_DIR/scripts/serve.py" --video "$VIDEO_ABS" --port "$PORT" --open
else
  exec "$VENV_PY" "$SKILL_DIR/scripts/serve.py" --port "$PORT" --open
fi
