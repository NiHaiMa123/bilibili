"""B 站公开 API 客户端。

只使用公开可访问的接口；遇到 412/429 等风控立即抛结构化错误并停止，
不做代理轮换或循环重试绕过限制。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

API_BASE = "https://api.bilibili.com"
SUBTITLE_HOST_SUFFIX = "hdslb.com"

# 已知风控/权限错误码
CODE_RISK_CONTROL = -412
CODE_NOT_LOGIN = -101
CODE_NO_PERMISSION = -403
CODE_TOO_MANY_REQUESTS = -509


class BiliApiError(RuntimeError):
    """B 站接口返回了非零 code。"""

    def __init__(self, message: str, *, code: int | None = None, url: str = "",
                 payload: dict[str, Any] | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.url = url
        self.payload = payload or {}
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": "bilibili_api",
            "error_code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }


class BiliClient:
    """轻量 HTTP 客户端，带节流、UA、Cookie 与错误映射。

    实测要点（2026-10-23，本机）：
    - 本机存在 HTTP(S)_PROXY=127.0.0.1:7897，该出口 IP 被 B 站搜索接口风控（HTTP 412），
      因此默认 ``trust_env=False`` 直连；确实需要代理时再显式开启。
    - 搜索接口必须带完整浏览器请求头（Referer/Origin/Accept/Accept-Language），否则同样 412。
    - 先访问一次首页拿到 buvid3 / b_nut，再带 cookie 请求，稳定性明显更好。
    """

    def __init__(
        self,
        *,
        user_agent: str,
        cookie_header: str | None = None,
        interval: float = 0.6,
        timeout: float = 20.0,
        trust_env: bool = False,
        bootstrap_cookies: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.cookie_header = cookie_header
        self.interval = max(0.0, interval)
        self.timeout = timeout
        self.trust_env = trust_env
        self.bootstrap_cookies = bootstrap_cookies
        self._last_request = 0.0
        self._client: httpx.Client | None = None
        self._bootstrapped = False

    # -- 生命周期 -------------------------------------------------------
    def _get_client(self) -> httpx.Client:
        if self._client is None:
            headers = {
                "User-Agent": self.user_agent,
                "Referer": "https://www.bilibili.com/",
                "Origin": "https://www.bilibili.com",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            self._client = httpx.Client(
                headers=headers,
                timeout=self.timeout,
                follow_redirects=True,
                trust_env=self.trust_env,
            )
        return self._client

    def _bootstrap(self) -> None:
        """访问首页换取 buvid3 / b_nut，失败不致命。"""
        if self._bootstrapped or not self.bootstrap_cookies:
            return
        self._bootstrapped = True
        client = self._get_client()
        try:
            client.get("https://www.bilibili.com/")
        except httpx.HTTPError:
            pass

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> BiliClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 请求 -----------------------------------------------------------
    def _throttle(self) -> None:
        if self.interval <= 0:
            return
        wait = self.interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _send(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """发请求并处理 B 站的 code 约定，返回原始响应。"""
        self._bootstrap()
        self._throttle()
        client = self._get_client()
        req_headers: dict[str, str] = {}
        if self.cookie_header:
            req_headers["Cookie"] = self.cookie_header
        if headers:
            req_headers.update(headers)
        try:
            resp = client.request(
                method, url, params=params, headers=req_headers, data=data
            )
        except httpx.HTTPError as exc:
            raise BiliApiError(f"网络请求失败：{exc}", url=url, retryable=True) from exc

        if resp.status_code in (412, 429):
            raise BiliApiError(
                f"接口被限流/风控（HTTP {resp.status_code}），已停止请求：{url}",
                code=resp.status_code,
                url=url,
                retryable=False,
            )
        if resp.status_code >= 500:
            raise BiliApiError(
                f"服务端错误（HTTP {resp.status_code}）：{url}",
                code=resp.status_code,
                url=url,
                retryable=True,
            )
        if resp.status_code != 200:
            raise BiliApiError(
                f"HTTP {resp.status_code}：{url}", code=resp.status_code, url=url
            )

        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise BiliApiError(f"响应不是合法 JSON：{url}", url=url) from exc

        if not isinstance(payload, dict):
            raise BiliApiError(f"响应结构异常：{url}", url=url)

        code = payload.get("code")
        if code != 0:
            message = str(payload.get("message") or payload.get("msg") or "接口返回错误")
            retryable = code == CODE_TOO_MANY_REQUESTS
            raise BiliApiError(
                f"接口错误 code={code} message={message}",
                code=code if isinstance(code, int) else None,
                url=url,
                payload=payload,
                retryable=retryable,
            )
        return resp

    def get_json_with_cookies(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """返回 (响应体, 本次响应 ``Set-Cookie`` 带来的 cookie)。

        B 站扫码登录成功后，Cookie 是放在 ``Set-Cookie`` 响应头里的，
        不在 JSON 里——必须同时读响应头才能拿到登录态。
        """
        resp = self._send("GET", url, params=params, headers=headers)
        cookies = dict(resp.cookies)
        return resp.json(), cookies

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        method: str = "GET",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """请求并解析 B 站标准响应（``code``/``message``/``data``）。"""
        resp = self._send(method, url, params=params, headers=headers, data=data)
        return resp.json()

    def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        """GET 纯文本（字幕正文等），不做 code 检查。"""
        self._bootstrap()
        self._throttle()
        client = self._get_client()
        headers: dict[str, str] = {}
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        try:
            resp = client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise BiliApiError(f"网络请求失败：{exc}", url=url, retryable=True) from exc
        if resp.status_code != 200:
            raise BiliApiError(f"HTTP {resp.status_code}：{url}", code=resp.status_code, url=url)
        return resp.text

    # -- 具体接口 -------------------------------------------------------
    def search_videos(
        self,
        keyword: str,
        *,
        page: int = 1,
        order: str = "totalrank",
        page_size: int = 20,
    ) -> dict[str, Any]:
        data = self.get_json(
            f"{API_BASE}/x/web-interface/search/type",
            params={
                "search_type": "video",
                "keyword": keyword,
                "page": page,
                "order": order,
                "page_size": page_size,
            },
        )
        return data.get("data") or {}

    def view(self, bvid: str) -> dict[str, Any]:
        data = self.get_json(f"{API_BASE}/x/web-interface/view", params={"bvid": bvid})
        return data.get("data") or {}

    def pagelist(self, bvid: str) -> list[dict[str, Any]]:
        data = self.get_json(f"{API_BASE}/x/player/pagelist", params={"bvid": bvid})
        return data.get("data") or []

    def player_v2(self, bvid: str, cid: int) -> dict[str, Any]:
        data = self.get_json(
            f"{API_BASE}/x/player/v2", params={"bvid": bvid, "cid": cid}
        )
        return data.get("data") or {}


def is_trusted_subtitle_url(url: str) -> bool:
    """只允许从 B 站官方 CDN 域名取字幕正文。"""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return host.endswith(SUBTITLE_HOST_SUFFIX) or host.endswith("bilibili.com")


def read_cookie_file(path: str | Path) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    text = p.read_text(encoding="utf-8").strip()
    return text or None
