"""配置加载。

优先级：显式参数 > 环境变量 > config.toml > 内置默认值。
所有目录类配置按项目根目录解析成绝对路径（Windows 中文/空格路径安全）。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def project_root() -> Path:
    """包所在仓库根目录（src/bilibili_agent/config.py → 仓库根）。"""
    return Path(__file__).resolve().parents[2]


def _find_config_file(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    env = os.environ.get("BILI_CONFIG")
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p
    for cand in (project_root() / "config.toml", Path.cwd() / "config.toml"):
        if cand.is_file():
            return cand
    return None


def _resolve(value: str | os.PathLike[str] | None, base: Path) -> str:
    if not value:
        return ""
    p = Path(str(value)).expanduser()
    if not p.is_absolute():
        p = base / p
    return str(p)


@dataclass(slots=True)
class Config:
    root: Path
    config_path: Path | None = None

    bdl_cli: str = ""
    ffmpeg: str = ""
    ffprobe: str = ""

    media_cache: str = ""
    output_dir: str = ""
    temp_dir: str = ""

    cookie_file: str = ""
    user_agent: str = DEFAULT_UA
    request_interval: float = 0.6
    use_env_proxy: bool = False

    search_limit: int = 20
    search_order: str = "totalrank"

    preferred_languages: list[str] = field(
        default_factory=lambda: ["zh-Hans", "zh-CN", "zh-Hant", "zh"]
    )
    min_segments: int = 3

    max_edge: int = 1280
    image_format: str = "jpg"
    max_images: int = 25
    min_gap_seconds: float = 3.0
    phash_distance: int = 6

    # 烧录字幕 OCR（Level 3）
    ocr_enabled: bool = True
    ocr_auto: bool = True
    ocr_band: str = "auto"  # auto=自动检测字幕区 | bottom=固定底部 | top=固定顶部
    ocr_sample_interval: float = 1.0
    ocr_band_top: float = 0.74
    ocr_band_bottom: float = 0.99
    ocr_top_band_top: float = 0.02
    ocr_top_band_bottom: float = 0.26
    # 区域跟踪（O1）
    ocr_region_windows: int = 6
    ocr_region_window_seconds: float = 6.0
    ocr_region_fps: float = 3.0
    ocr_region_min_score: float = 0.35
    ocr_min_score: float = 0.60
    ocr_min_coverage: float = 0.10

    asr_python: str = ""
    asr_model_path: str = ""
    asr_aligner_path: str = ""
    asr_device: str = "cuda:0"
    asr_language: str = "auto"  # auto=自动识别；也可指定 Chinese/English 等
    asr_max_chunk_seconds: float = 120.0
    asr_sample_rate: int = 16000

    raw: dict[str, Any] = field(default_factory=dict)

    # -- 派生路径 -------------------------------------------------------
    @property
    def media_cache_dir(self) -> Path:
        return Path(self.media_cache or (self.root / ".cache" / "media"))

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir or (self.root / "output"))

    @property
    def temp_path(self) -> Path:
        return Path(self.temp_dir or (self.root / ".cache" / "tmp"))

    def ensure_dirs(self) -> None:
        for d in (self.media_cache_dir, self.output_path, self.temp_path):
            d.mkdir(parents=True, exist_ok=True)

    def cookie_header(self) -> str | None:
        """读取 Cookie 文件内容；未配置或文件不存在返回 None。"""
        if not self.cookie_file:
            return None
        p = Path(self.cookie_file)
        if not p.is_file():
            return None
        text = p.read_text(encoding="utf-8").strip()
        return text or None


def _get(table: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = table
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def load_config(explicit: str | os.PathLike[str] | None = None) -> Config:
    """读取 config.toml（若存在）并套用环境变量覆盖。"""
    root = project_root()
    cfg_path = _find_config_file(explicit)
    data: dict[str, Any] = {}
    if cfg_path:
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)

    cfg = Config(root=root, config_path=cfg_path, raw=data)

    cfg.bdl_cli = _get(data, "paths", "bdl_cli", default="") or ""
    cfg.ffmpeg = _get(data, "paths", "ffmpeg", default="") or ""
    cfg.ffprobe = _get(data, "paths", "ffprobe", default="") or ""
    cfg.media_cache = _resolve(_get(data, "paths", "media_cache"), root)
    cfg.output_dir = _resolve(_get(data, "paths", "output_dir"), root)
    cfg.temp_dir = _resolve(_get(data, "paths", "temp_dir"), root)

    cfg.cookie_file = _resolve(_get(data, "bilibili", "cookie_file"), root)
    if not cfg.cookie_file:
        # 默认落在项目缓存目录（已被 .gitignore 忽略），`bili login` 会写到这里
        cfg.cookie_file = str(root / ".cache" / "cookie.txt")
    cfg.user_agent = _get(data, "bilibili", "user_agent", default=DEFAULT_UA)
    cfg.request_interval = float(_get(data, "bilibili", "request_interval", default=0.6))
    cfg.use_env_proxy = bool(_get(data, "bilibili", "use_env_proxy", default=False))

    cfg.search_limit = int(_get(data, "search", "limit", default=20))
    cfg.search_order = _get(data, "search", "order", default="totalrank")

    langs = _get(data, "subtitle", "preferred_languages")
    if isinstance(langs, list) and langs:
        cfg.preferred_languages = [str(x) for x in langs]
    cfg.min_segments = int(_get(data, "subtitle", "min_segments", default=3))

    cfg.max_edge = int(_get(data, "frame", "max_edge", default=1280))
    cfg.image_format = _get(data, "frame", "format", default="jpg")
    cfg.max_images = int(_get(data, "frame", "max_images", default=25))
    cfg.min_gap_seconds = float(_get(data, "frame", "min_gap_seconds", default=3.0))
    cfg.phash_distance = int(_get(data, "frame", "phash_distance", default=6))

    cfg.ocr_enabled = bool(_get(data, "ocr", "enabled", default=True))
    cfg.ocr_auto = bool(_get(data, "ocr", "auto", default=True))
    cfg.ocr_band = str(_get(data, "ocr", "band", default="auto"))
    cfg.ocr_sample_interval = float(_get(data, "ocr", "sample_interval", default=1.0))
    cfg.ocr_band_top = float(_get(data, "ocr", "band_top", default=0.74))
    cfg.ocr_band_bottom = float(_get(data, "ocr", "band_bottom", default=0.99))
    cfg.ocr_top_band_top = float(_get(data, "ocr", "top_band_top", default=0.02))
    cfg.ocr_top_band_bottom = float(_get(data, "ocr", "top_band_bottom", default=0.26))
    cfg.ocr_region_windows = int(_get(data, "ocr", "region_windows", default=6))
    cfg.ocr_region_window_seconds = float(
        _get(data, "ocr", "region_window_seconds", default=6.0)
    )
    cfg.ocr_region_fps = float(_get(data, "ocr", "region_fps", default=3.0))
    cfg.ocr_region_min_score = float(
        _get(data, "ocr", "region_min_score", default=0.35)
    )
    cfg.ocr_min_score = float(_get(data, "ocr", "min_score", default=0.60))
    cfg.ocr_min_coverage = float(_get(data, "ocr", "min_coverage", default=0.10))

    cfg.asr_python = _get(data, "asr", "python", default="") or ""
    cfg.asr_model_path = _resolve(_get(data, "asr", "model_path"), root)
    cfg.asr_aligner_path = _resolve(_get(data, "asr", "aligner_path"), root)
    cfg.asr_device = _get(data, "asr", "device", default="cuda:0")
    cfg.asr_language = _get(data, "asr", "language", default="auto")
    cfg.asr_max_chunk_seconds = float(_get(data, "asr", "max_chunk_seconds", default=120.0))
    cfg.asr_sample_rate = int(_get(data, "asr", "sample_rate", default=16000))

    # 环境变量覆盖
    if env := os.environ.get("BDL_CLI"):
        cfg.bdl_cli = env
    if env := os.environ.get("FFMPEG"):
        cfg.ffmpeg = env
    if env := os.environ.get("FFPROBE"):
        cfg.ffprobe = env
    if env := os.environ.get("BDL_COOKIE_FILE"):
        cfg.cookie_file = env
    if env := os.environ.get("QWEN_ASR_MODEL_PATH"):
        cfg.asr_model_path = env
    if env := os.environ.get("QWEN_ASR_PYTHON"):
        cfg.asr_python = env

    return cfg
