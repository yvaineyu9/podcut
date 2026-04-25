---
name: podcast-cutter
description: End-to-end pipeline for turning long-form Chinese video podcasts into polished, social-ready cuts. Runs as a local web app — one command launches a browser editor that transcribes the video with speaker diarization, suggests what to cut and highlight via AI, lets the user refine via keyboard, previews the edit live, exports the final MP4, and dumps 金句 for 小红书 drafting. Trigger whenever the user mentions processing a podcast, podcast interview, video interview, long recording, 播客, 访谈, 视频剪辑, 逐字稿, transcribing a multi-speaker recording, cutting down a recorded conversation, or extracting 小红书 / 公众号 content from a video. Use even when the user does not say the word "podcast" — any long multi-speaker video that needs trimming and social repurposing is in scope.
---

# Podcast Cutter

Turn a multi-hour recorded conversation into a trimmed-down final video + social-ready 金句 drafts. Optimized for Chinese podcasts with 2–4 speakers.

## When to use this skill

Trigger on any workflow that looks like: *"I have a recorded conversation / interview / podcast episode, help me cut it down and get content out of it."* The user rarely describes the full pipeline — jump in at whichever stage they need.

Common phrasings:
- 录了一个播客 / 访谈 / 对谈 / 直播回放
- 帮我剪视频 / 做逐字稿 / 分辨说话人
- 想从这个视频里挑几段发小红书
- "Transcribe this with speaker labels"
- "Cut this 3-hour interview down to 90 minutes"

## First-time setup

Run once. Installs Homebrew (if missing), `python@3.11`, `ffmpeg`, creates a venv, installs `whisperx` + `pyannote.audio`, prompts for a free HuggingFace token. ~10 min, ~4 GB disk.

```bash
bash ~/.claude/skills/podcast-cutter/scripts/setup.sh
```

HuggingFace token steps (script walks through):
1. Register at https://huggingface.co/
2. Accept terms at https://huggingface.co/pyannote/speaker-diarization-3.1 AND https://huggingface.co/pyannote/segmentation-3.0
3. Create a read token at https://huggingface.co/settings/tokens

## The normal flow (one command)

```bash
bash ~/.claude/skills/podcast-cutter/scripts/start.sh <path/to/video>
```

This starts a local HTTP server on `127.0.0.1:8787`, opens the editor in the default browser, and auto-loads the video. No external network, no API keys at runtime.

Inside the editor:

1. **▶ 开始转录** (if no transcript exists) — opens a modal to pick speaker count / language / model, then runs `transcribe.py` in the background with a live progress bar. The resulting `<video>.transcript.json` lands next to the video.
2. **✨ 智能建议** — opens a modal with per-speaker weight sliders, a target compression ratio, and a "strip fillers" checkbox. Produces a first-pass auto-tagging (what to cut, what to highlight). Each AI-suggested segment shows a small `AI` badge and can be overridden with one click or keypress.
3. **Manual refinement** — keyboard-driven: `X` cut, `H` highlight, `Z` clear, `↑↓` step segments, `Space` play, `P` switch between 原片 and ✂ 成片 preview.
4. **✂ 成片 preview mode** (right video panel) — playback automatically skips any segment tagged `cut`, so what you see is what the exported video will be.
5. **💾 导出最终视频** — runs `cut.py` in the background, downloads the final MP4 when done.

After export, ask Claude (you) in this conversation to draft a 小红书 post based on the highlights:

```bash
~/.claude/skills/podcast-cutter/.venv/bin/python \
  ~/.claude/skills/podcast-cutter/scripts/extract.py \
  <video>.selections.json
```

The Markdown file has two sections: 金句清单 (all `highlight`-tagged segments) and 完整保留稿 (the edited transcript). **Draft the 小红书 post yourself** based on the 金句清单 — see style guide below.

## 小红书 drafting guide

Short punchy lines, strong hook on line 1, heavy emoji, 3–5 hashtags at the end, conversational tone. One 金句 usually becomes the hook, then 2–3 supporting lines expand it. ~200 Chinese characters.

Template:
```
[hook line — one 金句, no emoji before it, then ✨ or 👇]

[1–2 lines of context, why this quote matters]

[second supporting line or 金句]

[optional: a question that invites comments]

#标签1 #标签2 #播客分享 #独立开发 #金句分享
```

Example (from demo):
```
"你永远不会在犹豫里找到答案，只能在行动里找到" ✨

从大厂辞职做独立开发一年的阿宁说的，我停下来看了三遍。

他说："点子从来不缺，缺的是把点子变成用户真的愿意用的东西的能力。"

两件完全不同的事。共勉。

#独立开发 #播客金句 #大厂离职 #从0到1 #勇气
```

Do NOT post to any platform on the user's behalf. Leave the draft as text in chat (or a `.md` file) and let the user copy-paste.

## Architecture

Everything runs locally on the user's machine.

```
start.sh <video>
    └─▶ serve.py (Python stdlib HTTP server on 127.0.0.1:8787)
          ├─ serves editor/index.html
          ├─ serves the video file (Range-aware, for scrubbing)
          ├─ /api/jobs (runs transcribe.py / cut.py as subprocesses)
          ├─ /api/suggest (scores segments and returns cut/highlight suggestions)
          └─ /api/download/<token> (streams the final MP4)
```

No external APIs are called during editing/cutting. The only network use is during `setup.sh` (Homebrew + pip + HuggingFace model download on first transcription).

## Data contracts

### transcript.json (produced by transcribe.py)

```json
{
  "video_path": "/abs/path/to/video.mp4",
  "duration": 14400.0,
  "language": "zh",
  "num_speakers": 2,
  "segments": [
    { "id": 0, "start": 0.0, "end": 3.52, "speaker": "SPEAKER_00", "text": "大家好…" }
  ]
}
```

### selections.json (produced by the editor, consumed by cut.py / extract.py)

```json
{
  "video_path": "/abs/path/to/video.mp4",
  "created_at": "2026-04-16T12:30:00Z",
  "speaker_names": { "SPEAKER_00": "主持人", "SPEAKER_01": "嘉宾" },
  "segments": [
    { "id": 0, "start": 0.0, "end": 3.52, "speaker": "SPEAKER_00",
      "text": "大家好…", "tags": ["highlight"] }
  ]
}
```

**Tag vocabulary (canonical — do not invent others):**
- `cut` — drop this segment from the final video
- `highlight` — memorable quote / 金句 (used by extract.py for 小红书 source)

A segment with empty `tags` or no `cut` is kept. `cut.py` ignores any tag other than `cut`.

## Running the CLI scripts directly (advanced)

The editor is the intended UX, but each script also runs stand-alone:

```bash
# Transcribe
.venv/bin/python scripts/transcribe.py <video> --num-speakers 2

# Cut (from an existing selections.json)
.venv/bin/python scripts/cut.py <video> <selections.json> --output final.mp4

# Dump social source material
.venv/bin/python scripts/extract.py <selections.json>
```

## Troubleshooting

**"什么 API 连不上"** — all APIs in this skill are on `127.0.0.1` (your own machine). No external network needed. If the editor says "独立模式" instead of "server", start.sh didn't launch — run it again.

**Editor shows "独立模式"** — the editor was opened as `file://` instead of through the server. Use `start.sh` for the full experience (one-click transcribe/export).

**HF_TOKEN errors** — rerun setup.sh, and make sure terms were accepted on BOTH pyannote model pages (see first-time setup).

**Transcription is slow** — expected on macOS. `faster-whisper` under WhisperX is CPU-only. 4 hours ≈ 1 hour with large-v3, or ~20 min with medium.

**Too many speakers detected** — always pass "说话人数" in the transcribe modal if known. pyannote over-segments silent speakers into phantom ones.

**Export fails with A/V desync** — rarely, phone/DSLR recordings have variable-rate audio. Normalize first:
`ffmpeg -i video.mp4 -c:a aac -ar 48000 normalized.mp4` and re-transcribe.

**Port 8787 in use** — `serve.py` falls back to the next free port in 8787-8799 automatically.

## What NOT to do

- Do not call external APIs with transcripts — they contain unpublished user content.
- Do not post to 小红书 / 公众号 / anywhere on the user's behalf. Drafts only.
- Do not re-run `transcribe.py` unprompted when a `.transcript.json` already sits next to the video — it's expensive.
- Do not delete the source video after cutting; the user may want to re-cut.
