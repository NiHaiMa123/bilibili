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


# ---------------------------------------------------------------- T18 分页
class _PagedClient:
    """模拟多页搜索结果。"""

    def __init__(self, total: int, per_page: int = 20):
        self.total = total
        self.per_page = per_page
        self.calls: list[int] = []

    def search_videos(self, keyword, *, page=1, order="totalrank", page_size=20):
        self.calls.append(page)
        start = (page - 1) * self.per_page
        result = []
        for i in range(start, min(start + self.per_page, self.total)):
            result.append({
                "bvid": f"BV{i:010d}",
                "title": f"视频{i}",
                "author": "u",
                "duration": "10:00",
            })
        return {"result": result, "numResults": self.total}


def test_search_limit_over_one_page_uses_multiple_requests():
    from bilibili_agent.search import SearchProvider

    client = _PagedClient(total=60)
    hits = SearchProvider(client).search("测试", limit=30)
    assert len(hits) == 30
    assert client.calls == [1, 2]


def test_search_dedupes_across_pages():
    from bilibili_agent.search import SearchProvider

    class _DupClient(_PagedClient):
        def search_videos(self, keyword, *, page=1, order="totalrank", page_size=20):
            self.calls.append(page)
            # 每页都返回同样的 20 条（模拟重叠）
            return {
                "result": [
                    {"bvid": f"BV{i:010d}", "title": f"v{i}", "author": "u"}
                    for i in range(20)
                ],
                "numResults": 100,
            }

    client = _DupClient(total=100)
    hits = SearchProvider(client).search("测试", limit=30)
    # 只有 20 个唯一结果，不能造假凑数
    assert len(hits) == 20
    assert len({h.bvid for h in hits}) == 20


def test_search_stops_at_last_page():
    from bilibili_agent.search import SearchProvider

    client = _PagedClient(total=25)
    hits = SearchProvider(client).search("测试", limit=50)
    assert len(hits) == 25
    assert client.calls == [1, 2]


def test_search_respects_max_requests():
    from bilibili_agent.search import SearchProvider

    client = _PagedClient(total=1000)
    hits = SearchProvider(client).search("测试", limit=50, max_requests=2)
    assert len(client.calls) <= 2
    assert len(hits) <= 40


def test_search_propagates_api_error_without_retry_loop():
    import pytest

    from bilibili_agent.bili_api import BiliApiError
    from bilibili_agent.search import SearchProvider

    class _ErrClient:
        def __init__(self):
            self.calls = 0

        def search_videos(self, keyword, **kwargs):
            self.calls += 1
            raise BiliApiError("接口错误 code=-412", code=-412)

    client = _ErrClient()
    with pytest.raises(BiliApiError):
        SearchProvider(client).search("测试", limit=30)
    assert client.calls == 1
