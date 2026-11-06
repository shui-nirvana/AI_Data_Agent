"""Explicit read-only registry of fixed A profiling tools, without third-party plugin discovery.


"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    """Fixed detection tool definition."""
    tool_id: str      # Unique tool ID.
    version: str      # Tool version.
    description: str  # Tool description.
    deterministic: bool = True
    read_only: bool = True
    resource_class: str = "bounded_table"


# Registered tools.
TOOLS = (
    Tool("csv_structure", "0.2.0", "表头、行数和解析器约束检测"),
    Tool("missing_cells", "0.2.0", "每列空字符串计数"),
    Tool("duplicate_rows", "0.2.0", "完全重复行检测（仅检测，不自动处理）"),
    Tool("type_format", "0.2.0", "白名单日期格式计数，不猜测业务类型"),
    Tool("numeric_range", "0.2.0", "严格十进制字面值范围及负数数量（仅提示）"),
)


def registered_tools() -> tuple[Tool, ...]:
    """Return all registered tools."""
    return TOOLS
