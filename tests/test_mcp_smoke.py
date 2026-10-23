"""MCP 服务器冒烟测试：真的起一个 stdio 服务器，用 MCP 客户端连上去调用。

跑法：
    .venv\\Scripts\\python.exe -m pytest tests/test_mcp_smoke.py -q
或
    .venv\\Scripts\\python.exe tests/test_mcp_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "src" / "bilibili_agent" / "mcp_server.py")


def _server_command() -> list[str]:
    """优先用本项目 venv 的解释器；否则用当前解释器 + PYTHONPATH。"""
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_python.is_file():
        return [str(venv_python), "-m", "bilibili_agent.mcp_server"]
    return [sys.executable, SERVER]


def test_mcp_server_lists_tools():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(
            command=_server_command()[0],
            args=_server_command()[1:],
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [t.name for t in tools.tools]

    names = asyncio.run(run())
    assert set(names) >= {
        "bili_search",
        "bili_inspect",
        "bili_materialize",
        "bili_job_status",
        "bili_read_transcript",
    }


def test_mcp_tool_schemas_have_enums():
    """参数枚举必须在 schema 里描述清楚，不能让 Agent 猜。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        cmd = _server_command()
        params = StdioServerParameters(
            command=cmd[0], args=cmd[1:],
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                out = {}
                for t in result.tools:
                    schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", {})
                    out[t.name] = schema
                return out

    schemas = asyncio.run(run())
    materialize = schemas["bili_materialize"]["properties"]
    assert "mode" in materialize and "subtitle_source" in materialize
    # 参数描述与枚举必须真的进 schema，Agent 才能不靠猜
    assert materialize["mode"]["description"]
    assert set(materialize["mode"]["enum"]) == {
        "metadata", "subtitle", "illustrated", "assets", "both",
    }
    assert set(materialize["subtitle_source"]["enum"]) == {
        "auto", "platform", "ocr", "asr",
    }
    assert "url_or_bvid" in schemas["bili_inspect"]["properties"]
    assert schemas["bili_inspect"]["properties"]["url_or_bvid"]["description"]
    assert "start_ms" in schemas["bili_read_transcript"]["properties"]
    assert "sort" in schemas["bili_search"]["properties"]
    assert set(schemas["bili_search"]["properties"]["sort"]["enum"]) == {
        "relevance", "newest", "most_viewed", "most_danmaku", "most_stow",
    }


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
