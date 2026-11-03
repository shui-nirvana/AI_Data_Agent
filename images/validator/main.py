"""
D 的容器内入口。控制层调度仍关闭，不得在 API 进程调用。
仅读取固定只读路径中的原件/规范/候选，并产生有界验证报告。
"""

import csv
import json
from pathlib import Path

from backend.domain.spec import FrozenSpec
from images.validator.verify import verify


def main() -> None:
    """验证程序主入口"""
    output = Path("/output")
    output.mkdir(parents=True, exist_ok=True)
    try:
        with Path("/input/spec.json").open("rb") as stream:
            content = stream.read(262145)
        if len(content) > 262144:
            raise ValueError("规范超过大小限制")
        spec = FrozenSpec.model_validate_json(content)
        report = verify(Path("/input/data.csv"), Path("/input/candidate.csv"), spec)
    except (ValueError, csv.Error, OSError) as exc:
        # 不把异常的原始消息写入报告，它可能包含用户数据或宿主路径。
        report = {"verdict": "rejected", "error_code": "INVALID_INPUT_OR_SPEC",
                  "error_type": type(exc).__name__}
    (output / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )
    if report["verdict"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
