"""B 站关键词搜索。

只返回真实接口字段；HTML 高亮标签、失效结果、重复 BV 会被过滤。
"""

from __future__ import annotations

import re
from typing import Any

from .bili_api import BiliApiError, BiliClient
from .schemas import SearchHit

# 搜索结果的 title 带 <em class="keyword"> 高亮
_TAG_RE = re.compile(r"<[^>]+>")

# 排序方式与接口 order 参数的对应
ORDERS = {
    "relevance": "totalrank",
    "totalrank": "totalrank",
    "newest": "pubdate",
    "pubdate": "pubdate",
    "most_viewed": "click",
    "click": "click",
    "most_danmaku": "dm",
    "dm": "dm",
    "most_stow": "stow",
    "stow": "stow",
}


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


def _parse_duration(value: Any) -> int | None:
    """``"12:34"`` / ``"1:02:03"`` / 数字 → 秒。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    seconds = 0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


def _normalize_cover(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    return url


def hit_from_raw(raw: dict[str, Any], *, rank_reason: str | None = None) -> SearchHit | None:
    """把一条搜索结果转成 SearchHit；字段缺失不编造。

    注意：B 站搜索结果里的 ``subtitle`` 字段其实是视频简介，不是"有无字幕"标记，
    因此 ``has_subtitle`` 一律留 False —— 只有实际查过 player 接口才置 True。
    """
    bvid = str(raw.get("bvid") or "").strip()
    if not bvid:
        return None
    title = _strip_tags(str(raw.get("title") or ""))
    if not title:
        return None
    description = _strip_tags(str(raw.get("description") or "")) or None
    play = raw.get("play")
    view_count = int(play) if isinstance(play, (int, float)) else None
    pubdate = raw.get("pubdate")
    published_at = int(pubdate) if isinstance(pubdate, (int, float)) else None
    aid = raw.get("aid")
    return SearchHit(
        bvid=bvid,
        title=title,
        author=str(raw.get("author") or "").strip() or None,
        url=f"https://www.bilibili.com/video/{bvid}",
        aid=int(aid) if isinstance(aid, (int, float)) else None,
        duration_s=_parse_duration(raw.get("duration")),
        published_at=published_at,
        view_count=view_count,
        description=description,
        cover_url=_normalize_cover(raw.get("pic")),
        source="bilibili_search",
        has_subtitle=False,
        rank_reason=rank_reason,
    )


class SearchProvider:
    """关键词 → 候选视频列表。"""

    def __init__(self, client: BiliClient) -> None:
        self.client = client

    def search(
        self,
        keyword: str,
        *,
        limit: int = 20,
        order: str = "totalrank",
        page: int = 1,
        max_requests: int = 5,
    ) -> list[SearchHit]:
        """返回去重后的候选列表。``order`` 接受语义名或接口原始值。

        ``limit`` 大于单页容量（20）时按需翻页：去重后仍不足才继续请求，
        总请求数受 ``max_requests`` 限制；接口报错（风控等）直接抛出并停止，
        不做重试循环。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            raise BiliApiError("搜索关键词为空", code=-400)
        api_order = ORDERS.get(order, order or "totalrank")
        limit = max(1, min(int(limit), 50))
        page_size = min(limit, 20)

        hits: list[SearchHit] = []
        seen: set[str] = set()
        current_page = max(1, int(page))
        requests = 0

        while len(hits) < limit and requests < max_requests:
            data = self.client.search_videos(
                keyword, page=current_page, order=api_order, page_size=page_size
            )
            requests += 1
            results = data.get("result") or []
            if not results:
                break
            for raw in results:
                hit = hit_from_raw(raw, rank_reason=f"order={api_order}")
                if hit is None or hit.bvid in seen:
                    continue
                seen.add(hit.bvid)
                hits.append(hit)
                if len(hits) >= limit:
                    break
            if len(results) < page_size:
                break  # 已经到最后一页
            current_page += 1

        return hits
