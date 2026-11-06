"""Bounded literal CSV reading for A/D containers only. Byte, row, column, and cell limits do not replace container memory limits.


"""

import csv
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from backend.domain.csv_policy import CSV_POLICY, CsvPolicy


@dataclass(frozen=True)
class CsvTable:
    sha256: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


def read_csv(source: Path, policy: CsvPolicy = CSV_POLICY) -> CsvTable:
    """Read UTF-8 with an optional BOM, comma delimiter, and standard quoting; never infer types or skip malformed rows."""
    with source.open("rb") as stream:
        content = stream.read(policy.max_bytes + 1)
    if len(content) > policy.max_bytes:
        raise ValueError("CSV 超过字节限制")
    text = content.decode("utf-8-sig", errors="strict")
    old_limit = csv.field_size_limit()
    csv.field_size_limit(policy.max_field_chars)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader, None)
        if not header or len(header) > policy.max_columns or len(set(header)) != len(header):
            raise ValueError("CSV 表头缺失、重复或超过列限制")
        if any(not item.strip() or "\x00" in item for item in header):
            raise ValueError("CSV 表头包含空名称或 NUL")
        rows = []
        for row in reader:
            if len(row) != len(header) or any("\x00" in value for value in row):
                raise ValueError("CSV 记录列数不匹配或包含 NUL")
            rows.append(tuple(row))
            if len(rows) > policy.max_rows or len(rows) * len(header) > policy.max_cells:
                raise ValueError("CSV 超过行数或总单元格限制")
        return CsvTable(hashlib.sha256(content).hexdigest(), tuple(header), tuple(rows))
    finally:
        csv.field_size_limit(old_limit)
