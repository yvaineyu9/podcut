#!/usr/bin/env python3
"""
Dump the marked-up transcript in a form Claude can use to write social drafts.

Usage:
    extract.py <selections.json> [--output social.md]

Produces a single Markdown file with two sections:

    ## 金句清单   — all segments tagged `highlight`, one per line with timestamp
    ## 完整保留稿 — every segment NOT tagged `cut` (the edited transcript)

Intentionally does NOT call any LLM. Claude reads this in the conversation and
drafts the 小红书 post from the 金句清单.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def fmt_ts(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def render_segment(seg: dict, name_map: dict[str, str]) -> str:
    start = fmt_ts(float(seg["start"]))
    end = fmt_ts(float(seg["end"]))
    spk = name_map.get(seg.get("speaker", "?"), seg.get("speaker", "?"))
    text = (seg.get("text") or "").strip()
    return f"- **[{start}–{end}] {spk}:** {text}"


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("selections", type=Path)
    ap.add_argument("--output", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.selections.exists():
        print(f"✗ selections not found: {args.selections}", file=sys.stderr)
        return 1

    data = json.loads(args.selections.read_text())
    all_segs = data.get("segments", [])
    name_map = data.get("speaker_names", {}) or {}

    highlights = [s for s in all_segs if "highlight" in (s.get("tags") or [])]
    kept = [s for s in all_segs if "cut" not in (s.get("tags") or [])]

    out_path = args.output or args.selections.with_name(
        args.selections.stem.replace(".selections", "") + ".social.md"
    )

    created = datetime.now().isoformat(timespec="seconds")
    video_path = data.get("video_path", "(unknown)")

    chunks = [
        f"# Social source · {Path(video_path).name}",
        "",
        f"_Generated {created} from `{args.selections.name}`_",
        "",
        f"- source video: `{video_path}`",
        f"- segments total: {len(all_segs)} · kept: {len(kept)} · 金句: {len(highlights)}",
        "",
        "---",
        "",
        "## 金句清单",
        "",
    ]
    if not highlights:
        chunks.append("_(还没有标金句，在编辑器里按 H 标几个再来)_")
    else:
        chunks.append(f"_{len(highlights)} 句_")
        chunks.append("")
        for s in highlights:
            chunks.append(render_segment(s, name_map))
    chunks += ["", "---", "", "## 完整保留稿", "", f"_{len(kept)} 段（剪辑后的文字版，可做节目 show-notes）_", ""]
    for s in kept:
        chunks.append(render_segment(s, name_map))

    out_path.write_text("\n".join(chunks))

    print(f"✅ Wrote {out_path}")
    print(f"   金句: {len(highlights)} · 保留: {len(kept)} / 总 {len(all_segs)}")
    print()
    print("Next: 把这个文件给 Claude，让它基于金句清单写一篇小红书草稿。")
    return 0


if __name__ == "__main__":
    sys.exit(run())
