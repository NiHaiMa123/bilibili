# Agent 接入指南

本文只记录**在本机实际验证过**的配置方式。命令与字段均来自各客户端自身的
CLI/`--help` 输出；如果换机器或客户端版本不同，请先用 `--help` 复核。

## 0. 前置

```powershell
cd E:\project\bilibili
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[mcp]"
Copy-Item config.example.toml config.toml   # 填好 bdl_cli / ffmpeg / asr 路径
```

自检：

```powershell
.venv\Scripts\bili.exe doctor
```

`doctor` 里 `bdl_cli` / `ffmpeg` / `asr_model` / `gpu` 都是 `OK` 才算就绪；
只有纯字幕路线时，`ocr` / `qwen3_asr` 缺失不影响使用。

## 1. MiniMax Code（本机已验证）

MiniMax Code 用自己的 MCP 配置管理命令，stdio 服务器直接填解释器路径：

```powershell
mavis mcp create bilibili stdio `
  "E:\project\bilibili\.venv\Scripts\python.exe" `
  '["-m","bilibili_agent.mcp_server"]' `
  --description "bilibili-agent 本地 MCP"
```

本机已经用等价方式注册成功（`mavis mcp list` 可见 `bilibili`，transport=stdio）。
用 `mavis mcp update bilibili ...` 可改路径，用 `mavis mcp delete bilibili` 可移除。

注册后需要在客户端里重新加载会话，MCP 工具才会出现在工具列表中。

## 2. Codex（本机已验证配置文件格式）

Codex 桌面版把 MCP 服务器写在 `~/.codex/config.toml`，字段为本机实际存在的
`command` / `args` / `env_vars` / `startup_timeout_sec`，环境变量放在
`[mcp_servers.<name>.env]` 子表里。追加：

```toml
[mcp_servers.bilibili]
command = 'E:\project\bilibili\.venv\Scripts\python.exe'
args = ["-m", "bilibili_agent.mcp_server"]
startup_timeout_sec = 60

[mcp_servers.bilibili.env]
PYTHONIOENCODING = "utf-8"
```

> 本机 `codex` CLI 未安装（只有桌面版），因此以上字段是**照抄本机现有
> `[mcp_servers.node_repl]` 配置的写法**，未经 Codex CLI 亲自拉起验证；
> 首次使用请先确认客户端里能看到 `bili_*` 工具。

## 3. Claude Code（本机已验证 CLI 语法）

`claude mcp add` 的用法来自本机 `claude mcp add --help`：

```powershell
claude mcp add bilibili -- "E:\project\bilibili\.venv\Scripts\python.exe" -m bilibili_agent.mcp_server
```

* 默认作用域 `local`；用 `--scope user` 可对所有项目生效。
* 需要额外环境变量时加 `-e KEY=value`（放在 `--` 之前）。

## 4. DSH

本机未安装 DSH，无法在此验证其 MCP 注册字段。通用 stdio 形式如下，
请按 DSH 自己的文档落地后再用：

```json
{
  "mcpServers": {
    "bilibili": {
      "command": "E:\\project\\bilibili\\.venv\\Scripts\\python.exe",
      "args": ["-m", "bilibili_agent.mcp_server"]
    }
  }
}
```

## 5. 没有 MCP 支持时的 CLI 兜底

所有 MCP 工具都有等价 CLI，输出 JSON 字段一致：

```powershell
bili search "关键词" --limit 10 --json
bili inspect BVxxxxxxxxxx --json
bili fetch BVxxxxxxxxxx --parts 1 --mode subtitle --json
bili fetch BVxxxxxxxxxx --parts 1 --mode illustrated --subtitle-source asr --json
bili status <job_id> --json
```

## 6. 给 Agent 的调用约定

见 [`../skills/bilibili/SKILL.md`](../skills/bilibili/SKILL.md)。要点：

* 搜索结果只是候选，**不能**当作"看过视频"的内容依据；
* 引用必须带 BV 号 + 时间戳；
* 长内容不要一次性读进上下文，用 `bili_read_transcript` 的 `start_ms/end_ms` 精读。

## 7. 冒烟验证

注册完成后，用下面三条验证链路是否打通（任一失败先看 `bili doctor`）：

1. `bili_search` → 返回候选列表（有 `bvid` / `title` / `url`）。
2. `bili_inspect` → 返回分 P 与 `cid`。
3. `bili_materialize(mode="subtitle")` → 返回 `outputs` 里的本地
   `transcript.md` 路径；再用 `bili_read_transcript` 按时间段取到正文。

### 长任务走异步

OCR / ASR / 图文模式可能跑几分钟，Agent 调用会超时。`bili_materialize` 的
`wait=false` 会**提交一个持久本地任务并立刻返回** `job_id`（不是阻塞计算）：

```json
{"ok": true, "status": "queued", "job_id": "a1b2c3d4e5f6", "pid": 12345}
```

之后轮询 `bili_job_status(job_id)`，状态为
`queued → running → completed / failed`：

* `completed` → `outputs` 里有产物路径，`chapters` 里有章节索引；
* `failed` → `error` 里带 `stage` 与可读原因（不会永远停在 running）。

任务状态目录：`<output_dir>/../.cache/tmp/jobs`（与 `bili fetch --async`
用的是同一套协议与同一批文件），状态文件是原子替换写入的，读取不会看到半截 JSON。

完整回归矩阵见 [`PROGRESS.md`](PROGRESS.md)。
