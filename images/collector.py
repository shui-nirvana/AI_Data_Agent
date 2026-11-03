"""镜像内可信产物收集器。Docker archive API 在部分引擎不能读取 tmpfs。"""

import io
import os
from pathlib import Path
import signal
import stat
import sys
import tarfile

OUTPUTS = {
    "PROFILE_A": ("profile.json", "issues.json"),
    "CLEAN_C": ("cleaned.csv",),
    "VERIFY_D": ("validation.json",),
}
LIMITS = {"PROFILE_A": 1_000_000, "CLEAN_C": 12_000_000, "VERIFY_D": 1_000_000}


def stop_children():
    """容器 PID 命名空间内只有可信 PID 1 和本收集器可继续运行。

    无提权能力的作业与收集器同 UID；SIGSTOP 不可被捕获。
    按 PID 顺序多轮枚举，停止可能遗留的 fork 后台子进程。
    """
    stopped = set()
    for _ in range(8):
        pids = sorted(int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit())
        pending = [pid for pid in pids if pid not in (1, os.getpid()) and pid not in stopped]
        if not pending:
            return
        for pid in pending:
            try:
                os.kill(pid, signal.SIGSTOP)
                stopped.add(pid)
            except ProcessLookupError:
                pass
    raise RuntimeError("cannot quiesce descendants")


def read_regular(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not a regular output")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("output quota exceeded")
        return data


def main():
    stage = sys.argv[1] if len(sys.argv) == 2 else ""
    if stage not in OUTPUTS:
        raise SystemExit(2)
    stop_children()
    files = {}
    for name in OUTPUTS[stage]:
        try:
            files[name] = read_regular(Path("/output") / name, LIMITS[stage])
        except FileNotFoundError:
            pass  # 缺输出仍收集有界诊断，由 worker 明确判失败，绝非成功。
    if sum(map(len, files.values())) > LIMITS[stage]:
        raise ValueError("aggregate output quota exceeded")
    for name in ("stdout.log", "stderr.log"):
        try:
            fd = os.open(f"/tmp/{name}", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    files[name] = stream.read(65536)
        except OSError:
            pass  # 日志可缺失，固定产物和收集退出码不可缺失。
    if stage == "CLEAN_C":
        try:
            files["runtime.json"] = read_regular(Path("/tmp/runtime.json"), 4096)
        except FileNotFoundError:
            pass
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(data))


if __name__ == "__main__":
    main()
