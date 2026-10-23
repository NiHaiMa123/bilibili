"""RapidOCR 引擎封装（OCR 区域跟踪与字幕识别共用同一个引擎实例）。

只依赖 RapidOCR 3.x（ONNX Runtime + PP-OCRv6），纯 CPU 可跑。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

_ENGINE = None
_LOCK = threading.Lock()


@dataclass(slots=True)
class OcrBox:
    """一个文字框（坐标为相对画面的比例，取值 0~1）。"""

    text: str
    score: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "score": round(self.score, 4),
            "x0": round(self.x0, 4),
            "y0": round(self.y0, 4),
            "x1": round(self.x1, 4),
            "y1": round(self.y1, 4),
        }


class OcrEngine:
    """薄封装：输入 numpy 图像，输出文字框。"""

    def __init__(self, min_score: float = 0.0) -> None:
        self.min_score = min_score
        self._engine = None
        self.calls = 0

    def _ensure(self):
        if self._engine is None:
            from rapidocr import RapidOCR

            self._engine = RapidOCR()
        return self._engine

    def boxes(self, image) -> list[OcrBox]:
        """识别整幅图像，返回文字框列表。

        **坐标统一归一化到 0~1**（相对输入图像），调用方不必再关心分辨率。
        """
        out = self._ensure()(image)
        self.calls += 1
        raw_texts = getattr(out, "txts", None)
        raw_scores = getattr(out, "scores", None)
        raw_boxes = getattr(out, "boxes", None)
        texts = [] if raw_texts is None else list(raw_texts)
        scores = [] if raw_scores is None else list(raw_scores)
        boxes = [] if raw_boxes is None else list(raw_boxes)

        try:

            height, width = image.shape[:2]
        except Exception:  # noqa: BLE001
            height, width = 1, 1
        w = max(float(width), 1.0)
        h = max(float(height), 1.0)

        result: list[OcrBox] = []
        for idx, (text, score) in enumerate(zip(texts, scores, strict=False)):
            text = (text or "").strip()
            if not text or score < self.min_score:
                continue
            if idx >= len(boxes) or boxes[idx] is None:
                continue
            xs = [float(p[0]) for p in boxes[idx]]
            ys = [float(p[1]) for p in boxes[idx]]
            result.append(OcrBox(
                text=text,
                score=float(score),
                x0=min(xs) / w, y0=min(ys) / h,
                x1=max(xs) / w, y1=max(ys) / h,
            ))
        return result


def get_engine(min_score: float = 0.0) -> OcrEngine:
    """获取共享引擎实例（首次调用时才真正加载模型）。"""
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            _ENGINE = OcrEngine(min_score=min_score)
        return _ENGINE
