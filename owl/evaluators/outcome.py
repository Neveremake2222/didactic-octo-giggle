"""结果正确性评估。

回答：最终产物是否正确？目标文件是否改对？stop_reason 是否合理？
如果指定了 expected_stop_reason，则评估该停止原因是否匹配。

评分规则：
  - task_success=True → score=1.0
  - expected_failure 模式：stop_reason 匹配 expected_stop_reason → score=1.0，否则 score=0.0
  - task_success=False 但 stop_reason 合理 → score=0.5
  - task_success=False 且 stop_reason 不合理 → score=0.0
"""

from __future__ import annotations

from typing import Any


class OutcomeEvaluator:
    """结果评估器。"""

    def evaluate(
        self,
        trace_events: list[dict[str, Any]],
        task_state: dict[str, Any],
        metrics: dict[str, Any],
        task_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """评估结果正确性。

        Parameters
        ----------
        task_config: 可选，任务配置字典，用于获取 expected_stop_reason 等字段。
        """
        status = str(task_state.get("status", ""))
        stop_reason = str(task_state.get("stop_reason", ""))
        tool_steps = int(task_state.get("tool_steps", 0))
        attempts = int(task_state.get("attempts", 0))

        task_success = status == "completed" and stop_reason == "final_answer_returned"

        # 预期的故障模式（expected_failure）：验证 stop_reason 是否符合预期
        expected_stop_reason = None
        if task_config:
            expected_stop_reason = task_config.get("expected_stop_reason")

        if expected_stop_reason is not None:
            match = stop_reason == expected_stop_reason
            return {
                "score": 1.0 if match else 0.0,
                "details": {
                    "task_success": False,
                    "expected_failure": True,
                    "expected_stop_reason": expected_stop_reason,
                    "actual_stop_reason": stop_reason,
                    "stop_reason_match": match,
                    "tool_steps": tool_steps,
                    "attempts": attempts,
                },
            }

        # 计算 score（原始逻辑）
        if task_success:
            score = 1.0
        elif stop_reason in ("step_limit_reached", "retry_limit_reached", "model_error"):
            # 至少知道为什么失败
            score = 0.3
        elif status == "stopped":
            score = 0.2
        else:
            score = 0.0

        return {
            "score": score,
            "details": {
                "task_success": task_success,
                "status": status,
                "stop_reason": stop_reason,
                "tool_steps": tool_steps,
                "attempts": attempts,
            },
        }
