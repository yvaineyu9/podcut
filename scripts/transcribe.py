#!/usr/bin/env python3
"""
Transcribe a video with speaker diarization using WhisperX + pyannote.

Usage:
    transcribe.py <video> [--num-speakers N] [--language zh|en|auto]
                          [--model large-v3|medium|small]
                          [--output path/to/transcript.json]

Writes a transcript JSON next to the video (or to --output if given) with the
schema described in the skill's SKILL.md.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def load_env(env_path: Path) -> None:
    """Load HF_TOKEN (and other vars) from the skill's .env file."""
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def ensure_ffmpeg() -> None:
    if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
        print("✗ ffmpeg not on PATH. Run setup.sh first.", file=sys.stderr)
        sys.exit(1)


def extract_audio(video: Path, out_wav: Path) -> None:
    """Extract 16kHz mono wav — the format WhisperX and pyannote both prefer."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video),
        "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(out_wav),
    ]
    subprocess.run(cmd, check=True)


def probe_duration(path: Path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]).decode().strip()
    return float(out)


def fmt_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video", type=Path, help="Path to input video")
    ap.add_argument("--num-speakers", type=int, default=None,
                    help="Known speaker count. Strongly improves diarization — pass it if you know it.")
    ap.add_argument("--min-speakers", type=int, default=None)
    ap.add_argument("--max-speakers", type=int, default=None)
    ap.add_argument("--language", default="zh",
                    help="'zh', 'en', or 'auto'. Default 'zh' (this skill targets Chinese podcasts).")
    ap.add_argument("--model", default="large-v3",
                    help="Whisper model size. large-v3 = best Chinese accuracy; medium ≈ 3× faster.")
    ap.add_argument("--output", type=Path, default=None,
                    help="Output JSON path. Defaults to <video>.transcript.json")
    ap.add_argument("--batch-size", type=int, default=8)
    args = ap.parse_args(argv)

    video = args.video.resolve()
    if not video.exists():
        print(f"✗ video not found: {video}", file=sys.stderr)
        return 1

    # Load HF token from skill's .env
    skill_dir = Path(__file__).resolve().parent.parent
    load_env(skill_dir / ".env")
    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        print("✗ HF_TOKEN not set. Run setup.sh or add HF_TOKEN to .env.", file=sys.stderr)
        return 1

    ensure_ffmpeg()

    output_path = args.output or video.with_suffix("").with_name(video.stem + ".transcript.json")
    print(f"▶ Video:     {video}")
    print(f"▶ Duration:  {fmt_duration(probe_duration(video))}")
    print(f"▶ Model:     {args.model} · language={args.language}")
    if args.num_speakers:
        print(f"▶ Speakers:  {args.num_speakers} (fixed)")
    print(f"▶ Output:    {output_path}")
    print()

    # Import heavy deps only now (so --help is fast).
    print("⏳ Loading whisperx…")
    import whisperx  # noqa: E402
    import torch  # noqa: E402

    # If we've already pre-downloaded the model (e.g. via ModelScope), use the local
    # directory directly and skip the huggingface_hub fetch entirely.
    local_model = Path.home() / ".cache" / "huggingface" / "hub" / f"models--Systran--faster-whisper-{args.model}" / "snapshots" / "manual"
    if local_model.exists() and (local_model / "model.bin").exists():
        print(f"   → using local model at {local_model}")
        args.model = str(local_model)

    # macOS: faster-whisper/CTranslate2 is CPU-only; pyannote can use MPS for a speedup.
    transcribe_device = "cpu"
    compute_type = "int8"
    diarize_device = "mps" if torch.backends.mps.is_available() else "cpu"

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmpd:
        tmp_wav = Path(tmpd) / "audio.wav"
        print("⏳ Extracting audio (16kHz mono)…")
        extract_audio(video, tmp_wav)

        # --- 1. Whisper transcription ----------------------------------------
        print(f"⏳ Transcribing with whisper-{args.model} on {transcribe_device}… (this is the slow part)")
        asr_model = whisperx.load_model(
            args.model,
            device=transcribe_device,
            compute_type=compute_type,
            language=None if args.language == "auto" else args.language,
        )
        audio = whisperx.load_audio(str(tmp_wav))
        asr_result = asr_model.transcribe(
            audio,
            batch_size=args.batch_size,
            language=None if args.language == "auto" else args.language,
        )
        detected_lang = asr_result.get("language", args.language)
        print(f"   → detected language: {detected_lang}, {len(asr_result['segments'])} raw segments")

        # Free up RAM before loading alignment model
        del asr_model
        import gc; gc.collect()

        # --- 2. Word-level alignment -----------------------------------------
        # WhisperX's alignment gives accurate timestamps needed for diarization merge.
        try:
            print("⏳ Aligning word timestamps…")
            align_model, metadata = whisperx.load_align_model(
                language_code=detected_lang, device=transcribe_device,
            )
            aligned = whisperx.align(
                asr_result["segments"], align_model, metadata, audio,
                device=transcribe_device, return_char_alignments=False,
            )
            del align_model; gc.collect()
        except Exception as e:
            # Alignment models don't exist for every language; we can still diarize on segment timestamps.
            print(f"   ⚠ alignment skipped ({e}); using segment-level timestamps only")
            aligned = asr_result

        # --- 3. Speaker diarization ------------------------------------------
        # Cache the post-Whisper result so if diarization fails we can retry
        # without redoing the 20+ minute Whisper pass.
        cache_path = video.with_name(video.stem + ".whisper-cache.json")
        try:
            cache_path.write_text(json.dumps({
                "language": detected_lang,
                "segments": aligned["segments"] if "segments" in aligned else aligned,
            }, ensure_ascii=False, default=str))
            print(f"   → cached Whisper output to {cache_path.name}")
        except Exception as e:
            print(f"   ⚠ couldn't cache Whisper output: {e}")

        print(f"⏳ Running speaker diarization on {diarize_device}…")
        from whisperx.diarize import DiarizationPipeline, assign_word_speakers
        try:
            # whisperx/pyannote versions differ on the auth kwarg name — try both.
            try:
                diarize_model = DiarizationPipeline(
                    use_auth_token=hf_token, device=diarize_device,
                )
            except TypeError:
                diarize_model = DiarizationPipeline(
                    token=hf_token, device=diarize_device,
                )
            diarize_kwargs = {}
            if args.num_speakers:
                diarize_kwargs["num_speakers"] = args.num_speakers
            else:
                if args.min_speakers:
                    diarize_kwargs["min_speakers"] = args.min_speakers
                if args.max_speakers:
                    diarize_kwargs["max_speakers"] = args.max_speakers
            diarize_segments = diarize_model(str(tmp_wav), **diarize_kwargs)
            final_result = assign_word_speakers(diarize_segments, aligned)
        except Exception as e:
            print(f"   ⚠ diarization failed ({e}); saving transcript without speaker labels — you can relabel in the editor")
            final_result = aligned if isinstance(aligned, dict) and "segments" in aligned else {"segments": aligned}
            for seg in final_result["segments"]:
                seg["speaker"] = "SPEAKER_00"

    # --- Build output JSON --------------------------------------------------
    out_segments = []
    for i, seg in enumerate(final_result["segments"]):
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
        "duration": probe_duration(video),
        "language": detected_lang,
        "num_speakers": len(speakers_seen),
        "speakers": speakers_seen,
        "model": args.model,
        "segments": out_segments,
    }

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    elapsed = time.time() - t0
    print()
    print(f"✅ Done in {fmt_duration(elapsed)}")
    print(f"   {len(out_segments)} segments, {len(speakers_seen)} speakers: {', '.join(speakers_seen)}")
    print(f"   Saved to {output_path}")
    print()
    print("Next:  open ~/.claude/skills/podcast-cutter/editor/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(run())
