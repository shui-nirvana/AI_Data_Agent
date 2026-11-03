"""D 独立验算。只依据原件和可信冻结规范，不导入 C 或 A 的转换逻辑。"""

import re
from datetime import date
from pathlib import Path

from backend.domain.spec import Action, FrozenSpec
from images.csv_input import read_csv

VALIDATOR_VERSION = "1.0"


def expected_date(value: str, formats: tuple[str, ...]) -> str:
    """仅匹配获批的完整格式，不启发式解析；无效日期保持原字面值。"""
    patterns = {
        "YYYY-MM-DD": (r"(\d{4})-(\d{2})-(\d{2})", (0, 1, 2)),
        "YYYY/MM/DD": (r"(\d{4})/(\d{2})/(\d{2})", (0, 1, 2)),
        "DD-MM-YYYY": (r"(\d{2})-(\d{2})-(\d{4})", (2, 1, 0)),
    }
    for format_name in formats:
        pattern, order = patterns[format_name]
        match = re.fullmatch(pattern, value, flags=re.ASCII)
        if match:
            numbers = tuple(int(part) for part in match.groups())
            try:
                return date(*(numbers[index] for index in order)).isoformat()
            except ValueError:
                return value
    return value


def verify(source: Path, candidate: Path, spec: FrozenSpec) -> dict:
    """逐格验证，失败返回固定错误码，不把敏感原值写入日志或报告。"""
    # 即使调用方通过 model_construct 绕过验证，这里仍重新核验可信规范。
    spec = FrozenSpec.model_validate(spec.model_dump(mode="json"))
    original, result = read_csv(source), read_csv(candidate)
    report = {
        "schema_version": "1.0", "validator_version": VALIDATOR_VERSION,
        "input_sha256": original.sha256, "output_sha256": result.sha256,
        "spec_sha256": spec.spec_sha256, "verdict": "rejected",
        "checks": [], "changed_cells": 0, "mismatch_count": 0,
    }
    structural = (
        ("input_hash", original.sha256 == spec.input_sha256),
        ("same_columns", original.columns == result.columns == spec.columns),
        ("same_row_count", len(original.rows) == len(result.rows)),
        ("zero_change_bytes", any(d.action != Action.PRESERVE for d in spec.decisions)
         or original.sha256 == result.sha256),
    )
    report["checks"] = [{"id": key, "passed": passed} for key, passed in structural]
    if not all(passed for _, passed in structural):
        return report
    actions = {decision.column: decision for decision in spec.decisions}
    for before, after in zip(original.rows, result.rows):
        for column, raw, actual in zip(original.columns, before, after):
            expected = raw
            decision = actions.get(column)
            if decision and decision.action == Action.NORMALIZE_DATE:
                expected = expected_date(raw, decision.params.input_formats)
            elif decision and decision.action == Action.FILL_NULL_WITH_LITERAL and raw == "":
                expected = decision.params.literal
            report["changed_cells"] += raw != actual
            report["mismatch_count"] += expected != actual
    correct = report["mismatch_count"] == 0
    report["checks"].append({"id": "exact_authorized_cells_and_row_positions", "passed": correct})
    report["verdict"] = "passed" if correct else "rejected"
    return report
