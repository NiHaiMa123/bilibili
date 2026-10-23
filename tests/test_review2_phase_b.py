"""REVIEW2 Phase B 回归测试（T11–T20）。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from bilibili_agent.ocr_engine import OcrBox
from bilibili_agent.ocr_subtitles import (
    OcrConfig,
    Roi,
    RoiPlan,
    RoiSlot,
    SubtitleOcr,
)
from bilibili_agent.pipeline import Pipeline
from bilibili_agent.schemas import Segment
from bilibili_agent.subtitle_region import (
    ROLE_DIALOGUE,
    ROLE_SECONDARY,
    ROLE_TIMELINE,
    RegionTrack,
    SubtitleRegionTracker,
    TrackerConfig,
)
from test_routing import Harness, _segs


def _box(text: str, y0: float = 0.82, y1: float = 0.90,
         x0: float = 0.20, x1: float = 0.70, score: float = 0.95) -> OcrBox:
    return OcrBox(text=text, score=score, x0=x0, y0=y0, x1=x1, y1=y1)


class _FakeEngine:
    """按"帧内时间戳"返回对应的文字框。"""

    def __init__(self, boxes_by_time: dict[int, list[OcrBox]]):
        self.boxes_by_time = boxes_by_time
        self.calls = 0

    def boxes(self, image) -> list[OcrBox]:
        self.calls += 1
        t = int(image[0, 0, 0])
        return self.boxes_by_time.get(t, [])


def _install_fake_windows(monkeypatch, times_by_window: list[list[int]]):
    """让 tracker 的每个采样窗口返回指定的时刻集合。"""

    def fake_window_frames(self, video, start_s, length_s, out_dir):
        # out_dir 形如 .../win{w}
        w = int(Path(out_dir).name.replace("win", ""))
        return [(t, Path(f"fake_{t}.jpg")) for t in times_by_window[w]]

    monkeypatch.setattr(
        SubtitleRegionTracker, "_window_frames", fake_window_frames
    )
    monkeypatch.setattr(
        SubtitleRegionTracker, "_read_image",
        lambda self, path: np.full((10, 10, 3), int(path.stem.split("_")[1])),
    )


# ---------------------------------------------------------------- T11 跨窗口关联
def test_far_apart_windows_still_merge_into_one_region(monkeypatch):
    """窗口 0/2/5 位置相同、文本不同 → 同一个区域候选拿到跨窗口支持。"""
    times_by_window = [
        [0, 200], [400, 600], [800, 1000],
        [1200, 1400], [1600, 1800], [2000, 2200],
    ]
    boxes: dict[int, list[OcrBox]] = {}
    for w, times in enumerate(times_by_window):
        for t in times:
            boxes[t] = [_box(f"字幕{w}_{t}")]
    engine = _FakeEngine(boxes)
    tracker = SubtitleRegionTracker(
        engine=engine, ffmpeg="ffmpeg",
        config=TrackerConfig(windows=6, link_gap_ms=1600),
    )
    _install_fake_windows(monkeypatch, times_by_window)
    det = tracker.track("fake.mp4", duration_ms=600_000)

    assert det.best is not None
    # 6 个窗口里的同一条字幕带应被归并成一个候选
    assert len(det.candidates) == 1
    cand = det.candidates[0]
    assert len(cand.windows) == 6
    assert "persists_across_windows" in cand.evidence
    assert "text_turnover" in cand.evidence


# ---------------------------------------------------------------- T12 静态 UI 负向
def test_static_ui_across_windows_is_penalised(monkeypatch):
    """顶部标题栏跨窗口文本不变 → 负向特征；下方对白反复更新 → 应选中对白。"""
    # 窗口时间要真正拉开（模拟十分钟视频的 6 个采样窗口）
    times_by_window = [[w * 100_000 + i * 200 for i in range(3)]
                       for w in range(6)]
    boxes: dict[int, list[OcrBox]] = {}
    for w, times in enumerate(times_by_window):
        for t in times:
            boxes[t] = [
                _box("常驻标题", y0=0.03, y1=0.10, x0=0.05, x1=0.45),
                _box(f"对白{w}{t}", y0=0.84, y1=0.93, x0=0.15, x1=0.85),
            ]
    engine = _FakeEngine(boxes)
    tracker = SubtitleRegionTracker(
        engine=engine, ffmpeg="ffmpeg", config=TrackerConfig(windows=6)
    )
    _install_fake_windows(monkeypatch, times_by_window)
    det = tracker.track("fake.mp4", duration_ms=600_000)

    assert det.best is not None
    assert det.best.y0 > 0.7, "应当选中下方对白，而不是顶部常驻标题"
    static_cands = [
        c for c in det.candidates
        if "static_ui_text" in c.evidence
    ]
    assert static_cands, "常驻标题应被判为静态界面文字"


# ---------------------------------------------------------------- T13 分时段 ROI
def test_layout_switch_produces_two_time_regions(monkeypatch):
    """前半底部、后半顶部 → 输出两套带时间范围的区域，而不是合成一个。"""
    times_by_window = [[w * 100_000 + i * 200 for i in range(3)]
                       for w in range(6)]
    boxes: dict[int, list[OcrBox]] = {}
    for w, times in enumerate(times_by_window):
        for t in times:
            if w < 3:
                boxes[t] = [_box(f"底部字幕{w}{t}", y0=0.84, y1=0.93)]
            else:
                boxes[t] = [_box(f"顶部字幕{w}{t}", y0=0.03, y1=0.11)]
    engine = _FakeEngine(boxes)
    tracker = SubtitleRegionTracker(
        engine=engine, ffmpeg="ffmpeg", config=TrackerConfig(windows=6)
    )
    _install_fake_windows(monkeypatch, times_by_window)
    det = tracker.track("fake.mp4", duration_ms=600_000)

    roles = [r.role for r in det.regions]
    assert ROLE_DIALOGUE in roles
    assert ROLE_TIMELINE in roles, "位置切换应输出独立时段的区域"
    timeline = next(r for r in det.regions if r.role == ROLE_TIMELINE)
    assert timeline.y0 < 0.2, "后半段区域应指向顶部"
    # 时间范围不重叠（相邻即可）
    main = next(r for r in det.regions if r.role == ROLE_DIALOGUE)
    assert timeline.start_ms >= main.end_ms or main.start_ms >= timeline.end_ms


def test_roi_plan_routes_by_time():
    plan = RoiPlan(
        [
            RoiSlot(0, 1000, [Roi(x0=0, y0=0.8, x1=1, y1=0.9)]),
            RoiSlot(1000, 2000, [Roi(x0=0, y0=0.0, x1=1, y1=0.1)]),
        ],
        Roi(x0=0, y0=0.8, x1=1, y1=0.9),
    )
    assert plan.rois_at(500)[0].y0 == 0.8
    assert plan.rois_at(1500)[0].y0 == 0.0
    # 落在任何 slot 之外 → 取时间最近的
    assert plan.rois_at(9999)[0].y0 == 0.0


# ---------------------------------------------------------------- T14/T15 完整性
def _segments_spanning(spans: list[tuple[int, int]]) -> list[Segment]:
    return [
        Segment(start_ms=s, end_ms=e, text="字幕", source="hard_subtitle_ocr")
        for s, e in spans
    ]


def test_continuous_speech_with_10_percent_ocr_is_not_complete():
    """十分钟连续有人讲解，只识别到 1 分钟字幕 → 不能判定为完整。"""
    pipe = Pipeline.__new__(Pipeline)  # 只需要静态方法
    segments = _segments_spanning([(0, 60_000)])
    stats = {"segment_time_coverage": 0.1, "covered_ms": 60_000,
             "frames_with_text": 60, "sampled_frames": 600}
    speech = [(0, 600_000)]  # 全片都在说话
    out = Pipeline._assess_ocr_completeness(pipe, segments, 600_000, stats, speech)
    assert out["status"] == "incomplete"
    assert out["metrics"]["speech_coverage"] < 0.2
    assert out["metrics"]["longest_speech_gap_ms"] > 400_000


def test_long_silence_but_all_speech_covered_is_complete():
    """视频大量静音，但字幕覆盖了全部有声时段 → 有证据时可判完整。"""
    pipe = Pipeline.__new__(Pipeline)
    segments = _segments_spanning([(0, 30_000), (300_000, 320_000)])
    stats = {"segment_time_coverage": 0.05, "covered_ms": 50_000,
             "frames_with_text": 50, "sampled_frames": 600}
    speech = [(0, 30_000), (300_000, 320_000)]  # 只有两段在说话
    out = Pipeline._assess_ocr_completeness(pipe, segments, 600_000, stats, speech)
    assert out["status"] == "complete"
    assert out["metrics"]["speech_coverage"] == 1.0


def test_without_speech_evidence_status_is_unknown():
    pipe = Pipeline.__new__(Pipeline)
    segments = _segments_spanning([(0, 60_000)])
    stats = {"segment_time_coverage": 0.1, "covered_ms": 60_000}
    out = Pipeline._assess_ocr_completeness(pipe, segments, 600_000, stats, [])
    assert out["status"] == "unknown"
    assert out["metrics"]["has_ocr_evidence"] is True


# ---------------------------------------------------------------- T16 路由约束
def test_source_ocr_never_runs_asr(tmp_path):
    """source=ocr：完整性不足也不能偷偷执行 ASR。"""
    h = Harness(tmp_path, platform_segments=None, ocr_segments=[],
                asr_segments=_segs())
    res = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="ocr")
    assert "asr" not in h.calls
    assert not res.ok


def test_auto_uses_asr_when_completeness_incomplete(tmp_path):
    """auto：完整性不足 → 走 ASR 补缺。"""
    h = Harness(tmp_path, platform_segments=None, ocr_segments=_segs(2),
                asr_segments=_segs(), speech=[(0, 600_000)])
    res = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="auto")
    assert "asr" in h.calls
    assert res.parts[0]["subtitle_source"] == "hard_subtitle_ocr+qwen3_asr"


# ---------------------------------------------------------------- T17 双语
def test_bilingual_two_rois_are_both_used(monkeypatch, tmp_path):
    """两行相距较远的中英字幕 → 两个 ROI；实际识别要拿到两行。"""
    ocr = SubtitleOcr(config=OcrConfig(sample_interval=1.0, min_samples=1))
    main = Roi(x0=0.20, y0=0.80, x1=0.85, y1=0.86, method="dialogue_subtitle")
    second = Roi(x0=0.20, y0=0.87, x1=0.85, y1=0.93, method="secondary_language_subtitle")
    plan = RoiPlan([RoiSlot(0, 10_000, [main, second])], main)

    frames = [tmp_path / f"f{i}.jpg" for i in range(2)]
    for f in frames:
        f.write_bytes(b"x")
    monkeypatch.setattr(ocr, "extract_frames", lambda video, out: frames)
    monkeypatch.setattr(SubtitleOcr, "_roi_change", staticmethod(lambda p, c: 1.0))


    class _Img:
        shape = (100, 100, 3)

    monkeypatch.setattr("cv2.imread", lambda path, *a, **k: _Img())
    monkeypatch.setattr(
        SubtitleOcr, "_crop_roi",
        lambda self, image, roi: np.zeros((4, 4, 3), np.uint8),
    )

    calls = {"n": 0}

    def fake_ocr_roi(self, crop, roi, frame_size=None):
        calls["n"] += 1
        # 第一个 ROI 是中文行，第二个是英文行
        return [[("中文行", 0.95)]] if roi.y0 < 0.86 else [[("English line", 0.95)]]

    monkeypatch.setattr(SubtitleOcr, "_ocr_roi", fake_ocr_roi)

    obs = ocr.observe("fake.mp4", Path("."), plan)
    assert obs[0].text
    assert "中文行" in obs[0].text
    assert "English line" in obs[0].text
    assert obs[0].lines == 2


def test_visual_text_excludes_all_confirmed_regions():
    """第二字幕行不能被误归到界面文字通道。"""
    ocr = SubtitleOcr(config=OcrConfig())
    main = Roi(x0=0.15, y0=0.825, x1=0.85, y1=0.905)

    class _Det:
        regions = [
            type("R", (), {"x0": 0.15, "y0": 0.825, "x1": 0.85, "y1": 0.905,
                           "role": "dialogue_subtitle"})(),
            type("R", (), {"x0": 0.20, "y0": 0.87, "x1": 0.85, "y1": 0.93,
                           "role": "secondary_language_subtitle"})(),
        ]
        tracks = []

    track = RegionTrack()
    track.observations.append((0, _box("English line", y0=0.87, y1=0.93)))
    _Det.tracks = [track]

    out = ocr._collect_on_screen_text(_Det(), main)
    assert out == [], "第二字幕行不应出现在界面文字通道"


# ---------------------------------------------------------------- T18 位置迁移
def test_bottom_to_top_migration_yields_two_stages(monkeypatch, tmp_path):
    """前半底部、后半顶部 → 识别时按时间切换 ROI。"""
    ocr = SubtitleOcr(config=OcrConfig(sample_interval=1.0, min_samples=1))
    bottom = Roi(x0=0.0, y0=0.80, x1=1.0, y1=0.92, method="dialogue_subtitle")
    top = Roi(x0=0.0, y0=0.02, x1=1.0, y1=0.14, method="timeline_subtitle")
    plan = RoiPlan(
        [RoiSlot(0, 1000, [bottom]), RoiSlot(1000, 2000, [top])], bottom
    )

    frames = [tmp_path / f"f{i}.jpg" for i in range(3)]
    for f in frames:
        f.write_bytes(b"x")
    monkeypatch.setattr(ocr, "extract_frames", lambda video, out: frames)
    monkeypatch.setattr(SubtitleOcr, "_roi_change", staticmethod(lambda p, c: 1.0))


    class _Img:
        shape = (100, 100, 3)

    monkeypatch.setattr("cv2.imread", lambda path, *a, **k: _Img())
    monkeypatch.setattr(
        SubtitleOcr, "_crop_roi",
        lambda self, image, roi: np.zeros((4, 4, 3), np.uint8),
    )

    def fake_ocr_roi(self, crop, roi, frame_size=None):
        if roi.y0 < 0.5:
            return [[("顶部字幕", 0.95)]]
        return [[("底部字幕", 0.95)]]

    monkeypatch.setattr(SubtitleOcr, "_ocr_roi", fake_ocr_roi)

    obs = ocr.observe("fake.mp4", Path("."), plan)
    assert obs[0].text == "底部字幕"
    assert obs[2].text == "顶部字幕"


# ---------------------------------------------------------------- T19 长句不被裁断
def test_long_sentence_not_truncated_by_envelope():
    """长短句交替时，稳健外包络要能容下长句两端。"""
    tracker = SubtitleRegionTracker(engine=None, config=TrackerConfig())
    track = RegionTrack()
    # 一条短句 + 一条很长的句子
    track.observations.append((0, _box("短", x0=0.40, x1=0.55)))
    track.observations.append((1000, _box("很长的一句字幕内容", x0=0.05, x1=0.95)))
    track.observations.append((2000, _box("短", x0=0.40, x1=0.55)))
    rect = tracker._robust_envelope([track])
    assert rect[0] <= 0.05 + 1e-6, "左边界不能切掉长句开头"
    assert rect[2] >= 0.95 - 1e-6, "右边界不能切掉长句结尾"


# ---------------------------------------------------------------- T20 UI 不污染
def test_ui_dense_video_keeps_dialogue_clean():
    """UI 密集时，对白 ROI 之外的界面文字应进独立通道，不进 transcript。"""
    ocr = SubtitleOcr(config=OcrConfig())
    dialogue = Roi(x0=0.15, y0=0.825, x1=0.85, y1=0.905)

    class _Det:
        regions = []
        tracks = []

    left = RegionTrack()
    left.observations.append((0, _box("左侧文件列表", y0=0.20, y1=0.30,
                                       x0=0.01, x1=0.20)))
    right = RegionTrack()
    right.observations.append((0, _box("右侧属性面板", y0=0.30, y1=0.40,
                                       x0=0.80, x1=0.99)))
    _Det.tracks = [left, right]

    out = ocr._collect_on_screen_text(_Det(), dialogue)
    assert len(out) == 2
    assert {t["kind"] for t in out} == {"ui_text"}
    assert all(t["bbox"][0] < 0.25 or t["bbox"][2] > 0.75 for t in out)


# ------------------------------------------------- T21/T22 manifest 诊断可观测性
def _ocr_stats_fixture() -> dict:
    """一份"像真的"的 OCR 统计：ROI 计划 + 区域检测 + 采样指标。"""
    return {
        "sampled_frames": 100,
        "frames_with_text": 90,
        "frame_errors": 1,
        "segments": 40,
        "sample_interval_s": 1.0,
        "time_precision": "sample_interval",
        "coverage_ratio": 0.81,
        "segment_time_coverage": 0.81,
        "covered_ms": 48_000,
        "span_ratio": 0.99,
        "roi": {
            "x0": 0.0, "y0": 0.80, "x1": 1.0, "y1": 0.92,
            "method": "dialogue_subtitle", "heuristic_score": 0.91,
        },
        "roi_slots": [{
            "start_ms": 0, "end_ms": 60_000,
            "rois": [{
                "x0": 0.0, "y0": 0.80, "x1": 1.0, "y1": 0.92,
                "method": "dialogue_subtitle", "heuristic_score": 0.91,
            }],
        }],
        "region_detection": {
            "regions": [{
                "x0": 0.0, "y0": 0.80, "x1": 1.0, "y1": 0.92,
                "start_ms": 0, "end_ms": 60_000,
                "role": "dialogue_subtitle", "heuristic_score": 0.91,
                "evidence": ["text_turnover"],
            }],
            "method": "temporal_tracking_v1",
            "warnings": [],
            "sampled_frames": 60,
            "ocr_calls": 60,
            "duration_ms": 60_000,
            "fallback": False,
        },
        "on_screen_text": 3,
    }


def test_manifest_records_ocr_completeness_fallback_and_roi_plan(tmp_path):
    """REVIEW2 §3.2 / R2-06：manifest 要写明完整性状态、依据、fallback 与 ROI 计划。

    修复前：这些诊断只留在内存里，manifest 里查不到，
    也就无法判断"这次 OCR 到底可不可信"。
    """
    h = Harness(tmp_path, platform_segments=None, ocr_segments=_segs(3),
                ocr_stats=_ocr_stats_fixture(), speech=[(0, 60_000)])
    res = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="ocr")
    assert res.ok, res.to_dict()

    manifest = json.loads(
        (Path(res.parts[0]["directory"]) / "manifest.json").read_text(encoding="utf-8")
    )
    ocr = manifest["extra"]["ocr"]

    # 完整性状态 + 依据 + 诊断指标
    assert ocr["completeness"]["status"] in ("complete", "incomplete", "unknown")
    assert ocr["completeness"]["reason"]
    assert "has_ocr_evidence" in ocr["completeness"]["metrics"]

    # 是否触发 fallback（显式字段，不要靠"缺失"推断）
    assert ocr["fallback"] is False

    # 采样帧数 / OCR 调用数
    assert ocr["sampled_frames"] == 100
    assert ocr["ocr_calls"] == 60

    # ROI：主 ROI + 按时间选用的计划 + 区域时间边界
    assert ocr["roi"]["heuristic_score"] == 0.91
    assert ocr["roi_slots"] and ocr["roi_slots"][0]["rois"]
    region = manifest["extra"]["ocr_region"]["regions"][0]
    assert region["start_ms"] == 0 and region["end_ms"] == 60_000

    # 时间精度要如实标注，不能宣称毫秒级
    assert ocr["time_precision"] == "sample_interval"


def test_manifest_ocr_block_absent_when_source_is_platform(tmp_path):
    """平台字幕路线不应凭空造出 OCR 诊断块。"""
    h = Harness(tmp_path, platform_segments=_segs(2))
    res = h.fetch("BV1xx411c7mD", mode="subtitle", subtitle_source="platform")
    assert res.ok
    manifest = json.loads(
        (Path(res.parts[0]["directory"]) / "manifest.json").read_text(encoding="utf-8")
    )
    assert "ocr" not in manifest["extra"]
    assert "ocr_region" not in manifest["extra"]


# ---------------------------------------------------------------- T23 L2 归并
def test_l2_merge_survives_envelope_growth(monkeypatch):
    """某个窗口出现一次偏高的框之后，后续窗口的轨道仍要并进同一个候选。

    修复前：L2 归并拿**累积外包络**的高度做字高比，
    外包络被偏高框撑大之后，后面窗口里完全正常的轨道会因为"字高比过大"被拒，
    同一条字幕带被切成两个候选，还凭空多出一个"位置变化"的区域。
    """
    times_by_window = [[0, 333, 666], [3000, 3333, 3666]]
    normal = _box("普通字幕", y0=0.83, y1=0.90, x0=0.20, x1=0.70)
    taller = _box("偏高一点", y0=0.815, y1=0.905, x0=0.20, x1=0.70)
    boxes: dict[int, list[OcrBox]] = {
        0: [normal],
        333: [normal],
        666: [taller],
        3000: [normal],
        3333: [normal],
        3666: [normal],
    }
    engine = _FakeEngine(boxes)
    tracker = SubtitleRegionTracker(
        engine=engine, ffmpeg="ffmpeg",
        config=TrackerConfig(windows=2, link_gap_ms=1600),
    )
    _install_fake_windows(monkeypatch, times_by_window)
    det = tracker.track("fake.mp4", duration_ms=10_000)

    assert len(det.candidates) == 1, (
        "同一条字幕带被切成了多个候选："
        f"{[(c.rect, round(c.heuristic_score, 3)) for c in det.candidates]}"
    )
    assert len(det.candidates[0].windows) == 2


# ------------------------------------------------------ T26 第二字幕行的判定
def _two_bands_tracker(monkeypatch, upper: OcrBox, lower: OcrBox):
    """造一个"上下两条带"的跟踪器：每条带各占一半画面高度。"""
    times_by_window = [[0, 200, 400], [3000, 3200, 3400]]
    boxes: dict[int, list[OcrBox]] = {}
    for t in times_by_window[0]:
        boxes[t] = [upper, lower]
    for t in times_by_window[1]:
        boxes[t] = [upper, lower]
    engine = _FakeEngine(boxes)
    tracker = SubtitleRegionTracker(
        engine=engine, ffmpeg="ffmpeg", config=TrackerConfig(windows=2)
    )
    _install_fake_windows(monkeypatch, times_by_window)
    return tracker


def test_distant_ui_band_is_not_promoted_to_second_subtitle_line(monkeypatch):
    """远处的界面文字带**不能**被当成"双语第二行"。

    修复前：第二行只要求"基线不同"，于是 PMX Editor 那种满屏 UI 的教程里，
    一条 UI 带会被当成第二字幕行进入 ROI 计划，
    整屏界面文字被 OCR 进对白（实测字符数从 4.2k 涨到 9.9k）。
    """
    # UI 带也放在画面中间（横向与对白带重叠），
    # 这样"是不是同一列"这种宽松条件拦不住它，只有"是否紧挨着"能拦
    upper = _box("界面文字", y0=0.62, y1=0.70, x0=0.10, x1=0.60)
    lower = _box("对白字幕", y0=0.82, y1=0.90, x0=0.15, x1=0.85)
    tracker = _two_bands_tracker(monkeypatch, upper, lower)
    det = tracker.track("fake.mp4", duration_ms=10_000)

    assert det.best is not None
    roles = [r.role for r in det.regions]
    assert ROLE_SECONDARY not in roles, (
        f"远处 UI 带被误判成第二字幕行：{[(r.role, r.y0, r.y1) for r in det.regions]}"
    )
    # 主字幕带仍然要被选出来
    assert det.best.y0 > 0.75


def test_genuine_second_line_below_main_is_promoted(monkeypatch):
    """紧挨着主行下方的第二行（双语）要能被识别为第二字幕行。"""
    upper = _box("中文行", y0=0.80, y1=0.86, x0=0.20, x1=0.80)
    lower = _box("English line", y0=0.87, y1=0.93, x0=0.20, x1=0.80)
    tracker = _two_bands_tracker(monkeypatch, upper, lower)
    det = tracker.track("fake.mp4", duration_ms=10_000)

    roles = [r.role for r in det.regions]
    assert ROLE_SECONDARY in roles, (
        f"紧邻的第二行没有被识别出来：{[(r.role, r.y0, r.y1) for r in det.regions]}"
    )


# ------------------------------------------------------------------ T27 字高判据
def _candidate(rect: tuple[float, float, float, float], box_h: float, text: str = "x"):
    """造一个区域候选：外包络是 rect，里面的字高是 box_h。"""
    from bilibili_agent.subtitle_region import RegionCandidate

    tr = RegionTrack()
    # 一条占满 rect 竖直范围、但字只有 box_h 高的轨迹
    mid = (rect[1] + rect[3]) / 2
    tr.observations.append((0, OcrBox(
        text=text, score=0.9, x0=rect[0], y0=mid - box_h / 2,
        x1=rect[2], y1=mid + box_h / 2,
    )))
    cand = RegionCandidate(tracks=[tr])
    cand.rect = rect
    return cand


def test_second_line_requires_similar_text_size():
    """界面小字（外包络很高但字很小）不能被当成第二字幕行。

    外包络高度区分不了"一条字幕行"和"一堆小字界面元素"，
    真正能区分的是**实际框的字高**：
    实测同一个教程里，字幕行中位字高 0.078，界面小字只有 0.023～0.027。
    """
    main = _candidate((0.030, 0.806, 0.968, 0.923), box_h=0.078)
    ui = _candidate((0.000, 0.620, 1.000, 0.915), box_h=0.025)
    assert not SubtitleRegionTracker._is_second_line(main, ui), (
        "界面小字被当成了第二字幕行"
    )
    # 同样字大的另一行 → 确实是第二行
    second = _candidate((0.030, 0.930, 0.968, 0.999), box_h=0.070)
    assert SubtitleRegionTracker._is_second_line(main, second)


def test_position_changed_region_requires_similar_text_size():
    """位置变了但字变小了 → 不是"字幕换位置"，是界面元素。"""
    main = _candidate((0.030, 0.806, 0.968, 0.923), box_h=0.078)
    ui = _candidate((0.100, 0.100, 0.900, 0.180), box_h=0.022)
    assert not SubtitleRegionTracker._is_position_changed(main, ui)
    moved = _candidate((0.100, 0.100, 0.900, 0.180), box_h=0.075)
    assert SubtitleRegionTracker._is_position_changed(main, moved)


# ------------------------------------------------------------- T28 ROI 内字高筛选
class _FakeOcrEngine:
    """返回固定文字框的假引擎（坐标是裁剪块内的相对坐标）。"""

    def __init__(self, boxes: list[OcrBox]):
        self._boxes = boxes

    def boxes(self, image) -> list[OcrBox]:
        return list(self._boxes)


def test_small_ui_text_inside_roi_is_filtered_out(monkeypatch):
    """ROI 内混进的界面小字（字高不到字幕一半）要被筛掉（T20）。

    软件教程里文件浏览器侧栏常常就在画面左下角，和字幕带**纵向重叠**，
    只看位置分不开，只能按字高分。
    """
    ocr = SubtitleOcr(config=OcrConfig())
    roi = Roi(x0=0.0, y0=0.80, x1=1.0, y1=0.93, text_height=0.078)
    engine = _FakeOcrEngine([
        OcrBox(text="这是字幕", score=0.95, x0=0.25, y0=0.05, x1=0.75, y1=0.75),
        OcrBox(text="Downloads", score=0.99, x0=0.03, y0=0.40, x1=0.10, y1=0.55),
    ])
    monkeypatch.setattr(ocr, "_get_engine", lambda: engine)

    out = ocr._ocr_roi(np.zeros((4, 4, 3), np.uint8), roi, frame_size=(720, 1280))
    texts = [t for line in out for t, _ in line]
    assert "这是字幕" in texts
    assert "Downloads" not in texts, "界面小字没有被筛掉"


def test_roi_without_text_height_uses_largest_box_as_reference(monkeypatch):
    """没给 text_height 时退化为"按 ROI 里最大的字高"筛。"""
    ocr = SubtitleOcr(config=OcrConfig())
    roi = Roi(x0=0.0, y0=0.80, x1=1.0, y1=0.93)
    engine = _FakeOcrEngine([
        OcrBox(text="这是字幕", score=0.95, x0=0.25, y0=0.05, x1=0.75, y1=0.75),
        OcrBox(text="Downloads", score=0.99, x0=0.03, y0=0.40, x1=0.10, y1=0.55),
    ])
    monkeypatch.setattr(ocr, "_get_engine", lambda: engine)

    out = ocr._ocr_roi(np.zeros((4, 4, 3), np.uint8), roi, frame_size=(720, 1280))
    texts = [t for line in out for t, _ in line]
    assert texts == ["这是字幕"]


# --------------------------------------------- T24 主区域时段 / T25 重复行去重
class _Region:
    """最小化的区域替身（只需要 _build_plan 用到的字段）。"""

    def __init__(self, x0, y0, x1, y1, role, start_ms, end_ms, score):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.role = role
        self.start_ms, self.end_ms = start_ms, end_ms
        self.heuristic_score = score


def test_main_slot_only_spans_its_own_active_range():
    """主区域的时段是**它自己的有效时段**，不是整片。

    修复前：主区域的 slot 覆盖 0~整片，于是"位置迁移"的区域虽然被检测出来，
    实际识别时仍然永远命中主区域 → 后半段字幕读不到（半片空白）。
    """
    ocr = SubtitleOcr(config=OcrConfig())
    main = _Region(0.10, 0.80, 0.90, 0.90, ROLE_DIALOGUE, 0, 12_000, 0.90)
    later = _Region(0.10, 0.05, 0.90, 0.15, ROLE_TIMELINE, 12_000, 24_000, 0.80)
    plan = ocr._build_plan([main, later], ocr._fallback_roi(), 24_000)

    assert plan.rois_at(5_000)[0].y0 > 0.7, "前半段应使用底部区域"
    assert plan.rois_at(15_000)[0].y0 < 0.2, "后半段应改用顶部区域"
    # 时段边界要对得上
    assert plan.slots[0].end_ms <= 12_000


def test_identical_lines_from_overlapping_rois_are_deduped():
    """两个重叠 ROI 识别出同一行时，transcript 里不能出现"句子\\n句子"。"""
    text, score, nlines = SubtitleOcr._lines_to_text([
        [("同一句字幕", 0.92)],
        [("同一句字幕", 0.81)],
        [("另一行", 0.90)],
    ])
    assert text == "同一句字幕\n另一行"
    assert nlines == 2
