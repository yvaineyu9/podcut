#!/usr/bin/env bash
# Sets up podcast-cutter: python@3.11 + ffmpeg + whisperx + pyannote.
# Idempotent — safe to re-run.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$SKILL_DIR/.venv"
ENV_FILE="$SKILL_DIR/.env"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
info() { printf "  \033[2m→\033[0m %s\n" "$*"; }
warn() { printf "\033[33m⚠ %s\033[0m\n" "$*"; }
ok()   { printf "\033[32m✓ %s\033[0m\n" "$*"; }
err()  { printf "\033[31m✗ %s\033[0m\n" "$*" >&2; }

bold "📦 podcast-cutter setup"
echo

# --- Homebrew ---------------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
  bold "Installing Homebrew"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Pick up brew on Apple Silicon
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
else
  ok "Homebrew present ($(brew --version | head -1))"
fi

# --- System deps ------------------------------------------------------------
bold "Installing python@3.11 and ffmpeg"
for pkg in python@3.11 ffmpeg; do
  if brew list --versions "$pkg" >/dev/null 2>&1; then
    ok "$pkg already installed"
  else
    info "brew install $pkg"
    brew install "$pkg"
  fi
done

PYTHON_BIN="$(brew --prefix python@3.11)/bin/python3.11"
if [[ ! -x "$PYTHON_BIN" ]]; then
  err "python@3.11 not found at $PYTHON_BIN after install"
  exit 1
fi
ok "python: $PYTHON_BIN ($($PYTHON_BIN --version))"
ok "ffmpeg: $(ffmpeg -version | head -1)"

# --- Virtualenv -------------------------------------------------------------
bold "Creating virtualenv at $VENV_DIR"
if [[ ! -d "$VENV_DIR" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  ok "venv created"
else
  ok "venv already exists"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip wheel setuptools

# --- Python deps ------------------------------------------------------------
bold "Installing torch (CPU build — MPS fallback handled at runtime)"
# whisperx's faster-whisper backend is CPU-only on macOS; pyannote uses torch directly and can do MPS.
pip install --quiet "torch>=2.2" "torchaudio>=2.2"

bold "Installing whisperx + pyannote"
# whisperx pins specific versions; let it resolve.
pip install --quiet "whisperx>=3.1.1"

# Sanity imports
python - <<'PY'
import importlib, sys
mods = ["whisperx", "pyannote.audio", "torch"]
for m in mods:
    try:
        importlib.import_module(m)
        print(f"  ✓ import {m}")
    except Exception as e:
        print(f"  ✗ import {m}: {e}", file=sys.stderr)
        sys.exit(1)
PY

ok "Python packages installed"

# --- HuggingFace token ------------------------------------------------------
bold "HuggingFace token (for pyannote speaker diarization)"
if [[ -f "$ENV_FILE" ]] && grep -q "^HF_TOKEN=" "$ENV_FILE"; then
  ok ".env already has HF_TOKEN"
else
  cat <<EOF

  pyannote.audio needs a free HuggingFace token. Steps:

    1.  Register at  https://huggingface.co/
    2.  Accept terms at BOTH of these model pages (click 'Agree and access'):
          https://huggingface.co/pyannote/speaker-diarization-3.1
          https://huggingface.co/pyannote/segmentation-3.0
    3.  Create a 'read' token at  https://huggingface.co/settings/tokens

EOF
  read -r -p "  Paste your HuggingFace token (starts with 'hf_'): " HF_TOKEN
  if [[ -z "$HF_TOKEN" ]]; then
    warn "No token entered — you can add one later by editing $ENV_FILE"
  else
    umask 077
    printf "HF_TOKEN=%s\n" "$HF_TOKEN" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    ok "Token saved to $ENV_FILE (chmod 600)"
  fi
fi

# --- Done -------------------------------------------------------------------
echo
bold "✅ Setup complete"
echo
cat <<EOF
Next steps:

  1. Transcribe a video (first run downloads ~3 GB of models):
     $VENV_DIR/bin/python $SKILL_DIR/scripts/transcribe.py <video.mp4> --num-speakers 2

  2. Open the editor:
     open $SKILL_DIR/editor/index.html

  3. After marking segments and exporting selections.json:
     $VENV_DIR/bin/python $SKILL_DIR/scripts/cut.py <video.mp4> <selections.json>
     $VENV_DIR/bin/python $SKILL_DIR/scripts/extract.py <selections.json>

EOF
