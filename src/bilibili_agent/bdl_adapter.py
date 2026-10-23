"""BDL CLI 适配层。

只通过命令行调用独立 CLI（``bdl.exe``），不 fork 上游、不驱动桌面 GUI。
所有调用都用参数数组传参，避免 Windows 空格/中文路径的转义问题。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# BDL CLI 退出码：0 成功、1 运行失败、2 参数错误
EXIT_OK = 0


class BdlError(RuntimeError):
    """BDL 调用失败。"""

    def __init__(self, message: str, *, stage: str = "bdl", code: int | None = None,
                 stderr: str = "", retryable: bool = False):
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.stderr = stderr
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "error_code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }


@dataclass(slots=True)
class DownloadOutput:
    """一次下载的真实产物（以 CLI 输出的清单为准，不猜文件名）。"""

    input: str
    source_title: str
    outputs: list[str] = field(default_factory=list)
    probed: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "source_title": self.source_title,
            "outputs": self.outputs,
            "probed": self.probed,
        }


def find_executable(name: str, explicit: str = "") -> str:
    """定位可执行文件：显式路径 → PATH。找不到返回空串。"""
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return str(p)
        found = shutil.which(explicit)
        return found or ""
    return shutil.which(name) or ""


class BdlAdapter:
    """BDL CLI 的薄封装。"""

    def __init__(
        self,
        exe_path: str,
        *,
        cookie_file: str = "",
        ffmpeg_path: str = "",
        timeout: int = 3600,
    ) -> None:
        self.exe_path = exe_path
        self.cookie_file = cookie_file
        self.ffmpeg_path = ffmpeg_path
        self.timeout = timeout

    # -- 基础 -----------------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(self.exe_path) and Path(self.exe_path).is_file()

    def _base_args(self) -> list[str]:
        args: list[str] = []
        if self.cookie_file and Path(self.cookie_file).is_file():
            args += ["--cookie-file", str(self.cookie_file)]
        return args

    def _run(self, args: list[str], *, stage: str, timeout: int | None = None) -> str:
        """执行命令并返回 stdout；失败抛 BdlError。"""
        if not self.available:
            raise BdlError(
                f"未找到 BDL CLI 可执行文件：{self.exe_path or '(未配置)'}",
                stage=stage,
                code=None,
            )
        cmd = [self.exe_path, *self._base_args(), *args]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BdlError(f"BDL 调用超时（{stage}）", stage=stage, retryable=True) from exc

        if proc.returncode != EXIT_OK:
            stderr = (proc.stderr or "").strip()
            message = stderr or f"BDL 退出码 {proc.returncode}"
            # 412/429/403 属风控或权限限制，重试没有意义
            restricted = any(t in message for t in ("412", "429", "403", "风险", "限制"))
            raise BdlError(
                message,
                stage=stage,
                code=proc.returncode,
                stderr=stderr,
                retryable=not restricted,
            )

        out = (proc.stdout or "").strip()
        if not out:
            return out
        return out

    def _run_json(self, args: list[str], *, stage: str, timeout: int | None = None) -> Any:
        raw = self._run(["--json", *args], stage=stage, timeout=timeout)
        if not raw:
            raise BdlError("BDL 未返回数据", stage=stage)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BdlError(
                f"BDL 返回的不是合法 JSON：{raw[:200]}",
                stage=stage,
            ) from exc

    # -- 对外能力 -------------------------------------------------------
    def version(self) -> str:
        raw = self._run(["--version"], stage="version")
        return raw.strip()

    def ffmpeg_check(self) -> dict[str, Any]:
        args = ["ffmpeg-check"]
        if self.ffmpeg_path:
            args += ["--ffmpeg", self.ffmpeg_path]
        return self._run_json(args, stage="ffmpeg-check", timeout=120)

    def verify_cookie(self) -> dict[str, Any]:
        return self._run_json(["verify-cookie"], stage="verify-cookie", timeout=60)

    def parse(self, target: str) -> dict[str, Any]:
        """解析输入，返回 NormalizedSourceTree。"""
        return self._run_json(["parse", target], stage="parse", timeout=300)

    def download(
        self,
        target: str,
        *,
        parts: list[int] | None = None,
        output_dir: str,
        media_mode: str = "audio-video",
        container: str = "mp4",
        quality: str = "best",
        codec: str = "auto",
    ) -> list[DownloadOutput]:
        """下载指定范围。``parts`` 必填（或由调用方保证 items/--all）。"""
        args = ["download", target]
        if parts:
            args += ["--parts", ",".join(str(p) for p in parts)]
        args += [
            "--output",
            str(output_dir),
            "--media-mode",
            media_mode,
            "--container",
            container,
            "--quality",
            quality,
            "--codec",
            codec,
        ]
        data = self._run_json(args, stage="download")
        downloads = data.get("downloads") or []
        results: list[DownloadOutput] = []
        for item in downloads:
            outputs = [str(p) for p in (item.get("outputs") or [])]
            results.append(
                DownloadOutput(
                    input=str(item.get("input") or target),
                    source_title=str(item.get("source_title") or ""),
                    outputs=outputs,
                )
            )
        return results
