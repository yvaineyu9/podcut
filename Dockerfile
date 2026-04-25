# =============================================================================
# PodCut — local-first podcast editor
# =============================================================================
# Build:   docker build -t podcut .
# Run:     docker run --rm -it -p 8787:8787 \
#              -v $(pwd)/videos:/data \
#              -v podcut-cache:/root/.cache \
#              -e HF_TOKEN=hf_xxx \
#              podcut
# =============================================================================
#
# Why pin to slim-bookworm?
#   torchcodec 0.7.0 (a hard dep of whisperx + pyannote.audio) ships precompiled
#   native libs that link against libavutil 56–59, i.e. ffmpeg 4–7.
#     - bookworm (Debian 12)  → apt's ffmpeg is 5.1.x  (libavutil 57)  ✅
#     - trixie  (Debian 13)   → apt's ffmpeg is 7.x    (libavutil 59)  ✅
#     - python:3.11-slim (no suffix) drifts to whatever's current — risky.
#   So we pin to slim-bookworm to lock the ffmpeg ABI into the green zone.

FROM python:3.11-slim-bookworm AS base

# System deps. ffmpeg is the version-sensitive one (see above).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Build-time sanity check: confirm ffmpeg is in the torchcodec-supported range.
# (Bails out the build if a future base-image bump silently lands ffmpeg 8+.)
RUN ffmpeg -version | head -1 \
 && ffmpeg_major=$(ffmpeg -version | head -1 | grep -oE 'version [0-9]+' | grep -oE '[0-9]+') \
 && if [ "$ffmpeg_major" -gt 7 ]; then \
        echo "ERROR: ffmpeg $ffmpeg_major is incompatible with torchcodec (need 4–7)" >&2; exit 1; \
    fi \
 && echo "ffmpeg major version $ffmpeg_major — torchcodec compatible ✓"

# -----------------------------------------------------------------------------
# Python deps layer (cached separately so app changes don't bust the big install)
# -----------------------------------------------------------------------------
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip wheel setuptools \
    && pip install --no-cache-dir -r requirements.txt

# Sanity-check imports at build time so we fail fast if pip resolution drifted.
RUN python -c "import whisperx, pyannote.audio, torch, faster_whisper, ctranslate2, torchcodec; \
print('torch', torch.__version__, '· torchcodec', torchcodec.__version__, \
      '· whisperx', whisperx.__version__ if hasattr(whisperx,'__version__') else '?', \
      '· faster_whisper', faster_whisper.__version__)"

# -----------------------------------------------------------------------------
# App layer
# -----------------------------------------------------------------------------
COPY scripts/   scripts/
COPY editor/    editor/
COPY README.md  ./

# In Docker we don't have a .venv; tell serve.py to use the system python.
ENV PODCUT_VENV_PYTHON=/usr/local/bin/python

# Default HF endpoint to the China-friendly mirror; override via -e at runtime.
ENV HF_ENDPOINT=https://hf-mirror.com
ENV HF_HUB_DOWNLOAD_TIMEOUT=300

# Where the user mounts their video files
VOLUME ["/data"]

# Where HF / ModelScope caches live (mount a named volume to persist between runs)
ENV HF_HOME=/root/.cache/huggingface
VOLUME ["/root/.cache"]

EXPOSE 8787

# Default command: start the editor server with no preselected video.
# The user picks one in the browser. To preselect, run:
#   docker run ... podcut python scripts/serve.py --video /data/foo.mp4 --port 8787
CMD ["python", "scripts/serve.py", "--port", "8787"]
