#!/usr/bin/env bash
# Sets up podcast-cutter: python@3.11 + ffmpeg@7 + whisperx + pyannote.
# Idempotent — safe to re-run.
#
# IMPORTANT: We install ffmpeg@7, NOT ffmpeg. torchcodec (a hard dep of
# whisperx + pyannote) ships precompiled libs that only support ffmpeg 4–7
# (libavutil 56–59). ffmpeg 8 silently breaks audio loading. ffmpeg@7 is
# keg-only, so it coexists with whatever ffmpeg the user already has —
# start.sh prepends it to PATH at runtime.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$SKILL_DIR/.venv"
ENV_FILE="$SKILL_DIR/.env"
REQUIREMENTS="$SKILL_DIR/requirements.txt"

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
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
else
  ok "Homebrew present ($(brew --version | head -1))"
fi

# --- System deps ------------------------------------------------------------
bold "Installing python@3.11 and ffmpeg@7"
info "Note: we install ffmpeg@7 (not the latest ffmpeg) because torchcodec only"
info "      supports ffmpeg 4–7. ffmpeg@7 is keg-only and won't replace your existing ffmpeg."
for pkg in python@3.11 ffmpeg@7; do
  if brew list --versions "$pkg" >/dev/null 2>&1; then
    ok "$pkg already installed ($(brew list --versions "$pkg" | head -1))"
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

FFMPEG7_PREFIX="$(brew --prefix ffmpeg@7)"
FFMPEG7_BIN="$FFMPEG7_PREFIX/bin/ffmpeg"
if [[ ! -x "$FFMPEG7_BIN" ]]; then
  err "ffmpeg@7 not found at $FFMPEG7_BIN after install"
  exit 1
fi
ok "ffmpeg: $FFMPEG7_BIN ($($FFMPEG7_BIN -version | head -1 | cut -d' ' -f1-3))"

# Sanity check: confirm libavutil major version is in the supported range.
LIBAVUTIL_MAJOR="$($FFMPEG7_BIN -version | grep -oE 'libavutil *[0-9]+' | grep -oE '[0-9]+' | head -1)"
if [[ -n "$LIBAVUTIL_MAJOR" ]] && (( LIBAVUTIL_MAJOR > 59 )); then
  warn "ffmpeg@7's libavutil is $LIBAVUTIL_MAJOR — torchcodec needs ≤ 59. Something is wrong."
elif [[ -n "$LIBAVUTIL_MAJOR" ]]; then
  ok "libavutil major: $LIBAVUTIL_MAJOR (torchcodec needs ≤ 59)"
fi

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

# --- Python deps from pinned requirements.txt -------------------------------
if [[ -f "$REQUIREMENTS" ]]; then
  bold "Installing pinned Python deps from requirements.txt"
  pip install --quiet -r "$REQUIREMENTS"
else
  warn "requirements.txt not found — falling back to loose install"
  pip install --quiet "torch>=2.2" "torchaudio>=2.2" "whisperx>=3.1.1"
fi

# Sanity imports
python - <<'PY'
import importlib, sys
mods = ["whisperx", "pyannote.audio", "torch", "faster_whisper", "ctranslate2"]
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
          https://huggingface.co/pyannote/speaker-diarization-community-1
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

  1. Launch the editor (will use ffmpeg@7 from Homebrew automatically):
     bash $SKILL_DIR/scripts/start.sh <video.mp4>

  2. Or run the CLI scripts directly:
     $VENV_DIR/bin/python $SKILL_DIR/scripts/transcribe.py <video.mp4> --num-speakers 2
     $VENV_DIR/bin/python $SKILL_DIR/scripts/cut.py        <video.mp4> <selections.json>
     $VENV_DIR/bin/python $SKILL_DIR/scripts/extract.py    <selections.json>

EOF
