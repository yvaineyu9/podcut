<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="editor/logo-mark.svg">
  <img src="editor/logo-mark.svg" alt="PodCut" width="120" height="120">
</picture>

# PodCut

**把几小时的中文播客 → 剪好的成片 + 小红书金句**

End-to-end pipeline for turning long-form Chinese video podcasts into polished, social-ready cuts.
Local-first · works offline · no per-minute fees.

</div>

---

## What it does

Drop a 4-hour video in. Get out:
- 🎙️ A speaker-labeled transcript (`SPEAKER_00`, `SPEAKER_01`, …)
- ✂️ A trimmed final video (the boring bits cut, the good bits kept)
- 🪙 A bullet list of 金句 ready to drop into a 小红书 / 公众号 draft

It's a one-command local web app. The UI is a Mario-themed pixel-art editor with a dual-lane timeline for speakers, a `🍄` mushroom playhead, and bouncing `¥` coins above your highlighted clips. Keyboard-driven editing (`X` to cut, `H` for 金句).

## Pipeline

```
video.mp4
   │
   ▼  [1] transcribe.py   (WhisperX + pyannote)
transcript.json   ← speaker-labeled segments with timestamps
   │
   ▼  [2] editor (browser) — AI suggests, you refine
selections.json   ← each segment tagged keep / cut / highlight
   │
   ├──▶  [3a] cut.py     →  final.mp4 / final.mov  (trimmed video)
   └──▶  [3b] extract.py →  social.md  (金句 + full kept transcript, for 小红书 drafting)
```

## Features

- **WhisperX + pyannote** — automatic transcription with speaker diarization
- **AI suggestions** — heuristic scoring marks fillers as `cut` and quotables as `highlight`, on a per-speaker weight you control
- **Preview mode** — play the video as if cuts were already applied (skips `cut`-tagged segments live)
- **Lossless concat** — `ffmpeg` re-encodes once at clean cut boundaries, no glitches
- **MP4 or MOV export** — pick your container in the UI
- **Mario timeline** — chunky pixel art shows everyone's segments by speaker, gold coins flag highlights, a mushroom is the playhead. Just for fun.
- **HF mirror support** — `HF_ENDPOINT=https://hf-mirror.com` is the default for China users
- **ModelScope fallback** — when HuggingFace is unreachable, you can pre-download the Whisper model from ModelScope (Aliyun CDN, ~40 MB/s from China) and PodCut will pick up the local cache

## Install

### Option A — Native (recommended for Apple Silicon)

```bash
git clone https://github.com/LauraKim/podcut.git
cd podcut
bash scripts/setup.sh
```

`setup.sh` installs Homebrew (if missing), `python@3.11`, `ffmpeg`, creates a venv, installs `whisperx` + `pyannote.audio`, and prompts for a free HuggingFace token. ~10 min, ~4 GB disk.

**Required HuggingFace gated-repo terms** — visit each link and click "Agree and access":
- https://huggingface.co/pyannote/speaker-diarization-community-1
- https://huggingface.co/pyannote/segmentation-3.0

Then create a read token at https://huggingface.co/settings/tokens.

### Option B — Docker

```bash
git clone https://github.com/LauraKim/podcut.git
cd podcut
cp .env.example .env       # then edit .env and paste your HF_TOKEN
docker compose up
```

Browser opens to `http://localhost:8787`. Drop videos into `./videos/` (mounted as `/data` inside the container).

> ⚠️ Native picker (`osascript`) won't work in Docker. Use the **"📂 选择视频文件"** button in the editor — the server will pick from files in `/data` instead.

## Usage

```bash
bash scripts/start.sh /path/to/video.mp4
# or, with no argument, pick the video in the browser:
bash scripts/start.sh
```

This starts a local HTTP server on `127.0.0.1:8787`, opens the editor in your default browser, and (if you passed a video) auto-loads it.

In the editor:

1. **▶ 开始转录** — click if there's no transcript yet. Pick speaker count, language, and model size. Runs in the background; progress and live log shown in a modal.
2. **✨ AI suggestions** — auto-runs after the transcript loads. The `🤖 AI 建议` toggle in the top bar turns it off (manual mode) or back on. Click `⚙` for fine-tuning (per-speaker weight, target compression ratio, strip-fillers).
3. **Refine** — keyboard shortcuts:
   - `X` — cut · `H` — 金句 · `Z` — clear tags
   - `Space` — play/pause · `J` / `L` — seek ±5s · `↑` / `↓` — prev / next segment
   - `P` — toggle 原片 / ✂ 成片 preview
4. **💾 导出最终视频** — runs `cut.py` on the server, downloads the final MP4 or MOV when done.
5. Run `python scripts/extract.py <selections.json>` to dump the 金句 list (`social.md`) — then ask Claude (or anyone) to rewrite it into a 小红书 post.

## Architecture

Everything is local. No third-party APIs are called during editing or cutting.

```
start.sh <video>
    └─▶ serve.py  (Python stdlib HTTP server on 127.0.0.1:8787)
          ├─ serves editor/index.html  (single-file Tailwind + Alpine.js app)
          ├─ serves the video file (Range-aware, for scrubbing)
          ├─ /api/jobs       → spawns transcribe.py / cut.py as subprocesses
          ├─ /api/suggest    → scores segments and returns cut/highlight suggestions
          ├─ /api/pick-video → opens the macOS native file picker via osascript
          └─ /api/download/<token> → streams the final MP4
```

Network use happens only on first run (downloading whisper + pyannote model weights, ~3 GB total).

## Data contracts

**transcript.json** (output of `transcribe.py`)
```json
{
  "video_path": "/abs/path.mp4",
  "duration": 14400.0,
  "language": "zh",
  "num_speakers": 4,
  "segments": [
    { "id": 0, "start": 0.0, "end": 3.52, "speaker": "SPEAKER_00", "text": "..." }
  ]
}
```

**selections.json** (output of editor → input to `cut.py` / `extract.py`)
```json
{
  "video_path": "/abs/path.mp4",
  "speaker_names": { "SPEAKER_00": "主持人", "SPEAKER_01": "嘉宾" },
  "segments": [
    { "id": 0, "start": 0.0, "end": 3.52, "speaker": "SPEAKER_00",
      "text": "...", "tags": ["highlight"] }
  ]
}
```

Tags: `cut` (drop) and `highlight` (金句). Anything else is kept by default.

## Troubleshooting

**HuggingFace download stalls / `Read timed out` from `cas-bridge.xethub.hf.co`** — Common in China. Set `HF_ENDPOINT=https://hf-mirror.com` (already default). For the big Whisper model.bin file the mirror still redirects to xet, so as a last resort run:

```bash
python -c "from modelscope import snapshot_download; \
  snapshot_download('pengzhendong/faster-whisper-medium', \
  cache_dir='~/.cache/modelscope/')"
```

then symlink/copy the files into `~/.cache/huggingface/hub/models--Systran--faster-whisper-medium/snapshots/manual/`. PodCut auto-detects that path.

**Diarization fails but Whisper succeeded** — `transcribe.py` saves a `<video>.whisper-cache.json` after Whisper completes. Run `python scripts/diarize_only.py <video> --num-speakers N` to retry just the speaker step without redoing the 20+ minutes of Whisper work.

**Editor shows "独立模式"** — you opened the HTML directly via `file://` instead of through the server. Use `bash scripts/start.sh` for the full experience.

**Port 8787 in use** — `serve.py` falls back to the next free port automatically (8787–8799).

## Project layout

```
podcut/
├── scripts/
│   ├── setup.sh           # one-time install (brew + python@3.11 + venv + deps)
│   ├── start.sh           # one-command launcher
│   ├── serve.py           # local HTTP server (stdlib only)
│   ├── transcribe.py      # video → transcript.json
│   ├── diarize_only.py    # rerun speaker diarization from a Whisper cache
│   ├── cut.py             # selections.json + video → final.mp4 / .mov
│   └── extract.py         # selections.json → social.md
├── editor/
│   ├── index.html         # the single-file editor (Tailwind + Alpine.js)
│   └── logo.html          # logo mark explorations
├── demo/
│   ├── transcript.json    # 4-min fake podcast transcript
│   ├── selections.example.json
│   └── README.md
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── SKILL.md               # for use as a Claude Code skill
```

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- [whisperx](https://github.com/m-bain/whisperX) — alignment + diarization wrapper around Whisper
- [pyannote.audio](https://github.com/pyannote/pyannote-audio) — speaker diarization
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — CTranslate2-based Whisper inference
- [ModelScope](https://modelscope.cn/) — China-friendly model mirror
- 🍄 Mario assets are tributes only; not affiliated with Nintendo.
