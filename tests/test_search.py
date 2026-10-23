"""搜索结果解析（纯离线，不联网）。"""

from __future__ import annotations

from bilibili_agent.search import hit_from_raw


def _raw(**over):
    base = {
        "type": "video",
        "bvid": "BV1xx411c7mD",
        "aid": 2,
        "title": "字幕<em class=\"keyword\">君</em>交流场所",
        "description": "一个<em class=\"keyword\">测试</em>视频",
        "author": "碧诗",
        "pic": "//i0.hdslb.com/bfs/archive/x.jpg",
        "play": 5551537,
        "pubdate": 1252458549,
        "duration": "34:15",
    }
    base.update(over)
    return base


def test_strips_highlight_tags():
    hit = hit_from_raw(_raw())
    assert hit is not None
    assert hit.title == "字幕君交流场所"
    assert hit.description == "一个测试视频"


def test_parses_duration_formats():
    assert hit_from_raw(_raw(duration="12:34")).duration_s == 754
    assert hit_from_raw(_raw(duration="1:02:03")).duration_s == 3723
    assert hit_from_raw(_raw(duration="90")).duration_s == 90
    assert hit_from_raw(_raw(duration=None)).duration_s is None


def test_normalizes_cover_url():
    hit = hit_from_raw(_raw())
    assert hit.cover_url == "https://i0.hdslb.com/bfs/archive/x.jpg"


def test_missing_bvid_returns_none():
    assert hit_from_raw(_raw(bvid="")) is None
    assert hit_from_raw(_raw(title="")) is None


def test_missing_fields_stay_none():
    hit = hit_from_raw(_raw(author="", pubdate=None, play=None, pic=None))
    assert hit.author is None
    assert hit.published_at is None
    assert hit.view_count is None
    assert hit.cover_url is None


def test_subtitle_field_is_description_not_flag():
    """B 站搜索结果里的 subtitle 字段其实是简介，不能当成"有字幕"标记。"""
    hit = hit_from_raw(_raw(subtitle="这是一条简介"))
    assert hit.has_subtitle is False


def test_url_is_derived_from_bvid():
    hit = hit_from_raw(_raw())
    assert hit.url == "https://www.bilibili.com/video/BV1xx411c7mD"
