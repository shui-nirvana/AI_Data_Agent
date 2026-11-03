"""
服务端分配的私有路径管理。目前特意未实现发布功能。
该模块负责管理输入文件和临时文件的存储路径，确保文件访问的安全性和原子性。
"""

import os
from pathlib import Path
from uuid import UUID


class InputFiles:
    """输入文件管理器"""
    def __init__(self, root: Path) -> None:
        """
        初始化管理目录。
        
        参数:
            root: 存储输入文件的根目录。
        """
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        # 在非 Windows 系统上限制目录访问权限
        if os.name != "nt":
            self.root.chmod(0o700)

    def input_path(self, task_id: str) -> Path:
        """
        获取任务的正式输入 CSV 文件路径。
        通过 UUID 规范化防止路径穿越攻击。
        """
        return self.root / f"{UUID(task_id)}.csv"

    def temporary_path(self, task_id: str) -> Path:
        """
        获取任务的上传临时文件路径（.part）。
        """
        return self.root / f"{UUID(task_id)}.part"

    def publish(self, *_args: object, **_kwargs: object) -> None:
        """
        发布产物。
        注意：只有经过 D 验证且绑定哈希的文件才允许发布。
        """
        raise NotImplementedError("只有经过 D 验证且绑定哈希的文件才允许发布")
