"""Level 4：本地 Qwen3-ASR 兜底。

本包只提供与 B 站视频无关的通用转写能力。
"""

from .adapter import ASRAdapter, ASRError, ASRResult

__all__ = ["ASRAdapter", "ASRError", "ASRResult"]
