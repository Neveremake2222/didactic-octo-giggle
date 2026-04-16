"""过程质量评估。

回答：
  - 有无无效重复调用？
  - 是否出现无进展循环？
  - 工具调用序列是否合理？
  - 是否过早宣称完成？

评分规则（0-1）：
  - 无任何过程问题 → 1.0
  - 轻微重复（1-2 次）→ 0.8
  - 中等问题（重复 > 2 或有循环）→ 0.5
  - 严重问题（过早宣称完成）→ 0.2
"""

from __future__ import annotations

from typing import Any


class ProcessEvaluator:
    """过程质量评估器。"""

    def evaluate(
        self,
        trace_events: list[dict[str, Any]],
        task_state: dict[str, Any],
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        """评估过程质量。"""
        process = metrics.get("process", {})

        repeated = process.get("repeated_identical_call_count", 0)
        no_progress = process.get("no_progress_loop_count", 0)
        blocked = process.get("blocked_tool_call_count", 0)
        failed_calls = process.get("failed_tool_call_count", 0)

        # 检测"过早宣称完成"：最后一步之前没有任何工具调用
        tool_events = [e for e in trace_events if (e.get("event_name") or e.get("event")) == "tool_executed"]
        premature_done = len(tool_events) == 0

        issues: list[str] = []
        if repeated > 2:
            issues.append(f"repeated_calls({repeated})")
        if no_progress > 0:
            issues.append(f"no_progress_loops({no_progress})")
        if blocked > 0:
            issues.append(f"blocked_tools({blocked})")
        if failed_calls > 2:
            issues.append(f"failed_calls({failed_calls})")
        if premature_done:
            issues.append("premature_done")

        # 计算 score
        if not issues:
            score = 1.0
        elif premature_done or repeated > 4:
            score = 0.2
        elif repeated > 2 or no_progress > 0:
            score = 0.5
        else:
            score = 0.8

        return {
            "score": score,
            "details": {
                "repeated_identical_call_count": repeated,
                "no_progress_loop_count": no_progress,
                "blocked_tool_call_count": blocked,
                "failed_tool_call_count": failed_calls,
                "premature_done": premature_done,
                "issues": issues,
            },
        }
