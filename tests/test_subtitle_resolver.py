"""字幕轨道选择与解析（离线）。"""

from __future__ import annotations

import json

from bilibili_agent.subtitle_resolver import (
    SubtitleTrack,
    _track_rank,
    choose_track,
    list_subtitle_tracks,
    resolve_platform_subtitles,
)


class FakeClient:
    def __init__(self, tracks_by_endpoint=None, bodies=None):
        self.tracks_by_endpoint = tracks_by_endpoint or {}
        self.bodies = bodies or {}
        self.text_calls = []

    def get_json(self, url, params=None, headers=None, **kwargs):
        for endpoint, tracks in self.tracks_by_endpoint.items():
            if endpoint in url:
                return {"data": {"subtitle": {"subtitles": tracks}}}
        return {"data": {}}

    def get_text(self, url, params=None):
        self.text_calls.append(url)
        return self.bodies.get(url, json.dumps({"body": []}))


def _track(lan, ident=1, url="https://aisubtitle.hdslb.com/bbs/x.json",
           lan_doc="中文", type_=0, ai_status=None):
    return {
        "lan": lan,
        "lan_doc": lan_doc,
        "id": ident,
        "subtitle_url": url,
        "type": type_,
        "ai_status": ai_status,
    }


def test_track_rank_prefers_human_chinese_over_ai():
    human = SubtitleTrack(lan="zh-CN", lan_doc="中文", url="u1", type=0)
    ai = SubtitleTrack(lan="ai-zh", lan_doc="中文", url="u2", type=1, ai_status=2)
    assert _track_rank(human, ["zh-Hans", "zh-CN", "zh"]) < _track_rank(
        ai, ["zh-Hans", "zh-CN", "zh"]
    )


def test_track_rank_prefers_configured_language():
    en = SubtitleTrack(lan="en-US", lan_doc="English", url="u1", type=0)
    zh = SubtitleTrack(lan="zh-CN", lan_doc="中文", url="u2", type=0)
    preferred = ["zh-Hans", "zh-CN", "en-US"]
    assert _track_rank(zh, preferred) < _track_rank(en, preferred)


def test_choose_track_is_deterministic():
    tracks = [
        SubtitleTrack(lan="ai-ja", lan_doc="日", url="u1", type=1, ai_status=2),
        SubtitleTrack(lan="ai-zh", lan_doc="中", url="u2", type=1, ai_status=2),
        SubtitleTrack(lan="zh-CN", lan_doc="中文", url="u3", type=0),
    ]
    preferred = ["zh-Hans", "zh-CN", "zh"]
    first = choose_track(tracks, preferred)
    assert first is not None and first.lan == "zh-CN"
    # 顺序打乱后结果不变
    shuffled = [tracks[1], tracks[0], tracks[2]]
    again = choose_track(shuffled, preferred)
    assert again is not None and again.lan == "zh-CN"


def test_list_tracks_merges_both_endpoints():
    client = FakeClient({
        "player/wbi/v2": [_track("ai-zh", 1, "https://aisubtitle.hdslb.com/a.json")],
        "player/v2": [_track("zh-CN", 2, "https://aisubtitle.hdslb.com/b.json")],
    })
    tracks = list_subtitle_tracks("BV1", 1, client)
    lans = {t.lan for t in tracks}
    assert lans == {"ai-zh", "zh-CN"}


def test_list_tracks_dedupes_by_id():
    same = _track("zh-CN", 7)
    client = FakeClient({
        "player/wbi/v2": [same],
        "player/v2": [same],
    })
    assert len(list_subtitle_tracks("BV1", 1, client)) == 1


def test_resolve_skips_track_with_span_far_beyond_duration():
    """跨度明显超长（合并版/错配文件）的字幕轨道要被跳过。"""
    long_url = "https://aisubtitle.hdslb.com/long.json"
    good_url = "https://aisubtitle.hdslb.com/good.json"
    client = FakeClient(
        tracks_by_endpoint={
            "player/wbi/v2": [
                _track("zh-CN", 1, long_url),
                _track("ai-zh", 2, good_url, type_=1, ai_status=2),
            ],
        },
        bodies={
            long_url: json.dumps({"body": [
                {"from": 0.0, "to": 10.0, "content": "长字幕"},
                {"from": 10.0, "to": 300.0, "content": "继续"},
                {"from": 300.0, "to": 600.0, "content": "还在继续"},
            ]}),
            good_url: json.dumps({"body": [
                {"from": 0.0, "to": 10.0, "content": "短字幕"},
                {"from": 10.0, "to": 20.0, "content": "正常"},
            ]}),
        },
    )
    segments, track, warnings = resolve_platform_subtitles(
        "BV1", 1, client, duration_ms=100_000
    )
    assert track is not None and track.url == good_url
    assert len(segments) == 2
    assert any("远超视频时长" in w for w in warnings)


def test_resolve_returns_empty_when_no_tracks():
    client = FakeClient({"player/v2": []})
    segments, track, warnings = resolve_platform_subtitles("BV1", 1, client)
    assert segments == []
    assert track is None
    assert warnings
