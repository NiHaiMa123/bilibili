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
from .cache import is_fresh, part_dir, settings_digest
from .config import Config
from .inspect import inspect_video
from .md_writer import write_article_md, write_manifest, write_srt, write_transcript_md
from .schemas import Frame, Manifest, Segment, VideoInfo, VideoPart
from .search import SearchProvider
from .segmentation import segment_chapters
from .subtitle_normalize import coverage_stats, to_srt
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
        failed = False

        for part in selected:
            try:
                res = self._fetch_part(
                    video, part, mode=mode, subtitle_source=subtitle_source,
                    refresh=refresh, warnings=warnings,
                )
            except Exception as exc:  # noqa: BLE001
                failed = True
                part_results.append({
                    "page": part.page, "cid": part.cid, "ok": False,
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
            if not res.get("ok"):
                failed = True

        return FetchResult(
            ok=not failed,
            status="failed" if failed else "completed",
            bvid=video.bvid,
            title=video.title,
            parts=part_results,
            outputs=all_outputs,
            warnings=warnings,
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

    def _fallback_to_media(
        self,
        video: VideoInfo,
        part: VideoPart,
        out_dir: Path,
        warnings: list[str],
        *,
        media_mode: str = "audio-only",
        try_ocr: bool = False,
    ) -> tuple[list[Segment], str, list[str], dict[str, Any] | None]:
        """平台字幕缺失时的兜底：Level 2 软字幕 → Level 3 OCR → Level 4 ASR。"""
        meta: dict[str, Any] | None = None
        # OCR 需要画面，必须有视频流
        need_video = try_ocr
        try:
            media_files = self._ensure_media(
                video, part, media_mode="audio-video" if need_video else media_mode
            )
        except BdlError as exc:
            warnings.append(f"媒体下载失败：{exc}")
            return [], "none", warnings, None
        if not media_files:
            return [], "none", warnings, None

        media_path = Path(media_files[0])

        # Level 2：容器内的独立字幕轨
        soft = self._soft_subtitle_segments(media_path, warnings)
        if soft:
            return soft, "soft_subtitle", warnings, meta

        # Level 3：画面烧录字幕 OCR
        if try_ocr:
            segs, ocr_meta = self._run_ocr(video, part, media_path, out_dir, warnings)
            if segs:
                return segs, "hard_subtitle_ocr", warnings, ocr_meta
            # OCR 覆盖不足 → 继续走 ASR

        # Level 4：本地 Qwen3-ASR
        adapter = ASRAdapter(
            python=self.cfg.asr_python,
            model_path=self.cfg.asr_model_path,
            device=self.cfg.asr_device,
            chunk_seconds=self.cfg.asr_max_chunk_seconds,
            aligner_path=self.cfg.asr_aligner_path,
            ffmpeg=self.cfg.ffmpeg,
        )
        problems = adapter.diagnose()
        if problems:
            for p in problems:
                warnings.append(p)
            warnings.append("没有平台字幕，也没有可用的 ASR 环境；本次不产出文本内容")
            return [], "none", warnings, None

        wav_path = out_dir / "raw" / "audio_16k.wav"
        try:
            adapter.extract_wav(media_path, wav_path, sample_rate=self.cfg.asr_sample_rate)
        except ASRError as exc:
            warnings.append(f"音频提取失败：{exc}")
            return [], "none", warnings, None

        try:
            result = adapter.transcribe(wav_path, out_json=out_dir / "raw" / "asr.json")
        except ASRError as exc:
            warnings.append(f"ASR 失败：{exc}")
            return [], "none", warnings, None

        warnings.extend(result.warnings)
        if not result.ok:
            warnings.append("ASR 没有识别出可用文字")
            return [], "none", warnings, result.meta
        return result.segments, "qwen3_asr", warnings, result.meta

    def _run_ocr(
        self,
        video: VideoInfo,
        part: VideoPart,
        media_path: Path,
        out_dir: Path,
        warnings: list[str],
    ) -> tuple[list[Segment], dict[str, Any] | None]:
        """Level 3：跑烧录字幕 OCR。"""
        from .ocr_subtitles import OcrConfig, SubtitleOcr

        selector = SubtitleOcr(
            ffmpeg=self.cfg.ffmpeg,
            config=OcrConfig(
                band=self.cfg.ocr_band,
                band_top=self.cfg.ocr_band_top,
                band_bottom=self.cfg.ocr_band_bottom,
                sample_interval=self.cfg.ocr_sample_interval,
                min_score=self.cfg.ocr_min_score,
            ),
        )
        problems = selector.diagnose()
        if problems:
            for p in problems:
                warnings.append(p)
            return [], None

        duration_ms = (part.duration_s or 0) * 1000 or None
        try:
            result = selector.run(media_path, duration_ms=duration_ms)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"烧录字幕 OCR 失败：{exc}")
            return [], None

        warnings.extend(result.warnings)
        coverage = float(result.stats.get("coverage_ratio") or 0.0)
        if result.segments and coverage < self.cfg.ocr_min_coverage:
            warnings.append(
                f"烧录字幕覆盖率 {coverage:.1%} 低于阈值 {self.cfg.ocr_min_coverage:.1%}，"
                "改用语音转写"
            )
            return [], result.stats
        return result.segments, result.stats

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

        digest = settings_digest(
            pipeline=PIPELINE_VERSION,
            mode=mode,
            subtitle_source=subtitle_source,
            languages=self.cfg.preferred_languages,
            bvid=video.bvid,
            cid=part.cid,
        )

        if not refresh and is_fresh(out_dir, digest, mode):
            manifest = _read_json(out_dir / "manifest.json") or {}
            return {
                "page": part.page, "cid": part.cid, "ok": True,
                "cached": True,
                "outputs": manifest.get("files") or [],
                "subtitle_source": manifest.get("subtitle_source"),
                "directory": str(out_dir),
            }

        segments: list[Segment] = []
        used_source = "none"
        track_info: dict[str, Any] | None = None
        asr_meta: dict[str, Any] | None = None

        # Level 1：平台字幕
        if subtitle_source in ("auto", "platform"):
            segs, track, warns = resolve_platform_subtitles(
                video.bvid, part.cid, self.client,
                preferred_languages=self.cfg.preferred_languages,
                min_segments=self.cfg.min_segments,
                duration_ms=(part.duration_s or 0) * 1000 or None,
            )
            warnings.extend(warns)
            if segs:
                segments = segs
                used_source = "platform_subtitle"
                if track is not None:
                    track_info = track.to_dict()

        # Level 2/3/4：没有平台字幕时，软字幕 → 烧录字幕 OCR → 语音转写
        wants_video = mode in ("illustrated", "both")
        if not segments and subtitle_source in ("auto", "ocr", "asr"):
            try_ocr = (
                self.cfg.ocr_enabled
                and (subtitle_source == "ocr" or self.cfg.ocr_auto)
            )
            segs, source, warns, meta = self._fallback_to_media(
                video, part, out_dir, warnings,
                media_mode="audio-video" if wants_video else "audio-only",
                try_ocr=try_ocr,
            )
            warnings.extend(warns)
            if segs:
                segments = segs
                used_source = source
                asr_meta = meta

        if not segments:
            manifest = Manifest(
                bvid=video.bvid, cid=part.cid, page=part.page,
                source_url=f"{video.source_url}?p={part.page}",
                title=video.title, author=video.author,
                duration_ms=(part.duration_s or 0) * 1000 or None,
                generated_at=_now_iso(),
                subtitle_source="none",
                mode=mode,
                pipeline_version=PIPELINE_VERSION,
                settings_digest=digest,
                files=[],
                warnings=list(warnings),
                coverage=coverage_stats([], (part.duration_s or 0) * 1000 or None),
            )
            payload = manifest.to_dict()
            # 标记为失败：后续运行不能把它当缓存命中，否则永远拿不到内容
            payload["status"] = "failed"
            write_manifest(out_dir / "manifest.json", payload)
            return {
                "page": part.page, "cid": part.cid, "ok": False,
                "subtitle_source": "none",
                "directory": str(out_dir),
                "outputs": [],
                "error": {
                    "stage": "subtitle",
                    "error_code": -404,
                    "message": "没有可用的字幕内容",
                    "retryable": False,
                },
            }

        chapters = segment_chapters(segments)
        files: list[str] = []

        coverage = coverage_stats(segments, (part.duration_s or 0) * 1000 or None)
        if coverage.get("span_exceeds_duration"):
            warnings.append(
                f"字幕时长跨度 {coverage['span_ms']}ms 超过视频时长 "
                f"{(part.duration_s or 0) * 1000}ms（可能是 AI 字幕多轨或分 P 时长不一致），"
                "覆盖率已按 1.0 计"
            )

        write_srt(out_dir / "transcript.srt", to_srt(segments))
        files.append("transcript.srt")

        write_transcript_md(
            out_dir / "transcript.md", video, part, segments,
            subtitle_source=used_source, mode=mode, chapters=chapters,
            extra={"字幕语言": (track_info or {}).get("lan_doc") or ""}
            if track_info else None,
        )
        files.append("transcript.md")

        # 图文模式：抽关键帧并生成 article.md
        frames: list[Frame] = []
        if mode in ("illustrated", "both"):
            frames, frame_warnings = self._build_frames(video, part, segments, out_dir)
            warnings.extend(frame_warnings)
            if frames:
                write_article_md(
                    out_dir / "article.md", video, part, segments, frames,
                    subtitle_source=used_source, mode=mode, chapters=chapters,
                )
                files.append("article.md")

        manifest = Manifest(
            bvid=video.bvid, cid=part.cid, page=part.page,
            source_url=f"{video.source_url}?p={part.page}",
            title=video.title, author=video.author,
            duration_ms=(part.duration_s or 0) * 1000 or None,
            generated_at=_now_iso(),
            subtitle_source=used_source,
            mode=mode,
            model=(asr_meta or {}).get("model") if asr_meta else None,
            pipeline_version=PIPELINE_VERSION,
            settings_digest=digest,
            files=files,
            warnings=list(warnings),
            coverage=coverage,
            extra={
                **({"subtitle_track": track_info} if track_info else {}),
                **({"asr": asr_meta} if asr_meta else {}),
                **({"frames": [f.to_dict() for f in frames]} if frames else {}),
            },
        )
        write_manifest(out_dir / "manifest.json", manifest.to_dict())

        return {
            "page": part.page, "cid": part.cid, "ok": True,
            "cached": False,
            "subtitle_source": used_source,
            "directory": str(out_dir),
            "outputs": [str(out_dir / name) for name in files],
            "coverage": coverage,
            "frames": len(frames),
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
