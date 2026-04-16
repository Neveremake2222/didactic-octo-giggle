"""局部上下文来源数据类型。

定义 Agent 在工作过程中可能发现并注入的上下文来源：
  - AGENTS.md / README.md / CONTRIBUTING.md（项目文档）
  - 各类规则文件（.owl, .claude, CLAUDE.md 等）

这些来源由 ContextDiscovery 发现，经 ContextInjectedTracker 去重后注入 prompt。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 优先级最高的项目文档文件名
CANONICAL_DOC_FILES = ("AGENTS.md", "README.md", "CONTRIBUTING.md")

# 规则文件名（不区分大小写）
RULE_FILE_NAMES = (
    ".owl",
    ".claude",
    "CLAUDE.md",
)

# 规则目录
RULE_DIRS = (
    "docs/rules",
    "docs/guidelines",
    ".github/AGENTS.md",
)


@dataclass
class ContextSource:
    """一次上下文来源发现的结果。

    Attributes:
        source_id:        repo 内相对路径（稳定身份，同一文件多次发现应相同）
        absolute_path:     磁盘全路径
        discovered_from:   触发发现的源文件路径
        content:          发现时的文件内容
        header:           首行非注释文字（用于 prompt 摘要）
        fingerprint:      SHA-256(content)，用于判断内容是否变化
        category:         来源类型
    """

    source_id: str           # repo 相对路径
    absolute_path: str       # 磁盘全路径
    discovered_from: str      # 触发发现的文件
    content: str            # 全文
    header: str = ""        # 首行摘要
    fingerprint: str = ""   # SHA-256(content)
    category: str = "other"  # "AGENTS.md" | "README.md" | "CONTRIBUTING.md" | "rule_file" | "other"

    def is_stale(self, current_content: str) -> bool:
        """判断内容是否已过期（与发现时相比发生变化）。"""
        import hashlib
        current_fp = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
        return current_fp != self.fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "absolute_path": self.absolute_path,
            "discovered_from": self.discovered_from,
            "content": self.content,
            "header": self.header,
            "fingerprint": self.fingerprint,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextSource:
        return cls(
            source_id=str(data.get("source_id", "")),
            absolute_path=str(data.get("absolute_path", "")),
            discovered_from=str(data.get("discovered_from", "")),
            content=str(data.get("content", "")),
            header=str(data.get("header", "")),
            fingerprint=str(data.get("fingerprint", "")),
            category=str(data.get("category", "other")),
        )
