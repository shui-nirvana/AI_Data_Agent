"""真实 Docker 开发隔离检查；全部使用人工测试源码，不调用模型。

应先通过正常 A/C/D 烟测。检查不等于完整安全认证，不测试内核逃逸。
--stress 会主动占用 C 的内存限额并等待一次墙钟超时，只在专用开发机运行。
"""

import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from backend.domain.spec import freeze_spec
from backend.jobs.contracts import JobRequest, Stage
from backend.jobs.policy import LIMITS
from workers.sandbox_manager.docker_backend import DockerExecutionBackend

COPY_INPUT = """
from pathlib import Path
Path('/output/cleaned.csv').write_bytes(Path('/input/data.csv').read_bytes())
"""

BOUNDARIES = """
import errno
import os
from pathlib import Path
import socket
assert os.getuid() == 10001 and os.getgid() == 10001
assert not Path('/var/run/docker.sock').exists()
assert not Path('/input/host-only.txt').exists()
for path in ('/input/data.csv', '/app/write-probe'):
    try:
        with open(path, 'wb') as stream:
            stream.write(b'not allowed')
    except OSError as error:
        assert error.errno in (None, errno.EROFS, errno.EACCES, errno.EPERM)
    else:
        raise AssertionError('write boundary failed')
try:
    sock = socket.socket()
    sock.settimeout(2)
    sock.connect(('1.1.1.1', 443))
except OSError:
    pass
else:
    raise AssertionError('network boundary failed')
""" + COPY_INPUT

QUOTA = """
import errno
from pathlib import Path
for directory in ('/output', '/tmp'):
    path = Path(directory) / ('cleaned.csv' if directory == '/output' else 'quota-probe')
    try:
        with path.open('wb') as stream:
            for _ in range(20):
                stream.write(b'x' * 1048576)
    except OSError as error:
        assert error.errno == errno.ENOSPC
    else:
        raise AssertionError('temporary disk quota failed')
    finally:
        path.unlink(missing_ok=True)
""" + COPY_INPUT

LOG_FLOOD = """
for _ in range(100):
    print('x' * 2000, flush=True)
""" + COPY_INPUT

STRESS = {
    "wall_timeout": "import time\nwhile True:\n    time.sleep(1)\n",
    "memory_limit": "blocks = []\nwhile True:\n    blocks.append(bytearray(16 * 1024 * 1024))\n",
}


def run_checks(manifest: Path, output: Path, stress: bool = False) -> Path:
    configuration = json.loads(manifest.read_text(encoding="utf-8"))
    images = {Stage(key): value for key, value in configuration["images"].items()}
    directory = output / str(uuid4())
    directory.mkdir(parents=True)
    source = directory / "data.csv"
    source.write_bytes(b"id,value\n1,a\n")
    # 此文件不挂载进任何容器；源码只检查它未出现在 /input。
    (directory / "host-only.txt").write_text("non-sensitive test marker", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    task_id = str(uuid4())
    spec = freeze_spec(task_id=task_id, input_sha256=digest, owner_id="demo",
                       task_version=1, columns=("id", "value"), decisions=(), issues=())
    spec_path = directory / "spec.json"
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    backend = DockerExecutionBackend(directory / "attempts", images)
    checks = {"boundaries": BOUNDARIES, "tmpfs_quota": QUOTA, "log_truncation": LOG_FLOOD}
    if stress:
        checks.update(STRESS)
    records = []
    report_path = directory / "isolation.json"
    for name, code in checks.items():
        code_path = directory / f"{name}.py"
        code_path.write_text(code, encoding="utf-8")
        job = JobRequest(task_id, str(uuid4()), Stage.CLEAN_C, digest, spec.spec_sha256,
                         hashlib.sha256(code_path.read_bytes()).hexdigest())
        backend.prepare(job, {"data.csv": source, "spec.json": spec_path, "clean.py": code_path})
        receipt = backend.submit(job, LIMITS[job.stage])
        status = backend.status(receipt)
        execution = json.loads(Path(status.result_ref).read_text(encoding="utf-8"))
        attempt = backend.root / job.attempt_id
        if name == "boundaries":
            # 新入口把吞掉的越权异常也记作违规；必须失败，不再要求生成成功候选。
            # 检查的是禁用操作被入口/容器阻止，不单独证明底层 seccomp/capabilities。
            passed = (status.state == "failed" and execution.get("runtime_category") == "security_violation"
                      and not (attempt / "result").exists())
        elif name == "wall_timeout":
            passed = status.state == "failed" and status.timed_out
        elif name == "memory_limit":
            stderr = attempt / "logs/stderr.log"
            memory_error = stderr.exists() and b"MemoryError" in stderr.read_bytes()
            passed = (status.state == "failed" and not status.timed_out
                      and (status.limit_reason == "oom" or status.exit_code == 137
                           or (status.exit_code == 1 and
                               (memory_error or execution.get("runtime_category") == "restricted_or_input_error"))))
        else:
            candidate = attempt / "result/cleaned.csv"
            passed = (status.state == "finished" and candidate.exists()
                      and hashlib.sha256(candidate.read_bytes()).hexdigest() == digest)
            if name == "log_truncation":
                log = attempt / "logs/stdout.log"
                passed = passed and log.exists() and log.stat().st_size == 65536
        # SDK 查询同标签容器为空，确认这次尝试确实被回收。
        remaining = backend.client.containers.list(
            all=True, filters={"label": f"ai-data-agent.attempt={job.attempt_id}"}
        )
        passed = passed and not remaining and hashlib.sha256(source.read_bytes()).hexdigest() == digest
        records.append({"check": name, "passed": passed, "execution": execution})
        report_path.write_text(json.dumps({
            "kind": "test_source_not_llm", "checks": records,
            "complete": len(records) == len(checks), "stress_requested": stress,
            "deployment_platform": configuration["deployment_platform"],
            "target_platform_verified": False,
        }, indent=2), encoding="utf-8")
        if not passed:
            raise RuntimeError(f"{name} 未通过，证据见 {report_path}")
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description="真实 Docker 开发隔离检查（不是完整安全认证）")
    parser.add_argument("--images", type=Path, default=Path(".runtime/docker/images.json"))
    parser.add_argument("--output", type=Path, default=Path(".runtime/docker/isolation"))
    parser.add_argument("--stress", action="store_true", help="主动触发内存限额与墙钟超时")
    args = parser.parse_args()
    report = run_checks(args.images, args.output, args.stress)
    print(f"所选隔离检查通过；证据：{report}。PID/CPU/取消/崩溃仍需目标机验收。")


if __name__ == "__main__":
    main()
