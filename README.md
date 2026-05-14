<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="editor/logo-mark.svg">
  <img src="editor/logo-mark.svg" alt="PodCut" width="120" height="120">
</picture>

# PodCut

**播客/音频剪辑工具 — 转录 → 标记 → 导出**

Apple Silicon 原生加速，本地运行，无需付费 API。

</div>

---

## 功能

- **AI 转录** — mlx-whisper 在 Apple Silicon 上原生运行，4分钟音频约36秒转好
- **波形时间线** — 真实音频波形，支持 Ctrl+滚轮缩放（最高30倍），精准定位
- **标记剪辑** — 键盘驱动：`X` 删除废话，`H` 标记金句，`Z` 清除
- **AI 建议** — 自动标记填充词/废话为删除，标记亮点为金句
- **成片预览** — `P` 键切换，播放时自动跳过删除段
- **导出** — 一键导出 MP4/MOV 精剪成片
- **视频+音频** — 支持 mp4/mov/mkv + mp3/m4a/wav/flac

## 快速开始

### 1. 安装

```bash
git clone https://github.com/yvaineyu9/podcut.git
cd podcut
bash scripts/setup.sh
```

> `setup.sh` 会安装 `python@3.11`、`ffmpeg@7`、创建 venv、安装依赖。约10分钟。

### 2. 安装 mlx-whisper（推荐）

```bash
.venv/bin/pip install mlx-whisper
```

> Apple Silicon 原生转录引擎，不需要 HF_TOKEN，不需要 torchcodec。

### 3. 启动

```bash
bash scripts/start.sh
# 或直接指定文件：
bash scripts/start.sh /path/to/podcast.mp3
```

浏览器自动打开 `http://127.0.0.1:8787`

## 使用流程

```
音频/视频文件
   │
   ▼  📂 选择文件（支持视频和音频）
   │
   ▼  ▶ 开始转录（mlx-whisper，选 large-v3 或 small）
   │
   ▼  编辑器：波形时间线 + 段落列表
   │   X = 删除  H = 金句  Z = 清除
   │   AI 自动建议 + 手动微调
   │
   ▼  💾 导出最终视频/音频
```

## 编辑器界面

```
┌──────────────────────────────────────────────┐
│ Header (logo · 文件名 · AI开关 · 导出)        │
├──────────────────────────────────────────────┤
│          媒体预览（视频大画面/音频播放器）       │
├──────────────────────────────────────────────┤
│ ⏪ ▶ ⏩ │ 时间码 │ 当前说话人: 文本            │
├──────────────────────────────────────────────┤
│ [-][+] 缩放 │ 统计信息                        │
│ ┌────────────────────────────────────────┐   │
│ │ 时间刻度尺                              │   │
│ │ ~~音频波形~~ (紫=保留 红=删除 金=金句)   │   │
│ │ ████ 说话人色带 ████                    │   │
│ └────────────────────────────────────────┘   │
├────────┬─────────────────────────────────────┤
│ 统计    │ 段落列表                            │
│ 说话人  │ 00:13 SPEAKER_00: 文本... [💣][🪙]  │
│ 筛选    │ ...                                 │
└────────┴─────────────────────────────────────┘
```

## 快捷键

| 操作 | 快捷键 |
|------|--------|
| 播放/暂停 | `Space` |
| 后退/前进 5s | `J` / `L` |
| 上/下一段 | `↑` / `↓` |
| 跳到段首 | `0` |
| 标记删除 | `X` |
| 标记金句 | `H` |
| 清除标签 | `Z` |
| 原片/成片切换 | `P` |
| 波形放大/缩小 | `+` / `-` |

## 转录模型

| 模型 | 大小 | 中文精度 | 速度（M2） |
|------|------|---------|-----------|
| **large-v3** | ~3GB | 最好 | 68分钟→6分钟 |
| **small** | ~460MB | 够用 | 68分钟→2分钟 |

首次使用需下载模型。如遇网络问题，脚本会自动绕过本地代理。

## 项目结构

```
podcut/
├── scripts/
│   ├── setup.sh             # 一键安装
│   ├── start.sh             # 启动服务
│   ├── serve.py             # HTTP 服务 + API
│   ├── transcribe_mlx.py    # mlx-whisper 转录（推荐）
│   ├── transcribe.py        # WhisperX 转录（需 HF_TOKEN）
│   ├── cut.py               # 按标记切割音视频
│   └── extract.py           # 导出金句列表
├── editor/
│   └── index.html           # 单文件编辑器（Tailwind + Alpine.js）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 技术栈

- **转录**: mlx-whisper（Apple Silicon）/ WhisperX（通用）
- **波形**: ffmpeg PCM 提取 + Canvas 渲染
- **前端**: Alpine.js + Tailwind CSS（单文件 HTML）
- **后端**: Python stdlib HTTP server
- **切割**: ffmpeg

## License

MIT
