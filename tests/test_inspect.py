"""inspect 解析（用真实 BDL parse 响应做夹具，不联网）。"""

from __future__ import annotations

import json
from pathlib import Path

from bilibili_agent.inspect import (
    _parts_from_bdl,
    extract_bvid,
    extract_page,
)

FIXTURE = Path(__file__).parent / "fixtures" / "bdl_parse_video.json"


def _tree():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_extract_bvid_from_plain():
    assert extract_bvid("BV1xx411c7mD") == "BV1xx411c7mD"


def test_extract_bvid_from_url():
    url = "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
    assert extract_bvid(url) == "BV1xx411c7mD"


def test_extract_bvid_returns_none_for_av():
    assert extract_bvid("av12345") is None
    assert extract_bvid("") is None


def test_extract_page():
    assert extract_page("https://www.bilibili.com/video/BV1xx411c7mD?p=3") == 3
    assert extract_page("BV1xx411c7mD") is None


def test_parts_from_real_bdl_tree():
    parts = _parts_from_bdl(_tree())
    assert len(parts) == 1
    p = parts[0]
    assert p.page == 1
    assert p.cid == 62131
    assert p.duration_s == 2055
    assert p.bvid == "BV1xx411c7mD"


def test_parts_from_empty_tree():
    assert _parts_from_bdl({}) == []
