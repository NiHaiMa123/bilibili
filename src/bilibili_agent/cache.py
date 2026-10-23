"""产物目录、缓存键与 manifest 读写。

缓存键覆盖 bvid+cid+mode+语言+算法版本+相关参数；同设置重复运行直接复用产物。
模式从 subtitle 升为 illustrated 时复用字幕数据。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def part_dir(root: Path, bvid: str, page: int, cid: int) -> Path:
    """``output/BVxxxxxxxxxx/P01_<cid>/``"""
    return Path(root) / bvid / f"P{page:02d}_{cid}"


def settings_digest(**parts: Any) -> str:
    """把影响产物的参数压成稳定摘要。"""
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def read_manifest(directory: Path) -> dict[str, Any] | None:
    path = Path(directory) / "manifest.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_manifest(directory: Path, manifest: dict[str, Any]) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def is_fresh(directory: Path, digest: str, mode: str) -> bool:
    """manifest 存在、摘要一致、且模式不低于请求模式时视为可复用。

    失败的结果（没有产出文件、或显式标记 failed）**不可复用**，
    否则一次失败会被后续运行当成"缓存命中"，永远拿不到内容。
    """
    manifest = read_manifest(Path(directory))
    if not manifest:
        return False
    if manifest.get("status") == "failed":
        return False
    if manifest.get("settings_digest") != digest:
        return False
    existing = manifest.get("mode") or "metadata"
    order = {"metadata": 0, "subtitle": 1, "assets": 1, "illustrated": 2, "both": 2}
    if order.get(existing, 0) < order.get(mode, 0):
        return False
    files = manifest.get("files") or []
    if not files:
        return False
    for rel in files:
        if not (Path(directory) / rel).exists():
            return False
    return True


def existing_subtitle_artifacts(directory: Path) -> dict[str, str] | None:
    """从已有 manifest 里找回字幕产物路径，供 subtitle → illustrated 升级复用。"""
    manifest = read_manifest(Path(directory))
    if not manifest:
        return None
    files = manifest.get("files") or []
    found = {}
    for rel in files:
        name = Path(rel).name
        if name in ("transcript.md", "transcript.srt"):
            found[name] = str(Path(directory) / rel)
    return found or None
