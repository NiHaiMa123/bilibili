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
