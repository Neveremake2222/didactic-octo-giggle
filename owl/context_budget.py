"""Token 预算、裁剪策略与层级优先级。

budget 和 builder 的区别：
  - budget 只负责"在给定的字符预算内，决定保留多少、裁剪多少"
  - builder 负责"把多个上下文来源组装成最终的 prompt 文本"

裁剪策略：
  1. 先裁 low-priority 层（ON_DEMAND）
  2. 再裁 medium 层（RUNTIME）
  3. 最后裁 COMPACTED
  4. RESIDENT 和 SYSTEM 不裁剪
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# 字符级预算（估算：1 token ≈ 4 chars）
DEFAULT_TOTAL_BUDGET = 12000

# 各 section 的默认预算
DEFAULT_SECTION_BUDGETS: dict[str, int] = {
    "prefix": 3600,
    "memory": 1600,
    "relevant_memory": 1200,
    "history": 5200,
}

# 各 section 的最低保障（floor，裁到此处停止）
DEFAULT_SECTION_FLOORS: dict[str, int] = {
    "prefix": 1200,
    "memory": 400,
    "relevant_memory": 300,
    "history": 1500,
}

# 超预算时的裁剪顺序（先裁 lower-priority sections）
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix")


def _tail_clip(text: str, limit: int) -> str:
    """从文本尾部截断到指定字符数。"""
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


@dataclass
class BudgetConfig:
    """一轮上下文的预算配置。

    包含总预算、各 section 预算、floor 保障，以及裁剪顺序。
    """

    total: int = DEFAULT_TOTAL_BUDGET
    sections: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SECTION_BUDGETS))
    floors: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SECTION_FLOORS))
    reduction_order: tuple[str, ...] = DEFAULT_REDUCTION_ORDER

    def section_budget(self, section: str) -> int:
        return self.sections.get(section, 0)

    def section_floor(self, section: str) -> int:
        return self.floors.get(section, max(20, self.sections.get(section, 0) // 4))

    def apply_overflow_reduction(
        self,
        budgets: dict[str, int],
        overflow: int,
    ) -> tuple[dict[str, int], list[dict[str, Any]]]:
        """应用溢出裁剪，返回（更新后的 budgets, reduction_log）。

        按 reduction_order 逐个 section 裁剪，直到总字符数不超过 total 或无
        可裁剪 section 为止。
        """
        budgets = dict(budgets)
        reduction_log: list[dict[str, Any]] = []
        remaining_overflow = overflow

        for section in self.reduction_order:
            if remaining_overflow <= 0:
                break
            floor = self.section_floor(section)
            current = budgets.get(section, 0)
            if current <= floor:
                continue

            new_budget = max(floor, current - remaining_overflow)
            if new_budget < current:
                actual_reduction = current - new_budget
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current,
                        "after_chars": new_budget,
                        "overflow_absorbed": actual_reduction,
                    }
                )
                budgets[section] = new_budget
                remaining_overflow -= actual_reduction

        return budgets, reduction_log

    def compute_floors(self) -> dict[str, int]:
        """基于 section budgets 计算默认值 floors。"""
        return {
            section: max(20, budget // 4)
            for section, budget in self.sections.items()
        }


class ContextBudget:
    """Token 预算管理器。

    职责：
    - 按优先级裁剪各 section
    - 保证总字符数不超过 total budget
    - 记录裁剪日志（哪些 section 被裁了多少）
    """

    def __init__(
        self,
        total: int = DEFAULT_TOTAL_BUDGET,
        section_budgets: dict[str, int] | None = None,
        section_floors: dict[str, int] | None = None,
        reduction_order: tuple[str, ...] | None = None,
    ):
        self.config = BudgetConfig(
            total=total,
            sections=dict(DEFAULT_SECTION_BUDGETS),
            floors=dict(DEFAULT_SECTION_FLOORS),
            reduction_order=DEFAULT_REDUCTION_ORDER,
        )
        if section_budgets:
            self.config.sections.update({str(k): int(v) for k, v in section_budgets.items()})
        if section_floors:
            self.config.floors.update({str(k): int(v) for k, v in section_floors.items()})
        if reduction_order:
            self.config.reduction_order = tuple(reduction_order)

    def budget_for(self, section: str) -> int:
        return self.config.section_budget(section)

    def floor_for(self, section: str) -> int:
        return self.config.section_floor(section)

    def apply_reduction(
        self,
        section_texts: dict[str, str],
        selected_notes: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """在给定的 section texts 上应用预算约束，返回（裁剪后的 texts, log）。

        核心逻辑：
        1. 计算实际文本总字符数 vs total budget，得到真实溢出量
        2. 按 reduction_order 逐个 section 裁剪，直到不超预算
        3. 按最终 budgets 对各 section 文本进行截断
        """
        section_texts = dict(section_texts)
        reduction_log: list[dict[str, Any]] = []

        # 计算实际文本总字符数
        total_chars = sum(len(text) for text in section_texts.values())
        overflow = max(0, total_chars - self.config.total)

        if overflow <= 0:
            return section_texts, reduction_log

        # 按 reduction_order 计算各 section 的最终预算
        # 目标是：reduce 后所有 section 的总字符数不超过 total
        budgets = dict(self.config.sections)

        for section in self.config.reduction_order:
            floor = self.config.section_floor(section)
            current_budget = budgets.get(section, 0)
            if current_budget <= floor:
                continue
            # 按比例裁剪，但不低于 floor
            available = current_budget - floor
            # 只裁减当前溢出量的比例（每 section 均摊）
            reduction = min(available, overflow)
            new_budget = current_budget - reduction
            new_budget = max(floor, new_budget)
            if new_budget < current_budget:
                actual_reduction = current_budget - new_budget
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_absorbed": actual_reduction,
                    }
                )
                budgets[section] = new_budget
                overflow -= actual_reduction
            if overflow <= 0:
                break

        # 应用裁剪到各 section 文本
        for section, budget in budgets.items():
            if section in section_texts:
                section_texts[section] = _tail_clip(section_texts[section], budget)

        return section_texts, reduction_log

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.config.total,
            "sections": dict(self.config.sections),
            "floors": dict(self.config.floors),
            "reduction_order": list(self.config.reduction_order),
        }
