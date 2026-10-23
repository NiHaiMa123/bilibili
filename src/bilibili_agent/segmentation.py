"""章节切分。

规则式切分：长停顿 + 目标时长上限。不依赖 LLM，也不改写原文。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schemas import Segment


@dataclass(slots=True)
class Chapter:
    index: int
    start_ms: int
    end_ms: int
    segment_ids: list[int] = field(default_factory=list)
    title: str = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "segment_ids": self.segment_ids,
            "title": self.title,
        }


def _title_from_text(text: str, limit: int = 28) -> str:
    """用章节首句的前若干字做标题（原文摘录，不生成新描述）。"""
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "…"


def segment_chapters(
    segments: list[Segment],
    *,
    gap_threshold_ms: int = 2000,
    target_chapter_ms: int = 120_000,
    max_chapter_ms: int = 300_000,
) -> list[Chapter]:
    """把时间轴切成章节。

    切分点：与上一句间隔超过 ``gap_threshold_ms``，或本章已达
    ``target_chapter_ms``；超过 ``max_chapter_ms`` 强制断开。
    """
    if not segments:
        return []

    chapters: list[Chapter] = []
    current: list[Segment] = []
    chapter_start = segments[0].start_ms

    def flush() -> None:
        nonlocal current
        if not current:
            return
        chapters.append(
            Chapter(
                index=len(chapters) + 1,
                start_ms=current[0].start_ms,
                end_ms=current[-1].end_ms,
                segment_ids=[],
                title=_title_from_text(current[0].text),
            )
        )
        current = []

    last_end: int | None = None
    for seg in segments:
        if last_end is not None:
            gap = seg.start_ms - last_end
            chapter_len = seg.end_ms - chapter_start
            if gap >= gap_threshold_ms or chapter_len >= target_chapter_ms:
                flush()
                chapter_start = seg.start_ms
        current.append(seg)
        last_end = seg.end_ms
        if seg.end_ms - chapter_start >= max_chapter_ms:
            flush()
            chapter_start = seg.end_ms
    flush()

    # 回填 segment 索引（章节覆盖的 segment 下标）
    for chapter in chapters:
        for si, seg in enumerate(segments):
            if seg.end_ms <= chapter.start_ms:
                continue
            if seg.start_ms > chapter.end_ms:
                break
            chapter.segment_ids.append(si)
    return chapters
