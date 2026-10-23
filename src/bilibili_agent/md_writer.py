"""产物写出：transcript.md / article.md / manifest.json。

Markdown 里只放讲者原文与元信息；任何摘要都不冒充原视频说过的话。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .schemas import Frame, Segment, VideoInfo, format_clock
from .segmentation import Chapter

SOURCE_LABELS = {
    "platform_subtitle": "platform_subtitle（B 站官方/AI 字幕）",
    "soft_subtitle": "soft_subtitle（媒体文件内独立字幕轨）",
    "hard_subtitle_ocr": "hard_subtitle_ocr（画面烧录字幕 OCR）",
    "qwen3_asr": "qwen3_asr（本地语音转写）",
    "none": "none",
}


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _header_lines(
    video: VideoInfo,
    part,
    *,
    mode: str,
    subtitle_source: str,
    generated_at: str,
    extra: dict[str, Any] | None = None,
) -> list[str]:
    lines = [
        f"# {video.title}",
        "",
        f"- 来源：{video.source_url}"
        + (f"?p={part.page}" if part and part.page > 1 else ""),
        f"- UP主：{video.author or '未知'}",
        f"- 分P：P{part.page} / {part.title}" if part else "- 分P：P1",
        f"- 时长：{format_clock(part.duration_s * 1000) if part and part.duration_s else '未知'}",
        f"- 字幕来源：{SOURCE_LABELS.get(subtitle_source, subtitle_source)}",
        f"- 生成模式：{mode}",
        f"- 生成时间：{generated_at}",
        "- 说明：转写/识别可能有误，具体以原视频为准。",
    ]
    if video.description:
        desc = " ".join(video.description.split())
        if len(desc) > 300:
            desc = desc[:300] + "…"
        lines.append(f"- 简介：{desc}")
    for k, v in (extra or {}).items():
        lines.append(f"- {k}：{v}")
    lines.append("")
    return lines


def write_transcript_md(
    path: Path,
    video: VideoInfo,
    part,
    segments: list[Segment],
    *,
    subtitle_source: str,
    mode: str = "subtitle",
    generated_at: str | None = None,
    chapters: list[Chapter] | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """写出完整时间轴文本（可按时间戳搜索）。"""
    generated_at = generated_at or _now_iso()
    lines = _header_lines(
        video, part, mode=mode, subtitle_source=subtitle_source,
        generated_at=generated_at, extra=extra,
    )

    if chapters:
        lines.append("## 章节索引")
        lines.append("")
        for ch in chapters:
            lines.append(
                f"- [{format_clock(ch.start_ms)}–{format_clock(ch.end_ms)}] {ch.title}"
            )
        lines.append("")

    lines.append("## 全片时间轴")
    lines.append("")
    if not segments:
        lines.append("_没有可用的字幕/转写内容。_")
        lines.append("")
    for seg in segments:
        lines.append(f"[{format_clock(seg.start_ms)}] {seg.text}")
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_article_md(
    path: Path,
    video: VideoInfo,
    part,
    segments: list[Segment],
    frames: list[Frame],
    *,
    subtitle_source: str,
    mode: str = "illustrated",
    generated_at: str | None = None,
    chapters: list[Chapter] | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """写出按章节组织的图文稿。图片使用相对路径。"""
    generated_at = generated_at or _now_iso()
    lines = _header_lines(
        video, part, mode=mode, subtitle_source=subtitle_source,
        generated_at=generated_at, extra=extra,
    )

    if chapters is None:
        from .segmentation import segment_chapters

        chapters = segment_chapters(segments)

    if not frames:
        lines.append("> 本次没有产出关键帧，仅包含文字时间轴。")
        lines.append("")

    for ch in chapters:
        lines.append(f"## [{format_clock(ch.start_ms)}–{format_clock(ch.end_ms)}] {ch.title}")
        lines.append("")
        # 本章的关键帧，按时间排序
        ch_frames = sorted(
            (f for f in frames if ch.start_ms <= f.time_ms <= ch.end_ms),
            key=lambda f: f.time_ms,
        )
        frame_idx = 0

        for i in ch.segment_ids:
            if i < 0 or i >= len(segments):
                continue
            seg = segments[i]
            # 关键帧插到"它之后第一句话"之前，保证图与讲解顺序一致
            while frame_idx < len(ch_frames) and ch_frames[frame_idx].time_ms <= seg.start_ms:
                f = ch_frames[frame_idx]
                lines.append(f"![{format_clock(f.time_ms)} 关键帧]({f.relative_path})")
                lines.append("")
                frame_idx += 1
            lines.append(f"[{format_clock(seg.start_ms)}] {seg.text}")

        # 本章剩余的帧（都晚于最后一句）补在章末
        while frame_idx < len(ch_frames):
            f = ch_frames[frame_idx]
            lines.append(f"![{format_clock(f.time_ms)} 关键帧]({f.relative_path})")
            lines.append("")
            frame_idx += 1
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_manifest(path: Path, manifest: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def write_srt(path: Path, srt_text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(srt_text, encoding="utf-8")
    return path
