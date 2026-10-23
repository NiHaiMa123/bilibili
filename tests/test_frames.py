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
