"""
显式的只读 A 工具注册表。不使用第三方插件发现机制。
该模块定义了在阶段 A（画像探查）中使用的固定检测工具。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    """固定检测工具定义"""
    tool_id: str      # 工具唯一 ID
    version: str      # 工具版本
    description: str  # 工具功能描述
    deterministic: bool = True
    read_only: bool = True
    resource_class: str = "bounded_table"


# 已注册的工具列表
TOOLS = (
    Tool("csv_structure", "0.2.0", "表头、行数和解析器约束检测"),
    Tool("missing_cells", "0.2.0", "每列空字符串计数"),
    Tool("duplicate_rows", "0.2.0", "完全重复行检测（仅检测，不自动处理）"),
    Tool("type_format", "0.2.0", "白名单日期格式计数，不猜测业务类型"),
    Tool("numeric_range", "0.2.0", "严格十进制字面值范围及负数数量（仅提示）"),
)


def registered_tools() -> tuple[Tool, ...]:
    """返回所有已注册的工具"""
    return TOOLS
