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
    assert stats["coverage_ratio"] == 0.1
