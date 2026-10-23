"""ASR 适配层：用外部 ASR 虚拟环境的解释器调用 worker。

不把 torch/qwen_asr 装进本项目虚拟环境；模型与依赖留在用户本地配置的路径里。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schemas import Segment

WORKER = Path(__file__).resolve().parent / "worker.py"


class ASRError(RuntimeError):
    """ASR 不可用或执行失败。"""

    def __init__(self, message: str, *, stage: str = "asr", retryable: bool = False):
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "error_code": None,
            "message": str(self),
            "retryable": self.retryable,
        }


@dataclass(slots=True)
class ASRResult:
    segments: list[Segment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.segments)


class ASRAdapter:
    """把 wav 转成带时间戳的 Segment。"""

    def __init__(
        self,
        *,
        python: str,
        model_path: str,
        device: str = "cuda:0",
        chunk_seconds: float = 120.0,
        aligner_path: str = "",
        ffmpeg: str = "",
        language: str = "auto",
        timeout: int = 7200,
    ) -> None:
        self.python = python
        self.model_path = model_path
        self.device = device
        self.chunk_seconds = chunk_seconds
        self.aligner_path = aligner_path
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""
        self.language = language or "auto"
        self.timeout = timeout

    # -- 依赖检查 -------------------------------------------------------
    def diagnose(self) -> list[str]:
        """返回人类可读的依赖诊断；为空表示就绪。"""
        problems: list[str] = []
        if not self.python or not Path(self.python).is_file():
            problems.append(
                f"未配置可用的 ASR 解释器（{self.python or '(空)'}）；"
                "请在 config.toml 的 [asr].python 中指向已安装 torch+qwen_asr 的 python"
            )
        if not self.model_path or not Path(self.model_path).is_dir():
            problems.append(
                f"ASR 模型目录不存在：{self.model_path or '(空)'}；"
                "请在 config.toml 的 [asr].model_path 中指向本地 Qwen3-ASR 模型"
            )
        if not self.ffmpeg:
            problems.append("未找到 ffmpeg，无法提取音频")
        return problems

    @property
    def ready(self) -> bool:
        return not self.diagnose()

    # -- 音频 -----------------------------------------------------------
    def extract_wav(self, media_path: str | Path, out_wav: str | Path,
                    *, sample_rate: int = 16000) -> Path:
        """用 ffmpeg 提取单声道 PCM WAV。"""
        if not self.ffmpeg:
            raise ASRError("未找到 ffmpeg，无法提取音频")
        out_wav = Path(out_wav)
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.ffmpeg, "-y", "-i", str(media_path),
            "-vn", "-ac", "1", "-ar", str(sample_rate),
            "-c:a", "pcm_s16le", str(out_wav),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=1800)
        except subprocess.TimeoutExpired as exc:
            raise ASRError("ffmpeg 提取音频超时", stage="ffmpeg", retryable=True) from exc
        if proc.returncode != 0 or not out_wav.is_file():
            tail = (proc.stderr or "").strip().splitlines()[-5:]
            raise ASRError(f"ffmpeg 提取音频失败：{' | '.join(tail)}", stage="ffmpeg")
        return out_wav

    # -- 转写 -----------------------------------------------------------
    def transcribe(
        self,
        wav_path: str | Path,
        *,
        language: str | None = None,
        out_json: str | Path | None = None,
    ) -> ASRResult:
        """转写 wav，返回带时间戳的 Segment。

        ``language`` 为 None/``auto``/``none``（不区分大小写）时表示**自动识别**，
        底层传的是 Python 的 ``None``；绝对不能把字符串 ``"None"`` 传给模型，
        qwen_asr 的 ``validate_language`` 会直接抛 ValueError。
        """
        problems = self.diagnose()
        if problems:
            raise ASRError("；".join(problems))

        lang = language if language is not None else self.language
        if isinstance(lang, str) and lang.strip().lower() in ("", "auto", "none"):
            lang_arg: str | None = None
        else:
            lang_arg = str(lang)

        wav_path = Path(wav_path)
        out_json = Path(out_json) if out_json else wav_path.with_suffix(".asr.json")
        cmd = [
            self.python, str(WORKER),
            "--model", self.model_path,
            "--audio", str(wav_path),
            "--out", str(out_json),
            "--device", self.device,
            "--language", lang_arg or "auto",
            "--chunk-seconds", str(self.chunk_seconds),
        ]
        if self.aligner_path:
            cmd += ["--aligner", self.aligner_path]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise ASRError("ASR 转写超时", stage="asr", retryable=True) from exc
        except OSError as exc:
            raise ASRError(f"无法启动 ASR 解释器：{exc}", stage="asr") from exc

        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-6:]
            raise ASRError(
                f"ASR worker 退出码 {proc.returncode}：{' | '.join(tail)}", stage="asr"
            )

        if not out_json.is_file():
            raise ASRError("ASR worker 没有产出结果文件", stage="asr")
        try:
            payload = json.loads(out_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ASRError(f"ASR 结果无法解析：{exc}", stage="asr") from exc

        segments = [
            Segment(
                start_ms=int(s.get("start_ms", 0)),
                end_ms=int(s.get("end_ms", 0)),
                text=str(s.get("text", "")).strip(),
                source="qwen3_asr",
                flags=[str(s.get("granularity") or "chunk")],
            )
            for s in payload.get("segments") or []
            if str(s.get("text", "")).strip()
        ]
        warnings = [str(w) for w in payload.get("warnings") or []]
        meta = {
            "model": payload.get("model"),
            "aligner": payload.get("aligner"),
            "device": payload.get("device"),
            "language": payload.get("language"),
            "audio_duration_s": payload.get("audio_duration_s"),
            "chunks": payload.get("chunks"),
            "granularity": segments[0].flags[0] if segments else None,
        }
        if not segments:
            warnings.append("ASR 没有识别出任何文字（可能是无声/纯音乐）")
        return ASRResult(segments=segments, warnings=warnings, meta=meta)
