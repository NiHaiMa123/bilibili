"""Level 1：B 站可下载字幕。

通过播放器接口拿到字幕清单，再从官方 CDN 取字幕正文。
纯字幕模式成功即停止，不下载视频、不加载 ASR。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .bili_api import BiliApiError, BiliClient, is_trusted_subtitle_url
from .schemas import Segment


@dataclass(slots=True)
class SubtitleTrack:
    """一条字幕轨道的元信息。"""

    lan: str
    lan_doc: str
    url: str
    type: int | None = None
    ai_status: int | None = None
    ai_type: int | None = None
    body: str | None = field(default=None, repr=False)

    @property
    def is_ai(self) -> bool:
        """这条轨道是不是 AI 生成/翻译的字幕。

        **不能靠 URL 判断**：人工字幕和 AI 字幕同样托管在 ``aisubtitle.hdslb.com``，
        用域名区分会把人工字幕误判成 AI 字幕（进而选错轨道）。
        可靠依据是：``lan`` 以 ``ai-`` 开头、``ai_status``/``ai_type`` 非零，
        或 ``type == 1``（实测人工字幕为 0、AI 字幕为 1）。
        """
        if (self.lan or "").lower().startswith("ai-"):
            return True
        if self.ai_status is not None and self.ai_status > 0:
            return True
        if self.ai_type is not None and self.ai_type > 0:
            return True
        if self.type is not None and self.type == 1:
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "lan": self.lan,
            "lan_doc": self.lan_doc,
            "type": self.type,
            "is_ai": self.is_ai,
            "ai_status": self.ai_status,
        }


def _lang_rank(lan: str, preferred: list[str]) -> int:
    """按用户指定 > 中文 > 原语言的顺序打分。"""
    low = (lan or "").lower()
    for idx, want in enumerate(preferred):
        if low == want.lower():
            return idx
    if low.startswith("zh") or low in ("cn", "chs", "cht"):
        return len(preferred)
    return len(preferred) + 1


def _track_rank(track: SubtitleTrack, preferred: list[str]) -> tuple:
    """字幕轨道排序键。

    规则：语言优先级优先 → 人工字幕优先于 AI 字幕 → 同级按 lan 稳定排序。
    ``ai-zh`` 这类"AI + 语言"轨会被当作对应语言的 AI 版本，排在人工轨之后。
    """
    low = (track.lan or "").lower()
    wanted = {p.lower() for p in preferred}

    rank = len(preferred) + 1
    for idx, want in enumerate(preferred):
        if low == want:
            rank = idx
            break
    else:
        if low.startswith("ai-"):
            tail = low[3:]
            rank = len(preferred) if tail in wanted else len(preferred) + 1
        elif low.startswith("zh") or low in ("cn", "chs", "cht"):
            rank = len(preferred)
        elif any(low.startswith(w + "-") for w in wanted):
            rank = len(preferred)

    return (rank, 1 if track.is_ai else 0, track.lan)


def list_subtitle_tracks(
    bvid: str,
    cid: int,
    client: BiliClient,
) -> list[SubtitleTrack]:
    """列出该分 P 的所有字幕轨道（未拉正文）。

    同时问两个接口再合并：
    - ``x/player/wbi/v2``：网页播放器真正在用的接口，给出完整轨道集合
      （人工字幕 + 各种 AI 翻译轨）。
    - ``x/player/v2``：老接口，有时会单独给出人工字幕轨。

    两个接口返回的内容并不稳定（同一视频不同次调用可能给出不同的轨道集合），
    所以合并去重后再挑。
    """
    tracks: dict[str, SubtitleTrack] = {}

    def collect(payload: dict[str, Any]) -> None:
        subtitle = (payload or {}).get("subtitle") or {}
        for raw in subtitle.get("subtitles") or []:
            url = str(raw.get("subtitle_url") or "")
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            track = SubtitleTrack(
                lan=str(raw.get("lan") or ""),
                lan_doc=str(raw.get("lan_doc") or ""),
                url=url,
                type=raw.get("type"),
                ai_status=raw.get("ai_status"),
                ai_type=raw.get("ai_type"),
            )
            tracks.setdefault(str(raw.get("id") or track.url), track)

    for endpoint in ("x/player/wbi/v2", "x/player/v2"):
        try:
            data = client.get_json(
                f"https://api.bilibili.com/{endpoint}",
                params={"bvid": bvid, "cid": cid},
            )
        except BiliApiError:
            continue
        collect(data.get("data") or {})

    return list(tracks.values())


def fetch_track_body(track: SubtitleTrack, client: BiliClient) -> str:
    """拉取字幕正文文本。只允许官方 CDN 域名。"""
    if not is_trusted_subtitle_url(track.url):
        raise BiliApiError(f"拒绝非官方字幕地址：{track.url}")
    return client.get_text(track.url)


def choose_track(
    tracks: list[SubtitleTrack],
    preferred: list[str],
) -> SubtitleTrack | None:
    """按语言优先级 + 人工字幕优先选一条。"""
    if not tracks:
        return None
    return sorted(tracks, key=lambda t: _track_rank(t, preferred))[0]


def resolve_platform_subtitles(
    bvid: str,
    cid: int,
    client: BiliClient,
    *,
    preferred_languages: list[str] | None = None,
    min_segments: int = 3,
    duration_ms: int | None = None,
) -> tuple[list[Segment], SubtitleTrack | None, list[str]]:
    """Level 1 主流程。

    返回 ``(segments, track, warnings)``。没有可用字幕时 segments 为空列表，
    调用方应继续走 Level 2/3/4，而不是把空文本当成功。

    多条候选轨道时按偏好顺序逐个尝试：取第一条解析成功、且时间跨度与视频时长
    不明显冲突的字幕。跨度异常（例如 AI 字幕覆盖到视频时长的两倍）会被跳过并记录，
    因为那通常意味着拿到了合并版/错配的字幕文件。
    """
    warnings: list[str] = []
    preferred = preferred_languages or ["zh-Hans", "zh-CN", "zh"]
    try:
        tracks = list_subtitle_tracks(bvid, cid, client)
    except BiliApiError as exc:
        return [], None, [f"字幕清单获取失败：{exc}"]

    if not tracks:
        return [], None, ["该分 P 没有平台字幕"]

    ranked = sorted(tracks, key=lambda t: _track_rank(t, preferred))

    from .subtitle_normalize import parse_bilibili_subtitle, validate_segments

    fallback: tuple[list[Segment], SubtitleTrack, str] | None = None
    for track in ranked:
        try:
            body = fetch_track_body(track, client)
        except BiliApiError as exc:
            warnings.append(f"字幕正文下载失败（{track.lan}）：{exc}")
            continue

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            warnings.append(f"字幕正文不是合法 JSON（{track.lan}）")
            continue

        segments = parse_bilibili_subtitle(payload)
        segments, issues = validate_segments(segments, min_segments=min_segments)
        if not segments:
            warnings.append(f"字幕为空或片段过少（{track.lan}）")
            continue

        # 跨度合理性检查：明显超出视频时长的字幕多半是错配的合并文件
        span_ms = segments[-1].end_ms - segments[0].start_ms
        if duration_ms and span_ms > duration_ms * 1.5:
            warnings.append(
                f"字幕轨道 {track.lan} 跨度 {span_ms}ms 远超视频时长 {duration_ms}ms，已跳过"
            )
            if fallback is None:
                fallback = (segments, track, body)
            continue

        warnings.extend(f"{track.lan}: {i}" for i in issues)
        track.body = body
        return segments, track, warnings

    if fallback is not None:
        segments, track, body = fallback
        warnings.append("所有候选字幕的时长跨度都与视频时长不符，已采用最先取到的一条")
        track.body = body
        return segments, track, warnings

    return [], None, warnings or ["没有可用的字幕轨道"]
