"""字幕归一化与 SRT 读写。"""

from __future__ import annotations

from bilibili_agent.subtitle_normalize import (
    coverage_stats,
    parse_bilibili_subtitle,
    parse_srt,
    to_srt,
    validate_segments,
)


def test_parse_dict_body():
    payload = {
        "body": [
            {"from": 0.0, "to": 3.5, "content": "第一句"},
            {"from": 3.5, "to": 8.25, "content": "第二句"},
        ]
    }
    segs = parse_bilibili_subtitle(payload)
    assert len(segs) == 2
    assert segs[0].start_ms == 0
    assert segs[1].end_ms == 8250
    assert segs[0].source == "platform_subtitle"


def test_parse_bare_list_body():
    payload = [{"from": 1.0, "to": 2.0, "content": "裸数组"}]
    segs = parse_bilibili_subtitle(payload)
    assert len(segs) == 1
    assert segs[0].text == "裸数组"


def test_parse_ignores_bad_entries():
    payload = {
        "body": [
            {"from": 0.0, "to": 1.0, "content": "好的"},
            {"from": "x", "to": 2.0, "content": "坏时间"},
            {"from": 2.0, "to": 3.0},
            {"from": 3.0, "to": 4.0, "content": "   "},
        ]
    }
    segs = parse_bilibili_subtitle(payload)
    assert [s.text for s in segs] == ["好的"]


def test_validate_sorts_and_fixes_inverted():
    segs = parse_bilibili_subtitle([
        {"from": 5.0, "to": 6.0, "content": "后"},
        {"from": 0.0, "to": 2.0, "content": "前"},
    ])
    segs[0].end_ms = 1000  # 制造倒挂
    out, issues = validate_segments(segs, min_segments=1)
    assert out[0].start_ms == 0
    assert out[0].end_ms >= out[0].start_ms
    assert out[1].start_ms == 5000


def test_validate_drops_duplicate_adjacent():
    segs = parse_bilibili_subtitle([
        {"from": 0.0, "to": 2.0, "content": "重复"},
        {"from": 0.05, "to": 2.0, "content": "重复"},
        {"from": 3.0, "to": 4.0, "content": "不同"},
    ])
    out, issues = validate_segments(segs, min_segments=1)
    assert len(out) == 2
    assert any("重复" in i for i in issues)


def test_validate_reports_too_few():
    segs = parse_bilibili_subtitle([{"from": 0.0, "to": 1.0, "content": "孤句"}])
    _, issues = validate_segments(segs, min_segments=3)
    assert any("片段过少" in i for i in issues)


def test_music_only_segment_dropped():
    segs = parse_bilibili_subtitle([{"from": 0.0, "to": 1.0, "content": "♪♪♪"}])
    out, _ = validate_segments(segs, min_segments=1)
    assert out == []


def test_srt_roundtrip():
    segs = parse_bilibili_subtitle([
        {"from": 0.0, "to": 3.5, "content": "第一句"},
        {"from": 3.5, "to": 8.25, "content": "第二句"},
    ])
    srt = to_srt(segs)
    assert "00:00:00,000 --> 00:00:03,500" in srt
    back = parse_srt(srt)
    assert len(back) == 2
    assert back[1].text == "第二句"
    assert back[1].start_ms == 3500


def test_coverage_stats():
    segs = parse_bilibili_subtitle([
        {"from": 0.0, "to": 10.0, "content": "0123456789"},
    ])
    stats = coverage_stats(segs, 100_000)
    assert stats["segments"] == 1
    assert stats["chars"] == 10
    assert stats["segment_time_coverage"] == 0.1
    assert stats["span_ratio"] == 0.1


def test_coverage_uses_time_union_not_span():
    """10 分钟视频里只有 3 秒字幕 → 覆盖率必须很低，不能接近 100%。"""
    segs = parse_bilibili_subtitle([
        {"from": 0.0, "to": 1.0, "content": "一"},
        {"from": 300.0, "to": 301.0, "content": "二"},
        {"from": 599.0, "to": 600.0, "content": "三"},
    ])
    stats = coverage_stats(segs, 600_000)
    # 时间并集 = 3 秒 / 600 秒 = 0.5%
    assert stats["segment_time_coverage"] == 0.005
    # 首尾跨度仍然很大，但它是 span_ratio，不能冒充覆盖率
    assert stats["span_ratio"] == 1.0
    assert stats["segment_time_coverage"] < stats["span_ratio"]


def test_coverage_merges_overlapping_segments():
    segs = parse_bilibili_subtitle([
        {"from": 0.0, "to": 5.0, "content": "一"},
        {"from": 3.0, "to": 9.0, "content": "二"},
    ])
    stats = coverage_stats(segs, 10_000)
    # 重叠部分不重复计算 → 0~9s = 9s
    assert stats["covered_ms"] == 9000
    assert stats["segment_time_coverage"] == 0.9
