"""控制层只校验有界 JSON 证据与哈希，绝不解析原始 CSV。"""

import hashlib
import json
from pathlib import Path

from backend.domain.csv_policy import CSV_POLICY, PARSER_VERSION


def read_json(path: Path, limit: int = 1_000_000):
    if path.is_symlink() or not path.is_file():
        raise ValueError("证据必须是普通文件")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("JSON 证据超过配额")
    return json.loads(data)


def file_hash(path: Path, limit: int) -> str:
    """读取字节验证内容绑定，不解释 CSV 字段。"""
    if path.is_symlink() or not path.is_file():
        raise ValueError("产物必须是普通文件")
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            size += len(chunk)
            if size > limit:
                raise ValueError("产物超过配额")
            digest.update(chunk)
    return digest.hexdigest()


def validate_profile(profile, issues, digest: str):
    """接受固定扫描器版本/完整工具集合，拒绝损坏或不完整的审批证据。"""
    if not isinstance(profile, dict) or not isinstance(issues, list):
        raise ValueError("A 输出类型错误")
    if (profile.get("schema_version") != "1.0" or profile.get("scanner_version") != "0.2.0"
            or profile.get("parser_version") != PARSER_VERSION
            or profile.get("status") != "completed" or profile.get("input_sha256") != digest
            or profile.get("limits") != CSV_POLICY.model_dump()):
        raise ValueError("A 版本、完成状态或输入不匹配")
    columns = profile.get("columns")
    count = profile.get("row_count")
    if (not isinstance(columns, list) or not 1 <= len(columns) <= CSV_POLICY.max_columns
            or any(not isinstance(c, str) or not c.strip() or len(c) > CSV_POLICY.max_field_chars
                   for c in columns) or len(set(columns)) != len(columns)
            or type(count) is not int or not 0 <= count <= CSV_POLICY.max_rows
            or count * len(columns) > CSV_POLICY.max_cells):
        raise ValueError("A 表结构或行数不合约")
    expected = {"csv_structure", "missing_cells", "duplicate_rows", "type_format", "numeric_range"}
    runs = profile.get("tool_runs", [])
    if (not isinstance(runs, list) or len(runs) != len(expected)
            or any(not isinstance(r, dict) or r.get("status") != "completed"
                   or r.get("version") != "0.2.0" or r.get("input_sha256") != digest for r in runs)
            or {r.get("tool_id") for r in runs} != expected):
        raise ValueError("A 工具未全部完成")
    aggregates = {}
    for key in ("missing_by_column", "date_formats_by_column", "numeric_by_column"):
        mapping = profile.get(key)
        if not isinstance(mapping, dict) or set(mapping) != set(columns):
            raise ValueError("A 聚合字段不完整")
        aggregates[key] = mapping
    expected_findings = {}
    for column in columns:
        missing = aggregates["missing_by_column"][column]
        dates = aggregates["date_formats_by_column"][column]
        numeric = aggregates["numeric_by_column"][column]
        if (type(missing) is not int or not 0 <= missing <= count
                or not isinstance(dates, dict)
                or any(name not in ("YYYY-MM-DD", "YYYY/MM/DD", "DD-MM-YYYY")
                       or type(value) is not int or not 0 <= value <= count
                       for name, value in dates.items()) or sum(dates.values()) > count
                or not isinstance(numeric, dict)
                or any(type(numeric.get(key)) is not int or not 0 <= numeric[key] <= count
                       for key in ("count", "negative_count"))
                or numeric["negative_count"] > numeric["count"]):
            raise ValueError("A 聚合数值不合约")
        for key in ("min", "max"):
            value = numeric.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 130):
                raise ValueError("A 数值范围不合约")
        facts = {
            "missing_cells": missing,
            "type_format": sum(n for name, n in dates.items() if name != "YYYY-MM-DD"),
            "numeric_range": numeric["negative_count"],
        }
        expected_findings[column] = {tool: n for tool, n in facts.items() if n}
    semantics = {
        "missing_cells": ("empty_string", {"preserve", "fill_null_with_literal"}),
        "type_format": ("parseable_nonstandard_date", {"preserve", "normalize_date"}),
        "numeric_range": ("negative_number", {"preserve"}),
    }
    seen = set()
    if len(issues) > len(columns):
        raise ValueError("A 问题数超过列数")
    for issue in issues:
        if not isinstance(issue, dict):
            raise ValueError("问题类型错误")
        column = issue.get("column")
        if (column not in columns or column in seen or issue.get("source") != "fixed"
                or issue.get("input_sha256") != digest):
            raise ValueError("A 问题来源或列不合约")
        seen.add(column)
        identity = json.dumps([digest, "0.2.0", column], ensure_ascii=False)
        if issue.get("issue_id") != hashlib.sha256(identity.encode("utf-8")).hexdigest():
            raise ValueError("A 问题标识不匹配")
        findings = issue.get("findings")
        if not isinstance(findings, list) or not 1 <= len(findings) <= len(semantics):
            raise ValueError("A 发现集合错误")
        actions, tool_ids = set(), set()
        for finding in findings:
            if not isinstance(finding, dict):
                raise ValueError("发现类型错误")
            tool = finding.get("tool_id")
            if tool not in semantics or tool in tool_ids:
                raise ValueError("发现工具未登记或重复")
            tool_ids.add(tool)
            predicate, allowed = semantics[tool]
            affected = finding.get("affected_count")
            if (finding.get("source") != "fixed" or finding.get("tool_version") != "0.2.0"
                    or finding.get("column") != column or finding.get("input_sha256") != digest
                    or finding.get("predicate_id") != predicate
                    or not isinstance(finding.get("actions"), list)
                    or set(finding["actions"]) != allowed
                    or type(affected) is not int or not 1 <= affected <= count):
                raise ValueError("发现谓词/动作或范围错误")
            actions.update(allowed)
            if expected_findings[column].get(tool) != affected:
                raise ValueError("发现范围与聚合事实不一致")
        if not isinstance(issue.get("actions"), list) or set(issue["actions"]) != actions:
            raise ValueError("审批动作集合不匹配")
        if tool_ids != set(expected_findings[column]):
            raise ValueError("列发现不完整")
    if seen != {column for column, facts in expected_findings.items() if facts}:
        raise ValueError("A 问题集合不完整，不能视为零问题")
    return profile, issues
