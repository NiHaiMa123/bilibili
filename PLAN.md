# Bilibili 视频检索与图文知识化 Agent — 实施计划

> 状态：PLAN / 待实施；目标运行环境：Windows 本机；主要消费者：Droid/DSH、Codex、MiniMax Code 等编码 Agent。  
> 上游下载器：[Yuelioi/bdl](https://github.com/Yuelioi/bdl)；本项目独立实现搜索、获取、转写、抽帧与 Agent 接口，不修改用户当前安装的 BDL 桌面应用。  
> 本计划基于 2026-10-23 检查的上游仓库与 CLI 文档。**计划中出现的 `bili ...` 命令和 MCP 工具名是本项目要实现的目标接口，并非已可用功能。**

## 0. 最终目标与使用体验

用户向 Agent 提问：“搜索 B 站上关于某项技术/软件操作的视频，读完后告诉我怎么做，并给出处”。

Agent 自动执行：

1. **发现**：按关键词检索 B 站，返回可追溯的视频候选（标题、UP 主、时长、发布时间、BV 号、简介、分 P），必要时改写检索词再搜。
2. **选择**：Agent 按主题、视频质量和用户需求筛选 1～N 个候选；没必要的不要全部下载；遇到模糊候选先读取元信息。
3. **获取**：针对指定 BV/AV/URL 和分 P 获取字幕/音轨/视频。优先用官方字幕，字幕不存在时按可用媒介决定 OCR 和 ASR。
4. **知识化**：把时间戳文本与**真正解释内容的关键画面**结合，生成可搜索、可阅读、可由 Agent 引用的本地 Markdown。支持只生成字幕文本，不强制下载完整视频。
5. **消费**：Agent 通过本地工具得到生成文件路径/内容摘要，按章节阅读，回答时带视频链接与时间戳，不把未知内容编造为原视频说过的话。

两个核心输出模式：

- `subtitle`：字幕/语音完整文字版（`.srt` / `transcript.md`），**默认不抽帧**。
- `illustrated`：按时间轴组织的图文 `article.md`，图像文件存在本地相对路径下；用于 UI 操作、建模、教程、展示类视频。

扩展模式：`metadata`（只检索/查看，不下载）、`assets`（取封面/指定帧）、`both`（完整文字+图文）。默认 `illustrated` 同时保留原始字幕或转写，不能只留下主观总结。

**成败标准**：Agent 不需要手动复制链接到桌面 UI，也不需要用户逐一操作 BDL；完成搜索→选择→生成→读取，并能告诉用户资料来自哪段视频的哪一分钟。

## 1. 已确认的上游能力与边界（先复用，避免重复造轮子）

### 1.1 BDL CLI 已存在，不必驱动桌面窗口

上游仓库包含：

- `crates/bdl-cli/`：独立命令行程序。
- `crates/bdl-core/`：来源解析、媒体下载、媒体封装和部分字幕处理逻辑。
- `docs/CLI.md`：CLI 使用和 JSON 协议。
- `crates/bdl-core/src/subtitles.rs`：B 站 JSON 字幕转换为 SRT/ASS 的实现。
- `crates/bdl-core/src/resolver/video.rs`：视频与字幕资源等元信息解析路径。

经核对，最新 Release **v0.9.1**（2026-10-19 发布）包含独立 Windows CLI 资产 `bdl-cli-0.9.1-windows-x64.zip`。**已安装 BDL 桌面应用 ≠ 已安装 `bdl.exe` CLI**，实施时必须先探测 `bdl --version`，必要时单独安装 CLI。不能误用图形版 EXE 当成 CLI。

CLI 已支持 `parse`、`download`、`verify-cookie`、`ffmpeg-check` 和 `--json`；支持普通视频与多 P 等来源。示例（安装 CLI 后）：

```powershell
bdl --json parse BVxxxxxxxxxx
bdl --json download BVxxxxxxxxxx --parts 1 --output "D:\bili-cache\media" --media-mode audio-video
bdl --json download BVxxxxxxxxxx --parts 1 --output "D:\bili-cache\media" --media-mode audio-only
bdl --json ffmpeg-check
```

**重要缺口**：当前 `docs/CLI.md` 明确写明，CLI 下载只生成最终媒体文件，不下载封面、字幕、弹幕或 NFO；桌面 GUI 支持部分附件，不代表 CLI 已暴露相同接口。需要本项目增加字幕获取适配层，或以后视维护成本向 BDL 上游贡献/扩展 CLI。首版**优先调用独立 BDL CLI**，不 fork 整个 BDL，不通过 GUI 自动点按钮。

BDL CLI 的 `--json` 成功信息写 stdout、错误写 stderr；`download` 返回 `downloads[].outputs`。程序只认**实际输出清单**，不得依靠猜测文件名；同时核验存在性、文件大小、FFprobe 可读性。调用 `download` 时必须指定 `--parts` / `--items` 或显式 `--all --state`，首版一律限定目标范围。

CLI 的 Cookie 与桌面版登录状态**不共享**：`--cookie-file` / `BDL_COOKIE` 需要用户显式配置，不能假定桌面版已登录就能自动下载受限视频。凭据不得写入日志、输出的 Markdown 或 Git。

参考：
- [BDL 仓库](https://github.com/Yuelioi/bdl)
- [BDL CLI 文档](https://github.com/Yuelioi/bdl/blob/main/docs/CLI.md)
- [BDL 下载器字幕转换](https://github.com/Yuelioi/bdl/blob/main/crates/bdl-core/src/subtitles.rs)
- [BDL Releases](https://github.com/Yuelioi/bdl/releases)

### 1.2 为什么本项目建议用 Python 编排

优先：**Python 3.11+ 作为流程层** + 系统调用 `bdl.exe`、`ffmpeg.exe`、`ffprobe.exe` + 独立本地 MCP（stdio）。原因：

- Qwen3-ASR 已在 `E:\project\tts` 本地 Python 环境落地，复用成本低。
- 图像处理/OCR、视频抽帧、SRT 处理、Markdown 生成和测试在 Python 生态中直接可用。
- Rust/BDL 下载逻辑不再重写，维持独立可升级的上游二进制。
- CLI 既能供 shell Agent 直接使用，又能包装为 MCP，避免绑定特定 Agent 平台。

暂不自建桌面 UI、浏览器插件、云后端、向量数据库、视频平台登录系统；这些不是完成当前目标的必要条件。

## 2. 功能架构与数据通路

```text
Agent 提出检索主题
    │
    ▼
SearchProvider（B站搜索 API；必要时普通网页搜索作为备用）
    │ → 候选列表、BV/AV、标题、UP、发布时间、简介、播放量、P 数
    ▼
Rank / Inspect（只分析与筛选，不盲目下载）
    │ → 选定 BV + 分 P/CID
    ▼
Materializer（任务状态、缓存、输出模式）
    ├── SubtitleResolver：B站可下载字幕 / 媒体软字幕
    └── BdlAdapter：BDL CLI 获取音频或完整视频
                    │
                    ├── HardSubtitleOCR：视频画面中烧录字幕 OCR
                    ├── ASRAdapter：复用 E:\project\tts 的 Qwen3-ASR
                    └── FrameSelector：镜头切换 + 教学界面变化 + 时间轴
                         │
                         ▼
                 Normalize / Segment / Evidence
                         │
                         ▼
                 MarkdownWriter + manifest.json
                         │
                         ▼
              CLI 命令 / MCP 工具返回本地文件
                         │
                         ▼
               Codex / DSH / MiniMax Code 阅读
```

层间使用稳定 JSON 数据模型，不把原生 B 站响应、BDL 的临时媒体直链或模型推理状态直接写进最终文档。

## 3. 搜索与锁定视频

### 3.1 搜索接口

建立 `SearchProvider`，第一实现为 Bilibili API：

- 按关键词搜索视频，返回前 10～20 个候选；支持 `relevance / newest / most_viewed` 等有依据的排序。
- 可研究使用 `/x/web-interface/wbi/search/type`（`search_type=video`）；如需要 WBI 签名，按当前接口方式实现，不能直接复用失效旧接口。
- 返回结构统一成 `SearchHit`：`bvid, aid?, title, author, duration, published_at, view_count?, description?, url, cover_url?, source, rank_reason?`。
- 过滤 HTML 标题标签、失效结果、重复 BV，保留原始地址；不可用字段为 `null`，不凭空补。
- 支持多关键词依次搜、BV 去重；对比视频长度、时效、标题匹配和简介，不只用播放量排序。
- 如接口异常/风控，返回结构化错误；可通过已配置的浏览器/网页搜索获取候选链接，再用 BDL `parse` 复核。网页搜索不是虚构“已获取视频内容”。
- 搜索接口对 Cookie、WBI、HTTP 412/429 等异常要识别；有权限就用正常登录态，遇限制**停止并明确报错**，不做代理轮换或循环请求绕过限制。

用户给出 BV/URL 时直接进入 `inspect`，跳过搜索。用户提及某个 UP 主、时间范围或教程版本时，将其用作筛选条件。

### 3.2 精确确认选中对象

调用 `bdl --json parse <BV>` 并解析 `NormalizedSourceTree`；记录多 P 列表、对应 CID、时长及标题。明确 `bvid + cid + page_index` 是本地转换最小单元，防止把 P1 字幕误绑到 P2 画面。

允许 `top_k` 候选，Agent 在实际下载前选择至多需要的几个；首版默认只下载一个视频的选中分 P，不自动批量下载整张搜索结果。

输出：
```json
{
  "bvid": "BVxxxxxxxxxx",
  "url": "https://www.bilibili.com/video/BVxxxxxxxxxx",
  "title": "示例标题",
  "parts": [{"page": 1, "cid": 123456, "title": "P1", "duration_s": 600}],
  "selected_parts": [1]
}
```

> 以上示例的 BV、CID、时长只是结构示意，不能作为测试时的真实资源。

## 4. 字幕与语音的四级获取路径（核心）

将“字幕文件”“视频轨内软字幕”“画面烧录字幕”“原音频转写”区分开，不能混称一类。

### Level 1 — B 站可下载字幕（最快、最高优先级）

1. 用已解析的 `aid/bvid + cid` 查询播放器字幕元数据；按语言优先级选目标语言（用户指定 > 中文 > 原语言）。
2. 下载字幕正文（如 Bilibili JSON），转换成带 `start_ms/end_ms/text` 的统一 `Segment`，输出 SRT、`transcript.md`。
3. 能借鉴 BDL 的 `resolver/video.rs` 和 `subtitles.rs`；若 CLI 不暴露字幕资产，首版从本项目的 `SubtitleResolver` 通过官方页面使用的接口读取。
4. 执行基本质量检查：空字幕、极少片段、时间错乱、重复内容、字幕仅有歌曲/广告标记等，必要时触发备用路径。
5. 对官方/AI 字幕来源标明 `source = platform_subtitle`（若能识别自动字幕则进一步标记），保留原始字幕 JSON 可选。

**纯字幕模式成功后停止**，无需下载整段视频或加载 Qwen3-ASR。图文模式继续获取视频画面。

### Level 2 — 媒体文件中的独立软字幕轨

若平台没有提供可取出的字幕资源，检查下载媒体的 `ffprobe -show_streams` 字幕轨；存在则用 FFmpeg 提取、转换、归一化。不能把封装中的字幕轨误判成画面里烧录的文字。

### Level 3 — 画面烧录字幕 OCR（用户所说“内嵌字幕”）

如果没有可用字幕文件，但视频画面底部有持续出现的对白字幕：

1. 先低频探测（例如间隔 1～2 秒 + 开头/中部/末尾抽样），检测底部文字区域是否存在**连贯、持续变化的字幕**；不能把 PPT 标题、B 站水印、UI 文字当作对白。
2. 以可配置的字幕区域为主提取（默认底部中央候选区域；分辨率自适应），首选一套可本机安装的中文 OCR 引擎（候选：RapidOCR + ONNX Runtime；若质量不足再评估 PaddleOCR）。
3. 使用文本相似度、时间连续性、相邻帧稳定性合并同一句，保留第一次和最后一次出现的时间；对重复行、局部更新和双行字幕特别处理。
4. 评估 `coverage`（有效字幕时长/视频时长、每分钟有效字数、OCR 置信信息及视觉稳定性），低覆盖度就进入 ASR。
5. 画面中如果同时有操作说明/PPT 内容，单独提取为 `on_screen_text` 证据，不混进语音字幕。
6. 若 OCR 片段清晰而少量有误，可只对冲突片段调用 ASR 辅助核验；避免“视觉字幕有但整集盲目语音重转写”。

**注意**：OCR 只能看到被采样的帧，不等于完整逐帧读取。对短暂出现字幕应自动调整采样率/窗口，在 `manifest` 记录模式与覆盖度，无法保证完整时明确标记。

### Level 4 — Qwen3-ASR（没有可用字幕或 OCR 不足）

必须复用本地 `E:\project\tts` 已有 Qwen3-ASR 工作代码。**执行 Agent 先实地检查该目录**，不要按想象写 `import` 路径，不要直接复制整个 TTS 项目。

实施步骤：

1. 清点目录内：ASR 入口代码、依赖、模型路径、推理参数、硬件适配、音频切块与已有测试/样例。
2. **复制 ASR 实现所需的最小源码和相关配置到本项目的 `src/bilibili_agent/asr/local_impl/`（或更合适的明确位置）**，记录原始来源路径和许可证；如依赖复杂，可先用适配接口调用已有目录，随后再迁移。**不复制原项目中与 ASR 无关的 TTS 训练/推理模块**。
3. 不提交 `.venv`、模型权重、大型缓存、音频数据和密钥；以 `QWEN_ASR_MODEL_PATH` / 配置文件复用本地模型位置。
4. 用 `ffmpeg` 将视频提取为单声道 PCM WAV（推荐 16 kHz，符合适配后的模型需求），将长音频切成可管理片段（静音/VAD 切分，最大切块时长由现有推理实现决定），记录原始全局时间偏移。
5. 调用 Qwen3-ASR 批量/顺序转写；正确处理中文、英文、混合词、无声段；对没有可信语音的片段不补造文字。
6. **时间戳必须可追溯**：若现有实现提供分词/字级时间戳则直接使用；可评估官方 `Qwen3-ForcedAligner-0.6B` 对识别文本对齐。若只有整块转写，就保留块级时间，不按平均语速伪造逐字精准时间。
7. 保留 `source = qwen3_asr` 和模型版本、块偏移、识别警告；逐段异常（幻觉重复、长静音产字、语速离谱、重叠段重复）可以局部复检。

模型官方资料：https://github.com/QwenLM/Qwen3-ASR

### 4.1 自动模式路由

```text
有可用平台字幕？ ──是──► 用平台字幕
        否
有媒体软字幕？ ──是──► 提取软字幕
        否
视频疑似烧录字幕？ ──是──► OCR → 覆盖达标？ ──是──► 用 OCR
                                   │ 否
                                   ▼
                         用 Qwen3-ASR 补缺/校对
        否
        ▼
      Qwen3-ASR
```

**不要强制串行重复劳动**：平台字幕成功就不加载 OCR/ASR；OCR 很差可跳过全量 OCR 直接 ASR。允许用户通过 `--subtitle-source platform|ocr|asr|auto` 显式指定方法。ASR 因 GPU/模型未就绪而不可用时应返回清楚的安装/配置诊断，不把空文本当成功。

## 5. 图片选取与图文 Markdown

### 5.1 抽帧策略

目标不是每隔 10 秒放一张图，而是让 Agent 看得懂**视频里真正讲解的步骤、图表、软件界面、物体结构**。

首版采用确定性、成本可控的混合抽帧，不依赖额外付费视觉 API：

- FFmpeg/OpenCV 识别镜头切换、静态 PPT 页面切换、UI/模型视角明显变化。
- 以字幕段落/主题块时间为锚，优先选接近关键文字且清晰的代表帧（避开转场黑帧、模糊帧、片头片尾、重复帧）。
- 对教程的持续静态画面，可保留长时间不变的一张；对短操作步骤可在同一章节放 2～3 张。
- 可选使用 OCR 提取 PPT/界面中重要文本，作为帧描述附注，不自动把画面推断为事实。
- 图片使用 JPEG/WebP/Png 可配置格式；默认最长边约 1280 px；以感知哈希去重，设置 `max_images`、`min_gap_seconds`、目标图像数量，避免输出上百张重复图。
- 每张图保存 `timestamp_ms`、`frame_id`、与哪些字幕段绑定、选取原因（`scene_change / ui_change / text_slide / transcript_anchor`）。
- 对字幕“点击右上角这个按钮”等依赖画面的语句，必须尽量选该句发生期间的帧；没有匹配画面时不编造具体按钮位置。

首版先做 `scene change + 字幕时间锚 + 图像相似度去重`，效果不够再升级视觉模型做关键帧重要性打分。**不要一开始引入需要 Agent 逐帧主观判断的复杂大模型流水线。**

### 5.2 章节与文本组织

`transcript.md` 保留**完整时间轴文本**，不依赖摘要能否准确；`article.md` 则按时间窗口和话题生成若干节，把对应图片嵌入到临近的文本下面。

章节可用“较长停顿 + 字幕语义变化 + 镜头/幻灯片变化”划分。初版允许规则分段，不强行用 LLM 自动总结；如果调用 Agent 对章节摘要，则必须基于原字幕+画面、把“讲者原文”和“工具摘要”分开，保证可追溯且不能补写未出现的事实。

输出形式（相对路径对 VS Code/Markdown 编辑器友好）：

```markdown
# 视频标题

- 来源：https://www.bilibili.com/video/BVxxxxxxxxxx?p=1
- UP主：...
- 分P：P1 / ...
- 字幕来源：platform_subtitle | soft_subtitle | hard_subtitle_ocr | qwen3_asr
- 生成模式：illustrated
- 说明：转写/识别可能有误，具体以原视频为准

## [00:32–02:15] 界面准备

[00:45] 讲者的实际字幕/转写内容……

![01:03 软件界面关键帧](assets/frame_000063000.jpg)

[01:40] 下一段字幕……

## [02:15–04:10] 操作过程
...
```

## 6. 目录、数据契约和缓存

建议仓库结构：

```text
bilibili/
├─ PLAN.md
├─ README.md                     # 实施时补充安装、示例和状态
├─ pyproject.toml
├─ .gitignore
├─ config.example.toml
├─ src/bilibili_agent/
│  ├─ cli.py                     # bili 命令
│  ├─ mcp_server.py              # stdio MCP
│  ├─ schemas.py                 # SearchHit/VideoPart/Segment/Frame/Job
│  ├─ search.py
│  ├─ inspect.py
│  ├─ bdl_adapter.py
│  ├─ subtitle_resolver.py
│  ├─ subtitle_normalize.py
│  ├─ ocr_subtitles.py
│  ├─ asr/adapter.py
│  ├─ asr/local_impl/           # 从 E:\project\tts 迁移的最小必要实现
│  ├─ frame_selector.py
│  ├─ segmentation.py
│  ├─ md_writer.py
│  ├─ cache.py
│  └─ pipeline.py
├─ skills/bilibili/SKILL.md      # Agent 调用约定
├─ docs/AGENT_SETUP.md           # DSH / Codex / MiniMax Code 配置与验证
├─ tests/
│  ├─ fixtures/                  # 不含受保护的大媒体文件
│  ├─ test_search.py
│  ├─ test_subtitles.py
│  ├─ test_ocr_merge.py
│  ├─ test_frames.py
│  ├─ test_writer.py
│  └─ test_pipeline.py
└─ output/                       # gitignore，不提交下载的视频
```

每个视频 P 的本地结果目录：

```text
output/
└─ BVxxxxxxxxxx/
   └─ P01_<cid>/
      ├─ manifest.json           # bvid/aid/cid/元数据/来源/状态/产物/版本
      ├─ transcript.srt          # 有文本时间戳时生成
      ├─ transcript.md           # 全量文本，可按时间戳搜索
      ├─ article.md              # 图文模式生成
      ├─ assets/
      │  ├─ frame_000063000.jpg
      │  └─ frame_000142000.jpg
      └─ raw/                    # 原始字幕等可选，临时音视频不必永久保留
```

数据结构至少统一成：

- `Segment {start_ms, end_ms, text, source, confidence?, flags?}`。
- `Frame {time_ms, relative_path, reason, linked_segment_ids[]}`。
- `Manifest {bvid, aid?, cid, page, source_url, title, author, duration_ms, generated_at, subtitle_source, model?, pipeline_version, settings_digest, files[], warnings[], coverage?}`。
- `Job {id, state, stage, progress?, output_path?, error?}`：`queued / running / completed / failed / cancelled`。

缓存键至少覆盖 `bvid+cid+mode+字幕语言+算法/模型版本+相关参数`。同设置重复运行直接复用产物；模式从 `subtitle` 升为 `illustrated` 时复用字幕数据、增补视频和图片。下次运行若需要更新在线字幕可显式 `--refresh`。失败保留可复用中间结果，不应把不完整文件标记为完成。

运行可配置下载缓存目录、输出目录、临时目录、BDL CLI 路径、FFmpeg 路径、Cookie 文件路径、ASR 模型路径。对 Windows 中文路径/空格路径使用 subprocess 参数数组而非字符串拼接。

## 7. Agent 接口：先 CLI，再 MCP

### 7.1 本项目目标 CLI（待实现）

```powershell
bili doctor
bili search "MMD PMX Editor 换装" --limit 10 --json
bili inspect BVxxxxxxxxxx --json
bili fetch BVxxxxxxxxxx --parts 1 --mode subtitle --json
bili fetch BVxxxxxxxxxx --parts 1 --mode illustrated --subtitle-source auto --max-images 25 --json
bili fetch "https://www.bilibili.com/video/BVxxxxxxxxxx?p=2" --mode both --json
bili status <job_id> --json
```

要求：

- 所有命令提供 `--help`、非交互用 `--json`，明确 stdout 数据、stderr 日志及退出码。
- `doctor` 检查 BDL CLI、FFmpeg/FFprobe、配置、OCR、Qwen3-ASR 和 GPU 可用性；允许仅字幕路线部分依赖缺失。
- `search` 只搜索不下载；`inspect` 元信息；`fetch` 严格按选中 P/模式执行。
- 返回结果至少包含 `ok, status, bvid, parts, outputs, warnings`；失败包含 `stage, error_code, message, retryable`。
- CLI 可同步返回最终结果；耗时操作支持持久本地任务 ID + `status` 查询/取消，避免 Agent 工具调用超时。Windows 子进程正确收集 stdout/stderr，退出码不为零时不误报完成。
- 默认同时只运行少量任务，避免重复启动大型 ASR 模型和竞态写文件；用户可取消；停止任务不删已完成且校验通过的输出。
- 下载全片音视频只有在 OCR/截图或用户明确需要时启动；纯字幕优先直读字幕，ASR 情况下用 `audio-only`。

### 7.2 MCP 工具（stdio，本机，无需公网服务）

最低工具集：

1. `bili_search(query, limit?, sort?) -> SearchHit[]`
2. `bili_inspect(url_or_bvid) -> VideoInfo/VideoPart[]`
3. `bili_materialize(url_or_bvid, parts?, mode?, subtitle_source?, max_images?) -> job/output`
4. `bili_job_status(job_id) -> Job`
5. `bili_read_transcript(bvid, part?, start_ms?, end_ms?) -> text + source_url`（可选，但长视频建议做，避免一次读爆上下文）

必须提供简洁的 Schema 描述及参数枚举，不暴露原始 Cookie、签名 URL。工具输出长字幕时只返回文档路径、章节索引与摘要元数据，Agent 按需读章节；不能一次把几万字及几十张图片塞入 MCP 响应。

`skills/bilibili/SKILL.md` 定义调用决策：先搜索→检查→选中→根据问题选 `subtitle` 或 `illustrated`→读取实际产物→按视频时间戳引用；**不能把搜索摘要当成视频的已验证内容**。适配 DSH、Codex、MiniMax Code 的具体 MCP 注册方式应在本机根据对应客户端支持情况验证，再写入 `docs/AGENT_SETUP.md`，不要杜撰配置字段。没有 MCP 支持时仍可直接运行 CLI。

## 8. 实施顺序和完成标准（按 P0→P4 实施，不要同时铺开）

### P0 — 环境探查与最小下载链路（先完成）

- [ ] Windows 检查已安装桌面 BDL 的版本，确认独立 CLI 是否已安装；优先使用 v0.9.1 或与接口兼容的后续稳定版。
- [ ] 配置 BDL CLI、FFmpeg、FFprobe，可传入 BV 并正确获得 `parse --json`、`download --json` 数据。
- [ ] 先列目录查看 `E:\project\tts` 的 ASR 实现与模型，不推测代码入口；确认 GPU 和所需依赖。
- [ ] 创建 Python 项目、`bili doctor`、`BdlAdapter` 与配置示例。
- [ ] 一个真实可访问普通视频选 P1，可以得到真实元信息并把文件路径返回给脚本，不误用桌面版程序。

**验收 P0**：从终端传真实 BV → 得到 P1 的 CID/标题 → 使用 BDL CLI 下载 → JSON 记录实际文件路径和 FFprobe 信息。

### P1 — 搜索 + 平台字幕 + 纯文字 MD（第一个可交付版本）

- [ ] B 站关键词搜索，给 Agent 候选列表；`inspect` 返回 BV 分 P。
- [ ] 可用平台字幕提取、JSON/SRT 转换、中文语言选择、时间戳校验。
- [ ] `transcript.srt`、`transcript.md`、`manifest.json` 生成、幂等缓存。
- [ ] CLI `search / inspect / fetch --mode subtitle` 的 JSON 协议稳定。
- [ ] 先完成 CLI 的端到端调用；更新 README 的使用示例。

**验收 P1**：Agent 能按主题发现视频、选择 BV/P、无需视频下载就获得真实字幕 Markdown，可通过原链接与时间戳核查，且二次运行命中缓存。

### P2 — OCR + 本地 Qwen3-ASR 兜底

- [ ] 实现软字幕探测、烧录字幕 OCR 检测与相邻帧合并。
- [ ] 按实际路径从 `E:\project\tts` 复制最小 ASR 代码，封装 `ASRAdapter`；模型路径可配置。
- [ ] 没有字幕时自动下载音频/视频，OCR 失败或不完整时转 ASR。
- [ ] 保留片段时间戳来源和识别方式；必要时整块时间戳而非假精确字级时间戳。
- [ ] 加入语音、无语音、混合语言、静音重复、烧录字幕低覆盖等测试样例。

**验收 P2**：平台字幕有/无、烧录字幕有/无的三类视频，至少能走通官方字幕、OCR、ASR 三条路线；文字内容有实际时间定位，无法识别时明确失败而不是生成空文档。

### P3 — 图文 MD + 视频教程可读性

- [ ] FFmpeg/OpenCV 提帧；按镜头、画面变化、字幕时间选关键帧。
- [ ] 去重、黑帧/模糊帧过滤、图片大小与张数限制。
- [ ] 段落与代表帧对齐，输出 `article.md`，图片相对路径在 VS Code 可直接显示。
- [ ] 多 P 独立生成并可汇总 `index.md`；图像不匹配文字时不要编造画面描述。
- [ ] 可针对软件教程、3D 建模讲解、PPT 授课视频评估漏掉关键步骤/截错图片情况。

**验收 P3**：对一段 10～30 分钟的实际软件教程，生成的 MD 在 VS Code 中可直接打开；操作步骤附近有不同的有效截图，图片和字幕大体同步，没有大量重复图，也不缺来源链接。

### P4 — MCP / SKILL 接入真实 Agent 与自动回归

- [ ] 完成 MCP stdio 工具、长任务状态/读取接口、`skills/bilibili/SKILL.md`。
- [ ] 分别用 **DSH、Codex、MiniMax Code** 测试一条真实完整任务；若客户端 MCP 注册不兼容则先用 CLI 工作流，不阻塞交付。
- [ ] 输出基础文档 `docs/AGENT_SETUP.md`，涵盖安装、MCP 注册、CLI 兜底、示例提示词。
- [ ] 测试搜索→多结果筛选→指定 P 下载→生成→Agent 读图文→回答引证。
- [ ] 编写完整回归矩阵与可脱网运行的 mock/fixture 测试，重新跑已缓存产物应尽量不联网、不重复转写。

**验收 P4**：三个 Agent 至少各成功执行一次搜索→锁定→读取原始内容→根据内容回答，回答可回链到 BV 和时间戳；接口需由 Agent 自行调用，而非人工转发视频给它。

## 9. 检测与质量度量

优先衡量“提取结果真实、可用、低成本”，而不是给没有依据的主观分数。

| 指标 | 实现建议 | 最小验收 |
| --- | --- | --- |
| 检索有效性 | query→候选 BV 的相关度人工抽检 | 能找到与测试主题有关的可用视频 |
| 字幕覆盖率 | 有效字幕时间范围 / 视频语音相关时段，近似指标需说明 | 不把 20 秒字幕当整段 20 分钟覆盖 |
| OCR 重复率 | 重复句/总句、时间段冲突 | 相同字幕连续帧合并，不每帧重复 |
| ASR 可追溯性 | 每条文本有至少块级时间和 `source` | 无假精准时间、无长静音幻觉 |
| 图文对齐 | 图片 timestamp 是否在相邻讲解窗口 | 图片对应相关讲解，不整篇图文错位 |
| 关键帧多样性 | 感知哈希相似图占比 | 去掉大量几乎相同的帧 |
| 资源效率 | 是否无必要下载视频/调用 ASR、缓存命中 | 有字幕的纯文字模式不下载完整视频 |
| 可复现性 | manifest + pipeline 版本 + 模型/参数 + 元数据 | 输出能追溯视频/P/来源与配置 |

人工回归视频至少覆盖：有平台字幕、仅烧录字幕、只有语音、教程类图多字少、长视频/多 P、无可用音轨/不可访问视频各一例。测试素材需要合法可访问；只在本地保留小片段用于调试，不向公开 Git 仓库提交受版权保护的原视频、字幕或含账号信息的真实请求。

## 10. 开发边界和容易踩的坑

1. **BDL 桌面端和 CLI 是两个产品入口**：当前 GUI 能下载字幕，不表示当前 CLI 支持。首版直接用现成 CLI 下载媒体，字幕单做适配层。
2. **字幕的四类来源必须记录**：平台字幕、软字幕、烧录字幕 OCR、ASR，质量和时间精度不同。
3. **图文不能简单固定间隔抽帧**：UI 操作、PPT 页面和讲解时间必须相互关联；不用 LLM 也可以先实现够用的画面变化检测。
4. **Agent 搜索与获取是两件事**：仅返回标题/简介不能声称看过视频；只有成功获得字幕、音频识别或画面才可引用内容。
5. **长内容控制上下文**：MCP 不直接返回全部帧或全文；返回文件地址和章节索引，由 Agent 精读需要的部分。
6. **B 站接口会变化/可能受风控**：明确处理 WBI、Cookie 与限制信息；不进行风控绕过，不预设所有视频可用。
7. **不要无差别复制整个 TTS 工程**：按本地实际依赖复制 ASR 部分，模型与 venv 留在用户本地配置路径。
8. **Windows 路径**：空格、中文和盘符测试；程序用参数数组执行 CLI，避免 shell 转义导致的错误。
9. **遵守账号权限和版权**：本地知识化供个人研究；不要默认公开分发下载的视频、完整字幕和大量截图；服务端凭据/签名 URL 不入 Git。
10. **优先完成可运行闭环**：遇到“复杂 OCR/提帧质量优化”问题，先让 P1、P2 的实用路径通过真实样本，再做复杂算法；不要只写架构、反复重构而迟迟没有可用成果。

## 11. 给实施 Agent 的直接任务单

> 阅读本 `PLAN.md` 后，**从 P0 开始实施，不要重新写一份空泛的大计划，也不要把 P0～P4 一次性全部做成未经验证的骨架**。
>
> 1. 实地检测 `bdl.exe`、`ffmpeg`、`E:\project\tts`，确认 ASR 代码实际位置。使用真实命令输出而非猜测。
> 2. 实现 P0 和 P1 的最短可运行闭环；写测试并执行。把真实试运行成功与失败现象（不含 Cookie）记入 `docs/PROGRESS.md`。
> 3. 通过 P1 后实施 P2→P3→P4。每阶段更新进度、测试结论和下一步。遇到 BDL CLI 缺失功能时用独立适配模块，不先 fork 整个上游。
> 4. 保持模块简单，优先利用现有 BDL/Qwen3-ASR；小范围验证成功后再做全量自动化。完成时提供一条可复制的 `bili search` 与 `bili fetch` 示例，以及 Agent 实际读到的本地图文 MD 路径。

---

### 相关技术参考

- BDL 项目：https://github.com/Yuelioi/bdl
- BDL CLI：https://github.com/Yuelioi/bdl/blob/main/docs/CLI.md
- BDL 视频解析：https://github.com/Yuelioi/bdl/blob/main/crates/bdl-core/src/resolver/video.rs
- BDL 字幕转换：https://github.com/Yuelioi/bdl/blob/main/crates/bdl-core/src/subtitles.rs
- B 站 API 社区接口索引（以实测为准）：https://github.com/SocialSisterYi/bilibili-API-collect
- Qwen3-ASR：https://github.com/QwenLM/Qwen3-ASR
