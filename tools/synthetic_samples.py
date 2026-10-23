"""生成**合成**字幕测试片段，并用真实 OCR 管线做端到端验证。

REVIEW2 §4「Windows 真实场景」要求：除仓库已有的真实样本外，
还要有**双语**、**字幕中途换位置**、**短长字幕交替**三类样本；
找不到公开视频时允许生成 10～30 秒合成片段，但必须明确标注"合成"。

本脚本生成三类片段（**全部为合成素材，非真实 B 站视频**）：

* ``synthetic_bilingual.mp4``     —— 底部中英双行字幕
* ``synthetic_position_switch.mp4`` —— 前半底部、后半顶部
* ``synthetic_long_short.mp4``    —— 短句与超长句交替

用法::

    python tools/synthetic_samples.py make --out output/synthetic
    python tools/synthetic_samples.py run  --dir output/synthetic
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

WIDTH, HEIGHT = 1280, 720
FPS = 10
FFMPEG = shutil.which("ffmpeg") or ""
CN_FONT = Path("C:/Windows/Fonts/msyh.ttc")
EN_FONT = Path("C:/Windows/Fonts/arial.ttf")


# --------------------------------------------------------------------- 渲染
def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    center_x: int,
    center_y: int,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int] = (255, 255, 255),
    stroke: tuple[int, int, int] = (0, 0, 0),
    stroke_width: int = 3,
    max_width: int = WIDTH - 80,
) -> None:
    """把一行字按**中心**画在指定位置（上下都要有描边才清晰）。

    太宽就自动缩小字号——否则超长句会被画布边界**裁掉**，
    那测的就不是"字幕区能不能容纳长句"，而是"画布够不够宽"了。
    """
    if not text:
        return
    size = font.size
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    if w > max_width and size > 12:
        new_size = max(12, int(size * max_width / w))
        font = ImageFont.truetype(font.path, new_size)
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = center_x - w // 2 - bbox[0]
    y = center_y - h // 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke_width,
              stroke_fill=stroke)


def _subtitle_spec(clip: str, t: float) -> list[tuple[str, float, bool]]:
    """返回该时刻应该出现的 [(文本, 相对 y 中心, 是否英文)]。"""
    idx = int(t // 2.5)

    if clip == "bilingual":
        cn = [
            "今天我们来聊聊模型换衣服这件事",
            "首先打开材质面板往下拉",
            "找到名字里带材质的那个贴图",
            "把它拖到模型身上就可以了",
            "最后再调整一下裙子的长度",
            "这样整个换装就完成了",
        ]
        en = [
            "Today we talk about changing clothes",
            "First open the material panel",
            "Find the texture named material",
            "Drag it onto the model",
            "Then adjust the skirt length",
            "That is all for this tutorial",
        ]
        return [
            (cn[idx % len(cn)], 0.795, False),
            (en[idx % len(en)], 0.875, True),
        ]

    if clip == "position_switch":
        cn = [
            "前半段我们把字幕放在画面底部",
            "这样看起来比较符合日常习惯",
            "到了后半段我们把字幕挪到顶部",
            "位置变了内容也要跟着换",
            "两段都要能正常识别出来",
            "这样才算真正支持了位置迁移",
        ]
        y = 0.83 if t < 12.0 else 0.10
        return [(cn[idx % len(cn)], y, False)]

    if clip == "long_short":
        long_text = (
            "这是一个刻意写得很长的句子用来检验字幕区域能不能完整容纳"
            "左右两端而不会把开头的字和结尾的字裁掉"
        )
        short_text = "好的"
        pairs = [
            (short_text, 0.83),
            (long_text, 0.83),
        ]
        text, y = pairs[idx % 2]
        return [(text, y, False)]

    raise ValueError(f"未知片段类型：{clip}")


def _render_clip(clip: str, out_path: Path, duration_s: float) -> None:
    cn_font = _font(CN_FONT, 34)
    en_font = _font(EN_FONT, 26)
    total = int(duration_s * FPS)
    with tempfile.TemporaryDirectory(prefix="synthetic-") as tmp:
        frames_dir = Path(tmp)
        for i in range(total):
            t = i / FPS
            img = Image.new("RGB", (WIDTH, HEIGHT), (24, 26, 32))
            draw = ImageDraw.Draw(img)
            # 一点点背景纹理，避免整帧纯色（更接近真实视频的采样特征）
            draw.rectangle([0, int(HEIGHT * 0.45), WIDTH, int(HEIGHT * 0.62)],
                           fill=(38, 42, 54))
            draw.text((24, 24), f"{clip}  t={t:0.1f}s", font=en_font,
                      fill=(150, 150, 160))
            for text, y, is_en in _subtitle_spec(clip, t):
                font = en_font if is_en else cn_font
                # 英文描边细一点，避免相邻字母被描边糊在一起而丢掉词间空格
                _draw_centered(draw, text, WIDTH // 2, int(HEIGHT * y), font,
                               stroke_width=2 if is_en else 3)
            img.save(frames_dir / f"f_{i:05d}.png")

        cmd = [
            FFMPEG, "-hide_banner", "-nostats", "-y",
            "-framerate", str(FPS),
            "-i", str(frames_dir / "f_%05d.png"),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(out_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)


# ------------------------------------------------------------------ 生成入口
CLIPS = {
    "bilingual": ("synthetic_bilingual.mp4", 20.0),
    "position_switch": ("synthetic_position_switch.mp4", 24.0),
    "long_short": ("synthetic_long_short.mp4", 20.0),
}


def cmd_make(args: argparse.Namespace) -> int:
    if not FFMPEG:
        print("找不到 ffmpeg，无法生成合成片段", file=sys.stderr)
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for clip, (name, duration) in CLIPS.items():
        target = out_dir / name
        print(f"[合成] {clip} → {target}（{duration:.0f}s）", flush=True)
        _render_clip(clip, target, duration)
    meta = {
        "synthetic": True,
        "note": "本目录片段均为**合成素材**，由 tools/synthetic_samples.py 生成，"
                "不是真实 B 站视频。",
        "clips": {k: {"file": v[0], "duration_s": v[1]} for k, v in CLIPS.items()},
    }
    (out_dir / "SYNTHETIC.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"完成，目录：{out_dir}")
    return 0


# ------------------------------------------------------------------ 验证入口
def cmd_run(args: argparse.Namespace) -> int:
    from bilibili_agent.ocr_subtitles import OcrConfig, SubtitleOcr

    src_dir = Path(args.dir)
    ffmpeg = args.ffmpeg or FFMPEG
    cfg = OcrConfig(
        band="auto",
        sample_interval=1.0,
        min_samples=2,
        min_duration_ms=700,
        region_windows=6,
        region_window_seconds=4.0,
        region_fps=3.0,
    )
    report: dict = {"synthetic": True, "clips": {}}
    failures: list[str] = []

    for clip, (name, duration) in CLIPS.items():
        video = src_dir / name
        if not video.is_file():
            failures.append(f"{clip}: 缺少 {video}")
            continue
        print(f"\n=== 合成样本 {clip}（{name}）===", flush=True)
        ocr = SubtitleOcr(config=cfg, ffmpeg=ffmpeg)
        result = ocr.run(video, duration_ms=int(duration * 1000))
        stats = result.stats
        plan = stats.get("roi_slots") or []
        print(f"  段数={len(result.segments)}  采样帧={stats.get('sampled_frames')}  "
              f"检测期OCR调用={ (stats.get('region_detection') or {}).get('ocr_calls')}  "
              f"warnings={result.warnings}")
        for slot in plan:
            for r in slot["rois"]:
                print(f"  ROI时段[{slot['start_ms']}~{slot['end_ms']}] "
                      f"x={r['x0']}~{r['x1']} y={r['y0']}~{r['y1']} "
                      f"method={r['method']} score={r['heuristic_score']}")
        for seg in result.segments:
            print(f"   [{seg.start_ms:>6}~{seg.end_ms:>6}] {seg.text}")
        print(f"  界面文字通道条目：{len(result.on_screen_text)}")
        report["clips"][clip] = {
            "file": name,
            "duration_s": duration,
            "segments": [s.to_dict() for s in result.segments],
            "stats": {k: v for k, v in stats.items() if k != "region_detection"},
            "region_detection": {
                k: v for k, v in (stats.get("region_detection") or {}).items()
                if k != "candidates"
            },
            "on_screen_text": result.on_screen_text,
            "warnings": result.warnings,
        }

        # -- 逐片段断言 --
        if clip == "bilingual":
            cn_hit = any("模型" in s.text or "材质" in s.text or "裙子" in s.text
                         for s in result.segments)
            flat = "".join(s.text for s in result.segments).replace(" ", "")
            en_hit = any(k in flat for k in ("Todaywetalkabout", "Firstopenthematerial",
                                             "Findthetexturenamed", "Dragitontothemodel",
                                             "Thenadjusttheskirt", "Thatisallfortutorial"))
            if not cn_hit:
                failures.append("bilingual: 中文行没被识别")
            if not en_hit:
                failures.append("bilingual: 英文行没被识别")
            print(f"  → 中文行命中={cn_hit} 英文行命中={en_hit}")
        elif clip == "position_switch":
            # 底部阶段：0~12s；顶部阶段：12~24s
            bottom = [s for s in result.segments if s.end_ms <= 13_000]
            top = [s for s in result.segments if s.start_ms >= 11_000]
            if not bottom:
                failures.append("position_switch: 底部阶段没有字幕")
            if not top:
                failures.append("position_switch: 顶部阶段没有字幕")
            print(f"  → 底部阶段段数={len(bottom)} 顶部阶段段数={len(top)}")
            for s in top:
                print(f"     顶部段: [{s.start_ms}~{s.end_ms}] {s.text}")
        elif clip == "long_short":
            long_text = (
                "这是一个刻意写得很长的句子用来检验字幕区域能不能完整容纳"
                "左右两端而不会把开头的字和结尾的字裁掉"
            )
            longest = max(result.segments, key=lambda s: len(s.text), default=None)
            got = longest.text if longest else ""
            print(f"  → 最长一段 {len(got)} 字：{got}")
            if longest is None or len(got) < 15:
                failures.append("long_short: 超长句整段没被识别")
            else:
                # 长句两端都要在（不能只剩中间一截）
                head = "".join(ch for ch in long_text[:6])
                tail = "".join(ch for ch in long_text[-6:])
                if head not in got:
                    failures.append(f"long_short: 长句开头被裁掉（缺 {head!r}）")
                if tail not in got:
                    failures.append(f"long_short: 长句结尾被裁掉（缺 {tail!r}）")
                else:
                    print("  → 长句两端完整")

    report["failures"] = failures
    out_json = src_dir / "synthetic_report.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n报告：{out_json}")
    if failures:
        print("\n未通过：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n全部合成样本断言通过")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_make = sub.add_parser("make", help="生成合成片段")
    p_make.add_argument("--out", default="output/synthetic")
    p_make.set_defaults(func=cmd_make)

    p_run = sub.add_parser("run", help="用真实 OCR 管线验证合成片段")
    p_run.add_argument("--dir", default="output/synthetic")
    p_run.add_argument("--ffmpeg", default="")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
