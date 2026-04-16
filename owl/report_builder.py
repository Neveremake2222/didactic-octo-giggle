"""从 trace + task_state + metrics 自动生成结构化 report。

report 不是原始事件流，而是面向"阅读"和"汇总"的产物。
它回答：这次 run 结果是什么？中间发生了什么？失败原因是什么？

report 尽量从 trace + task_state 自动生成，而不是靠手工拼。
"""

from __future__ import annotations

from typing import Any

from .trace_schema import (
    EVENT_RUN_STARTED,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_FAILED,
    EVENT_TOOL_EXECUTED,
    EVENT_SECURITY_EVENT,
    EVENT_MODEL_REQUESTED,
    EVENT_CONTEXT_BUILT,
)


def build_report(
    task_state: dict[str, Any],
    trace_events: list[dict[str, Any]],
    metrics: dict[str, Any],
    failure_category: str | None = None,
) -> dict[str, Any]:
    """从 trace + task_state + metrics 生成结构化 report。

    Parameters
    ----------
    task_state: 任务状态字典（来自 task_state.json）
    trace_events: trace.jsonl 解析后的字典列表
    metrics: compute_metrics() 产出的指标字典
    failure_category: 可选，由 failure_analyzer 提供的失败分类

    Returns
    -------
    结构化 report，包含：
      - run 基本信息
      - outcome_summary（最终结果）
      - process_summary（关键工具调用概览）
      - efficiency_summary（耗时、token 等）
      - safety_summary（安全事件）
      - metrics（四类指标嵌入）
      - failure_category（如果有）
    """
    run_id = str(task_state.get("run_id", ""))
    task_id = str(task_state.get("task_id", ""))
    status = str(task_state.get("status", ""))
    stop_reason = str(task_state.get("stop_reason", ""))
    final_answer = str(task_state.get("final_answer", ""))
    tool_steps = int(task_state.get("tool_steps", 0))
    attempts = int(task_state.get("attempts", 0))

    # ---- Outcome Summary ----
    outcome_summary = {
        "status": status,
        "stop_reason": stop_reason,
        "task_success": status == "completed" and stop_reason == "final_answer_returned",
        "final_answer_preview": (final_answer[:200] + "...") if len(final_answer) > 200 else final_answer,
    }

    # ---- Process Summary ----
    tool_events = [e for e in trace_events if (e.get("event_name") or e.get("event")) == EVENT_TOOL_EXECUTED]
    tool_sequence = [
        {
            "tool": e.get("tool_name") or e.get("name", ""),
            "status": e.get("status") or e.get("tool_status", ""),
            "step": e.get("step_id"),
        }
        for e in tool_events
    ]
    model_events = [e for e in trace_events if (e.get("event_name") or e.get("event")) == EVENT_MODEL_REQUESTED]
    context_events = [e for e in trace_events if (e.get("event_name") or e.get("event")) == EVENT_CONTEXT_BUILT]

    process_summary = {
        "total_tool_calls": len(tool_events),
        "total_model_calls": len(model_events),
        "total_context_built": len(context_events),
        "tool_sequence": tool_sequence,
        "unique_tools": list({e.get("tool_name") for e in tool_events if e.get("tool_name")}),
    }

    # ---- Efficiency Summary ----
    efficiency = metrics.get("efficiency", {})
    efficiency_summary = {
        "total_runtime_ms": efficiency.get("total_runtime_ms", 0),
        "avg_tool_ms": efficiency.get("avg_tool_ms", 0),
        "tool_count_with_duration": efficiency.get("tool_count_with_duration", 0),
        "context_built_count": efficiency.get("context_built_count", 0),
    }

    # ---- Safety Summary ----
    security_events = [e for e in trace_events if (e.get("event_name") or e.get("event")) == EVENT_SECURITY_EVENT]
    tool_with_status = [e for e in tool_events if (e.get("status") or e.get("tool_status", "")) in ("rejected", "blocked")]
    safety_summary = {
        "security_event_count": len(security_events),
        "blocked_tool_count": len(tool_with_status),
        "security_event_types": list({e.get("error_type") for e in security_events if e.get("error_type")}),
    }

    # ---- Assemble final report ----
    report: dict[str, Any] = {
        "run_id": run_id,
        "task_id": task_id,
        "status": status,
        "stop_reason": stop_reason,
        "tool_steps": tool_steps,
        "attempts": attempts,
        "outcome_summary": outcome_summary,
        "process_summary": process_summary,
        "efficiency_summary": efficiency_summary,
        "safety_summary": safety_summary,
        "metrics": metrics,
    }

    if failure_category:
        report["failure_category"] = failure_category

    return report
