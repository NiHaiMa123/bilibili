"""Level 3：画面烧录字幕 OCR。

只认**真正在对白字幕**，不把 PPT 标题、B 站水印、界面文字当对白：

1. 先在字幕带（画面底部/顶部的窄条）里取文本，避免整屏 UI 文字污染；
2. 按固定间隔抽帧，逐帧 OCR；
3. 相邻帧文本相似则合并成一句，记录首次/末次出现时间；
4. 过滤水印区域与低置信度片段；
5. 计算覆盖度（有效字幕时长 / 视频时长、每分钟有效字数）。

引擎：RapidOCR 3.x（ONNX Runtime + **PP-OCRv6** 模型），纯 CPU 可跑，无需 torch。
"""

from __future__ import annotations

import difflib
import json
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
# 纯符号/颜文字行
_NOISE_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)


@dataclass(slots=True)
class BandDetection:
    """自动检测到的字幕区。"""

    top: float
    bottom: float
    confidence: float
    evidence: list[dict[str, Any]] = field(default_factory=list)
    method: str = "auto"

    def to_dict(self) -> dict[str, Any]:
        return {
            "top": round(self.top, 4),
            "bottom": round(self.bottom, 4),
            "confidence": round(self.confidence, 4),
            "method": self.method,
            "evidence": self.evidence[:12],
        }


@dataclass(slots=True)
class OcrConfig:
    """字幕带与采样参数。"""

    band: str = "auto"  # auto=自动检测 | bottom=固定底部 | top=固定顶部
    band_top: float = 0.74  # 字幕带上沿（相对画面高度，band=bottom 时生效）
    band_bottom: float = 0.99  # 字幕带下沿
    sample_interval: float = 1.0  # 抽帧间隔（秒）
    min_score: float = 0.60  # 单个文本框的最低置信度
    scale: int = 2  # 送入 OCR 前放大倍数（小字更稳）
    similarity: float = 0.72  # 相邻帧判为同一句的相似度阈值
    min_duration_ms: int = 700  # 短于这个时长的片段丢弃
    min_samples: int = 2  # 至少连续出现这么多帧才认作一句（过滤闪烁噪声）
    watermark_filter: bool = True
    max_chars: int = 80
    # 字幕文字的几何约束（相对画面尺寸）
    min_text_height: float = 0.020  # 太小的是界面小字/任务栏时钟，不是字幕
    max_text_height: float = 0.16  # 太大的是标题/大字报
    center_tolerance: float = 0.30  # 字幕一般水平居中，允许偏离中心的幅度
    static_text_ratio: float = 0.15  # 跨帧出现比例超过它 → 判为界面静态文字
    # 自动检测字幕区
    detect_samples: int = 12  # 用于检测的采样帧数
    detect_padding: float = 0.035  # 字幕区上下留白（相对画面高度）

    @property
    def fixed_band(self) -> tuple[float, float] | None:
        """固定字幕区；``auto`` 表示交给检测。"""
        if self.band == "bottom":
            return (self.band_top, self.band_bottom)
        if self.band == "top":
            return (0.02, 1.0 - self.band_bottom)
        return None


@dataclass(slots=True)
class OcrObservation:
    """某一帧上看到的一行字幕。"""

    time_ms: int
    text: str
    score: float


@dataclass(slots=True)
class OcrResult:
    segments: list[Segment] = field(default_factory=list)
    on_screen_text: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def normalize_text(text: str) -> str:
    """归一化：去空白、全角转半角常见符号，便于比较相似度。"""
    text = re.sub(r"\s+", "", text or "")
    text = text.replace("，", ",").replace("。", ".")
    return text.strip()


def similarity(a: str, b: str) -> float:
    """两段文本的相似度（0~1）。"""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def is_watermark(text: str) -> bool:
    """判断是不是水印/账号名等非对白内容。"""
    compact = (text or "").strip()
    if not compact:
        return True
    return any(p.search(compact) for p in _WATERMARK_PATTERNS)


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
            from rapidocr import RapidOCR

            self._engine = RapidOCR()
        return self._engine

    # -- 抽帧 -----------------------------------------------------------
    def extract_frames(self, video: str | Path, out_dir: Path) -> list[Path]:
        """按固定间隔把视频拆成图片，返回文件列表（按时间序）。"""
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

    # -- 字幕区自动检测 --------------------------------------------------
    def _probe_duration(self, video: str | Path) -> float:
        """用 ffprobe 取视频时长（秒）。"""
        ffprobe = shutil.which("ffprobe") or ""
        if not ffprobe:
            return 0.0
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json",
                 "-show_format", str(video)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
            )
            data = json.loads(proc.stdout or "{}")
            return float((data.get("format") or {}).get("duration") or 0.0)
        except (subprocess.SubprocessError, OSError, ValueError):
            return 0.0

    def _extract_sample_frames(
        self, video: str | Path, out_dir: Path, samples: int
    ) -> list[Path]:
        """在整段视频上均匀抽 ``samples`` 帧。"""
        if not self.ffmpeg:
            return []
        duration = self._probe_duration(video)
        if duration <= 0:
            return []
        fps = max(samples / duration, 1e-4)
        out_dir.mkdir(parents=True, exist_ok=True)
        pattern = str(out_dir / "d_%06d.jpg")
        cmd = [
            self.ffmpeg, "-hide_banner", "-nostats", "-y",
            "-i", str(video),
            "-vf", f"fps={fps:.8f}",
            "-q:v", "4",
            pattern,
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800)
        except (subprocess.SubprocessError, OSError):
            return []
        return sorted(out_dir.glob("d_*.jpg"))

    def detect_band(self, video: str | Path, work_dir: Path) -> BandDetection:
        """**自动框出字幕区**，不依赖写死的位置。

        判据：**字幕每句都在变，界面文字不会变**。
        把整帧里所有文字框按高度聚成若干"行带"，
        同一个行带在不同采样帧里出现过**不同文字**的，就是字幕带；
        一直不变的（菜单、文件名、任务栏时钟、状态栏）就不是。

        返回值带置信度与证据（采样到的动态文字），
        便于人工或 Agent 复核，也可以据此在 manifest 里留下可审计的痕迹。
        """
        fallback = BandDetection(
            top=self.config.band_top, bottom=self.config.band_bottom,
            confidence=0.0, method="fallback",
        )
        frames = self._extract_sample_frames(
            video, Path(work_dir) / "detect", self.config.detect_samples
        )
        if not frames:
            return fallback

        try:
            import cv2
        except ImportError:  # pragma: no cover
            return fallback

        engine = self._get_engine()
        samples: list[list[dict]] = []
        for frame in frames:
            img = cv2.imread(str(frame))
            if img is None:
                samples.append([])
                continue
            h, w = img.shape[:2]
            # 整帧 OCR，先缩到 960 宽提速
            ratio = min(1.0, 960.0 / max(w, 1))
            if ratio < 1.0:
                img = cv2.resize(
                    img, (max(int(w * ratio), 1), max(int(h * ratio), 1)),
                    interpolation=cv2.INTER_AREA,
                )
            out = engine(img)
            raw_texts = getattr(out, "txts", None)
            raw_scores = getattr(out, "scores", None)
            raw_boxes = getattr(out, "boxes", None)
            texts = [] if raw_texts is None else list(raw_texts)
            scores = [] if raw_scores is None else list(raw_scores)
            boxes = [] if raw_boxes is None else list(raw_boxes)

            items: list[dict] = []
            for idx, (text, score) in enumerate(zip(texts, scores, strict=False)):
                if score < self.config.min_score:
                    continue
                text = (text or "").strip()
                if not text or _NOISE_RE.match(text):
                    continue
                if idx >= len(boxes) or boxes[idx] is None:
                    continue
                xs = [float(p[0]) for p in boxes[idx]]
                ys = [float(p[1]) for p in boxes[idx]]
                inv = 1.0 / max(ratio, 1e-6)
                bh = (max(ys) - min(ys)) * inv / max(h, 1)
                cy = (sum(ys) / len(ys)) * inv / max(h, 1)
                cx = (sum(xs) / len(xs)) * inv / max(w, 1)
                if bh < self.config.min_text_height or bh > self.config.max_text_height:
                    continue
                items.append({"text": text, "cy": cy, "cx": cx, "h": bh})
            samples.append(items)

        # 按 cy 聚成"行带"（容差 3% 画面高度，非链式，避免整段并成一条）
        all_cy = sorted({round(b["cy"], 3) for items in samples for b in items})
        clusters: list[list[float]] = []
        for cy in all_cy:
            if clusters and abs(cy - clusters[-1][0]) <= 0.03:
                clusters[-1].append(cy)
            else:
                clusters.append([cy])

        evidence: list[dict] = []
        dynamic: list[dict] = []
        for cluster in clusters:
            lo, hi = min(cluster), max(cluster)
            texts_in: set[str] = set()
            heights: list[float] = []
            for items in samples:
                for b in items:
                    if lo - 0.03 <= b["cy"] <= hi + 0.03:
                        texts_in.add(b["text"])
                        heights.append(b["h"])
            mean_h = sum(heights) / len(heights) if heights else 0.0
            record = {
                "cy": round(sum(cluster) / len(cluster), 3),
                "text_height": round(mean_h, 4),
                "distinct_samples": len(texts_in),
                "examples": list(texts_in)[:4],
                "dynamic": len(texts_in) >= 2,
            }
            evidence.append(record)
            if record["dynamic"]:
                dynamic.append(record)

        if not dynamic:
            return fallback

        # 在所有"会变的文字带"里挑**字号最大**的那条——字幕总是比
        # 文件名、状态栏、时钟大得多。挑最靠下的会被任务栏骗，挑最大的不会。
        best = max(dynamic, key=lambda e: e["text_height"])
        pad = self.config.detect_padding
        top = max(0.0, best["cy"] - pad)
        bottom = min(1.0, best["cy"] + best["text_height"] + pad)
        confidence = round(min(1.0, best["text_height"] / max(self.config.min_text_height, 1e-6) / 2), 3)
        return BandDetection(
            top=top, bottom=bottom, confidence=confidence,
            evidence=sorted(evidence, key=lambda e: e["cy"]),
            method="auto",
        )

    # -- OCR ------------------------------------------------------------
    def _ocr_boxes(self, image_path: Path, band: tuple[float, float]) -> list[dict]:
        """对单帧的指定区域做 OCR，返回该帧所有候选文字框。

        字幕带里往往还混着界面文字（文件名、按钮、菜单、任务栏时钟）。
        这里只做**逐框**的几何与置信度过滤，选行交给上层，
        这样才能在跨帧统计里识别出"一直不变的界面文字"。
        """
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("缺少 opencv（opencv-python-headless）") from exc

        img = cv2.imread(str(image_path))
        if img is None:
            return []
        h, w = img.shape[:2]
        top, bottom = band
        y0, y1 = int(h * top), int(h * bottom)
        crop = img[y0:y1, :]
        if crop.size == 0:
            return []
        if self.config.scale > 1:
            crop = cv2.resize(
                crop, None, fx=self.config.scale, fy=self.config.scale,
                interpolation=cv2.INTER_CUBIC,
            )

        out = self._get_engine()(crop)
        # 注意：boxes 是 numpy 数组，不能用 `or []` 兜底（会触发真值判断报错）
        raw_texts = getattr(out, "txts", None)
        raw_scores = getattr(out, "scores", None)
        raw_boxes = getattr(out, "boxes", None)
        texts = [] if raw_texts is None else list(raw_texts)
        scores = [] if raw_scores is None else list(raw_scores)
        boxes = [] if raw_boxes is None else list(raw_boxes)

        cands: list[dict] = []
        for idx, (text, score) in enumerate(zip(texts, scores, strict=False)):
            if score < self.config.min_score:
                continue
            text = (text or "").strip()
            if not text or _NOISE_RE.match(text):
                continue
            if self.config.watermark_filter and is_watermark(text):
                continue
            if idx >= len(boxes) or boxes[idx] is None:
                continue
            xs = [float(p[0]) for p in boxes[idx]]
            ys = [float(p[1]) for p in boxes[idx]]
            scale = max(self.config.scale, 1)
            cx = (sum(xs) / len(xs)) / scale / max(w, 1)
            cy = (sum(ys) / len(ys)) / scale / max(h, 1) + top
            bh = (max(ys) - min(ys)) / scale / max(h, 1)
            bw = (max(xs) - min(xs)) / scale / max(w, 1)
            if bh < self.config.min_text_height or bh > self.config.max_text_height:
                continue
            if abs(cx - 0.5) > self.config.center_tolerance:
                continue
            cands.append({"text": text, "score": float(score), "cx": cx,
                          "cy": cy, "w": bw, "h": bh})
        return cands

    @staticmethod
    def _pick_line(cands: list[dict]) -> list[dict]:
        """从候选框里挑出"对白字幕那一行"。

        规则：**字幕是这段里最大的文字**。
        不能简单取"最下面一行"——Windows 任务栏时钟、播放器状态栏都比字幕更低，
        但字号小得多。字号相近时才优先选更靠下的。
        """
        if not cands:
            return []
        ordered = sorted(cands, key=lambda c: c["cy"])
        lines: list[list[dict]] = []
        for c in ordered:
            if lines and abs(c["cy"] - lines[-1][-1]["cy"]) <= max(c["h"], 0.01) * 0.8:
                lines[-1].append(c)
            else:
                lines.append([c])

        def line_height(ln: list[dict]) -> float:
            return max(c["h"] for c in ln)

        def line_cy(ln: list[dict]) -> float:
            return sum(c["cy"] for c in ln) / len(ln)

        best = max(lines, key=line_height)
        tallest = line_height(best)
        for ln in lines:
            if ln is best:
                continue
            if line_height(ln) >= tallest * 0.8 and line_cy(ln) > line_cy(best):
                best = ln
        return sorted(best, key=lambda c: c["cx"])

    def observe(
        self,
        video: str | Path,
        work_dir: Path,
        band: tuple[float, float],
    ) -> list[OcrObservation]:
        """抽帧 + OCR，得到逐帧观察结果。

        两遍处理：先统计所有帧里出现过的文字框，再把**跨帧基本不变**的
        （界面文字、水印、状态栏）剔除掉——字幕每句都在变，界面文字不会。
        """
        frames = self.extract_frames(video, work_dir)
        interval_ms = int(self.config.sample_interval * 1000)

        per_frame: list[list[dict]] = []
        for frame in frames:
            try:
                per_frame.append(self._ocr_boxes(frame, band))
            except Exception:  # noqa: BLE001
                per_frame.append([])

        # 统计"同一段文字在同一位置"出现的次数
        freq: dict[tuple, int] = {}
        for boxes in per_frame:
            for b in boxes:
                key = (b["text"], round(b["cx"], 2), round(b["cy"], 2))
                freq[key] = freq.get(key, 0) + 1
        static_threshold = max(3, int(len(frames) * self.config.static_text_ratio))

        observations: list[OcrObservation] = []
        for idx, boxes in enumerate(per_frame):
            time_ms = idx * interval_ms
            dynamic = [
                b for b in boxes
                if freq[(b["text"], round(b["cx"], 2), round(b["cy"], 2))] < static_threshold
            ]
            line = self._pick_line(dynamic)
            if not line:
                observations.append(OcrObservation(time_ms, "", 0.0))
                continue
            merged = "".join(c["text"] for c in line)
            if len(merged) > self.config.max_chars:
                merged = merged[: self.config.max_chars]
            avg_score = sum(c["score"] for c in line) / len(line)
            observations.append(OcrObservation(time_ms, merged, avg_score))
        return observations

    # -- 合并 -----------------------------------------------------------
    def merge(self, observations: list[OcrObservation]) -> list[Segment]:
        """把逐帧观察合并成字幕句：相似即同一句，记录首末出现时间。"""
        segments: list[Segment] = []
        current_text = ""
        current_start = 0
        current_end = 0
        current_score = 0.0
        current_count = 0

        def flush() -> None:
            nonlocal current_text, current_start, current_end, current_score, current_count
            # 至少连续出现 min_samples 帧才认作一句：
            # 只出现一帧的多半是转场闪烁、水印或识别噪声。
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
                ))
            current_text = ""
            current_count = 0

        for obs in observations:
            text = obs.text.strip()
            if not text:
                flush()
                continue
            if current_text and similarity(current_text, text) >= self.config.similarity:
                # 同一句：取更长的（更完整）那个文本，时间延长
                if len(normalize_text(text)) > len(normalize_text(current_text)):
                    current_text = text
                current_end = obs.time_ms + int(self.config.sample_interval * 1000)
                current_score = max(current_score, obs.score)
                current_count += 1
            else:
                flush()
                current_text = text
                current_start = obs.time_ms
                current_end = obs.time_ms + int(self.config.sample_interval * 1000)
                current_score = obs.score
                current_count = 1

        flush()
        return segments

    # -- 主流程 ---------------------------------------------------------
    def run(self, video: str | Path, duration_ms: int | None = None) -> OcrResult:
        result = OcrResult()
        problems = self.diagnose()
        if problems:
            result.warnings.extend(problems)
            return result

        with tempfile.TemporaryDirectory(prefix="bili-ocr-") as tmp:
            work = Path(tmp)
            # 1) 决定用哪个字幕区：配置固定 or 自动检测
            fixed = self.config.fixed_band
            if fixed is not None:
                band = fixed
                detection = BandDetection(
                    top=fixed[0], bottom=fixed[1], confidence=1.0,
                    method="fixed",
                )
            else:
                detection = self.detect_band(video, work)
                band = (detection.top, detection.bottom)
                if detection.method == "fallback":
                    result.warnings.append(
                        "未能自动定位字幕区（画面里没有检测到会变化的文字），"
                        f"已回退到默认区域 {band[0]:.0%}~{band[1]:.0%}"
                    )

            # 2) 主识别
            try:
                observations = self.observe(video, work, band)
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f"OCR 抽帧/识别失败：{exc}")
                return result

        segments = self.merge(observations)
        result.segments = segments

        non_empty = [o for o in observations if o.text]
        result.stats = {
            "sampled_frames": len(observations),
            "frames_with_text": len(non_empty),
            "segments": len(segments),
            "sample_interval_s": self.config.sample_interval,
            "band": [round(band[0], 4), round(band[1], 4)],
            "band_detection": detection.to_dict(),
        }
        if duration_ms:
            covered = sum(
                max(0, s.end_ms - s.start_ms) for s in segments
            )
            result.stats["coverage_ratio"] = round(
                min(covered / duration_ms, 1.0), 4
            )
            result.stats["covered_ms"] = covered
        if not segments:
            result.warnings.append(
                f"在字幕区 {band[0]:.0%}~{band[1]:.0%} 里没有识别到连续文字"
                "（可能该视频没有烧录字幕，或字幕在别的位置）"
            )
        return result
