#!/usr/bin/env python3
"""
Local HTTP server + editor — turns podcast-cutter into a single-page app.

Endpoints
    GET  /                       editor HTML
    GET  /api/state              which video/transcript is loaded
    GET  /api/video              streams the current video (Range-aware)
    GET  /api/transcript         the current transcript JSON, if any
    POST /api/jobs               { type: "transcribe" | "cut", params: {...} }
    GET  /api/jobs/<id>          { status, progress, stdout_tail, result }
    POST /api/suggest            {speaker_weights, target_ratio, strip_fillers}
    GET  /api/download/<token>   download a finished cut video
    POST /api/save-selections    persists the editor's selections.json next to video

Everything is localhost. No external network. No API keys.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

SKILL_DIR = Path(__file__).resolve().parent.parent
EDITOR_HTML = SKILL_DIR / "editor" / "index.html"

# Where to find python for spawning subprocesses (transcribe.py, cut.py).
# Native install: lives in <skill>/.venv/bin/python.
# Docker: no venv; the Dockerfile sets PODCUT_VENV_PYTHON=/usr/local/bin/python.
# Fall back to whatever Python is already running this server.
_default_venv = SKILL_DIR / ".venv" / "bin" / "python"
VENV_PYTHON = Path(os.environ.get("PODCUT_VENV_PYTHON") or
                   (_default_venv if _default_venv.exists() else sys.executable))


def _ffmpeg7_bin_path() -> str | None:
    """On macOS, locate Homebrew's ffmpeg@7 (keg-only). Returns its bin dir, or None.
    torchcodec only supports ffmpeg 4–7; the latest `brew install ffmpeg` is 8.x and breaks it.
    setup.sh installs ffmpeg@7 specifically; this helper makes sure subprocesses can find it
    even if the parent shell didn't run start.sh."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.check_output(
            ["brew", "--prefix", "ffmpeg@7"],
            stderr=subprocess.DEVNULL, text=True, timeout=3,
        ).strip()
        bin_dir = Path(out) / "bin"
        if (bin_dir / "ffmpeg").exists():
            return str(bin_dir)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def _augmented_env(extra: dict | None = None) -> dict:
    """Build a subprocess env that has ffmpeg@7 first on PATH (Mac) and any extras applied."""
    env = os.environ.copy()
    bin7 = _ffmpeg7_bin_path()
    if bin7 and not env.get("PATH", "").startswith(bin7):
        env["PATH"] = f"{bin7}:{env.get('PATH', '')}"
    if extra:
        env.update(extra)
    return env


# ============================================================================
# Session state (single active video per server)
# ============================================================================

class State:
    def __init__(self) -> None:
        self.video_path: Path | None = None
        self.transcript_path: Path | None = None
        self.jobs: dict[str, Job] = {}
        self.downloads: dict[str, Path] = {}
        self.lock = threading.Lock()


class Job:
    def __init__(self, job_type: str, params: dict) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.type = job_type
        self.params = params
        self.status = "pending"    # pending | running | done | error
        self.progress = 0.0        # 0..1
        self.stdout: list[str] = []
        self.result: dict | None = None
        self.error: str | None = None
        self.started_at = time.time()

    def log(self, line: str) -> None:
        self.stdout.append(line)
        if len(self.stdout) > 200:
            self.stdout = self.stdout[-200:]

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "progress": self.progress,
            "stdout_tail": "\n".join(self.stdout[-30:]),
            "result": self.result,
            "error": self.error,
            "elapsed": int(time.time() - self.started_at),
        }


STATE = State()


# ============================================================================
# Suggestion algorithm
# ============================================================================

_FILLER_RE = re.compile(r"^[嗯啊哦呃唔对好是的哈呵呀嘿噢ok\s,.，。、!?！？\-—…]*$", re.IGNORECASE)
_EMPHASIS = ["其实", "但是", "不过", "最重要", "最关键", "核心", "关键", "一句话",
             "记住", "必须", "千万", "真的", "绝对", "归根结底", "说白了", "本质"]


def suggest_tags(segments: list[dict], speaker_weights: dict[str, float],
                 target_ratio: float, strip_fillers: bool) -> list[dict]:
    """Score each segment, then greedily cut low-scorers until compression hits target.
    Returns [{id, tags: [cut? highlight?]}] for segments that got a suggestion."""
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
    keepers = [(s, sc) for (s, sc) in scored if s["id"] not in cuts and (float(s["end"]) - float(s["start"])) >= 3.0]
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
# Job runner (subprocess + progress parsing)
# ============================================================================

def run_transcribe(job: Job) -> None:
    video = STATE.video_path
    if not video:
        job.status = "error"; job.error = "No video selected"
        return
    params = job.params
    # Output transcript next to the video so subsequent runs can auto-load.
    out_path = video.with_suffix("").with_name(video.stem + ".transcript.json")
    cmd = [str(VENV_PYTHON), str(SKILL_DIR / "scripts" / "transcribe.py"),
           str(video), "--output", str(out_path)]
    if params.get("num_speakers"):
        cmd += ["--num-speakers", str(params["num_speakers"])]
    if params.get("language"):
        cmd += ["--language", str(params["language"])]
    if params.get("model"):
        cmd += ["--model", str(params["model"])]

    # Use hf-mirror.com by default — HuggingFace is often unreachable from China.
    # Users can override by exporting HF_ENDPOINT before running start.sh.
    # _augmented_env() also prepends ffmpeg@7's bin to PATH so the right ffmpeg is used.
    env = _augmented_env({
        "HF_ENDPOINT": os.environ.get("HF_ENDPOINT", "https://hf-mirror.com"),
        # Disable Xet backend (cas-bridge.xethub.hf.co) which bypasses the mirror
        # and fails frequently on networks that can't reach AWS S3 reliably.
        "HF_HUB_DISABLE_XET": "1",
        "HF_XET_HIGH_PERFORMANCE": "0",
        # Also disable fast hf_transfer which sometimes hangs on flaky networks.
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_ENABLE_HF_TRANSFER": "0",
    })

    job.log(f"$ HF_ENDPOINT={env['HF_ENDPOINT']} {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
    # Poor-man's progress: parse the progress phases out of the stdout lines.
    PHASES = [
        ("Loading whisperx",           0.05),
        ("Extracting audio",           0.10),
        ("Transcribing",               0.20),
        ("detected language",          0.55),
        ("Aligning word timestamps",   0.65),
        ("Running speaker diarization", 0.80),
        ("✅ Done",                     1.00),
    ]
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        job.log(line)
        for needle, pct in PHASES:
            if needle in line and pct > job.progress:
                job.progress = pct
                break
    proc.wait()
    if proc.returncode != 0:
        job.status = "error"; job.error = f"transcribe.py exited {proc.returncode}"
        return

    STATE.transcript_path = out_path
    job.progress = 1.0
    job.status = "done"
    job.result = {"transcript_path": str(out_path)}


def run_cut(job: Job) -> None:
    video = STATE.video_path
    if not video:
        job.status = "error"; job.error = "No video selected"
        return
    selections = job.params.get("selections")
    if not selections:
        job.status = "error"; job.error = "Missing selections payload"
        return

    # Write selections to a temp file next to the video
    sel_path = video.with_name(video.stem + ".selections.json")
    sel_path.write_text(json.dumps(selections, ensure_ascii=False, indent=2))

    fmt = str(job.params.get("format", "mp4")).lower()
    if fmt not in ("mp4", "mov"):
        fmt = "mp4"
    out_path = video.with_name(f"{video.stem}.final.{fmt}")
    cmd = [str(VENV_PYTHON), str(SKILL_DIR / "scripts" / "cut.py"),
           str(video), str(sel_path), "--output", str(out_path)]
    if "fade" in job.params:
        cmd += ["--fade", str(job.params["fade"])]

    job.log(f"$ {' '.join(cmd)}")
    # cut.py shells out to ffmpeg, so it also needs ffmpeg@7 on PATH (Mac).
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, env=_augmented_env())

    # Progress from cut.py's "[ x/ N]" lines
    bar_re = re.compile(r"\[\s*(\d+)/\s*(\d+)\]")
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line: continue
        job.log(line)
        m = bar_re.search(line)
        if m:
            i, n = int(m.group(1)), int(m.group(2))
            # segments done = first 85%, remaining 15% = concat
            job.progress = 0.85 * (i / max(n, 1))
        elif "Concatenating" in line:
            job.progress = 0.92
        elif "✅ Done" in line:
            job.progress = 1.0

    proc.wait()
    if proc.returncode != 0:
        job.status = "error"; job.error = f"cut.py exited {proc.returncode}"
        return

    token = uuid.uuid4().hex[:16]
    STATE.downloads[token] = out_path
    job.progress = 1.0
    job.status = "done"
    job.result = {
        "path": str(out_path),
        "size": out_path.stat().st_size,
        "download_url": f"/api/download/{token}",
        "selections_path": str(sel_path),
    }


def start_job(job: Job) -> None:
    def _wrap():
        job.status = "running"
        try:
            if job.type == "transcribe":
                run_transcribe(job)
            elif job.type == "cut":
                run_cut(job)
            else:
                job.status = "error"; job.error = f"Unknown job type: {job.type}"
        except Exception as exc:  # noqa: BLE001
            job.status = "error"; job.error = f"{type(exc).__name__}: {exc}"
            job.log(f"ERROR: {exc}")
    t = threading.Thread(target=_wrap, daemon=True, name=f"job-{job.id}")
    t.start()


# ============================================================================
# HTTP handler
# ============================================================================

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")


class Handler(BaseHTTPRequestHandler):
    server_version = "PodcastCutter/1.0"

    # Quiet down default logging; keep one-line summary only
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    # ------------------------- helpers -------------------------

    def _json(self, code: int, data) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, content_type: str, body: bytes, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str | None = None) -> None:
        """Stream a file with Range support (needed for <video> scrubbing)."""
        if not path.exists():
            return self._json(404, {"error": f"not found: {path}"})
        size = path.stat().st_size
        if content_type is None:
            content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"

        range_hdr = self.headers.get("Range")
        if range_hdr:
            m = _RANGE_RE.match(range_hdr)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416); self.end_headers(); return
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with path.open("rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(64 * 1024, remaining))
                        if not chunk: break
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        remaining -= len(chunk)
                return

        # Full file
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk: break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0: return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    # ------------------------- routing -------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/" or path == "/index.html":
                return self._serve_file(EDITOR_HTML, "text/html; charset=utf-8")
            if path == "/api/state":
                return self._api_state()
            if path == "/api/video":
                if not STATE.video_path:
                    return self._json(404, {"error": "no video"})
                return self._serve_file(STATE.video_path)
            if path == "/api/transcript":
                if not STATE.transcript_path or not STATE.transcript_path.exists():
                    return self._json(404, {"error": "no transcript"})
                return self._serve_file(STATE.transcript_path, "application/json; charset=utf-8")
            if path.startswith("/api/jobs/"):
                jid = path[len("/api/jobs/"):]
                job = STATE.jobs.get(jid)
                if not job:
                    return self._json(404, {"error": "no such job"})
                return self._json(200, job.as_dict())
            if path.startswith("/api/download/"):
                token = path[len("/api/download/"):]
                fp = STATE.downloads.get(token)
                if not fp or not fp.exists():
                    return self._json(404, {"error": "no such download"})
                # Force download
                extra = {"Content-Disposition": f'attachment; filename="{fp.name}"'}
                # Serve with Range to allow preview
                ctype = mimetypes.guess_type(str(fp))[0] or "video/mp4"
                return self._serve_file(fp, ctype)
        except Exception as exc:  # noqa: BLE001
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._json(404, {"error": f"unknown route: {path}"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json_body()
            if path == "/api/jobs":
                return self._api_start_job(body)
            if path == "/api/suggest":
                return self._api_suggest(body)
            if path == "/api/save-selections":
                return self._api_save_selections(body)
            if path == "/api/pick-video":
                return self._api_pick_video()
            if path == "/api/set-video":
                return self._api_set_video(body)
        except Exception as exc:  # noqa: BLE001
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._json(404, {"error": f"unknown route: {path}"})

    def _api_pick_video(self):
        """Open macOS native file picker via osascript. Blocks until user picks or cancels."""
        script = (
            'POSIX path of (choose file with prompt "选择要处理的视频文件" '
            'of type {"public.movie","mp4","mov","m4v","mkv","avi","webm"})'
        )
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired:
            return self._json(408, {"error": "picker timed out"})
        if result.returncode != 0:
            # User cancelled (returncode 1 with "User canceled" message)
            return self._json(200, {"cancelled": True})
        picked = result.stdout.strip()
        if not picked:
            return self._json(200, {"cancelled": True})
        return self._activate_video(Path(picked))

    def _api_set_video(self, body: dict):
        """Manually set video path (also accepts paste-in path input)."""
        raw = (body.get("path") or "").strip()
        if not raw:
            return self._json(400, {"error": "missing path"})
        return self._activate_video(Path(raw).expanduser())

    def _activate_video(self, path: Path):
        path = path.resolve()
        if not path.exists():
            return self._json(404, {"error": f"file not found: {path}"})
        if not path.is_file():
            return self._json(400, {"error": f"not a file: {path}"})
        STATE.video_path = path
        # Auto-attach existing transcript if sitting next to the video
        guess = path.with_suffix("").with_name(path.stem + ".transcript.json")
        STATE.transcript_path = guess if guess.exists() else None
        return self._json(200, {
            "video_path": str(path),
            "video_name": path.name,
            "has_transcript": bool(STATE.transcript_path),
        })

    # ------------------------- handlers ------------------------

    def _api_state(self):
        video = STATE.video_path
        transcript = STATE.transcript_path
        # Auto-detect existing transcript next to the video
        if video and not transcript:
            guess = video.with_suffix("").with_name(video.stem + ".transcript.json")
            if guess.exists():
                STATE.transcript_path = guess
                transcript = guess
        data = {
            "video_path": str(video) if video else None,
            "video_name": video.name if video else None,
            "video_url": "/api/video" if video else None,
            "transcript_path": str(transcript) if transcript else None,
            "transcript_url": "/api/transcript" if transcript else None,
            "has_transcript": bool(transcript and transcript.exists()),
        }
        return self._json(200, data)

    def _api_start_job(self, body: dict):
        job_type = body.get("type")
        if job_type not in ("transcribe", "cut"):
            return self._json(400, {"error": f"invalid job type: {job_type}"})
        job = Job(job_type, body.get("params") or {})
        # For cut jobs, capture selections from the request
        if job_type == "cut":
            job.params["selections"] = body.get("selections")
            job.params["fade"] = body.get("fade", 0.15)
            job.params["format"] = body.get("format", "mp4")
        STATE.jobs[job.id] = job
        start_job(job)
        return self._json(200, {"job_id": job.id})

    def _api_suggest(self, body: dict):
        # Prefer segments from request body (works when transcript lives only in the browser).
        # Fall back to the server-tracked transcript file.
        segs = body.get("segments")
        if not segs:
            if not STATE.transcript_path or not STATE.transcript_path.exists():
                return self._json(400, {"error": "no transcript loaded (pass segments in body or load one server-side)"})
            segs = json.loads(STATE.transcript_path.read_text()).get("segments", [])
        weights = body.get("speaker_weights") or {}
        target = float(body.get("target_ratio", 0.7))
        strip = bool(body.get("strip_fillers", True))
        suggestions = suggest_tags(segs, weights, target, strip)
        return self._json(200, {"suggestions": suggestions,
                                "meta": {
                                    "total_segments": len(segs),
                                    "target_ratio": target,
                                    "suggested_cuts": sum(1 for s in suggestions if "cut" in s["tags"]),
                                    "suggested_highlights": sum(1 for s in suggestions if "highlight" in s["tags"]),
                                }})

    def _api_save_selections(self, body: dict):
        if not STATE.video_path:
            return self._json(400, {"error": "no video"})
        sel_path = STATE.video_path.with_name(STATE.video_path.stem + ".selections.json")
        sel_path.write_text(json.dumps(body, ensure_ascii=False, indent=2))
        return self._json(200, {"path": str(sel_path)})


# ============================================================================
# Entrypoint
# ============================================================================

def find_free_port(preferred: int) -> int:
    import socket
    for port in [preferred] + list(range(8787, 8800)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred  # fall back; will error at bind time


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", type=Path, default=None, help="Source video path (optional — can also pick in the browser)")
    ap.add_argument("--transcript", type=Path, default=None, help="Pre-existing transcript.json (optional)")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--open", action="store_true", help="Open the editor in the default browser")
    args = ap.parse_args()

    video = None
    if args.video:
        video = args.video.resolve()
        if not video.exists():
            print(f"✗ video not found: {video}", file=sys.stderr)
            return 1
        STATE.video_path = video

    # Auto-pick up existing transcript if not given
    if args.transcript:
        STATE.transcript_path = args.transcript.resolve()
    elif video:
        guess = video.with_suffix("").with_name(video.stem + ".transcript.json")
        if guess.exists():
            STATE.transcript_path = guess

    # Ensure editor file exists
    if not EDITOR_HTML.exists():
        print(f"✗ editor HTML missing at {EDITOR_HTML}", file=sys.stderr)
        return 1

    port = find_free_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    print()
    print("━" * 50)
    print(f"  🎬  Podcast Cutter")
    if video:
        print(f"  video:      {video.name}")
    else:
        print(f"  video:      (none — pick one in the editor)")
    if STATE.transcript_path:
        print(f"  transcript: {STATE.transcript_path.name}  (loaded)")
    else:
        print(f"  transcript: (none — click 开始转录 in the editor)")
    print(f"  editor:     {url}")
    print("━" * 50)
    print()
    print("Press Ctrl-C to stop the server.")
    print()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True

    if args.open:
        # Open after a short delay so the first request hits a running server
        def _open():
            time.sleep(0.4)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
