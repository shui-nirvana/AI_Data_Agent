"""版本化 CSV 契约。只定义配置，不在控制层导入或运行 CSV 解析器。"""

from pydantic import BaseModel, ConfigDict

PARSER_VERSION = "csv-utf8-v1"
DATE_FORMATS = ("YYYY-MM-DD", "YYYY/MM/DD", "DD-MM-YYYY")


class CsvPolicy(BaseModel):
    """拒绝重复/空表头、畸形记录；空值仅指空字符串，不包含空格或 NA。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str = PARSER_VERSION
    max_bytes: int = 10 * 1024 * 1024
    max_rows: int = 100_000
    max_columns: int = 100
    max_field_chars: int = 32_768
    max_cells: int = 1_000_000


CSV_POLICY = CsvPolicy()
