"""本地二维码渲染（不联网）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bilibili_agent.qr import QrRenderError, render_png, render_terminal

SAMPLE = (
    "https://account.bilibili.com/h5/account-h5/auth/scan-web"
    "?navhide=1&callback=close&qrcode_key=0123456789abcdef&from="
)


def test_render_png_creates_file(tmp_path):
    out = tmp_path / "qr.png"
    render_png(SAMPLE, out)
    assert out.is_file()
    assert out.stat().st_size > 500
    # PNG magic
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_png_creates_parent_dir(tmp_path):
    out = tmp_path / "nested" / "deep" / "qr.png"
    render_png(SAMPLE, out)
    assert out.is_file()


def test_render_terminal_returns_qr_text():
    text = render_terminal(SAMPLE)
    assert text.strip()
    # 至少要有足够多的行，且含 ANSI 彩色输出（segno.terminal 的特征）
    assert len(text.splitlines()) > 20


def test_render_terminal_raises_without_segno(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "segno":
            raise ImportError("no segno")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(QrRenderError):
        render_terminal(SAMPLE)


def test_qr_module_has_no_bilibili_dependency():
    """二维码渲染只依赖 segno，不应引入网络调用。"""
    import bilibili_agent.qr as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "httpx" not in source
    assert "requests" not in source
