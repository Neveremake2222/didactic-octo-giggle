"""五层上下文模型的定义与数据结构。

上下文和记忆的区别：
  - 记忆是仓库：系统持有、未来可能被召回的东西
  - 上下文是装配线：这一轮实际送进模型的东西

五层模型：
  1. RESIDENT   — 常驻层：system prompt、核心规则、工具定义（每轮都有，不裁剪）
  2. ON_DEMAND  — 按需层：只在相关时加载的说明文件、额外规则
  3. RUNTIME    — 运行时写入层：当前 plan、最近工具结果、中间假设
  4. COMPACTED  — 整合写入层：压缩后的文件摘要、任务总结、可复用结论
  5. SYSTEM     — 系统层：hooks、tool policy、路径约束（不一定进 prompt 但影响行为）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ContextLayer(Enum):
    """上下文五层枚举。"""
    RESIDENT = "resident"
    ON_DEMAND = "on_demand"
    RUNTIME = "runtime"
    COMPACTED = "compacted"
    SYSTEM = "system"


# 每层的默认优先级，数字越大越不容易被裁剪。
# RESIDENT 优先级最高，ON_DEMAND 最低。
DEFAULT_LAYER_PRIORITY = {
    ContextLayer.RESIDENT: 100,
    ContextLayer.SYSTEM: 90,
    ContextLayer.COMPACTED: 70,
    ContextLayer.RUNTIME: 50,
    ContextLayer.ON_DEMAND: 30,
}

# 哪些层允许被裁剪（RESIDENT 和 SYSTEM 默认不裁剪）。
TRIMMABLE_LAYERS = {
    ContextLayer.ON_DEMAND: True,
    ContextLayer.RUNTIME: True,
    ContextLayer.COMPACTED: True,
    ContextLayer.RESIDENT: False,
    ContextLayer.SYSTEM: False,
}


@dataclass
class ContextItem:
    """上下文中的一个条目。

    每个 ContextItem 描述一段待组装进 prompt 的内容，以及它的元信息。
    """

    # 内容文本
    content: str

    # 属于哪一层
    layer: ContextLayer

    # 该条目的标识名称（如 "system_prompt"、"file_summary:README.md"）
    name: str

    # 优先级：数字越大越不容易被裁剪
    priority: int = 0

    # 来源描述（如 "workspace.py"、"memory.render_memory_text()"）
    source: str = ""

    # 是否允许被裁剪（None 时按层的默认值决定）
    trimmable: bool | None = None

    # 该条目在原始上下文中的字符数
    raw_chars: int = 0

    # 裁剪后的字符数（由 builder 填写）
    rendered_chars: int = 0

    # 是否被裁剪了
    was_trimmed: bool = False

    # 额外元数据
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.raw_chars = len(self.content)
        if self.trimmable is None:
            self.trimmable = TRIMMABLE_LAYERS.get(self.layer, True)
        if self.priority == 0:
            self.priority = DEFAULT_LAYER_PRIORITY.get(self.layer, 50)

    def is_trimmable(self) -> bool:
        """该条目是否允许被裁剪。"""
        return bool(self.trimmable)


@dataclass
class ContextBundle:
    """一轮上下文的完整集合。

    由多个 ContextItem 组成，表示这一轮 prompt 的所有候选内容。
    builder 会根据 budget 决定保留哪些、裁剪哪些。
    """

    items: list[ContextItem] = field(default_factory=list)

    # 本轮用户请求（特殊处理，不参与裁剪）
    current_request: str = ""

    # 本轮 user_request 的来源描述
    current_request_source: str = "user"

    def add(self, item: ContextItem) -> ContextItem:
        """添加一个条目并返回它。"""
        self.items.append(item)
        return item

    def by_layer(self, layer: ContextLayer) -> list[ContextItem]:
        """按层筛选条目。"""
        return [item for item in self.items if item.layer == layer]

    def sorted_by_priority(self, trimmable_only: bool = False) -> list[ContextItem]:
        """按优先级排序（高优先级在前）。"""
        items = self.items
        if trimmable_only:
            items = [item for item in items if item.is_trimmable()]
        return sorted(items, key=lambda item: item.priority, reverse=True)

    def total_chars(self) -> int:
        """所有条目的总字符数。"""
        return sum(len(item.content) for item in self.items) + len(self.current_request)

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [
                {
                    "name": item.name,
                    "layer": item.layer.value,
                    "priority": item.priority,
                    "source": item.source,
                    "raw_chars": item.raw_chars,
                    "rendered_chars": item.rendered_chars,
                    "was_trimmed": item.was_trimmed,
                    "trimmable": item.is_trimmable(),
                }
                for item in self.items
            ],
            "current_request_chars": len(self.current_request),
            "total_chars": self.total_chars(),
        }


def classify_existing_section(section_name: str) -> ContextLayer:
    """把现有 context_manager 的 section name 映射到五层模型。

    现有 section:
      - prefix            → RESIDENT（system prompt、工具规则）
      - memory            → COMPACTED（工作记忆、文件摘要）
      - relevant_memory   → COMPACTED（相关记忆召回）
      - history           → RUNTIME（对话历史）
      - current_request   → RUNTIME（当前请求）
    """
    mapping = {
        "prefix": ContextLayer.RESIDENT,
        "memory": ContextLayer.COMPACTED,
        "relevant_memory": ContextLayer.COMPACTED,
        "history": ContextLayer.RUNTIME,
        "current_request": ContextLayer.RUNTIME,
    }
    return mapping.get(section_name, ContextLayer.RUNTIME)
