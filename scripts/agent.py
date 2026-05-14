#!/usr/bin/env python3
"""
PodCut Agent — CLI interface for automated podcast editing.

Commands:
    auto        Full pipeline: transcribe → tag → cut → done
    transcript  Dump transcript for manual review
    cut         Apply selections.json and export final audio/video
    extract     Extract highlights / kept transcript

Usage:
    # One-shot automatic edit (primary workflow)
    python scripts/agent.py auto <audio_file> [--language zh] [--model small] [--keep-ratio 0.7]

    # Step-by-step (advanced)
    python scripts/agent.py transcript <audio_file>
    python scripts/agent.py cut <audio_file> <selections.json> [--fade 0.3]
    python scripts/agent.py extract <selections.json>
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
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
# Suggestion algorithm (duplicated from serve.py to work standalone)
# ============================================================================

_FILLER_RE = re.compile(
    r"^[嗯啊哦呃唔对好是的哈呵呀嘿噢ok\s,.，。、!?！？\-—…]*$",
    re.IGNORECASE,
)
_EMPHASIS = [
    "其实", "但是", "不过", "最重要", "最关键", "核心", "关键", "一句话",
    "记住", "必须", "千万", "真的", "绝对", "归根结底", "说白了", "本质",
]


def suggest_tags(
    segments: list[dict],
    speaker_weights: dict[str, float],
    target_ratio: float,
    strip_fillers: bool,
) -> list[dict]:
    """Score each segment, then greedily cut low-scorers until compression hits target.

    Returns [{id, tags: [cut? highlight?]}] for segments that got a suggestion.
    """
    total = sum(float(s["end"]) - float(s["start"]) for s in segments) or 1.0
    scored: list[tuple[dict, float]] = []

    for s in segments:
        dur = float(s["end"]) - float(s["start"])
        text = (s.get("text") or "").strip()
        w = float(speaker_weights.get(s.get("speaker", ""), 1.0))
        score = w

        if strip_fillers and _FILLER_RE.match(text) and dur < 2.5:
            score *= 0.04
        elif dur < 1.2 and len(text) < 8:
            score *= 0.35

        if any(e in text for e in _EMPHASIS):
            score *= 1.35

        if dur > 8 and len(text) > 30:
            score *= 1.2

        # Slight penalty for host-like "mm-hmm" backchanneling even with full weight
        if len(text) < 4 and dur < 1.0:
            score *= 0.3

        scored.append((s, score))

    # Target cut duration
    target_keep_dur = total * max(0.05, min(0.99, target_ratio))
    target_cut_dur = total - target_keep_dur

    # Cut from lowest-scoring segments
    scored_asc = sorted(scored, key=lambda x: x[1])
    cuts: set[int] = set()
    cut_dur = 0.0
    for s, _ in scored_asc:
        if cut_dur >= target_cut_dur:
            break
        cuts.add(s["id"])
        cut_dur += float(s["end"]) - float(s["start"])

    # Highlights: top ~5% by score among non-cut segments, min duration 3s
    keepers = [
        (s, sc)
        for (s, sc) in scored
        if s["id"] not in cuts and (float(s["end"]) - float(s["start"])) >= 3.0
    ]
    keepers.sort(key=lambda x: -x[1])
    n_highlights = max(3, len(keepers) // 20)
    highlights = {s["id"] for s, _ in keepers[:n_highlights]}

    out: list[dict] = []
    for s in segments:
        tags: list[str] = []
        if s["id"] in cuts:
            tags.append("cut")
        if s["id"] in highlights:
            tags.append("highlight")
        if tags:
            out.append({"id": s["id"], "tags": tags})
    return out


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
    keep_ratio: float = 0.7,
    fade: float = 0.15,
):
    """Full automated pipeline: transcribe -> tag -> cut -> done."""
    audio = Path(audio_path).resolve()
    if not audio.exists():
        print(f"Error: {audio} not found", file=sys.stderr)
        sys.exit(1)

    is_audio = _is_audio_only(audio)
    media_type = "audio" if is_audio else "video"
    duration = _probe_duration(audio)

    print("=" * 60)
    print("  PodCut Auto Pipeline")
    print("=" * 60)
    print(f"  File:       {audio.name}")
    print(f"  Type:       {media_type}")
    print(f"  Duration:   {_fmt_dur(duration)}")
    print(f"  Language:   {language}")
    print(f"  Model:      {model} (mlx-whisper)")
    print(f"  Keep ratio: {keep_ratio:.0%}")
    print(f"  Fade:       {fade}s")
    print("=" * 60)
    print()

    t0 = time.time()

    # ---- Step 1: Transcribe ----
    transcript_path = audio.with_suffix("").with_name(audio.stem + ".transcript.json")
    if transcript_path.exists():
        print(f"[1/4] Transcript already exists: {transcript_path.name}")
        print(f"       Skipping transcription (delete the file to re-transcribe)")
        print()
    else:
        transcript_path = _run_transcribe(audio, language, model)

    # ---- Step 2: Read transcript ----
    print(f"[2/4] Reading transcript...")
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript_data = json.load(f)

    segments = transcript_data.get("segments", [])
    total_duration = transcript_data.get("duration", duration)
    print(f"       {len(segments)} segments, {_fmt_dur(total_duration)} total")
    print()

    # Print transcript summary for Claude Code to parse if needed
    print("--- TRANSCRIPT PREVIEW (first 20 segments) ---")
    for seg in segments[:20]:
        sid = seg["id"]
        start = seg["start"]
        end = seg["end"]
        speaker = seg.get("speaker", "?")
        text = (seg.get("text") or "").strip()
        start_str = f"{int(start // 60):02d}:{start % 60:05.2f}"
        end_str = f"{int(end // 60):02d}:{end % 60:05.2f}"
        print(f"  [{sid:3d}] [{start_str}-{end_str}] [{speaker}] {text}")
    if len(segments) > 20:
        print(f"  ... and {len(segments) - 20} more segments")
    print("--- END PREVIEW ---")
    print()

    # ---- Step 3: Auto-tag segments ----
    print(f"[3/4] Auto-tagging segments (target keep: {keep_ratio:.0%})...")

    suggestions = suggest_tags(
        segments,
        speaker_weights={},    # equal weight for all speakers
        target_ratio=keep_ratio,
        strip_fillers=True,
    )

    # Build a lookup: segment_id -> tags
    tag_map: dict[int, list[str]] = {}
    for s in suggestions:
        tag_map[s["id"]] = s["tags"]

    # Apply tags to all segments for the selections.json
    tagged_segments = []
    n_cut = 0
    n_highlight = 0
    n_keep = 0
    for seg in segments:
        tags = tag_map.get(seg["id"], [])
        tagged_seg = {
            "id": seg["id"],
            "start": seg["start"],
            "end": seg["end"],
            "speaker": seg.get("speaker", "SPEAKER_00"),
            "text": seg.get("text", ""),
            "tags": tags,
        }
        tagged_segments.append(tagged_seg)
        if "cut" in tags:
            n_cut += 1
        elif "highlight" in tags:
            n_highlight += 1
            n_keep += 1
        else:
            n_keep += 1

    cut_duration = sum(
        float(s["end"]) - float(s["start"])
        for s in tagged_segments
        if "cut" in s["tags"]
    )
    keep_duration = total_duration - cut_duration

    print(f"       Results: {n_keep} keep, {n_cut} cut, {n_highlight} highlights")
    print(f"       Duration: {_fmt_dur(total_duration)} -> {_fmt_dur(keep_duration)} (cut {_fmt_dur(cut_duration)})")
    print()

    # Write selections.json
    selections_path = audio.with_name(audio.stem + ".selections.json")
    selections_data = {
        "video_path": str(audio),
        "segments": tagged_segments,
    }
    selections_path.write_text(json.dumps(selections_data, ensure_ascii=False, indent=2))
    print(f"       Saved: {selections_path}")
    print()

    # ---- Step 4: Cut ----
    # Determine output format based on input
    if is_audio:
        out_ext = audio.suffix  # keep original audio format
        output_path = audio.with_name(audio.stem + "_final" + out_ext)
    else:
        output_path = audio.with_name(audio.stem + ".final.mp4")

    # Use cut.py for video files (re-encodes for clean cuts)
    # Use ffmpeg concat for audio files (stream copy, fast)
    if not is_audio:
        print(f"[4/4] Cutting video with cut.py...")
        cmd = [
            sys.executable,
            str(SCRIPT_DIR / "cut.py"),
            str(audio),
            str(selections_path),
            "--output", str(output_path),
            "--fade", str(fade),
        ]
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
            print(f"\nError: cut.py exited with code {proc.returncode}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"[4/4] Cutting audio (stream copy)...")
        _cut_audio_fast(audio, tagged_segments, output_path)

    elapsed = time.time() - t0

    # ---- Summary ----
    print()
    print("=" * 60)
    print("  DONE")
    print("=" * 60)
    print(f"  Original:    {_fmt_dur(total_duration)}")
    print(f"  Final:       {_fmt_dur(keep_duration)}")
    print(f"  Removed:     {_fmt_dur(cut_duration)} ({cut_duration / total_duration:.0%})")
    print(f"  Segments:    {n_keep} kept, {n_cut} cut, {n_highlight} highlighted")
    print(f"  Time:        {_fmt_dur(elapsed)}")
    print(f"  Output:      {output_path}")
    print(f"  Selections:  {selections_path}")
    print("=" * 60)
    print()
    print("To fine-tune in the web editor:")
    print(f"  python scripts/serve.py --video '{audio}'")
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
    p_auto = subparsers.add_parser("auto", help="Full pipeline: transcribe -> tag -> cut -> done")
    p_auto.add_argument("audio", type=str, help="Audio/video file path")
    p_auto.add_argument("--language", default="zh", help="Language code (default: zh)")
    p_auto.add_argument("--model", default="large-v3",
                        choices=["tiny", "base", "small", "medium", "large-v3", "turbo"],
                        help="Whisper model size (default: large-v3)")
    p_auto.add_argument("--keep-ratio", type=float, default=0.7,
                        help="Target ratio of content to keep, 0.0-1.0 (default: 0.7)")
    p_auto.add_argument("--fade", type=float, default=0.15,
                        help="Crossfade duration in seconds for video (default: 0.15)")

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
            keep_ratio=args.keep_ratio,
            fade=args.fade,
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
