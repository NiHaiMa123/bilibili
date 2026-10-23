"""轻量语音活动检测（能量法）。

用途：判断"OCR 字幕是否覆盖了全部**有语音的时段**"。

不引入 VAD 模型：用 ffmpeg 抽 16k 单声道 PCM，按 100ms 帧算**平均绝对幅度**
（mean |x|，与 RMS 同量级但更省算力），超过阈值的帧视为有声，
再合并短静音、去掉过短的碎片。

它**不能**判断语音内容，只回答"这段时间有没有人在说话"。
"""

from __future__ import annotations

import array
import subprocess
import wave
from pathlib import Path


def extract_wav(
    media: str | Path, out_wav: str | Path, ffmpeg: str, *, sample_rate: int = 16000
) -> Path | None:
    """用 ffmpeg 抽出单声道 PCM WAV。"""
    if not ffmpeg:
        return None
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y", "-i", str(media),
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", str(out_wav),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=1800)
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0 or not out_wav.is_file():
        return None
    return out_wav


def speech_intervals(
    wav_path: str | Path,
    *,
    frame_ms: int = 100,
    energy_threshold: float = 0.012,
    min_speech_ms: int = 300,
    merge_gap_ms: int = 500,
) -> list[tuple[int, int]]:
    """返回 [(start_ms, end_ms)] 的有声区间。"""
    path = Path(wav_path)
    if not path.is_file():
        return []
    try:
        with wave.open(str(path), "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
    except (wave.Error, OSError):
        return []

    if sampwidth == 2:
        samples = array.array("h")
        samples.frombytes(raw)
        scale = 1.0 / 32768.0
    elif sampwidth == 1:
        samples = array.array("b")
        samples.frombytes(raw)
        scale = 1.0 / 128.0
    else:
        return []
    if n_channels > 1:
        samples = samples[::n_channels]

    frame_len = max(1, int(rate * frame_ms / 1000))
    loud: list[bool] = []
    for i in range(0, len(samples), frame_len):
        chunk = samples[i:i + frame_len]
        if not chunk:
            break
        energy = sum(abs(v) for v in chunk) / len(chunk) * scale
        loud.append(energy >= energy_threshold)

    # 合并短静音
    intervals: list[list[int]] = []
    gap = 0
    for idx, is_loud in enumerate(loud):
        if is_loud:
            if intervals and gap * frame_ms <= merge_gap_ms:
                intervals[-1][1] = idx * frame_ms
            else:
                intervals.append([idx * frame_ms, idx * frame_ms])
            gap = 0
        else:
            gap += 1

    # 丢掉太短的碎片
    out: list[tuple[int, int]] = []
    for start, end in intervals:
        if end - start >= min_speech_ms:
            out.append((start, end))
    return out


def speech_intervals_from_media(
    media: str | Path,
    ffmpeg: str,
    work_dir: str | Path,
    **kwargs,
) -> tuple[list[tuple[int, int]], Path | None]:
    """从媒体文件直接得到有声区间。返回 (区间, 生成的 wav 路径)。"""
    wav = Path(work_dir) / "vad_16k.wav"
    got = extract_wav(media, wav, ffmpeg)
    if got is None:
        return [], None
    return speech_intervals(got, **kwargs), got


def covered_ratio(
    intervals: list[tuple[int, int]],
    segments: list[tuple[int, int]],
) -> tuple[float, int]:
    """返回 (被字幕覆盖的有声时长占比, 最长未被覆盖的有声间隙毫秒)。

    区间是闭开区间 [start, end)。
    """
    if not intervals:
        return 0.0, 0

    # 字幕区间先合并
    spans = sorted(segments)
    merged: list[list[int]] = []
    for start, end in spans:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    covered = 0
    longest_gap = 0
    for start, end in intervals:
        span = end - start
        if span <= 0:
            continue
        inside = 0
        gap = 0
        cursor = start
        for m_start, m_end in merged:
            if m_end <= cursor:
                continue
            if m_start >= end:
                break
            lo = max(m_start, start)
            hi = min(m_end, end)
            if hi > lo:
                inside += hi - lo
                if lo > cursor:
                    gap = max(gap, lo - cursor)
                cursor = hi
        if cursor < end:
            gap = max(gap, end - cursor)
        covered += inside
        longest_gap = max(longest_gap, gap)

    total = sum(max(0, e - s) for s, e in intervals)
    ratio = (covered / total) if total else 0.0
    return ratio, longest_gap
