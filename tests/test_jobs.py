"""后台任务机制（T01/T02/T03）：JobManager 协议、MCP 真异步、CLI --async --refresh。"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from bilibili_agent.jobs import JobManager, JobSpec, worker_main


def _spec(tmp_path: Path, **kw) -> JobSpec:
    return JobSpec(target=kw.pop("target", "BV1xx411c7mD"), **kw)


# ---------------------------------------------------------------- 状态协议
def test_create_writes_queued_state(tmp_path):
    mgr = JobManager(tmp_path / "jobs")
    job_id = mgr.create(_spec(tmp_path))
    job = mgr.read_job(job_id)
    assert job["state"] == "queued"
    assert job["id"] == job_id
    # spec 单独存，状态文件可反复替换
    assert mgr.read_spec(job_id).target == "BV1xx411c7mD"


def test_job_file_is_written_atomically(tmp_path):
    mgr = JobManager(tmp_path / "jobs")
    job_id = mgr.create(_spec(tmp_path))
    for i in range(5):
        mgr.update(job_id, state="running", stage=f"step{i}")
    # 读到的永远是完整 JSON，且没有残留临时文件
    data = mgr.read_job(job_id)
    assert data["state"] == "running"
    assert not list((tmp_path / "jobs").glob("*.tmp"))


def test_spec_roundtrip_preserves_params(tmp_path):
    mgr = JobManager(tmp_path / "jobs")
    spec = JobSpec(
        target="BV1", parts=[1, 2], mode="illustrated",
        subtitle_source="ocr", refresh=True, max_images=7, config_path="c.toml",
    )
    job_id = mgr.create(spec)
    back = mgr.read_spec(job_id)
    assert back.parts == [1, 2]
    assert back.mode == "illustrated"
    assert back.subtitle_source == "ocr"
    assert back.refresh is True
    assert back.max_images == 7


# ---------------------------------------------------------------- T01 立即返回
def test_submit_returns_before_work_completes(tmp_path):
    """submit 必须立刻返回 job_id，不在调用线程做重计算。"""
    mgr = JobManager(tmp_path / "jobs")
    job_id = mgr.create(_spec(tmp_path))
    # 用一个会睡一会儿的真实子进程模拟 worker
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    mgr.update(job_id, pid=proc.pid, state="queued")
    try:
        t0 = time.monotonic()
        status = mgr.status(job_id)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0
        assert status["state"] in ("queued", "running")
        assert status["id"] == job_id
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_status_detects_dead_worker(tmp_path):
    """worker 进程死掉时，状态必须转为 failed，不能永远停在 running。"""
    mgr = JobManager(tmp_path / "jobs")
    job_id = mgr.create(_spec(tmp_path))
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    mgr.update(job_id, pid=proc.pid, state="running")
    proc.kill()
    proc.wait(timeout=10)
    status = mgr.status(job_id)
    assert status["state"] == "failed"
    assert status["error"]["stage"] == "job_worker"


def test_status_of_unknown_job_is_none(tmp_path):
    mgr = JobManager(tmp_path / "jobs")
    assert mgr.status("nope") is None


# ---------------------------------------------------------------- T02 worker 终态
def test_worker_success_marks_completed(tmp_path, monkeypatch):
    import bilibili_agent.jobs as jobs_mod

    def fake_execute(spec):
        return {
            "ok": True, "status": "completed", "bvid": "BV1",
            "outputs": ["out/transcript.md"], "warnings": [],
        }

    monkeypatch.setattr(jobs_mod, "execute_fetch", fake_execute)
    mgr = JobManager(tmp_path / "jobs")
    job_id = mgr.create(_spec(tmp_path))
    rc = worker_main(job_id, tmp_path / "jobs")
    assert rc == 0
    job = mgr.read_job(job_id)
    assert job["state"] == "completed"
    assert job["outputs"] == ["out/transcript.md"]


def test_worker_failure_marks_failed_with_error(tmp_path, monkeypatch):
    import bilibili_agent.jobs as jobs_mod

    def boom(spec):
        raise RuntimeError("模拟流水线炸了")

    monkeypatch.setattr(jobs_mod, "execute_fetch", boom)
    mgr = JobManager(tmp_path / "jobs")
    job_id = mgr.create(_spec(tmp_path))
    rc = worker_main(job_id, tmp_path / "jobs")
    assert rc == 1
    job = mgr.read_job(job_id)
    assert job["state"] == "failed"
    assert "模拟流水线炸了" in job["error"]["message"]


def test_worker_without_spec_fails_cleanly(tmp_path):
    mgr = JobManager(tmp_path / "jobs")
    job_id = "deadbeef"
    mgr.write_job(job_id, {"id": job_id, "state": "queued"})
    rc = worker_main(job_id, tmp_path / "jobs")
    assert rc == 2
    assert mgr.read_job(job_id)["state"] == "failed"


def test_worker_result_not_ok_is_failed(tmp_path, monkeypatch):
    import bilibili_agent.jobs as jobs_mod

    monkeypatch.setattr(
        jobs_mod, "execute_fetch",
        lambda spec: {"ok": False, "status": "failed", "outputs": [],
                      "error": {"stage": "subtitle", "message": "没字幕"}},
    )
    mgr = JobManager(tmp_path / "jobs")
    job_id = mgr.create(_spec(tmp_path))
    worker_main(job_id, tmp_path / "jobs")
    job = mgr.read_job(job_id)
    assert job["state"] == "failed"
    assert job["error"]["stage"] == "subtitle"


# ---------------------------------------------------------------- T03 CLI --async --refresh
def test_cli_async_passes_refresh_to_spec(tmp_path):
    """--async 时 refresh 必须进 spec，不能静默复用旧产物。"""
    from bilibili_agent.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "fetch", "BV1", "--async", "--refresh", "--max-images", "9",
        "--parts", "1", "2",
    ])
    assert args.refresh is True
    assert args.max_images == 9
    assert args.parts == [1, 2]

    spec = JobSpec(
        target=args.target, parts=list(args.parts or []), mode=args.mode,
        subtitle_source=args.subtitle_source, refresh=bool(args.refresh),
        max_images=args.max_images,
    )
    assert spec.refresh is True
    assert spec.max_images == 9


def test_cli_async_job_file_records_refresh(tmp_path):
    """提交后，任务文件里的 spec 必须带 refresh=True。"""
    mgr = JobManager(tmp_path / "jobs")
    spec = JobSpec(target="BV1", refresh=True, max_images=4)
    job_id = mgr.create(spec)
    stored = mgr.read_job(job_id)["spec"]
    assert stored["refresh"] is True
    assert stored["max_images"] == 4


# ---------------------------------------------------------------- MCP 真异步
def test_mcp_wait_false_returns_job_id_without_blocking(tmp_path, monkeypatch):
    """MCP wait=false：必须立即返回 job_id，不能在调用线程里跑重计算。"""
    import bilibili_agent.mcp_server as mcp_mod

    class _FakeCfg:
        temp_path = tmp_path / "tmp"
        config_path = None
        max_images = 25

    class _FakePipe:
        def __init__(self):
            self.cfg = _FakeCfg()

        def fetch(self, *a, **kw):  # 不应被调用
            raise AssertionError("wait=false 时不应在 MCP 线程执行 fetch")

        def close(self):
            pass

    monkeypatch.setattr(mcp_mod, "_pipeline", lambda: _FakePipe())
    server = mcp_mod._build_server()

    import asyncio

    t0 = time.monotonic()
    result = asyncio.run(
        server.call_tool("bili_materialize", {"url_or_bvid": "BV1xx411c7mD", "wait": False})
    )
    elapsed = time.monotonic() - t0

    payload = _payload_of(result)
    assert elapsed < 5.0
    assert payload["job_id"]
    assert payload["status"] in ("queued", "running", "completed", "failed")
    # 任务确实被登记了
    mgr = JobManager(tmp_path / "tmp" / "jobs")
    assert mgr.read_job(payload["job_id"]) is not None


def test_mcp_wait_true_is_synchronous(tmp_path, monkeypatch):
    import bilibili_agent.mcp_server as mcp_mod

    class _FakeCfg:
        temp_path = tmp_path / "tmp"
        config_path = None
        max_images = 25

    class _FakePipe:
        def __init__(self):
            self.cfg = _FakeCfg()
            self.called = False

        def fetch(self, *a, **kw):
            self.called = True
            return _FakeResult()

        def close(self):
            pass

    class _FakeResult:
        def to_dict(self):
            return {
                "ok": True, "status": "completed", "bvid": "BV1", "parts": [],
                "outputs": ["x.md"], "warnings": [],
            }

    pipe = _FakePipe()
    monkeypatch.setattr(mcp_mod, "_pipeline", lambda: pipe)
    server = mcp_mod._build_server()

    import asyncio

    result = asyncio.run(
        server.call_tool("bili_materialize", {"url_or_bvid": "BV1", "wait": True})
    )
    payload = _payload_of(result)
    assert pipe.called
    assert payload["ok"] is True
    assert payload["outputs"] == ["x.md"]


def _payload_of(result):
    """从 MCP 调用结果里取出结构化 payload。"""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        if "result" in structured and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    content = getattr(result, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError("无法从 MCP 结果中取出 payload")
