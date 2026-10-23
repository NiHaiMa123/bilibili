"""Qwen3-ASR 转写 worker（独立脚本）。

**为什么独立**：运行 ASR 需要 torch + qwen_asr，这两个包体积大且与本项目其余依赖
无关。本 worker 不依赖 bilibili_agent 包内任何代码，只用标准库 + numpy + soundfile +
torch + qwen_asr，因此可以直接用外部 ASR 虚拟环境的解释器运行：

    <asr_python> src/bilibili_agent/asr/worker.py --model <模型目录> --audio <wav> --out <json>

实现要点（对齐 PLAN 第 4 章 Level 4）：
- 复用 qwen_asr 自带的 ``split_audio_into_chunks``（低能量边界切分，拼接可还原原音频），
  每个块记录全局偏移，保证时间戳可追溯。
- 有强制对齐模型时给出字/词级时间戳；没有时只给块级时间戳，**不按平均语速伪造逐字时间**。
- 无可信语音的块不补造文字。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 常见的中文/英文句末标点，用于把对齐结果切回句子
_SENTENCE_END = "。！？!?；;…"


def _load_wav(path: str):
    import numpy as np
    import soundfile as sf

    data, sr = sf.read(path, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return np.asarray(data, dtype="float32"), int(sr)


def _align_items(items, original: str) -> list[dict]:
    """把对齐项回贴到识别原文上。

    对齐器给出的 item 文本是"词/字"本身，不带空格与标点（英文会被粘在一起）。
    这里按顺序在原文中定位每个 item，从而还原原始的空格、标点与大小写。
    """
    low = original.lower()
    cursor = 0
    aligned: list[dict] = []
    for item in items:
        text = getattr(item, "text", "") or ""
        if not text:
            continue
        start = float(getattr(item, "start_time", 0.0) or 0.0)
        end = float(getattr(item, "end_time", start) or start)
        idx = low.find(text.lower(), cursor)
        if idx == -1:
            aligned.append({
                "start": start, "end": end, "text": text,
                "src_start": None, "src_end": None,
            })
        else:
            aligned.append({
                "start": start, "end": end,
                "text": original[idx: idx + len(text)],
                "src_start": idx, "src_end": idx + len(text),
            })
            cursor = idx + len(text)
    return aligned


def _group_aligned(items, original: str, max_chars: int = 60) -> list[tuple[float, float, str]]:
    """把字/词级对齐结果按标点与长度聚合成句子。"""
    aligned = _align_items(items, original)
    sentences: list[tuple[float, float, str]] = []
    buf: list[dict] = []

    for i, item in enumerate(aligned):
        buf.append(item)
        nxt = aligned[i + 1] if i + 1 < len(aligned) else None

        # 本 item 结束到下一个 item 开始之间的原文（可能含句末标点）
        if nxt is not None and item["src_end"] is not None and nxt["src_start"] is not None:
            gap = original[item["src_end"]: nxt["src_start"]]
            boundary = nxt["src_start"]
        elif nxt is None and item["src_end"] is not None:
            gap = original[item["src_end"]:]
            boundary = len(original)
        else:
            gap = ""
            boundary = None

        sentence_text = _sentence_text(buf, original, boundary)
        if any(ch in gap for ch in _SENTENCE_END) or len(sentence_text) >= max_chars:
            if sentence_text:
                sentences.append((buf[0]["start"], buf[-1]["end"], sentence_text))
            buf = []

    if buf:
        text = _sentence_text(buf, original, None)
        if text:
            sentences.append((buf[0]["start"], buf[-1]["end"], text))
    return sentences


def _sentence_text(buf: list[dict], original: str, boundary: int | None) -> str:
    """用原文区间拼出句子，保留空格与标点。"""
    first = buf[0]
    last = buf[-1]
    if first["src_start"] is not None and last["src_end"] is not None:
        end = boundary if boundary is not None else last["src_end"]
        return original[first["src_start"]: end].strip()
    # 原文里定位不到时退化为空格拼接
    return " ".join(b["text"] for b in buf).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Qwen3-ASR worker")
    ap.add_argument("--model", required=True, help="Qwen3-ASR 模型目录")
    ap.add_argument("--audio", required=True, help="输入 wav（16k 单声道）")
    ap.add_argument("--out", required=True, help="输出 JSON 路径")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument(
        "--language", default="auto",
        help="auto=自动识别（传 None 给模型）；也可指定 Chinese/English 等",
    )
    ap.add_argument("--chunk-seconds", type=float, default=120.0)
    ap.add_argument("--aligner", default="", help="强制对齐模型目录（可选）")
    ap.add_argument("--max-new-tokens", type=int, default=1024)
    ap.add_argument("--batch-size", type=int, default=8)
    args = ap.parse_args()

    try:
        import torch
        from qwen_asr import Qwen3ASRModel
        from qwen_asr.inference.utils import split_audio_into_chunks
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        print(f"worker 依赖缺失：{exc}；请用已安装 torch/qwen_asr 的解释器运行", file=sys.stderr)
        return 2

    warnings: list[str] = []
    device = args.device
    if device.startswith("cuda") and not torch.cuda.is_available():
        warnings.append("CUDA 不可用，回退到 CPU")
        device = "cpu"

    try:
        wav, sr = _load_wav(args.audio)
    except Exception as exc:  # noqa: BLE001
        print(f"读取音频失败：{exc}", file=sys.stderr)
        return 2

    duration_s = len(wav) / float(sr) if sr else 0.0
    chunks = split_audio_into_chunks(wav, sr, float(args.chunk_seconds))

    # 语言：auto/none 表示自动识别 → 传 Python None；
    # 绝不能把字符串 "None" 传给模型（validate_language 会抛 ValueError）。
    raw_lang = (args.language or "").strip()
    language: str | None = (
        None if raw_lang.lower() in ("", "auto", "none") else raw_lang
    )

    aligner_kwargs: dict = {}
    if args.aligner:
        aligner_kwargs["forced_aligner"] = args.aligner

    try:
        model = Qwen3ASRModel.from_pretrained(
            args.model,
            dtype=torch.bfloat16 if device.startswith("cuda") else torch.float32,
            device_map=device,
            max_inference_batch_size=args.batch_size,
            max_new_tokens=args.max_new_tokens,
            **aligner_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"模型加载失败：{exc}", file=sys.stderr)
        return 3

    use_timestamps = bool(args.aligner)
    segments: list[dict] = []
    for idx, (chunk, offset) in enumerate(chunks):
        try:
            result = model.transcribe(
                audio=(chunk, sr),
                language=language,
                return_time_stamps=use_timestamps,
            )[0]
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"第 {idx} 块转写失败：{exc}")
            continue

        text = (getattr(result, "text", "") or "").strip()
        if not text:
            continue

        chunk_len = len(chunk) / float(sr)
        if use_timestamps and getattr(result, "time_stamps", None) is not None:
            for start, end, sentence in _group_aligned(list(result.time_stamps), text):
                segments.append({
                    "start_ms": int(round((offset + start) * 1000)),
                    "end_ms": int(round((offset + end) * 1000)),
                    "text": sentence,
                    "granularity": "word",
                })
        else:
            # 块级时间戳：只标注整块的起止，不伪造逐字时间
            segments.append({
                "start_ms": int(round(offset * 1000)),
                "end_ms": int(round((offset + chunk_len) * 1000)),
                "text": text,
                "granularity": "chunk",
            })

    payload = {
        "model": str(args.model),
        "aligner": str(args.aligner) if args.aligner else None,
        "device": device,
        "language": language or "auto",
        "audio_duration_s": round(duration_s, 3),
        "chunks": len(chunks),
        "segments": segments,
        "warnings": warnings,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写入 {out_path}：{len(segments)} 段 / {len(chunks)} 块", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
