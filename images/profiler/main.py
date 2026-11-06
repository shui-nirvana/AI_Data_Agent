"""Restricted A profiler entrypoint for isolated CSV parsing; do not run inside FastAPI or treat this development entrypoint as a certified production sandbox.



"""

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from backend.domain.csv_policy import CSV_POLICY, PARSER_VERSION
from images.csv_input import read_csv
from images.profiler.registry import registered_tools

# CSV_POLICY supplies parsing limits; the container runtime must still enforce resource limits.


def scan(source: Path) -> tuple[dict, list[dict]]:
    """Scan a CSV source and return its profile and findings.




    """
    table = read_csv(source)
    header = table.columns
    missing = [0] * len(header)
    dates = [Counter() for _ in header]
    numeric = [{"count": 0, "negative_count": 0, "min": None, "max": None} for _ in header]
    seen: Counter[tuple[str, ...]] = Counter()
    duplicates = 0
    formats = {
        "YYYY-MM-DD": (r"[0-9]{4}-[0-9]{2}-[0-9]{2}", "%Y-%m-%d"),
        "YYYY/MM/DD": (r"[0-9]{4}/[0-9]{2}/[0-9]{2}", "%Y/%m/%d"),
        "DD-MM-YYYY": (r"[0-9]{2}-[0-9]{2}-[0-9]{4}", "%d-%m-%Y"),
    }
    for row in table.rows:
        duplicates += seen[row] > 0
        seen[row] += 1
        for index, value in enumerate(row):
            missing[index] += value == ""
            for name, (pattern, parser_format) in formats.items():
                if re.fullmatch(pattern, value):
                    try:
                        datetime.strptime(value, parser_format)
                    except ValueError:
                        continue
                    dates[index][name] += 1
                    break
            # Do not parse scientific notation, NaN/Infinity, currency, or thousands separators, or guess formats.
            if len(value) <= 128 and re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", value):
                number = Decimal(value)
                stats = numeric[index]
                stats["count"] += 1
                stats["negative_count"] += number < 0
                stats["min"] = number if stats["min"] is None else min(stats["min"], number)
                stats["max"] = number if stats["max"] is None else max(stats["max"], number)
    for stats in numeric:
        for key in ("min", "max"):
            stats[key] = str(stats[key]) if stats[key] is not None else None

    profile = {
        "schema_version": "1.0",
        "scanner_version": "0.2.0",
        "parser_version": PARSER_VERSION,
        "limits": CSV_POLICY.model_dump(),
        "input_sha256": table.sha256,
        "row_count": len(table.rows),
        "columns": list(header),
        "missing_by_column": dict(zip(header, missing)),
        "date_formats_by_column": {name: dict(counts) for name, counts in zip(header, dates)},
        "numeric_by_column": dict(zip(header, numeric)),
        "duplicate_rows": duplicates,
        "tool_runs": [
            {"tool_id": tool.tool_id, "version": tool.version, "status": "completed",
             "input_sha256": table.sha256}
            for tool in registered_tools()
        ],
    }
    
    issues = []
    def issue(tool_id, column, count, predicate, actions):
        identity = json.dumps([table.sha256, "0.2.0", tool_id, column], ensure_ascii=False)
        issues.append({
            "issue_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            "source": "fixed", "tool_id": tool_id, "tool_version": "0.2.0",
            "input_sha256": table.sha256, "column": column, "affected_count": count,
            "predicate_id": predicate, "actions": actions,
        })
    # Record findings by tool, then merge by column; the MVP permits one action per column.
    for index, name in enumerate(header):
        if missing[index]:
            issue("missing_cells", name, missing[index], "empty_string",
                  ["preserve", "fill_null_with_literal"])
        count = sum(n for format_name, n in dates[index].items() if format_name != "YYYY-MM-DD")
        if count:
            issue("type_format", name, count, "parseable_nonstandard_date",
                  ["preserve", "normalize_date"])
        if numeric[index]["negative_count"]:
            issue("numeric_range", name, numeric[index]["negative_count"], "negative_number",
                  ["preserve"])
    # Duplicate rows are dataset-level notices, not column approval items.
    profile["observations"] = [{"kind": "duplicate_rows", "affected_count": duplicates,
                               "actionable": False}] if duplicates else []
    cards = []
    for name in header:
        findings = [finding for finding in issues if finding["column"] == name]
        if findings:
            identity = json.dumps([table.sha256, "0.2.0", name], ensure_ascii=False)
            cards.append({
                "issue_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                "source": "fixed", "column": name, "input_sha256": table.sha256,
                "findings": findings,
                "actions": sorted({action for finding in findings for action in finding["actions"]}),
            })
    return profile, cards


def main() -> None:
    """In-container main entrypoint."""
    # Fixed in-container paths.
    target = Path("/output")
    target.mkdir(parents=True, exist_ok=True)
    try:
        profile, issues = scan(Path("/input/data.csv"))
    except (ValueError, csv.Error, OSError) as exc:
        profile = {"schema_version": "1.0", "scanner_version": "0.2.0",
                   "status": "failed", "error_code": "INVALID_OR_LIMITED_CSV",
                   "error_type": type(exc).__name__}
        (target / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
        raise SystemExit(1)
    profile["status"] = "completed"
    (target / "profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    (target / "issues.json").write_text(json.dumps(issues, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
