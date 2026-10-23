"""bilibili-agent: Bilibili 检索与图文知识化流水线。

分层：search → inspect → pipeline(fetch) → md_writer / cache。
对外暴露 CLI（``bili``）与 stdio MCP（``bilibili_agent.mcp_server``）。
"""

__version__ = "0.1.0"

# 产物结构变化时递增，manifest 中记录，便于追溯
PIPELINE_VERSION = "0.1.0"

__all__ = ["__version__", "PIPELINE_VERSION"]
