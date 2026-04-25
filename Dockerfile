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

FROM python:3.11-slim AS base

# System deps: ffmpeg for audio extraction + cut/concat, git for pip-from-git installs
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# Python deps layer (cached separately so app changes don't bust the big install)
# -----------------------------------------------------------------------------
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip wheel setuptools \
    && pip install --no-cache-dir -r requirements.txt

# -----------------------------------------------------------------------------
# App layer
# -----------------------------------------------------------------------------
COPY scripts/   scripts/
COPY editor/    editor/
COPY SKILL.md   ./
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

# Sanity-check imports at build time so we fail fast if pip resolution drifted
RUN python -c "import whisperx, pyannote.audio, torch; print('imports OK')"

# Default command: start the editor server with no preselected video.
# The user picks one in the browser. To preselect, run:
#   docker run ... podcut python scripts/serve.py --video /data/foo.mp4 --port 8787
CMD ["python", "scripts/serve.py", "--port", "8787"]
