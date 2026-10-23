"""编排层：inspect → 字幕获取 → 归一化 → 写产物 → manifest。

字幕来源按四级自动路由：
平台字幕 → 媒体软字幕 → 烧录字幕 OCR → Qwen3-ASR。
已实现的级别在各自模块里；未实现的级别会明确记录为 warning，不静默产出空文档。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import PIPELINE_VERSION
from .asr.adapter import ASRAdapter, ASRError
from .bdl_adapter import BdlAdapter, BdlError
from .bili_api import BiliApiError, BiliClient
from .cache import (
    component_is_fresh,
    get_component,
    part_dir,
    read_manifest,
    settings_digest,
)
from .config import Config
from .inspect import extract_page, inspect_video
from .md_writer import write_article_md, write_manifest, write_srt, write_transcript_md
from .schemas import Frame, Manifest, Segment, VideoInfo, VideoPart
from .search import SearchProvider
from .segmentation import segment_chapters
from .subtitle_normalize import coverage_stats, parse_srt, to_srt
from .subtitle_resolver import resolve_platform_subtitles

MODES = ("metadata", "subtitle", "illustrated", "assets", "both")
SUBTITLE_SOURCES = ("auto", "platform", "ocr", "asr")


@dataclass(slots=True)
class FetchResult:
    ok: bool
    status: str
    bvid: str | None = None
    title: str | None = None
    parts: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "bvid": self.bvid,
            "title": self.title,
            "parts": self.parts,
            "outputs": self.outputs,
            "warnings": self.warnings,
            "error": self.error,
        }


class Pipeline:
    """把各层串起来。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cfg.ensure_dirs()
        self.adapter = BdlAdapter(
            cfg.bdl_cli,
            cookie_file=cfg.cookie_file,
            ffmpeg_path=cfg.ffmpeg,
        )
        self.client = BiliClient(
            user_agent=cfg.user_agent,
            cookie_header=cfg.cookie_header(),
            interval=cfg.request_interval,
            trust_env=cfg.use_env_proxy,
        )
        self.search_provider = SearchProvider(self.client)

    def close(self) -> None:
        self.client.close()

    # -- search ---------------------------------------------------------
    def search(
        self, query: str, *, limit: int = 20, order: str = "totalrank"
    ) -> list:
        return self.search_provider.search(query, limit=limit, order=order)

    # -- inspect --------------------------------------------------------
    def inspect(self, target: str) -> VideoInfo:
        return inspect_video(target, adapter=self.adapter, client=self.client)

    # -- fetch ----------------------------------------------------------
    def fetch(
        self,
        target: str,
        *,
        parts: list[int] | None = None,
        mode: str = "subtitle",
        subtitle_source: str = "auto",
        refresh: bool = False,
    ) -> FetchResult:
        if mode not in MODES:
            return FetchResult(
                ok=False, status="failed",
                error={"stage": "validate", "error_code": -400,
                       "message": f"未知模式 {mode}", "retryable": False},
            )
        if subtitle_source not in SUBTITLE_SOURCES:
            return FetchResult(
                ok=False, status="failed",
                error={"stage": "validate", "error_code": -400,
                       "message": f"未知字幕来源 {subtitle_source}", "retryable": False},
            )

        warnings: list[str] = []
        try:
            video = self.inspect(target)
        except (BdlError, BiliApiError, Exception) as exc:  # noqa: BLE001
            payload = exc.to_dict() if hasattr(exc, "to_dict") else {
                "stage": "inspect", "error_code": None,
                "message": str(exc), "retryable": False,
            }
            return FetchResult(ok=False, status="failed", error=payload)

        if not video.parts:
            warnings.append("没有解析到分 P 信息")
            return FetchResult(
                ok=False, status="failed", bvid=video.bvid, title=video.title,
                warnings=warnings,
                error={"stage": "inspect", "error_code": -404,
                       "message": "没有可处理的分 P", "retryable": False},
            )

        # S1-3：URL 上的 ?p=N 在未显式指定 parts 时决定选哪个分 P；
        # 显式 --parts 优先。
        if not parts:
            page = extract_page(target)
            if page is not None:
                parts = [page]
                warnings.append(f"按 URL 参数 ?p={page} 选择分 P")

        selected = self._select_parts(video, parts, warnings)
        if not selected:
            return FetchResult(
                ok=False, status="failed", bvid=video.bvid, title=video.title,
                warnings=warnings,
                error={"stage": "inspect", "error_code": -400,
                       "message": "选中的分 P 不存在", "retryable": False},
            )

        if mode == "metadata":
            return FetchResult(
                ok=True, status="completed", bvid=video.bvid, title=video.title,
                parts=[self._part_meta(video, p) for p in selected],
                warnings=warnings,
            )

        part_results: list[dict[str, Any]] = []
        all_outputs: list[str] = []
        failed_count = 0
        ok_count = 0

        for part in selected:
            try:
                res = self._fetch_part(
                    video, part, mode=mode, subtitle_source=subtitle_source,
                    refresh=refresh, warnings=warnings,
                )
            except Exception as exc:  # noqa: BLE001
                failed_count += 1
                part_results.append({
                    "page": part.page, "cid": part.cid, "ok": False,
                    "status": "failed",
                    "error": {
                        "stage": "materialize",
                        "error_code": None,
                        "message": str(exc),
                        "retryable": False,
                    },
                })
                continue
            part_results.append(res)
            all_outputs.extend(res.get("outputs") or [])
            if res.get("ok"):
                ok_count += 1
            else:
                failed_count += 1

        # S1-10：多 P 部分失败时聚合为 partial/failed，且顶层要带分 P 错误原因
        if failed_count and ok_count:
            status = "partial"
        elif failed_count:
            status = "failed"
        else:
            status = "completed"

        top_error = None
        if status != "completed":
            first_error = next(
                (p.get("error") for p in part_results if p.get("error")), None
            )
            top_error = first_error or {
                "stage": "materialize",
                "error_code": -404,
                "message": f"{failed_count}/{len(selected)} 个分 P 未完成",
                "retryable": False,
            }

        return FetchResult(
            ok=status == "completed",
            status=status,
            bvid=video.bvid,
            title=video.title,
            parts=part_results,
            outputs=all_outputs,
            warnings=warnings,
            error=top_error,
        )

    # -- 媒体与兜底路线 -------------------------------------------------
    def _media_dir(self, video: VideoInfo, part: VideoPart, media_mode: str) -> Path:
        return (
            Path(self.cfg.media_cache_dir)
            / video.bvid
            / f"P{part.page:02d}_{part.cid}"
            / media_mode
        )

    def _ensure_media(
        self, video: VideoInfo, part: VideoPart, *, media_mode: str = "audio-only"
    ) -> list[str]:
        """下载（或复用缓存的）媒体文件，返回真实存在的路径。

        以 BDL CLI 报告的 outputs 为准，不猜文件名；复用时核对文件确实存在。
        """
        target_dir = self._media_dir(video, part, media_mode)
        marker = target_dir / "media.json"
        if marker.is_file():
            try:
                cached = json.loads(marker.read_text(encoding="utf-8"))
                paths = [str(p) for p in cached.get("outputs") or []]
                if paths and all(Path(p).is_file() for p in paths):
                    return paths
            except (json.JSONDecodeError, OSError):
                pass

        target_dir.mkdir(parents=True, exist_ok=True)
        results = self.adapter.download(
            video.bvid, parts=[part.page], output_dir=str(target_dir),
            media_mode=media_mode, container="mp4",
        )
        outputs: list[str] = []
        for res in results:
            for path in res.outputs:
                if Path(path).is_file():
                    outputs.append(path)
        if not outputs:
            raise BdlError("BDL 没有产出可用的媒体文件", stage="download")
        marker.write_text(
            json.dumps({"outputs": outputs, "media_mode": media_mode},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return outputs

    def _soft_subtitle_segments(
        self, media_path: Path, warnings: list[str]
    ) -> list[Segment]:
        """Level 2：探测媒体文件里的独立字幕轨并提取。"""
        ffprobe = self.cfg.ffprobe or shutil.which("ffprobe") or ""
        if not ffprobe:
            return []
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json",
                 "-select_streams", "s", "-show_streams", str(media_path)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
            )
            if proc.returncode != 0:
                return []
            streams = (json.loads(proc.stdout or "{}").get("streams") or [])
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
            return []
        if not streams:
            return []

        ffmpeg = self.cfg.ffmpeg or shutil.which("ffmpeg") or ""
        if not ffmpeg:
            return []
        out_srt = media_path.with_suffix(".soft.srt")
        try:
            subprocess.run(
                [ffmpeg, "-y", "-i", str(media_path), "-map", "0:s:0", str(out_srt)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=600,
            )
        except (subprocess.SubprocessError, OSError):
            return []
        if not out_srt.is_file():
            return []

        from .subtitle_normalize import parse_srt

        segments = parse_srt(out_srt.read_text(encoding="utf-8", errors="replace"))
        if segments:
            for seg in segments:
                seg.source = "soft_subtitle"
        else:
            warnings.append("媒体里有字幕轨，但提取后没有可用文本")
        return segments

    # -- 字幕来源：四个独立获取方法 + 显式调度 -----------------------
    def _get_platform_subtitles(
        self, video: VideoInfo, part: VideoPart
    ) -> tuple[list[Segment], Any, list[str]]:
        """Level 1：B 站官方/AI 字幕。不下载任何媒体。"""
        segs, track, warns = resolve_platform_subtitles(
            video.bvid, part.cid, self.client,
            preferred_languages=self.cfg.preferred_languages,
            min_segments=self.cfg.min_segments,
            duration_ms=(part.duration_s or 0) * 1000 or None,
        )
        return segs, track, warns

    def _get_soft_subtitles(
        self, media_path: Path, warnings: list[str]
    ) -> list[Segment]:
        """Level 2：媒体容器内的独立字幕轨。"""
        return self._soft_subtitle_segments(media_path, warnings)

    def _get_asr_subtitles(
        self,
        media_path: Path,
        out_dir: Path,
        warnings: list[str],
    ) -> tuple[list[Segment], dict[str, Any] | None]:
        """Level 4：本地 Qwen3-ASR。"""
        adapter = ASRAdapter(
            python=self.cfg.asr_python,
            model_path=self.cfg.asr_model_path,
            device=self.cfg.asr_device,
            chunk_seconds=self.cfg.asr_max_chunk_seconds,
            aligner_path=self.cfg.asr_aligner_path,
            ffmpeg=self.cfg.ffmpeg,
            language=self.cfg.asr_language,
        )
        problems = adapter.diagnose()
        if problems:
            for p in problems:
                warnings.append(p)
            return [], None

        wav_path = out_dir / "raw" / "audio_16k.wav"
        try:
            adapter.extract_wav(
                media_path, wav_path, sample_rate=self.cfg.asr_sample_rate
            )
        except ASRError as exc:
            warnings.append(f"音频提取失败：{exc}")
            return [], None

        try:
            result = adapter.transcribe(wav_path, out_json=out_dir / "raw" / "asr.json")
        except ASRError as exc:
            warnings.append(f"ASR 失败：{exc}")
            return [], None

        warnings.extend(result.warnings)
        if not result.ok:
            warnings.append("ASR 没有识别出可用文字")
            return [], result.meta
        return result.segments, result.meta

    def _resolve_subtitles(
        self,
        video: VideoInfo,
        part: VideoPart,
        out_dir: Path,
        *,
        subtitle_source: str,
        warnings: list[str],
    ) -> tuple[list[Segment], str, dict[str, Any] | None, Any]:
        """按 ``subtitle_source`` 的显式语义调度字幕来源。

        | source    | 允许来源                                   | 失败行为                     |
        | --------- | ------------------------------------------ | ---------------------------- |
        | platform  | 仅 B 站字幕                                | 报缺字幕，不下载媒体         |
        | ocr       | 仅视频烧录字幕 OCR                          | 不足则明确失败，不偷偷 ASR   |
        | asr       | 仅音频 Qwen3-ASR                            | 不跑 OCR/平台/软字幕         |
        | auto      | 平台 → 软字幕 → OCR(启用时) → ASR           | 任一级不完整可补缺口并记录   |
        """
        # ---- platform：只用 B 站字幕，不下载媒体 ----
        if subtitle_source == "platform":
            segs, track, warns = self._get_platform_subtitles(video, part)
            warnings.extend(warns)
            if not segs:
                warnings.append("按 --subtitle-source platform 语义：没有平台字幕，且不下载媒体")
            return segs, ("platform_subtitle" if segs else "none"), None, track

        # ---- asr：只用语音转写，优先 audio-only ----
        if subtitle_source == "asr":
            try:
                media_files = self._ensure_media(video, part, media_mode="audio-only")
            except BdlError as exc:
                warnings.append(f"媒体下载失败：{exc}")
                return [], "none", None, None
            if not media_files:
                return [], "none", None, None
            segs, meta = self._get_asr_subtitles(Path(media_files[0]), out_dir, warnings)
            if not segs:
                warnings.append("按 --subtitle-source asr 语义：语音转写未产出文本")
            return segs, ("qwen3_asr" if segs else "none"), meta, None

        # ---- ocr：只跑烧录字幕 OCR，失败不回落 ASR ----
        if subtitle_source == "ocr":
            try:
                media_files = self._ensure_media(video, part, media_mode="audio-video")
            except BdlError as exc:
                warnings.append(f"媒体下载失败：{exc}")
                return [], "none", None, None
            if not media_files:
                return [], "none", None, None
            segs, stats = self._run_ocr(
                video, part, Path(media_files[0]), out_dir, warnings
            )
            if not segs:
                warnings.append(
                    "按 --subtitle-source ocr 语义：烧录字幕 OCR 未产出可用文本，"
                    "不自动回退到语音转写"
                )
            return segs, ("hard_subtitle_ocr" if segs else "none"), stats, None

        # ---- auto：平台 → 软字幕 → OCR → ASR ----
        segs, track, warns = self._get_platform_subtitles(video, part)
        warnings.extend(warns)
        if segs:
            return segs, "platform_subtitle", None, track

        # 没有平台字幕：需要媒体（OCR 要画面），统一下载一次
        try:
            media_files = self._ensure_media(video, part, media_mode="audio-video")
        except BdlError as exc:
            warnings.append(f"媒体下载失败：{exc}")
            return [], "none", None, None
        if not media_files:
            return [], "none", None, None
        media_path = Path(media_files[0])

        soft = self._get_soft_subtitles(media_path, warnings)
        if soft:
            return soft, "soft_subtitle", None, None

        ocr_segments: list[Segment] = []
        ocr_stats: dict[str, Any] = {}
        if self.cfg.ocr_enabled and self.cfg.ocr_auto:
            ocr_segments, ocr_stats = self._run_ocr(
                video, part, media_path, out_dir, warnings
            )
            duration_ms = (part.duration_s or 0) * 1000 or None
            if ocr_segments and self._ocr_is_sufficient(
                ocr_segments, duration_ms, ocr_stats
            ):
                return ocr_segments, "hard_subtitle_ocr", ocr_stats, None
            if ocr_segments:
                warnings.append(
                    "烧录字幕" + str(ocr_stats.get("insufficient_reason", "覆盖不足"))
                    + "，改用语音转写补缺口"
                )
        else:
            warnings.append("OCR 已禁用（ocr.enabled/ocr.auto），跳过烧录字幕识别")

        asr_segments, meta = self._get_asr_subtitles(media_path, out_dir, warnings)
        if not asr_segments:
            if ocr_segments:
                # ASR 不可用，但 OCR 仍有内容 → 用 OCR 的结果，不算完全失败
                return ocr_segments, "hard_subtitle_ocr", ocr_stats, None
            return [], "none", meta, None

        if not ocr_segments:
            return asr_segments, "qwen3_asr", meta, None

        # O3-4：OCR 缺口用 ASR 补齐，合并后每段保留自己的 source
        merged, conflicts = self._merge_ocr_asr(ocr_segments, asr_segments)
        stats = dict(ocr_stats)
        stats["asr"] = meta
        stats["ocr_asr_conflicts"] = conflicts
        stats["merged_sources"] = sorted({s.source for s in merged})
        stats["merge_strategy"] = (
            "ASR 段与 OCR 重叠时按重叠比例处理：完全覆盖且文本一致才去重，"
            "否则保留 ASR 原文并标记 overlap_with_ocr（不做伪时间切分）"
        )
        # 粗粒度整段 ASR 单独留档，避免与 OCR 合并后信息遗失
        try:
            alt_path = out_dir / "raw" / "alternate_evidence.json"
            alt_path.parent.mkdir(parents=True, exist_ok=True)
            alt_path.write_text(
                json.dumps({
                    "asr_segments": [s.to_dict() for s in asr_segments],
                    "ocr_segments": [s.to_dict() for s in ocr_segments],
                    "conflicts": conflicts,
                    "strategy": stats["merge_strategy"],
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
        return merged, "hard_subtitle_ocr+qwen3_asr", stats, None

    def _run_ocr(
        self,
        video: VideoInfo,
        part: VideoPart,
        media_path: Path,
        out_dir: Path,
        warnings: list[str],
    ) -> tuple[list[Segment], dict[str, Any]]:
        """Level 3：跑烧录字幕 OCR。

        返回 (识别到的段, 统计)。**即使覆盖不足也返回识别到的段**，
        由调用方决定是直接采用，还是用 ASR 补缺口（O3-4）。
        """
        from .ocr_subtitles import OcrConfig, SubtitleOcr

        selector = SubtitleOcr(
            ffmpeg=self.cfg.ffmpeg,
            config=OcrConfig(
                band=self.cfg.ocr_band,
                band_top=self.cfg.ocr_band_top,
                band_bottom=self.cfg.ocr_band_bottom,
                top_band_top=self.cfg.ocr_top_band_top,
                top_band_bottom=self.cfg.ocr_top_band_bottom,
                sample_interval=self.cfg.ocr_sample_interval,
                min_score=self.cfg.ocr_min_score,
            ),
        )
        problems = selector.diagnose()
        if problems:
            for p in problems:
                warnings.append(p)
            return [], {}

        duration_ms = (part.duration_s or 0) * 1000 or None
        try:
            result = selector.run(media_path, duration_ms=duration_ms)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"烧录字幕 OCR 失败：{exc}")
            return [], {}

        warnings.extend(result.warnings)
        stats: dict[str, Any] = dict(result.stats)
        stats["on_screen_text"] = result.on_screen_text
        if result.segments and not self._ocr_is_sufficient(
            result.segments, duration_ms, stats
        ):
            stats["insufficient_reason"] = stats.get("insufficient_reason", "覆盖不足")
        return result.segments, stats

    @staticmethod
    def _merge_ocr_asr(
        ocr_segments: list[Segment], asr_segments: list[Segment]
    ) -> tuple[list[Segment], list[dict[str, Any]]]:
        """O3-4：OCR 缺口用 ASR 补齐，按时间去重合并。

        关键修正：**不能因为有任意重叠就丢掉整条 ASR**——
        那会让 30s 块级 ASR 因为 2s 重叠而整段消失。

        规则：
        * 无重叠 → 原样加入；
        * **完全被覆盖且文本高度一致** → 去重（保留画面原字）；
        * 部分重叠 / 文本不一致 → **保留 ASR 原文**并打 ``overlap_with_ocr`` 标记，
          同时把冲突记进结构化证据。块级 ASR **不做字符比例切分**，
          避免伪造词级时间戳；两种来源的原始文本都保留。
        """
        from .subtitle_normalize import merged_time_intervals

        try:
            from .ocr_subtitles import normalize_text, similarity
        except ImportError:  # pragma: no cover
            import re

            def normalize_text(text: str) -> str:
                return re.sub(r"\s+", "", text or "")

            def similarity(a: str, b: str) -> float:
                from difflib import SequenceMatcher

                return SequenceMatcher(None, a, b).ratio() if a and b else 0.0

        covered = merged_time_intervals(ocr_segments)
        conflicts: list[dict[str, Any]] = []
        merged = list(ocr_segments)

        for asr in asr_segments:
            overlap_ms = 0
            for start, end in covered:
                overlap_ms += max(0, min(asr.end_ms, end) - max(asr.start_ms, start))
            duration = max(1, asr.end_ms - asr.start_ms)
            ratio = overlap_ms / duration

            if ratio <= 0.0:
                merged.append(asr)
                continue

            # 有重叠：找出重叠的 OCR 段，比较文本
            similar = False
            for ocr in ocr_segments:
                if asr.start_ms < ocr.end_ms and asr.end_ms > ocr.start_ms:
                    a, b = normalize_text(asr.text), normalize_text(ocr.text)
                    if a and b and a == b:
                        similar = True
                    elif a and b and similarity(a, b) >= 0.8:
                        similar = True
                    else:
                        conflicts.append({
                            "start_ms": max(asr.start_ms, ocr.start_ms),
                            "end_ms": min(asr.end_ms, ocr.end_ms),
                            "overlap_ratio": round(ratio, 4),
                            "ocr_text": ocr.text,
                            "asr_text": asr.text,
                            "ocr_source": ocr.source,
                            "asr_source": asr.source,
                            "reason": "同一时段的画面文字与语音转写不一致"
                                      "（可能是翻译/意译），双方原文均保留",
                        })
                    break

            if ratio >= 0.95 and similar:
                # 完全被覆盖且内容一致 → 去重，保留画面原字
                continue

            # 部分重叠或内容不一致 → 保留 ASR 原文并标记
            asr.flags = list(asr.flags or []) + ["overlap_with_ocr"]
            merged.append(asr)

        merged.sort(key=lambda s: (s.start_ms, s.end_ms))
        return merged, conflicts

    def _ocr_is_sufficient(
        self,
        segments: list[Segment],
        duration_ms: int | None,
        stats: dict[str, Any],
    ) -> bool:
        """判断 OCR 结果是否足够完整。

        不能只看"整片覆盖率"这一个比例：
        * 短教程里大量静音会让比例天然偏低，但字幕其实是全的；
        * 语音连续却只识别出几句，则不能声明完整。
        所以同时看**时间并集覆盖率**与**采样帧里有字幕的比例**。
        """
        covered = int(stats.get("covered_ms") or 0)
        ratio = (covered / duration_ms) if duration_ms else 0.0
        if ratio >= self.cfg.ocr_min_coverage:
            return True

        sampled = int(stats.get("sampled_frames") or 0)
        with_text = int(stats.get("frames_with_text") or 0)
        density = (with_text / sampled) if sampled else 0.0
        if density >= 0.4:
            # 采样帧里大部分都有字幕 → 字幕是密集的，只是总时长占比低（长静音）
            stats["insufficient_reason"] = (
                f"覆盖率 {ratio:.1%} 偏低但采样密度 {density:.0%} 较高，判定为足够"
            )
            return True
        stats["insufficient_reason"] = (
            f"覆盖率 {ratio:.1%} 低于阈值 {self.cfg.ocr_min_coverage:.1%} "
            f"且采样密度 {density:.0%} 偏低"
        )
        return False

    def _build_frames(
        self,
        video: VideoInfo,
        part: VideoPart,
        segments: list[Segment],
        out_dir: Path,
    ) -> tuple[list[Frame], list[str]]:
        """图文模式：准备视频并挑关键帧。"""
        warnings: list[str] = []
        try:
            media_files = self._ensure_media(video, part, media_mode="audio-video")
        except BdlError as exc:
            return [], [f"图文模式需要视频，本次下载失败：{exc}"]
        if not media_files:
            return [], ["图文模式没有可用的视频文件"]

        from .frame_selector import FrameSelector, FrameSelectorConfig

        selector = FrameSelector(
            ffmpeg=self.cfg.ffmpeg,
            config=FrameSelectorConfig(
                max_images=self.cfg.max_images,
                min_gap_seconds=self.cfg.min_gap_seconds,
                max_edge=self.cfg.max_edge,
                image_format=self.cfg.image_format,
                phash_distance=self.cfg.phash_distance,
            ),
        )
        frames, warns = selector.select(
            Path(media_files[0]), segments, out_dir / "assets"
        )
        warnings.extend(warns)
        return frames, warnings

    # -- 内部 -----------------------------------------------------------
    def _select_parts(
        self, video: VideoInfo, parts: list[int] | None, warnings: list[str]
    ) -> list[VideoPart]:
        if not parts:
            return video.parts[:1]
        by_page = {p.page: p for p in video.parts}
        picked: list[VideoPart] = []
        for page in parts:
            if page in by_page:
                picked.append(by_page[page])
            else:
                warnings.append(f"分 P {page} 不存在，已跳过")
        return picked

    def _part_meta(self, video: VideoInfo, part: VideoPart) -> dict[str, Any]:
        return {
            "page": part.page,
            "cid": part.cid,
            "title": part.title,
            "duration_s": part.duration_s,
            "url": f"{video.source_url}?p={part.page}",
        }

    # -- 模式产物要求 ----------------------------------------------------
    @staticmethod
    def _frames_component_fresh(
        directory: Path,
        manifest: dict[str, Any] | None,
        digest: str,
        *,
        need_article: bool,
    ) -> bool:
        """帧组件是否可复用。

        必须逐张核对**真实图片文件**存在且非空，并且 frame_count 与清单一致；
        illustrated/both 还要 article.md 存在。缺任何一张图都要重新抽帧。
        """
        if not manifest or manifest.get("status") == "failed":
            return False
        comp = get_component(manifest, "frames")
        if not comp or comp.get("digest") != digest:
            return False
        image_files = comp.get("image_files") or []
        if not image_files:
            return False
        if int(comp.get("frame_count") or 0) != len(image_files):
            return False
        for rel in image_files:
            p = Path(directory) / rel
            if not p.is_file() or p.stat().st_size == 0:
                return False
        if need_article:
            article = comp.get("article_file")
            if not article:
                return False
            p = Path(directory) / article
            if not p.is_file() or p.stat().st_size == 0:
                return False
        return True

    def _image_suffix(self) -> str:
        """当前配置下的图片后缀（用于缺图提示，不写死 jpg）。"""
        return {"jpg": "jpg", "jpeg": "jpg", "webp": "webp", "png": "png"}.get(
            (self.cfg.image_format or "jpg").lower(), "jpg"
        )

    @staticmethod
    def _required_artifacts(mode: str) -> tuple[list[str], bool]:
        """返回 (必需产物相对路径列表, 是否需要字幕)。

        - subtitle：transcript.srt + transcript.md
        - illustrated / both：上述字幕 + article.md + 至少一张关键帧
        - assets：只要关键帧，**不要求任何字幕**
        - metadata：元信息即可
        """
        if mode == "subtitle":
            return ["transcript.srt", "transcript.md"], True
        if mode in ("illustrated", "both"):
            return ["transcript.srt", "transcript.md", "article.md"], True
        if mode == "assets":
            return [], False
        return [], False

    def _fetch_part(
        self,
        video: VideoInfo,
        part: VideoPart,
        *,
        mode: str,
        subtitle_source: str,
        refresh: bool,
        warnings: list[str],
    ) -> dict[str, Any]:
        out_dir = part_dir(self.cfg.output_path, video.bvid, part.page, part.cid)
        out_dir.mkdir(parents=True, exist_ok=True)

        # 组件级摘要：字幕与关键帧分开记账，改 max_images 不会作废字幕，
        # 改 ASR 模型也不会误用旧图片。
        transcript_digest = settings_digest(
            pipeline=PIPELINE_VERSION, kind="transcript",
            subtitle_source=subtitle_source,
            languages=self.cfg.preferred_languages,
            bvid=video.bvid, cid=part.cid,
            ocr_band=self.cfg.ocr_band,
            ocr_sample_interval=self.cfg.ocr_sample_interval,
            ocr_min_score=self.cfg.ocr_min_score,
            asr_model=self.cfg.asr_model_path,
            asr_aligner=self.cfg.asr_aligner_path,
            asr_chunk=self.cfg.asr_max_chunk_seconds,
            asr_language=self.cfg.asr_language,
        )
        frames_digest = settings_digest(
            pipeline=PIPELINE_VERSION, kind="frames",
            bvid=video.bvid, cid=part.cid,
            max_images=self.cfg.max_images,
            min_gap_seconds=self.cfg.min_gap_seconds,
            phash_distance=self.cfg.phash_distance,
            max_edge=self.cfg.max_edge,
            image_format=self.cfg.image_format,
        )
        digest = settings_digest(
            transcript=transcript_digest, frames=frames_digest, mode=mode
        )

        existing = read_manifest(out_dir)
        transcript_ok = component_is_fresh(
            out_dir, existing, "transcript", transcript_digest
        )
        frames_ok = self._frames_component_fresh(
            out_dir, existing, frames_digest,
            need_article=mode in ("illustrated", "both"),
        )

        def _cached() -> dict[str, Any]:
            manifest = existing or {}
            frame_comp = get_component(manifest, "frames") or {}
            return {
                "page": part.page, "cid": part.cid, "ok": True,
                "status": manifest.get("status", "completed"),
                "cached": True,
                "outputs": [
                    str(out_dir / rel) for rel in (manifest.get("files") or [])
                ],
                "subtitle_source": manifest.get("subtitle_source"),
                "directory": str(out_dir),
                "image_files": [
                    str(out_dir / rel)
                    for rel in (frame_comp.get("image_files") or [])
                ],
                "chapters": (manifest.get("extra") or {}).get("chapters") or [],
            }

        if not refresh:
            if mode == "subtitle" and transcript_ok:
                return _cached()
            if mode in ("illustrated", "both") and transcript_ok and frames_ok:
                return _cached()
            if mode == "assets" and frames_ok:
                return _cached()

        required, needs_subtitles = self._required_artifacts(mode)
        needs_frames = mode in ("illustrated", "both", "assets")

        segments: list[Segment] = []
        used_source = "none"
        track_info: dict[str, Any] | None = None
        extra_meta: dict[str, Any] | None = None
        reused_transcript = False

        # ---- 字幕（按显式语义调度；可复用已有 transcript 组件） ----
        if needs_subtitles:
            reusable = (
                transcript_ok and not refresh
                and (get_component(existing, "transcript") or {}).get(
                    "subtitle_source"
                ) == subtitle_source
            )
            if reusable:
                srt_path = out_dir / "transcript.srt"
                try:
                    segments = parse_srt(
                        srt_path.read_text(encoding="utf-8", errors="replace")
                    )
                except OSError:
                    segments = []
                if segments:
                    used_source = (
                        get_component(existing, "transcript") or {}
                    ).get("source", "platform_subtitle")
                    track_info = (existing.get("extra") or {}).get("subtitle_track")
                    reused_transcript = True
            if not segments:
                segments, used_source, extra_meta, track = self._resolve_subtitles(
                    video, part, out_dir,
                    subtitle_source=subtitle_source,
                    warnings=warnings,
                )
                if track is not None:
                    track_info = track.to_dict()

        chapters = segment_chapters(segments) if segments else []
        files: list[str] = []
        coverage = coverage_stats(segments, (part.duration_s or 0) * 1000 or None)
        if coverage.get("span_exceeds_duration"):
            warnings.append(
                f"字幕时长跨度 {coverage['span_ms']}ms 超过视频时长 "
                f"{(part.duration_s or 0) * 1000}ms（可能是 AI 字幕多轨或分 P 时长不一致）"
            )

        # ---- 文本产物 ----
        if segments:
            write_srt(out_dir / "transcript.srt", to_srt(segments))
            files.append("transcript.srt")
            write_transcript_md(
                out_dir / "transcript.md", video, part, segments,
                subtitle_source=used_source, mode=mode, chapters=chapters,
                extra={"字幕语言": (track_info or {}).get("lan_doc") or ""}
                if track_info else None,
            )
            files.append("transcript.md")

        # ---- 关键帧（assets 模式不依赖字幕） ----
        frames: list[Frame] = []
        if needs_frames:
            frames, frame_warnings = self._build_frames(video, part, segments, out_dir)
            warnings.extend(frame_warnings)
            if frames and segments:
                write_article_md(
                    out_dir / "article.md", video, part, segments, frames,
                    subtitle_source=used_source, mode=mode, chapters=chapters,
                )
                files.append("article.md")
            elif frames and mode == "assets":
                # assets 模式没有字幕就没有 article.md，只留图片
                pass

        # 帧组件以**真实图片清单**为准（不能从 files 反推，
        # 否则 assets 模式的帧清单会是空的，illustrated 模式会漏掉图片）
        image_files = [f.relative_path for f in frames]
        article_file = "article.md" if "article.md" in files else ""
        # manifest.files / outputs 也要包含图片，语义与 components.frames 一致
        for rel in image_files:
            if rel not in files:
                files.append(rel)

        # ---- 按模式校验真实产物 ----
        missing: list[str] = []
        for rel in required:
            path = out_dir / rel
            if not path.is_file() or path.stat().st_size == 0:
                missing.append(rel)
        if needs_frames and not frames:
            missing.append(f"assets/*.{self._image_suffix()}")
        if needs_frames:
            for f in frames:
                p = out_dir / f.relative_path
                if not p.is_file() or p.stat().st_size == 0:
                    missing.append(f.relative_path)
                    break
        if needs_frames and article_file:
            p = out_dir / article_file
            if not p.is_file() or p.stat().st_size == 0:
                missing.append(article_file)

        if missing:
            status = "failed"
        elif segments or frames:
            status = "completed"
        else:
            status = "failed"

        if status == "failed" and missing:
            warnings.append(
                f"模式 {mode} 缺少必需产物：{', '.join(sorted(set(missing)))}"
            )
        elif status == "failed":
            warnings.append(f"模式 {mode} 没有产出任何内容（字幕与关键帧都为空）")
        # 错误阶段要能区分"字幕没拿到"和"抽帧没拿到"，别只报一句"没有可用的字幕内容"
        text_missing = any(m.startswith("transcript.") for m in missing)
        frame_missing = any(
            m.startswith("article.md") or m.startswith("assets/") for m in missing
        )
        if text_missing and not frame_missing:
            error_stage = "subtitle"
        elif frame_missing and not text_missing:
            error_stage = "frames"
        else:
            error_stage = "materialize"

        manifest = Manifest(
            bvid=video.bvid, cid=part.cid, page=part.page,
            source_url=f"{video.source_url}?p={part.page}",
            title=video.title, author=video.author,
            duration_ms=(part.duration_s or 0) * 1000 or None,
            generated_at=_now_iso(),
            subtitle_source=used_source,
            mode=mode,
            model=(extra_meta or {}).get("model") if extra_meta else None,
            pipeline_version=PIPELINE_VERSION,
            settings_digest=digest,
            files=files,
            warnings=list(warnings),
            coverage=coverage,            extra={
                **({"subtitle_track": track_info} if track_info else {}),
                **({"asr": extra_meta.get("asr")} if extra_meta and extra_meta.get("asr") else {}),
                **({"frames": [f.to_dict() for f in frames]} if frames else {}),
                **({"chapters": [c.to_dict() for c in chapters]} if chapters else {}),
                # O4：画面文字走独立通道，不混进对白 transcript
                **({
                    "visual_text": extra_meta.get("on_screen_text")
                } if extra_meta and extra_meta.get("on_screen_text") else {}),
                **({
                    "ocr_region": extra_meta.get("region_detection")
                } if extra_meta and extra_meta.get("region_detection") else {}),
                **({
                    "ocr_asr_conflicts": extra_meta.get("ocr_asr_conflicts")
                } if extra_meta and extra_meta.get("ocr_asr_conflicts") else {}),
            },
        )
        payload = manifest.to_dict()
        payload["status"] = status
        payload["missing_artifacts"] = sorted(set(missing))
        # 组件级记账：字幕与关键帧分开，改一个不会作废另一个
        transcript_files = [f for f in files if f.startswith("transcript.")]
        # 帧组件登记**真实图片清单**（不是从 files 反推）
        frame_files = list(image_files)
        if article_file:
            frame_files.append(article_file)
        payload["components"] = {
            "transcript": {
                "digest": transcript_digest,
                # requested=用户请求的来源模式（platform/ocr/asr/auto）
                # source=实际用到的来源（platform_subtitle/qwen3_asr/...）
                "subtitle_source": subtitle_source,
                "source": used_source,
                "files": transcript_files,
                "reused": reused_transcript,
            },
            "frames": {
                "digest": frames_digest,
                "files": frame_files,
                "frame_count": len(frames),
                "image_files": list(image_files),
                "article_file": article_file,
            },
        }
        write_manifest(out_dir / "manifest.json", payload)

        return {
            "page": part.page, "cid": part.cid, "ok": status == "completed",
            "status": status,
            "cached": False,
            "subtitle_source": used_source,
            "directory": str(out_dir),
            "outputs": [str(out_dir / name) for name in files],
            "coverage": coverage,
            "frames": len(frames),
            "image_files": [str(out_dir / rel) for rel in image_files],
            "chapters": [c.to_dict() for c in chapters],
            **({
                "error": {
                    "stage": error_stage,
                    "error_code": -404,
                    "message": (
                        f"模式 {mode} 的必需产物缺失："
                        f"{', '.join(sorted(set(missing)))}"
                    ),
                    "retryable": False,
                }
            } if status != "completed" else {}),
        }


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any] | None:
    import json

    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def probe_media(path: Path, ffprobe: str) -> dict[str, Any] | None:
    """用 ffprobe 读取媒体信息，验证文件真的可读。"""
    exe = ffprobe or shutil.which("ffprobe") or ""
    if not exe or not Path(path).is_file():
        return None
    try:
        proc = subprocess.run(
            [exe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    import json

    try:
        data = json.loads(proc.stdout or "{}")
    except (json.JSONDecodeError, ValueError):
        return None
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    return {
        "path": str(path),
        "size_bytes": Path(path).stat().st_size,
        "duration_s": float(fmt.get("duration", 0) or 0),
        "format_name": fmt.get("format_name"),
        "streams": [
            {
                "codec_type": s.get("codec_type"),
                "codec_name": s.get("codec_name"),
                "width": s.get("width"),
                "height": s.get("height"),
                "channels": s.get("channels"),
                "sample_rate": s.get("sample_rate"),
            }
            for s in streams
        ],
    }
