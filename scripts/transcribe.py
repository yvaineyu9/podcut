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
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


DEFAULT_ZH_PROMPT = (
    "以下是中文播客访谈转写，主题可能涉及婚恋、相亲市场、结婚、生育、"
    "亲密关系、家庭、女性成长、职场和个人经历。请使用简体中文，"
    "保留口语表达，不要翻译成英文，不要把常见中文词识别成同音错词。"
)

DEFAULT_ZH_HOTWORDS = (
    "婚恋 相亲市场 结婚 生育 亲密关系 家庭 女性 成长 竞争力 护工 "
    "传统 路线 感情模式 价值 年龄 焦虑 本地 虚岁 选择"
)


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


def _homebrew_ffmpeg7_bin() -> Path | None:
    if sys.platform != "darwin":
        return None
    try:
        prefix = subprocess.check_output(
            ["brew", "--prefix", "ffmpeg@7"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    bin_dir = Path(prefix) / "bin"
    if (bin_dir / "ffmpeg").exists() and (bin_dir / "ffprobe").exists():
        return bin_dir
    return None


def resolve_ffmpeg_tools(ffmpeg_bin: Path | None = None) -> dict[str, str]:
    """Find ffmpeg/ffprobe, preferring Homebrew ffmpeg@7 on macOS."""
    candidates = [ffmpeg_bin, _homebrew_ffmpeg7_bin()]
    for bin_dir in candidates:
        if not bin_dir:
            continue
        ffmpeg = Path(bin_dir) / "ffmpeg"
        ffprobe = Path(bin_dir) / "ffprobe"
        if ffmpeg.exists() and ffprobe.exists():
            return {"ffmpeg": str(ffmpeg), "ffprobe": str(ffprobe)}

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    return {"ffmpeg": ffmpeg or "ffmpeg", "ffprobe": ffprobe or "ffprobe"}


def ensure_ffmpeg(tools: dict[str, str]) -> None:
    if subprocess.run([tools["ffmpeg"], "-version"], capture_output=True).returncode != 0:
        print("✗ ffmpeg not on PATH. Run setup.sh first.", file=sys.stderr)
        sys.exit(1)
    if subprocess.run([tools["ffprobe"], "-version"], capture_output=True).returncode != 0:
        print("✗ ffprobe not on PATH. Run setup.sh first.", file=sys.stderr)
        sys.exit(1)


def extract_audio(video: Path, out_wav: Path, tools: dict[str, str]) -> None:
    """Extract 16kHz mono wav — the format WhisperX and pyannote both prefer."""
    cmd = [
        tools["ffmpeg"], "-y", "-loglevel", "error",
        "-i", str(video),
        "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(out_wav),
    ]
    subprocess.run(cmd, check=True)


def probe_duration(path: Path, tools: dict[str, str]) -> float:
    out = subprocess.check_output([
        tools["ffprobe"], "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]).decode().strip()
    return float(out)


def fmt_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def build_asr_options(
    *,
    language: str,
    batch_size: int,
    prompt: str | None,
    hotwords: str | None,
    chunk_size: int,
) -> dict:
    """Build WhisperX ASR/VAD options in one auditable place."""
    forced_language = None if language == "auto" else language
    use_zh_context = forced_language in ("zh", "zh-cn", "zh-hans")

    initial_prompt = prompt
    effective_hotwords = hotwords
    if use_zh_context:
        initial_prompt = initial_prompt or DEFAULT_ZH_PROMPT
        effective_hotwords = effective_hotwords or DEFAULT_ZH_HOTWORDS

    return {
        "language": forced_language,
        "batch_size": batch_size,
        "asr_options": {
            "temperatures": [0.0],
            "condition_on_previous_text": True,
            "initial_prompt": initial_prompt,
            "hotwords": effective_hotwords,
        },
        "vad_options": {
            "chunk_size": chunk_size,
            "vad_onset": 0.500,
            "vad_offset": 0.363,
        },
    }


def build_asr_metadata(
    *,
    requested_model: str,
    effective_model: str,
    language: str,
    compute_type: str,
    asr_options: dict,
) -> dict:
    return {
        "requested_model": requested_model,
        "effective_model": effective_model,
        "language": language,
        "compute_type": compute_type,
        "batch_size": asr_options.get("batch_size"),
        "vad_options": asr_options.get("vad_options", {}),
        "used_initial_prompt": bool(asr_options.get("initial_prompt")),
        "used_hotwords": bool(asr_options.get("hotwords")),
    }


def _can_merge(prev: dict, seg: dict, *, max_gap: float, max_duration: float) -> bool:
    if prev.get("speaker") != seg.get("speaker"):
        return False
    if float(seg["start"]) - float(prev["end"]) > max_gap:
        return False
    return float(seg["end"]) - float(prev["start"]) <= max_duration


def merge_adjacent_segments(
    segments: list[dict],
    *,
    max_gap: float = 0.45,
    max_duration: float = 18.0,
) -> list[dict]:
    """Merge same-speaker micro-segments so downstream AI sees complete turns."""
    merged: list[dict] = []
    for seg in sorted(segments, key=lambda s: float(s["start"])):
        item = dict(seg)
        item["start"] = round(float(item["start"]), 3)
        item["end"] = round(float(item["end"]), 3)
        item["text"] = (item.get("text") or "").strip()
        if not merged or not _can_merge(merged[-1], item, max_gap=max_gap, max_duration=max_duration):
            merged.append(item)
            continue

        prev = merged[-1]
        prev["end"] = item["end"]
        if item["text"]:
            prev["text"] = f"{prev.get('text', '').strip()} {item['text']}".strip()
    for i, seg in enumerate(merged):
        seg["id"] = i
    return merged


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
    ap.add_argument("--chunk-size", type=int, default=30,
                    help="WhisperX VAD chunk size in seconds. Larger chunks preserve context for ASR.")
    ap.add_argument("--prompt", default=None,
                    help="Optional ASR initial prompt. Defaults to a Chinese podcast prompt when --language zh.")
    ap.add_argument("--hotwords", default=None,
                    help="Optional ASR hotwords. Defaults to Chinese podcast domain words when --language zh.")
    ap.add_argument("--merge-gap", type=float, default=0.45,
                    help="Merge adjacent same-speaker segments separated by at most this many seconds.")
    ap.add_argument("--max-merged-duration", type=float, default=18.0,
                    help="Maximum duration for merged transcript segments.")
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

    ffmpeg_tools = resolve_ffmpeg_tools()
    ensure_ffmpeg(ffmpeg_tools)

    output_path = args.output or video.with_suffix("").with_name(video.stem + ".transcript.json")
    print(f"▶ Video:     {video}")
    print(f"▶ Duration:  {fmt_duration(probe_duration(video, ffmpeg_tools))}")
    print(f"▶ Model:     {args.model} · language={args.language}")
    print(f"▶ ffmpeg:    {ffmpeg_tools['ffmpeg']}")
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
    requested_model = args.model
    local_model = Path.home() / ".cache" / "huggingface" / "hub" / f"models--Systran--faster-whisper-{args.model}" / "snapshots" / "manual"
    if local_model.exists() and (local_model / "model.bin").exists():
        print(f"   → using local model at {local_model}")
        args.model = str(local_model)

    # macOS: faster-whisper/CTranslate2 is CPU-only; pyannote can use MPS for a speedup.
    transcribe_device = "cpu"
    compute_type = "int8"
    diarize_device = "mps" if torch.backends.mps.is_available() else "cpu"
    options = build_asr_options(
        language=args.language,
        batch_size=args.batch_size,
        prompt=args.prompt,
        hotwords=args.hotwords,
        chunk_size=args.chunk_size,
    )

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmpd:
        tmp_wav = Path(tmpd) / "audio.wav"
        print("⏳ Extracting audio (16kHz mono)…")
        extract_audio(video, tmp_wav, ffmpeg_tools)

        # --- 1. Whisper transcription ----------------------------------------
        print(f"⏳ Transcribing with whisper-{args.model} on {transcribe_device}… (this is the slow part)")
        asr_model = whisperx.load_model(
            args.model,
            device=transcribe_device,
            compute_type=compute_type,
            language=options["language"],
            asr_options=options["asr_options"],
            vad_options=options["vad_options"],
        )
        audio = whisperx.load_audio(str(tmp_wav))
        asr_result = asr_model.transcribe(
            audio,
            batch_size=options["batch_size"],
            language=options["language"],
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
    raw_segment_count = len(out_segments)
    out_segments = merge_adjacent_segments(
        out_segments,
        max_gap=args.merge_gap,
        max_duration=args.max_merged_duration,
    )

    speakers_seen = sorted({s["speaker"] for s in out_segments})
    payload = {
        "video_path": str(video),
        "duration": probe_duration(video, ffmpeg_tools),
        "language": detected_lang,
        "num_speakers": len(speakers_seen),
        "speakers": speakers_seen,
        "model": args.model,
        "asr": build_asr_metadata(
            requested_model=requested_model,
            effective_model=args.model,
            language=detected_lang,
            compute_type=compute_type,
            asr_options={
                "batch_size": options["batch_size"],
                "vad_options": options["vad_options"],
                **options["asr_options"],
            },
        ),
        "raw_segment_count": raw_segment_count,
        "merge": {
            "enabled": True,
            "gap": args.merge_gap,
            "max_duration": args.max_merged_duration,
        },
        "segments": out_segments,
    }

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    elapsed = time.time() - t0
    print()
    print(f"✅ Done in {fmt_duration(elapsed)}")
    print(f"   {raw_segment_count} raw segments → {len(out_segments)} merged segments")
    print(f"   {len(speakers_seen)} speakers: {', '.join(speakers_seen)}")
    print(f"   Saved to {output_path}")
    print()
    print("Next:  open ~/.claude/skills/podcast-cutter/editor/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(run())
