"""锁定具体视频对象：BV/AV/URL → VideoInfo（含分 P 与 cid）。

分 P 的 cid 是本地转换的最小单元：字幕、画面、产物目录都以
``bvid + cid + page_index`` 为准，避免把 P1 的字幕绑到 P2 的画面。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from .bdl_adapter import BdlAdapter
from .bili_api import BiliApiError, BiliClient
from .schemas import VideoInfo, VideoPart

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"\bav(\d+)\b", re.IGNORECASE)
_PAGE_RE = re.compile(r"[?&]p=(\d+)")


class SourceError(RuntimeError):
    """无法解析视频来源。"""

    def __init__(self, message: str, *, stage: str = "inspect", code: int | None = None):
        super().__init__(message)
        self.stage = stage
        self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "error_code": self.code,
            "message": str(self),
            "retryable": False,
        }


def extract_bvid(target: str) -> str | None:
    """从 BV/AV/URL 中提取 BV 号（AV 与短链需网络确认）。"""
    text = (target or "").strip()
    if not text:
        return None
    if m := _BV_RE.search(text):
        return m.group(1)
    if re.fullmatch(r"av\d+", text, re.IGNORECASE):
        return None  # AV 需要一次接口换算
    return None


def extract_page(target: str) -> int | None:
    """URL 上的 ``?p=N``。"""
    if m := _PAGE_RE.search(target or ""):
        return int(m.group(1))
    return None


def _bvid_from_av(client: BiliClient, av: str) -> str:
    data = client.get_json(
        "https://api.bilibili.com/x/web-interface/view", params={"aid": av}
    )
    bvid = (data.get("data") or {}).get("bvid")
    if not bvid:
        raise SourceError(f"无法由 av{av} 解析出 BV 号", code=-404)
    return str(bvid)


# 允许的短链域名（B 站官方短链）
SHORT_LINK_HOSTS = ("b23.tv", "www.b23.tv")
# 短链跳转后允许落到的 B 站视频域名
ALLOWED_VIDEO_HOSTS = (
    "bilibili.com",
    "www.bilibili.com",
    "m.bilibili.com",
    "www.b23.tv",
)
MAX_SHORT_LINK_REDIRECTS = 5


def is_short_link(target: str) -> bool:
    """判断输入是否是 b23.tv 短链（按 scheme + host 判断，不用字符串包含）。"""
    text = (target or "").strip()
    if not text:
        return False
    if "://" not in text:
        text = "https://" + text
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host in SHORT_LINK_HOSTS


def _is_allowed_video_url(url: str) -> bool:
    """校验跳转落点是否是允许的 B 站视频域名。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host in ALLOWED_VIDEO_HOSTS


def _resolve_bvid(client: BiliClient, target: str) -> str:
    """把任意输入解析成 BV 号，必要时联网。"""
    text = (target or "").strip()
    if not text:
        raise SourceError("输入为空")
    if bvid := extract_bvid(text):
        return bvid
    if m := _AV_RE.fullmatch(text) or re.fullmatch(r"av\d+", text, re.IGNORECASE):
        return _bvid_from_av(client, m.group(1))
    if is_short_link(text):
        return _resolve_short_link(client, text)
    raise SourceError(f"无法识别的视频输入：{target}")


def _resolve_short_link(client: BiliClient, url: str) -> str:
    """跟随 b23.tv 短链跳转，校验每一跳的目标域名。

    * 只接受 https；
    * 入口 host 必须是官方短链域名；
    * 每一次跳转的目标都必须是允许的 B 站视频域名；
    * 跳转次数有限；超限或落到非 B 站页面一律明确报错。
    """
    http = client._get_client()  # noqa: SLF001 - 复用已配置 UA/超时的连接池
    current = url.strip()
    if "://" not in current:
        current = "https://" + current

    for _ in range(MAX_SHORT_LINK_REDIRECTS):
        parsed = urlparse(current)
        if parsed.scheme != "https":
            raise SourceError(f"短链必须使用 https：{current}")
        host = (parsed.hostname or "").lower()
        if host not in SHORT_LINK_HOSTS and not _is_allowed_video_url(current):
            raise SourceError(f"短链跳转到不允许的域名：{host or current}")
        try:
            resp = http.get(current)
        except Exception as exc:  # noqa: BLE001 - 统一转成 SourceError
            raise SourceError(f"短链解析失败：{exc}") from exc
        final = str(resp.url)
        if final == current:
            break
        current = final
        # 落点已经不是短链了（就是目标视频页），不必再跳
        if not is_short_link(current):
            break
    else:
        raise SourceError(
            f"短链跳转次数超过 {MAX_SHORT_LINK_REDIRECTS} 次，已放弃：{url}"
        )

    if not _is_allowed_video_url(current):
        raise SourceError(f"短链未跳转到 B 站视频页：{current}")
    if bvid := extract_bvid(current):
        return bvid
    raise SourceError(f"短链未跳转到可识别的视频页：{current}")


def _parts_from_bdl(tree: dict[str, Any]) -> list[VideoPart]:
    """从 BDL 的 NormalizedSourceTree 提取分 P。"""
    parts: list[VideoPart] = []
    for group in tree.get("groups") or []:
        for item in group.get("items") or []:
            for idx, part in enumerate(item.get("parts") or [], start=1):
                cid = part.get("cid")
                if cid is None:
                    continue
                parts.append(
                    VideoPart(
                        page=idx,
                        cid=int(cid),
                        title=str(part.get("title") or f"P{idx}"),
                        duration_s=part.get("duration_seconds"),
                        aid=part.get("aid"),
                        bvid=part.get("bvid"),
                        cover_url=item.get("cover_url"),
                    )
                )
            if parts:
                # 一个 item 对应一个视频，只取第一个非空 item
                break
        if parts:
            break
    return parts


def _parts_from_view(view: dict[str, Any]) -> list[VideoPart]:
    parts: list[VideoPart] = []
    for idx, page in enumerate(view.get("pages") or [], start=1):
        cid = page.get("cid")
        if cid is None:
            continue
        parts.append(
            VideoPart(
                page=int(page.get("page") or idx),
                cid=int(cid),
                title=str(page.get("part") or f"P{idx}"),
                duration_s=page.get("duration"),
                aid=view.get("aid"),
                bvid=view.get("bvid"),
                cover_url=view.get("pic"),
            )
        )
    return parts


def inspect_video(
    target: str,
    *,
    adapter: BdlAdapter | None = None,
    client: BiliClient | None = None,
) -> VideoInfo:
    """解析视频元信息。

    优先使用 BDL CLI 的 ``parse``（计划指定的路径），再用公开 view 接口
    补充简介、播放量等字段；view 失败不影响主流程。
    """
    bvid: str | None = None
    view: dict[str, Any] = {}
    view_error: str | None = None

    if client is not None:
        try:
            bvid = _resolve_bvid(client, target)
            view = client.view(bvid)
        except BiliApiError as exc:
            view_error = str(exc)
            # 明确的"视频不存在"要直接报错，不要退化成空元信息
            if exc.code == -404:
                raise SourceError(f"视频不存在或不可见：{target}", code=-404) from exc
            if bvid is None:
                bvid = extract_bvid(target)
        except SourceError:
            raise
    else:
        bvid = extract_bvid(target)

    if not bvid:
        raise SourceError(
            f"无法解析出 BV 号：{target}"
            + (f"（{view_error}）" if view_error else "")
        )

    # BDL parse：分 P 的权威来源
    tree: dict[str, Any] = {}
    if adapter is not None and adapter.available:
        try:
            tree = adapter.parse(bvid)
        except Exception:  # noqa: BLE001 - 允许在无 BDL 时降级
            tree = {}

    parts = _parts_from_bdl(tree) or (_parts_from_view(view) if view else [])
    if not parts and client is not None:
        try:
            pages = client.pagelist(bvid)
            for idx, page in enumerate(pages, start=1):
                if page.get("cid") is None:
                    continue
                parts.append(
                    VideoPart(
                        page=int(page.get("page") or idx),
                        cid=int(page["cid"]),
                        title=str(page.get("part") or f"P{idx}"),
                        duration_s=page.get("duration"),
                        bvid=bvid,
                    )
                )
        except BiliApiError:
            pass

    title = str(view.get("title") or "")
    if not title:
        source = tree.get("source") or {}
        title = str(source.get("title") or "")
    author = None
    if owner := view.get("owner"):
        author = str(owner.get("name") or "") or None
    if not author:
        for group in tree.get("groups") or []:
            for item in group.get("items") or []:
                if item.get("owner_name"):
                    author = str(item["owner_name"])
                    break
            if author:
                break

    duration_s = view.get("duration")
    if duration_s is None:
        for group in tree.get("groups") or []:
            for item in group.get("items") or []:
                if item.get("duration_seconds"):
                    duration_s = item["duration_seconds"]
                    break
            if duration_s is not None:
                break

    cover = view.get("pic")
    if not cover:
        for group in tree.get("groups") or []:
            for item in group.get("items") or []:
                if item.get("cover_url"):
                    cover = item["cover_url"]
                    break
            if cover:
                break
    if isinstance(cover, str) and cover.startswith("//"):
        cover = "https:" + cover

    return VideoInfo(
        bvid=bvid,
        title=title or bvid,
        author=author,
        aid=view.get("aid"),
        duration_s=duration_s,
        cover_url=cover,
        published_at=view.get("pubdate"),
        description=str(view.get("desc") or "") or None,
        parts=parts,
        source_url=f"https://www.bilibili.com/video/{bvid}",
    )
