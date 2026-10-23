"""本地渲染登录二维码。

参考实现：BDL 桌面版（bpi-rs + Rust ``qrcode`` crate）**不打开网页**，
而是拿到 ``qrcode/generate`` 返回的 ``url`` 后在本地把二维码画出来显示。

本模块用纯 Python 的 ``segno`` 做同样的事：

* PNG 图片写到本地文件，用系统默认程序打开（打开的是一张图，
  不涉及任何 B 站页面，因此不受"浏览器已登录导致 H5 页跳过二维码"的影响）；
* 同时输出终端字符版，方便在没有图形界面的环境里扫码。
"""

from __future__ import annotations

import io
from pathlib import Path


class QrRenderError(RuntimeError):
    """二维码渲染失败。"""


def render_png(data: str, path: str | Path, *, scale: int = 8, border: int = 2) -> Path:
    """把内容渲染成 PNG 文件，返回路径。"""
    try:
        import segno
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise QrRenderError(
            "缺少 segno 依赖，请先安装：uv pip install -e ."
        ) from exc

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        qr = segno.make(data, error="m")
        qr.save(str(path), kind="png", scale=scale, border=border, dark="#000000",
                light="#ffffff")
    except Exception as exc:  # noqa: BLE001
        raise QrRenderError(f"二维码渲染失败：{exc}") from exc
    return path


def render_terminal(data: str, *, border: int = 2) -> str:
    """把内容渲染成终端可显示的字符二维码。"""
    try:
        import segno
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise QrRenderError(
            "缺少 segno 依赖，请先安装：uv pip install -e ."
        ) from exc

    buffer = io.StringIO()
    try:
        qr = segno.make(data, error="m")
        qr.terminal(out=buffer, border=border)
    except Exception as exc:  # noqa: BLE001
        raise QrRenderError(f"二维码渲染失败：{exc}") from exc
    return buffer.getvalue()
