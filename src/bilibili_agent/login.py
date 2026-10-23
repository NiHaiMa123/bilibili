"""扫码登录：像 BDL 桌面版一样打开网页让用户登录，再把登录态落到本地 Cookie 文件。

为什么能自己做：
B 站网页端的扫码登录本身就是公开接口，
``/x/passport-login/web/qrcode/generate`` 申请二维码、
``/x/passport-login/web/qrcode/poll`` 轮询结果，
确认后接口直接返回 ``SESSDATA`` / ``bili_jct`` / ``DedeUserID`` 等 Cookie 字段。
所以不需要接管用户的浏览器、不需要读浏览器 Cookie 存储，
也不需要用户把密码或 Cookie 粘贴到任何地方——只用 B 站 App 扫一次码。

安全约定：
- Cookie 只写到本地文件（默认在项目 ``.cache/`` 下，已被 .gitignore 忽略）；
- 不写日志、不进 Markdown、不进 Git；
- 文件内容用 UTF-8 纯文本 Cookie header 格式，BDL CLI 的 ``--cookie-file`` 可直接复用。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bili_api import API_BASE, BiliApiError, BiliClient

PASSPORT_BASE = "https://passport.bilibili.com"
QR_GENERATE = f"{PASSPORT_BASE}/x/passport-login/web/qrcode/generate"
QR_POLL = f"{PASSPORT_BASE}/x/passport-login/web/qrcode/poll"
QR_REFERER = "https://passport.bilibili.com/login"

# 轮询返回码
QR_WAITING = 86101  # 未扫码
QR_SCANNED = 86090  # 已扫码，待确认
QR_EXPIRED = 86038  # 二维码已过期
QR_TOO_MANY = 86032  # 频率过快

# 登录成功后接口会返回这些 Cookie 字段，按此顺序拼成 Cookie header
COOKIE_FIELDS = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
    "buvid3",
    "buvid4",
    "_uuid",
)


@dataclass(slots=True)
class QrLoginSession:
    qrcode_key: str
    url: str
    created_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class LoginResult:
    ok: bool
    state: str  # confirmed | expired | waiting | scanned | error
    message: str = ""
    code: int | None = None
    cookies: dict[str, str] = field(default_factory=dict)
    refresh_token: str = ""

    @property
    def cookie_header(self) -> str:
        parts = [f"{k}={self.cookies[k]}" for k in COOKIE_FIELDS if self.cookies.get(k)]
        # 保留接口返回的其他字段，避免漏掉必要的登录态
        for k, v in self.cookies.items():
            if k not in COOKIE_FIELDS and k not in ("refresh_token",):
                parts.append(f"{k}={v}")
        return "; ".join(parts)


class LoginError(RuntimeError):
    """登录流程失败。"""

    def __init__(self, message: str, *, state: str = "error"):
        super().__init__(message)
        self.state = state

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": "login",
            "error_code": None,
            "message": str(self),
            "retryable": self.state == "expired",
        }


def generate_qr(client: BiliClient) -> QrLoginSession:
    """申请一个登录二维码。"""
    data = client.get_json(
        QR_GENERATE, headers={"Referer": QR_REFERER}
    ).get("data") or {}
    key = str(data.get("qrcode_key") or "")
    url = str(data.get("url") or "")
    if not key or not url:
        raise LoginError("接口没有返回二维码信息")
    return QrLoginSession(qrcode_key=key, url=url)


def poll_qr(client: BiliClient, session: QrLoginSession) -> LoginResult:
    """轮询一次扫码状态。

    成功时 Cookie 在响应的 ``Set-Cookie`` 头里（不在 JSON 里），
    参考 BDL 使用的 bpi-rs：``login.qr_poll`` 同样是从响应头取 cookies。
    """
    payload, set_cookies = client.get_json_with_cookies(
        QR_POLL,
        params={"qrcode_key": session.qrcode_key},
        headers={"Referer": QR_REFERER},
    )
    data = payload.get("data") or {}
    code = data.get("code")
    message = str(data.get("message") or "")

    if code == 0:
        cookies: dict[str, str] = {}
        # 1) Set-Cookie 响应头（真正的登录态来源）
        cookies.update(set_cookies)
        # 2) 兼容个别情况下 JSON 里也直接带字段的情况
        for k, v in data.items():
            if k in ("code", "message", "url", "timestamp", "refresh_token"):
                continue
            if isinstance(v, str) and v:
                cookies.setdefault(k, v)
        if not cookies.get("SESSDATA"):
            return LoginResult(
                ok=False, state="error", code=code,
                message=f"未取到登录 Cookie（Set-Cookie 有 {len(set_cookies)} 项）",
            )
        return LoginResult(
            ok=True,
            state="confirmed",
            message="登录成功",
            code=0,
            cookies=cookies,
            refresh_token=str(data.get("refresh_token") or ""),
        )
    if code == QR_WAITING:
        return LoginResult(ok=False, state="waiting", message=message or "未扫码", code=code)
    if code == QR_SCANNED:
        return LoginResult(ok=False, state="scanned",
                           message="已扫码，请在 B 站 App 上点确认", code=code)
    if code == QR_EXPIRED:
        return LoginResult(ok=False, state="expired", message=message or "二维码已过期", code=code)
    if code == QR_TOO_MANY:
        return LoginResult(ok=False, state="waiting",
                           message="操作过于频繁，请稍后重试", code=code)
    return LoginResult(ok=False, state="error", code=code,
                       message=message or f"未知状态 code={code}")


QR_CONFIRM = f"{PASSPORT_BASE}/x/passport-login/web/qrcode/confirm"


def render_login_qr(
    url: str, out_dir: str | Path
) -> tuple[Path, str]:
    """本地渲染登录二维码，返回 (PNG 路径, 终端字符版)。

    与 BDL 桌面版一致：不打开 B 站网页，避免"浏览器已登录 → H5 页跳过二维码"
    的分支问题。打开的是一张本地图片，任何程序打开都不会带上登录态。
    """
    from .qr import QrRenderError, render_png, render_terminal

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / "bilibili-login-qr.png"
    try:
        render_png(url, png)
        ascii_qr = render_terminal(url)
    except QrRenderError:
        raise
    return png, ascii_qr


def open_image(path: str | Path) -> bool:
    """用系统默认程序打开图片（看图软件 / 浏览器均可）。"""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606 - 交给系统默认程序
            return True
    except OSError:
        pass
    try:
        return webbrowser.open(str(Path(path).resolve().as_uri()))
    except Exception:  # noqa: BLE001
        return False


def confirm_qr(client: BiliClient, session: QrLoginSession) -> dict[str, Any]:
    """在网页上替用户点"确认"。

    H5 扫码页扫完之后会显示账号并要求点"确认"，那一步对应这个接口。
    用户点完，轮询才会返回 cookie。
    """
    return client.get_json(
        QR_CONFIRM,
        method="POST",
        data={"qrcode_key": session.qrcode_key},
        headers={"Referer": QR_REFERER},
    )


# 常见浏览器及其"无痕/隐私窗口"参数。
# 必须用干净会话打开：若浏览器已登录 B 站，H5 扫码页会跳过二维码直接显示
# "以某账号登录"的确认卡（按钮还是禁用态），二维码根本不会出现，扫码也无从谈起。
BROWSER_CANDIDATES = (
    (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "--inprivate"),
    (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "--inprivate"),
    (r"C:\Users\Administrator\AppData\Local\Google\Chrome\Application\chrome.exe",
     "--incognito"),
    (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "--incognito"),
    (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "--incognito"),
)


def find_private_browser(explicit: str = "") -> tuple[str, str] | None:
    """找到一个可用的浏览器与它的隐私窗口参数。"""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            flag = "--incognito" if "chrome" in p.name.lower() else "--inprivate"
            return str(p), flag
    for exe, flag in BROWSER_CANDIDATES:
        if Path(exe).is_file():
            return exe, flag
    return None


def open_login_page(
    url: str,
    private: bool = True,
    browser: str = "",
    profile_dir: str | Path | None = None,
) -> bool:
    """用浏览器打开扫码页。

    ``private=True`` 时使用**独立的临时 user-data-dir** 启动一个干净实例。
    不能只靠 ``--inprivate``：当系统里已经开着同款浏览器时，新 URL 会被转交给
    那个已有实例，隐私参数会被忽略，页面里就还带着原来的 B 站登录态，
    二维码不会显示（只会出现一个禁用态的【确认】按钮）。
    """
    if private:
        found = find_private_browser(browser)
        if found:
            exe, flag = found
            args = [exe, flag]
            if profile_dir:
                profile = Path(profile_dir)
                profile.mkdir(parents=True, exist_ok=True)
                args += [
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ]
            args.append(url)
            try:
                subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except OSError:
                pass
    try:
        if sys.platform == "win32":
            os.startfile(url)  # noqa: S606 - 交给系统默认浏览器处理
            return True
    except OSError:
        pass
    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001
        return False


def save_cookie(path: str | Path, cookie_header: str, refresh_token: str = "") -> Path:
    """写入本地 Cookie 文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = cookie_header.strip()
    if refresh_token:
        payload += f"\n# refresh_token={refresh_token}"
    path.write_text(payload + "\n", encoding="utf-8")
    return path


def read_cookie_file(path: str | Path) -> str | None:
    """读 Cookie 文件，去掉注释行。"""
    p = Path(path)
    if not p.is_file():
        return None
    lines = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    text = "; ".join(lines).strip()
    return text or None


def clear_cookie(path: str | Path) -> bool:
    """删除本地 Cookie 文件，返回是否真的删了。"""
    p = Path(path)
    if p.is_file():
        p.unlink()
        return True
    return False


def verify_login(client: BiliClient) -> dict[str, Any]:
    """用 ``nav`` 接口验证登录态，返回真实的账号信息。

    未登录时接口返回 code=-101，这里如实报告"未登录"，不算错误。
    """
    try:
        data = client.get_json(f"{API_BASE}/x/web-interface/nav").get("data") or {}
    except BiliApiError as exc:
        if exc.code == -101:
            return {"is_login": False, "uname": None, "mid": None,
                    "level": None, "vip": False}
        raise
    return {
        "is_login": bool(data.get("isLogin")),
        "uname": data.get("uname") or None,
        "mid": data.get("mid") or None,
        "level": ((data.get("level_info") or {}).get("current_level")),
        "vip": bool((data.get("vip") or {}).get("status") == 1),
    }


def run_login(
    client: BiliClient,
    cookie_path: str | Path,
    *,
    timeout: int = 180,
    poll_interval: float = 3.0,
    open_browser: bool = True,
    private_window: bool = True,
    browser: str = "",
    profile_dir: str | Path | None = None,
    qr_dir: str | Path | None = None,
    on_event=None,
) -> dict[str, Any]:
    """完整扫码登录流程：申请二维码 → 本地渲染二维码 → 轮询 → 写 Cookie 文件。

    ``on_event`` 可选回调，用于把进度推给 CLI（只输出状态，不含凭据）。
    """

    def emit(kind: str, **payload: Any) -> None:
        if on_event:
            on_event(kind, payload)

    session = generate_qr(client)

    # 本地渲染二维码（不打开 B 站网页）
    png_path = None
    ascii_qr = ""
    if qr_dir:
        try:
            png_path, ascii_qr = render_login_qr(session.url, qr_dir)
            emit("qr", url=session.url, png=str(png_path), ascii=ascii_qr)
            if open_browser:
                open_image(png_path)
                emit("opened", opened=True, private=False)
        except Exception as exc:  # noqa: BLE001
            emit("qr", url=session.url, png=None, ascii="")
            emit("opened", opened=False, private=False)
            emit("state", state="error", message=f"二维码渲染失败：{exc}")
    else:
        emit("qr", url=session.url, png=None, ascii="")
        opened = (
            open_login_page(
                session.url,
                private=private_window,
                browser=browser,
                profile_dir=profile_dir,
            )
            if open_browser
            else False
        )
        emit("opened", opened=opened, private=private_window and open_browser)

    deadline = time.monotonic() + timeout
    last_state = ""
    while time.monotonic() < deadline:
        try:
            result = poll_qr(client, session)
        except BiliApiError as exc:
            # 风控/网络问题：停下来报错，不做绕过
            raise LoginError(f"轮询登录状态失败：{exc}") from exc

        if result.state != last_state:
            emit("state", state=result.state, message=result.message,
                 code=result.code)
            last_state = result.state

        # 注意：不要在这里替用户调 qrcode/confirm。
        # 86090「已扫码未确认」表示"请在 B 站 App 上点确认"，
        # 用户在 App 确认后，下一次轮询就会返回 code=0 与 Set-Cookie 里的登录态。
        # 多余的 confirm 请求反而可能把 key 提前消费掉。

        if result.ok:
            path = save_cookie(cookie_path, result.cookie_header, result.refresh_token)
            emit("saved", path=str(path))
            info = verify_login(client)
            return {
                "ok": True,
                "status": "completed",
                "state": "confirmed",
                "cookie_file": str(path),
                "account": info,
            }
        if result.state == "expired":
            raise LoginError("二维码已过期，请重新执行登录", state="expired")
        if result.state == "error":
            raise LoginError(f"登录失败：{result.message}", state="error")

        time.sleep(poll_interval)

    raise LoginError(f"等待超时（超过 {timeout} 秒），二维码可能已失效", state="timeout")
