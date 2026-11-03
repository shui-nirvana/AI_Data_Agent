"""
控制平面小型记录；原始 CSV 和生成的代码不保存在状态中。
该模块定义了任务的状态枚举和核心数据记录类。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


# 模式版本，用于数据迁移和兼容性检查
SCHEMA_VERSION = "1.0"
# 问题来源类型：固定工具检测或未来可能支持的动态深度探查
IssueSource = Literal["fixed", "future_dynamic"]


class TaskState(StrEnum):
    """任务生命周期状态枚举"""
    UPLOADED = "UPLOADED"              # 已上传
    QUEUED = "QUEUED"                  # 已进入队列
    PROFILING = "PROFILING"            # 正在进行画像探查
    AWAITING_DECISIONS = "AWAITING_DECISIONS" # 等待用户决策审批
    SPEC_FROZEN = "SPEC_FROZEN"        # 清洗规范已冻结
    GENERATING = "GENERATING"          # 正在生成清洗代码
    EXECUTING = "EXECUTING"            # 正在隔离容器中执行代码
    REPAIRING = "REPAIRING"            # 正在尝试修复代码（自愈）
    VALIDATING = "VALIDATING"          # 正在独立核验结果
    VALIDATED = "VALIDATED"            # 核验通过，尚未发布；不是最终成功
    PUBLISHING = "PUBLISHING"          # 正在发布最终产物
    SUCCEEDED = "SUCCEEDED"            # 任务成功完成
    FAILED = "FAILED"                  # 任务失败
    CANCELLED = "CANCELLED"            # 任务已取消


@dataclass(frozen=True)
class TaskRecord:
    """任务核心记录类"""
    task_id: str          # 唯一任务 ID
    owner_id: str         # 所有者 ID
    goal: str             # 业务目标描述
    state: TaskState      # 当前状态
    input_sha256: str     # 输入文件的 SHA-256 哈希
    input_bytes: int      # 输入文件的大小（字节）
    version: int          # 记录版本号（用于乐观锁或变更追踪）
    created_at: str       # 创建时间（ISO 格式）


@dataclass(frozen=True)
class TaskEvent:
    """任务事件记录类，用于追踪进度历史"""
    sequence: int         # 事件序号
    task_id: str          # 关联的任务 ID
    kind: str             # 事件类型（通常对应状态变更）
    created_at: str       # 事件发生时间
