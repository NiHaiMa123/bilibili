"""安全边界：字幕 URL 校验、短链解析、BDL 缺失诊断。"""

from __future__ import annotations

import pytest

from bilibili_agent.bdl_adapter import BdlAdapter, BdlError
from bilibili_agent.bili_api import BiliApiError, is_trusted_subtitle_url
from bilibili_agent.inspect import (
    SourceError,
    _resolve_short_link,
    is_short_link,
)


# ---------------------------------------------------------------- T11 字幕 URL
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://aisubtitle.hdslb.com/bbs/x.json", True),
        ("https://hdslb.com/x.json", True),
        ("https://i0.hdslb.com/bbs/y.json", True),
        ("https://x.bilibili.com/z.json", True),
        # 后缀相似域名
        ("https://evilhdslb.com/x.json", False),
        ("https://hdslb.com.attacker.example/x.json", False),
        ("https://evilbilibili.com/x.json", False),
        ("https://notbilibili.com/x.json", False),
        # 非 https
        ("http://aisubtitle.hdslb.com/x.json", False),
        ("ftp://aisubtitle.hdslb.com/x.json", False),
        # 其它
        ("https://example.com/x.json", False),
        ("not a url", False),
    ],
)
def test_subtitle_url_validation(url, expected):
    assert is_trusted_subtitle_url(url) is expected


def test_get_text_does_not_send_account_cookie():
    """取字幕正文时不得把账号 Cookie 发给 CDN。"""
    from bilibili_agent.bili_api import BiliClient

    seen: dict[str, object] = {}

    class FakeResp:
        status_code = 200
        headers = {}
        url = "https://aisubtitle.hdslb.com/x.json"

        def __init__(self):
            self.text = "ok"

    class FakeClient:
        def get(self, url, params=None, headers=None, follow_redirects=True):
            seen["headers"] = dict(headers or {})
            seen["url"] = url
            return FakeResp()

    client = BiliClient(user_agent="UA", cookie_header="SESSDATA=secret")
    client._client = FakeClient()  # noqa: SLF001
    client._bootstrapped = True  # noqa: SLF001
    out = client.get_text("https://aisubtitle.hdslb.com/x.json")
    assert out == "ok"
    assert "Cookie" not in seen["headers"]


def test_get_text_rejects_unvalidated_redirect_target():
    """跳转到非官方域名必须被拒绝。"""
    from bilibili_agent.bili_api import BiliClient

    class Redirect:
        status_code = 302
        headers = {"location": "https://evil.example/x.json"}
        url = "https://aisubtitle.hdslb.com/x.json"

        @property
        def join(self, location):
            class _U:
                def __str__(self):
                    return location
            return _U()

    class FakeClient:
        def get(self, url, params=None, headers=None, follow_redirects=True):
            return Redirect()

    client = BiliClient(user_agent="UA")
    client._client = FakeClient()  # noqa: SLF001
    client._bootstrapped = True  # noqa: SLF001
    with pytest.raises(BiliApiError) as exc:
        client.get_text(
            "https://aisubtitle.hdslb.com/x.json",
            url_validator=is_trusted_subtitle_url,
        )
    assert "未经校验" in str(exc.value)


# ---------------------------------------------------------------- T07 短链
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://b23.tv/abc123", True),
        ("https://www.b23.tv/abc123", True),
        ("b23.tv/abc123", True),
        # 伪域名
        ("https://evil-b23.tv.attacker.example/x", False),
        ("https://notb23.tv/x", False),
        ("http://b23.tv/abc", False),
    ],
)
def test_is_short_link(url, expected):
    assert is_short_link(url) is expected


class _FakeResp:
    def __init__(self, url):
        self.url = url


class _FakeHttp:
    def __init__(self, chain):
        self.chain = list(chain)
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return _FakeResp(self.chain.pop(0))


class _FakeBiliClient:
    def __init__(self, chain):
        self.http = _FakeHttp(chain)

    def _get_client(self):
        return self.http


def test_short_link_resolves_to_bvid():
    client = _FakeBiliClient([
        "https://www.bilibili.com/video/BV1FW41157y3?p=2",
    ])
    assert _resolve_short_link(client, "https://b23.tv/abc") == "BV1FW41157y3"


def test_short_link_rejects_non_bilibili_target():
    client = _FakeBiliClient(["https://evil.example/video/BV1FW41157y3"])
    with pytest.raises(SourceError) as exc:
        _resolve_short_link(client, "https://b23.tv/abc")
    assert "不允许" in str(exc.value) or "未跳转" in str(exc.value)


def test_short_link_rejects_redirect_to_third_party():
    client = _FakeBiliClient([
        "https://evil.example/step1",
        "https://www.bilibili.com/video/BV1FW41157y3",
    ])
    with pytest.raises(SourceError):
        _resolve_short_link(client, "https://b23.tv/abc")


def test_short_link_does_not_call_empty_view():
    """短链解析不得再发那个无参数的 view 占位请求。"""
    class _BoomClient:
        def get_json(self, *args, **kwargs):
            raise AssertionError("短链解析不应调用 view 接口")

        def _get_client(self):
            return _FakeHttp(["https://www.bilibili.com/video/BV1FW41157y3"])

    assert _resolve_short_link(_BoomClient(), "https://b23.tv/abc") == "BV1FW41157y3"


# ---------------------------------------------------------------- T10 BDL 缺失
def test_missing_bdl_cli_raises_diagnosable_error():
    """未配置 BDL 时必须抛带 stage 的 BdlError，而不是 TypeError。"""
    adapter = BdlAdapter("")  # 空路径 → 不可用
    with pytest.raises(BdlError) as exc:
        adapter.parse("BV1FW41157y3")
    err = exc.value
    assert err.stage == "parse"
    assert "BDL CLI" in str(err)
    payload = err.to_dict()
    assert payload["stage"] == "parse"
    assert payload["error_code"] is None
