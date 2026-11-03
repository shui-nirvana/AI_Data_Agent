"""
不透明的作业引用；客户端和 LLM 绝不能直接设置 Docker 参数。
该模块定义了隔离执行作业的请求结构及其校验逻辑，确保执行过程的可追溯性和安全性。
"""

from dataclasses import dataclass
from enum import StrEnum
import re
from uuid import UUID


class Stage(StrEnum):
    """作业执行阶段枚举"""
    PROFILE_A = "PROFILE_A" # 阶段 A：固定工具画像探查
    CLEAN_C = "CLEAN_C"     # 阶段 C：执行 LLM 生成的清洗代码
    VERIFY_D = "VERIFY_D"   # 阶段 D：独立结果验证


@dataclass(frozen=True)
class JobRequest:
    """发送给 worker 的作业请求指令"""
    task_id: str          # 任务 ID
    attempt_id: str       # 尝试 ID（用于自愈重试的区分）
    stage: Stage          # 执行阶段
    input_sha256: str     # 输入文件哈希
    spec_sha256: str | None = None      # 冻结规范的哈希（C 和 D 阶段必需）
    code_sha256: str | None = None      # 清洗代码的哈希（C 阶段必需）
    candidate_sha256: str | None = None # 候选产物的哈希（D 阶段必需）

    def __post_init__(self) -> None:
        """验证请求的合法性和完整性"""
        UUID(self.task_id)
        UUID(self.attempt_id)
        if not isinstance(self.stage, Stage):
            raise ValueError("未知的作业阶段")
        
        # 长度不足以证明是哈希；拒绝非十六进制引用与无效的可选摘要。
        if not re.fullmatch(r"[0-9a-f]{64}", self.input_sha256):
            raise ValueError("必须提供有效的输入文件 SHA-256 哈希")
        for digest in (self.spec_sha256, self.code_sha256, self.candidate_sha256):
            if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("作业摘要必须为 SHA-256")
            
        # 根据不同阶段检查必需参数
        if self.stage != Stage.PROFILE_A and not self.spec_sha256:
            raise ValueError("C 和 D 阶段需要已冻结的规范哈希")
            
        if self.stage == Stage.CLEAN_C and not self.code_sha256:
            raise ValueError("C 阶段需要记录源代码哈希")
            
        if self.stage == Stage.VERIFY_D and not self.candidate_sha256:
            raise ValueError("D 阶段必须绑定特定的候选产物哈希")
