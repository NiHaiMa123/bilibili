"""Markdown 与 manifest 写出。"""

from __future__ import annotations

import json

from bilibili_agent.md_writer import (
    write_article_md,
    write_manifest,
    write_transcript_md,
)
from bilibili_agent.schemas import Frame, Segment, VideoInfo, VideoPart
from bilibili_agent.segmentation import segment_chapters


def _video():
    return VideoInfo(
        bvid="BV1xx411c7mD",
        title="测试标题",
        author="UP主",
        duration_s=120,
        cover_url="https://example.invalid/c.jpg",
        published_at=1700000000,
        description="视频简介",
        parts=[VideoPart(page=1, cid=62131, title="P1", duration_s=120)],
        source_url="https://www.bilibili.com/video/BV1xx411c7mD",
    )


def _segments():
    return [
        Segment(start_ms=0, end_ms=3000, text="开场白"),
        Segment(start_ms=3000, end_ms=8000, text="第一步操作"),
        Segment(start_ms=8000, end_ms=15000, text="第二步操作"),
    ]


def test_transcript_md_contains_metadata_and_timeline(tmp_path):
    out = tmp_path / "transcript.md"
    write_transcript_md(out, _video(), _video().parts[0], _segments(),
                        subtitle_source="platform_subtitle")
    text = out.read_text(encoding="utf-8")
    assert "# 测试标题" in text
    assert "https://www.bilibili.com/video/BV1xx411c7mD" in text
    assert "UP主：UP主" in text
    assert "platform_subtitle" in text
    assert "[00:00] 开场白" in text
    assert "[00:08] 第二步操作" in text


def test_transcript_md_marks_empty(tmp_path):
    out = tmp_path / "t.md"
    write_transcript_md(out, _video(), _video().parts[0], [], subtitle_source="none")
    text = out.read_text(encoding="utf-8")
    assert "没有可用的字幕" in text


def test_article_md_embeds_relative_frames(tmp_path):
    frames = [
        Frame(time_ms=3200, relative_path="assets/frame_0003200.jpg", reason="scene_change"),
        Frame(time_ms=9000, relative_path="assets/frame_0009000.jpg", reason="transcript_anchor"),
    ]
    out = tmp_path / "article.md"
    write_article_md(out, _video(), _video().parts[0], _segments(), frames,
                     subtitle_source="qwen3_asr")
    text = out.read_text(encoding="utf-8")
    assert "![00:03 关键帧](assets/frame_0003200.jpg)" in text
    assert "![00:09 关键帧](assets/frame_0009000.jpg)" in text
    assert "http" not in text.split("assets/")[0].split("## ")[-1]


def test_segment_chapters_splits_on_gap():
    segs = [
        Segment(start_ms=0, end_ms=1000, text="一"),
        Segment(start_ms=1000, end_ms=2000, text="二"),
        Segment(start_ms=90000, end_ms=91000, text="三"),
    ]
    chapters = segment_chapters(segs, gap_threshold_ms=2000)
    assert len(chapters) == 2
    assert chapters[0].start_ms == 0
    assert chapters[1].start_ms == 90000
    assert chapters[0].segment_ids == [0, 1]


def test_manifest_json_roundtrip(tmp_path):
    path = tmp_path / "manifest.json"
    payload = {
        "bvid": "BV1xx411c7mD", "cid": 62131, "page": 1,
        "source_url": "https://www.bilibili.com/video/BV1xx411c7mD",
        "title": "测试", "author": "UP主", "duration_ms": 120000,
        "generated_at": "2026-10-23T12:00:00+08:00",
        "subtitle_source": "qwen3_asr", "mode": "subtitle",
        "pipeline_version": "0.1.0", "settings_digest": "abc",
        "files": ["transcript.md"], "warnings": [],
    }
    write_manifest(path, payload)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["subtitle_source"] == "qwen3_asr"
    assert loaded["files"] == ["transcript.md"]
