#!/usr/bin/env python3
"""
Lightweight transcription using mlx-whisper (Apple Silicon native).
No torchcodec, no pyannote, no HF_TOKEN — just fast local transcription.

Usage:
    transcribe_mlx.py <video> [--language zh|en|auto]
                               [--model large-v3|medium|small|base|tiny]
                               [--output path/to/transcript.json]

Speaker diarization is NOT included — label speakers manually in the editor.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def find_ffmpeg() -> str:
    """Find ffmpeg, preferring Homebrew ffmpeg@7 on macOS."""
    if sys.platform == "darwin":
        try:
            prefix = subprocess.check_output(
                ["brew", "--prefix", "ffmpeg@7"],
                stderr=subprocess.DEVNULL, text=True, timeout=3,
            ).strip()
            ff = Path(prefix) / "bin" / "ffmpeg"
            if ff.exists():
                return str(ff)
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
    return shutil.which("ffmpeg") or "ffmpeg"


def find_ffprobe() -> str:
    if sys.platform == "darwin":
        try:
            prefix = subprocess.check_output(
                ["brew", "--prefix", "ffmpeg@7"],
                stderr=subprocess.DEVNULL, text=True, timeout=3,
            ).strip()
            fp = Path(prefix) / "bin" / "ffprobe"
            if fp.exists():
                return str(fp)
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
    return shutil.which("ffprobe") or "ffprobe"


def probe_duration(path: Path) -> float:
    out = subprocess.check_output([
        find_ffprobe(), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]).decode().strip()
    return float(out)


def extract_audio(video: Path, out_wav: Path) -> None:
    cmd = [
        find_ffmpeg(), "-y", "-loglevel", "error",
        "-i", str(video),
        "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(out_wav),
    ]
    subprocess.run(cmd, check=True)


def fmt_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


# Map friendly names to HuggingFace model IDs for mlx-whisper
MODEL_MAP = {
    "tiny":     "mlx-community/whisper-tiny-mlx",
    "base":     "mlx-community/whisper-base-mlx",
    "small":    "mlx-community/whisper-small-mlx",
    "medium":   "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "turbo":    "mlx-community/whisper-turbo",
}


def find_local_model(model_id: str) -> str | None:
    """Check if an mlx-whisper model is already downloaded locally."""
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
    repo_dir = cache_dir / f"models--{model_id.replace('/', '--')}"
    # Check snapshots/main (manual curl download)
    snap_main = repo_dir / "snapshots" / "main"
    if (snap_main / "weights.npz").exists() and (snap_main / "config.json").exists():
        return str(snap_main)
    # Check snapshots/<hash> (huggingface_hub download)
    snaps = repo_dir / "snapshots"
    if snaps.exists():
        for d in snaps.iterdir():
            if d.is_dir() and (d / "weights.npz").exists() and (d / "config.json").exists():
                return str(d)
    return None


def merge_segments(segments: list[dict], max_gap: float = 0.5, max_dur: float = 18.0) -> list[dict]:
    """Merge adjacent segments into larger blocks for easier editing."""
    if not segments:
        return []
    merged: list[dict] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = round(float(seg["start"]), 3)
        end = round(float(seg["end"]), 3)
        if merged and (start - merged[-1]["end"]) <= max_gap and (end - merged[-1]["start"]) <= max_dur:
            merged[-1]["end"] = end
            merged[-1]["text"] = merged[-1]["text"] + " " + text
        else:
            merged.append({"start": start, "end": end, "text": text})
    for i, seg in enumerate(merged):
        seg["id"] = i
        seg["speaker"] = "SPEAKER_00"
    return merged


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video", type=Path)
    ap.add_argument("--language", default="zh")
    ap.add_argument("--model", default="large-v3",
                    choices=list(MODEL_MAP.keys()),
                    help="Model size. large-v3 = best accuracy, turbo = fastest.")
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--merge-gap", type=float, default=0.5)
    ap.add_argument("--max-merged-duration", type=float, default=18.0)
    args = ap.parse_args(argv)

    video = args.video.resolve()
    if not video.exists():
        print(f"✗ video not found: {video}", file=sys.stderr)
        return 1

    output_path = args.output or video.with_suffix("").with_name(video.stem + ".transcript.json")
    model_id = MODEL_MAP.get(args.model, args.model)
    duration = probe_duration(video)

    print(f"▶ Video:     {video.name}")
    print(f"▶ Duration:  {fmt_duration(duration)}")
    print(f"▶ Model:     {args.model} (mlx-whisper)")
    print(f"▶ Language:  {args.language}")
    print(f"▶ Output:    {output_path}")
    print()

    print("⏳ Loading whisperx… (mlx-whisper)")
    # Bypass local proxy — huggingface_hub chokes on proxies that rewrite headers
    import os as _os
    for _pk in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        _os.environ.pop(_pk, None)
    _os.environ["HF_HUB_DISABLE_XET"] = "1"
    _os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
    import mlx_whisper

    t0 = time.time()

    with tempfile.TemporaryDirectory() as tmpd:
        tmp_wav = Path(tmpd) / "audio.wav"
        print("⏳ Extracting audio…")
        extract_audio(video, tmp_wav)

        print(f"⏳ Transcribing with {args.model} on Apple Silicon…")
        language = None if args.language == "auto" else args.language

        # Initial prompt for Chinese
        initial_prompt = None
        if language and language.startswith("zh"):
            initial_prompt = (
                "以下是中文播客访谈转写，请使用简体中文，"
                "保留口语表达，不要翻译成英文。"
            )

        # Use local model if available (avoids network issues)
        local_path = find_local_model(model_id)
        effective_model = local_path or model_id
        if local_path:
            print(f"   → using local model: {local_path}")
        else:
            print(f"   → downloading model: {model_id}")

        result = mlx_whisper.transcribe(
            str(tmp_wav),
            path_or_hf_repo=effective_model,
            language=language,
            initial_prompt=initial_prompt,
            word_timestamps=True,
            verbose=False,
        )

    detected_lang = result.get("language", args.language)
    raw_segments = result.get("segments", [])
    print(f"   → detected language: {detected_lang}, {len(raw_segments)} raw segments")

    # Merge into reasonable blocks
    merged = merge_segments(
        raw_segments,
        max_gap=args.merge_gap,
        max_dur=args.max_merged_duration,
    )

    payload = {
        "video_path": str(video),
        "duration": duration,
        "language": detected_lang,
        "num_speakers": 1,
        "speakers": ["SPEAKER_00"],
        "model": f"mlx-whisper/{args.model}",
        "segments": merged,
    }

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    elapsed = time.time() - t0
    print()
    print(f"✅ Done in {fmt_duration(elapsed)}")
    print(f"   {len(raw_segments)} raw → {len(merged)} merged segments")
    print(f"   Saved to {output_path}")
    print()
    print("Note: No speaker diarization — all segments labeled SPEAKER_00.")
    print("      Rename speakers manually in the editor.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
