"""产物目录、缓存键与 manifest 读写。

缓存按**组件**分开记账：

* ``transcript`` 组件：字幕文本（依赖字幕来源、语言、ASR 模型等）
* ``frames`` 组件：关键帧与 article.md（依赖 max_images / min_gap / 去重阈值等）

这样 ``subtitle → illustrated`` 升级时，只重跑抽帧，字幕直接复用
（从已有 SRT 重建时间轴）；而改了 ``max_images`` 就只会让 frames 失效，
不会误用上一轮的图片。
"""

from __future__ import annotations

import hashlib
import json
import os
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
    """原子写 manifest：先写临时文件再 os.replace，读取方不会看到半截 JSON。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)
    return path


def files_exist(directory: Path, files: list[str]) -> bool:
    """校验清单里的文件都真实存在且非空。"""
    if not files:
        return False
    for rel in files:
        p = Path(directory) / rel
        if not p.is_file() or p.stat().st_size == 0:
            return False
    return True


def get_component(manifest: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    """取 manifest 里的某个组件记录。"""
    if not manifest:
        return None
    comp = (manifest.get("components") or {}).get(name)
    return comp if isinstance(comp, dict) else None


def component_is_fresh(
    directory: Path,
    manifest: dict[str, Any] | None,
    name: str,
    digest: str,
) -> bool:
    """某个组件是否可以复用：摘要一致 + 文件都存在。"""
    if not manifest or manifest.get("status") == "failed":
        return False
    comp = get_component(manifest, name)
    if not comp:
        return False
    if comp.get("digest") != digest:
        return False
    return files_exist(Path(directory), comp.get("files") or [])


def is_fresh(directory: Path, digest: str, mode: str) -> bool:
    """整份产物是否可以复用（兼容旧版 manifest）。

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
    return files_exist(Path(directory), manifest.get("files") or [])


def existing_subtitle_artifacts(directory: Path) -> dict[str, str] | None:
    """从已有 manifest 里找回字幕产物路径，供 subtitle → illustrated 升级复用。"""
    manifest = read_manifest(Path(directory))
    if not manifest:
        return None
    found = {}
    comp = get_component(manifest, "transcript")
    for rel in (comp or {}).get("files") or manifest.get("files") or []:
        name = Path(rel).name
        if name in ("transcript.md", "transcript.srt"):
            found[name] = str(Path(directory) / rel)
    return found or None
