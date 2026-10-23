"""缓存组件化（T14）与 MCP 章节索引（T16）。"""

from __future__ import annotations

import json
from pathlib import Path

from test_routing import Harness, _segs


# ---------------------------------------------------------------- T14 组件缓存
def test_max_images_change_invalidates_frames_only(tmp_path):
    """改 max_images 只让 frames 组件失效，字幕应当被复用。"""
    h = Harness(tmp_path, platform_segments=_segs(), frame_count=2)
    h.cfg.max_images = 5
    first = h.fetch("BV1xx411c7mD", mode="illustrated", subtitle_source="platform")
    assert first.ok
    out = Path(first.parts[0]["directory"])
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["components"]["transcript"]["digest"]
    assert manifest["components"]["frames"]["digest"]

    # 改 max_images → frames 摘要变了，transcript 摘要不变
    h2 = Harness(tmp_path, platform_segments=_segs(), frame_count=2)
    h2.cfg.max_images = 9
    second = h2.fetch("BV1xx411c7mD", mode="illustrated", subtitle_source="platform")
    assert second.ok
    manifest2 = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest2["components"]["frames"]["digest"] != manifest["components"]["frames"]["digest"]
    assert (manifest2["components"]["transcript"]["digest"]
            == manifest["components"]["transcript"]["digest"])
    # 字幕被复用，没有重新去取
    assert manifest2["components"]["transcript"]["reused"] is True
    # 平台字幕只被取过一次
    assert h2.calls.count("platform") == 0


def test_ocr_param_change_invalidates_transcript(tmp_path):
    """改 OCR 采样率 → transcript 组件失效，需要重新取字幕。"""
    h = Harness(tmp_path, platform_segments=_segs())
    h.cfg.ocr_sample_interval = 1.0
    first = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="platform")
    assert first.ok
    out = Path(first.parts[0]["directory"])
    m1 = json.loads((out / "manifest.json").read_text(encoding="utf-8"))

    h2 = Harness(tmp_path, platform_segments=_segs())
    h2.cfg.ocr_sample_interval = 2.5
    second = h2.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="platform")
    assert second.ok
    m2 = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert (m2["components"]["transcript"]["digest"]
            != m1["components"]["transcript"]["digest"])
    # 字幕确实重新取过
    assert h2.calls.count("platform") == 1


def test_asr_model_change_invalidates_transcript(tmp_path):
    """换 ASR 模型 → 不能复用旧转写。"""
    h = Harness(tmp_path, asr_segments=_segs())
    h.cfg.asr_model_path = "/models/asr-a"
    first = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="asr")
    assert first.ok
    out = Path(first.parts[0]["directory"])
    m1 = json.loads((out / "manifest.json").read_text(encoding="utf-8"))

    h2 = Harness(tmp_path, asr_segments=_segs())
    h2.cfg.asr_model_path = "/models/asr-b"
    second = h2.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="asr")
    assert second.ok
    m2 = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert (m2["components"]["transcript"]["digest"]
            != m1["components"]["transcript"]["digest"])


def test_subtitle_to_illustrated_reuses_transcript(tmp_path):
    """subtitle → illustrated 升级：字幕复用，只跑抽帧。"""
    h = Harness(tmp_path, platform_segments=_segs(), frame_count=2)
    sub = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="platform")
    assert sub.ok
    out = Path(sub.parts[0]["directory"])

    h2 = Harness(tmp_path, platform_segments=_segs(), frame_count=2)
    ill = h2.fetch("BV1xx411c7mD", mode="illustrated", subtitle_source="platform")
    assert ill.ok
    assert (out / "article.md").is_file()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["components"]["transcript"]["reused"] is True
    # 升级时没有重新去取平台字幕
    assert h2.calls.count("platform") == 0


def test_failed_run_is_not_reusable(tmp_path):
    h = Harness(tmp_path, platform_segments=None, frames_ok=False)
    res = h.fetch("BV1xx411c7mD", mode="illustrated", subtitle_source="platform")
    assert not res.ok
    out = Path(res.parts[0]["directory"])
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"

    # 再次运行不应命中缓存（会重新尝试）
    h2 = Harness(tmp_path, platform_segments=_segs(), frames_ok=False)
    h2.fetch("BV1xx411c7mD", mode="illustrated", subtitle_source="platform")
    assert h2.calls.count("platform") == 1


def test_manifest_write_is_atomic(tmp_path):
    """manifest 不能留下临时文件（原子替换）。"""
    h = Harness(tmp_path, platform_segments=_segs())
    h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="platform")
    out = Path(h.cfg.output_path) / "BV1xx411c7mD"
    assert not list(out.rglob("*.tmp"))


# ---------------------------------------------------------------- T16 MCP 章节
def _mcp_server(monkeypatch, pipe):
    import bilibili_agent.mcp_server as mcp_mod

    monkeypatch.setattr(mcp_mod, "_pipeline", lambda: pipe)
    return mcp_mod._build_server()


def test_mcp_materialize_returns_chapters(tmp_path, monkeypatch):
    import asyncio


    class _Cfg:
        temp_path = tmp_path / "tmp"
        config_path = None
        max_images = 25

    class _Pipe:
        def __init__(self):
            self.cfg = _Cfg()

        def fetch(self, *a, **kw):
            return _Result()

        def close(self):
            pass

    class _Result:
        def to_dict(self):
            return {
                "ok": True, "status": "completed", "bvid": "BV1",
                "outputs": ["transcript.md"], "warnings": [],
                "parts": [
                    {"page": 1, "cid": 1, "ok": True,
                     "chapters": [
                         {"index": 1, "start_ms": 0, "end_ms": 5000, "title": "第一段"},
                         {"index": 2, "start_ms": 5000, "end_ms": 9000, "title": "第二段"},
                     ]},
                ],
            }

    server = _mcp_server(monkeypatch, _Pipe())
    result = asyncio.run(server.call_tool(
        "bili_materialize", {"url_or_bvid": "BV1", "wait": True}
    ))
    payload = _payload(result)
    assert payload["chapters"], "MCP 必须返回非空章节索引"
    assert len(payload["chapters"]) == 2
    assert payload["chapters"][0]["title"] == "第一段"


def test_mcp_read_transcript_by_chapter_range(tmp_path, monkeypatch):
    """按章节范围能读到对应文本。"""
    import asyncio

    from bilibili_agent.schemas import Segment
    from bilibili_agent.subtitle_normalize import to_srt

    class _Cfg:
        temp_path = tmp_path / "tmp"
        config_path = None
        output_path = str(tmp_path / "output")
        max_images = 25

    class _Pipe:
        def __init__(self):
            self.cfg = _Cfg()

        def close(self):
            pass

    out = tmp_path / "output" / "BV1" / "P01_1"
    out.mkdir(parents=True, exist_ok=True)
    segs = [
        Segment(start_ms=0, end_ms=2000, text="第一章的内容"),
        Segment(start_ms=5000, end_ms=7000, text="第二章的内容"),
    ]
    (out / "transcript.srt").write_text(to_srt(segs), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({
        "bvid": "BV1", "source_url": "https://www.bilibili.com/video/BV1",
        "subtitle_source": "platform_subtitle", "files": ["transcript.srt"],
    }), encoding="utf-8")

    server = _mcp_server(monkeypatch, _Pipe())
    result = asyncio.run(server.call_tool(
        "bili_read_transcript",
        {"bvid": "BV1", "part": 1, "start_ms": 4000, "end_ms": 8000},
    ))
    payload = _payload(result)
    assert payload["ok"] is True
    texts = " ".join(s["text"] for s in payload["segments"])
    assert "第二章的内容" in texts
    assert "第一章的内容" not in texts
    assert payload["source_url"].endswith("/BV1")


def _payload(result):
    import json as _json

    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        if "result" in structured and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            return _json.loads(text)
    raise AssertionError("无法从 MCP 结果中取出 payload")
