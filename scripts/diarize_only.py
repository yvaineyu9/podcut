#!/usr/bin/env python3
"""
Run ONLY pyannote speaker diarization, using a previously cached Whisper transcript.

Usage:
    diarize_only.py <video> [--num-speakers N]

Reads `<video>.whisper-cache.json` (saved by transcribe.py after Whisper completes)
and merges speaker labels onto the existing segments. Writes the final
`<video>.transcript.json`. Skips the ~20-minute Whisper pass entirely.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def load_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def extract_audio(video: Path, out_wav: Path) -> None:
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video),
        "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(out_wav),
    ], check=True)


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video", type=Path)
    ap.add_argument("--num-speakers", type=int, default=None)
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args(argv)

    video = args.video.resolve()
    if not video.exists():
        print(f"✗ video not found: {video}", file=sys.stderr); return 1

    cache_path = video.with_name(video.stem + ".whisper-cache.json")
    if not cache_path.exists():
        print(f"✗ whisper cache not found: {cache_path}", file=sys.stderr)
        print("   Run transcribe.py first.", file=sys.stderr); return 1

    skill_dir = Path(__file__).resolve().parent.parent
    load_env(skill_dir / ".env")
    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        print("✗ HF_TOKEN not set.", file=sys.stderr); return 1

    # Use mirror; user's network can't reach HF proper
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "300")

    output_path = args.output or video.with_name(video.stem + ".transcript.json")

    # Load cached Whisper segments
    cache = json.loads(cache_path.read_text())
    segs = cache.get("segments", [])
    lang = cache.get("language", "zh")
    print(f"▶ Cache:     {cache_path.name}  ({len(segs)} segments, language={lang})")

    # Extract audio from video (needed for pyannote — we don't have it saved)
    t0 = time.time()
    print("⏳ Extracting audio (16kHz mono)…")
    with tempfile.TemporaryDirectory() as tmpd:
        tmp_wav = Path(tmpd) / "audio.wav"
        extract_audio(video, tmp_wav)

        print("⏳ Loading pyannote pipeline…")
        import torch
        from whisperx.diarize import DiarizationPipeline, assign_word_speakers
        diarize_device = "mps" if torch.backends.mps.is_available() else "cpu"
        print(f"   device: {diarize_device}")

        # whisperx/pyannote versions differ on the auth kwarg name
        try:
            diarize_model = DiarizationPipeline(use_auth_token=hf_token, device=diarize_device)
        except TypeError:
            diarize_model = DiarizationPipeline(token=hf_token, device=diarize_device)

        kwargs = {}
        if args.num_speakers:
            kwargs["num_speakers"] = args.num_speakers
            print(f"   num_speakers: {args.num_speakers}")

        print("⏳ Running diarization…")
        diarize_segments = diarize_model(str(tmp_wav), **kwargs)
        aligned = {"segments": segs}
        final = assign_word_speakers(diarize_segments, aligned)

    del diarize_model; gc.collect()

    out_segments = []
    for i, seg in enumerate(final["segments"]):
        out_segments.append({
            "id": i,
            "start": round(float(seg["start"]), 3),
            "end": round(float(seg["end"]), 3),
            "speaker": seg.get("speaker", "UNKNOWN"),
            "text": (seg.get("text") or "").strip(),
        })

    speakers_seen = sorted({s["speaker"] for s in out_segments})
    payload = {
        "video_path": str(video),
        "duration": cache.get("duration") or max((s["end"] for s in out_segments), default=0),
        "language": lang,
        "num_speakers": len(speakers_seen),
        "speakers": speakers_seen,
        "model": cache.get("model", "medium"),
        "segments": out_segments,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    elapsed = time.time() - t0
    print()
    print(f"✅ Done in {int(elapsed)}s")
    print(f"   {len(out_segments)} segments · {len(speakers_seen)} speakers: {', '.join(speakers_seen)}")
    print(f"   → {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
