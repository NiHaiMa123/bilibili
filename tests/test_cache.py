"""缓存键、目录与幂等复用。"""

from __future__ import annotations

from bilibili_agent.cache import (
    is_fresh,
    part_dir,
    read_manifest,
    settings_digest,
    write_manifest,
)


def test_part_dir_layout():
    d = part_dir("output", "BV1xx411c7mD", 1, 62131)
    assert d.name == "P01_62131"
    assert d.parent.name == "BV1xx411c7mD"


def test_settings_digest_is_stable_and_order_free():
    a = settings_digest(mode="subtitle", cid=1, lang="zh")
    b = settings_digest(lang="zh", cid=1, mode="subtitle")
    assert a == b
    assert settings_digest(mode="illustrated", cid=1, lang="zh") != a


def test_manifest_roundtrip(tmp_path):
    write_manifest(tmp_path, {"bvid": "BV1", "mode": "subtitle", "files": []})
    data = read_manifest(tmp_path)
    assert data["bvid"] == "BV1"


def test_is_fresh_requires_existing_files(tmp_path):
    write_manifest(tmp_path, {
        "settings_digest": "d1", "mode": "subtitle", "files": ["transcript.md"],
    })
    assert not is_fresh(tmp_path, "d1", "subtitle")  # 文件不存在
    (tmp_path / "transcript.md").write_text("x", encoding="utf-8")
    assert is_fresh(tmp_path, "d1", "subtitle")
    assert not is_fresh(tmp_path, "d2", "subtitle")  # 摘要不同


def test_subtitle_artifact_can_be_reused_for_illustrated(tmp_path):
    write_manifest(tmp_path, {
        "settings_digest": "d1", "mode": "subtitle",
        "files": ["transcript.md", "transcript.srt"],
    })
    (tmp_path / "transcript.md").write_text("x", encoding="utf-8")
    (tmp_path / "transcript.srt").write_text("x", encoding="utf-8")
    assert is_fresh(tmp_path, "d1", "subtitle")
    # illustrated 需要新产物，不能直接判定为可复用
    assert not is_fresh(tmp_path, "d1", "illustrated")


def test_read_manifest_handles_broken_json(tmp_path):
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")
    assert read_manifest(tmp_path) is None


def test_failed_manifest_is_not_reused(tmp_path):
    """失败的结果不能被当成缓存命中，否则会永远拿不到内容。"""
    write_manifest(tmp_path, {
        "settings_digest": "d1", "mode": "subtitle", "files": [],
        "status": "failed",
    })
    assert not is_fresh(tmp_path, "d1", "subtitle")


def test_empty_file_list_is_not_fresh(tmp_path):
    write_manifest(tmp_path, {"settings_digest": "d1", "mode": "subtitle", "files": []})
    assert not is_fresh(tmp_path, "d1", "subtitle")
