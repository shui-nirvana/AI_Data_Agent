"""The control plane validates bounded JSON evidence and hashes; it never parses raw CSV."""

import hashlib
import json
from pathlib import Path

from backend.domain.csv_policy import CSV_POLICY, PARSER_VERSION


def read_json(path: Path, limit: int = 1_000_000):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Evidence must be a regular file")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("JSON evidence exceeds the size limit")
    return json.loads(data)


def file_hash(path: Path, limit: int) -> str:
    """Read bytes to verify content binding without interpreting CSV fields."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("Artifact must be a regular file")
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            size += len(chunk)
            if size > limit:
                raise ValueError("Artifact exceeds the size limit")
            digest.update(chunk)
    return digest.hexdigest()


def validate_profile(profile, issues, digest: str):
    """Accept the fixed scanner version and complete tool set; reject damaged or incomplete approval evidence."""
    if not isinstance(profile, dict) or not isinstance(issues, list):
        raise ValueError("Stage A output has an invalid type")
    if (profile.get("schema_version") != "1.0" or profile.get("scanner_version") != "0.2.0"
            or profile.get("parser_version") != PARSER_VERSION
            or profile.get("status") != "completed" or profile.get("input_sha256") != digest
            or profile.get("limits") != CSV_POLICY.model_dump()):
        raise ValueError("Stage A version, completion status, or input does not match")
    columns = profile.get("columns")
    count = profile.get("row_count")
    if (not isinstance(columns, list) or not 1 <= len(columns) <= CSV_POLICY.max_columns
            or any(not isinstance(c, str) or not c.strip() or len(c) > CSV_POLICY.max_field_chars
                   for c in columns) or len(set(columns)) != len(columns)
            or type(count) is not int or not 0 <= count <= CSV_POLICY.max_rows
            or count * len(columns) > CSV_POLICY.max_cells):
        raise ValueError("Stage A table structure or row count violates the contract")
    expected = {"csv_structure", "missing_cells", "duplicate_rows", "type_format", "numeric_range"}
    runs = profile.get("tool_runs", [])
    if (not isinstance(runs, list) or len(runs) != len(expected)
            or any(not isinstance(r, dict) or r.get("status") != "completed"
                   or r.get("version") != "0.2.0" or r.get("input_sha256") != digest for r in runs)
            or {r.get("tool_id") for r in runs} != expected):
        raise ValueError("Not all stage A tools completed")
    aggregates = {}
    for key in ("missing_by_column", "date_formats_by_column", "numeric_by_column"):
        mapping = profile.get(key)
        if not isinstance(mapping, dict) or set(mapping) != set(columns):
            raise ValueError("Stage A aggregate fields are incomplete")
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
            raise ValueError("Stage A aggregate values violate the contract")
        for key in ("min", "max"):
            value = numeric.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 130):
                raise ValueError("Stage A numeric range violates the contract")
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
        raise ValueError("Stage A issue count exceeds the column count")
    for issue in issues:
        if not isinstance(issue, dict):
            raise ValueError("Issue has an invalid type")
        column = issue.get("column")
        if (column not in columns or column in seen or issue.get("source") != "fixed"
                or issue.get("input_sha256") != digest):
            raise ValueError("Stage A issue source or column violates the contract")
        seen.add(column)
        identity = json.dumps([digest, "0.2.0", column], ensure_ascii=False)
        if issue.get("issue_id") != hashlib.sha256(identity.encode("utf-8")).hexdigest():
            raise ValueError("Stage A issue ID does not match")
        findings = issue.get("findings")
        if not isinstance(findings, list) or not 1 <= len(findings) <= len(semantics):
            raise ValueError("Stage A findings are invalid")
        actions, tool_ids = set(), set()
        for finding in findings:
            if not isinstance(finding, dict):
                raise ValueError("Finding has an invalid type")
            tool = finding.get("tool_id")
            if tool not in semantics or tool in tool_ids:
                raise ValueError("Finding tool is unregistered or duplicated")
            tool_ids.add(tool)
            predicate, allowed = semantics[tool]
            affected = finding.get("affected_count")
            if (finding.get("source") != "fixed" or finding.get("tool_version") != "0.2.0"
                    or finding.get("column") != column or finding.get("input_sha256") != digest
                    or finding.get("predicate_id") != predicate
                    or not isinstance(finding.get("actions"), list)
                    or set(finding["actions"]) != allowed
                    or type(affected) is not int or not 1 <= affected <= count):
                raise ValueError("Finding predicate, actions, or range is invalid")
            actions.update(allowed)
            if expected_findings[column].get(tool) != affected:
                raise ValueError("Finding range does not match aggregate facts")
        if not isinstance(issue.get("actions"), list) or set(issue["actions"]) != actions:
            raise ValueError("Approval action set does not match")
        if tool_ids != set(expected_findings[column]):
            raise ValueError("Column findings are incomplete")
    if seen != {column for column, facts in expected_findings.items() if facts}:
        raise ValueError("Stage A issues are incomplete and cannot be treated as zero issues")
    return profile, issues
