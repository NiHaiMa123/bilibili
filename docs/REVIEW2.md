# REVIEW2 — Bilibili Agent 第二轮代码审查：OCR 可靠性与产物缓存修复

> 执行对象：MiniMax Code  
> 仓库：NiHaiMa123/bilibili  
> 审查基线：`0f13d01337fda035c21b197a7d2f2e30b490adae`（2026-10-23，`main`）  
> 前置文档：`docs/REVIEW_FIX_OCR.md`、`docs/PROGRESS.md`  
> 当前状态：**待执行**。本文是对现有源码的静态审查；仓库所称 `183 passed` 及 Windows E2E 为提交者记录，不等于本次独立复测。

## 0. 总目标与执行约束

上轮 `S0/S1` 基础修复和 `O1~O4` OCR 改造已经落地，**本轮只做针对性修复和必要的小幅完善**，不要将现有识别系统整体推倒重写。

目标：
1. 修复 4 个可以直接影响产物正确性的高优先级问题：**回退 ROI 坐标、图片缓存登记、OCR/ASR 时间段合并丢词、OCR 空白后复用旧字幕**。
2. 修复跨采样窗口区域关联、字幕完整性判断、双语/中途位移 ROI 的实用性问题。
3. 每项新增**修复前能失败、修复后能通过**的离线回归测试，并在 Windows 本机用既有真实样本验证。
4. 完成后提交代码和测试，更新 `docs/PROGRESS.md`：逐项写清完成/暂缓、测试命令、统计结果、仍有的限制；报告最终 Commit SHA。

**特别注意**：
- 保留 RapidOCR 3.x（PP-OCRv6）、`OcrEngine`、FFmpeg、BDL CLI、Qwen3-ASR、当前 CLI/MCP API；没有必要更换 OCR 基础引擎。
- 不提交 Cookie、原视频、模型权重或绝对路径的私人配置。
- 以目标分支的**最新代码**为准：若其中某处已经修复，先验证，不要机械覆盖更新。
- 不得把启发式 `heuristic_score` 当作校准后的概率，不得把 1 秒采样时间戳宣称为毫秒级精准。
- 本轮发现的问题与建议以功能正确性为主，不必因非关键架构改进阻塞交付。

## 1. 第一阶段：P0 必须修复的 4 项明确问题

### R2-01｜OCR 自动检测失败时，fallback ROI 的 x/y 位置写错

**位置**：`src/bilibili_agent/ocr_subtitles.py`，`detect_roi()` 的 `best is None` 分支（审查时约 509–517 行）；`detect_band()` 中也有相似代码（约 474–476 行）。

**根因**：`Roi` 构造参数顺序是 `(x0, y0, x1, y1)`，但是回退时传入：

```python
Roi(
    self.config.band_top, self.config.band_top + 0.2,
    1.0, self.config.band_bottom, method="fallback",
)
```

默认配置 `band_top=0.74`、`band_bottom=0.99` 下，该 ROI 为 `x0=0.74, y0=0.94, x1=1.0, y1=0.99`，只识别右下角一小片区域，而非整条底部字幕区。

**修复**：
- 默认全宽底部 fallback 应为 `Roi(0.0, band_top, 1.0, band_bottom, method="fallback")`，并验证 `0 <= x0 < x1 <= 1`、`0 <= y0 < y1 <= 1`。
- 所有构造 `Roi` 的地方统一使用**关键字参数**，降低位置参数写反风险；可以给 `Roi` 新增严格验证。
- `detect_band()` 旧兜底代码即使当前不再调用，也不能保留同样错误。
- 若没有可信字幕区域，报告 `method=fallback`、`heuristic_score` 或 warning，不能将 fallback 当成高置信度真实检测。

**验收 T01**：模拟跟踪器没有 `best`，断言默认 ROI 是 `(0.0,0.74,1.0,0.99)` 而不是右下角；非默认自定义 `band_top/bottom` 同样正确；非法区间抛出明确错误。

### R2-02｜关键帧组件缓存未登记真正的图片文件

**位置**：`src/bilibili_agent/pipeline.py`，`_fetch_part()`（约 835–946 行），特别是：

```python
frame_files = [
    f for f in files
    if f == "article.md" or f.startswith("assets/")
]
```

**根因**：构造 `files` 时增加了 `article.md` 等文档，却没有把已创建的 `Frame.relative_path`（`assets/frame_....jpg`）添加到该清单。于是：
- `illustrated/both` 的帧组件可能只有 `article.md`，删掉图片仍可能被判定 `frames_ok`，复用缺图文档。
- `assets` 模式的 `frame_files` 可能为空，使其每次都被认定不能命中缓存。
- `outputs` 也可能只显示文档，不返回实际图片路径；对 Agent 不友好。
- 现有 `_fetch_part()` 还用硬编码字符串 `assets/*.jpg` 描述缺图，但配置支持 `webp/png`；错误消息不要与格式不一致。

**修复**：
1. 始终以 **`frames: list[Frame]` 的真实 `relative_path`** 建立帧组件清单和对应文件检查，而不是尝试从不含图片的 `files` 反推。
2. `frames` 组件应存储 `frame_count`、`image_files`、`article_file`（若存在）或等价字段，缓存命中条件同时核验图片全部存在且非空，最好校验 `frame_count` 与 `image_files` 一致。
3. `assets` 模式无需 `article.md`，但应能命中缓存并返回所有有效图片路径；`illustrated` 模式需同时校验 `article.md` 与图片。
4. `manifest.files` 和 CLI/MCP `outputs` 应语义清晰，若列出图片则保持与 `components.frames.files` 一致。不要因为兼容旧 manifest 就接受不完整帧缓存。
5. 如果抽帧失效、缺图或缓存不匹配，重建帧组件；原已有效字幕组件不应被无谓重转写。

**验收 T02–T05**：
- `illustrated` 首次成功，再删除其中一张图片，第二次不得报告完整缓存命中，必须重新抽帧/修复。
- `assets` 首次生成有图，第二次应实际命中缓存（不重复执行 `_build_frames`）；`outputs` 包含可打开的图片路径。
- `webp/png` 模式按实际后缀诊断缺图。
- `subtitle → illustrated` 增量升级仍复用字幕，只新增所需关键帧与 article。

### R2-03｜OCR 与 ASR 任意重叠就删除整条 ASR，造成补缺口丢内容

**位置**：`src/bilibili_agent/pipeline.py` 的 `_merge_ocr_asr()`（约 547–594 行）。

**现状**：当 ASR 段与 OCR 所覆盖的任意时间区间重叠，`if not overlaps: merged.append(asr)` 无法进入，整个 ASR 段被丢弃。只有冲突元信息被记入 `conflicts`。对没有 ForcedAligner 的块级 ASR 尤其严重。

**最小反例**：

```text
OCR:  [10000, 12000] "画面识别到的一句话"
ASR:  [    0, 30000] "第一步...第二步...第三步..."
```

现算法因两秒重叠，丢弃整条 30 秒 ASR，造成该 ASR 中其余内容遗失。

**修复要求**：
- 时间合并必须考虑**重叠比例/未覆盖剩余区间**，不能按“是否有任意重叠”判断整段舍弃。
- 对有强制对齐的词/句级 ASR：可以按可信时间戳拆分到 OCR 未覆盖区，做逐段时间去重。
- 对只有 30s、120s 大块时间戳的 ASR：**禁止按字符比例或平均语速伪造词级时间戳**。可采取“保留原始整块 ASR + 标记与 OCR 重叠/替代展示”策略；若希望生成单条无重复时间轴，先通过 ForcedAligner 获取可信时间定位再拆分。
- 结果里的 `Segment.source` 必须保留，必要时 `flags` 加 `overlap_with_ocr`；`manifest` 记录冲突及处理策略。`transcript.srt` 必须仍是合法且有序的字幕时间轴，不能把 OCR/ASR 同时间叠字当作“无重复”的完整输出。
- OCR 与 ASR 文本不一致可能是翻译/意译，默认保留原始证据，不要强行覆盖。
- 可考虑输出一个独立 `raw_asr.json` / `alternate_evidence.json` 保存粗粒度整段 ASR，避免 SRT 冲突和信息遗失，尽量复用现有 `raw/asr.json`。

**验收 T06–T09**：
- 上述 2 秒 OCR + 30 秒 ASR 反例，后 28 秒信息必须仍能通过产物被 Agent 检索到。
- ASR 完全被 OCR 覆盖且内容高度一致时，允许去重；真正没有重叠的段必须完整加入。
- 多个 OCR 段包围一条 ASR 块，不能把块级 ASR 原文静默删除。
- 包含文字冲突时，能在结构化证据中看到两种来源、时间范围和冲突原因。

### R2-04｜空白字幕帧之后可能错误复用 last_text

**位置**：`src/bilibili_agent/ocr_subtitles.py`，`observe()`（约 328–373 行）。

**根因**：`_ocr_roi()` 返回空文本时，代码没有清空 `last_text`。后一帧如果与空白帧像素变化较小（`change < threshold`），会重新沿用更早之前的旧字幕。

**修复**：
- 将状态区分为 `text` / `blank` / `error`，保留最近一次可信 OCR 结果和连续状态。
- 只有 `prev_state=text` 且当前画面确实近似未变，才能复用 `last_text`；识别到 `blank` 后不能跨越空白传播旧句。
- `error` 表示引擎故障或图像读取失败，不等于字幕消失，也不能无条件沿用旧句/无条件断句。应保留 `error` 供合并器根据邻近证据判断。
- 新字幕刚进入/字幕刚消失的变化边界不能被跳过；考虑对比字幕前景/边缘特征而非只依赖灰度均值。

**验收 T10**：合成序列 `字幕 A → 空白 → 空白 → 字幕 B`，逐帧模拟 `_roi_change`、`_ocr_roi`，断言不存在 `A → 空白 → A` 这种回生，合并输出只有正确的 A、B 两段；包含 OCR 异常帧时明确区分异常状态。

## 2. 第二阶段：P1 OCR 质量和泛化修复

### R2-05｜“跨窗口连续出现”打分项事实上无法生效

**位置**：`src/bilibili_agent/subtitle_region.py`，`TrackerConfig.link_gap_ms=1600`、`_link()`、`track()`、`_score()`（约 36–49、212–235、238–380 行）。

**问题**：六个时间分层采样窗口，相互间可能相隔数十秒乃至数分钟。但 `_link()` 遇到 `time_ms - last_time > 1600` 就新建轨迹，因此同一个字幕区域在远距离窗口无法关联；`track.windows` 很难覆盖三个窗口，`persists_across_windows` 加分无法反映全片布局。同时长期固定标题栏因轨迹切断，也可能无法按跨窗口时长判定静态 UI。

**建议（不要简单把 link_gap_ms 提高到几分钟）**：
- **两级轨迹**：L1 窗口内逐帧文字 track（保留短时间阈值）；L2 窗口之间按归一化行位置、字高、区域外框、排版模式、相似文字更新节奏，把局部 track 归并成“字幕区域候选”。
- L2 只表示**同一位置/样式区域**，不表示字幕文本在缺采样的几分钟内持续存在；不可据此虚构覆盖时长。
- `persists_across_windows` 依据区域在多个非相邻窗口中有可信字幕证据计分。
- UI 负向特征按跨窗口位置、**同文本**出现占比进行判断，不将每一窗口的独立短 track 错看成整段视频都在变化。
- 对开头是无字幕片头、中间才有字幕、末段换位置的视频，要允许分阶段 ROI。

**验收 T11–T13**：
- 十分钟视频：窗口 0/2/5 位置相同、文本不同 → 同一个区域候选得到跨窗口支持。
- 顶部静态标题跨窗口文本不变、下方对白反复更新 → 选下方对白。
- 视频后半字幕真实上移 → 输出具时间范围的两套 ROI，不把它们强行合成全片一个位置。

### R2-06｜OCR 完整性判定仍以 10% 覆盖作为充分条件

**位置**：`src/bilibili_agent/pipeline.py`，`_ocr_is_sufficient()`（约 596–627 行）。

**问题**：`if ratio >= cfg.ocr_min_coverage: return True`，默认阈值 `0.10`。十分钟视频只识别到约一分钟字幕，也可能被认定“足够完整”，`auto` 就不再调用 ASR。`frames_with_text/sampled_frames >= 0.4` 的判断也不足以证明语音段被完整覆盖。

**修复方向**：
1. 区分 `has_ocr_evidence`（检测到一些字幕）与 `is_transcript_complete`（足以作为完整讲解的可靠来源）；不要沿用一个阈值表达两种语义。
2. 如果成本可接受，使用轻量 VAD/音频能量区间估计`有语音时段`，计算 OCR 字幕与语音时段的覆盖和长缺口；如果无 VAD，应保守返回 `unknown/incomplete`，不要单凭 10% 宣布全片已完整。
3. 最长连续缺口、字幕段密度、分段窗口覆盖，以及明显视频类型（PPT/纯音乐/长静音）都是辅助证据。
4. `subtitle_source=ocr` 时继续遵循“只 OCR 不跑 ASR”的硬约束；`auto` 允许按策略触发 ASR 补缺；已有可靠平台字幕优先，不产生新开销。
5. 在 `manifest` 写明完整性状态（`complete / incomplete / unknown`）、依据和诊断指标，避免把阈值通过冒充识别准确率。

**验收 T14–T16**：
- 十分钟连续有人讲解，只有一分钟 OCR：不能判定为完整。
- 十分钟视频含大量静音、字幕覆盖了全部语音时段：在有语音活动证据时可判为完整，即使整片时长覆盖率较低。
- 明确 source=ocr，完整性不足也不能偷偷执行 ASR；auto 可根据证据回退。

### R2-07｜检测器找到了多区域，但实际 OCR 只使用一个全片 ROI

**位置**：`src/bilibili_agent/subtitle_region.py`（`regions` 带 `start_ms/end_ms/role`）、`src/bilibili_agent/ocr_subtitles.py`（`detect_roi()`/`observe()` 只处理 `best`）。

**问题**：
- 检测得到 `secondary_language_subtitle` 也只是放入元数据，实际识别流程只使用 `detection.best`，双语字幕的第二行可能从未 OCR。
- 视频中途从底部切换顶部/横幅，单一全片 ROI 不能跟随；检测器虽然输出时段，但实际 `observe` 没有使用时间路由。
- ROI 四边来自单条 OCR 文字轨迹矩形的中位数；遇到长短句交替，区域可能太窄，长字幕的左右端被裁掉。
- `_ocr_roi()` 当前可合并一个 ROI 内的多行，但这不等于已支持**两个彼此分离的 ROI**。

**修复**：
- 引入按时间选用的 `RegionTimeline` 或等价结构：`regions=[{time_start,time_end,rect,role,...}]`；每一帧选用有效主 ROI，必要时处理第二字幕行 ROI。
- 合并双语字幕可按配置保留双语、优先中文、或分语言字段；不要把不连续的多区域简单拼成一句。
- 用文字框横向范围的**稳健外包络**（例如合理分位数+边距），而非中位数矩形直接裁原字；边界限制在 0–1，避免引入左右 UI。
- 字幕转场/位置变化需要重新识别候选 ROI，不能仅依靠最初全片一次检测；已有 `fallback` 仍可使用。
- `visual_text` 需排除所有已确认的字幕区域（不只是 best），防止双语第二行被误归到界面通道。

**验收 T17–T20**：
- 两行相距较远的中英字幕都能按模式完整获取，另一行不误判为 UI。
- 前半底部、后半顶部字幕：产物分别含两阶段字幕，而不是半片空白。
- 同一位置短句和超长句交替：长句左右字符不被裁断。
- 同时存在软件左/右 UI 列表时，选择字幕 ROI 后不会将 UI 大量混入对白。

## 3. 额外应检查的联动风险

以下是本轮七项的联动检查，不要求额外做无边界的大重构：

1. **缓存失效条件**：`pipeline.py` 的 transcript digest 包含 OCR 带、采样率、分数、ASR 信息，但若新增/修改 region tracking 窗口数、帧率、阈值、ROI 算法参数，应反映到相应缓存版本/摘要。OCR 算法重大变更后不能直接复用旧版识别文本。
2. **真实 ROI 观测**：manifest 中应同时记录区域追踪的 `heuristic_score`、采样帧数、OCR 调用数、是否触发 fallback、多个区域时间边界，而不仅是最终那一个 `rect`。
3. **帧图片输出**：确保跨 `subtitle / illustrated / assets / both` 模式的输出及缓存命中行为一致；只要输出引用图片，就必须真实存在。
4. **文档与实现一致**：`ocr_subtitles.py` 顶部注释声称“文本转场时二分细化起止时间”，但 `PROGRESS.md` 显示未实现。请修改误导性注释；如本轮不做二分细化，明确 `time_precision=sample_interval`。
5. **OCR 性能证据**：`PROGRESS.md` 上次实测 106 段字幕、108 次检测阶段整帧 OCR、OCR 总耗时 118.6s（BV1FW41157y3）。新一轮至少记录同样样本的运行时长与两阶段 OCR 调用数，比较更改有无明显退化；没有人工标注集不能宣称准确率百分比。
6. **测试覆盖范围**：仓库此前 `183 passed`，但并不证明上面七类边界已覆盖；需要按下面新测试矩阵补足。

## 4. 执行顺序与强制回归矩阵

**Phase A：优先确保不丢字、不假成功**
- [ ] R2-01 修复 fallback ROI x/y。
- [ ] R2-02 修复关键帧文件列表、缓存与 outputs。
- [ ] R2-03 解决 OCR/ASR 任意交叉导致 ASR 整块消失。
- [ ] R2-04 修复空白帧后复活旧字幕。
- [ ] 在 Phase A 结束时执行现有 pytest/ruff + T01–T10，提交一个可独立使用的修复 commit。

**Phase B：提高字幕 ROI 泛化**
- [ ] R2-05 分层区域关联，使跨窗口证据真正有效。
- [ ] R2-06 改进完整性判断，避免 10% 直接认为完整。
- [ ] R2-07 多 ROI/中途位置变换、长句容纳与双语。
- [ ] 执行 T11–T20 + Windows 真实 E2E；提交第二个 commit。

| 测试 | 关键断言 |
| --- | --- |
| T01 | 无候选时 fallback ROI 是全宽底部 |
| T02 | illustrated 删除任意图片后，不能缓存命中 |
| T03 | assets 二次执行能命中图片缓存，outputs 包含图片 |
| T04 | jpg/png/webp 清单与必需产物检查一致 |
| T05 | subtitle→illustrated 仅重建图片，不重跑字幕 |
| T06 | OCR=10–12s，ASR=0–30s，非重叠内容不会被丢 |
| T07 | 纯重叠且同内容时可去重 |
| T08 | 块级 ASR 不做伪时间切分，保留原文证据 |
| T09 | OCR/ASR 冲突可追溯、来源字段正确 |
| T10 | OCR 序列 text→blank→blank→text 不复活旧文 |
| T11 | 远间隔的采样窗口仍能匹配同一字幕区域 |
| T12 | 跨窗口长期不变 UI 得到适当负向判定 |
| T13 | 片中布局切换输出不同时间段 ROI |
| T14 | 连续人声视频 OCR 仅 10% → 不判 complete |
| T15 | 长静音视频但已覆盖全部语音 → 有证据时可判 complete |
| T16 | source=ocr 不触发 ASR，source=auto 按策略补缺 |
| T17 | 双语两 ROI 实际获取，第二行不落入 UI |
| T18 | 字幕底部→顶部迁移时两段均可读取 |
| T19 | 长短字幕交替不截掉长句两端 |
| T20 | 软件教程 UI 密集时，不大量污染对白 |

### Windows 真实场景（按可访问性和本地环境执行）

首选仓库已用样本：
- `BV1FW41157y3`（PMX Editor 软件教程，烧录中文字幕）：强制 OCR 路线，检验 ROI、识别句数、漏句、UI 污染、字幕边界与画面文字通道。
- `BV18gDFBVEQF`（平台字幕样本）：验证 platform/auto 快捷路线、字幕缓存、不下载视频。
- 另增加**双语**、**字幕中途换位置**、**短长字幕交替**的真实/合成样本各一个；若难找符合条件的公开视频，生成 10～30 秒合成测试片段也可，但要明确标注“合成”。

可用基本命令（按本机环境调整）：

```powershell
.venv\Scripts\python.exe -m pytest -q
uvx ruff check src tests --select F,E9,W,I,UP,B,RET,C4 --line-length 100
.venv\Scripts\bili.exe doctor --json

.venv\Scripts\bili.exe fetch BV1FW41157y3 --parts 1 --mode subtitle --subtitle-source ocr --refresh --json
.venv\Scripts\bili.exe fetch BV1FW41157y3 --parts 1 --mode illustrated --subtitle-source auto --refresh --json
.venv\Scripts\bili.exe fetch BV18gDFBVEQF --parts 1 --mode subtitle --subtitle-source platform --refresh --json
```

记录信息应至少包含：总耗时、OCR 调用次数、检测 ROI、字幕句数、`segment_time_coverage`、最长连续缺口、`on_screen_text` 数量、产出图片数量、是否真正命中缓存、人工抽样发现的问题及处理情况。

## 5. MiniMax 最终交付规范

1. **先修代码，再写总结**。修复应有单元测试，不能仅把 `docs/PROGRESS.md` 的状态改成 ✅。
2. 在 `docs/PROGRESS.md` 追加“REVIEW2 执行结果”，记录 R2-01～R2-07 的状态与验证证据；需要暂缓时写明原因，不伪称完成。
3. 产物仍保持 `transcript.md / transcript.srt / article.md / assets / manifest.json`，扩展数据结构时尽量向后兼容。
4. Phase A、Phase B 各至少一次测试汇总和提交；将实际 `pytest` 用例数量与结果如实报告（不能沿用历史 `183 passed` 作为这轮结果）。
5. 提交 GitHub 并在回复中列明：commit SHA、修改文件、功能回归是否通过、Windows 真实视频测试是否执行、已知风险和下一步。

**验收重点不是让 OCR 分数看起来更高，而是让 Agent 在真实 B 站教程里不会因区域错选、缓存假命中、粗粒度 ASR 被吞或空帧旧字幕复活而丢失关键内容。**