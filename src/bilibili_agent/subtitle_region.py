"""O1：时序字幕区域跟踪。

思路（对照 docs/REVIEW_FIX_OCR.md 的 O1-1）：

1. **按时间分层**取多个短窗口（开头/中间/末尾），每个窗口连续采若干秒，
   而不是全片只均匀抽 12 张——后者会漏掉"中途换了字幕布局"的片段。
2. 每个采样帧做**整帧 OCR**，保留四维 box、文字、置信度、时间、字高。
3. 用**空间重合 + 位置稳定性**把跨帧的文字框关联成 region track；
   同一条轨迹允许文字更新（字幕本来就是一句一句换的）。
4. 轨迹聚合成一到多个四维 ROI；主字幕区与中英双行区可以同时给出。
5. 选 ROI 时用**可解释特征**打分：空间位置/居中（底部只是软先验）、
   行高与样式稳定、连续时段出现、内容按句替换；
   永久 UI 标题/水印/菜单与瞬时弹窗作为负向特征。
6. 返回 score + evidence；分数低时**不硬猜**，交回调用方 fallback。

注意：``heuristic_score`` 是启发式打分，**不是**经过校准的概率。
"""

from __future__ import annotations

import statistics
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ocr_engine import OcrBox, OcrEngine

# 角色
ROLE_DIALOGUE = "dialogue_subtitle"
ROLE_SECONDARY = "secondary_language_subtitle"
ROLE_ON_SCREEN = "on_screen_text"


@dataclass(slots=True)
class TrackerConfig:
    """跟踪参数。"""

    windows: int = 6  # 时间分层的窗口数
    window_seconds: float = 6.0  # 每个窗口的长度（秒）
    fps: float = 3.0  # 窗口内采样帧率
    max_width: int = 960  # 整帧 OCR 前的缩放宽度
    max_center_distance: float = 0.06  # 框中心最大可接受间距（相对画面）
    max_height_ratio: float = 1.35  # 字高比例上限（判断是不是同一行）
    link_gap_ms: int = 1600  # 超过这个间隔就不认为是同一条轨迹
    min_score: float = 0.35  # 低于此分认为"没把握"，交给调用方 fallback
    static_text_ratio: float = 0.6  # 同一文字占比超过它且持续很久 → 判为界面静态文字
    static_min_duration_ms: int = 20_000  # 持续这么久才算"永久" UI


@dataclass(slots=True)
class RegionTrack:
    """一条跨帧的文字轨迹。"""

    observations: list[tuple[int, OcrBox]] = field(default_factory=list)
    windows: set[int] = field(default_factory=set)

    # -- 几何 -----------------------------------------------------------
    def rect(self) -> tuple[float, float, float, float]:
        """轨迹的聚合 ROI（各框的中位数，抗个别离群框）。"""
        if not self.observations:
            return (0.0, 0.0, 0.0, 0.0)
        xs0 = [b.x0 for _, b in self.observations]
        ys0 = [b.y0 for _, b in self.observations]
        xs1 = [b.x1 for _, b in self.observations]
        ys1 = [b.y1 for _, b in self.observations]
        return (
            statistics.median(xs0), statistics.median(ys0),
            statistics.median(xs1), statistics.median(ys1),
        )

    def height(self) -> float:
        _, _, _, y1 = self.rect()
        _, y0, _, _ = self.rect()
        return max(y1 - y0, 0.0)

    def height_stability(self) -> float:
        """字高稳定度 0~1（越小越稳）。"""
        heights = [b.h for _, b in self.observations]
        if len(heights) < 2:
            return 1.0
        mean = sum(heights) / len(heights)
        if mean <= 0:
            return 1.0
        return min(1.0, statistics.pstdev(heights) / mean)

    # -- 时间与文本 ------------------------------------------------------
    @property
    def start_ms(self) -> int:
        return self.observations[0][0] if self.observations else 0

    @property
    def end_ms(self) -> int:
        return self.observations[-1][0] if self.observations else 0

    def text_sequence(self) -> list[str]:
        """按时间序的文本序列（相邻重复合并）。"""
        seq: list[str] = []
        for _, box in self.observations:
            if not seq or seq[-1] != box.text:
                seq.append(box.text)
        return seq

    def distinct_texts(self) -> int:
        return len({b.text for _, b in self.observations})

    def dominant_text_ratio(self) -> float:
        """出现最多的那个文字占全部观测的比例。"""
        if not self.observations:
            return 0.0
        counts: dict[str, int] = {}
        for _, box in self.observations:
            counts[box.text] = counts.get(box.text, 0) + 1
        return max(counts.values()) / len(self.observations)


@dataclass(slots=True)
class SubtitleRegion:
    """一个四维字幕 ROI（可带时间区间）。"""

    x0: float
    y0: float
    x1: float
    y1: float
    start_ms: int = 0
    end_ms: int = 0
    role: str = ROLE_DIALOGUE
    heuristic_score: float = 0.0
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "x0": round(self.x0, 4),
            "y0": round(self.y0, 4),
            "x1": round(self.x1, 4),
            "y1": round(self.y1, 4),
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "role": self.role,
            "heuristic_score": round(self.heuristic_score, 4),
            "evidence": self.evidence,
        }


@dataclass(slots=True)
class RegionDetection:
    regions: list[SubtitleRegion] = field(default_factory=list)
    method: str = "temporal_tracking_v1"
    warnings: list[str] = field(default_factory=list)
    tracks: list[RegionTrack] = field(default_factory=list)
    sampled_frames: int = 0
    ocr_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "regions": [r.to_dict() for r in self.regions],
            "method": self.method,
            "warnings": list(self.warnings),
            "sampled_frames": self.sampled_frames,
            "ocr_calls": self.ocr_calls,
        }

    @property
    def best(self) -> SubtitleRegion | None:
        return self.regions[0] if self.regions else None


class SubtitleRegionTracker:
    """在视频上跟踪出可能的字幕 ROI。"""

    def __init__(
        self,
        engine: OcrEngine,
        ffmpeg: str = "",
        config: TrackerConfig | None = None,
    ) -> None:
        self.engine = engine
        self.ffmpeg = ffmpeg
        self.config = config or TrackerConfig()

    # -- 抽帧 -----------------------------------------------------------
    def _window_frames(
        self, video: str | Path, start_s: float, length_s: float, out_dir: Path
    ) -> list[tuple[int, Path]]:
        """抽一个时间窗口内的帧，返回 [(时间毫秒, 路径)]。"""
        if not self.ffmpeg:
            return []
        out_dir.mkdir(parents=True, exist_ok=True)
        pattern = str(out_dir / "w_%06d.jpg")
        cmd = [
            self.ffmpeg, "-hide_banner", "-nostats", "-y",
            "-ss", f"{start_s:.3f}", "-t", f"{length_s:.3f}",
            "-i", str(video),
            "-vf", f"fps={self.config.fps:.4f}",
            "-q:v", "4",
            pattern,
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800)
        except (subprocess.SubprocessError, OSError):
            return []
        frames = sorted(out_dir.glob("w_*.jpg"))
        step = 1000.0 / max(self.config.fps, 0.1)
        return [
            (int(start_s * 1000) + int(i * step), p)
            for i, p in enumerate(frames)
        ]

    # -- 关联 ------------------------------------------------------------
    def _link(self, tracks: list[RegionTrack], time_ms: int, box: OcrBox) -> RegionTrack:
        """把一个文字框挂到已有轨迹，或新建轨迹。返回所属轨迹。"""
        best: RegionTrack | None = None
        best_dist = 1e9
        for track in tracks:
            last_time, last_box = track.observations[-1]
            if time_ms - last_time > self.config.link_gap_ms:
                continue
            dist = abs(last_box.cy - box.cy) + abs(last_box.cx - box.cx) * 0.5
            if dist > self.config.max_center_distance:
                continue
            h_ratio = (
                box.h / last_box.h if last_box.h > 0.01 else 10.0
            )
            if h_ratio > self.config.max_height_ratio or h_ratio < 1 / self.config.max_height_ratio:
                continue
            if dist < best_dist:
                best = track
                best_dist = dist
        if best is None:
            best = RegionTrack()
            tracks.append(best)
        best.observations.append((time_ms, box))
        return best

    # -- 打分 ------------------------------------------------------------
    def _score(self, track: RegionTrack) -> tuple[float, list[str]]:
        """可解释的打分。返回 (分, 证据列表)。"""
        if not track.observations:
            return 0.0, []
        x0, y0, x1, y1 = track.rect()
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        height = y1 - y0
        evidence: list[str] = []
        score = 0.0

        # 空间位置：底部是**软先验**，不是硬规则
        if cy > 0.60:
            score += 0.12
            evidence.append("bottom_soft_prior")
        elif cy > 0.40:
            score += 0.04

        # 水平居中
        centered = 1.0 - min(1.0, abs(cx - 0.5) / 0.4)
        score += 0.15 * centered
        if centered > 0.6:
            evidence.append("horizontally_centered")

        # 字高
        score += 0.20 * min(1.0, height / 0.06)
        if height >= 0.03:
            evidence.append("large_text")

        # 样式稳定
        stability = 1.0 - track.height_stability()
        score += 0.10 * stability
        if stability > 0.6:
            evidence.append("line_style_stable")

        # 内容按句替换（字幕的关键特征）
        if track.distinct_texts() >= 2:
            score += 0.25
            evidence.append("text_turnover")
        elif track.distinct_texts() == 1 and len(track.observations) > 1:
            score -= 0.05

        # 连续时段出现
        if len(track.windows) >= 3:
            score += 0.10
            evidence.append("persists_across_windows")
        elif len(track.windows) == 1 and len(track.observations) <= 2:
            # 瞬时弹窗
            score -= 0.12
            evidence.append("transient_popup")

        # 永久 UI 文字：同一句话长期不变
        duration = track.end_ms - track.start_ms
        if (
            track.dominant_text_ratio() >= self.config.static_text_ratio
            and duration >= self.config.static_min_duration_ms
        ):
            score -= 0.35
            evidence.append("static_ui_text")

        return max(0.0, min(1.0, score)), evidence

    # -- 主流程 ----------------------------------------------------------
    def track(self, video: str | Path, duration_ms: int | None = None) -> RegionDetection:
        detection = RegionDetection()
        if not self.ffmpeg:
            detection.warnings.append("未找到 ffmpeg，无法做字幕区跟踪")
            return detection

        total_s = (duration_ms or 0) / 1000.0
        if total_s <= 0:
            total_s = self._probe_duration(video)
        if total_s <= 0:
            detection.warnings.append("无法确定视频时长，跳过字幕区跟踪")
            return detection

        cfg = self.config
        tracks: list[RegionTrack] = []
        with tempfile.TemporaryDirectory(prefix="bili-region-") as tmp:
            for w in range(cfg.windows):
                # 窗口铺满整条时间轴，均匀分层
                start_s = max(0.0, (total_s - cfg.window_seconds) * w / max(cfg.windows - 1, 1))
                frames = self._window_frames(
                    video, start_s, cfg.window_seconds, Path(tmp) / f"win{w}"
                )
                for time_ms, path in frames:
                    image = self._read_image(path)
                    if image is None:
                        continue
                    detection.sampled_frames += 1
                    for box in self.engine.boxes(image):
                        track = self._link(tracks, time_ms, box)
                        track.windows.add(w)

        detection.tracks = tracks
        detection.ocr_calls = self.engine.calls

        scored: list[tuple[float, list[str], RegionTrack]] = []
        for track in tracks:
            if len(track.observations) < 2:
                continue
            score, evidence = self._score(track)
            scored.append((score, evidence, track))
        scored.sort(key=lambda x: -x[0])

        if not scored:
            detection.warnings.append("没有追踪到稳定的文字轨迹")
            return detection

        top_score, top_evidence, top_track = scored[0]
        if top_score < cfg.min_score:
            detection.warnings.append(
                f"字幕区启发式得分 {top_score:.2f} 低于阈值 {cfg.min_score:.2f}，"
                "交回旧检测器兜底"
            )
            return detection

        x0, y0, x1, y1 = top_track.rect()
        detection.regions.append(SubtitleRegion(
            x0=x0, y0=y0, x1=x1, y1=y1,
            start_ms=top_track.start_ms, end_ms=top_track.end_ms,
            role=ROLE_DIALOGUE,
            heuristic_score=top_score,
            evidence=top_evidence,
        ))

        # 第二行（双语）：几何接近但基线不同、且内容也在变
        for score, evidence, track in scored[1:]:
            if score < cfg.min_score * 0.8:
                break
            tx0, ty0, tx1, ty1 = track.rect()
            same_column = abs((tx0 + tx1) / 2 - (x0 + x1) / 2) < 0.25
            different_line = abs((ty0 + ty1) / 2 - (y0 + y1) / 2) > 0.04
            if same_column and different_line:
                detection.regions.append(SubtitleRegion(
                    x0=tx0, y0=ty0, x1=tx1, y1=ty1,
                    start_ms=track.start_ms, end_ms=track.end_ms,
                    role=ROLE_SECONDARY,
                    heuristic_score=score,
                    evidence=evidence,
                ))
                break

        return detection

    # -- 辅助 ------------------------------------------------------------
    def _probe_duration(self, video: str | Path) -> float:
        import json
        import shutil

        ffprobe = shutil.which("ffprobe") or ""
        if not ffprobe:
            return 0.0
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json",
                 "-show_format", str(video)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60,
            )
            data = json.loads(proc.stdout or "{}")
            return float((data.get("format") or {}).get("duration") or 0.0)
        except (subprocess.SubprocessError, OSError, ValueError):
            return 0.0

    def _read_image(self, path: Path):
        try:
            import cv2
        except ImportError:  # pragma: no cover
            return None
        img = cv2.imread(str(path))
        if img is None:
            return None
        h, w = img.shape[:2]
        ratio = min(1.0, self.config.max_width / max(w, 1))
        if ratio < 1.0:
            img = cv2.resize(
                img, (max(int(w * ratio), 1), max(int(h * ratio), 1)),
                interpolation=cv2.INTER_AREA,
            )
        return img
