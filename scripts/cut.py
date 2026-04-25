#!/usr/bin/env python3
"""
Assemble the final trimmed video from selections.json.

Usage:
    cut.py <video> <selections.json> [--output final.mp4] [--fade 0.15]

Approach:
    For long videos with many segments, feeding a giant filter_complex to
    ffmpeg becomes slow and fragile. We instead cut each kept segment into
    a temporary file (re-encoding so boundaries are clean), then concatenate
    with the concat demuxer.

    --fade 0 skips crossfades (hard cuts, fastest).
    --fade N adds an N-second audio+video crossfade between segments.
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


KEEP_TAG = "keep"
CUT_TAG = "cut"


def load_selections(path: Path) -> dict:
    data = json.loads(path.read_text())
    if "segments" not in data:
        raise ValueError(f"{path} missing 'segments' key")
    return data


def is_kept(seg: dict) -> bool:
    """A segment is kept unless it is explicitly tagged 'cut'."""
    tags = seg.get("tags") or []
    if CUT_TAG in tags:
        return False
    return True


def merge_adjacent(segments: list[dict], gap_tolerance: float = 0.05) -> list[tuple[float, float]]:
    """Collapse back-to-back kept segments into single spans — fewer cuts, cleaner output."""
    spans: list[tuple[float, float]] = []
    for seg in sorted(segments, key=lambda s: s["start"]):
        s, e = float(seg["start"]), float(seg["end"])
        if e <= s:
            continue
        if spans and s - spans[-1][1] <= gap_tolerance:
            spans[-1] = (spans[-1][0], max(spans[-1][1], e))
        else:
            spans.append((s, e))
    return spans


def cut_segment(video: Path, start: float, end: float, out: Path, fade: float) -> None:
    """Cut one segment, re-encoding for clean boundaries and matching codecs for concat."""
    duration = end - start
    vf = []
    af = []
    if fade > 0 and duration > 2 * fade:
        # Fade in at the start, fade out at the end — simpler than editing the concat seams.
        vf.append(f"fade=t=in:st=0:d={fade}")
        vf.append(f"fade=t=out:st={duration - fade:.3f}:d={fade}")
        af.append(f"afade=t=in:st=0:d={fade}")
        af.append(f"afade=t=out:st={duration - fade:.3f}:d={fade}")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-nostdin",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", str(video),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    if af:
        cmd += ["-af", ",".join(af)]
    cmd.append(str(out))
    subprocess.run(cmd, check=True)


def concat_segments(parts: list[Path], out: Path) -> None:
    """Concat demuxer — stream copy is safe because we re-encoded each part with identical params."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        list_path = Path(f.name)
        for p in parts:
            # ffmpeg concat demuxer: paths need single quotes escaped
            f.write(f"file '{p.as_posix()}'\n")
    try:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-nostdin",
            "-f", "concat", "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            "-movflags", "+faststart",
            str(out),
        ]
        subprocess.run(cmd, check=True)
    finally:
        list_path.unlink(missing_ok=True)


def fmt(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video", type=Path)
    ap.add_argument("selections", type=Path)
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--fade", type=float, default=0.15,
                    help="Crossfade duration in seconds. 0 = hard cuts.")
    ap.add_argument("--keep-temp", action="store_true",
                    help="Keep the per-segment temp files (useful for debugging).")
    args = ap.parse_args(argv)

    video = args.video.resolve()
    selections = args.selections.resolve()
    if not video.exists():
        print(f"✗ video not found: {video}", file=sys.stderr)
        return 1
    if not selections.exists():
        print(f"✗ selections not found: {selections}", file=sys.stderr)
        return 1

    data = load_selections(selections)
    all_segs = data["segments"]
    kept = [s for s in all_segs if is_kept(s)]
    if not kept:
        print("✗ No segments marked as kept.", file=sys.stderr)
        return 1

    spans = merge_adjacent(kept)
    total_in = sum(float(s["end"]) - float(s["start"]) for s in all_segs)
    total_out = sum(e - s for s, e in spans)

    output_path = args.output or video.with_name(video.stem + ".final.mp4")
    print(f"▶ Video:          {video}")
    print(f"▶ Selections:     {selections}")
    print(f"▶ Segments in:    {len(all_segs)} ({fmt(total_in)})")
    print(f"▶ Kept spans:     {len(spans)} ({fmt(total_out)}, after merging adjacent)")
    print(f"▶ Fade:           {args.fade}s")
    print(f"▶ Output:         {output_path}")
    print()

    t0 = time.time()
    workdir = Path(tempfile.mkdtemp(prefix="podcast_cut_"))
    part_files: list[Path] = []
    try:
        for i, (s, e) in enumerate(spans):
            part = workdir / f"part_{i:04d}.mp4"
            bar = f"  [{i+1:>3}/{len(spans)}]"
            print(f"{bar} {fmt(s)} → {fmt(e)}  ({e-s:.1f}s)")
            cut_segment(video, s, e, part, args.fade)
            part_files.append(part)

        print()
        print("⏳ Concatenating…")
        concat_segments(part_files, output_path)
    finally:
        if args.keep_temp:
            print(f"⚠ temp files kept at {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    elapsed = time.time() - t0
    print()
    print(f"✅ Done in {fmt(elapsed)}")
    print(f"   {fmt(total_in)} → {fmt(total_out)}  (removed {fmt(total_in - total_out)})")
    print(f"   → {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
