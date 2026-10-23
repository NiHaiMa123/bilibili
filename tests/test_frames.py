"""关键帧选取逻辑（离线，不依赖 ffmpeg 与真实视频）。"""

from __future__ import annotations

from bilibili_agent.frame_selector import FrameSelector, FrameSelectorConfig
from bilibili_agent.schemas import Segment


def _segments():
    return [
        Segment(start_ms=0, end_ms=2000, text="开场"),
        Segment(start_ms=5000, end_ms=7000, text="第一步"),
        Segment(start_ms=10000, end_ms=12000, text="第二步"),
    ]


def test_build_candidates_merges_scene_and_anchor():
    sel = FrameSelector(config=FrameSelectorConfig())
    cands = sel.build_candidates(
        [(3000, 0.9), (5000, 0.4)], _segments()
    )
    times = {c.time_ms: c for c in cands}
    assert 3000 in times and times[3000].reason == "scene_change"
    assert 5000 in times
    # 字幕锚点都在
    for seg in _segments():
        assert seg.start_ms in times
    # 场景变化点关联到最近的字幕段（3000ms 离 5000ms 那句最近）
    assert times[3000].linked == [1]


def test_build_candidates_links_scene_to_nearest_segment():
    sel = FrameSelector(config=FrameSelectorConfig())
    cands = sel.build_candidates([(11000, 0.8)], _segments())
    cand = next(c for c in cands if c.time_ms == 11000)
    assert cand.linked == [2]


def test_thin_respects_min_gap_and_max_images():
    cfg = FrameSelectorConfig(min_gap_seconds=5.0, max_images=3)
    sel = FrameSelector(config=cfg)
    cands = sel.build_candidates(
        [(1000, 0.9), (2000, 0.8), (3000, 0.7), (40000, 0.6)], _segments()
    )
    kept = sel.thin(cands)
    assert len(kept) <= 3
    for a, b in zip(kept, kept[1:], strict=False):
        assert abs(b.time_ms - a.time_ms) >= 5000


def test_dhash_distinguishes_images():
    sel = FrameSelector(config=FrameSelectorConfig())
    flat = bytes([100] * 72)
    # 每行水平方向递减的图案（会点亮 dHash 的比特）
    ramp = bytearray()
    for row in range(8):
        ramp.extend(range(200 - row, 200 - row - 9, -1))
    ramp = bytes(ramp)

    h_flat = sel._dhash(flat)
    h_ramp = sel._dhash(ramp)
    assert sel._hamming(h_flat, h_ramp) > 6
    assert sel._hamming(h_flat, h_flat) == 0
    assert sel._hamming(h_ramp, h_ramp) == 0


def test_select_without_ffmpeg_returns_warning(tmp_path):
    sel = FrameSelector(ffmpeg="", config=FrameSelectorConfig())
    frames, warnings = sel.select("nonexistent.mp4", _segments(), tmp_path / "assets")
    assert frames == []
    assert warnings


def test_config_suffix_mapping():
    assert FrameSelectorConfig(image_format="jpeg").suffix == "jpg"
    assert FrameSelectorConfig(image_format="png").suffix == "png"
    assert FrameSelectorConfig(image_format="webp").suffix == "webp"


# ---------------------------------------------------------------- T17 时间分桶
def test_late_segment_still_gets_frames():
    """前段 100 个场景变化 + 后段 2 个关键操作 → 后段仍要有图。"""
    cfg = FrameSelectorConfig(max_images=8, min_gap_seconds=1.0)
    sel = FrameSelector(config=cfg)
    # 前 25% 时间里有 100 个场景变化，后 75% 只有 2 个
    scene_changes = [(i * 100, 0.9) for i in range(100)]
    segments = [
        Segment(start_ms=800_000, end_ms=801_000, text="后段关键操作一"),
        Segment(start_ms=900_000, end_ms=901_000, text="后段关键操作二"),
    ]
    cands = sel.build_candidates(scene_changes, segments)
    kept = sel.thin(cands)
    assert kept
    assert len(kept) <= cfg.max_images
    late = [c for c in kept if c.time_ms >= 700_000]
    assert late, "后段必须保留至少一张关键帧"
    # 后段的字幕锚点应当被选中
    assert any(800_000 <= c.time_ms <= 901_000 for c in kept)


def test_frames_cover_whole_timeline():
    """选出的帧应分布在整条时间轴上，而不是挤在开头。"""
    cfg = FrameSelectorConfig(max_images=6, min_gap_seconds=0.5)
    sel = FrameSelector(config=cfg)
    scene_changes = [(i * 100, 0.9) for i in range(100)]  # 前 10 秒
    segments = [
        Segment(start_ms=i * 60_000, end_ms=i * 60_000 + 1000, text=f"第{i}段")
        for i in range(10)
    ]
    cands = sel.build_candidates(scene_changes, segments)
    kept = sel.thin(cands)
    times = [c.time_ms for c in kept]
    assert times == sorted(times)
    assert max(times) > 400_000, "后半段也要有帧"


def test_thin_respects_max_images():
    cfg = FrameSelectorConfig(max_images=5, min_gap_seconds=0.0)
    sel = FrameSelector(config=cfg)
    cands = sel.build_candidates([(i * 100, 0.5) for i in range(50)], [])
    kept = sel.thin(cands)
    assert len(kept) <= 5
