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
    Tool("csv_structure", "0.2.0", "Check headers, row counts and parser constraints"),
    Tool("missing_cells", "0.2.0", "Count empty strings per column"),
    Tool("duplicate_rows", "0.2.0", "Detect identical rows without modifying them"),
    Tool("type_format", "0.2.0", "Count allowed date formats without guessing business types"),
    Tool("numeric_range", "0.2.0", "Report strict decimal ranges and negative counts without modifying data"),
)


def registered_tools() -> tuple[Tool, ...]:
    """Return all registered tools."""
    return TOOLS
