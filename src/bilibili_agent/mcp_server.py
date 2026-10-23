"""stdio MCP 服务器：把流水线暴露给 Agent。

设计约束：
- 只暴露结构化参数与本地路径，不返回 Cookie、签名 URL、媒体直链。
- 长内容不进 MCP 响应：``bili_materialize`` 只返回文件路径与章节索引，
  Agent 需要时再用 ``bili_read_transcript`` 按时间段精读。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from .bdl_adapter import BdlError
from .bili_api import BiliApiError
from .config import load_config
from .inspect import SourceError
from .pipeline import MODES, SUBTITLE_SOURCES, Pipeline

SortOption = Literal["relevance", "newest", "most_viewed", "most_danmaku", "most_stow"]
ModeOption = Literal["metadata", "subtitle", "illustrated", "assets", "both"]
SourceOption = Literal["auto", "platform", "ocr", "asr"]

SORT_HINT: dict[str, str] = {
    "relevance": "综合相关度（默认）",
    "newest": "最新发布",
    "most_viewed": "播放量最高",
    "most_danmaku": "弹幕最多",
    "most_stow": "收藏最多",
}

SERVER_NAME = "bilibili"
INSTRUCTIONS = """Bilibili 视频检索与图文知识化工具。

典型流程：
1. bili_search 找候选（只搜索不下载）
2. bili_inspect 看分 P 与时长
3. bili_materialize 生成字幕或图文 Markdown（返回本地路径，不返回全文）
4. bili_read_transcript 按时间段读取需要引用的原文

注意：搜索摘要只是候选信息，不能当作"已看过视频"；
只有 materialize 成功产出的 transcript/article 才能作为内容依据引用。"""


def _tool_error(stage: str, message: str, code: int | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "failed",
        "error": {
            "stage": stage,
            "error_code": code,
            "message": message,
            "retryable": False,
        },
    }


def _build_server():
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name=SERVER_NAME, instructions=INSTRUCTIONS)

    def _pipeline() -> Pipeline:
        return Pipeline(load_config())

    @server.tool(
        name="bili_search",
        title="搜索 B 站视频",
        description=(
            "按关键词搜索 B 站视频，返回可追溯的候选列表（标题、UP 主、时长、发布时间、"
            "BV 号、简介）。只搜索不下载。用于回答'B 站上关于 X 的视频'这类问题。"
        ),
    )
    def bili_search(
        query: Annotated[str, Field(description="搜索关键词，例如 'MMD PMX Editor 换装'")],
        limit: Annotated[int, Field(description="返回条数，1-50，默认 10")] = 10,
        sort: Annotated[SortOption, Field(
            description="排序方式：" + "；".join(f"{k}={v}" for k, v in SORT_HINT.items())
        )] = "relevance",
    ) -> dict[str, Any]:
        """limit 1-50；sort ∈ relevance|newest|most_viewed|most_danmaku|most_stow。"""
        pipe = _pipeline()
        try:
            hits = pipe.search(query, limit=max(1, min(limit, 50)), order=sort)
        except BiliApiError as exc:
            return _tool_error("search", str(exc), exc.code)
        finally:
            pipe.close()
        return {
            "ok": True,
            "status": "completed",
            "query": query,
            "count": len(hits),
            "results": [
                {
                    "bvid": h.bvid,
                    "title": h.title,
                    "author": h.author,
                    "duration_s": h.duration_s,
                    "published_at": h.published_at,
                    "view_count": h.view_count,
                    "url": h.url,
                    "description": (h.description or "")[:160],
                }
                for h in hits
            ],
        }

    @server.tool(
        name="bili_inspect",
        title="查看视频元信息与分 P",
        description=(
            "解析 BV 号或视频 URL，返回标题、UP 主、时长与全部分 P（page/cid/时长）。"
            "用于在下载前确认要处理哪个分 P。"
        ),
    )
    def bili_inspect(
        url_or_bvid: Annotated[str, Field(
            description="BV 号或视频 URL，例如 'BV1FW41157y3' 或 "
                        "'https://www.bilibili.com/video/BV1FW41157y3?p=2'"
        )],
    ) -> dict[str, Any]:
        pipe = _pipeline()
        try:
            info = pipe.inspect(url_or_bvid)
        except (SourceError, BdlError, BiliApiError) as exc:
            payload = exc.to_dict() if hasattr(exc, "to_dict") else None
            if payload:
                return {
                    "ok": False, "status": "failed",
                    "error": {
                        "stage": payload.get("stage", "inspect"),
                        "error_code": payload.get("error_code"),
                        "message": payload.get("message", str(exc)),
                        "retryable": payload.get("retryable", False),
                    },
                }
            return _tool_error("inspect", str(exc))
        finally:
            pipe.close()
        return {
            "ok": True,
            "status": "completed",
            "bvid": info.bvid,
            "title": info.title,
            "author": info.author,
            "duration_s": info.duration_s,
            "url": info.source_url,
            "parts": [
                {
                    "page": p.page,
                    "cid": p.cid,
                    "title": p.title,
                    "duration_s": p.duration_s,
                    "url": f"{info.source_url}?p={p.page}",
                }
                for p in info.parts
            ],
        }

    @server.tool(
        name="bili_materialize",
        title="生成字幕/图文 Markdown",
        description=(
            "对指定 BV/URL 与分 P 生成本地 Markdown（纯字幕或图文）。"
            "mode ∈ metadata|subtitle|illustrated|assets|both；"
            "subtitle_source ∈ auto|platform|ocr|asr。"
            "返回产物路径与章节索引，不返回全文（用 bili_read_transcript 精读）。"
            "图文/OCR/ASR 较慢时可用 wait=false 后台跑，再用 bili_job_status 查询。"
        ),
    )
    def bili_materialize(
        url_or_bvid: Annotated[str, Field(description="BV 号或视频 URL")],
        parts: Annotated[
            list[int] | None,
            Field(description="要处理的分 P 页码列表（从 1 开始）；留空只处理 P1"),
        ] = None,
        mode: Annotated[ModeOption, Field(
            description="metadata=只看元信息；subtitle=纯文字时间轴；"
                        "illustrated=图文（含关键帧与 article.md）；assets=只要图片；"
                        "both=文字+图文"
        )] = "subtitle",
        subtitle_source: Annotated[SourceOption, Field(
            description="auto=自动按平台字幕→软字幕→OCR→ASR 依次尝试；"
                        "platform=只用 B 站官方字幕；ocr=只用画面烧录字幕识别；"
                        "asr=只用本地语音转写"
        )] = "auto",
        max_images: Annotated[int | None, Field(
            description="图文模式最多生成多少张关键帧，1-200，默认取配置值"
        )] = None,
        wait: Annotated[bool, Field(
            description="true=阻塞到完成；false=立即返回 job_id，之后用 bili_job_status 查询"
        )] = True,
    ) -> dict[str, Any]:
        if mode not in MODES:
            return _tool_error("validate", f"未知 mode：{mode}")
        if subtitle_source not in SUBTITLE_SOURCES:
            return _tool_error("validate", f"未知 subtitle_source：{subtitle_source}")

        pipe = _pipeline()
        if max_images:
            pipe.cfg.max_images = max(1, min(max_images, 200))
        try:
            result = pipe.fetch(
                url_or_bvid, parts=parts, mode=mode, subtitle_source=subtitle_source
            )
        except Exception as exc:  # noqa: BLE001
            return _tool_error("materialize", str(exc))
        finally:
            pipe.close()

        payload = result.to_dict()
        payload["chapters"] = _chapter_index(payload.get("parts") or [])
        payload["read_hint"] = (
            "用 bili_read_transcript(bvid, part) 按时间段读取正文后再引用。"
        )
        return payload

    @server.tool(
        name="bili_job_status",
        title="查询任务状态",
        description="查询一次 materialize 任务的产物与状态。",
    )
    def bili_job_status(
        job_id: Annotated[str, Field(description="bili_materialize 返回的 job_id")],
    ) -> dict[str, Any]:
        pipe = _pipeline()
        jobs_dir = pipe.cfg.temp_path / "jobs"
        path = Path(jobs_dir) / f"{job_id}.json"
        if not path.is_file():
            return _tool_error("status", f"找不到任务 {job_id}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return _tool_error("status", f"任务记录损坏：{exc}")

    @server.tool(
        name="bili_read_transcript",
        title="读取转写正文",
        description=(
            "读取已生成产物的正文。默认返回章节索引与统计；"
            "给出 start_ms/end_ms 时返回该时间段的原文文本。"
            "这样可以按需精读，避免一次性把整篇塞进上下文。"
        ),
    )
    def bili_read_transcript(
        bvid: Annotated[str, Field(description="已生成产物的 BV 号")],
        part: Annotated[int, Field(description="分 P 页码，从 1 开始")] = 1,
        start_ms: Annotated[int | None, Field(
            description="起始毫秒；与 end_ms 一起用。不给就只返回章节索引与统计"
        )] = None,
        end_ms: Annotated[int | None, Field(description="结束毫秒")] = None,
    ) -> dict[str, Any]:
        pipe = _pipeline()
        try:
            base = Path(pipe.cfg.output_path) / bvid
            if not base.is_dir():
                return _tool_error("read", f"没有 {bvid} 的产物目录")
            candidates = sorted(p for p in base.iterdir() if p.is_dir())
            target = None
            for c in candidates:
                if c.name.startswith(f"P{part:02d}_"):
                    target = c
                    break
            if target is None:
                return _tool_error("read", f"{bvid} 没有 P{part} 的产物")
            manifest_path = target / "manifest.json"
            manifest = {}
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    manifest = {}
            srt = target / "transcript.srt"
            text = srt.read_text(encoding="utf-8", errors="replace") if srt.is_file() else ""

            payload: dict[str, Any] = {
                "ok": True,
                "status": "completed",
                "bvid": bvid,
                "part": part,
                "directory": str(target),
                "source_url": manifest.get("source_url"),
                "subtitle_source": manifest.get("subtitle_source"),
                "coverage": manifest.get("coverage"),
                "files": manifest.get("files") or [],
                "total_chars": len(text),
            }
            if start_ms is not None or end_ms is not None:
                payload["segments"] = _slice_segments(text, start_ms, end_ms)
            else:
                payload["hint"] = (
                    "未指定时间段，只返回元信息。需要正文请传 start_ms/end_ms，"
                    "或直接读取目录下的 transcript.md / article.md。"
                )
            return payload
        finally:
            pipe.close()

    return server


def _slice_segments(
    srt_text: str, start_ms: int | None, end_ms: int | None
) -> list[dict[str, Any]]:
    """按时间窗口切出 SRT 里的片段。"""

    from .subtitle_normalize import parse_srt

    lo = start_ms if start_ms is not None else 0
    hi = end_ms if end_ms is not None else 1 << 62
    out: list[dict[str, Any]] = []
    for seg in parse_srt(srt_text):
        if seg.end_ms < lo or seg.start_ms > hi:
            continue
        out.append({
            "start_ms": seg.start_ms,
            "end_ms": seg.end_ms,
            "text": seg.text,
        })
    return out


def _chapter_index(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 manifest 里拼出章节索引（供 Agent 定位）。"""
    index: list[dict[str, Any]] = []
    for part in parts:
        for ch in part.get("chapters") or []:
            index.append(ch)
    return index


def main() -> int:
    try:
        server = _build_server()
    except ImportError:
        print(
            "缺少 mcp 依赖，请先安装：uv pip install -e '.[mcp]'",
            file=sys.stderr,
        )
        return 2
    server.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
