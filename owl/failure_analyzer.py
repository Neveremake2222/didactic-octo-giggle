"""失败分类器。

把 failed run 分类到具体的失败类别，而不是只输出 "failed"。
首版完全基于规则，不依赖 LLM。

分类规则按优先级排列：
  1. policy_blocked    — 有工具被安全策略拦截
  2. budget_exhausted  — 步数耗尽
  3. repeated_tool_loop — 连续重复调用同一工具
  4. context_insufficient — 模型错误 / 解析失败
  5. verification_failed — 验证脚本失败
  6. unknown_failure    — 无法分类
"""

from __future__ import annotations

from typing import Any

from .trace_schema import (
    EVENT_TOOL_EXECUTED,
    TOOL_STATUS_REJECTED,
    TOOL_STATUS_BLOCKED,
)

# 失败类别常量
FAILURE_CATEGORY_VERIFICATION_FAILED = "verification_failed"
FAILURE_CATEGORY_WRONG_TOOL_CHOICE = "wrong_tool_choice"
FAILURE_CATEGORY_REPEATED_TOOL_LOOP = "repeated_tool_loop"
FAILURE_CATEGORY_BUDGET_EXHAUSTED = "budget_exhausted"
FAILURE_CATEGORY_POLICY_BLOCKED = "policy_blocked"
FAILURE_CATEGORY_CONTEXT_INSUFFICIENT = "context_insufficient"
FAILURE_CATEGORY_UNKNOWN = "unknown_failure"

# 所有可能的失败类别
ALL_FAILURE_CATEGORIES = [
    FAILURE_CATEGORY_VERIFICATION_FAILED,
    FAILURE_CATEGORY_WRONG_TOOL_CHOICE,
    FAILURE_CATEGORY_REPEATED_TOOL_LOOP,
    FAILURE_CATEGORY_BUDGET_EXHAUSTED,
    FAILURE_CATEGORY_POLICY_BLOCKED,
    FAILURE_CATEGORY_CONTEXT_INSUFFICIENT,
    FAILURE_CATEGORY_UNKNOWN,
]


def classify_failure(
    task_state: dict[str, Any],
    trace_events: list[dict[str, Any]],
    metrics: dict[str, Any] | None = None,
) -> str:
    """根据 task_state + trace 事件对失败进行分类。

    如果 task_state.status 不是失败状态，返回空字符串。

    Parameters
    ----------
    task_state: 任务状态字典
    trace_events: trace.jsonl 事件列表
    metrics: 可选，compute_metrics() 的产出

    Returns
    -------
    失败类别字符串，或者空字符串（如果任务成功）
    """
    status = str(task_state.get("status", ""))
    stop_reason = str(task_state.get("stop_reason", ""))

    # 如果任务成功完成，不需要分类
    if status == "completed" and stop_reason == "final_answer_returned":
        return ""

    # 获取 process metrics（直接从 metrics 或从 trace 计算）
    if metrics:
        process = metrics.get("process", {})
    else:
        process = _extract_process_from_trace(trace_events)

    # 获取 safety 相关信息
    safety = {}
    if metrics:
        safety = metrics.get("safety", {})
    else:
        safety = _extract_safety_from_trace(trace_events)

    # ---- 规则 1: policy_blocked ----
    if safety.get("policy_block_count", 0) > 0:
        return FAILURE_CATEGORY_POLICY_BLOCKED

    # 检查 trace 中是否有被拦截的工具调用
    blocked_tools = [
        e for e in trace_events
        if (e.get("event_name") or e.get("event")) == EVENT_TOOL_EXECUTED
        and (e.get("status") or e.get("tool_status", "")) in (TOOL_STATUS_REJECTED, TOOL_STATUS_BLOCKED)
    ]
    if blocked_tools:
        return FAILURE_CATEGORY_POLICY_BLOCKED

    # ---- 规则 2: budget_exhausted ----
    if stop_reason in ("step_limit_reached", "retry_limit_reached"):
        return FAILURE_CATEGORY_BUDGET_EXHAUSTED

    # ---- 规则 3: repeated_tool_loop ----
    repeated = process.get("repeated_identical_call_count", 0)
    no_progress = process.get("no_progress_loop_count", 0)
    if repeated > 2 or no_progress > 0:
        return FAILURE_CATEGORY_REPEATED_TOOL_LOOP

    # ---- 规则 4: context_insufficient ----
    if stop_reason == "model_error":
        return FAILURE_CATEGORY_CONTEXT_INSUFFICIENT

    # ---- 规则 5: verification_failed ----
    # 如果有 verification_failed 事件
    verif_failed = [
        e for e in trace_events
        if (e.get("event_name") or e.get("event")) == "verification_failed"
    ]
    if verif_failed:
        return FAILURE_CATEGORY_VERIFICATION_FAILED

    # ---- 规则 6: 如果失败了但以上都不匹配 ----
    if status in ("failed", "stopped"):
        return FAILURE_CATEGORY_UNKNOWN

    # 未失败
    return ""


def classify_failure_from_files(
    task_state_path: str,
    trace_path: str,
) -> str:
    """从文件路径加载并分类。"""
    import json
    from pathlib import Path

    ts = json.loads(Path(task_state_path).read_text(encoding="utf-8"))
    events = []
    for line in Path(trace_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return classify_failure(ts, events)


def _extract_process_from_trace(trace_events: list[dict[str, Any]]) -> dict[str, Any]:
    """从 trace 事件中提取 process 指标（当 metrics 不可用时）。"""
    tool_calls = [e for e in trace_events if (e.get("event_name") or e.get("event")) == EVENT_TOOL_EXECUTED]
    tool_names = [e.get("tool_name", "") for e in tool_calls]

    # 重复调用
    call_sigs = [(e.get("tool_name", ""), e.get("input_summary", "")) for e in tool_calls]
    seen: dict[tuple, int] = {}
    for sig in call_sigs:
        seen[sig] = seen.get(sig, 0) + 1
    repeated = sum(c - 1 for c in seen.values() if c > 1)

    # 无进展循环
    no_progress = 0
    if tool_names:
        consecutive = 1
        for i in range(1, len(tool_names)):
            if tool_names[i] == tool_names[i - 1]:
                consecutive += 1
                if consecutive >= 3:
                    no_progress += 1
            else:
                consecutive = 1

    return {
        "tool_call_count": len(tool_calls),
        "repeated_identical_call_count": repeated,
        "no_progress_loop_count": no_progress,
    }


def _extract_safety_from_trace(trace_events: list[dict[str, Any]]) -> dict[str, Any]:
    """从 trace 事件中提取 safety 指标。"""
    blocked = [
        e for e in trace_events
        if (e.get("event_name") or e.get("event")) == EVENT_TOOL_EXECUTED
        and (e.get("status") or e.get("tool_status", "")) in (TOOL_STATUS_REJECTED, TOOL_STATUS_BLOCKED)
    ]
    return {"policy_block_count": len(blocked)}
