"""Versioned CSV contract. Define configuration only; never import or run a CSV parser in the control plane."""

from pydantic import BaseModel, ConfigDict

PARSER_VERSION = "csv-utf8-v1"
DATE_FORMATS = ("YYYY-MM-DD", "YYYY/MM/DD", "DD-MM-YYYY")


class CsvPolicy(BaseModel):
    """Reject duplicate or empty headers and malformed records. Only an empty string is null; spaces and NA are not."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str = PARSER_VERSION
    max_bytes: int = 10 * 1024 * 1024
    max_rows: int = 100_000
    max_columns: int = 100
    max_field_chars: int = 32_768
    max_cells: int = 1_000_000


CSV_POLICY = CsvPolicy()
