"""REVIEW2 Phase A 回归测试（T01–T10）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bilibili_agent.ocr_subtitles import OcrConfig, OcrObservation, Roi, SubtitleOcr
from bilibili_agent.pipeline import Pipeline
from bilibili_agent.schemas import Segment
from test_routing import Harness, _segs


# ---------------------------------------------------------------- T01 fallback ROI
def test_fallback_roi_is_full_width_bottom(monkeypatch):
    """跟踪器没有 best 时，回退 ROI 必须是全宽底部，而不是右下角一小块。"""
    ocr = SubtitleOcr(config=OcrConfig(band_top=0.74, band_bottom=0.99))

    class _NoBest:
        best = None
        tracks: list = []
        warnings: list = []

        def to_dict(self):
            return {"regions": [], "method": "temporal_tracking_v1", "warnings": []}

    monkeypatch.setattr(
        "bilibili_agent.subtitle_region.SubtitleRegionTracker.track",
        lambda self, video, duration_ms=None: _NoBest(),
    )
    plan, info, _ = ocr.detect_roi("fake.mp4", Path("."))
    roi = plan.fallback
    assert roi.x0 == 0.0
    assert roi.y0 == pytest.approx(0.74)
    assert roi.x1 == 1.0
    assert roi.y1 == pytest.approx(0.99)
    assert roi.method == "fallback"
    assert roi.heuristic_score == 0.0
    # 必须明确说明这是回退，不当成高置信检测
    assert info.get("fallback") is True
    assert any("fallback" in w for w in info.get("warnings", []))


def test_fallback_roi_honours_custom_band(monkeypatch):
    ocr = SubtitleOcr(config=OcrConfig(band_top=0.55, band_bottom=0.80))

    class _NoBest:
        best = None
        tracks: list = []
        warnings: list = []

        def to_dict(self):
            return {"regions": []}

    monkeypatch.setattr(
        "bilibili_agent.subtitle_region.SubtitleRegionTracker.track",
        lambda self, video, duration_ms=None: _NoBest(),
    )
    plan, _, _ = ocr.detect_roi("fake.mp4", Path("."))
    roi = plan.fallback
    assert (roi.x0, roi.y0, roi.x1, roi.y1) == (0.0, 0.55, 1.0, 0.80)


def test_roi_rejects_invalid_ranges():
    with pytest.raises(ValueError):
        Roi(x0=0.9, y0=0.1, x1=0.2, y1=0.3)
    with pytest.raises(ValueError):
        Roi(x0=0.1, y0=0.9, x1=0.2, y1=0.3)
    with pytest.raises(ValueError):
        Roi(x0=-0.1, y0=0.1, x1=0.2, y1=0.3)
    with pytest.raises(ValueError):
        Roi(x0=0.1, y0=0.1, x1=1.2, y1=0.3)


def test_detect_band_fallback_also_full_width(monkeypatch):
    ocr = SubtitleOcr(config=OcrConfig(band_top=0.74, band_bottom=0.99))

    class _NoBest:
        best = None
        tracks: list = []

        def to_dict(self):
            return {"regions": []}

    monkeypatch.setattr(
        "bilibili_agent.subtitle_region.SubtitleRegionTracker.track",
        lambda self, video, duration_ms=None: _NoBest(),
    )
    roi = ocr.detect_band("fake.mp4", Path("."))
    assert (roi.x0, roi.y0, roi.x1, roi.y1) == (0.0, 0.74, 1.0, 0.99)


# ---------------------------------------------------------------- T02 缺图不得缓存命中
def test_illustrated_missing_image_is_not_cache_hit(tmp_path):
    """illustrated 产物里删掉一张图后，第二次不得报告完整缓存命中。"""
    h = Harness(tmp_path, platform_segments=_segs(), frame_count=2)
    first = h.fetch("BV1xx411c7mD", mode="illustrated", subtitle_source="platform")
    assert first.ok
    out = Path(first.parts[0]["directory"])
    images = sorted((out / "assets").glob("*.jpg"))
    assert len(images) == 2

    images[0].unlink()  # 删掉一张图

    h2 = Harness(tmp_path, platform_segments=_segs(), frame_count=2)
    second = h2.fetch("BV1xx411c7mD", mode="illustrated", subtitle_source="platform")
    # 必须重新抽帧（frames 被调用）
    assert "frames" in h2.calls
    assert not second.parts[0].get("cached")


# ---------------------------------------------------------------- T03 assets 命中缓存
def test_assets_mode_second_run_hits_image_cache(tmp_path):
    """assets 模式二次执行应命中图片缓存，且 outputs 包含图片路径。"""
    h = Harness(tmp_path, platform_segments=None, frame_count=3)
    first = h.fetch("BV1xx411c7mD", mode="assets", subtitle_source="auto")
    assert first.ok
    out = Path(first.parts[0]["directory"])
    assert len(list((out / "assets").glob("*.jpg"))) == 3

    h2 = Harness(tmp_path, platform_segments=None, frame_count=3)
    second = h2.fetch("BV1xx411c7mD", mode="assets", subtitle_source="auto")
    assert second.parts[0].get("cached") is True
    # 第二次不应该重新抽帧
    assert "frames" not in h2.calls
    # outputs 要包含真实可打开的图片路径
    outputs = second.parts[0]["outputs"]
    imgs = [o for o in outputs if o.endswith(".jpg")]
    assert len(imgs) == 3
    for p in imgs:
        assert Path(p).is_file()


def test_assets_mode_outputs_contain_images(tmp_path):
    h = Harness(tmp_path, platform_segments=None, frame_count=2)
    res = h.fetch("BV1xx411c7mD", mode="assets", subtitle_source="auto")
    assert res.ok
    outputs = res.parts[0]["outputs"]
    assert any(o.endswith(".jpg") for o in outputs)
    assert res.parts[0]["image_files"]


# ---------------------------------------------------------------- T04 后缀一致性
@pytest.mark.parametrize("fmt,suffix", [("jpg", "jpg"), ("png", "png"), ("webp", "webp")])
def test_missing_image_message_uses_configured_suffix(tmp_path, fmt, suffix):
    h = Harness(tmp_path, platform_segments=_segs(), frame_count=1)
    h.cfg.image_format = fmt
    res = h.fetch("BV1xx411c7mD", mode="illustrated", subtitle_source="platform")
    assert res.ok
    out = Path(res.parts[0]["directory"])
    images = list((out / "assets").glob(f"*.{suffix}"))
    assert images, f"应当生成 .{suffix} 图片"
    # manifest 里的图片清单后缀一致
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    comp = manifest["components"]["frames"]
    assert comp["image_files"]
    assert all(f.endswith(f".{suffix}") for f in comp["image_files"])


# ---------------------------------------------------------------- T05 增量升级
def test_subtitle_to_illustrated_reuses_transcript_only(tmp_path):
    h = Harness(tmp_path, platform_segments=_segs(), frame_count=2)
    sub = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="platform")
    assert sub.ok

    h2 = Harness(tmp_path, platform_segments=_segs(), frame_count=2)
    ill = h2.fetch("BV1xx411c7mD", mode="illustrated", subtitle_source="platform")
    assert ill.ok
    # 字幕没有重新取，只新增了抽帧
    assert h2.calls.count("platform") == 0
    assert "frames" in h2.calls
    out = Path(ill.parts[0]["directory"])
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["components"]["transcript"]["reused"] is True
    assert manifest["components"]["frames"]["frame_count"] == 2


# ---------------------------------------------------------------- T06/T07/T08/T09 合并
def test_asr_block_not_dropped_by_small_overlap():
    """2 秒 OCR + 30 秒 ASR：不能把整条 ASR 丢掉。"""
    ocr_segs = [Segment(start_ms=10000, end_ms=12000, text="画面识别到的一句话",
                        source="hard_subtitle_ocr")]
    asr_segs = [Segment(start_ms=0, end_ms=30000, text="第一步...第二步...第三步...",
                        source="qwen3_asr")]
    merged, conflicts = Pipeline._merge_ocr_asr(ocr_segs, asr_segs)
    sources = [s.source for s in merged]
    assert "qwen3_asr" in sources, "ASR 段不能因为 2 秒重叠就整条消失"
    # 其余 28 秒的信息仍可被检索到
    asr_seg = next(s for s in merged if s.source == "qwen3_asr")
    assert asr_seg.start_ms == 0 and asr_seg.end_ms == 30000
    # 有冲突但两边原文都保留
    assert conflicts
    assert conflicts[0]["ocr_text"] == "画面识别到的一句话"
    assert conflicts[0]["asr_text"].startswith("第一步")


def test_asr_fully_covered_and_identical_is_deduplicated():
    ocr_segs = [Segment(start_ms=0, end_ms=10000, text="完全一样的一句话",
                        source="hard_subtitle_ocr")]
    asr_segs = [Segment(start_ms=0, end_ms=10000, text="完全一样的一句话",
                        source="qwen3_asr")]
    merged, conflicts = Pipeline._merge_ocr_asr(ocr_segs, asr_segs)
    assert [s.source for s in merged] == ["hard_subtitle_ocr"]
    assert not conflicts


def test_block_level_asr_is_not_fake_split():
    """块级 ASR 不做按字符比例的伪时间切分，起点/终点必须保持原样。"""
    ocr_segs = [Segment(start_ms=10000, end_ms=12000, text="画面文字",
                        source="hard_subtitle_ocr")]
    asr_segs = [Segment(start_ms=0, end_ms=30000, text="一整块语音内容",
                        source="qwen3_asr")]
    merged, _ = Pipeline._merge_ocr_asr(ocr_segs, asr_segs)
    asr_seg = next(s for s in merged if s.source == "qwen3_asr")
    assert (asr_seg.start_ms, asr_seg.end_ms) == (0, 30000)
    assert "overlap_with_ocr" in asr_seg.flags


def test_conflicts_are_traceable_with_sources():
    ocr_segs = [Segment(start_ms=0, end_ms=5000, text="画面上的中文",
                        source="hard_subtitle_ocr")]
    asr_segs = [Segment(start_ms=0, end_ms=5000, text="spoken english",
                        source="qwen3_asr")]
    merged, conflicts = Pipeline._merge_ocr_asr(ocr_segs, asr_segs)
    assert conflicts
    c = conflicts[0]
    assert c["ocr_source"] == "hard_subtitle_ocr"
    assert c["asr_source"] == "qwen3_asr"
    assert c["start_ms"] == 0 and c["end_ms"] == 5000
    assert c["reason"]
    # 冲突时两边都保留（不擅自用 ASR 改写画面原字）
    assert len(merged) == 2


def test_merged_segments_are_time_ordered():
    ocr_segs = [Segment(start_ms=20000, end_ms=22000, text="B", source="hard_subtitle_ocr")]
    asr_segs = [
        Segment(start_ms=0, end_ms=5000, text="A", source="qwen3_asr"),
        Segment(start_ms=10000, end_ms=12000, text="C", source="qwen3_asr"),
    ]
    merged, _ = Pipeline._merge_ocr_asr(ocr_segs, asr_segs)
    times = [s.start_ms for s in merged]
    assert times == sorted(times)


# ---------------------------------------------------------------- T10 空白帧不复活
def test_blank_frame_does_not_resurrect_old_subtitle(monkeypatch, tmp_path):
    """text → blank → blank → text：不能出现 A → 空白 → A 的复活。"""
    ocr = SubtitleOcr(config=OcrConfig(sample_interval=1.0, change_threshold=0.5,
                                        min_samples=1))

    frames = [tmp_path / f"f{i}.jpg" for i in range(4)]
    for f in frames:
        f.write_bytes(b"x")
    monkeypatch.setattr(ocr, "extract_frames", lambda video, out: frames)

    # 第 3、4 帧与第 2 帧"几乎没变"（复用路径本应触发）
    changes = [1.0, 1.0, 0.0, 0.0]
    seq = iter(changes)
    monkeypatch.setattr(
        SubtitleOcr, "_roi_change", staticmethod(lambda prev, curr: next(seq))
    )

    # OCR 结果：第 1 帧有字幕，第 2、3 帧空白，第 4 帧换新字幕
    ocr_results = {
        0: [[("字幕A", 0.95)]],
        1: [],
        2: [],
        3: [[("字幕B", 0.95)]],
    }
    idx = {"i": 0}

    def fake_ocr_roi(self, crop, roi, frame_size=None):
        i = idx["i"]
        idx["i"] += 1
        return ocr_results.get(i, [])

    monkeypatch.setattr(SubtitleOcr, "_ocr_roi", fake_ocr_roi)

    class _Img:
        shape = (10, 10, 3)

    import numpy as np

    monkeypatch.setattr(
        SubtitleOcr, "_crop_roi", lambda self, image, roi: np.zeros((4, 4, 3), np.uint8)
    )
    monkeypatch.setattr(
        "cv2.imread", lambda path, *a, **k: _Img()
    )

    roi = Roi(x0=0.0, y0=0.8, x1=1.0, y1=0.95)
    obs = ocr.observe("fake.mp4", tmp_path, roi)
    texts = [o.text for o in obs]
    # 空白帧之后不能再次出现字幕A
    assert texts[0] == "字幕A"
    assert texts[1] == "" and texts[2] == ""
    assert "字幕A" not in texts[3:]
    assert texts[3] == "字幕B"

    segs = ocr.merge(obs)
    assert [s.text for s in segs] == ["字幕A", "字幕B"]


def test_single_sample_subtitle_is_dropped_by_default():
    """默认 min_samples=2：只出现一帧的碎片不当成字幕（噪声保护）。"""
    ocr = SubtitleOcr(config=OcrConfig(sample_interval=1.0))
    obs = [
        OcrObservation(0, "一闪而过的碎片", 0.9),
        OcrObservation(1000, "", 0.0),
    ]
    assert ocr.merge(obs) == []


def test_error_frame_is_distinct_from_blank(monkeypatch, tmp_path):
    """引擎异常帧不等于字幕消失，合并时不应无条件断句。"""
    ocr = SubtitleOcr(config=OcrConfig(sample_interval=1.0, min_samples=1))
    obs = [
        OcrObservation(0, "字幕A", 0.9),
        OcrObservation(1000, "", 0.0, error="ocr_failed:boom"),
        OcrObservation(2000, "字幕A", 0.9),
    ]
    segs = ocr.merge(obs)
    # 异常帧不打断同一句
    assert len(segs) == 1
    assert segs[0].text == "字幕A"
