# Demo Data

A fake 4-minute Chinese podcast — host (小王) interviewing a guest (阿宁) about leaving a big company to become an independent developer. Use it to play with the editor before running transcription on your real video.

## Files

| File | What it is |
|---|---|
| `transcript.json` | Raw transcript output — what `transcribe.py` would produce. 55 segments, 2 speakers, 244s. |
| `selections.example.json` | The same transcript **pre-marked** (cut / highlight / 小红书 / 公众号). Load this to see what a finished editing session looks like. |
| `social.example.md` | What `extract.py` produces from `selections.example.json`. Generated with:<br>`python scripts/extract.py demo/selections.example.json` |

## Try the editor (no video needed)

```bash
open ~/.claude/skills/podcast-cutter/editor/index.html
```

1. **"转录稿"** → choose `demo/transcript.json`.
   You'll immediately see the transcript, speaker colors, and the top timeline bar (driven by `duration` in the JSON — no video required).
2. **"上次标注"** → optionally load `demo/selections.example.json` to see a marked-up state.
3. Try the keyboard shortcuts — press `?` in the editor for the cheat sheet.
4. Click **"导出 selections.json"** — it downloads to `~/Downloads/`.

## Pair it with a video (optional)

The demo's `transcript.json` says `duration: 244.0`, so for full sync you'd want a ~4-minute video. Any video file will load — the timeline just won't line up perfectly with the transcript. Quick options:

- Drop in any 3–5 minute `.mp4` or `.mov` you have lying around (screen recordings, trailers, …)
- Or record ~4 minutes of yourself talking with QuickTime → save as .mov → load it

The editor remembers your tagging via `localStorage` (keyed on the video/transcript filename), so you can reload and keep going.

## Test extract.py

```bash
python3 ~/.claude/skills/podcast-cutter/scripts/extract.py \
  ~/.claude/skills/podcast-cutter/demo/selections.example.json
```

Look at the generated `demo/social.example.md` for the grouped raw text that feeds into the 小红书 / 公众号 writing step.

## Test cut.py

`cut.py` needs a real video to cut from — it calls `ffmpeg` on the source file. Once you have `ffmpeg` installed (via `setup.sh`) and any video aligned with the demo transcript, you can try:

```bash
~/.claude/skills/podcast-cutter/.venv/bin/python \
  ~/.claude/skills/podcast-cutter/scripts/cut.py \
  <your-video> \
  ~/.claude/skills/podcast-cutter/demo/selections.example.json \
  --output /tmp/demo-final.mp4
```

Out of 244s, the example would produce a ~212s final (removes the filler `cut`-tagged segments).
