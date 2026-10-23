"""烧录字幕 OCR 的离线逻辑（不需要 OCR 引擎与视频文件）。"""

from __future__ import annotations

from bilibili_agent.ocr_subtitles import (
    OcrConfig,
    OcrObservation,
    SubtitleOcr,
    is_watermark,
    normalize_text,
    similarity,
)


def test_normalize_text_strips_spaces():
    assert normalize_text(" 你 好 世界 ") == "你好世界"
    assert normalize_text("你好，世界") == "你好,世界"


def test_similarity_exact_and_partial():
    assert similarity("你好", "你好") == 1.0
    assert similarity("", "你好") == 0.0
    assert similarity("今天要讲的内容", "今天要讲的内容") == 1.0
    assert similarity("今天要讲的内容", "完全不一样") < 0.5


def test_is_watermark():
    assert is_watermark("bilibili") is True
    assert is_watermark("哔哩哔哩") is True
    assert is_watermark("@某个UP主") is True
    assert is_watermark("") is True
    assert is_watermark("这是正文字幕") is False


def _ocr(**kw) -> SubtitleOcr:
    return SubtitleOcr(ffmpeg="ffmpeg", config=OcrConfig(**kw))


def test_merge_groups_same_line_into_one_segment():
    ocr = _ocr(sample_interval=1.0, similarity=0.7)
    obs = [
        OcrObservation(0, "你好", 0.95),
        OcrObservation(1000, "你好", 0.93),
        OcrObservation(2000, "你好", 0.97),
        OcrObservation(3000, "再见", 0.91),
        OcrObservation(4000, "再见", 0.92),
    ]
    segs = ocr.merge(obs)
    assert len(segs) == 2
    assert segs[0].text == "你好"
    assert segs[0].start_ms == 0
    assert segs[0].end_ms == 3000
    assert segs[1].text == "再见"
    assert segs[0].source == "hard_subtitle_ocr"


def test_merge_keeps_longer_text_when_same_line():
    ocr = _ocr(sample_interval=1.0, similarity=0.6)
    obs = [
        OcrObservation(0, "你好", 0.9),
        OcrObservation(1000, "你好世界", 0.9),
        OcrObservation(2000, "你好世界", 0.9),
    ]
    segs = ocr.merge(obs)
    assert len(segs) == 1
    assert segs[0].text == "你好世界"


def test_merge_drops_short_flashes():
    """只出现一帧的碎片（转场/水印闪烁）应当被丢弃。"""
    ocr = _ocr(sample_interval=1.0, min_duration_ms=900)
    obs = [
        OcrObservation(0, "完整的一句字幕", 0.9),
        OcrObservation(1000, "完整的一句字幕", 0.9),
        OcrObservation(2000, "完", 0.9),
    ]
    segs = ocr.merge(obs)
    assert len(segs) == 1
    assert segs[0].text == "完整的一句字幕"


def test_merge_handles_empty_frames_as_gaps():
    """空帧表示画面上没有字幕，会把前后两句断开。"""
    ocr = _ocr(sample_interval=1.0)
    obs = [
        OcrObservation(0, "第一句", 0.9),
        OcrObservation(1000, "第一句", 0.9),
        OcrObservation(2000, "", 0.0),
        OcrObservation(3000, "第二句", 0.9),
        OcrObservation(4000, "第二句", 0.9),
    ]
    segs = ocr.merge(obs)
    assert [s.text for s in segs] == ["第一句", "第二句"]


def test_merge_uses_confidence():
    ocr = _ocr(sample_interval=1.0)
    obs = [
        OcrObservation(0, "一句", 0.80),
        OcrObservation(1000, "一句", 0.99),
    ]
    segs = ocr.merge(obs)
    assert segs[0].confidence == 0.99


def test_band_defaults_to_auto():
    cfg = OcrConfig()
    assert cfg.band == "auto"
    assert cfg.fixed_band is None


def test_fixed_band_modes():
    bottom = OcrConfig(band="bottom", band_top=0.74, band_bottom=0.99)
    assert bottom.fixed_band == (0.74, 0.99)
    top = OcrConfig(band="top", band_bottom=0.99)
    assert top.fixed_band is not None
    assert top.fixed_band[1] < 0.5  # 顶部


def test_diagnose_reports_missing_engine(monkeypatch):
    import importlib.util

    monkeypatch.setattr("bilibili_agent.ocr_subtitles.shutil.which", lambda _n: None)
    ocr = SubtitleOcr(ffmpeg="")
    problems = ocr.diagnose()
    assert any("ffmpeg" in p for p in problems)
    # rapidocr 已安装时不报缺依赖
    if importlib.util.find_spec("rapidocr"):
        assert not any("RapidOCR" in p for p in problems)
