# bilibili-agent

把 B 站视频变成 Agent 可读、可引用的本地 Markdown。

> 搜索 → 锁定 BV/分 P → 拿到字幕/关键帧 → 生成本地 `.md` + `manifest.json` → Agent 按时间戳引用。

实施进度与**真实试运行记录**见 [`docs/PROGRESS.md`](docs/PROGRESS.md)；
总体设计与分阶段验收标准见 [`PLAN.md`](PLAN.md)。

## 现在能做什么

| 能力 | 状态 |
| --- | --- |
| 关键词搜索（B 站公开接口，带风控识别） | ✅ |
| `inspect` 解析分 P / cid / 时长 / 简介 | ✅ |
| 扫码登录（本地渲染二维码，对齐 BDL 实现） | ✅ |
| B 站官方/AI 字幕（Level 1） | ✅ 需登录 |
| 媒体内独立字幕轨（Level 2） | ✅ 已实现，真实样本少见 |
| 画面烧录字幕 OCR（Level 3） | ✅ RapidOCR 3.x + **PP-OCRv6**，四维 ROI 时序跟踪 |
| 本地 Qwen3-ASR 转写（Level 4） | ✅ 块级 + 强制对齐**字级**时间戳 |
| 关键帧抽取 + 图文 `article.md` | ✅ 场景切换 + 字幕锚点 + 感知哈希去重，按时间分桶保证全片覆盖 |
| stdio MCP（5 个工具）+ Agent SKILL | ✅ `wait=false` 走真异步任务（持久 job_id + 状态查询） |

## 环境要求

| 组件 | 用途 | 说明 |
| --- | --- | --- |
| Python 3.11+ | 流程层 | 推荐 3.12 |
| [BDL CLI](https://github.com/Yuelioi/bdl/releases) | 解析与下载媒体 | 独立的 `bdl.exe`，**不是**桌面版 |
| FFmpeg / FFprobe | 转码、抽帧、校验 | `winget install Gyan.FFmpeg` |
| （可选）Qwen3-ASR | 无字幕时的语音转写 | 复用本机已有模型与虚拟环境 |

## 安装

```powershell
cd E:\project\bilibili
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
uv pip install --python .venv\Scripts\python.exe -e ".[mcp]"   # 需要 MCP 时
Copy-Item config.example.toml config.toml                      # 按需修改
```

`config.toml` 已被 `.gitignore` 忽略；Cookie 等凭据永远不会写进产物或 Git。

## 命令

```powershell
bili doctor                                   # 检查依赖
bili login                                    # 扫码登录（打开网页，用 B 站 App 扫码）
bili whoami                                   # 查看当前登录账号
bili logout                                   # 清除本地登录态
bili search "MMD PMX Editor 换装" --limit 10 --json
bili inspect BVxxxxxxxxxx --json
bili fetch BVxxxxxxxxxx --parts 1 --mode subtitle --json
bili fetch BVxxxxxxxxxx --parts 1 --mode illustrated --subtitle-source asr --max-images 25 --json
bili fetch "https://www.bilibili.com/video/BVxxxxxxxxxx?p=2" --mode both --json
bili status <job_id> --json
```

* stdout 只输出数据，stderr 输出日志；退出码 `0` 成功 / `1` 运行失败 / `2` 参数错误。
* `search` 只搜索不下载；`fetch` 严格按选中的分 P 与模式执行。
* `--mode`：`metadata`（只看元信息）、`subtitle`（纯文字）、`illustrated`（图文）、`assets`（取图）、`both`。
* `--subtitle-source`：`auto`（自动路由）/ `platform` / `ocr` / `asr`。
* `--refresh` 忽略缓存重新生成；`--async` 后台执行，用 `bili status <job_id>` 查询。
* `bili media <文件路径> --json` 用 ffprobe 校验已下载媒体。

## 字幕来源与路由

```text
有平台字幕？ ──是──► platform_subtitle
        否
有媒体软字幕？ ──是──► soft_subtitle
        否
疑似烧录字幕？ ──是──► 字幕带 OCR ──► hard_subtitle_ocr
                                  │
                     覆盖率不达标？ ──是──► 本地 Qwen3-ASR ──► qwen3_asr
        否
        ▼
  本地 Qwen3-ASR ──► qwen3_asr（块级时间戳）
        │
     配了 ForcedAligner
        └──────────► qwen3_asr（字/词级时间戳）
```

产物里会标明实际用了哪一级，时间精度也不同——
ASR 没有对齐模型时只给块级时间戳，**不会按平均语速伪造逐字时间**。

### 烧录字幕 OCR（Level 3）

```powershell
uv pip install --python .venv\Scripts\python.exe -e ".[ocr]"
bili fetch BVxxxxxxxxxx --parts 1 --mode subtitle --subtitle-source ocr
```

* 引擎：**RapidOCR 3.10 + ONNX Runtime + PP-OCRv6**（纯 CPU 可跑，不依赖 torch）。
* **字幕区自动检测**（`band = "auto"`，默认）：按时间分层取多个短窗口做整帧 OCR，
  把文字框跨帧关联成轨迹，聚合成**四维 ROI**（x0,y0,x1,y1）；
  选区依据可解释特征（底部只是软先验、水平居中、字大、样式稳、内容按句替换），
  永久 UI 文字与瞬时弹窗是负向特征。检测结果与 evidence 写进 manifest。
* **变化触发式识别**：ROI 画面几乎没变就跳过 OCR、沿用上一帧文本——
  于是"同一句字幕持续好几秒"会被完整保留，不会被误删。
* 跨帧长期不变的那一段判为界面文字，转入 `manifest.extra.visual_text` 独立通道，
  **不混进对白 transcript**。
* 字幕区内的文字按行分组，支持 1~3 行（双行/双语），行间保留换行。
* 覆盖率按**时间并集**计算（`segment_time_coverage`），不是首尾跨度；
  低于阈值时 auto 模式会用本地 ASR 补缺口，两边文本冲突会同时保留原始证据。
* 字幕起止时间精度 = 采样间隔（默认 1s），manifest 里标为
  `time_precision="sample_interval"`，不伪装毫秒级精准。

> OCR 只看被采样的帧，不等于逐帧读取；短暂出现的字幕可能被漏掉，
> 这一点会写进 manifest 的 `coverage` 字段。

> ⚠️ B 站官方/AI 字幕在**未登录**时一律取不到（接口返回 `need_login_subtitle=true`）。
> 需要 Level 1 时，先执行 `bili login` 扫码登录（见下节）。

## 登录

和 BDL 桌面版一样是"扫码登录"，但实现方式更干净：**CLI 自己渲染二维码，不开网页**，
所以不受"浏览器已登录导致 B 站页面跳过二维码"的影响，也不需要复制粘贴任何凭据。

```powershell
bili login          # 渲染二维码 → 用 B 站 App 扫码并在手机上确认 → 自动保存登录态
bili whoami         # 确认当前账号
bili logout         # 清除本地登录态
```

* 走 B 站网页版扫码的公开接口（`qrcode/generate` + `qrcode/poll`），
  与 BDL 桌面版所用的 bpi-rs 完全一致；二维码由本地 `segno` 渲染成 PNG 后自动打开。
* 登录态写到 `.cache/cookie.txt`（UTF-8 Cookie header，已被 `.gitignore` 忽略）。
  这个格式 BDL CLI 的 `--cookie-file` 可以直接复用。
* Cookie 不进日志、不进 Markdown、不进 Git；`bili logout` 一键清除。
* 登录后 `bili doctor` 的 `cookie` 项变成 `OK`，Level 1 平台字幕随之可用。

> 登录是**必须**的：未登录时 B 站的官方/AI 字幕一律取不到（接口返回
> `need_login_subtitle=true`），此时只能走本地 ASR 兜底。

## 产物

```text
output/BVxxxxxxxxxx/P01_<cid>/
├─ manifest.json     # 元数据、字幕来源、覆盖度、版本与参数摘要
├─ transcript.srt    # 带时间戳的字幕
├─ transcript.md     # 完整时间轴文本，可按时间戳搜索
├─ article.md        # 图文模式产出（图片相对路径）
├─ assets/           # 关键帧
└─ raw/              # ASR 原始结果等
```

Markdown 头部会写明来源链接、UP 主、分 P、字幕来源与生成时间，Agent 引用时可回链到 BV 与时间戳。

## 作为 Agent 工具

```powershell
# Codex / DSH / MiniMax Code 通用：直接用 CLI
bili search "关键词" --json
```

MCP（stdio）服务器与 `skills/bilibili/SKILL.md` 见
[`docs/AGENT_SETUP.md`](docs/AGENT_SETUP.md) 与 [`skills/bilibili/SKILL.md`](skills/bilibili/SKILL.md)。

## 开发

```powershell
.venv\Scripts\python.exe -m pytest -q
```

测试全部离线可跑（不联网、不下载视频），用 mock 与本地夹具。

## 说明与边界

* 仅供个人研究使用：不要公开分发下载的视频、完整字幕与大量截图。
* 平台字幕、媒体软字幕、烧录字幕 OCR、语音转写是四种不同来源，质量与时间精度不同，产物里会分别标注。
* 遇到 B 站风控（HTTP 412/429）会立即停止并报错，不做绕过。
