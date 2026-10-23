"""``bili`` 命令行入口。

约定：
- stdout 只输出数据（``--json`` 时是 JSON，否则是人类可读文本）
- stderr 输出日志与错误
- 退出码：0 成功 / 1 运行失败 / 2 参数错误
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from . import PIPELINE_VERSION, __version__
from .bdl_adapter import BdlError
from .bili_api import BiliApiError
from .config import load_config
from .inspect import SourceError
from .pipeline import MODES, SUBTITLE_SOURCES, Pipeline, probe_media

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

JOBS_DIRNAME = "jobs"


# ---------------------------------------------------------------- 输出helper
def _emit_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _log(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")


def _fail(payload: dict[str, Any], as_json: bool) -> int:
    if as_json:
        _emit_json(payload)
    else:
        err = payload.get("error") or {}
        _log(f"[{payload.get('stage', 'error')}] {err.get('message', payload)}")
    return EXIT_FAIL


# ---------------------------------------------------------------- doctor
def _check(name: str, ok: bool, detail: str, *, required_for: list[str] | None = None,
           hint: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "ok": ok,
        "detail": detail,
        "required_for": required_for or [],
        "hint": hint,
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    checks: list[dict[str, Any]] = []

    # 配置
    checks.append(_check(
        "config", cfg.config_path is not None,
        str(cfg.config_path) if cfg.config_path else "未找到 config.toml（使用内置默认值）",
        hint="复制 config.example.toml 为 config.toml 后按需配置",
    ))

    # BDL CLI
    bdl_path = cfg.bdl_cli or shutil.which("bdl") or ""
    bdl_detail = bdl_path or "未找到 bdl 可执行文件"
    bdl_ok = False
    if bdl_path and Path(bdl_path).is_file():
        from .bdl_adapter import BdlAdapter

        try:
            bdl_detail = BdlAdapter(bdl_path).version()
            bdl_ok = True
        except BdlError as exc:
            bdl_detail = f"执行失败：{exc}"
    checks.append(_check(
        "bdl_cli", bdl_ok, bdl_detail,
        required_for=["download", "illustrated", "assets"],
        hint="从 https://github.com/Yuelioi/bdl/releases 下载 bdl-cli-<ver>-windows-x64.zip 并解压",
    ))

    # FFmpeg / FFprobe
    ffmpeg = cfg.ffmpeg or shutil.which("ffmpeg") or ""
    ffprobe = cfg.ffprobe or shutil.which("ffprobe") or ""
    ffmpeg_detail = ffmpeg or "未找到 ffmpeg"
    if ffmpeg:
        try:
            proc = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30)
            ffmpeg_detail = (proc.stdout or "").splitlines()[0] if proc.stdout else ffmpeg
        except (subprocess.SubprocessError, OSError):
            ffmpeg_detail = f"{ffmpeg}（无法执行）"
    checks.append(_check(
        "ffmpeg", bool(ffmpeg), ffmpeg_detail,
        required_for=["download", "illustrated", "assets"],
        hint="winget install Gyan.FFmpeg",
    ))
    checks.append(_check(
        "ffprobe", bool(ffprobe), ffprobe or "未找到 ffprobe",
        required_for=["media_verify"], hint="winget install Gyan.FFmpeg",
    ))

    # OCR 引擎
    ocr_ok = False
    ocr_detail = "未检测到 RapidOCR / PaddleOCR"
    try:  # pragma: no cover - 取决于本机环境
        import importlib.util

        if importlib.util.find_spec("rapidocr"):
            ocr_ok = True
            ocr_detail = "已安装 RapidOCR 3.x（ONNX Runtime + PP-OCRv6）"
        elif importlib.util.find_spec("paddleocr"):
            ocr_ok = True
            ocr_detail = "已安装 PaddleOCR"
    except Exception:  # noqa: BLE001
        pass
    checks.append(_check(
        "ocr", ocr_ok, ocr_detail, required_for=["ocr"],
        hint="uv pip install -e '.[ocr]'",
    ))

    # ASR
    asr_python = cfg.asr_python or ""
    asr_ok = False
    asr_detail = "未配置 asr.python"
    if asr_python and Path(asr_python).is_file():
        try:
            # 用 find_spec 而不是 import：不会真的加载 torch（首次 import 可能要 1-2 分钟）
            probe = (
                "import importlib.util, sys; "
                "a = importlib.util.find_spec('qwen_asr'); "
                "t = importlib.util.find_spec('torch'); "
                "print('ok' if (a and t) else 'missing')"
            )
            proc = subprocess.run([asr_python, "-c", probe], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=90)
            verdict = (proc.stdout or "").strip()
            if proc.returncode == 0 and verdict == "ok":
                asr_ok = True
                asr_detail = "qwen_asr + torch 就绪"
            elif verdict == "missing":
                asr_detail = "解释器缺少 qwen_asr 或 torch"
            else:
                asr_detail = f"解释器探测失败：{verdict or (proc.stderr or '').strip()[:120]}"
        except subprocess.TimeoutExpired:
            asr_detail = "解释器探测超时"
        except (subprocess.SubprocessError, OSError) as exc:
            asr_detail = f"解释器无法执行：{exc}"
    checks.append(_check(
        "qwen3_asr", asr_ok, asr_detail, required_for=["asr"],
        hint="在 config.toml 的 [asr] 中配置 python 与 model_path",
    ))
    checks.append(_check(
        "asr_model", bool(cfg.asr_model_path and Path(cfg.asr_model_path).is_dir()),
        cfg.asr_model_path or "未配置 asr.model_path", required_for=["asr"],
    ))
    checks.append(_check(
        "asr_aligner",
        bool(cfg.asr_aligner_path and Path(cfg.asr_aligner_path).is_dir()),
        cfg.asr_aligner_path if cfg.asr_aligner_path else "未配置 asr.aligner_path（将只有块级时间戳）",
        required_for=["asr_word_timestamps"],
        hint="Qwen/Qwen3-ForcedAligner-0.6B；没有它时 ASR 只给块级时间戳",
    ))

    # GPU
    gpu_detail = "未检测到 NVIDIA GPU"
    gpu_ok = False
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            proc = subprocess.run(
                [smi, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                gpu_ok = True
                gpu_detail = proc.stdout.strip().splitlines()[0]
        except (subprocess.SubprocessError, OSError):
            pass
    checks.append(_check(
        "gpu", gpu_ok, gpu_detail, required_for=["asr_gpu"],
        hint="ASR 在 CPU 上仍可运行，只是更慢",
    ))

    # Cookie
    cookie = cfg.cookie_header()
    checks.append(_check(
        "cookie", bool(cookie),
        "已配置 Cookie" if cookie else "未配置 Cookie（公开内容可用；受限内容不可用）",
        required_for=["restricted"],
    ))

    # 字幕路线判定：平台字幕要登录态，否则只能靠 ASR 兜底
    platform_ok = bool(cookie)
    asr_route_ok = asr_ok and bool(cfg.asr_model_path and Path(cfg.asr_model_path).is_dir())
    if platform_ok:
        subtitle_detail = "平台字幕路线可用（已配置 Cookie）"
    elif asr_route_ok:
        subtitle_detail = "平台字幕需要登录态；当前可走本地 ASR 兜底路线"
    else:
        subtitle_detail = "既没有 Cookie，也没有可用的 ASR 环境；无字幕视频将无法产出文本"
    checks.append(_check(
        "subtitle_route", platform_ok or asr_route_ok, subtitle_detail,
        required_for=["subtitle"],
        hint="配置 [bilibili].cookie_file 后可使用 B 站官方/AI 字幕",
    ))

    payload = {
        "ok": True,
        "status": "ok",
        "version": __version__,
        "pipeline_version": PIPELINE_VERSION,
        "config_path": str(cfg.config_path) if cfg.config_path else None,
        "output_dir": str(cfg.output_path),
        "media_cache": str(cfg.media_cache_dir),
        "checks": checks,
    }
    if args.json:
        _emit_json(payload)
    else:
        print(f"bilibili-agent {__version__}（pipeline {PIPELINE_VERSION}）")
        for c in checks:
            mark = "OK  " if c["ok"] else "MISS"
            print(f"  [{mark}] {c['name']:<14} {c['detail']}")
            if not c["ok"] and c["hint"]:
                print(f"           → {c['hint']}")
    return EXIT_OK


# ---------------------------------------------------------------- search
def cmd_search(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    pipe = Pipeline(cfg)
    try:
        hits = pipe.search(args.query, limit=args.limit, order=args.order)
    except BiliApiError as exc:
        return _fail(exc.to_dict(), args.json)
    finally:
        pipe.close()

    payload = {
        "ok": True,
        "status": "completed",
        "query": args.query,
        "count": len(hits),
        "results": [h.to_dict() for h in hits],
    }
    if args.json:
        _emit_json(payload)
    else:
        for i, h in enumerate(hits, start=1):
            dur = f"{h.duration_s}s" if h.duration_s else "?"
            sub = " [有字幕]" if h.has_subtitle else ""
            print(f"{i:>2}. {h.bvid}  {dur:>7}  {h.author or '?'}  {h.title}{sub}")
            print(f"    {h.url}")
    return EXIT_OK


# ---------------------------------------------------------------- inspect
def cmd_inspect(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    pipe = Pipeline(cfg)
    try:
        info = pipe.inspect(args.target)
    except (SourceError, BdlError, BiliApiError) as exc:
        payload = exc.to_dict() if hasattr(exc, "to_dict") else {
            "stage": "inspect", "error_code": None, "message": str(exc), "retryable": False,
        }
        return _fail(payload, args.json)
    finally:
        pipe.close()

    payload = {
        "ok": True, "status": "completed",
        "bvid": info.bvid, "title": info.title, "author": info.author,
        "duration_s": info.duration_s, "url": info.source_url,
        "parts": [p.to_dict() for p in info.parts],
    }
    if args.json:
        _emit_json(payload)
    else:
        print(f"{info.bvid}  {info.title}")
        print(f"UP主：{info.author or '未知'}  时长：{info.duration_s or '?'}s")
        for p in info.parts:
            print(f"  P{p.page}  cid={p.cid}  {p.duration_s or '?'}s  {p.title}")
    return EXIT_OK


# ---------------------------------------------------------------- fetch
def _jobs_dir(cfg) -> Path:
    d = cfg.temp_path / JOBS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def cmd_fetch(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    job_id = uuid.uuid4().hex[:12]
    jobs_dir = _jobs_dir(cfg)
    job_path = jobs_dir / f"{job_id}.json"

    if args.async_:
        cmd = [
            sys.executable, "-m", "bilibili_agent.cli", "_fetch_worker",
            "--config", str(cfg.config_path) if cfg.config_path else "",
            "--target", args.target,
            "--parts", ",".join(str(p) for p in (args.parts or [])),
            "--mode", args.mode,
            "--subtitle-source", args.subtitle_source,
            "--job-path", str(job_path),
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        payload = {
            "ok": True, "status": "queued", "job_id": job_id,
            "pid": proc.pid, "bvid": None, "parts": [], "outputs": [], "warnings": [],
        }
        if args.json:
            _emit_json(payload)
        else:
            print(f"任务已启动：{job_id}（pid={proc.pid}）")
            print(f"用 `bili status {job_id}` 查询进度")
        return EXIT_OK

    pipe = Pipeline(cfg)
    try:
        result = pipe.fetch(
            args.target, parts=args.parts, mode=args.mode,
            subtitle_source=args.subtitle_source, refresh=args.refresh,
        )
    finally:
        pipe.close()

    payload = result.to_dict()
    payload["job_id"] = job_id
    job_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        _emit_json(payload)
    else:
        if payload["ok"]:
            print(f"完成：{payload.get('title')}（{payload.get('bvid')}）")
        for p in payload.get("parts") or []:
            state = "缓存命中" if p.get("cached") else "新生成"
            print(f"  P{p.get('page')} [{p.get('subtitle_source')}] {state}")
            for o in p.get("outputs") or []:
                print(f"    {o}")
        for w in payload.get("warnings") or []:
            _log(f"  ! {w}")
        if not payload["ok"]:
            err = payload.get("error") or {}
            _log(f"失败：[{err.get('stage')}] {err.get('message')}")
    return EXIT_OK if payload["ok"] else EXIT_FAIL


def cmd_fetch_worker(args: argparse.Namespace) -> int:
    """后台 worker：执行 fetch 并把结果写入 job 文件。"""
    cfg = load_config(args.config)
    job_path = Path(args.job_path)
    job_path.parent.mkdir(parents=True, exist_ok=True)
    job_path.write_text(
        json.dumps({"ok": True, "status": "running", "stage": "fetch"},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pipe = Pipeline(cfg)
    try:
        result = pipe.fetch(
            args.target,
            parts=[int(p) for p in args.parts.split(",") if p.strip()] if args.parts else None,
            mode=args.mode, subtitle_source=args.subtitle_source,
        )
    except Exception as exc:  # noqa: BLE001
        payload = {"ok": False, "status": "failed",
                   "error": {"stage": "worker", "error_code": None,
                             "message": str(exc), "retryable": False}}
        job_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return EXIT_FAIL
    finally:
        pipe.close()
    payload = result.to_dict()
    job_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return EXIT_OK if payload["ok"] else EXIT_FAIL


# ---------------------------------------------------------------- status
def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    jobs_dir = _jobs_dir(cfg)
    path = jobs_dir / f"{args.job_id}.json"
    if not path.is_file():
        return _fail(
            {"stage": "status", "error_code": -404,
             "message": f"找不到任务 {args.job_id}", "retryable": False},
            args.json,
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if args.json:
        _emit_json(payload)
    else:
        print(f"任务 {args.job_id}: {payload.get('status')}")
        for o in payload.get("outputs") or []:
            print(f"  {o}")
    return EXIT_OK


# ---------------------------------------------------------------- login
def cmd_login(args: argparse.Namespace) -> int:
    """扫码登录：像 BDL 桌面版一样打开网页，扫码后把登录态写到本地 Cookie 文件。"""
    cfg = load_config(args.config)
    from .login import LoginError, run_login

    cookie_path = args.cookie_file or cfg.cookie_file
    if not cookie_path:
        return _fail(
            {"stage": "login", "error_code": -400,
             "message": "没有可用的 Cookie 文件路径", "retryable": False},
            args.json,
        )

    pipe = Pipeline(cfg)

    def emit(kind: str, payload: dict) -> None:
        if kind == "qr":
            ascii_qr = payload.get("ascii") or ""
            if ascii_qr:
                _log("")
                _log(ascii_qr)
                _log("")
            png = payload.get("png")
            if png:
                _log(f"二维码图片：{png}")
            _log(f"扫码链接：{payload.get('url')}")
        elif kind == "opened":
            if payload.get("opened"):
                _log("二维码图片已打开，请用 B 站 App 扫码并在手机上确认。")
            else:
                _log("无法自动打开图片，请手动打开上面的 PNG 文件。")
        elif kind == "state":
            code = payload.get("code")
            _log(f"状态：{payload.get('message') or payload.get('state')}"
                 + (f"（code={code}）" if code else ""))
        elif kind == "confirmed":
            _log("已在服务端完成确认登录。")
        elif kind == "saved":
            _log(f"登录态已保存到：{payload['path']}")

    try:
        result = run_login(
            pipe.client, cookie_path,
            timeout=args.timeout,
            open_browser=not args.no_browser,
            private_window=not args.normal_window,
            browser=args.browser or "",
            profile_dir=(None if args.normal_window
                         else str(pipe.cfg.temp_path / "login-browser-profile")),
            qr_dir=str(pipe.cfg.temp_path),
            on_event=emit,
        )
    except LoginError as exc:
        return _fail(exc.to_dict(), args.json)
    finally:
        pipe.close()

    if args.json:
        _emit_json(result)
    else:
        account = result.get("account") or {}
        print(f"登录成功：{account.get('uname') or '未知账号'}"
              f"（UID {account.get('mid')}）")
        print(f"Cookie 文件：{result.get('cookie_file')}")
    return EXIT_OK


def cmd_logout(args: argparse.Namespace) -> int:
    """清除本地登录态。"""
    cfg = load_config(args.config)
    from .login import clear_cookie

    path = args.cookie_file or cfg.cookie_file
    removed = clear_cookie(path) if path else False
    payload = {"ok": True, "status": "completed", "cookie_file": path, "removed": removed}
    if args.json:
        _emit_json(payload)
    else:
        print("已清除本地登录态" if removed else "本地没有登录态文件")
    return EXIT_OK


def cmd_whoami(args: argparse.Namespace) -> int:
    """查看当前登录态与账号信息。"""
    cfg = load_config(args.config)
    from .login import verify_login

    pipe = Pipeline(cfg)
    try:
        info = verify_login(pipe.client)
    except BiliApiError as exc:
        return _fail(exc.to_dict(), args.json)
    finally:
        pipe.close()

    payload = {
        "ok": True, "status": "completed",
        "cookie_file": cfg.cookie_file or None,
        "has_cookie_file": bool(cfg.cookie_header()),
        **info,
    }
    if args.json:
        _emit_json(payload)
    else:
        if info["is_login"]:
            print(f"已登录：{info['uname']}（UID {info['mid']}，等级 {info['level']}）")
        else:
            print("未登录（或 Cookie 已失效），执行 `bili login` 重新扫码")
    return EXIT_OK


# ---------------------------------------------------------------- media
def cmd_media(args: argparse.Namespace) -> int:
    """对已下载的文件跑 ffprobe 校验（P0 验收用）。"""
    cfg = load_config(args.config)
    results = []
    for path in args.paths:
        p = Path(path)
        info = probe_media(p, cfg.ffprobe)
        results.append({
            "path": str(p),
            "exists": p.is_file(),
            "size_bytes": p.stat().st_size if p.is_file() else 0,
            "probe": info,
            "ok": bool(info),
        })
    payload = {"ok": all(r["ok"] for r in results), "status": "completed", "results": results}
    if args.json:
        _emit_json(payload)
    else:
        for r in results:
            print(f"{r['path']}  exists={r['exists']}  size={r['size_bytes']}")
            if r["probe"]:
                for s in r["probe"]["streams"]:
                    print(f"    {s['codec_type']}: {s['codec_name']} "
                          f"{s.get('width') or ''}{'x' if s.get('width') else ''}{s.get('height') or ''}")
    return EXIT_OK if payload["ok"] else EXIT_FAIL


# ---------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bili",
        description="Bilibili 视频检索与图文知识化 Agent 工具",
    )
    parser.add_argument("--config", default=None, help="config.toml 路径")
    parser.add_argument("--version", action="version", version=f"bilibili-agent {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="检查依赖与配置")
    p_doctor.add_argument("--json", action="store_true", help="输出 JSON")
    p_doctor.set_defaults(func=cmd_doctor)

    p_search = sub.add_parser("search", help="按关键词搜索视频（不下载）")
    p_search.add_argument("query", help="搜索关键词")
    p_search.add_argument("--limit", type=int, default=20, help="返回条数（1-50）")
    p_search.add_argument(
        "--order", default="totalrank",
        help="排序：relevance|newest|most_viewed|most_danmaku|most_stow",
    )
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_inspect = sub.add_parser("inspect", help="查看视频元信息与分 P")
    p_inspect.add_argument("target", help="BV 号或视频 URL")
    p_inspect.add_argument("--json", action="store_true")
    p_inspect.set_defaults(func=cmd_inspect)

    p_fetch = sub.add_parser("fetch", help="生成字幕/图文 Markdown")
    p_fetch.add_argument("target", help="BV 号或视频 URL")
    p_fetch.add_argument("--parts", type=int, nargs="*", default=None, help="要处理的分 P")
    p_fetch.add_argument("--mode", default="subtitle", choices=list(MODES))
    p_fetch.add_argument("--subtitle-source", default="auto", choices=list(SUBTITLE_SOURCES))
    p_fetch.add_argument("--refresh", action="store_true", help="忽略缓存重新生成")
    p_fetch.add_argument("--async", dest="async_", action="store_true", help="后台执行")
    p_fetch.add_argument("--json", action="store_true")
    p_fetch.set_defaults(func=cmd_fetch)

    p_worker = sub.add_parser("_fetch_worker", help=argparse.SUPPRESS)
    p_worker.add_argument("--config", default=None)
    p_worker.add_argument("--target", required=True)
    p_worker.add_argument("--parts", default="")
    p_worker.add_argument("--mode", default="subtitle")
    p_worker.add_argument("--subtitle-source", default="auto")
    p_worker.add_argument("--job-path", required=True)
    p_worker.set_defaults(func=cmd_fetch_worker)

    p_login = sub.add_parser("login", help="扫码登录（打开网页，用 B 站 App 扫码）")
    p_login.add_argument("--timeout", type=int, default=180, help="等待确认的秒数")
    p_login.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_login.add_argument(
        "--normal-window", action="store_true",
        help="用普通窗口打开（默认用隐私窗口；已登录的浏览器看不到二维码）",
    )
    p_login.add_argument("--browser", default="", help="指定浏览器可执行文件路径")
    p_login.add_argument("--cookie-file", default="", help="Cookie 文件路径（默认 .cache/cookie.txt）")
    p_login.add_argument("--json", action="store_true")
    p_login.set_defaults(func=cmd_login)

    p_logout = sub.add_parser("logout", help="清除本地登录态")
    p_logout.add_argument("--cookie-file", default="")
    p_logout.add_argument("--json", action="store_true")
    p_logout.set_defaults(func=cmd_logout)

    p_whoami = sub.add_parser("whoami", help="查看当前登录账号")
    p_whoami.add_argument("--json", action="store_true")
    p_whoami.set_defaults(func=cmd_whoami)

    p_status = sub.add_parser("status", help="查询任务状态")
    p_status.add_argument("job_id")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_media = sub.add_parser("media", help="用 ffprobe 校验媒体文件")
    p_media.add_argument("paths", nargs="+")
    p_media.add_argument("--json", action="store_true")
    p_media.set_defaults(func=cmd_media)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认可能是 GBK；强制 UTF-8，避免中文输出乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - 非标准流
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        _log("已中断")
        return EXIT_FAIL
    except SystemExit as exc:
        return int(exc.code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
