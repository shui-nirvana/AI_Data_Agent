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
        raise ValueError("CSV exceeds the byte limit")
    text = content.decode("utf-8-sig", errors="strict")
    old_limit = csv.field_size_limit()
    csv.field_size_limit(policy.max_field_chars)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader, None)
        if not header or len(header) > policy.max_columns or len(set(header)) != len(header):
            raise ValueError("CSV header is missing, contains duplicates, or exceeds the column limit")
        if any(not item.strip() or "\x00" in item for item in header):
            raise ValueError("CSV header contains an empty name or NUL")
        rows = []
        for row in reader:
            if len(row) != len(header) or any("\x00" in value for value in row):
                raise ValueError("CSV record has the wrong number of columns or contains NUL")
            rows.append(tuple(row))
            if len(rows) > policy.max_rows or len(rows) * len(header) > policy.max_cells:
                raise ValueError("CSV exceeds the row or total cell limit")
        return CsvTable(hashlib.sha256(content).hexdigest(), tuple(header), tuple(rows))
    finally:
        csv.field_size_limit(old_limit)
