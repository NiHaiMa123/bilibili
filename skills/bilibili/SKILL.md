---
name: bilibili
description: 检索 B 站视频并把字幕/关键帧整理成本地 Markdown，用于回答"这个操作怎么做"类问题并给出处。当用户问"B 站上有讲 XX 的视频吗""搜一下 B 站 XX 教程"或需要引用视频原话时使用。
---

# Bilibili 检索与图文知识化

把 B 站视频变成可以精读、可以引用的本地 Markdown。Agent 自己完成
"搜索 → 锁定 → 生成 → 读取 → 带时间戳引用"，不需要用户手动复制链接。

## 决策流程

### 1. 搜索（只搜索，不下载）

```json
bili_search {"query": "MMD PMX Editor 换装", "limit": 10, "sort": "relevance"}
```

* `sort`：`relevance` | `newest` | `most_viewed` | `most_danmaku` | `most_stow`
* 搜索命中只是**候选**，不是"看过视频"。不要把标题/简介当作视频内容引用。

### 2. 锁定对象

```json
bili_inspect {"url_or_bvid": "BV1FW41157y3"}
```

拿到分 P 列表（`page` / `cid` / 时长）。多 P 视频要明确选哪一 P：
默认只处理 P1，不要把整张搜索结果都下载一遍。

### 3. 生成产物

```json
bili_materialize {
  "url_or_bvid": "BV1FW41157y3",
  "parts": [1],
  "mode": "subtitle",
  "subtitle_source": "auto"
}
```

选模式的判断依据：

| 用户诉求 | mode | 说明 |
| --- | --- | --- |
| 只是想知道"讲了啥" | `subtitle` | 纯文字，最快，不下视频 |
| 需要看界面/操作步骤 | `illustrated` | 额外抽关键帧，生成 article.md |
| 只想看封面或某一帧 | `assets` | 不需要正文 |
| 只确认视频是否存在/多长 | `metadata` | 不下载任何媒体 |

`subtitle_source`：`auto`（默认，按平台字幕 → 软字幕 → OCR → ASR 依次尝试）
| `platform`（只认官方字幕）| `ocr` | `asr`（只走本地语音转写）。

图文/OCR/ASR 较慢时用 `wait=false` 后台跑，再用 `bili_job_status` 查。

### 4. 读取产物

```json
bili_read_transcript {"bvid": "BV1FW41157y3", "part": 1}
bili_read_transcript {"bvid": "BV1FW41157y3", "part": 1, "start_ms": 60000, "end_ms": 120000}
```

不指定时间段时只返回章节索引与统计；**按需**拉取要引用的片段，避免一次性把整篇塞进上下文。
图文产物直接读 `article.md`（图片是相对路径，编辑器里能直接显示）。

## 引用规范

* 引用视频内容时必须带上 **BV 号 + 时间戳**，例如"（BV1FW41157y3，02:04）"。
* 字幕来源要如实说明：`platform_subtitle` / `soft_subtitle` / `hard_subtitle_ocr` / `qwen3_asr`。
* 转写可能有错：不要把 ASR 结果说成"视频原话逐字稿"，说"该视频约 02:04 处讲到……"。
* 只有在产物里实际读到过的内容才能引用。搜索结果和视频简介不算内容。

## 常见失败与处理

| 现象 | 处理 |
| --- | --- |
| `bili_search` 返回 412 | B 站风控，停止请求并告知用户，不要换代理重试 |
| 字幕为空 | 改用 `subtitle_source: "asr"`，会下载音频并本地转写（需要 GPU/模型） |
| `bili_materialize` 返回 `stage: "subtitle"` 且无产物 | 该视频没有可用字幕，明确告诉用户"没有字幕"，不要编造内容 |
| 视频不存在 / -404 | 换一个候选，不要重试同一个 BV |

## 没有 MCP 时的等价命令

```powershell
bili search "关键词" --limit 10 --json
bili inspect BVxxxxxxxxxx --json
bili fetch BVxxxxxxxxxx --parts 1 --mode subtitle --json
bili status <job_id> --json
```

CLI 输出的 JSON 字段与 MCP 工具一致。
