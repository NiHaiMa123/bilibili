"""字幕源路由语义（T04/T05/T06）、模式产物要求（T08/T09/T24）与 ?p= 选择（T13）。"""

from __future__ import annotations

import json
from pathlib import Path

from bilibili_agent.config import Config
from bilibili_agent.pipeline import Pipeline
from bilibili_agent.schemas import Frame, Segment, VideoInfo, VideoPart


def _video() -> VideoInfo:
    return VideoInfo(
        bvid="BV1xx411c7mD",
        title="测试视频",
        author="UP主",
        aid=2,
        duration_s=120,
        parts=[
            VideoPart(page=1, cid=62131, title="P1", duration_s=60),
            VideoPart(page=2, cid=62132, title="P2", duration_s=60),
        ],
        source_url="https://www.bilibili.com/video/BV1xx411c7mD",
    )


class Harness(Pipeline):
    """可控的流水线替身：记录各来源是否被调用。"""

    def __init__(self, tmp_path: Path, **behaviour):
        cfg = Config(root=tmp_path, config_path=None)
        cfg.output_dir = str(tmp_path / "output")
        cfg.media_cache = str(tmp_path / "media")
        cfg.temp_dir = str(tmp_path / "tmp")
        cfg.ocr_enabled = behaviour.get("ocr_enabled", True)
        cfg.ocr_auto = behaviour.get("ocr_auto", True)
        super().__init__(cfg)
        self.calls: list[str] = []
        self.behaviour = behaviour

    # -- 各来源的可控实现 --
    def inspect(self, target):
        self.calls.append("inspect")
        return _video()

    def _get_platform_subtitles(self, video, part):
        self.calls.append("platform")
        segs = self.behaviour.get("platform_segments")
        if segs is None:
            return [], None, ["没有平台字幕"]
        return segs, None, []

    def _ensure_media(self, video, part, *, media_mode="audio-only"):
        self.calls.append(f"download:{media_mode}")
        if not self.behaviour.get("media_ok", True):
            from bilibili_agent.bdl_adapter import BdlError

            raise BdlError("模拟下载失败", stage="download")
        d = Path(self.cfg.media_cache) / f"media_{part.page}_{media_mode}.mp4"
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(b"fake-media")
        return [str(d)]

    def _get_soft_subtitles(self, media_path, warnings):
        self.calls.append("soft")
        return self.behaviour.get("soft_segments") or []

    def _run_ocr(self, video, part, media_path, out_dir, warnings):
        self.calls.append("ocr")
        return self.behaviour.get("ocr_segments") or [], self.behaviour.get("ocr_stats")

    def _get_asr_subtitles(self, media_path, out_dir, warnings):
        self.calls.append("asr")
        return self.behaviour.get("asr_segments") or [], self.behaviour.get("asr_meta")

    def _build_frames(self, video, part, segments, out_dir):
        self.calls.append("frames")
        if not self.behaviour.get("frames_ok", True):
            return [], ["模拟抽帧失败"]
        assets = out_dir / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        frames = []
        for i in range(self.behaviour.get("frame_count", 2)):
            p = assets / f"frame_{i:03d}.jpg"
            p.write_bytes(b"fake-jpeg-bytes")
            frames.append(Frame(time_ms=i * 1000, relative_path=f"assets/{p.name}",
                               reason="scene_change"))
        return frames, []


def _segs(n=3):
    return [
        Segment(start_ms=i * 1000, end_ms=i * 1000 + 900, text=f"第{i}句")
        for i in range(n)
    ]


# ---------------------------------------------------------------- T04 asr 独占
def test_source_asr_never_runs_ocr(tmp_path):
    """source=asr 时，即使 ocr_auto=true 也不能跑 OCR。"""
    h = Harness(tmp_path, ocr_auto=True, asr_segments=_segs(), media_ok=True)
    res = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="asr")
    assert res.ok
    assert "ocr" not in h.calls
    assert "asr" in h.calls
    assert "platform" not in h.calls
    assert "soft" not in h.calls
    assert res.parts[0]["subtitle_source"] == "qwen3_asr"


def test_source_asr_prefers_audio_only(tmp_path):
    h = Harness(tmp_path, asr_segments=_segs())
    h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="asr")
    assert "download:audio-only" in h.calls
    assert "download:audio-video" not in h.calls


# ---------------------------------------------------------------- T05 ocr 独占
def test_source_ocr_failure_never_runs_asr(tmp_path):
    """source=ocr 且 OCR 无结果时，ASR 必须一次都不调用，且明确失败。"""
    h = Harness(tmp_path, ocr_segments=[])
    res = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="ocr")
    assert "asr" not in h.calls
    assert "ocr" in h.calls
    assert not res.ok
    assert res.status == "failed"
    assert res.parts[0]["error"]["stage"] == "subtitle"
    assert any("不自动回退" in w for w in res.warnings)


def test_source_ocr_success_uses_ocr(tmp_path):
    h = Harness(tmp_path, ocr_segments=_segs())
    res = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="ocr")
    assert res.ok
    assert res.parts[0]["subtitle_source"] == "hard_subtitle_ocr"
    assert "asr" not in h.calls


# ---------------------------------------------------------------- T06 platform 独占
def test_source_platform_does_not_download_media(tmp_path):
    """source=platform 且无字幕时，不得触发任何媒体下载。"""
    h = Harness(tmp_path, platform_segments=None)
    res = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="platform")
    assert not any(c.startswith("download:") for c in h.calls)
    assert not res.ok
    assert res.parts[0]["error"]["stage"] == "subtitle"


def test_source_platform_success_does_not_download_media(tmp_path):
    h = Harness(tmp_path, platform_segments=_segs())
    res = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="platform")
    assert res.ok
    assert not any(c.startswith("download:") for c in h.calls)
    assert res.parts[0]["subtitle_source"] == "platform_subtitle"


# ---------------------------------------------------------------- auto 回退链
def test_auto_falls_back_platform_to_ocr(tmp_path):
    h = Harness(tmp_path, platform_segments=None, ocr_segments=_segs())
    res = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="auto")
    assert res.ok
    assert h.calls.index("platform") < h.calls.index("ocr")
    assert res.parts[0]["subtitle_source"] == "hard_subtitle_ocr"


def test_auto_falls_back_to_asr_when_ocr_empty(tmp_path):
    h = Harness(tmp_path, platform_segments=None, ocr_segments=[],
                asr_segments=_segs())
    res = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="auto")
    assert res.ok
    assert "asr" in h.calls
    assert res.parts[0]["subtitle_source"] == "qwen3_asr"


def test_auto_prefers_soft_subtitle_over_ocr(tmp_path):
    h = Harness(tmp_path, platform_segments=None, soft_segments=_segs(),
                ocr_segments=_segs())
    res = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="auto")
    assert res.parts[0]["subtitle_source"] == "soft_subtitle"
    assert "ocr" not in h.calls


# ---------------------------------------------------------------- T08 illustrated
def test_illustrated_fails_when_frames_missing(tmp_path):
    """抽帧失败时 illustrated 不能返回完整成功。"""
    h = Harness(tmp_path, platform_segments=_segs(), frames_ok=False)
    res = h.fetch("BV1xx411c7mD", mode="illustrated", subtitle_source="platform")
    assert not res.ok
    assert res.status == "failed"
    assert res.parts[0]["error"]["stage"] == "frames"
    manifest = json.loads(
        (Path(res.parts[0]["directory"]) / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "failed"
    assert "article.md" in manifest["missing_artifacts"]


def test_illustrated_ok_when_article_and_frames_exist(tmp_path):
    h = Harness(tmp_path, platform_segments=_segs(), frame_count=2)
    res = h.fetch("BV1xx411c7mD", mode="illustrated", subtitle_source="platform")
    assert res.ok
    out = Path(res.parts[0]["directory"])
    assert (out / "article.md").is_file()
    assert (out / "transcript.md").is_file()
    assert len(list((out / "assets").glob("*.jpg"))) == 2
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"


# ---------------------------------------------------------------- T09 assets
def test_assets_mode_works_without_any_subtitle(tmp_path):
    """assets 模式不要求字幕：没有平台字幕也应产出真实图片。"""
    h = Harness(tmp_path, platform_segments=None, frame_count=3)
    res = h.fetch("BV1xx411c7mD", mode="assets", subtitle_source="auto")
    assert res.ok, res.to_dict()
    out = Path(res.parts[0]["directory"])
    imgs = list((out / "assets").glob("*.jpg"))
    assert len(imgs) == 3
    assert not (out / "transcript.md").exists()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["subtitle_source"] == "none"


# ---------------------------------------------------------------- T13 ?p= 选择
def test_url_page_param_selects_that_part(tmp_path):
    h = Harness(tmp_path, platform_segments=_segs())
    res = h.fetch(
        "https://www.bilibili.com/video/BV1xx411c7mD?p=2",
        mode="subtitle", subtitle_source="platform",
    )
    assert res.ok
    assert len(res.parts) == 1
    assert res.parts[0]["page"] == 2
    assert res.parts[0]["cid"] == 62132


def test_explicit_parts_overrides_url_page_param(tmp_path):
    h = Harness(tmp_path, platform_segments=_segs())
    res = h.fetch(
        "https://www.bilibili.com/video/BV1xx411c7mD?p=2",
        parts=[1], mode="subtitle", subtitle_source="platform",
    )
    assert res.parts[0]["page"] == 1
    assert res.parts[0]["cid"] == 62131


def test_no_page_param_defaults_to_p1(tmp_path):
    h = Harness(tmp_path, platform_segments=_segs())
    res = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="platform")
    assert res.parts[0]["page"] == 1
    assert res.parts[0]["cid"] == 62131


def test_invalid_page_param_reports_error(tmp_path):
    h = Harness(tmp_path, platform_segments=_segs())
    res = h.fetch(
        "https://www.bilibili.com/video/BV1xx411c7mD?p=9",
        mode="subtitle", subtitle_source="platform",
    )
    assert not res.ok
    assert res.error is not None


# ---------------------------------------------------------------- T24 多P聚合
def test_multi_part_partial_failure_status(tmp_path):
    """多 P 部分失败 → 顶层 partial，且带分 P 级错误原因。"""
    class PartialHarness(Harness):
        def _get_platform_subtitles(self, video, part):
            self.calls.append("platform")
            if part.page == 2:
                return [], None, ["P2 没有字幕"]
            return _segs(), None, []

    h = PartialHarness(tmp_path)
    res = h.fetch("BV1xx411c7mD", parts=[1, 2], mode="subtitle",
                  subtitle_source="platform")
    assert res.status == "partial"
    assert not res.ok
    assert res.error is not None
    assert res.error["message"]
    failed = [p for p in res.parts if not p["ok"]]
    assert len(failed) == 1
    assert failed[0]["page"] == 2
    assert failed[0]["error"]["stage"] == "subtitle"
