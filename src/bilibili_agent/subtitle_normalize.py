"""字幕归一化：B 站 JSON → 统一 Segment，并做质量校验与 SRT 输出。"""

from __future__ import annotations

import json
import re
from typing import Any

from .schemas import Segment, format_timestamp

# 只有音乐符号/表情/标点的片段，视为无信息量
_ONLY_MUSIC_RE = re.compile(r"^[\s♪♫♬\-—…·•]+$")
_WS_RE = re.compile(r"\s+")


def parse_bilibili_subtitle(payload: Any) -> list[Segment]:
    """解析 B 站字幕正文。

    支持两种形态：``{"body": [...]}`` 与直接 ``[...]``。
    每项含 ``from``/``to``（秒）与 ``content``。
    """
    if isinstance(payload, dict):
        body = payload.get("body")
    elif isinstance(payload, list):
        body = payload
    else:
        body = None
    if not isinstance(body, list):
        return []

    segments: list[Segment] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        start = item.get("from", item.get("start"))
        end = item.get("to", item.get("end"))
        content = item.get("content")
        if content is None:
            content = item.get("text")
        if start is None or end is None or not isinstance(content, str):
            continue
        try:
            start_ms = int(round(float(start) * 1000))
            end_ms = int(round(float(end) * 1000))
        except (TypeError, ValueError):
            continue
        text = _WS_RE.sub(" ", content).strip()
        if not text:
            continue
        segments.append(
            Segment(
                start_ms=max(0, start_ms),
                end_ms=max(0, end_ms),
                text=text,
                source="platform_subtitle",
            )
        )
    return segments


def validate_segments(
    segments: list[Segment],
    *,
    min_segments: int = 3,
) -> tuple[list[Segment], list[str]]:
    """基本质量检查：排序、去重、丢弃无效片段、修正倒挂时间。

    返回 ``(segments, issues)``；issues 是可读的诊断信息。
    """
    issues: list[str] = []
    if not segments:
        return [], ["字幕为空"]

    cleaned: list[Segment] = []
    for seg in segments:
        if seg.end_ms < seg.start_ms:
            seg.end_ms = seg.start_ms
        if _ONLY_MUSIC_RE.match(seg.text or ""):
            continue
        cleaned.append(seg)

    cleaned.sort(key=lambda s: (s.start_ms, s.end_ms))

    # 去掉完全重复的相邻片段
    deduped: list[Segment] = []
    for seg in cleaned:
        if deduped:
            prev = deduped[-1]
            if (
                prev.text == seg.text
                and abs(prev.start_ms - seg.start_ms) < 200
                and abs(prev.end_ms - seg.end_ms) < 200
            ):
                prev.end_ms = max(prev.end_ms, seg.end_ms)
                continue
        deduped.append(seg)

    if len(deduped) < len(cleaned):
        issues.append(f"合并了 {len(cleaned) - len(deduped)} 条重复字幕")

    if len(deduped) < min_segments:
        issues.append(f"字幕片段过少（{len(deduped)} < {min_segments}）")

    # 时间轴倒退检查
    for prev, nxt in zip(deduped, deduped[1:], strict=False):
        if nxt.start_ms < prev.start_ms:
            issues.append("检测到时间轴倒退，已按时间排序")
            break

    if deduped:
        span = deduped[-1].end_ms - deduped[0].start_ms
        if span <= 0:
            issues.append("字幕时间跨度为 0")

    return deduped, issues


def to_srt(segments: list[Segment]) -> str:
    """生成 SRT 文本。"""
    blocks: list[str] = []
    for idx, seg in enumerate(segments, start=1):
        blocks.append(
            f"{idx}\n"
            f"{format_timestamp(seg.start_ms, ',')} --> {format_timestamp(seg.end_ms, ',')}\n"
            f"{seg.text}\n"
        )
    return "\n".join(blocks)


def to_vtt(segments: list[Segment]) -> str:
    """生成 WebVTT 文本。"""
    blocks = ["WEBVTT", ""]
    for seg in segments:
        blocks.append(
            f"{format_timestamp(seg.start_ms, '.')} --> {format_timestamp(seg.end_ms, '.')}\n"
            f"{seg.text}\n"
        )
    return "\n".join(blocks)


_SRT_TIME_RE = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def parse_srt(text: str) -> list[Segment]:
    """解析 SRT 文本（Level 2 软字幕提取后使用）。"""
    segments: list[Segment] = []
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    idx = 0
    total = len(lines)
    while idx < total:
        line = lines[idx].strip()
        m = _SRT_TIME_RE.search(line)
        if not m:
            idx += 1
            continue
        g = m.groups()
        start_ms = (int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2])) * 1000 + int(g[3].ljust(3, "0"))
        end_ms = (int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6])) * 1000 + int(g[7].ljust(3, "0"))
        idx += 1
        buf: list[str] = []
        while idx < total and lines[idx].strip():
            buf.append(lines[idx].strip())
            idx += 1
        content = " ".join(buf).strip()
        if content:
            segments.append(
                Segment(start_ms=start_ms, end_ms=end_ms, text=content,
                        source="soft_subtitle")
            )
    return segments


def coverage_stats(segments: list[Segment], duration_ms: int | None) -> dict[str, Any]:
    """字幕覆盖度统计（用于 manifest 与质量度量）。

    B 站 AI 字幕偶尔会带上超出视频时长的片段（例如该 P 实际时长 377s，
    字幕却覆盖到 756s）。这种情况要**如实标记**而不是让覆盖率超过 1。
    """
    if not segments:
        return {
            "segments": 0,
            "span_ms": 0,
            "coverage_ratio": 0.0,
            "chars": 0,
            "chars_per_minute": 0.0,
        }
    span = segments[-1].end_ms - segments[0].start_ms
    chars = sum(len(s.text) for s in segments)
    minutes = max(span, 1) / 60000
    raw_ratio = (span / duration_ms) if duration_ms else None
    exceeds = bool(raw_ratio is not None and raw_ratio > 1.1)
    ratio = None
    if raw_ratio is not None:
        ratio = min(raw_ratio, 1.0)
    return {
        "segments": len(segments),
        "span_ms": span,
        "coverage_ratio": round(ratio, 4) if ratio is not None else None,
        "raw_coverage_ratio": round(raw_ratio, 4) if raw_ratio is not None else None,
        "span_exceeds_duration": exceeds,
        "chars": chars,
        "chars_per_minute": round(chars / minutes, 2),
    }


def dump_segments_json(segments: list[Segment]) -> str:
    return json.dumps([s.to_dict() for s in segments], ensure_ascii=False, indent=2)
