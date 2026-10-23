"""本地持久任务（JobManager）。

CLI 的 ``--async`` 与 MCP 的 ``wait=false`` 共用这一套协议与状态目录，
避免维护两套任务机制。

约定：
* 每个任务两个文件：``<id>.spec.json``（入参，提交后不再改）与
  ``<id>.json``（状态，可反复原子替换）；
* 状态流转：``queued → running → completed / failed``；
* 状态文件用"写临时文件 + os.replace"原子替换，读取方不会看到半截 JSON；
* worker 进程死掉时，``status()`` 会把任务判为 failed，
  不会出现永远停在 queued/running 的假运行。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

JOB_SUFFIX = ".json"
SPEC_SUFFIX = ".spec.json"


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class JobSpec:
    """一次取素材任务的完整入参（必须能独立还原，不依赖父进程内存）。"""

    target: str
    parts: list[int] = field(default_factory=list)
    mode: str = "subtitle"
    subtitle_source: str = "auto"
    refresh: bool = False
    max_images: int | None = None
    config_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobSpec:
        return cls(
            target=str(data.get("target") or ""),
            parts=[int(p) for p in (data.get("parts") or [])],
            mode=str(data.get("mode") or "subtitle"),
            subtitle_source=str(data.get("subtitle_source") or "auto"),
            refresh=bool(data.get("refresh")),
            max_images=(
                int(data["max_images"]) if data.get("max_images") is not None else None
            ),
            config_path=str(data.get("config_path") or ""),
        )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写 JSON：先写临时文件，再 os.replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def _pid_alive(pid: int) -> bool:
    """检查进程是否还活着（Windows / POSIX 通用）。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30,
            )
            return str(pid) in (out.stdout or "")
        except (subprocess.SubprocessError, OSError):
            return True  # 查不到就当活着，避免误杀
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class JobManager:
    """任务状态的唯一来源。"""

    def __init__(self, jobs_dir: str | Path) -> None:
        self.dir = Path(jobs_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- 路径 -----------------------------------------------------------
    def job_path(self, job_id: str) -> Path:
        return self.dir / f"{job_id}{JOB_SUFFIX}"

    def spec_path(self, job_id: str) -> Path:
        return self.dir / f"{job_id}{SPEC_SUFFIX}"

    # -- 读写 -----------------------------------------------------------
    def read_job(self, job_id: str) -> dict[str, Any] | None:
        path = self.job_path(job_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def write_job(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = {**payload, "updated_at": _now_iso()}
        _atomic_write_json(self.job_path(job_id), payload)
        return payload

    def update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        current = self.read_job(job_id) or {"id": job_id, "state": "queued"}
        current.update(fields)
        return self.write_job(job_id, current)

    def read_spec(self, job_id: str) -> JobSpec | None:
        path = self.spec_path(job_id)
        if not path.is_file():
            return None
        try:
            return JobSpec.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, ValueError):
            return None

    # -- 创建 / 提交 ----------------------------------------------------
    def create(self, spec: JobSpec) -> str:
        job_id = uuid.uuid4().hex[:12]
        _atomic_write_json(self.spec_path(job_id), spec.to_dict())
        self.write_job(job_id, {
            "id": job_id,
            "state": "queued",
            "stage": "queued",
            "progress": None,
            "created_at": _now_iso(),
            "spec": spec.to_dict(),
            "outputs": [],
            "error": None,
            "result": None,
        })
        return job_id

    def submit(self, spec: JobSpec) -> dict[str, Any]:
        """提交后台任务并**立即返回**（不在本进程做重计算）。"""
        job_id = self.create(spec)
        cmd = [
            sys.executable, "-m", "bilibili_agent.jobs",
            "--job", job_id, "--jobs-dir", str(self.dir),
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.update(
                job_id, state="failed", stage="spawn",
                error={
                    "stage": "job_spawn",
                    "error_code": None,
                    "message": f"无法启动后台任务：{exc}",
                    "retryable": True,
                },
            )
            return self.read_job(job_id) or {"id": job_id, "state": "failed"}

        self.update(job_id, pid=proc.pid, state="queued", stage="queued")
        return self.read_job(job_id) or {"id": job_id, "state": "queued"}

    # -- 查询 -----------------------------------------------------------
    def status(self, job_id: str) -> dict[str, Any] | None:
        """查询任务状态；worker 已死则判为 failed。"""
        job = self.read_job(job_id)
        if job is None:
            return None
        if job.get("state") in ("queued", "running"):
            pid = int(job.get("pid") or 0)
            if pid and not _pid_alive(pid):
                job = self.update(
                    job_id, state="failed", stage="worker_lost",
                    error={
                        "stage": "job_worker",
                        "error_code": None,
                        "message": "后台任务进程已退出，状态未更新（可能是被杀死或启动即失败）",
                        "retryable": False,
                    },
                )
        return job

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        jobs = []
        for path in sorted(self.dir.glob(f"*{JOB_SUFFIX}"), reverse=True):
            if path.name.endswith(SPEC_SUFFIX):
                continue
            data = self.read_job(path.stem)
            if data:
                jobs.append(data)
            if len(jobs) >= limit:
                break
        return jobs


# ---------------------------------------------------------------- worker 入口
def execute_fetch(spec: JobSpec) -> dict[str, Any]:
    """按 spec 执行一次 fetch（worker 与同步路径共用）。"""
    from .config import load_config
    from .pipeline import Pipeline

    cfg = load_config(spec.config_path or None)
    if spec.max_images:
        cfg.max_images = max(1, min(int(spec.max_images), 200))
    pipe = Pipeline(cfg)
    try:
        result = pipe.fetch(
            spec.target,
            parts=spec.parts or None,
            mode=spec.mode,
            subtitle_source=spec.subtitle_source,
            refresh=spec.refresh,
        )
    finally:
        pipe.close()
    return result.to_dict()


def worker_main(job_id: str, jobs_dir: str | Path) -> int:
    """后台 worker：读 spec → 跑 fetch → 写终态。"""
    mgr = JobManager(jobs_dir)
    spec = mgr.read_spec(job_id)
    if spec is None:
        mgr.update(
            job_id, state="failed", stage="spec",
            error={
                "stage": "job_spec",
                "error_code": None,
                "message": "任务入参文件缺失或损坏",
                "retryable": False,
            },
        )
        return 2

    mgr.update(job_id, state="running", stage="fetch", progress=None)
    try:
        payload = execute_fetch(spec)
    except Exception as exc:  # noqa: BLE001 - 任何异常都要落到 failed
        mgr.update(
            job_id, state="failed", stage="fetch",
            error={
                "stage": "fetch",
                "error_code": None,
                "message": f"{type(exc).__name__}: {exc}",
                "retryable": False,
            },
        )
        return 1

    ok = bool(payload.get("ok"))
    mgr.update(
        job_id,
        state="completed" if ok else "failed",
        stage="done",
        outputs=payload.get("outputs") or [],
        result=payload,
        error=(None if ok else (payload.get("error") or {
            "stage": "fetch",
            "error_code": None,
            "message": "任务未成功完成",
            "retryable": False,
        })),
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="bilibili-agent 后台任务 worker")
    parser.add_argument("--job", required=True)
    parser.add_argument("--jobs-dir", required=True)
    args = parser.parse_args(argv)
    return worker_main(args.job, args.jobs_dir)


if __name__ == "__main__":
    raise SystemExit(main())
