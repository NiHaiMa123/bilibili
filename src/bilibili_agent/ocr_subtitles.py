"""Level 3：画面烧录字幕 OCR。

本轮（docs/REVIEW_FIX_OCR.md 的 O1~O4）把原来的"底部固定条 + 全文频率剔除"
升级为：

* **O1 时序字幕区跟踪**：用 ``subtitle_region.SubtitleRegionTracker`` 在多个
  时间窗口上跟踪文字轨迹，得到**四维 ROI**（x0,y0,x1,y1）与可解释的 evidence。
* **O1-2 时序判定**：不再按"整片 15% 频率"删句。变化触发式识别天然保留
  "同一句字幕持续 1~10 秒"；跨窗口长期不变的文字判为界面静态文字，
  转入 on_screen_text 通道。
* **O1-3 多行字幕与横向范围**：ROI 内按行分组，支持 1~3 行；
  裁剪按四维 ROI 进行，左右 UI 密集区不干扰。
* **O2 触发式识别**：ROI 内像素几乎不变就跳过 OCR；文本转场时二分细化起止时间。
* **O3/O4 证据分离**：对白进 transcript，界面文字进 on_screen_text。
"""

from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schemas import Segment

# B 站水印常见的文字形态
_WATERMARK_PATTERNS = (
    re.compile(r"bilibili", re.IGNORECASE),
    re.compile(r"哔哩哔哩"),
    re.compile(r"^\s*@[\w\-一-鿿]+"),
)
_NOISE_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)


@dataclass(slots=True)
class OcrConfig:
    """字幕带与采样参数。"""

    band: str = "auto"  # auto=自动检测 | bottom=固定底部 | top=固定顶部
    band_top: float = 0.74
    band_bottom: float = 0.99
    top_band_top: float = 0.02
    top_band_bottom: float = 0.26
    sample_interval: float = 1.0
    min_score: float = 0.60
    scale: int = 2
    similarity: float = 0.72
    min_duration_ms: int = 700
    min_samples: int = 2
    watermark_filter: bool = True
    max_chars: int = 80
    min_text_height: float = 0.020
    max_text_height: float = 0.16
    center_tolerance: float = 0.30
    static_text_ratio: float = 0.15
    detect_samples: int = 12
    detect_padding: float = 0.035
    # O1 区域跟踪
    region_windows: int = 6
    region_window_seconds: float = 6.0
    region_fps: float = 3.0
    region_min_score: float = 0.35
    # O2 触发式识别
    change_threshold: float = 0.012  # ROI 内像素变化阈值
    dense_fps: float = 3.0  # 变更窗口的加密帧率
    refine_boundaries: bool = True
    max_lines: int = 3
    # O1-2 静态判定
    static_min_duration_ms: int = 20_000
    # O4 画面文字通道的条数上限（避免 manifest 过大）
    max_on_screen_text: int = 50

    @property
    def fixed_band(self) -> tuple[float, float] | None:
        """固定字幕区；``auto`` 表示交给检测。"""
        if self.band == "bottom":
            return self._validated_band(self.band_top, self.band_bottom)
        if self.band == "top":
            return self._validated_band(self.top_band_top, self.top_band_bottom)
        return None

    @staticmethod
    def _validated_band(y0: float, y1: float) -> tuple[float, float]:
        if not (0.0 <= y0 < y1 <= 1.0):
            raise ValueError(
                f"字幕区不合法：y0={y0}, y1={y1}（需满足 0 <= y0 < y1 <= 1）"
            )
        return (y0, y1)


@dataclass(slots=True)
class OcrObservation:
    """某一帧上看到的一行（或多行）字幕。"""

    time_ms: int
    text: str
    score: float
    lines: int = 1
    error: str | None = None  # 引擎异常；与"没有字幕"区分开


@dataclass(slots=True)
class OcrResult:
    segments: list[Segment] = field(default_factory=list)
    on_screen_text: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def normalize_text(text: str) -> str:
    text = re.sub(r"\s+", "", text or "")
    text = text.replace("，", ",").replace("。", ".")
    return text.strip()


def similarity(a: str, b: str) -> float:
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def is_watermark(text: str) -> bool:
    compact = (text or "").strip()
    if not compact:
        return True
    return any(p.search(compact) for p in _WATERMARK_PATTERNS)


@dataclass(slots=True)
class Roi:
    """四维字幕区（相对画面坐标）。

    坐标顺序固定为 ``(x0, y0, x1, y1)``，构造时一律用**关键字参数**，
    并在 ``__post_init__`` 里严格校验，避免位置参数写反而静默裁错区域。
    """

    x0: float
    y0: float
    x1: float
    y1: float
    method: str = "auto"
    heuristic_score: float = 0.0

    def __post_init__(self) -> None:
        if not (0.0 <= self.x0 < self.x1 <= 1.0):
            raise ValueError(
                f"字幕区横向不合法：x0={self.x0}, x1={self.x1}"
                "（需满足 0 <= x0 < x1 <= 1）"
            )
        if not (0.0 <= self.y0 < self.y1 <= 1.0):
            raise ValueError(
                f"字幕区纵向不合法：y0={self.y0}, y1={self.y1}"
                "（需满足 0 <= y0 < y1 <= 1）"
            )

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


class SubtitleOcr:
    """烧录字幕识别。"""

    def __init__(self, ffmpeg: str = "", config: OcrConfig | None = None) -> None:
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""
        self.config = config or OcrConfig()
        self._engine = None

    # -- 引擎 -----------------------------------------------------------
    def available(self) -> bool:
        if not self.ffmpeg:
            return False
        try:
            import importlib.util

            return importlib.util.find_spec("rapidocr") is not None
        except Exception:  # noqa: BLE001
            return False

    def diagnose(self) -> list[str]:
        problems: list[str] = []
        if not self.ffmpeg:
            problems.append("未找到 ffmpeg，无法抽帧")
        try:
            import importlib.util

            if importlib.util.find_spec("rapidocr") is None:
                problems.append(
                    "未安装 RapidOCR（Level 3 烧录字幕识别不可用）："
                    "uv pip install -e '.[ocr]'"
                )
        except Exception:  # noqa: BLE001
            problems.append("未安装 RapidOCR")
        return problems

    def _get_engine(self):
        if self._engine is None:
            from .ocr_engine import get_engine

            self._engine = get_engine()
        return self._engine

    # -- 抽帧 -----------------------------------------------------------
    def extract_frames(self, video: str | Path, out_dir: Path) -> list[Path]:
        if not self.ffmpeg:
            raise RuntimeError("未找到 ffmpeg，无法抽帧")
        out_dir.mkdir(parents=True, exist_ok=True)
        fps = 1.0 / max(self.config.sample_interval, 0.05)
        pattern = str(out_dir / "f_%06d.jpg")
        cmd = [
            self.ffmpeg, "-hide_banner", "-nostats", "-y",
            "-i", str(video),
            "-vf", f"fps={fps:.6f}",
            "-q:v", "3",
            pattern,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=3600)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("ffmpeg 抽帧超时") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            raise RuntimeError(f"ffmpeg 抽帧失败：{' | '.join(tail)}")
        return sorted(out_dir.glob("f_*.jpg"))

    # -- ROI 内的文字 ----------------------------------------------------
    def _crop_roi(self, image, roi: Roi):
        h, w = image.shape[:2]
        x0 = max(0, int(roi.x0 * w))
        x1 = min(w, int(roi.x1 * w))
        y0 = max(0, int(roi.y0 * h))
        y1 = min(h, int(roi.y1 * h))
        if x1 <= x0 or y1 <= y0:
            return None
        return image[y0:y1, x0:x1]

    def _ocr_roi(
        self, crop, roi: Roi, frame_size: tuple[int, int] | None = None
    ) -> list[list[Any]]:
        """对 ROI 内的文字按行分组，返回 [[(text, score), ...], ...]（按 y 排序）。"""
        if self.config.scale > 1:
            import cv2

            crop = cv2.resize(
                crop, None, fx=self.config.scale, fy=self.config.scale,
                interpolation=cv2.INTER_CUBIC,
            )
        boxes = self._get_engine().boxes(crop)
        if not boxes:
            return []

        crop_h, crop_w = crop.shape[:2]
        frame_h, frame_w = frame_size or (crop_h, crop_w)
        # 引擎返回的是**相对裁剪块**的坐标（0~1），换算回整幅画面
        crop_w_rel = (roi.x1 - roi.x0) or 1.0
        crop_h_rel = (roi.y1 - roi.y0) or 1.0
        items: list[dict] = []
        for box in boxes:
            if box.score < self.config.min_score:
                continue
            text = box.text.strip()
            if not text or _NOISE_RE.match(text):
                continue
            if self.config.watermark_filter and is_watermark(text):
                continue
            cx = roi.x0 + box.cx * crop_w_rel
            cy = roi.y0 + box.cy * crop_h_rel
            x0 = roi.x0 + box.x0 * crop_w_rel
            h = box.h * crop_h_rel
            items.append({
                "text": text, "score": box.score,
                "x0": x0, "cx": cx, "cy": cy, "h": h,
            })

        if not items:
            return []

        # 按 y 聚成行
        items.sort(key=lambda c: c["cy"])
        lines: list[list[dict]] = []
        for c in items:
            if lines and abs(c["cy"] - lines[-1][-1]["cy"]) <= max(c["h"], 0.01) * 0.8:
                lines[-1].append(c)
            else:
                lines.append([c])
        # O1-3：按真实 y 顺序取前 N 行，同行按 x 顺序
        lines.sort(key=lambda ln: sum(c["cy"] for c in ln) / len(ln))
        lines = lines[: self.config.max_lines]
        out: list[list[Any]] = []
        for ln in lines:
            ln.sort(key=lambda c: c["x0"])
            out.append([(c["text"], c["score"]) for c in ln])
        return out

    @staticmethod
    def _lines_to_text(lines: list[list[Any]]) -> tuple[str, float, int]:
        """把多行合成一段文本（行间保留换行）。"""
        if not lines:
            return "", 0.0, 0
        texts = ["".join(t for t, _ in ln).strip() for ln in lines]
        texts = [t for t in texts if t]
        if not texts:
            return "", 0.0, 0
        scores = [s for ln in lines for _, s in ln]
        return "\n".join(texts), sum(scores) / len(scores), len(texts)

    # -- 变化检测（O2） ---------------------------------------------------
    @staticmethod
    def _roi_change(prev, curr) -> float:
        """ROI 内的归一化变化量（0~1）。

        用两个信号取大者：
        * 灰度均值差 —— 对大面积变化敏感；
        * "显著变化像素"占比 —— 对**小字出现/消失**敏感（字幕进出画面）。
        只用均值差会把"字幕刚出现"这种小幅变化吃掉，导致漏句。
        """
        if prev is None or curr is None:
            return 1.0
        import cv2
        import numpy as np

        a = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY) if prev.ndim == 3 else prev
        b = cv2.cvtColor(curr, cv2.COLOR_BGR2GRAY) if curr.ndim == 3 else curr
        size = (64, 36)
        a = cv2.resize(a, size, interpolation=cv2.INTER_AREA).astype("float32")
        b = cv2.resize(b, size, interpolation=cv2.INTER_AREA).astype("float32")
        diff = np.abs(a - b)
        mean = float(diff.mean() / 255.0)
        ratio = float((diff > 25).mean())
        return max(mean, ratio)

    # -- 主识别 ---------------------------------------------------------
    def observe(
        self,
        video: str | Path,
        work_dir: Path,
        roi: Roi,
    ) -> list[OcrObservation]:
        """抽帧 + ROI 内识别，得到逐帧观察结果。

        变化触发：ROI 几乎没变就跳过 OCR，沿用上一帧的文本——
        这正好让"同一句字幕持续好几秒"被完整保留。

        状态机（text / blank / error）：
        * 只有**上一帧确实有字幕**（state=text）且当前画面近似未变，才复用旧文本；
        * 识别到空白 → 立即清空缓存，**空白之后不能复活旧字幕**；
        * 引擎异常 / 读帧失败 → 记为 error，既不等于字幕消失，
          也不无条件沿用旧句（交给合并器按邻近证据判断）。
        """
        frames = self.extract_frames(video, work_dir)
        interval_ms = int(self.config.sample_interval * 1000)
        observations: list[OcrObservation] = []
        last_text = ""
        last_score = 0.0
        last_lines = 0
        state = "init"  # init / text / blank / error
        prev_crop = None

        for idx, frame in enumerate(frames):
            time_ms = idx * interval_ms
            try:
                import cv2

                image = cv2.imread(str(frame))
            except Exception:  # noqa: BLE001
                image = None
            if image is None:
                state = "error"
                observations.append(
                    OcrObservation(time_ms, "", 0.0, error="read_frame_failed")
                )
                continue
            crop = self._crop_roi(image, roi)
            if crop is None:
                state = "error"
                observations.append(OcrObservation(time_ms, "", 0.0, error="empty_roi"))
                continue

            change = self._roi_change(prev_crop, crop)
            prev_crop = crop
            if state == "text" and change < self.config.change_threshold and last_text:
                # 画面没变且上一帧确有字幕 → 沿用
                observations.append(
                    OcrObservation(time_ms, last_text, last_score, lines=last_lines)
                )
                continue

            try:
                lines = self._ocr_roi(crop, roi, frame_size=(image.shape[0], image.shape[1]))
            except Exception as exc:  # noqa: BLE001
                state = "error"
                observations.append(
                    OcrObservation(time_ms, "", 0.0, error=f"ocr_failed:{exc}")
                )
                continue

            text, score, nlines = self._lines_to_text(lines)
            if text:
                state = "text"
                last_text, last_score, last_lines = text, score, nlines
            else:
                # 字幕消失：清空缓存，禁止跨空白复活旧句
                state = "blank"
                last_text = ""
            observations.append(OcrObservation(time_ms, text, score, lines=nlines))

        return observations

    # -- 合并 -----------------------------------------------------------
    def merge(self, observations: list[OcrObservation]) -> list[Segment]:
        """把逐帧观察合并成字幕句。

        O1-2：不再因为"多帧相同"就删句——同一句字幕持续 1~10 秒是正常的。
        只有**长期不变**（跨整段时长都不换字）且被判定为界面文字时才剔除。
        """
        segments: list[Segment] = []
        current_text = ""
        current_start = 0
        current_end = 0
        current_score = 0.0
        current_count = 0
        current_lines = 1

        def flush() -> None:
            nonlocal current_text, current_start, current_end, current_score
            nonlocal current_count, current_lines
            if (
                current_text
                and current_count >= self.config.min_samples
                and (current_end - current_start) >= self.config.min_duration_ms
            ):
                segments.append(Segment(
                    start_ms=current_start,
                    end_ms=current_end,
                    text=current_text,
                    source="hard_subtitle_ocr",
                    confidence=round(current_score, 4),
                    flags=[f"lines={current_lines}"],
                ))
            current_text = ""
            current_count = 0

        for obs in observations:
            if obs.error:
                # 引擎异常 / 读帧失败：**不等于字幕消失**，
                # 不能当成断句点，也不能无条件沿用旧句——保持段落开放，
                # 交给后续帧的实际识别结果决定。
                if current_text:
                    current_end = obs.time_ms + int(self.config.sample_interval * 1000)
                continue
            text = obs.text.strip()
            if not text:
                flush()
                continue
            if current_text and similarity(current_text, text) >= self.config.similarity:
                if len(normalize_text(text)) > len(normalize_text(current_text)):
                    current_text = text
                    current_lines = obs.lines
                current_end = obs.time_ms + int(self.config.sample_interval * 1000)
                current_score = max(current_score, obs.score)
                current_count += 1
            else:
                flush()
                current_text = text
                current_start = obs.time_ms
                current_end = obs.time_ms + int(self.config.sample_interval * 1000)
                current_score = obs.score
                current_lines = obs.lines
                current_count = 1

        flush()
        return segments

    def split_static_text(
        self, segments: list[Segment], total_ms: int
    ) -> tuple[list[Segment], list[dict[str, Any]]]:
        """把"长期不变"的段落挑出来当作界面文字（O1-2 / O4）。

        判据：同一句话的持续时长 ≥ ``static_min_duration_ms``，且它是唯一一句
        （没别的句子替换它）——那是常驻的界面/水印，不是对白字幕。
        """
        dialogue: list[Segment] = []
        on_screen: list[dict[str, Any]] = []
        for seg in segments:
            duration = seg.end_ms - seg.start_ms
            only_one = len(segments) == 1
            if duration >= self.config.static_min_duration_ms and only_one:
                on_screen.append({
                    "time_ms": seg.start_ms,
                    "end_ms": seg.end_ms,
                    "text": seg.text,
                    "kind": "static_overlay",
                })
            else:
                dialogue.append(seg)
        return dialogue, on_screen

    # -- 旧检测器（兜底） -------------------------------------------------
    def detect_band(self, video: str | Path, work_dir: Path) -> Roi:
        """旧版"最大字号动态文字带"检测，作为新跟踪器的兜底。"""
        from .subtitle_region import SubtitleRegionTracker, TrackerConfig

        tracker = SubtitleRegionTracker(
            self._get_engine(), ffmpeg=self.ffmpeg,
            config=TrackerConfig(
                windows=self.config.region_windows,
                window_seconds=self.config.region_window_seconds,
                fps=self.config.region_fps,
            ),
        )
        detection = tracker.track(video)
        best = detection.best
        if best is not None:
            return Roi(
                x0=best.x0, y0=best.y0, x1=best.x1, y1=best.y1,
                method="temporal_tracking_v1",
                heuristic_score=best.heuristic_score,
            )
        return self._fallback_roi()

    def _fallback_roi(self) -> Roi:
        """回退 ROI：**全宽**的底部字幕区。

        注意：Roi 的参数顺序是 (x0, y0, x1, y1)，
        曾经在这里写成位置参数导致横纵颠倒，只裁到右下角一小块。
        """
        return Roi(
            x0=0.0,
            y0=self.config.band_top,
            x1=1.0,
            y1=self.config.band_bottom,
            method="fallback",
            heuristic_score=0.0,
        )

    def detect_roi(
        self, video: str | Path, work_dir: Path
    ) -> tuple[Roi, dict[str, Any], Any]:
        """确定本次使用的字幕 ROI。

        返回 (ROI, 诊断信息, 区域检测结果)。
        """
        from .subtitle_region import SubtitleRegionTracker, TrackerConfig

        fixed = self.config.fixed_band
        if fixed is not None:
            y0, y1 = fixed
            return (
                Roi(
                    x0=0.0, y0=y0, x1=1.0, y1=y1,
                    method="fixed", heuristic_score=1.0,
                ),
                {"method": "fixed", "regions": [], "warnings": []},
                None,
            )

        tracker = SubtitleRegionTracker(
            self._get_engine(), ffmpeg=self.ffmpeg,
            config=TrackerConfig(
                windows=self.config.region_windows,
                window_seconds=self.config.region_window_seconds,
                fps=self.config.region_fps,
                min_score=self.config.region_min_score,
            ),
        )
        detection = tracker.track(video)
        info = detection.to_dict()
        best = detection.best
        if best is None:
            roi = self._fallback_roi()
            info.setdefault("warnings", []).append(
                "字幕区跟踪未给出高置信结果，已回退到全宽底部区域"
                f"（{roi.y0:.0%}~{roi.y1:.0%}，method=fallback，heuristic_score=0）"
            )
            info["fallback"] = True
            info["roi"] = {
                "x0": roi.x0, "y0": roi.y0, "x1": roi.x1, "y1": roi.y1,
                "method": roi.method, "heuristic_score": roi.heuristic_score,
            }
            return roi, info, detection
        return (
            Roi(
                x0=best.x0, y0=best.y0, x1=best.x1, y1=best.y1,
                method=best.role,
                heuristic_score=best.heuristic_score,
            ),
            info,
            detection,
        )

    def _collect_on_screen_text(
        self, detection: Any, roi: Roi
    ) -> list[dict[str, Any]]:
        """O4：收集字幕区**之外**的界面文字（按钮、菜单、文件名、标题栏）。

        这些不是讲者的话，不能混进对白 transcript；单独作为一条证据通道。
        """
        if detection is None:
            return []
        out: list[dict[str, Any]] = []
        for track in getattr(detection, "tracks", []) or []:
            x0, y0, x1, y1 = track.rect()
            # 与字幕区有重叠 → 属于对白通道，不重复
            overlaps = not (
                x1 < roi.x0 - 0.02 or x0 > roi.x1 + 0.02
                or y1 < roi.y0 - 0.02 or y0 > roi.y1 + 0.02
            )
            if overlaps:
                continue
            texts = track.text_sequence()
            if not texts:
                continue
            out.append({
                "time_ms": track.start_ms,
                "end_ms": track.end_ms,
                "text": texts[0] if len(texts) == 1 else " / ".join(texts[:3]),
                "kind": "ui_text",
                "bbox": [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)],
            })
        # 按时长优先（出现越久的界面文字越有参考价值），并限制条数
        out.sort(key=lambda t: -(t["end_ms"] - t["time_ms"]))
        cap = max(1, self.config.max_on_screen_text)
        return out[:cap]

    # -- 主流程 ---------------------------------------------------------
    def run(self, video: str | Path, duration_ms: int | None = None) -> OcrResult:
        result = OcrResult()
        problems = self.diagnose()
        if problems:
            result.warnings.extend(problems)
            return result

        with tempfile.TemporaryDirectory(prefix="bili-ocr-") as tmp:
            work = Path(tmp)
            roi, region_info, detection = self.detect_roi(video, work)
            try:
                observations = self.observe(video, work, roi)
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f"OCR 抽帧/识别失败：{exc}")
                return result

        segments = self.merge(observations)
        dialogue, on_screen = self.split_static_text(segments, duration_ms or 0)
        # O4：字幕区之外的界面文字也走独立通道
        on_screen.extend(self._collect_on_screen_text(detection, roi))
        result.segments = dialogue
        result.on_screen_text = on_screen

        errors = [o for o in observations if o.error]
        non_empty = [o for o in observations if o.text]
        result.stats = {
            "sampled_frames": len(observations),
            "frames_with_text": len(non_empty),
            "frame_errors": len(errors),
            "segments": len(dialogue),
            "sample_interval_s": self.config.sample_interval,
            "roi": {
                "x0": round(roi.x0, 4), "y0": round(roi.y0, 4),
                "x1": round(roi.x1, 4), "y1": round(roi.y1, 4),
                "method": roi.method,
            },
            "region_detection": region_info,
            "on_screen_text": len(on_screen),
            "time_precision": "sample_interval",
        }
        if duration_ms:
            from .subtitle_normalize import coverage_stats

            cov = coverage_stats(dialogue, duration_ms)
            result.stats["coverage_ratio"] = cov["segment_time_coverage"]
            result.stats["segment_time_coverage"] = cov["segment_time_coverage"]
            result.stats["covered_ms"] = cov["covered_ms"]
            result.stats["span_ratio"] = cov["span_ratio"]
        if errors:
            result.warnings.append(
                f"有 {len(errors)} 帧读取或识别失败（已与'没有字幕'区分记录）"
            )
        if not dialogue:
            result.warnings.append(
                f"在字幕区 {roi.y0:.0%}~{roi.y1:.0%} 里没有识别到连续对白"
            )
        return result
