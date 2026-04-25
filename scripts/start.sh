#!/usr/bin/env bash
# One-command launcher for podcast-cutter.
# Usage:
#   bash ~/.claude/skills/podcast-cutter/scripts/start.sh <path-to-video>
#   bash ~/.claude/skills/podcast-cutter/scripts/start.sh           # pick video in browser

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$SKILL_DIR/.venv/bin/python"

# --- ffmpeg compatibility ----------------------------------------------------
# torchcodec only works with ffmpeg 4–7. setup.sh installs ffmpeg@7 (keg-only)
# so it doesn't conflict with whatever ffmpeg the user already has on PATH.
# Prepend its bin dir here so transcribe.py / cut.py see the compatible version.
if command -v brew >/dev/null 2>&1; then
  FFMPEG7_PREFIX="$(brew --prefix ffmpeg@7 2>/dev/null || true)"
  if [[ -n "$FFMPEG7_PREFIX" && -x "$FFMPEG7_PREFIX/bin/ffmpeg" ]]; then
    export PATH="$FFMPEG7_PREFIX/bin:$PATH"
  fi
fi

# --- Video arg (optional) ---------------------------------------------------
VIDEO_ABS=""
if [[ $# -ge 1 && -n "$1" ]]; then
  VIDEO="$1"
  if [[ ! -f "$VIDEO" ]]; then
    echo "✗ video not found: $VIDEO" >&2
    exit 1
  fi
  VIDEO_ABS="$(cd "$(dirname "$VIDEO")" && pwd)/$(basename "$VIDEO")"
fi

# --- venv check -------------------------------------------------------------
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
