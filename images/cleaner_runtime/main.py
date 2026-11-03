"""
C 固定容器内入口；只允许 worker 在受限 Linux 容器内运行。
源码始终不可信。API/Graph Runner 不得导入调用本模块。
"""

import os
import csv
import json
from pathlib import Path
import runpy
import sys

from backend.domain.spec import FrozenSpec


def main() -> None:
    """清洗程序主入口"""
    if os.environ.get("AI_AGENT_SANDBOX_JOB") != "CLEAN_C":
        raise SystemExit("C 仅允许在受信 worker 创建的受限容器中运行")
    # 环境标识不是沙箱安全边界；边界由 worker 的实际 Docker 配置保证。
    with Path("/input/spec.json").open("rb") as stream:
        content = stream.read(262145)
    if len(content) > 262144:
        raise ValueError("规范超过大小限制")
    FrozenSpec.model_validate_json(content)
    if Path("/input/clean.py").stat().st_size > 262144:
        raise ValueError("源码超过大小限制")
    violation = False

    def audit(event, args):
        """额外保守拒绝与分类，不代替 Docker 隔离；捕获后吞掉异常也算违规。"""
        nonlocal violation
        denied = event.startswith(("socket.", "subprocess.", "ctypes."))
        denied = denied or event in ("os.system", "os.fork", "os.exec", "os.posix_spawn")
        if event == "open":
            path, mode, flags = args
            if isinstance(path, (str, bytes)):
                name = os.fsdecode(path)
                if name.startswith(("/input/", "/output/")):
                    writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
                    denied = bool(writing and name != "/output/cleaned.csv")
                elif name.startswith(("/etc/", "/proc/", "/sys/", "/dev/")):
                    denied = True
        if denied:
            violation = True
            raise PermissionError("sandbox operation denied")

    sys.addaudithook(audit)
    category = "completed"
    try:
        runpy.run_path("/input/clean.py", run_name="__main__")
    except (PermissionError, OSError, MemoryError, csv.Error, UnicodeError):
        category = "restricted_or_input_error"
    except (SyntaxError, NameError, TypeError, AttributeError, KeyError, IndexError,
            ValueError, ImportError, RuntimeError, ZeroDivisionError):
        # 不保存异常消息/堆栈。只产生粗粒度安全反馈，禁止模型猜测业务值。
        category = "runtime_error"
    except BaseException:
        category = "unknown_error"
    if violation:
        category = "security_violation"
    Path("/tmp/runtime.json").write_text(json.dumps({"category": category}), encoding="utf-8")
    if category != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
