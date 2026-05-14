# PodCut Agent — Claude Code 播客剪辑工具

## 工具位置
`/Users/smalldog/Desktop/podcut/scripts/agent.py`

## 主要工作流：Claude 分析转录稿 → 手写 selections.json → 执行剪辑

### Step 1: 转录

```bash
python3 /Users/smalldog/Desktop/podcut/scripts/agent.py auto <音频/视频文件> [--language zh] [--model large-v3]
```

这个命令会：
1. 用 mlx-whisper 转录（如已有转录文件则跳过）
2. 打印**完整转录稿**（所有 segment）

### Step 2: Claude 分析转录稿

Claude 读取 `auto` 输出的完整转录稿，根据下方「剪辑决策指南」决定每个 segment 的标记，然后手动编写 `selections.json`。

格式：
```json
{
  "video_path": "/abs/path/to/audio.mp3",
  "segments": [
    {"id": 0, "start": 0.0, "end": 3.5, "speaker": "SPEAKER_00", "text": "...", "tags": ["cut"]},
    {"id": 1, "start": 3.5, "end": 12.0, "speaker": "SPEAKER_00", "text": "...", "tags": []},
    {"id": 2, "start": 12.0, "end": 18.0, "speaker": "SPEAKER_00", "text": "...", "tags": ["highlight"]}
  ]
}
```

### Step 3: 执行切割

```bash
python3 /Users/smalldog/Desktop/podcut/scripts/agent.py cut <音频/视频文件> <selections.json> [--fade 0.3]
```

### 参数说明
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--language` | `zh` | 语言代码，`zh`/`en`/`auto` |
| `--model` | `large-v3` | 模型大小：`tiny`/`base`/`small`/`medium`/`large-v3`/`turbo` |
| `--fade` | `0.0` | 视频转场淡入淡出时长（秒），仅 cut 命令 |

### 示例
```bash
# 中文播客，转录并打印
python3 scripts/agent.py auto ~/podcast/ep01.mp3

# 英文播客，用small模型（更快）
python3 scripts/agent.py auto ~/podcast/interview.mp4 --language en --model small

# Claude 分析转录稿后，写好 selections.json，然后执行剪辑
python3 scripts/agent.py cut ~/podcast/ep01.mp3 ~/podcast/ep01.selections.json --fade 0.15
```

## 其他命令

### 查看转录稿
```bash
python3 scripts/agent.py transcript <音频文件路径>
```
输出每个 segment 的 ID、时间、说话人、文字。需要先运行 `auto` 生成转录文件。

### 提取金句（可选）
```bash
python3 scripts/agent.py extract <selections.json>
```

## 人工微调：Web 编辑器

剪辑完成后，如果对结果不满意，可以打开 Web 编辑器微调：

```bash
python3 scripts/serve.py --video '<原始文件路径>'
```

编辑器会加载原始文件和已有的转录/选择数据，可以：
- 逐段试听
- 手动调整 cut/highlight 标记
- 重新导出

## 剪辑决策指南

### 删除（标记 cut）
- 假启动、重复开场
- 纯废话："嗯"、"对对对"、"然后"（独立短句且无内容）
- 调设备、换座位等与内容无关的段落
- 重复表达：同一观点反复说第二遍第三遍
- 跑题太远且无法拉回的段落
- 过长的犹豫/卡壳段

### 保留（无标签）
- 有信息增量的观点表达
- 故事/案例/经历分享
- 话题转换的过渡句
- 不同视角的回应和讨论
- 有情绪张力的段落（即使短）

### 高亮金句（标记 highlight）
- 有洞察的总结性发言
- 引人共鸣的个人体悟
- 可以单独拿出来做短视频/图文的句子
- 讨论的高潮/转折点

### 注意事项
- 保持话题完整性：不要从话题中间切断
- 保留上下文：如果一个金句需要前面的铺垫才能理解，铺垫也要保留
- 多人讨论中，保留必要的回应（即使很短）让对话自然
- 转录可能有错字，根据上下文理解真实含义
- 时间精度 ±2秒，不要在说话中间切断

## 输出文件说明

运行后，会在源文件同目录生成：
- `<name>.transcript.json` — 转录稿
- `<name>.selections.json` — 标记后的选择数据（可用于 Web 编辑器加载）
- `<name>_final.<ext>` — 最终剪辑成品（音频保持原格式，视频输出 mp4）
