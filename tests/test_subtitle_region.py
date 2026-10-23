"""时序字幕区跟踪与多通道证据（T20/T21/T22/T23）。"""

from __future__ import annotations

from pathlib import Path

from bilibili_agent.ocr_engine import OcrBox
from bilibili_agent.ocr_subtitles import (
    OcrConfig,
    OcrObservation,
    SubtitleOcr,
)
from bilibili_agent.subtitle_region import (
    ROLE_DIALOGUE,
    ROLE_SECONDARY,
    RegionTrack,
    SubtitleRegionTracker,
    TrackerConfig,
)


def _box(text: str, y0: float = 0.82, y1: float = 0.90,
         x0: float = 0.20, x1: float = 0.70, score: float = 0.95) -> OcrBox:
    return OcrBox(text=text, score=score, x0=x0, y0=y0, x1=x1, y1=y1)


def _track(texts: list[str], *, y0=0.82, y1=0.90, step=1000) -> RegionTrack:
    track = RegionTrack()
    for i, t in enumerate(texts):
        track.observations.append((i * step, _box(t, y0=y0, y1=y1)))
    return track


# ---------------------------------------------------------------- T21 打分
def test_bottom_centered_changing_text_scores_high():
    tracker = SubtitleRegionTracker(engine=None, config=TrackerConfig())
    track = _track(["第一句", "第二句", "第三句"])
    score, evidence = tracker._score(track)
    assert score > 0.6
    assert "text_turnover" in evidence
    assert "bottom_soft_prior" in evidence
    assert "large_text" in evidence


def test_static_ui_text_is_penalised():
    """常驻不变的标题栏/水印不该被当成对白字幕。"""
    tracker = SubtitleRegionTracker(engine=None, config=TrackerConfig())
    track = _track(["文件列表"] * 30, step=2000)  # 持续很久且从不换字
    score, evidence = tracker._score(track)
    assert "static_ui_text" in evidence
    assert score < 0.6


def test_transient_popup_is_penalised():
    tracker = SubtitleRegionTracker(engine=None, config=TrackerConfig())
    track = RegionTrack()
    track.observations.append((0, _box("弹窗")))
    track.windows.add(0)
    score, evidence = tracker._score(track)
    assert "transient_popup" in evidence


def test_top_subtitle_can_still_win_when_it_changes():
    """底部只是软先验——顶部字幕如果同样稳定且内容在换，也应拿到高分。"""
    tracker = SubtitleRegionTracker(engine=None, config=TrackerConfig())
    track = _track(["顶行字幕一", "顶行字幕二", "顶行字幕三"], y0=0.03, y1=0.11)
    score, _ = tracker._score(track)
    assert score > 0.5


# ---------------------------------------------------------------- 轨迹关联
def test_link_groups_same_position():
    tracker = SubtitleRegionTracker(engine=None, config=TrackerConfig())
    tracks: list[RegionTrack] = []
    for i in range(4):
        tracker._link(tracks, i * 1000, _box(f"字幕{i}"))
    assert len(tracks) == 1
    assert tracks[0].distinct_texts() == 4


def test_link_separates_different_positions():
    tracker = SubtitleRegionTracker(engine=None, config=TrackerConfig())
    tracks: list[RegionTrack] = []
    tracker._link(tracks, 0, _box("底部字幕", y0=0.85, y1=0.93))
    tracker._link(tracks, 200, _box("顶部标题", y0=0.05, y1=0.12))
    assert len(tracks) == 2


def test_link_respects_time_gap():
    tracker = SubtitleRegionTracker(engine=None, config=TrackerConfig())
    tracks: list[RegionTrack] = []
    tracker._link(tracks, 0, _box("字幕"))
    tracker._link(tracks, 60_000, _box("字幕"))
    assert len(tracks) == 2


# ---------------------------------------------------------------- T22 ROI 跟踪
class _FakeEngine:
    def __init__(self, boxes_by_time: dict[int, list[OcrBox]]):
        self.boxes_by_time = boxes_by_time
        self.calls = 0

    def boxes(self, image) -> list[OcrBox]:
        self.calls += 1
        # 用图像里的"时间戳"取对应框：测试里用 numpy 存时间
        try:

            t = int(image[0, 0, 0])
        except Exception:  # noqa: BLE001
            t = 0
        return self.boxes_by_time.get(t, [])


def _fake_frames(monkeypatch, times: list[int]):
    """让 tracker._window_frames 直接返回带"时间戳"的假帧。"""
    import numpy as np

    def fake_window_frames(self, video, start_s, length_s, out_dir):
        return [(t, Path(f"fake_{t}.jpg")) for t in times]

    monkeypatch.setattr(
        SubtitleRegionTracker, "_window_frames", fake_window_frames
    )
    monkeypatch.setattr(
        SubtitleRegionTracker, "_read_image",
        lambda self, path: np.full((10, 10, 3), int(path.stem.split("_")[1])),
    )


def test_tracker_finds_bottom_subtitle_region(monkeypatch):
    times = list(range(0, 2000, 200))
    boxes = {t: [_box(f"字幕{t}")] for t in times}
    engine = _FakeEngine(boxes)
    tracker = SubtitleRegionTracker(engine=engine, ffmpeg="ffmpeg", config=TrackerConfig(windows=3))
    _fake_frames(monkeypatch, times)
    det = tracker.track("fake.mp4", duration_ms=60_000)
    assert det.best is not None
    best = det.best
    assert best.role == ROLE_DIALOGUE
    assert 0.7 < best.y0 < 0.9
    assert best.heuristic_score > 0.5
    assert "text_turnover" in best.evidence
    assert det.to_dict()["method"] == "temporal_tracking_v1"


def test_tracker_prefers_changing_text_over_static_ui(monkeypatch):
    """同一个视频里，既有常驻标题栏又有会变的字幕 → 应选字幕。"""
    times = list(range(0, 2000, 200))
    boxes = {
        t: [
            _box("常驻标题栏", y0=0.05, y1=0.11, x0=0.05, x1=0.45),
            _box(f"对白字幕{t}", y0=0.84, y1=0.93, x0=0.15, x1=0.85),
        ]
        for t in times
    }
    engine = _FakeEngine(boxes)
    tracker = SubtitleRegionTracker(engine=engine, ffmpeg="ffmpeg", config=TrackerConfig(windows=3))
    _fake_frames(monkeypatch, times)
    det = tracker.track("fake.mp4", duration_ms=60_000)
    assert det.best is not None
    assert det.best.y0 > 0.7  # 选的是底部对白，不是顶部标题


def test_tracker_reports_low_confidence_instead_of_guessing(monkeypatch):
    """没有可判断的轨迹时要明确说明，而不是硬猜一个。"""
    times = list(range(0, 400, 200))
    engine = _FakeEngine({t: [] for t in times})
    tracker = SubtitleRegionTracker(engine=engine, ffmpeg="ffmpeg", config=TrackerConfig(windows=2))
    _fake_frames(monkeypatch, times)
    det = tracker.track("fake.mp4", duration_ms=60_000)
    assert det.best is None
    assert det.warnings


def test_tracker_can_return_bilingual_second_region(monkeypatch):
    times = list(range(0, 2000, 200))
    boxes = {
        t: [
            _box(f"中文{t}", y0=0.80, y1=0.86, x0=0.20, x1=0.80),
            _box(f"English {t}", y0=0.87, y1=0.93, x0=0.20, x1=0.80),
        ]
        for t in times
    }
    engine = _FakeEngine(boxes)
    tracker = SubtitleRegionTracker(engine=engine, ffmpeg="ffmpeg", config=TrackerConfig(windows=3))
    _fake_frames(monkeypatch, times)
    det = tracker.track("fake.mp4", duration_ms=60_000)
    assert len(det.regions) >= 2
    roles = {r.role for r in det.regions}
    assert ROLE_DIALOGUE in roles
    assert ROLE_SECONDARY in roles


# ---------------------------------------------------------------- T20 合并
def test_long_static_subtitle_is_not_dropped():
    """同一句字幕保持 6 秒是正常的，不能因为"多帧相同"就删掉。"""
    ocr = SubtitleOcr(config=OcrConfig(sample_interval=1.0, min_samples=2))
    obs = [OcrObservation(i * 1000, "同一句字幕", 0.95) for i in range(7)]
    segs = ocr.merge(obs)
    assert len(segs) == 1
    assert segs[0].text == "同一句字幕"
    assert segs[0].start_ms == 0
    assert segs[0].end_ms >= 6000


def test_multiline_text_is_preserved():
    ocr = SubtitleOcr(config=OcrConfig(sample_interval=1.0, min_samples=2))
    obs = [
        OcrObservation(0, "第一行\n第二行", 0.95, lines=2),
        OcrObservation(1000, "第一行\n第二行", 0.95, lines=2),
    ]
    segs = ocr.merge(obs)
    assert len(segs) == 1
    assert "\n" in segs[0].text
    assert "第一行" in segs[0].text and "第二行" in segs[0].text
    assert "lines=2" in segs[0].flags


def test_static_overlay_moves_to_on_screen_channel():
    """唯一的、长期不变的一段 → 判为界面文字，转入 on_screen_text。"""
    ocr = SubtitleOcr(config=OcrConfig(static_min_duration_ms=20_000))
    segs = [
        __import__("bilibili_agent.schemas", fromlist=["Segment"]).Segment(
            start_ms=0, end_ms=60_000, text="常驻界面文字",
            source="hard_subtitle_ocr",
        )
    ]
    dialogue, on_screen = ocr.split_static_text(segs, total_ms=60_000)
    assert dialogue == []
    assert on_screen and on_screen[0]["text"] == "常驻界面文字"
    assert on_screen[0]["kind"] == "static_overlay"


# ---------------------------------------------------------------- T23 坏帧
def test_engine_error_is_distinguished_from_blank_frame():
    """引擎异常与"确实没有字幕"必须分开记录。"""
    obs = [
        OcrObservation(0, "有字幕", 0.9),
        OcrObservation(1000, "", 0.0),                    # 真的没有字幕
        OcrObservation(2000, "", 0.0, error="ocr_failed:boom"),  # 引擎异常
    ]
    errors = [o for o in obs if o.error]
    blanks = [o for o in obs if not o.error and not o.text]
    assert len(errors) == 1
    assert len(blanks) == 1
    assert errors[0].error.startswith("ocr_failed")


# ---------------------------------------------------------------- O3 合并
def test_merge_ocr_asr_fills_gaps_and_keeps_sources():
    from bilibili_agent.pipeline import Pipeline
    from bilibili_agent.schemas import Segment

    ocr_segs = [
        Segment(start_ms=0, end_ms=5000, text="画面上的字幕", source="hard_subtitle_ocr"),
    ]
    asr_segs = [
        Segment(start_ms=0, end_ms=4000, text="讲者说的内容", source="qwen3_asr"),
        Segment(start_ms=20000, end_ms=24000, text="缺口处的语音", source="qwen3_asr"),
    ]
    merged, conflicts = Pipeline._merge_ocr_asr(ocr_segs, asr_segs)
    sources = {s.source for s in merged}
    assert "hard_subtitle_ocr" in sources
    assert "qwen3_asr" in sources
    # 缺口被补上了
    assert any(s.start_ms == 20000 for s in merged)
    # 重叠且文本差异大 → 记冲突
    assert conflicts
    assert conflicts[0]["note"]
