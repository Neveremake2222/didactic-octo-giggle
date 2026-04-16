"""效率评估。

回答：
  - 总耗时多少？
  - 工具平均耗时？
  - 上下文构建了多少次？

评分规则（0-1）：
  - 效率正常 → 1.0
  - 略慢（工具调用 > 10 或 runtime > 30s）→ 0.7
  - 明显低效 → 0.4
"""

from __future__ import annotations

from typing import Any


class EfficiencyEvaluator:
    """效率评估器。"""

    def evaluate(
        self,
        trace_events: list[dict[str, Any]],
        task_state: dict[str, Any],
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        """评估运行效率。"""
        efficiency = metrics.get("efficiency", {})
        process = metrics.get("process", {})

        total_runtime_ms = efficiency.get("total_runtime_ms", 0)
        avg_tool_ms = efficiency.get("avg_tool_ms", 0)
        tool_count = process.get("tool_call_count", 0)
        context_built_count = efficiency.get("context_built_count", 0)

        # 计算 score
        score = 1.0
        if tool_count > 10:
            score -= 0.2
        if total_runtime_ms > 30000:
            score -= 0.2
        if context_built_count > 5:
            score -= 0.1
        score = max(0.0, score)

        return {
            "score": score,
            "details": {
                "total_runtime_ms": total_runtime_ms,
                "avg_tool_ms": avg_tool_ms,
                "tool_call_count": tool_count,
                "context_built_count": context_built_count,
            },
        }
