"""流水线编排（离线：mock 掉 BDL 与 B 站接口）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bilibili_agent.bili_api import BiliApiError
from bilibili_agent.config import Config
from bilibili_agent.pipeline import Pipeline


class FakeClient:
    """只实现流水线用到的方法。"""

    def __init__(self, subtitle_payload=None, has_subtitles=True):
        self.subtitle_payload = subtitle_payload
        self.has_subtitles = has_subtitles

    def close(self):
        pass

    def get_json(self, url, params=None, headers=None, **kwargs):
        # 字幕清单：wbi/v2 与 v2 两个接口都要能返回
        if "player" in url:
            if not self.has_subtitles:
                return {"data": {"subtitle": {"subtitles": []}}}
            return {
                "data": {
                    "subtitle": {
                        "subtitles": [
                            {"lan": "zh-CN", "lan_doc": "中文（中国）", "id": 1,
                             "subtitle_url": "https://aisubtitle.hdslb.com/bbs/x.json",
                             "type": 0},
                        ]
                    }
                }
            }
        if "web-interface/view" in url:
            return {"data": self.view(params.get("bvid"))}
        raise AssertionError(f"未预期的接口调用：{url}")

    def player_v2(self, bvid, cid):
        return self.get_json("https://api.bilibili.com/x/player/v2")

    def get_text(self, url, params=None, **kwargs):
        if self.subtitle_payload is None:
            return json.dumps({"body": []})
        return json.dumps(self.subtitle_payload)

    def view(self, bvid):
        return {
            "bvid": bvid, "aid": 2, "title": "测试标题", "desc": "简介",
            "duration": 120, "pubdate": 1700000000, "pic": "https://x/c.jpg",
            "owner": {"name": "UP主", "mid": 2},
            "pages": [{"cid": 62131, "page": 1, "part": "P1", "duration": 120}],
        }

    def pagelist(self, bvid):
        return [{"cid": 62131, "page": 1, "part": "P1", "duration": 120}]

    def search_videos(self, keyword, **kwargs):
        return {"result": [], "numResults": 0}


class FakeAdapter:
    available = True

    def __init__(self, tree=None):
        self.tree = tree or {}

    def parse(self, target):
        return self.tree

    def download(self, *args, **kwargs):
        raise AssertionError("本测试不应触发下载")


@pytest.fixture()
def pipeline(tmp_path: Path):
    cfg = Config(root=tmp_path, config_path=None)
    cfg.output_dir = str(tmp_path / "output")
    cfg.media_cache = str(tmp_path / "media")
    cfg.temp_dir = str(tmp_path / "tmp")
    pipe = Pipeline(cfg)
    yield pipe
    pipe.close()


def _install(pipe, client, adapter=None):
    pipe.client = client
    pipe.adapter = adapter or FakeAdapter()


def test_fetch_metadata_mode(pipeline):
    _install(pipeline, FakeClient())
    result = pipeline.fetch("BV1xx411c7mD", mode="metadata")
    assert result.ok
    assert result.bvid == "BV1xx411c7mD"
    assert result.parts[0]["cid"] == 62131


def test_fetch_subtitle_mode_writes_artifacts(pipeline):
    payload = {"body": [
        {"from": 0.0, "to": 3.0, "content": "第一句"},
        {"from": 3.0, "to": 6.0, "content": "第二句"},
        {"from": 6.0, "to": 9.0, "content": "第三句"},
    ]}
    _install(pipeline, FakeClient(subtitle_payload=payload))
    result = pipeline.fetch("BV1xx411c7mD", mode="subtitle")
    assert result.ok, result.to_dict()
    part = result.parts[0]
    assert part["subtitle_source"] == "platform_subtitle"
    out_dir = Path(part["directory"])
    assert (out_dir / "transcript.md").is_file()
    assert (out_dir / "transcript.srt").is_file()
    assert (out_dir / "manifest.json").is_file()
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["subtitle_source"] == "platform_subtitle"
    assert manifest["coverage"]["segments"] == 3


def test_fetch_is_idempotent(pipeline):
    payload = {"body": [
        {"from": 0.0, "to": 3.0, "content": "一"},
        {"from": 3.0, "to": 6.0, "content": "二"},
        {"from": 6.0, "to": 9.0, "content": "三"},
    ]}
    _install(pipeline, FakeClient(subtitle_payload=payload))
    first = pipeline.fetch("BV1xx411c7mD", mode="subtitle")
    second = pipeline.fetch("BV1xx411c7mD", mode="subtitle")
    assert first.ok and second.ok
    assert second.parts[0].get("cached") is True


def test_fetch_without_subtitles_fails_explicitly(pipeline):
    """显式只要平台字幕时，没有字幕必须明确失败，不能产出空文档。"""
    _install(pipeline, FakeClient(has_subtitles=False))
    result = pipeline.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="platform")
    assert not result.ok
    part = result.parts[0]
    assert part["error"]["stage"] == "subtitle"
    assert part["subtitle_source"] == "none"


def test_fetch_rejects_unknown_mode(pipeline):
    _install(pipeline, FakeClient())
    result = pipeline.fetch("BV1xx411c7mD", mode="nope")
    assert not result.ok
    assert result.error["stage"] == "validate"


def test_fetch_rejects_unknown_subtitle_source(pipeline):
    _install(pipeline, FakeClient())
    result = pipeline.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="magic")
    assert not result.ok
    assert result.error["stage"] == "validate"


def test_fetch_unknown_bvid_reports_error(pipeline):
    class Broken(FakeClient):
        def view(self, bvid):
            raise BiliApiError("接口错误 code=-404", code=-404)

    _install(pipeline, Broken())
    result = pipeline.fetch("BV1notexist000")
    assert not result.ok
    assert result.error is not None
