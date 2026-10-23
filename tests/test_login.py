"""扫码登录模块（离线，不联网）。"""

from __future__ import annotations

import json
from pathlib import Path

from bilibili_agent.bili_api import BiliApiError
from bilibili_agent.login import (
    QR_EXPIRED,
    QR_SCANNED,
    QR_WAITING,
    clear_cookie,
    poll_qr,
    read_cookie_file,
    run_login,
    save_cookie,
    verify_login,
)


class FakeClient:
    """按 URL 分发：generate 返回二维码，poll 按脚本返回轮询结果，nav 返回账号。"""

    def __init__(self, responses=None, generate=None, nav=None, set_cookies=None):
        self.responses = list(responses or [])
        self.generate = generate or {
            "data": {"qrcode_key": "k", "url": "https://example.invalid/qr"}
        }
        self.nav = nav
        self.set_cookies = list(set_cookies or [])
        self.calls = 0

    def get_json(self, url, params=None, headers=None, **kwargs):
        self.calls += 1
        if "qrcode/generate" in url:
            payload = self.generate
            if isinstance(payload, Exception):
                raise payload
            return payload
        if "/nav" in url:
            if self.nav is None:
                raise BiliApiError("账号未登录", code=-101)
            return self.nav
        payload = self.responses.pop(0) if self.responses else {"data": {"code": QR_WAITING}}
        if isinstance(payload, Exception):
            raise payload
        return payload

    def get_json_with_cookies(self, url, params=None, headers=None):
        payload = self.get_json(url, params=params, headers=headers)
        cookies = self.set_cookies.pop(0) if self.set_cookies else {}
        return payload, cookies


def _poll_payload(code, message="", **extra):
    data = {"code": code, "message": message}
    data.update(extra)
    return {"data": data}


def test_poll_waiting():
    client = FakeClient([_poll_payload(QR_WAITING, "未扫码")])
    result = poll_qr(client, _session())
    assert not result.ok
    assert result.state == "waiting"


def test_poll_scanned():
    client = FakeClient([_poll_payload(QR_SCANNED, "已扫码，等待确认")])
    result = poll_qr(client, _session())
    assert result.state == "scanned"


def test_poll_expired():
    client = FakeClient([_poll_payload(QR_EXPIRED, "二维码已过期")])
    result = poll_qr(client, _session())
    assert result.state == "expired"


def test_poll_confirmed_reads_cookies_from_set_cookie_header():
    """B 站扫码成功的 Cookie 在 Set-Cookie 响应头里，不在 JSON 里。"""
    client = FakeClient(
        responses=[_poll_payload(0, "", url="https://passport.bilibili.com/ok",
                                 refresh_token="rt")],
        set_cookies=[{
            "SESSDATA": "sess", "bili_jct": "jct", "DedeUserID": "123",
            "DedeUserID__ckMd5": "md5", "buvid3": "bv3", "sid": "sid",
        }],
    )
    result = poll_qr(client, _session())
    assert result.ok
    assert result.state == "confirmed"
    assert result.cookies["SESSDATA"] == "sess"
    assert result.cookies["bili_jct"] == "jct"
    assert result.refresh_token == "rt"
    header = result.cookie_header
    assert "SESSDATA=sess" in header
    assert "bili_jct=jct" in header
    # refresh_token 不应混进 Cookie header
    assert "refresh_token" not in header


def test_poll_confirmed_falls_back_to_json_fields():
    """个别情况下 JSON 里直接带字段时也能解析出来。"""
    client = FakeClient(
        responses=[_poll_payload(0, "", SESSDATA="sess", bili_jct="jct")],
    )
    result = poll_qr(client, _session())
    assert result.ok
    assert result.cookies["SESSDATA"] == "sess"


def test_poll_confirmed_without_any_cookie_is_error():
    client = FakeClient(responses=[_poll_payload(0, "")])
    result = poll_qr(client, _session())
    assert not result.ok
    assert result.state == "error"


def test_save_and_read_cookie_file(tmp_path):
    path = tmp_path / "cookie.txt"
    save_cookie(path, "SESSDATA=abc; bili_jct=def", refresh_token="rt")
    text = read_cookie_file(path)
    assert text is not None
    assert "SESSDATA=abc" in text
    assert "bili_jct=def" in text
    # refresh_token 以注释形式保存，不参与 Cookie
    assert "refresh_token=rt" not in text


def test_read_cookie_file_missing(tmp_path):
    assert read_cookie_file(tmp_path / "nope.txt") is None


def test_clear_cookie(tmp_path):
    path = tmp_path / "cookie.txt"
    save_cookie(path, "SESSDATA=abc")
    assert clear_cookie(path) is True
    assert not path.exists()
    assert clear_cookie(path) is False


def test_verify_login_handles_not_logged_in():
    class NotLogged(FakeClient):
        def get_json(self, url, params=None, headers=None):
            raise BiliApiError("账号未登录", code=-101)

    info = verify_login(NotLogged([]))
    assert info["is_login"] is False
    assert info["uname"] is None


def test_verify_login_reads_account():
    client = FakeClient(
        nav={"data": {"isLogin": True, "uname": "倪海马", "mid": 12345,
                      "level_info": {"current_level": 6}, "vip": {"status": 1}}}
    )
    info = verify_login(client)
    assert info["is_login"] is True
    assert info["uname"] == "倪海马"
    assert info["level"] == 6
    assert info["vip"] is True


def test_run_login_writes_file(tmp_path):
    client = FakeClient([
        _poll_payload(QR_WAITING, "未扫码"),
        _poll_payload(0, "", SESSDATA="sess", bili_jct="jct", DedeUserID="1"),
        {"data": {"isLogin": True, "uname": "me", "mid": 1,
                  "level_info": {}, "vip": {}}},
    ])
    path = tmp_path / "cookie.txt"
    events = []
    result = run_login(
        client, path, timeout=10, poll_interval=0.01, open_browser=False,
        on_event=lambda k, p: events.append(k),
    )
    assert result["ok"] is True
    assert Path(result["cookie_file"]).is_file()
    assert "SESSDATA=sess" in path.read_text(encoding="utf-8")
    assert "qr" in events and "saved" in events


def test_run_login_expired_raises(tmp_path):
    client = FakeClient([_poll_payload(QR_EXPIRED, "二维码已过期")])
    from bilibili_agent.login import LoginError

    try:
        run_login(client, tmp_path / "c.txt", timeout=5, poll_interval=0.01,
                  open_browser=False)
    except LoginError as exc:
        assert exc.state == "expired"
    else:
        raise AssertionError("应当抛出 LoginError")


def _session():
    from bilibili_agent.login import QrLoginSession

    return QrLoginSession(qrcode_key="k", url="https://example.invalid")


def test_json_import_is_used():
    # 保持 json 依赖显式，便于将来扩展状态文件格式
    assert json.dumps({"ok": True}) == '{"ok": true}'
