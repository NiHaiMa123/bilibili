"""关键帧选取。

目标不是"每隔 10 秒放一张"，而是让 Agent 看到视频里真正讲解的步骤、
界面、图表。首版用确定性、可控成本的做法：

1. FFmpeg 场景切换检测（``select='gt(scene,...)'``）拿到视觉变化点；
2. 以字幕/转写的时间锚补充画面稳定但有讲解的时段；
3. 过滤黑帧与几乎重复的帧（dHash 感知哈希）；
4. 记录每张图的时间戳、原因与关联字幕段。

不依赖付费视觉 API，也不做需要 Agent 逐帧主观判断的大模型流水线。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schemas import Frame, Segment

_PTS_RE = re.compile(r"pts_time:([0-9.]+)")
_SCENE_RE = re.compile(r"lavfi\.scene_score=([0-9.]+)")

IMAGE_SUFFIX = {"jpg": "jpg", "jpeg": "jpg", "webp": "webp", "png": "png"}


@dataclass(slots=True)
class FrameSelectorConfig:
    max_images: int = 25
    min_gap_seconds: float = 3.0
    max_edge: int = 1280
    image_format: str = "jpg"
    phash_distance: int = 6
    scene_threshold: float = 0.30
    black_threshold: int = 16
    quality: int = 3

    @property
    def suffix(self) -> str:
        return IMAGE_SUFFIX.get(self.image_format.lower(), "jpg")


@dataclass(slots=True)
class Candidate:
    time_ms: int
    reason: str
    score: float = 0.0
    linked: list[int] = field(default_factory=list)


class FrameSelector:
    """从一个视频文件里挑出有信息量的关键帧。"""

    def __init__(self, ffmpeg: str = "", config: FrameSelectorConfig | None = None) -> None:
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""
        self.config = config or FrameSelectorConfig()

    # -- 探测 -----------------------------------------------------------
    def detect_scene_changes(self, video: str | Path, limit: int = 400) -> list[tuple[int, float]]:
        """返回 [(时间毫秒, 场景分)]。"""
        if not self.ffmpeg:
            return []
        cmd = [
            self.ffmpeg, "-hide_banner", "-nostats", "-i", str(video),
            "-an", "-vf",
            f"select='gt(scene,{self.config.scene_threshold})',metadata=print:file=-",
            "-f", "null", "-",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=1800)
        except (subprocess.SubprocessError, OSError):
            return []
        out = proc.stdout or ""
        times = [float(m) for m in _PTS_RE.findall(out)]
        scores = [float(m) for m in _SCENE_RE.findall(out)]
        pairs: list[tuple[int, float]] = []
        for i, t in enumerate(times):
            score = scores[i] if i < len(scores) else 0.0
            pairs.append((int(round(t * 1000)), score))
        pairs.sort(key=lambda x: x[1], reverse=True)
        return pairs[:limit]

    # -- 候选 -----------------------------------------------------------
    def build_candidates(
        self,
        scene_changes: list[tuple[int, float]],
        segments: list[Segment],
    ) -> list[Candidate]:
        """合并场景变化点与字幕时间锚，按时间排序并去重。"""
        candidates: dict[int, Candidate] = {}

        for time_ms, score in scene_changes:
            cand = candidates.setdefault(time_ms, Candidate(time_ms, "scene_change"))
            cand.score = max(cand.score, score)

        # 字幕锚：取每条字幕的起始点（讲解发生的那一刻）
        for idx, seg in enumerate(segments):
            time_ms = seg.start_ms
            cand = candidates.get(time_ms)
            if cand is None:
                cand = Candidate(time_ms, "transcript_anchor")
                candidates[time_ms] = cand
            elif cand.reason == "scene_change":
                cand.reason = "scene_change"
            cand.linked.append(idx)

        # 关联最近的字幕段
        for cand in candidates.values():
            if cand.linked:
                continue
            best = None
            best_delta = None
            for idx, seg in enumerate(segments):
                delta = abs(seg.start_ms - cand.time_ms)
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best = idx
            if best is not None and best_delta is not None and best_delta <= 60_000:
                cand.linked.append(best)

        ordered = sorted(candidates.values(), key=lambda c: (-c.score, c.time_ms))
        # 场景变化优先保留，再按时间补齐
        ordered.sort(key=lambda c: (0 if c.reason == "scene_change" else 1, c.time_ms))
        return ordered

    def thin(self, candidates: list[Candidate]) -> list[Candidate]:
        """按最小间隔抽稀，并限制总数。"""
        gap_ms = int(self.config.min_gap_seconds * 1000)
        kept: list[Candidate] = []
        for cand in candidates:
            if kept and abs(cand.time_ms - kept[-1].time_ms) < gap_ms:
                # 保留分更高的那个
                if cand.score > kept[-1].score:
                    kept[-1] = cand
                continue
            kept.append(cand)
            if len(kept) >= self.config.max_images:
                break
        return kept

    # -- 抽帧与去重 -----------------------------------------------------
    def _extract(self, video: str | Path, time_ms: int, out_path: Path) -> bool:
        if not self.ffmpeg:
            return False
        max_edge = self.config.max_edge
        vf = (
            f"scale='if(gt(iw,ih),min({max_edge},iw),-2)':"
            f"'if(gt(iw,ih),-2,min({max_edge},ih))'"
        )
        cmd = [
            self.ffmpeg, "-hide_banner", "-nostats", "-y",
            "-ss", f"{time_ms / 1000:.3f}", "-i", str(video),
            "-frames:v", "1", "-vf", vf,
            "-q:v", str(self.config.quality), str(out_path),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=300)
        except (subprocess.SubprocessError, OSError):
            return False
        return proc.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0

    def _gray_signature(self, image: Path) -> tuple[bytes, float] | None:
        """取 9x8 灰度图，返回 (原始字节, 平均亮度)。"""
        if not self.ffmpeg:
            return None
        cmd = [
            self.ffmpeg, "-hide_banner", "-nostats", "-i", str(image),
            "-vf", "scale=9:8,format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
        except (subprocess.SubprocessError, OSError):
            return None
        data = proc.stdout or b""
        if len(data) < 72:
            return None
        return data[:72], sum(data[:72]) / 72.0

    @staticmethod
    def _dhash(sig: bytes) -> int:
        """9x8 灰度图的差值哈希（64 bit）。"""
        bits = 0
        for row in range(8):
            base = row * 9
            for col in range(8):
                bits <<= 1
                if sig[base + col] > sig[base + col + 1]:
                    bits |= 1
        return bits

    @staticmethod
    def _hamming(a: int, b: int) -> int:
        return bin(a ^ b).count("1")

    # -- 主流程 ---------------------------------------------------------
    def select(
        self,
        video: str | Path,
        segments: list[Segment],
        assets_dir: Path,
    ) -> tuple[list[Frame], list[str]]:
        """挑关键帧并写入 ``assets_dir``，返回 (frames, warnings)。"""
        warnings: list[str] = []
        if not self.ffmpeg:
            return [], ["未找到 ffmpeg，无法抽帧"]
        video = Path(video)
        if not video.is_file():
            return [], [f"视频文件不存在：{video}"]

        assets_dir = Path(assets_dir)
        assets_dir.mkdir(parents=True, exist_ok=True)

        scene_changes = self.detect_scene_changes(video)
        candidates = self.build_candidates(scene_changes, segments)
        kept = self.thin(candidates)

        frames: list[Frame] = []
        hashes: list[int] = []
        for cand in kept:
            suffix = self.config.suffix
            name = f"frame_{cand.time_ms:09d}.{suffix}"
            out_path = assets_dir / name
            if not self._extract(video, cand.time_ms, out_path):
                warnings.append(f"抽帧失败：{cand.time_ms}ms")
                continue

            sig = self._gray_signature(out_path)
            if sig is None:
                frames.append(Frame(
                    time_ms=cand.time_ms,
                    relative_path=f"assets/{name}",
                    reason=cand.reason,
                    linked_segment_ids=list(cand.linked),
                ))
                continue

            raw, brightness = sig
            if brightness < self.config.black_threshold:
                # 黑帧/转场帧：丢掉
                out_path.unlink(missing_ok=True)
                continue

            h = self._dhash(raw)
            if any(self._hamming(h, prev) <= self.config.phash_distance for prev in hashes):
                out_path.unlink(missing_ok=True)
                continue
            hashes.append(h)

            frames.append(Frame(
                time_ms=cand.time_ms,
                relative_path=f"assets/{name}",
                reason=cand.reason,
                linked_segment_ids=list(cand.linked),
            ))

        if not frames and kept:
            warnings.append("候选帧全部被黑帧/重复帧过滤掉")
        return frames, warnings

    def describe(self) -> dict[str, Any]:
        return {
            "ffmpeg": self.ffmpeg,
            "max_images": self.config.max_images,
            "min_gap_seconds": self.config.min_gap_seconds,
            "max_edge": self.config.max_edge,
            "format": self.config.suffix,
            "phash_distance": self.config.phash_distance,
            "scene_threshold": self.config.scene_threshold,
        }
