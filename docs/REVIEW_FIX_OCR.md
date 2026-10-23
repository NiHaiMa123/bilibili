# REVIEW — Bilibili Agent 缺陷修复与 OCR 字幕区升级

> 交付对象：MiniMax Code；状态：待执行；基线：main @ 4da33da2e43dc0c7a270e4c432e75350a976e2ff（2026-10-23）。
>
> 目标：修复代码审查发现的明确逻辑缺陷，提升 Agent 长视频任务可靠性；在不推翻现有 RapidOCR/BDL/Qwen3-ASR 技术栈的前提下，将“最大字号动态文字行”升级为可靠的**时序字幕区域跟踪**。
>
> **直接实施，而不是再写一份架构方案。** 先核对最新版代码：若某项已经修复，验证测试后标记完成即可；不要覆盖其他人新提交。保留已能工作的 BDL、平台字幕、ASR 及 MCP 路径。该文档是审查建议和执行规范，问题位置对应上述基线，不代表在目标 Windows 本机已复现实测。

## 0. 执行原则、交付物和先后顺序

1. **先修 Bug（S0/S1），再升级 OCR（O1~O4）**。各阶段均要运行离线测试、做最少量真实端到端验证。
2. 不要“只改测试以通过”；每个问题增加能在修复前失败、修复后通过的针对性测试。
3. 沿用 Python 3.11+、现有 BDL CLI、FFmpeg、RapidOCR（PP-OCRv6）、外部 Qwen3-ASR Python 环境；不引入新桌面 GUI 或服务端。
4. 不直接移动或复制 E:\project\tts 中的模型权重，不提交媒体、Cookie 或本机绝对路径配置。
5. 本轮**代码修改和测试结果**应提交 Git；更新 docs/PROGRESS.md 一节“REVIEW_FIX_OCR 执行结果”，逐项记录：已修复 / 暂缓 / 无法复现，给出测试命令及关键真实产物路径（脱敏）。
6. 遇到依赖平台 API 或样本才能判断的现象，保留明确诊断，不把“待验证”写成“已修复”。

## 1. S0：优先修复影响整个 Agent 工作流的 Bug

### S0-1. MCP 的 wait=false 实际还是同步阻塞

代码：src/bilibili_agent/mcp_server.py 的 bili_materialize（约 165–221 行）；CLI 相关代码 cli.py（约 307–414 行）。

现状：MCP 暴露 wait 参数，但无论 true/false 都直接 pipe.fetch()；返回的数据也不包含可查询的 job_id。Agent 在 OCR/ASR 长任务上容易超时，bili_job_status 对这种调用找不到任务。

要求：
- wait=true 可以同步执行；wait=false 必须**提交持久本地任务并快速返回** job_id、queued/running 状态，不在 MCP 调用线程执行重计算。
- 优先复用 cli.py 现有 _fetch_worker / jobs 机制或抽成共享 JobManager；CLI 和 MCP 使用同一状态目录与任务协议，避免维护两套。
- Worker 将任务状态 queued → running → completed / failed 写入 job 文件；原子替换文件，防止 status 读到半截 JSON。
- 子进程无法启动、启动后立即退出、被异常杀死/超时等要有明确失败状态；不得永久停留 queued/running。实现基础任务取消可放下一阶段，但 status 不能永远假运行。
- 如果先做最低可用版本，保留简单单机实现，不必引入 Redis/Celery。
- max_images 和其他任务参数需原样传给 worker；避免共享 cfg 参数“本进程生效，异步进程丢失”。
- CLI --async 也要透传 --refresh。现有 CLI _fetch_worker 未透传 refresh。

验收测试：
- mock pipeline 中故意 sleep，调用 MCP wait=false，应在任务完成前返回 job_id；轮询可见 running→completed，outputs 存在。
- wait=true 仍同步返回结果；Worker 抛异常返回 failed 和有信息的 error。
- 单独验证 CLI --async --refresh 不会静默复用旧产物。

### S0-2. 字幕源选择语义错误，强制 ASR 可能走 OCR

代码：pipeline.py _fetch_part（约 492–524）和 _fallback_to_media（约 280–351）。

根因：try_ocr = ocr_enabled and (subtitle_source == "ocr" or cfg.ocr_auto)；当 source="asr" 且 ocr_auto=true 时仍进入 OCR；source="ocr" 失败后又进入 ASR。更高优先级的媒体软字幕也可能覆盖显式的 asr/ocr 选择。

将路由定义明确如下：

| subtitle_source | 允许来源 | 失败行为 |
| --- | --- | --- |
| platform | 仅 B 站字幕 | 报缺字幕，不下载媒体 |
| ocr | 仅视频烧录字幕 OCR | OCR 不足则明确失败，不偷偷 ASR |
| asr | 仅音频 Qwen3-ASR | 不运行 OCR/平台字幕/软字幕；优先 audio-only |
| auto | 平台字幕 → 可用软字幕 → OCR（启用时）→ ASR | 任一级不完整可补缺口并记录来源 |

建议将获取方法分离成小函数（platform/soft/ocr/asr），用显式调度而不是一个布尔参数控制整个大分支。

验收：
- source=asr 时模拟 OCR 不可用/配置 auto=true，仍只调用 ASR。
- source=ocr 且 OCR 失败时，ASR mock 断言调用 0 次。
- source=platform 时 BDL download mock 调用 0 次。
- auto 下确实按需回退，附上每一级诊断。

### S0-3. b23.tv 短链接解析提前调用无参数 view，导致失败

代码：inspect.py _resolve_bvid（约 76–82）。

现状：短链解析前使用无参数的 x/web-interface/view “占位请求”；真实接口可返回非零错误，导致短链根本走不到跳转。

要求：
- 删除这个无效请求，直接走合法的短链解析流程。
- 校验 URL scheme=https、hostname 属于 b23.tv / www.b23.tv（或经过明确核实的官方短链域）；不能仅用原始字符串包含 "b23.tv"。
- 跳转次数有限，最终目标属于允许的 B 站视频域名；检查跳转中间域名，不把用户 Cookie 或凭据送到第三方。
- 从最终 URL 提取 BV，保留 URL 中可解析的 p 参数；对不合法链接返回明确错误。

验收：普通 b23.tv 成功，伪域名、未允许的重定向、无 BV 目标明确拒绝；短链处理不再调用空参数 view。

### S0-4. 图文 / assets 模式失败仍可能被当作完成

代码：pipeline.py _fetch_part（约 525–621）。

现状：有字幕但 _build_frames 失败/无图片时仍返回 ok=true，甚至未创建 article.md；模式 assets 也先强求字幕，否则不会产出图片。

要求：
- 为不同模式定义 **必需产物**：
  - subtitle：transcript.srt + transcript.md（文本可用）。
  - illustrated / both：上述字幕 + article.md + 至少一张存在且可读的关键帧。
  - assets：仅关键帧文件及 manifest，**不要求任何字幕**；可通过场景/时间采样独立完成。
  - metadata：元信息即可。
- 在 _fetch_part 末尾依据请求模式校验实际产物，而非仅按 segments 是否非空认定成功。
- 若部分成功但不满足目标，显式 status=partial/failed、warnings、已生成 outputs；不可让缓存把 partial 当成 illustrated 完成。
- 用户明确请求 illustrated 且抽帧失败时，不能谎报完整成功。

验收：强制抽帧失败 → illustrated 不得返回完整成功；assets 且平台/音频都没有字幕仍能正常生成图片；各模式按真实输出校验。

### S0-5. 未配置 BDL 时的异常类型错误

代码：bdl_adapter.py _run 中约第 102 行。

现状：BdlError 构造参数是 code，但缺程序的分支传入 error_code=None，触发 TypeError，遮蔽原始错误。

改为合法参数，并加入缺失 BDL 文件的测试，必须返回带 stage 的可诊断 BdlError，不出现 TypeError。

## 2. S1：正确性、安全边界与结果可用性

### S1-1. 字幕 URL 校验与鉴权头

代码：bili_api.py is_trusted_subtitle_url（约 269–275）、BiliClient.get_text、HTTP 重定向。

当前 host.endswith("hdslb.com") / host.endswith("bilibili.com") 可接受 evilhdslb.com 这种后缀相似域名；未强制 HTTPS。更重要的是 get_text 会把整个 Cookie 头附在取文本请求中。

要求：
- 官方允许域名采用 host == suffix or host.endswith("." + suffix)，且 scheme 必须 https；按实际使用场景收紧到必要的子域，不接受任意链接。
- 跟随跳转时每一步目标重新验证；默认字幕 CDN 请求不附带帐号 Cookie，确实需要鉴权时仅发给允许且必要的域名。
- 测试 evilhdslb.com、hdslb.com.attacker.example、http 协议、有效官方子域、非官方重定向。
- 保持现有合法视频字幕下载可用。

### S1-2. OCR 固定顶部区域上下界颠倒

代码：ocr_subtitles.py OcrConfig.fixed_band（约 82–89）。

当前 top 分支 default band_bottom=0.99 会返回 (0.02,0.01)，裁剪为空。

要求明确 top 区高度的配置语义（最好使用独立 top_top/top_bottom 或显式 ROI），合法化/校验 (0≤y0<y1≤1)，而不是悄悄返回空图片。测试 top / bottom / auto 及非法区间。

### S1-3. ?p=2 参数没有驱动分 P 选择

代码：inspect.py 已有 extract_page，pipeline.py _select_parts 默认只选 P1。

当 target URL 带 ?p=N、且未显式传 parts 时，默认该 N；显式 --parts 优先。所有下载、字幕、文档和 manifest 用选中 P 的 CID。测试 P1/P2、无参数、显式覆盖及错误 P 号。

### S1-4. 缓存键缺少重要参数；模式升级没有真正复用字幕

代码：pipeline.py settings_digest（约 468–477）、cache.py existing_subtitle_artifacts。

摘要目前未包含 max_images、min_gap_seconds、phash_distance、OCR ROI/采样/阈值、ASR 模型路径/aligner/chunk、实际配置版本等；这些参数变化可能命中错误缓存。cache.py 虽声明可复用 subtitle→illustrated，但 pipeline.py 未见真正调用 existing_subtitle_artifacts。

要求：
- 明确 transcript 与 frames 分开缓存的依赖；优先以 component-level digest 复用纯字幕，illustrated 升级只跑新增抽帧；保留足够元数据重建需要的时间戳 segments（可解析已有 SRT）。
- 依赖变更使对应部分重建，不能修改 max_images 后仍返回上一轮图片。
- 原子写 manifest、校验文件实际存在/大小；失败、partial 或 mode 不符合不能命中完整缓存。
- max_images 从 MCP 调用生效且参与缓存；ASR 配置更换后不会误复用旧转写。

### S1-5. 覆盖率用首尾跨度而不是真实字幕覆盖

代码：subtitle_normalize.py coverage_stats（约 175–205）。

如视频 10 分钟，仅第 0、300、599 秒各有一句，现实现仍可能接近 100%。

应新增：
- **segment_time_coverage**：将所有合法 [start,end] 与 [0,duration] 截交、合并时间并集后除以 duration，避免重叠重复计算；
- **span_ratio**：原有首尾跨度保留，但必须另命名，不能冒充“实际覆盖”；
- 可用音频 VAD/ASR 时进一步算 speech_coverage，用于判断漏掉了多少语音；
- 短对白与长静音并存的教程不要仅靠总时长比例宣布识别失败。
- 平台字幕严重越界/错 P 的轨道不得因 coverage 截到 1.0 就报高质量。

这直接影响 OCR→ASR 回退判定，见 O3。

### S1-6. MCP 章节索引字段缺失

代码：mcp_server.py _chapter_index（约 328–334），pipeline.py _fetch_part 返回值。

MCP 读取 parts[].chapters，但 Pipeline 的分 P 结果没有这个字段，所以章节索引为空。真实把章节结构输出到 manifest/part result，或 MCP 从已生成产物读取；测试生成包含两章的字幕文件后，materialize 返回非空 chapters，read_transcript 能用章节范围读到相应文本。

### S1-7. 关键帧选择偏向开头/高切换区域

代码：frame_selector.py build_candidates / thin（约 89–145）。

现在场景变化候选按分组优先排在字幕锚点前面、并按分数/时间混合顺序遍历；thin 却用上一张候选的时间来判断间隔，可能造成不可靠的抽稀。场景变化过多也可能提前占满 max_images，使后半段没有图。

要求：
- 按视频时间分桶（如每章或均匀时间窗），各桶有最低保障的候选预算；再按字幕相关性、UI 变化、清晰度、去重质量评分。
- 决定 min_gap 时依时间排序/与已选图片比较；局部超量时保留高质量帧；最终全片按时间输出。
- 固定 max_images 可覆盖全片/主要章节，避免前 1/4 视频用完配额。
- 测试“前段 100 个场景变化 + 后段 2 个关键操作”时后段仍有图。

### S1-8. 搜索 limit=50 实际只查询一页 20 条

代码：search.py SearchProvider.search（约 107–141）。

limit>20 时按需分页，去重、尊重结果上限和 API 错误，限制总请求数，遇风控停止而非重试循环。测试模拟两页，limit=30 真返回30个去重结果。

### S1-9. ASR 默认只按中文识别

代码：asr/adapter.py、asr/worker.py 中 language 默认 Chinese。

改为可配置语言，**自动识别作为通用默认**；用户明确选择 Chinese/English/其他支持语言时才强制指定。需核对当前安装 qwen_asr 对自动识别的实际参数形式（官方示例通常使用 language=None），不能把字符串 "None" 传给模型。测试中文/英文路径参数及回退诊断，模型真实验证放到 E2E。

### S1-10. 文字型成功和错误输出一致性

- CLI async --refresh 的透传、status 文件状态一致性如 S0-1。
- 多 P 部分失败时聚合为 partial/failed，不能顶层不带分 P 错误原因。
- 返回给 Agent 的内容路径、source_url（带正确 ?p）、时间戳和 status 不得相互矛盾。
- 高代价调用的错误诊断应带阶段，避免对用户只报“没有可用的字幕内容”。

## 3. O1：OCR 字幕区改造——先区域跟踪，再逐句 OCR

**背景：当前方案优点应保留**。现有 ocr_subtitles.py 的 RapidOCR + PP-OCRv6、自动检测、几何过滤、跨帧合并已经在 BV1FW41157y3 软件教程上跑通；检测出的 band=[0.816,0.9212] 是有效的实测依据。不要重新开发 OCR 识别模型。

当前限制：
- detect_band() 在全视频均匀采 12 帧，按文本框 cy 聚成行带，在动态文字带里取最大字号，可能错选 PPT 大标题/游戏 UI/弹窗。
- 只得到纵向 top/bottom，没有横向 x0/x1；桌面软件左右面板仍污染。
- 按“全文同一位置出现超过15%”判静态，会把长字幕错删；识别后 _pick_line 只拿一行，双行字幕丢一行。
- 单一全视频 ROI 不适合部分片段改成双语/顶部/不同字幕布局。
- confidence 现在近似只和字高挂钩，不代表经校准的“字幕区正确概率”；命名为 heuristic_score 更准确。

### O1-1. 新增 SubtitleRegionTracker，不要直接把 detect_band 扩成巨型函数

建议在 src/bilibili_agent/ 下设 subtitle_region.py（或者合理的小模块），可复用现有 OCR 引擎。负责：
1. 按**时间分层**取多个短窗口，而不是全片仅均匀取12张；例如开头/中间/末尾及检测到布局变化附近，各连续采 5–10 秒；窗口内 2–4 fps 只是初始可配参数，先测真实耗时。
2. 整帧 OCR，保留 box=(x0,y0,x1,y1)（相对尺寸）、文字、置信度、时间、字高、所在行；不要一开始就裁底部。
3. 使用空间重合（IoU/中心距离）、行高、位置稳定性跨帧关联文字框为 region track。同轨迹允许文字更新；不要要求每一帧完全同文本。
4. 聚集同轨迹形成一到多个四维 ROI：x0,y0,x1,y1；支持主字幕区、中英双行区，必要时按时间段变更 ROI。
5. 选 ROI 时按多项可解释特征打分：
   - 空间位置/宽度/居中情况（底部是**软先验**，不是硬规则）；
   - 行高与样式稳定、连续时段出现、内容按句替换；
   - 字幕文本长度/变化节奏合理；
   - 永久 UI 标题/水印/菜单和瞬时弹窗的负向特征；
   - 可选结合 ASR/VAD 的语音时段匹配，不把匹配当作唯一准则。
6. 返回 ROI 的 **score+evidence**：哪些时间段、哪些文本变化支持该结论。低分时允许多候选并自动 fallback 原 detector，而不是强行猜一个。

建议新数据模型（示意，不是现成接口）：

~~~json
{
  "regions": [
    {
      "x0": 0.12, "y0": 0.81, "x1": 0.88, "y1": 0.93,
      "start_ms": 0, "end_ms": 600000,
      "role": "dialogue_subtitle",
      "heuristic_score": 0.84,
      "evidence": ["same_position", "text_turnover", "line_style"]
    }
  ],
  "method": "temporal_tracking_v1",
  "warnings": []
}
~~~

**不要把例子中的 0.84 叫已校准概率。**

### O1-2. 静态界面文字和真字幕的时序判定

重写 observe() 内频率剔除策略：

- 以**局部时间窗和轨迹生命周期**判静态，而不是按整片采样数 * 0.15 简单删句。
- 同一句字幕在 1–10 秒内一直不变是正常现象，不能因为多帧相同就删除。
- 永久 UI 文字往往在多个不相邻窗口、长达数分钟保持同一 text+geometry；这与“连续几秒后换另一句”的字幕模式不同。
- 短促只出现一帧的字幕并非一定噪声；在文字置信度高、相邻帧确认/短时高采样支持时应允许保留，不硬性丢弃。
- 对 OCR 字符级小误差、同一句更新（逐字出现/双行换行），使用文本相似性、轨迹连续性与位置稳定性联合合并；不得将两句不同话误并成一句。
- 对软件操作教程：文件名/按钮/菜单/PPT 标题应输出到独立 on_screen_text 或 frame evidence，而非 dialogue transcript。

### O1-3. 多行字幕和横向范围

- 一个 ROI 内允许 1～3 行字幕，按真实 y 顺序、同行 x 顺序合并，保留换行或一个空格。
- 中英双语字幕可保留分语言行，若目标仅要中文需明确选择策略；不要简单取字体最大的一行。
- 字幕区背景、描边和字幕高度变化需要容忍；左右 UI 密集区不应干扰中心对白字幕。
- 在 OCR 识别前裁剪四维 ROI，并能为每一帧给出该 ROI 在原始画面上的坐标；当新布局出现时可重新定位。

## 4. O2：OCR 触发式识别与字幕起止时间细化

现有 extract_frames() 按固定 1 fps 抽整片，容易漏掉短句；每帧都 OCR 又浪费 CPU。

建议流程：
1. **轻量字幕区域变化检测**：在锁定的 ROI 内比较局部像素/边缘/文本区域变化，跳过几乎不变的帧；变化后再做 RapidOCR。
2. **双速率**：全片较低采样用于覆盖；有变更/疑似漏句窗口局部加密到 2–4 fps（按实际速度测试，可视情况更高），不用整片逐帧 OCR。
3. **时间边界**：确认文本转场前后，在局部时间点二分/小步扫描帧，细化字幕开始和结束；若未细化，manifest 标注时间精度为采样间隔级，禁止伪装毫秒精准。
4. **低置信复核**：文字检测框小/模糊/遮挡时对附近多帧取清晰版本，合并候选；如仍失败标记 uncertain，不输出编造文本。
5. OCR 坏帧不能被静默变成语句分隔；需要把“空白帧”和“引擎异常/读帧失败”区分记录。
6. 字幕画面位置随视频时间变化时，允许按时间段保存多个 ROI，不重新 OCR 整片。

效率验收：记录检测阶段和识别阶段消耗帧数、OCR 调用数、总耗时；新策略至少不应在同样配置下比旧方案无理由地大幅增加 OCR 调用量。优先改善正确率和漏句率，不为速度牺牲明显信息。

## 5. O3：OCR 与 ASR 的协作及完整性判断

目标不是强制 OCR 和 ASR 都跑，而是根据内容需求选择成本最低、信息最全的可靠路径。

1. 有完整平台字幕时**不跑 OCR/ASR**；pure subtitle 模式不下载视频。
2. 无平台字幕时先检测有无烧录字幕；如确有高质量 OCR，则生成 hard_subtitle_ocr。
3. **不要用 10% 的整片覆盖率阈值判定 OCR 足够**。应结合有效字幕时长并集、视频长度、字幕句数、语音活动/VAD 覆盖、连续遗漏时段。短教程存在长时间静音时不应被误判；语音连续但 OCR 只识别几句时不能声明完整。
4. OCR 缺口显著时在 auto 模式调用 ASR **补缺口**，先可实现简单全片 ASR 并按时间去重合并，之后再优化为仅在缺口区间转写。保留 segment 的 source 和置信/flag，不能把两种来源悄悄融合后都标成 OCR。
5. ASR 文本与 OCR 文本很不一致时，保留原始证据、标记冲突，避免盲目以 ASR 改写图上原字。字幕可能是翻译/意译，不能仅凭相似度断言 OCR 错误。
6. 可复用 Qwen3-ASR + ForcedAligner 的全局时间偏移，但只有可靠对齐的片段才按词/字时间输出。统一时基后再匹配字幕与关键帧。

## 6. O4：将“字幕文字”与“画面文字”分成两条证据通道

本项目最终目的是让 Agent 学会看 B 站教程，不只是还原话语。

- **speech/subtitle**：讲者对白/烧录对白字幕/Qwen3-ASR，输出 transcript.srt、transcript.md。
- **on_screen_text**：按钮、菜单、文件名、参数、PPT 文字、软件界面标签，绑定 frame/time_ms；可以随图文 article.md 附上 OCR 证据，不要把它当成发言。
- 图片关联章节应按时间戳和讲解段选择；含“点击这里/调整成这样”的句子附近必须尽量有能观察到操作界面的关键帧，不能用不相关图片冒充。
- 可先让 UI 文本只写进 manifest.extra.visual_text / 图像辅助信息，暂不引入视觉大模型自动叙述“点击了哪个按钮”。
- 如果以后采用视觉模型做判断，仅用于低置信 ROI/截图的局部复核，不作为所有视频的强制依赖。

图文示例：

~~~md
## [04:12–04:55] 修改模型材质

[04:17] 接下来把这里的材质替换掉。（语音/字幕）

![04:18 PMX Editor 操作界面](assets/frame_000258000.jpg)

画面中可识别文字（OCR，未必是讲者原话）：Material / Texture / ... 
~~~

## 7. 外部工具对照基线（可研究，不要求整套替换）

- VideoSubFinder：https://github.com/SWHL/VideoSubFinder
  - 重点研究：从视频发现烧录字幕区域、字幕图像的时间连贯性，而非整片帧上跑通用 OCR。
- RapidVideOCR：https://github.com/SWHL/RapidVideOCR
  - 重点研究：对 VideoSubFinder 提取的字幕图像进行 RapidOCR 批处理、生成 SRT/ASS/TXT。
- Subtitle Edit：https://github.com/SubtitleEdit/subtitleedit
  - 重点研究：硬字幕检测、重复画面合并、时间边界调整及人工检查路径。
- Qwen3-ASR：https://github.com/QwenLM/Qwen3-ASR
  - 用于补全确实缺失的语音内容及对齐；注意多语言与时间戳策略。

要先在相同测试视频上比较效果，再决定是否借鉴现成算法/CLI；**不强制加入三个大型外部工具依赖**。

## 8. 回归测试矩阵（必须实际执行）

### 离线测试：先补针对性失败用例

| Case | 输入/模拟 | 预期 |
| --- | --- | --- |
| T01 | MCP bili_materialize(wait=false) | 立即返回 job_id；状态可查询，最后 completed |
| T02 | MCP 异步 worker 异常 | failed + error，不能一直 running |
| T03 | CLI --async --refresh | 后台真的刷新，不复用旧缓存 |
| T04 | source=asr，ocr_auto=true | OCR 绝不执行 |
| T05 | source=ocr，OCR 空结果 | ASR 绝不执行，返回明确失败 |
| T06 | source=platform，无字幕 | 不触发媒体下载 |
| T07 | b23.tv 短链 | 不触发空 view 请求；合法目标正常解析 |
| T08 | illustrated 抽帧失败 | 不能 ok=true/缓存成完整 illustrated |
| T09 | assets 且无字幕 | 仍可得到真实图片 |
| T10 | 无 BDL CLI | BdlError 可诊断，不产生 TypeError |
| T11 | 字幕恶意相似域名/HTTP/跳转 | 拒绝且不发送 Cookie |
| T12 | fixed top / bottom 区域 | 有效 ROI y0<y1，默认 top 不再颠倒 |
| T13 | URL ?p=2 / --parts 1 | 分别选择 P2 / P1，CID 正确 |
| T14 | OCR 采样率/ROI、max_images 等改动 | 对应缓存失效/增量重建 |
| T15 | 10分钟视频仅 3 秒字幕 | 时间并集覆盖很低，不是约 100% |
| T16 | MCP 两章节 | chapters 非空，可按范围读取 |
| T17 | 前段大量镜头切换 | 后半段关键帧仍有保障 |
| T18 | 搜索 limit=30 | 必要时查两页并去重达到目标 |
| T19 | ASR 语言自动/指定 | 传参符合真实 qwen_asr API |
| T20 | 长字幕静止 6秒、两行字幕 | 不误删；两行都保留 |
| T21 | 软件标题栏常驻、弹窗变化 | 不错误归入对白字幕 |
| T22 | 字幕在顶部/中部/双语/中途移位 | 对应时间段的 ROI 跟踪正确 |
| T23 | 检测引擎单帧错误 vs 真正空白字幕帧 | 两者状态区分，避免断句错误 |
| T24 | 多 P 部分失败 | 顶层与分 P 状态、产物一致 |

### Windows 真实 E2E 样本（不要把受版权保护的媒体上传 Git）

优先使用仓库 PROGRESS.md 已跑通的示例：
- BV1FW41157y3：PMX Editor 教学；验证 OCR 自动字幕 ROI、中文句子、关键帧与语音并行证据。
- BV18gDFBVEQF：已记录存在可提取的平台字幕；验证纯 subtitle 模式不下载视频。
- 另自选合法可访问的多 P、英文或双语、PPT 大标题、小号字幕、字幕移位的视频各至少一段短样本。
- 每个样本导出 report JSON：选中 P/CID、所用来源、ROI、coverage 指标、OCR 耗时/调用数、识别段数、图数、失败/警告。
- 对旧版 OCR 和新版输出做小规模人工对照：字幕 ROI 命中率、字幕漏识/重复/UI 污染、时间对齐。没有标注样本时只报告观察，不杜撰百分比分数。

基本命令：

~~~powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check src tests
bili doctor --json
bili fetch BV1FW41157y3 --parts 1 --mode subtitle --subtitle-source ocr --refresh --json
bili fetch BV1FW41157y3 --parts 1 --mode illustrated --subtitle-source auto --refresh --json
~~~

以上最后两条要求 Windows 本地依赖、视频可访问；不能在网络或环境未就绪时宣称通过。ruff 可执行文件路径需按实际安装情况调整。

## 9. 最终验收与提交要求

- [ ] S0 五项已修复，分别有回归测试；MCP 长任务不阻塞、状态真实。
- [ ] S1 正确性问题已覆盖；安全域名校验与 Cookie 不外泄通过测试。
- [ ] OCR 不再只依据“字号最大/15%全文频率”判断字幕；实现至少一个跨帧 ROI 跟踪策略，含四维坐标与 evidence。
- [ ] 双行字幕、长时间保持的字幕、软件 UI 密集画面、位置变化至少有测试，不会简单被剔除。
- [ ] 可以独立输出 on_screen_text（先保留为 JSON 元数据也可以）。
- [ ] illustrated 真正存在 article.md 和图片，assets 不依赖字幕；Agent 能从 MCP 读到正确的索引与文件。
- [ ] 原有 87 个离线测试无回归（若数量变化以实际测试为准），新增测试覆盖本轮缺陷。
- [ ] 至少两条 Windows 真实场景运行报告写入 docs/PROGRESS.md；若不能运行须写明实际阻塞原因。
- [ ] 提交源代码、测试、配置说明与进度记录到 Git；回复用户时给 commit SHA、修改文件、通过/未通过项目与可复现命令。

**优先级提醒**：首轮先交付所有 S0 与关键 S1 的正确性修复，再推进 O1/O2。不要为“完美泛化字幕识别”让整个 Agent 的实用闭环长时间不可用，也不要将计划中的视觉大模型作为必需依赖。