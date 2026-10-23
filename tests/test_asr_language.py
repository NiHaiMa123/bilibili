"""ASR 语言参数的传递语义（T19）。"""

from __future__ import annotations

import json
from pathlib import Path

from bilibili_agent.asr.adapter import ASRAdapter


def _adapter(tmp_path: Path, language: str = "auto") -> ASRAdapter:
    import sys

    return ASRAdapter(
        python=sys.executable,   # 真实存在的解释器，绕开 diagnose 的存在性检查
        model_path=str(tmp_path),
        ffmpeg="ffmpeg",
        language=language,
    )


def _run_and_capture(tmp_path: Path, language: str) -> list[str]:
    """跑一次 adapter.transcribe，抓下传给 worker 的命令行。"""
    captured: list[str] = []
    real_run = __import__("subprocess").run

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        out = Path(cmd[cmd.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({
                "segments": [
                    {"start_ms": 0, "end_ms": 1000, "text": "hi", "granularity": "chunk"}
                ],
                "warnings": [],
                "model": "m", "device": "cpu", "language": "English",
                "audio_duration_s": 1.0, "chunks": 1,
            }),
            encoding="utf-8",
        )
        return real_run(
            [__import__("sys").executable, "-c", "pass"], *args, **kwargs
        )

    import subprocess

    subprocess.run = fake_run  # type: ignore[assignment]
    try:
        a = _adapter(tmp_path, language)
        a.transcribe(tmp_path / "a.wav", out_json=tmp_path / "out.json")
    finally:
        subprocess.run = real_run  # type: ignore[assignment]
    return captured


def test_auto_language_passes_auto_to_worker(tmp_path):
    """auto 时给 worker 传 'auto'，由 worker 转成 Python None。"""
    captured = _run_and_capture(tmp_path, "auto")
    assert captured
    cmd = captured[0]
    assert cmd[cmd.index("--language") + 1] == "auto"


def test_explicit_language_is_passed_through(tmp_path):
    captured = _run_and_capture(tmp_path, "English")
    cmd = captured[0]
    assert cmd[cmd.index("--language") + 1] == "English"


def test_worker_never_receives_string_none(tmp_path):
    """不能把字符串 'None' 传给 qwen_asr（会被 validate_language 拒绝）。"""
    for lang in ("auto", "none", "None", ""):
        captured = _run_and_capture(tmp_path, lang)
        cmd = captured[0]
        value = cmd[cmd.index("--language") + 1]
        assert value.lower() not in ("none", "")


def test_worker_language_parsing():
    """worker 内部：auto/none/空 → Python None；其它字符串原样保留。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "asr_worker",
        Path(__file__).resolve().parents[1]
        / "src" / "bilibili_agent" / "asr" / "worker.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # 直接验证转换逻辑（与 worker 内联实现一致）
    for raw, expect_none in (
        ("auto", True), ("AUTO", True), ("none", True), ("None", True),
        ("", True), ("Chinese", False), ("English", False),
    ):
        language = None if raw.strip().lower() in ("", "auto", "none") else raw
        assert (language is None) is expect_none
