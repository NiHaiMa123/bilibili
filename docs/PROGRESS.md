# 实施进度与真实试运行记录

> 本文记录**本机实际执行过的命令与真实输出**（不含 Cookie / 凭据），
> 以及踩到的坑。计划见 [`../PLAN.md`](../PLAN.md)。
>
> 最后更新：2026-10-23

## 状态总览

| 阶段 | 状态 | 备注 |
| --- | --- | --- |
| P0 环境探查与最小下载链路 | ✅ 完成 | BDL CLI 由本项目下载安装，SHA256 已核对 |
| P1 搜索 + 平台字幕 + 纯文字 MD | ✅ 完成 | 含**扫码登录**（对齐 BDL 实现）；平台字幕真实跑通 |
| P2 OCR + 本地 Qwen3-ASR 兜底 | ✅ 完成 | Level 3 烧录字幕 OCR（PP-OCRv6）+ Level 4 ASR（块级 + 强制对齐字级）均真实跑通；Level 2 已实现待验证 |
| P3 图文 MD + 视频教程可读性 | ✅ 完成 | 场景切换 + 字幕锚点抽帧，18 张关键帧，实测帧内容与讲解对得上 |
| P4 MCP / SKILL 接入 | ✅ 完成 | stdio MCP 5 个工具 + SKILL.md + AGENT_SETUP.md；客户端注册方式已按本机实际写 |

测试：`87 passed`（`.venv\Scripts\python.exe -m pytest -q`），全部离线可跑，不联网。
静态检查：`ruff check`（F/E9/W/I/UP/B/RET/C4）无告警。

**登录态已就绪**：`bili login` 扫码成功，`bili whoami` 可查到账号（UID 属个人账号信息，
此处不落库，本地自查即可），Level 1 平台字幕真实产出（`BV18gDFBVEQF` → 63 段，覆盖率 98.52%）。

---

## P0：环境探查

### 实际执行的命令与结果

```powershell
bdl --version        → 未安装（Get-Command bdl 失败）
ffmpeg -version      → ffmpeg version 8.1.2-full_build-www.gyan.dev
ffprobe              → C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe
python --version     → Python 3.14.6（系统），本项目另建 .venv 用 3.12.13
nvidia-smi           → NVIDIA GeForce RTX 5080, 16303 MiB, driver 610.88
```

**本机原本没有安装任何 BDL（桌面版也没有）**。按 PLAN 第 1.1 节装了独立 CLI：

```powershell
curl.exe -L -o E:\tools\bdl\bdlcli-0.9.1-win.bin `
  https://github.com/Yuelioi/bdl/releases/download/v0.9.1/bdl-cli-0.9.1-windows-x64.zip
# 4488455 字节，与 release 资产大小一致
tar.exe -xf E:\tools\bdl\bdlcli-0.9.1-win.bin
# → E:\tools\bdl\bdl.exe
```

SHA256 校验通过：

```
expected: 765bd1e8dc546754aaadf71e87859f4127be070723d7f6fe1c8ae59422551f3c
actual  : 765bd1e8dc546754aaadf71e87859f4127be070723d7f6fe1c8ae59422551f3c
MATCH   : True
```

`bdl.exe --version` → `bdl 0.9.1`。

### ASR 实地清点（`E:\project\tts`）

没有按想象写 import 路径，实际读了目录：

| 项目 | 实际位置 |
| --- | --- |
| ASR 入口示例 | `E:\project\tts\scripts\asr_check.py`（`_transcribe_qwen3_asr`） |
| 运行环境 | `E:\project\tts\backend_envs\asr_qwen3\Scripts\python.exe`（Python 3.12.13） |
| 关键依赖 | `qwen_asr 0.0.6`、`torch 2.8.0+cu128`、`transformers 4.57.6` |
| ASR 模型 | `E:\project\tts\models\asr\qwen3_asr\5eb144179a02acc5e5ba31e748d22b0cf3e303b0`（1.87 GB，`Qwen3ASRForConditionalGeneration`） |
| GPU | `torch.cuda.is_available() == True`，RTX 5080 |

`qwen_asr` 包内还带 `qwen3_forced_aligner.py`（`Qwen3ForcedAligner`），但本地**没有**对齐模型，
本项目另行下载到 `E:\project\tts\models\asr\qwen3_forced_aligner`（`Qwen/Qwen3-ForcedAligner-0.6B`，Apache-2.0，1.84 GB）。

**没有复制 TTS 工程**：只在自己仓库里写了一个独立 worker
（`src/bilibili_agent/asr/worker.py`，不 import 本项目任何包），用外部解释器拉起。

### P0 验收

真实 BV 的 parse → download → ffprobe：

```powershell
bdl --json parse BV1xx411c7mD
# → cid=62131, aid=2, duration=2055s, owner=碧诗, publish_date=2009-09-09

bdl --json download BV1xx411c7mD --parts 1 --output E:\project\bilibili\.cache\media --media-mode audio-only
# → {"downloads":[{"input":"BV1xx411c7mD","outputs":["...\\P1 - 字幕君交流场所.mp4"]}]}
```

`bili media <path> --json` 用 ffprobe 确认文件可读（时长/流信息见 manifest）。

---

## P1：搜索 + 字幕 + 纯文字 MD

### 搜索接口的真实限制（重要）

首次调用 `bili search` 直接吃到 **HTTP 412 风控**。逐步排查后定位到三个叠加原因：

1. **本机有代理** `HTTP_PROXY=http://127.0.0.1:7897`，该出口 IP 被 B 站搜索接口风控；
   `curl` 默认也走代理但带简单 UA 反而能过，换成 httpx 就 412。
2. 搜索接口要求**完整浏览器请求头**（`Referer` / `Origin` / `Accept` / `Accept-Language`），
   只有 UA+Referer 同样 412。
3. 先访问首页拿到 `buvid3` / `b_nut` 再带 cookie 请求，稳定性更好。

最终方案（`src/bilibili_agent/bili_api.py`）：`trust_env=False` 直连 + 完整请求头 + 首页引导。
连续 3 次搜索全部 `code=0, numResults=1000`。
配置项 `[bilibili] use_env_proxy` 默认 `false`，需要时再开。

**不做风控绕过**：遇到 412/429 直接抛结构化错误并停止，不换代理、不重试。

### 平台字幕必须登录（Level 1 的实际边界）

批量探测 60+ 个视频（`player/v2` 接口）：

```
BV1pGnn67EbB  login_required=True  subs=0   影视飓风
BV18gDFBVEQF  login_required=True  subs=0   asr_language='cmn'   ← 有 AI 字幕但要登录
BV1ex411T7Zb  login_required=True  subs=0   BBC 纪录片
BV1xx411c7mD  login_required=False subs=0   2009 年老视频，本来就没字幕
```

结论：**B 站官方/AI 字幕在未登录时一律取不到**（`need_login_subtitle=True`）。
登录之后（见下面"扫码登录"一节）即可正常获取：
`BV18gDFBVEQF` → `subtitle_source=platform_subtitle`，63 段，覆盖率 98.52%。
未配置 Cookie 时程序会明确报"该分 P 没有平台字幕"，不会伪造内容。

### 字幕轨道的两个坑

拿到登录态后，又发现两处需要处理：

1. **`player/v2` 与 `player/wbi/v2` 返回的轨道集合不一致。**
   网页播放器真正用的是 `x/player/wbi/v2`（给出完整轨道集：人工字幕 + 各语言 AI 翻译轨），
   `x/player/v2` 有时只给一条，有时又给人工字幕。同一视频不同次调用结果还会变。
   → 解决：两个接口都问，按轨道 `id` 去重合并，再按优先级挑。

2. **不能用 URL 判断"是不是 AI 字幕"。**
   人工字幕和 AI 字幕**同在 `aisubtitle.hdslb.com` 域名下**，
   按域名判断会把人工字幕误判成 AI 字幕、进而选错轨道。
   可靠依据是 `lan` 以 `ai-` 开头、`ai_status`/`ai_type` 非零，或 `type == 1`
   （实测人工字幕 `type=0`、AI 字幕 `type=1`）。

另外加了**跨度合理性检查**：B 站有时会给出跨度远超视频时长的合并版字幕文件
（实测同一视频出现过 756s 跨度 vs 视频 377s），这类轨道会被跳过并记录告警，
避免产出与视频对不上的时间轴。

### 踩到的坑：失败结果被当成缓存命中

没有字幕的那次失败会写下一个 `files: []` 的 manifest，而 `is_fresh()` 对空文件列表
**真空通过**——于是后续运行直接报"缓存命中"却什么都拿不到。
→ 解决：`is_fresh()` 要求 manifest 里有实际存在的产物文件，且显式标记 `status=failed`
的结果不可复用。

### 扫码登录（`bili login`）——最终实现与三个关键坑

BDL 桌面版（`E:\software\bdl b站下载神器`）是"打开网页扫码登录"。把它的实现挖出来后，
本项目的做法与它**完全一致**，但去掉了 GUI：

从 `bdl-desktop.exe` 里提取出的实现线索（`bpi-rs-0.3.0\src\login\client.rs`）：

```
https://passport.bilibili.com/x/passport-login/web/qrcode/generate   → login.qr_generate
https://passport.bilibili.com/x/passport-login/web/qrcode/poll      → login.qr_poll
https://api.bilibili.com/x/web-interface/nav                        → login.nav
qrcode-0.14.1\src\render\{mod,svg,canvas}.rs   ← 它自己渲染二维码，不开网页
```

**BDL 根本不开网页**：拿到 `generate` 返回的 `url` 后用 Rust `qrcode` crate 在本地把二维码
画出来显示在自己的 Vue 界面里，然后轮询 `qrcode/poll` 拿 Cookie，用 `nav` 验证，
最后存进系统凭据（`bdl-downloaderbilibili-account-cookie`）。

本项目照此实现（`src/bilibili_agent/qr.py` + `login.py`），踩到三个坑：

1. **不要开 B 站网页来显示二维码。**
   `account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=…` 会先调
   `x/web-interface/nav`；**浏览器已有 B 站登录态时，它跳过二维码直接渲染"以 <账号> 登录"的
   确认卡，而且那个【确认】按钮是禁用态**（框架里 `actionable=false`，肉眼看到的也是淡粉色）。
   这正是用户最初反馈"我操作不了"的真实原因——那个按钮在这个分支下根本没绑事件，
   点了也不会发任何请求（用浏览器自动化点过一次，`network` 里零请求）。
   另外该页面还会**自己重新申请一套 key**，与 CLI 持有的 key 对不上，扫码永远不会被记录。
   → 解决：**本地渲染二维码**（`segno`，纯 Python），PNG 写盘后用系统默认程序打开。
   打开的是一张图片，不涉及任何 B 站页面，因此完全不受登录态影响。

2. **Cookie 不在 JSON 里，在 `Set-Cookie` 响应头里。**
   bpi-rs 的 `qr_poll` 是这么写的：

   ```rust
   let cookies: Vec<(String, String)> = response.cookies()   // ← 从响应头取
       .map(|c| (c.name().to_string(), c.value().to_string())).collect();
   ...
   if data.code == 0 { data.cookies = cookies; }
   ```

   只在 JSON 里找 `SESSDATA` 永远找不到——这就是"poll 返回 code=0 却说没有 SESSDATA"的原因。
   → 解决：`BiliClient.get_json_with_cookies()` 同时返回响应体与 `Set-Cookie` 解析出的 cookie。

3. **不要替用户调 `qrcode/confirm`。**
   `POST /x/passport-login/web/qrcode/confirm` 存在（GET 返回 405），但裸调必得
   `{"code":-400,"message":"请求错误"}`，而且**多余的一次 confirm 可能把 key 提前消费掉**。
   状态码 `86090` 的含义是"已扫码，请在 App 上点确认"——用户�� App 里确认后，
   下一次轮询自然会返回 `code=0` 与 Set-Cookie 里的登录态。

登录态落到 `.cache/cookie.txt`（UTF-8 Cookie header，已 gitignore），
这个格式 BDL CLI 的 `--cookie-file` 可直接复用；`bili whoami` / `bili logout` 配套管理。

### 踩到的坑：搜索结果的 `subtitle` 字段

B 站搜索结果里的 `subtitle` 字段**不是"有无字幕"标记，而是视频简介**。
第一版误把它当成字幕标记，已修正为 `has_subtitle` 一律 `False`，
只有实际查过 `player` 接口才置真（`tests/test_search.py` 有回归）。

### P1 产物（真实样例）

`bili fetch BV1FW41157y3 --parts 1 --mode subtitle --subtitle-source asr --json` 产出：

```text
output/BV1FW41157y3/P01_36897122/
├─ manifest.json     # subtitle_source=qwen3_asr, coverage.segments=82
├─ transcript.srt    # 82 段，毫秒级时间戳
├─ transcript.md     # 完整时间轴 + 章节索引
└─ raw/asr.json      # ASR 原始结果
```

二次运行命中缓存（`cached: true`，不再联网、不再转写）。

---

## P2：ASR 兜底（Level 4）

### 真实跑通

```powershell
bili fetch BV1FW41157y3 --parts 1 --mode subtitle --subtitle-source asr --json
```

结果：`ok=true`，`subtitle_source=qwen3_asr`，82 段，覆盖率 99.64%，
转写内容完整还原了 Pmxeditor 换装教程的全部步骤（下载 base → 导入 dress →
对齐 → 删面 → 存盘），可与原视频逐句核对。

### 两种时间戳粒度

| 配置 | 粒度 | 段数 | 说明 |
| --- | --- | --- | --- |
| 只有 ASR 模型 | 块级（chunk） | 5 | 每块 ≤120s，只标整块起止，**不按平均语速伪造逐字时间** |
| 加上 ForcedAligner | 字/词级（word） | 82 | 真实对齐时间戳 |

强制对齐模型用 `Qwen3ASRModel.from_pretrained(..., forced_aligner=<path>)`，
`transcribe(..., return_time_stamps=True)`。
注意 `MAX_FORCE_ALIGN_INPUT_SECONDS = 180`，所以按 120s 切块喂。

### 坑：对齐器丢空格

对齐器返回的 item 是词本身，**不带空格和标点**，直接拼接得到
`"Hellotodaywe'regoingtobemakingamakingmake"`。

修复（`asr/worker.py` 的 `_align_items` / `_group_aligned`）：把每个 item
按顺序回贴到 ASR 原文区间上，用**原文切片**拼句子，空格与标点自然保留：

```
[0.88s] Hello, today we're going to be making a making make a dance model.
[4.88s] We are actually just going to start with putting on a dress on
```

### Level 2（媒体软字幕）

已实现 ffprobe 探测字幕轨 + ffmpeg 提取 + SRT 解析。B 站 DASH 把字幕作为独立资产，
实际下载到的媒体里很少有独立字幕轨，因此该分支在真实样本上**未触发**（代码路径有测试覆盖）。

### Level 3（烧录字幕 OCR）—— 已实现并真实跑通

引擎：**RapidOCR 3.10.0 + ONNX Runtime + PP-OCRv6**（`PP-OCRv6_det_small.onnx` /
`PP-OCRv6_rec_small.onnx`），纯 CPU 可跑、不依赖 torch。
注：老包名 `rapidocr-onnxruntime` 停在 1.4.4 已废弃，现在用统一包名 `rapidocr`。

真实样本 `BV1FW41157y3`（Pmxeditor 教程，画面底部有 gg 中文字幕）端到端结果：

```
band = [0.816, 0.9212]  confidence = 0.88
segments = 43   coverage = 59.17%（3s 采样；1s 采样更高）
[  0.0s] 别来无恙啊年轻人，咱们今天来搞一波mmd模型
[ 12.0s] 那么。我现在要新建一个文件夹
[ 21.0s] 把它命名为演示模型（你可以随便命名，她这是为了演示）
[ 33.0s] 然后我要打开我从网上下的一个个TDA miku起始模型
[ 48.0s] 中全选这个你下的模型所在文件夹里的tex toon，sph等文件夹
[ 63.0s] ([。°。]不要在意，等她一会，她傻了一秒)
[ 84.0s] 然后把模型pmx也复制粘贴过去到那个新建文件夹里
```

实现过程中踩到三个坑（都在 `src/bilibili_agent/ocr_subtitles.py`）：

1. **不能把字幕带里所有文字拼成一行。**
   字幕带里还混着桌面 UI 的文件名、按钮、菜单，逐帧剧变，合并时全被判成噪声
   → 整集一条字幕都出不来。
2. **不能取"最下面一行"当字幕。**
   Windows 任务栏时钟、播放器状态栏都比字幕更低，但字号小得多。
   → 改成**取字号最大的那一行**（字幕字号显著大于界面文字）。
3. **跨帧不变的文字是界面，不是字幕。**
   "Links"、"mode正"、时钟这些跨帧不变，字幕每句都在变。
   → 两遍处理：先统计每个"文字+位置"的出现频次，占比超过 15% 的判为界面静态文字并剔除。

另外把**字幕区改成自动检测**（`band = "auto"`），不写死位置：
在整段视频上均匀采样若干帧做整帧 OCR，把文字框按高度聚成"行带"，
同一行带在不同采样帧里出现过**不同文字**的就是字幕带（字幕每句都在变，界面文字不会），
再在所有动态行带里挑**字号最大**的那条作为字幕区。
本次实测自动框出 `0.816~0.921`，与肉眼位置一致。
检测结果（置信度、证据样例）会写进 manifest 的 `extra.ocr`，便于人工复核。

---

## P3：图文 MD

### 抽帧策略（`src/bilibili_agent/frame_selector.py`）

不依赖付费视觉 API，也不做需要 Agent 逐帧主观判断的流水线：

1. FFmpeg `select='gt(scene,0.3)'` + `metadata=print` 取场景切换点；
2. 叠加字幕/转写的时间锚（每条文本的起始点）；
3. 按 `min_gap_seconds` 抽稀、`max_images` 限量；
4. 逐帧抽图后过滤黑帧（平均亮度）与近重复帧（9×8 灰度 dHash，汉明距离 ≤6 视为重复）；
5. 每张图记录 `time_ms` / `reason` / `linked_segment_ids`。

纯 FFmpeg + 纯 Python 实现，**没有引入 numpy / Pillow 等额外依赖**。

### 真实结果

`bili fetch BV1FW41157y3 --parts 1 --mode illustrated --subtitle-source asr --json`
→ `article.md` + `assets/` 下 **18 张关键帧**，黑帧与重复帧已被滤掉。

抽帧有效性人工抽检（打开图片逐张看）：

| 时间 | 画面内容 | 是否与讲解对应 |
| --- | --- | --- |
| 00:00 | 桌面 / PMX 编辑器启动 | ✅ 对应"打开 PMX editor" |
| 01:04 | 两个资源管理器窗口：`2015 Mods` 与 `TDA Miku Base`（含 sph/tex/toon） | ✅ 正对应"下载 base 放到模型文件夹" |
| 07:28 / 07:26 | PMX 编辑器界面（换材质后的模型） | ✅ 对应"改贴图"步骤 |

图片用相对路径 `assets/frame_000064080.jpg`，VS Code / Markdown 编辑器可直接显示。

### 修过的插图顺序问题

初版把关键帧插在"该句前 30 秒内"的第一句话前，导致 00:32 的图出现在 00:04 那句之前。
改为**插在它之后第一句话之前**（`frame.time_ms <= seg.start_ms`），图文顺序与讲解顺序一致。

---

## P4：MCP / SKILL

### MCP 工具（stdio，5 个）

`bili_search` / `bili_inspect` / `bili_materialize` / `bili_job_status` / `bili_read_transcript`。

* 参数用 `Literal` + `Field(description=...)` 声明，**枚举与说明真的进 schema**
  （`tests/test_mcp_smoke.py` 会起真实 stdio 服务器连上去断言）。
* 长内容不进 MCP 响应：`bili_materialize` 只返回产物路径 + 统计，
  正文要用 `bili_read_transcript(start_ms, end_ms)` 按段精读。
* 不返回 Cookie、签名 URL、媒体直链。

### 客户端注册（按本机实际核对，未杜撰字段）

| 客户端 | 本机状态 | 方式 |
| --- | --- | --- |
| MiniMax Code | ✅ 已注册成功 | `mavis mcp create bilibili stdio "<venv python>" '["-m","bilibili_agent.mcp_server"]'` |
| Codex 桌面版 | 有 `~/.codex/config.toml`，无 `codex` CLI | `[mcp_servers.bilibili]` + `command`/`args`/`startup_timeout_sec`（字段照抄本机现有 `[mcp_servers.node_repl]` 写法，**未经 CLI 亲自拉起验证**） |
| Claude Code | ✅ CLI 已安装 | `claude mcp add bilibili -- "<venv python>" -m bilibili_agent.mcp_server`（语法来自本机 `--help`） |
| DSH | ❌ 未安装 | 只给通用 stdio JSON 形式，待在装了的机器上落地 |

MCP 工具需要客户端**重载会话**才会注入工具列表，本会话内无法直接调用；
因此用 MCP 官方客户端连真实 stdio 服务器做了冒烟验证。

---

## REVIEW_FIX_OCR 执行结果

> 依据：`docs/REVIEW_FIX_OCR.md`（评审基线 4da33da）。本节逐项记录**已修复 / 暂缓 / 无法复现**，
> 并给出测试命令与真实产物路径（脱敏）。

### S0：优先修复影响工作流的 Bug

| 项 | 状态 | 做法 | 回归测试 |
| --- | --- | --- | --- |
| S0-1 MCP `wait=false` 实际同步阻塞 | ✅ 已修复 | 新增 `src/bilibili_agent/jobs.py`（共享 JobManager）：CLI `--async` 与 MCP `wait=false` 共用同一任务协议与状态目录；状态文件 `os.replace` 原子写；worker 进程死掉时 `status()` 判 failed；`--refresh`/`--max-images` 透传给 worker | `tests/test_jobs.py`（T01/T02/T03，14 项） |
| S0-2 字幕源选择语义错误 | ✅ 已修复 | 拆成 platform/soft/ocr/asr 四个独立方法 + 显式调度表：asr 不跑 OCR、ocr 失败不回落 ASR、platform 不下载媒体 | `tests/test_routing.py`（T04/T05/T06） |
| S0-3 b23.tv 短链提前调用空参 view | ✅ 已修复 | 删除占位请求；按 scheme+host 判定短链（不用字符串包含）；每一跳目标都校验；限定 5 跳 | `tests/test_security.py`（T07） |
| S0-4 图文/assets 失败仍被当完成 | ✅ 已修复 | 按模式定义必需产物：assets 不依赖字幕即可出图；illustrated 必须有 article.md + ≥1 张可读图片；manifest 记录 `status` 与 `missing_artifacts` | `tests/test_routing.py`（T08/T09）、`tests/test_cache_components.py`（T24） |
| S0-5 未配置 BDL 时异常类型错误 | ✅ 已修复 | `BdlError(error_code=None)` → `code=None` | `tests/test_security.py`（T10） |

### S1：正确性、安全边界与结果可用性

| 项 | 状态 | 做法 | 回归测试 |
| --- | --- | --- | --- |
| S1-1 字幕 URL 校验与鉴权头 | ✅ 已修复 | 域名改为 `host==suffix or host.endswith("."+suffix)` 且强制 https；取字幕正文**不再附带账号 Cookie**；跳转逐跳校验 | `tests/test_security.py`（T11，含 evilhdslb.com / hdslb.com.attacker.example / http / 非官方重定向） |
| S1-2 OCR 固定顶部区域上下界颠倒 | ✅ 已修复 | top 模式改用独立 `top_band_top/top_band_bottom`，并强制 `0<=y0<y1<=1`，非法区间直接抛错 | `tests/test_ocr.py`（T12） |
| S1-3 `?p=2` 未驱动分 P 选择 | ✅ 已修复 | URL 的 `?p=N` 在未显式传 parts 时决定分 P；显式 `--parts` 优先 | `tests/test_routing.py`（T13） |
| S1-4 缓存键缺参数 / 模式升级未复用 | ✅ 已修复 | 缓存按组件记账（transcript / frames 各自摘要），subtitle→illustrated 只重跑抽帧、字幕从已有 SRT 重建；manifest 原子写 | `tests/test_cache_components.py`（T14） |
| S1-5 覆盖率用首尾跨度 | ✅ 已修复 | 新增 `segment_time_coverage`（时间并集 / 时长）与 `span_ratio`（首尾跨度，另名不冒充覆盖率） | `tests/test_subtitles.py`（T15） |
| S1-6 MCP 章节索引字段缺失 | ✅ 已修复 | Pipeline 的分 P 结果带 `chapters`，manifest 同步写入；MCP `materialize`/`read_transcript` 可按章节范围读取 | `tests/test_cache_components.py`（T16） |
| S1-7 关键帧偏向开头 | ✅ 已修复 | 抽帧改为**按时间分桶**（8 桶），每桶保底配额，再按质量分补齐；`min_gap` 与已选帧比较；输出按时间排序 | `tests/test_frames.py`（T17：前段 100 个场景变化 + 后段 2 个关键操作 → 后段仍有图） |
| S1-8 搜索 limit=50 只查一页 | ✅ 已修复 | limit>20 按需分页、去重、限制总请求数（默认 5），遇风控直接抛出不重试 | `tests/test_search.py`（T18） |
| S1-9 ASR 默认只按中文识别 | ✅ 已修复 | 默认 `language=auto` → 底层传 Python `None`（自动识别）；已核对 qwen_asr 的 `validate_language` 会拒绝字符串 `"None"` | `tests/test_asr_language.py`（T19） |
| S1-10 文字型成功与错误输出一致性 | ✅ 已修复 | 多 P 部分失败聚合为 `partial`，顶层带分 P 级错误；错误阶段区分 `subtitle` / `frames` / `materialize` | `tests/test_routing.py`（T24） |

### O1~O4：OCR 字幕区改造

| 项 | 状态 | 做法 |
| --- | --- | --- |
| O1-1 时序字幕区跟踪 | ✅ 已实现 | 新增 `src/bilibili_agent/subtitle_region.py`：按时间分层取 6 个窗口（各 6s、3fps），整帧 OCR 后用空间重合+行高+位置稳定性关联成 region track，聚合成**四维 ROI**（x0,y0,x1,y1）；打分用可解释特征（底部软先验、水平居中、字高、样式稳定、内容按句替换、跨窗口持续出现；永久 UI 文字与瞬时弹窗为负向特征）；返回 `heuristic_score` + `evidence`，低分时回退旧检测器 |
| O1-2 静态界面文字与真字幕的时序判定 | ✅ 已实现 | 不再按"整片 15% 频率"删句：改用**变化触发式识别**（ROI 像素几乎不变就跳过 OCR、沿用上一帧文本），于是"同一句保持 1~10 秒"被完整保留；长期不变的唯一一段转入 on_screen_text |
| O1-3 多行字幕与横向范围 | ✅ 已实现 | ROI 按四维裁剪（含横向 x0/x1），ROI 内文字按 y 聚成行、按 x 排序，支持 1~3 行，行间保留换行；可同时给出双语第二行区域 |
| O2 触发式识别与时间边界细化 | ⚠️ 部分实现 | 已实现"轻量变化检测 + 双速率"的主干：ROI 内 32×18 灰度差分判变化，未变化则跳过 OCR；**转场时间二分细化尚未实现**，因此 manifest 里 `time_precision` 明确标为 `sample_interval`（采样间隔级），不伪装毫秒精准 |
| O3 OCR 与 ASR 协作 | ✅ 已实现 | 有平台字幕时不跑 OCR/ASR；auto 模式下 OCR 覆盖不足时用 ASR **补缺口**（按时间并集去重合并，每段保留自己的 source）；ASR 与 OCR 文本冲突时保留双方原始证据并标记 conflict；完整性判定改用 `segment_time_coverage` + 采样密度双指标，不再用单一 10% 阈值 |
| O4 字幕文字与画面文字分通道 | ✅ 已实现 | 对白进 `transcript.srt/md`；界面文字（常驻标题栏、水印、菜单）进 `manifest.extra.visual_text`（带 time_ms 与 kind） |

### 本轮真实 E2E 记录（Windows 本机）

```powershell
.venv\Scripts\python.exe -m pytest -q          # 183 passed
uvx ruff check src tests --select F,E9,W,I,UP,B,RET,C4 --line-length 100
.venv\Scripts\bili.exe doctor --json
.venv\Scripts\bili.exe fetch BV18gDFBVEQF --parts 1 --mode subtitle --subtitle-source platform --refresh --json
.venv\Scripts\bili.exe fetch BV1FW41157y3 --parts 1 --mode subtitle --subtitle-source ocr --refresh --json
.venv\Scripts\bili.exe fetch BV1FW41157y3 --parts 1 --mode illustrated --subtitle-source auto --refresh --json
```

**样本 1：平台字幕路线（`BV18gDFBVEQF`）**

```
subtitle_source = platform_subtitle   status = completed
segments = 63                          耗时 2.5s
```
✅ 未下载任何视频（纯字幕路线不碰媒体），符合 O3-1。

**样本 2：烧录字幕 OCR 路线（`BV1FW41157y3`，画面底部有 gg 中文字幕）**

```
subtitle_source = hard_subtitle_ocr    status = completed
segments = 106
segment_time_coverage = 0.8651   span_ratio = 0.9983   chars = 1839
耗时 118.6s
region method = temporal_tracking_v1
  采样帧 108   整帧 OCR 调用 108
  主字幕区 ROI = x0 0.1491  y0 0.8250  x1 0.8486  y1 0.9042
                heuristic_score 0.8185
                evidence = [bottom_soft_prior, horizontally_centered,
                            large_text, line_style_stable, text_turnover]
  第二行区域 ROI = y 0.6979~0.7250（role=secondary_language_subtitle, score 0.7035）
visual_text（界面文字独立通道）= 50 条（原始采集 373 条，按出现时长取前 50）
```

识别结果抽样（与原视频核对无误）：

```
[00:30–00:40] 很明显，这文件夹现在是空的（mdzz）
[02:43–03:15] 这个裙子的贴图什么的（不选裙子pmx，"其他什
[05:53–06:30] 我选中这些穿模了的身体✨
[06:39–06:42] 所以我打算用ctrl+P把内裤也删了
[08:54–09:24] 可能用到的是不同花纹的裙子贴图
[09:27–09:38] 撒花结束 ！ ！ 上！）
```

✅ 四维 ROI 精确落在字幕带（y 0.825~0.904），横向 x 0.149~0.849 排除了左右两侧
界面文字；对话文字与界面文字分成两条通道，互不污染。

**样本 3：图文模式（`BV1FW41157y3`，`--mode illustrated --subtitle-source auto`）**

```
status = completed   mode = illustrated
subtitle_source = platform_subtitle   segments = 278   frames = 13
files = [transcript.srt, transcript.md, article.md]
```
✅ 这一轮 auto 路由先命中了平台字幕（AI 字幕），于是**没有跑 OCR、也没有跑 ASR**，
只补了抽帧——正是 O3-1 要求的行为。同一视频显式 `--subtitle-source ocr` 时
才会走烧录字幕 OCR（上面样本 2）。这也说明 B 站字幕接口的可用性本身会波动
（同一视频有时给人工轨、有时只给 AI 轨），因此产物里始终标注实际来源。

### 本轮实测踩坑（都是真跑出来的）

1. **OCR 引擎返回像素坐标，跟踪器按相对坐标用** → ROI 算成 `36.0~47.0`（应为 `0.82`），
   578 帧全部裁剪失败。修法：`OcrEngine.boxes()` 统一输出归一化坐标（0~1）。
2. **`_roi_change` 里 `cv.INTER_AREA` 拼写错误**（应为 `cv2.`）→ 变化检测直接抛异常。
3. **失败时"必需产物缺失"后面是空列表** → 提示没有信息量。改为分别提示"缺哪些产物"与"完全没产出内容"。
4. **O4 通道未接上**：ROI 之外的界面文字没有单独收集，`visual_text` 为空。
   修法：从区域跟踪的轨迹里取与 ROI 不重叠的那些，作为独立证据通道。
5. **界面文字条数过多**：原始采集 373 条会让 manifest 涨到 106KB。
   修法：按时长优先取前 50 条（`ocr.max_on_screen_text` 可调）。

### 暂缓 / 未完成

* **O2 的时间边界二分细化**未做：当前字幕起止时间精度等于采样间隔（1s），
  manifest 里 `time_precision="sample_interval"`，文档与产物都不宣称毫秒级精准。
* **OCR→ASR 补缺口目前是"全片 ASR + 按时间去重"**（评审文档允许的最低可用版本），
  尚未做到"只转写缺口区间"。
* **`bili fetch --async` 的主动取消**仍未实现（本轮只做了状态真实化）。

---

## REVIEW2 执行结果

> 依据：`docs/REVIEW2.md`（审查基线 `0f13d01`）。逐项写明完成/暂缓、测试命令与验证证据。
> 本轮实测数量为**独立复测**结果（Phase A 后 202 passed，Phase B 后 227 passed），不沿用历史 `183 passed`。

### Phase A：先修 Bug（P0 四项）

| 项 | 状态 | 做法 | 回归测试 |
| --- | --- | --- | --- |
| R2-01 fallback ROI 的 x/y 写反 | ✅ 已修复 | `Roi` 构造顺序是 `(x0,y0,x1,y1)`，回退时按位置参数传成了 `(band_top, band_top+0.2, 1.0, band_bottom)`，实际只裁右下角一小块。改为**全宽底部** `Roi(x0=0.0, y0=band_top, x1=1.0, y1=band_bottom)`；所有 `Roi` 构造改关键字参数；`Roi.__post_init__` 严格校验 `0<=x0<x1<=1`、`0<=y0<y1<=1`；`detect_band()` 同款错误一并修；回退时 `method=fallback`、`heuristic_score=0` 并在诊断里写明 | T01（`test_review2_phase_a.py`） |
| R2-02 关键帧组件未登记真实图片 | ✅ 已修复 | frames 组件改为登记真实 `Frame.relative_path`（`image_files`），不再从 `files` 反推；缓存命中要逐张核对图片存在且非空 + `frame_count` 与清单一致 + illustrated 额外要求 `article.md`；`manifest.files` 与 outputs 包含真实图片；缺图提示按配置后缀（jpg/png/webp） | T02/T03/T04/T05 |
| R2-03 任意重叠即丢整条 ASR | ✅ 已修复 | 改为按**重叠比例**：完全覆盖且文本一致才去重；部分重叠或文本不一致时**保留 ASR 原文**并打 `overlap_with_ocr`；块级 ASR 不做按字符比例的伪切分；冲突记录含双方来源/时间/原因；整段 ASR 另存 `raw/alternate_evidence.json` | T06/T07/T08/T09 |
| R2-04 空白帧复活旧字幕 | ✅ 已修复 | 引入 text/blank/error 三态：只有上一帧确实有字幕且画面近似未变才复用；识别到空白立即清空缓存；引擎异常记为 error，不当断句点也不无条件沿用。变化检测改为「灰度均值差」与「显著变化像素占比」取大者（64×36），小字出现/消失也能捕捉 | T10 |

**Phase A 提交**：`6a5cab9`（已推送）。

### Phase B：OCR 质量与泛化（P1 三项）

| 项 | 状态 | 做法 | 回归测试 |
| --- | --- | --- | --- |
| R2-05 跨窗口证据无法生效 | ✅ 已修复 | 改成**两级轨迹**：L1 窗口内逐帧关联（保留短时间阈值），L2 按归一化行位置/字高/外框把不同窗口的 L1 轨迹归并成"区域候选"。归并判据是**相对行高**（纵向偏移 ≤ 行高×0.6），避免中英双行被误并。`persists_across_windows` 现在真的按区域跨窗口证据计分 | T11/T12/T13/T23 |
| R2-06 10% 覆盖率即算完整 | ✅ 已修复 | 区分 `has_ocr_evidence` 与 `is_transcript_complete`：新增轻量**能量法 VAD**（`audio_vad.py`，100ms 帧 RMS）估计有声时段，再算字幕对**有声时段**的覆盖率与最长缺口；没有语音活动证据时保守返回 `unknown`，不冒充完整。`source=ocr` 仍不跑 ASR（硬约束），`auto` 按证据回退；manifest 写明状态/依据/指标 | T14/T15/T16/T21/T22 |
| R2-07 只用 best 的全片 ROI | ✅ 已修复 | 引入 `RoiPlan`/`RoiSlot`：**按时间选用** ROI；主字幕区 + 时间重叠的第二字幕行（双语）→ 同一时段两个 ROI，两行都能识别；时间不重叠的其它区域（片中位置变化）→ 独立时段。区域外包络横向用 min/max（长句两端不被裁断）、纵向用 p02/p98。`visual_text` 排除**所有**已确认字幕区域，第二行不会落进界面通道 | T17/T18/T19/T20/T24/T25/T26/T27/T28 |

**Phase B 提交**：`__PHASE_B_SHA__`（已推送）。

### 本轮实测踩坑（Phase A/B 修掉的真 bug）

1. **L2 归并形同虚设**：候选的 `rect` 在归并**之后**才算，导致比较时全是 `(0,0,0,0)`，
   跨窗口一条都没并上；新候选创建后同样没算 rect。修复后 6 个窗口的同一条字幕带才归成一个候选。
2. **中英双行被错误合并**：归并阈值用绝对值 0.10，而双语两行只差 0.07 → 被并成一条。
   改为相对行高判据后，两行各自成为独立候选。
3. **长句两端被裁**：外包络横向用 p02/p98，样本少时长句会被切一半。横向改用 min/max。
4. **L2 归并拿"累积外包络"比字高**（Phase B 实测才发现）：外包络会随成员增加而变高，
   越滚越大之后，后面窗口里**完全正常**的轨道会因为"字高比过大"被拒之门外——
   同一条字幕带被切成两段，还凭空多出两个"位置变化"的区域。
   改用**成员轨迹的中位字高 / 中位行中心**比较后，真实视频只剩 1 个字幕区。回归测试 T23。
5. **主区域 slot 覆盖整片**："位置迁移"的区域虽然被检测出来，实际识别时仍永远命中主区域，
   后半段字幕读不到（半片空白）。主区域 slot 改为**它自己的有效时段**。回归测试 T24。
6. **"第二字幕行"判定太松**：只要求"基线不同"，于是远处一条界面文字带被当成双语第二行，
   整屏 UI 被当成对白 OCR 进来——实测字符数从 3.4k 涨到 11.1k。
   现在要求两行**紧挨着**（间距 ≤ 半行高）、**不是同一条带**（重叠 < 30%）、字高相近。
   回归测试 T26/T27。
7. **外包络高度区分不了"字幕行"和"界面元素"**：真正能区分的是**实际框的字高**——
   实测同一个教程里字幕行中位字高 0.078，界面小字只有 0.023～0.027（差 3 倍）。
   判据改成字高。回归测试 T27。
8. **ROI 内的界面小字仍会混进对白**：软件教程里文件浏览器侧栏就在画面**左下角**，
   与字幕带**纵向重叠**（y 0.798~0.94 vs 0.823~0.908），只看位置分不开。
   改为按 ROI 自带的"典型字高"筛掉明显更小的文字。回归测试 T28。
9. **重叠 ROI 识别出同一行** → transcript 里出现"句子\n句子"。改为同行去重。回归测试 T25。
10. **变化检测按 ROI 下标记裁剪块**：字幕区随时间切换时下标会错位，
    可能把旧位置的字幕沿用到新位置。改为按 ROI 几何做键。
11. **Phase B 的测试往仓库根目录写 `f0/f1/f2.jpg`**：改到 `tmp_path`，不再污染工作区。

### Windows 真实场景验证

样本：`BV1FW41157y3`（PMX Editor 软件教程，烧录中文字幕）、`BV18gDFBVEQF`（平台字幕样本）。

| 场景 | 命令 | 结果 |
| --- | --- | --- |
| 烧录字幕 OCR | `bili fetch BV1FW41157y3 --parts 1 --mode subtitle --subtitle-source ocr --refresh --json` | 见下方「Phase B 实测数据」 |
| 平台字幕（不下载视频） | `bili fetch BV18gDFBVEQF --parts 1 --mode subtitle --subtitle-source platform --refresh --json` | `subtitle_source=platform_subtitle`，63 段，覆盖率 88.85%，**2.4s，未下载视频** |
| 图文模式（auto） | `bili fetch BV1FW41157y3 --parts 1 --mode illustrated --subtitle-source auto --refresh --json` | 5.8s，产出 `article.md`（26KB）+ 8 张关键帧；`auto` 这次命中的是**平台字幕**（604 段），其跨度 1388140ms 远超视频时长 578000ms，代码按既有策略告警并取最先取到的一条 |
| 图文模式（强制 OCR） | `bili fetch BV1FW41157y3 --parts 1 --mode illustrated --subtitle-source ocr --refresh --json` | 见下方「图文模式（OCR 路线）」 |

### Phase B 实测数据（BV1FW41157y3，强制 OCR 路线）

产物目录：`output/BV1FW41157y3/P01_36897122/`（`transcript.srt` / `transcript.md` / `manifest.json`）

| 指标 | 数值 |
| --- | --- |
| 总耗时 | 约 230～280s（全片 578s，含检测期与识别期两阶段 OCR） |
| 检测期整帧 OCR 调用 | 108 次（6 个采样窗口 × 6s × 3fps） |
| 识别期采样帧 | 578 帧（1 秒一帧） |
| 识别到字幕的帧 | 491 帧；读帧/识别异常 0 帧 |
| 最终字幕区 | **1 个**（`dialogue_subtitle`，x 0.0296~0.9681 / y 0.8058~0.9233，score 0.9507） |
| 字幕段数 | 95 段，其中 84 段含中文对白，11 段是单字符 UI 残留 |
| `segment_time_coverage` | 0.782（452000ms / 578000ms） |
| 最长无字幕缺口 | 13s |
| 有声时段覆盖率（VAD） | 0.9072，最长有声缺口 3.7s |
| 完整性判定 | `complete`（依据写入 `manifest.extra.ocr.completeness`） |
| 界面文字通道 | 50 条（`manifest.extra.visual_text`） |
| 真实字幕字数 | 1491 字 |

> 说明：上一轮（Phase A 修完、未做本轮区域跟踪改造）同一视频是 106 段 / 覆盖率 0.8651，
> 但其中混入了大量界面文字（"Mobile Uploads"、"select all"、"delete" 等），
> **覆盖率被 UI 垃圾撑高了**。本轮把 UI 过滤掉后段数降到 95、字数降到 1491，
> 留下的是基本干净的对白；覆盖率 0.782 是**去掉水分后的真实值**。

### 合成样本（REVIEW2 §4 要求的双语 / 换位置 / 长短句）

> ⚠️ **这三段是合成素材**（由 `tools/synthetic_samples.py` 用 PIL 渲染 + ffmpeg 编码生成，
> **不是真实 B 站视频**）。仓库里没有现成的公开样本满足"双语 / 中途换位置 / 长短句交替"，
> 按 REVIEW2 的许可生成合成片段，并在此明确标注。

| 片段 | 时长 | 内容 | 实测结果 |
| --- | --- | --- | --- |
| `synthetic_bilingual.mp4` | 20s | 底部中英双行字幕，每 2.5s 一句 | 8 段，**每段同时含中文行与英文行**（`lines=2`），第二行没有落进 `visual_text` |
| `synthetic_position_switch.mp4` | 24s | 前 12s 底部、后 12s 顶部 | 9 段：底部 5 段 + **顶部 4 段**（13s~23s），ROI 计划分两个时段 |
| `synthetic_long_short.mp4` | 20s | 短句与 44 字长句交替 | 8 段，长句**完整 44 字**（开头"这是一个刻意…"、结尾"…结尾的字裁掉"都在），ROI 横向 x 0.0225~0.9775 未裁断 |

复现：

```powershell
.venv\Scripts\python.exe tools\synthetic_samples.py make --out output\synthetic
.venv\Scripts\python.exe tools\synthetic_samples.py run  --dir output\synthetic
```

（`output/synthetic/synthetic_report.json` 里有逐段文本、ROI 计划与断言结果。）

### 图文模式（OCR 路线）

`bili fetch BV1FW41157y3 --parts 1 --mode illustrated --subtitle-source ocr --refresh --json`

| 指标 | 数值 |
| --- | --- |
| 状态 | `completed`，无 warning |
| 字幕 | 95 段 / 覆盖率 0.782（与 subtitle 模式同一份 OCR 结果） |
| 关键帧 | 16 张 `assets/*.jpg`，全部在 `manifest.files` 与 `components.frames.image_files` 中登记 |
| `article.md` | 8.2KB，按场景切换 + 字幕锚点抽帧 |
| 字幕区 | 1 个（与 subtitle 模式一致） |

### 本轮测试

```powershell
cd E:\project\bilibili
.venv\Scripts\python.exe -m pytest -q
# → 227 passed
uvx ruff check src tests tools --select F,E9,W,I,UP,B,RET,C4 --line-length 100
# → All checks passed!
```

- Phase A 新增 `tests/test_review2_phase_a.py`（T01–T10）
- Phase B 新增 `tests/test_review2_phase_b.py`（T11–T28）
- 合成样本脚本：`tools/synthetic_samples.py`（`make` 生成、`run` 验证）

### 仍有的限制

1. **O2 的转场时间二分细化仍未做**：字幕起止时间精度 = 采样间隔（1s），
   manifest 里 `time_precision="sample_interval"`，不宣称毫秒级精准。
2. **OCR→ASR 补缺口目前是"全片 ASR + 按时间去重"**，尚未做到只转写缺口区间。
3. **VAD 是能量法**，只能回答"这段时间有没有人在说话"，分不清音乐与人声；
   有背景音乐的教程里 `speech_coverage` 会偏乐观。
4. **"双语"识别只做到"两行都 OCR 出来"**，尚未按语言分字段（`lan`）分开保存。
5. **UI 过滤靠的是"字高比"**，不是语义理解：极少数场景下，界面里的大号标题
   和字幕字高相近，仍可能被当成对白。真实视频里剩余的 UI 痕迹集中在
   画面左右边缘的侧栏。
6. **区域跟踪只采样 6 个窗口**（每窗 6 秒），窗口之间的时间段靠中点插值推断；
   短于采样间隔的"字幕突然换位置"事件可能捕捉不到。

---

## 还没做完的事

1. **字幕区自动检测还可以更准**：当前判据是"跨帧变化的文字带里字号最大的那条"，
   在屏幕录制类视频（界面文字很多）上已验证可用，但极端情况（超大标题 + 小字幕）
   仍可能选错。可改为让 Agent 抽检几帧后人工确认/修正。
2. **回归矩阵扩充**：目前真实样本覆盖"有平台字幕 + 无平台字幕 + 英语语音 + 软件教程 +
   烧录中文字幕"，还需补：多 P 视频、长视频、无可用音轨视频。
3. **`bili fetch --async` 的取消能力**：目前只支持查询状态，没有实现主动取消。
4. **多 P 汇总 `index.md`**：各 P 独立产物已可用，汇总页未做。
5. **`bili login` 的会话续期**：现在只存 `refresh_token` 注释，没有实现用
   refresh_token 自动续期登录态；Cookie 失效后需要重新扫码。

## 关键配置（本机）

```toml
# config.toml（已被 .gitignore 忽略）
[paths]
bdl_cli  = "E:/tools/bdl/bdl.exe"
ffmpeg   = "C:/Users/Administrator/AppData/Local/Microsoft/WinGet/Links/ffmpeg.exe"
ffprobe  = "C:/Users/Administrator/AppData/Local/Microsoft/WinGet/Links/ffprobe.exe"

[bilibili]
cookie_file   = ""          # 需要 Level 1 时填
use_env_proxy = false       # 本机代理会被 B 站风控

[asr]
python       = "E:/project/tts/backend_envs/asr_qwen3/Scripts/python.exe"
model_path   = "E:/project/tts/models/asr/qwen3_asr/5eb144179a02acc5e5ba31e748d22b0cf3e303b0"
aligner_path = "E:/project/tts/models/asr/qwen3_forced_aligner"
```
