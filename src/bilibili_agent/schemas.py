"""统一数据契约。

层与层之间只交换这里定义的 dataclass；B 站原始响应、BDL 的临时直链、
模型中间状态都不直接进入最终文档。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal

# 字幕来源分级（PLAN 第 4 章）
SubtitleSource = Literal[
    "platform_subtitle",  # B 站可下载字幕（Level 1）
    "soft_subtitle",  # 媒体文件内独立字幕轨（Level 2）
    "hard_subtitle_ocr",  # 画面烧录字幕 OCR（Level 3）
    "qwen3_asr",  # 本地语音转写（Level 4）
    "none",
]

# 生成模式
Mode = Literal["metadata", "subtitle", "illustrated", "assets", "both"]


def _clean_text(text: str) -> str:
    """去掉 B 站搜索结果里的高亮标签与多余空白。"""
    import re

    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


@dataclass(slots=True)
class SearchHit:
    """搜索候选。只包含真实接口返回的字段，缺失即 None，不凭空补。"""

    bvid: str
    title: str
    author: str | None = None
    url: str | None = None
    aid: int | None = None
    duration_s: int | None = None
    published_at: int | None = None  # unix 秒
    view_count: int | None = None
    description: str | None = None
    cover_url: str | None = None
    source: str = "bilibili_search"
    has_subtitle: bool = False
    rank_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(slots=True)
class VideoPart:
    """一个分 P。cid 是本地转换的最小单元。"""

    page: int
    cid: int
    title: str
    duration_s: int | None = None
    aid: int | None = None
    bvid: str | None = None
    cover_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(slots=True)
class VideoInfo:
    bvid: str
    title: str
    author: str | None = None
    aid: int | None = None
    duration_s: int | None = None
    cover_url: str | None = None
    published_at: int | None = None
    description: str | None = None
    parts: list[VideoPart] = field(default_factory=list)
    source_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["parts"] = [p.to_dict() for p in self.parts]
        return d


@dataclass(slots=True)
class Segment:
    """一条带时间戳的文本。"""

    start_ms: int
    end_ms: int
    text: str
    source: SubtitleSource = "platform_subtitle"
    confidence: float | None = None
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(slots=True)
class Frame:
    """一张关键帧。"""

    time_ms: int
    relative_path: str
    reason: str
    linked_segment_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(slots=True)
class Manifest:
    """每个分 P 的产物清单，输出到 manifest.json。"""

    bvid: str
    cid: int
    page: int
    source_url: str
    title: str
    author: str | None = None
    duration_ms: int | None = None
    generated_at: str = ""
    subtitle_source: SubtitleSource = "none"
    mode: Mode = "metadata"
    model: str | None = None
    pipeline_version: str = ""
    settings_digest: str = ""
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["extra"] = self.extra
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Manifest:
        known = {f.name for f in dataclasses.fields(cls)}
        payload = {k: v for k, v in data.items() if k in known}
        extra = data.get("extra") or {}
        payload["extra"] = extra
        return cls(**payload)


@dataclass(slots=True)
class Job:
    """长任务状态。"""

    id: str
    state: Literal["queued", "running", "completed", "failed", "cancelled"] = "queued"
    stage: str = ""
    progress: float | None = None
    output_path: str | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def format_timestamp(ms: int, sep: str = ".") -> str:
    """毫秒 → SRT 时间戳 ``HH:MM:SS,mmm``。"""
    if ms < 0:
        ms = 0
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{milli:03d}"


def format_clock(ms: int) -> str:
    """毫秒 → ``MM:SS`` / ``HH:MM:SS``（用于 Markdown 正文）。"""
    if ms < 0:
        ms = 0
    total = ms // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
