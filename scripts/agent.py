#!/usr/bin/env python3
"""
PodCut Agent — CLI interface for podcast editing.

Commands:
    auto        Transcribe + print full transcript for Claude to analyze
    transcript  Dump transcript for manual review
    cut         Apply selections.json and export final audio/video
    extract     Extract highlights / kept transcript

Usage:
    # Transcribe and print transcript (Claude reads it and decides cuts)
    python scripts/agent.py auto <audio_file> [--language zh] [--model small]

    # Step-by-step (advanced)
    python scripts/agent.py transcript <audio_file>
    python scripts/agent.py cut <audio_file> <selections.json> [--fade 0.3]
    python scripts/agent.py extract <selections.json>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent


# ============================================================================
# Utilities
# ============================================================================

def _ffmpeg_path() -> str:
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
    ff7 = Path("/opt/homebrew/opt/ffmpeg@7/bin/ffmpeg")
    if ff7.exists():
        return str(ff7)
    return "ffmpeg"


def _ffprobe_path() -> str:
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
    ff7 = Path("/opt/homebrew/opt/ffmpeg@7/bin/ffprobe")
    if ff7.exists():
        return str(ff7)
    return "ffprobe"


def _fmt_dur(seconds: float) -> str:
    """Format seconds as M:SS or H:MM:SS."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _probe_duration(path: Path) -> float:
    """Get media duration via ffprobe."""
    out = subprocess.check_output([
        _ffprobe_path(), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]).decode().strip()
    return float(out)


def _is_audio_only(path: Path) -> bool:
    """Check if a file is audio-only (no video streams)."""
    try:
        out = subprocess.check_output([
            _ffprobe_path(), "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]).decode().strip()
        return len(out) == 0
    except subprocess.SubprocessError:
        # If ffprobe fails, guess from extension
        return path.suffix.lower() in (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".wma")


# ============================================================================
# Transcription (using transcribe_mlx.py as subprocess)
# ============================================================================

def _run_transcribe(audio: Path, language: str, model: str) -> Path:
    """Run transcribe_mlx.py and return the transcript path."""
    transcript_path = audio.with_suffix("").with_name(audio.stem + ".transcript.json")

    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "transcribe_mlx.py"),
        str(audio),
        "--language", language,
        "--model", model,
        "--output", str(transcript_path),
    ]

    print(f"[1/4] Transcribing with mlx-whisper ({model})...")
    print(f"       command: {' '.join(cmd)}")
    print()

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            print(f"       {line}")
    proc.wait()

    if proc.returncode != 0:
        print(f"\nError: transcribe_mlx.py exited with code {proc.returncode}", file=sys.stderr)
        sys.exit(1)

    if not transcript_path.exists():
        print(f"\nError: Expected transcript at {transcript_path} but file not found", file=sys.stderr)
        sys.exit(1)

    print()
    return transcript_path


# ============================================================================
# Command: auto (full pipeline)
# ============================================================================

def cmd_auto(
    audio_path: str,
    language: str = "zh",
    model: str = "large-v3",
):
    """Transcribe audio/video and print full transcript for Claude to analyze."""
    audio = Path(audio_path).resolve()
    if not audio.exists():
        print(f"Error: {audio} not found", file=sys.stderr)
        sys.exit(1)

    is_audio = _is_audio_only(audio)
    media_type = "audio" if is_audio else "video"
    duration = _probe_duration(audio)

    print("=" * 60)
    print("  PodCut — Transcribe & Print")
    print("=" * 60)
    print(f"  File:       {audio.name}")
    print(f"  Type:       {media_type}")
    print(f"  Duration:   {_fmt_dur(duration)}")
    print(f"  Language:   {language}")
    print(f"  Model:      {model} (mlx-whisper)")
    print("=" * 60)
    print()

    # ---- Step 1: Transcribe ----
    transcript_path = audio.with_suffix("").with_name(audio.stem + ".transcript.json")
    if transcript_path.exists():
        print(f"[1/2] Transcript already exists: {transcript_path.name}")
        print(f"       Skipping transcription (delete the file to re-transcribe)")
        print()
    else:
        transcript_path = _run_transcribe(audio, language, model)

    # ---- Step 2: Print full transcript ----
    print(f"[2/2] Reading transcript...")
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript_data = json.load(f)

    segments = transcript_data.get("segments", [])
    total_duration = transcript_data.get("duration", duration)
    print(f"       {len(segments)} segments, {_fmt_dur(total_duration)} total")
    print()

    # Print FULL transcript for Claude to read and make editing decisions
    print("=" * 60)
    print("  FULL TRANSCRIPT")
    print("=" * 60)
    print(f"# File: {audio.name}")
    print(f"# Duration: {_fmt_dur(total_duration)}")
    print(f"# Segments: {len(segments)}")
    print(f"# Format: [ID] [START-END] [SPEAKER] text")
    print()

    for seg in segments:
        sid = seg["id"]
        start = seg["start"]
        end = seg["end"]
        speaker = seg.get("speaker", "?")
        text = (seg.get("text") or "").strip()
        start_str = f"{int(start // 60):02d}:{start % 60:05.2f}"
        end_str = f"{int(end // 60):02d}:{end % 60:05.2f}"
        print(f"[{sid}] [{start_str}-{end_str}] [{speaker}] {text}")

    print()
    print("=" * 60)
    print("  NEXT STEPS")
    print("=" * 60)
    print()
    print("请分析以上转录稿，决定每段的标记（cut/highlight/无标签），")
    print("然后写入 selections.json 并调用以下命令执行剪辑：")
    print()
    print(f"  python3 {SCRIPT_DIR / 'agent.py'} cut '{audio}' '<selections.json>'")
    print()
    print("selections.json 格式：")
    print('  {')
    print(f'    "video_path": "{audio}",')
    print('    "segments": [')
    print('      {"id": 0, "start": 0.0, "end": 3.5, "speaker": "...", "text": "...", "tags": ["cut"]},')
    print('      {"id": 1, "start": 3.5, "end": 12.0, "speaker": "...", "text": "...", "tags": []},')
    print('      {"id": 2, "start": 12.0, "end": 18.0, "speaker": "...", "text": "...", "tags": ["highlight"]}')
    print('    ]')
    print('  }')
    print()
    print("标记说明：")
    print('  "cut"       — 删除该段')
    print('  "highlight"  — 金句（保留并标记）')
    print('  []           — 普通保留')
    print()
    print(f"  Transcript: {transcript_path}")
    print()


def _cut_audio_fast(audio: Path, segments: list[dict], output_path: Path):
    """Cut audio using ffmpeg stream copy (fast, no re-encoding)."""
    ffmpeg = _ffmpeg_path()

    keep_segments = [s for s in segments if "cut" not in s.get("tags", [])]
    if not keep_segments:
        print("Error: No segments to keep!", file=sys.stderr)
        sys.exit(1)

    # Merge adjacent kept segments into continuous spans
    spans: list[tuple[float, float]] = []
    for seg in keep_segments:
        start = float(seg["start"])
        end = float(seg["end"])
        if spans and abs(start - spans[-1][1]) < 0.1:
            spans[-1] = (spans[-1][0], end)
        else:
            spans.append((start, end))

    print(f"       {len(keep_segments)} segments -> {len(spans)} spans")

    tmp_dir = Path("/tmp/podcut_agent_cuts")
    tmp_dir.mkdir(exist_ok=True)

    part_files = []
    for i, (start, end) in enumerate(spans):
        part_path = tmp_dir / f"part_{i:04d}{audio.suffix}"
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-i", str(audio),
            "-ss", str(start), "-to", str(end),
            "-c", "copy",
            str(part_path),
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        part_files.append(part_path)

    # Concat all parts
    concat_file = tmp_dir / "concat.txt"
    with open(concat_file, "w") as f:
        for p in part_files:
            f.write(f"file '{p}'\n")

    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"       ffmpeg error: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Cleanup
    for p in part_files:
        p.unlink(missing_ok=True)
    concat_file.unlink(missing_ok=True)

    print(f"       Output: {output_path}")


# ============================================================================
# Command: transcript (existing, improved)
# ============================================================================

def cmd_transcript(audio_path: str):
    """Dump transcript in a Claude-friendly format for content analysis."""
    audio = Path(audio_path).resolve()
    if not audio.exists():
        print(f"Error: {audio} not found", file=sys.stderr)
        sys.exit(1)

    # Look for existing transcript
    transcript_path = audio.with_suffix("").with_name(audio.stem + ".transcript.json")
    if not transcript_path.exists():
        transcript_path = Path(str(audio) + ".transcript.json")
    if not transcript_path.exists():
        print(f"Error: No transcript found. Expected: {audio.stem}.transcript.json", file=sys.stderr)
        print("Run: python scripts/agent.py auto <file>  (or transcribe_mlx.py first)", file=sys.stderr)
        sys.exit(1)

    with open(transcript_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    duration = data.get("duration", 0)
    segments = data.get("segments", [])

    print(f"# Podcast Transcript")
    print(f"# File: {audio.name}")
    print(f"# Duration: {_fmt_dur(duration)}")
    print(f"# Segments: {len(segments)}")
    print(f"# Speakers: {data.get('num_speakers', 'unknown')}")
    print()
    print("# Format: [ID] [START-END] [SPEAKER] text")
    print("# ---")
    print()

    for seg in segments:
        sid = seg["id"]
        start = seg["start"]
        end = seg["end"]
        speaker = seg.get("speaker", "?")
        text = seg.get("text", "").strip()
        start_str = f"{int(start // 60):02d}:{start % 60:05.2f}"
        end_str = f"{int(end // 60):02d}:{end % 60:05.2f}"
        print(f"[{sid}] [{start_str}-{end_str}] [{speaker}] {text}")


# ============================================================================
# Command: cut (existing, improved)
# ============================================================================

def cmd_cut(audio_path: str, selections_path: str, fade: float = 0.0):
    """Cut audio/video based on selections.json."""
    audio = Path(audio_path).resolve()
    selections = Path(selections_path).resolve()

    if not audio.exists():
        print(f"Error: {audio} not found", file=sys.stderr)
        sys.exit(1)
    if not selections.exists():
        print(f"Error: {selections} not found", file=sys.stderr)
        sys.exit(1)

    is_audio = _is_audio_only(audio)

    if not is_audio:
        # Use cut.py for video (re-encodes for clean boundaries)
        output_path = audio.with_name(audio.stem + ".final.mp4")
        cmd = [
            sys.executable,
            str(SCRIPT_DIR / "cut.py"),
            str(audio),
            str(selections),
            "--output", str(output_path),
            "--fade", str(fade),
        ]
        print(f"Running cut.py for video...")
        result = subprocess.run(cmd, text=True)
        if result.returncode != 0:
            sys.exit(1)
        return str(output_path)

    # Audio: fast stream-copy cut
    with open(selections, "r", encoding="utf-8") as f:
        data = json.load(f)

    segments = data.get("segments", [])
    keep_segments = [s for s in segments if "cut" not in s.get("tags", [])]

    if not keep_segments:
        print("Error: No segments to keep!", file=sys.stderr)
        sys.exit(1)

    spans: list[tuple[float, float]] = []
    for seg in keep_segments:
        start = float(seg["start"])
        end = float(seg["end"])
        if spans and abs(start - spans[-1][1]) < 0.1:
            spans[-1] = (spans[-1][0], end)
        else:
            spans.append((start, end))

    print(f"Keeping {len(keep_segments)} segments in {len(spans)} spans")
    total_keep = sum(e - s for s, e in spans)
    total_orig = float(segments[-1]["end"]) if segments else 0
    print(f"Duration: {_fmt_dur(total_orig)} -> {_fmt_dur(total_keep)}")

    ffmpeg = _ffmpeg_path()
    tmp_dir = Path("/tmp/podcut_agent_cuts")
    tmp_dir.mkdir(exist_ok=True)

    part_files = []
    for i, (start, end) in enumerate(spans):
        part_path = tmp_dir / f"part_{i:04d}{audio.suffix}"
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-i", str(audio),
            "-ss", str(start), "-to", str(end),
            "-c", "copy", str(part_path),
        ]
        subprocess.run(cmd, capture_output=True)
        part_files.append(part_path)

    concat_file = tmp_dir / "concat.txt"
    with open(concat_file, "w") as f:
        for p in part_files:
            f.write(f"file '{p}'\n")

    output_path = audio.with_name(audio.stem + "_final" + audio.suffix)
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy", str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"ffmpeg error: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    for p in part_files:
        p.unlink(missing_ok=True)
    concat_file.unlink(missing_ok=True)

    print(f"Output: {output_path}")
    return str(output_path)


# ============================================================================
# Command: extract (existing)
# ============================================================================

def cmd_extract(selections_path: str):
    """Extract highlights and kept transcript for social media use."""
    with open(selections_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    segments = data.get("segments", [])
    highlights = [s for s in segments if "highlight" in s.get("tags", [])]
    kept = [s for s in segments if "cut" not in s.get("tags", [])]

    print("# Highlights\n")
    for s in highlights:
        start = s.get("start", 0)
        print(f"- [{_fmt_dur(start)}] {s['text'].strip()}")

    print(f"\n\n# Full Kept Transcript ({len(kept)} segments)\n")
    for s in kept:
        print(f"{s['text'].strip()}")


# ============================================================================
# CLI entry point
# ============================================================================

def _parse_args():
    """Parse CLI arguments with argparse for the auto command, fallback for legacy commands."""
    import argparse

    parser = argparse.ArgumentParser(
        description="PodCut Agent — automated podcast editing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # auto
    p_auto = subparsers.add_parser("auto", help="Transcribe + print full transcript for Claude to analyze")
    p_auto.add_argument("audio", type=str, help="Audio/video file path")
    p_auto.add_argument("--language", default="zh", help="Language code (default: zh)")
    p_auto.add_argument("--model", default="large-v3",
                        choices=["tiny", "base", "small", "medium", "large-v3", "turbo"],
                        help="Whisper model size (default: large-v3)")

    # transcript
    p_trans = subparsers.add_parser("transcript", help="Dump transcript for manual review")
    p_trans.add_argument("audio", type=str, help="Audio/video file path")

    # cut
    p_cut = subparsers.add_parser("cut", help="Apply selections and export final audio/video")
    p_cut.add_argument("audio", type=str, help="Audio/video file path")
    p_cut.add_argument("selections", type=str, help="Path to selections.json")
    p_cut.add_argument("--fade", type=float, default=0.0, help="Crossfade duration")

    # extract
    p_extract = subparsers.add_parser("extract", help="Extract highlights from selections.json")
    p_extract.add_argument("selections", type=str, help="Path to selections.json")

    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if args.command == "auto":
        cmd_auto(
            args.audio,
            language=args.language,
            model=args.model,
        )
    elif args.command == "transcript":
        cmd_transcript(args.audio)
    elif args.command == "cut":
        cmd_cut(args.audio, args.selections, fade=args.fade)
    elif args.command == "extract":
        cmd_extract(args.selections)
    else:
        print(__doc__)
        sys.exit(1)
