"""Trace 完整性和顺序验证。

验证 trace 事件是否包含所有必备事件，并且事件顺序是否合法。
用于 L3 诊断评测。

必备事件（每个 run 至少包含）：
  - run_started
  - context_built
  - model_requested
  - model_parsed
  - 至少一个 tool_executed 或 run_completed/run_finished/run_failed

可解释性要求（trace 至少要能回答）：
  1. 当前运行在哪个阶段结束
  2. prompt 中有哪些 section
  3. 为什么发起这次工具调用
  4. 为什么停止
  5. 记忆在什么时候写入和召回
"""

from __future__ import annotations

from typing import Any


# 必备事件列表
REQUIRED_EVENTS = [
    "run_started",
    "context_built",
    "model_requested",
    "model_parsed",
]

# 事件顺序约束：(before_event, after_event) — before 必须出现在 after 之前
ORDER_CONSTRAINTS = [
    ("run_started", "context_built"),
    ("context_built", "model_requested"),
    ("model_requested", "model_parsed"),
    ("model_parsed", "tool_executed"),
    ("run_started", "run_finished"),
    ("run_started", "run_completed"),
    ("run_started", "run_failed"),
]


def _get_event_name(event: dict[str, Any]) -> str:
    """Extract event name from a trace event dict (supports both event_name and event keys)."""
    return str(event.get("event_name") or event.get("event") or "")


def validate_trace_completeness(trace_events: list[dict[str, Any]]) -> dict[str, Any]:
    """Check that required events are present and the trace tells a complete story.

    Returns
    -------
    dict with keys:
        complete: bool — all required events present and has a terminal event
        missing_events: list[str] — required events not found
        event_count: int — total event count
        unique_events: list[str] — unique event names found
        has_terminal_event: bool — has run_finished/run_completed/run_failed
        has_tool_event: bool — has at least one tool_executed
        has_memory_events: bool — has memory_written or memory_recalled
    """
    event_names = [_get_event_name(e) for e in trace_events]
    unique_events = sorted(set(event_names))

    # Check required events
    missing = [req for req in REQUIRED_EVENTS if req not in event_names]

    # Check terminal events (at least one tool or a terminal lifecycle event)
    has_tool = "tool_executed" in event_names
    terminal_events = {"run_finished", "run_completed", "run_failed"}
    has_terminal = bool(set(event_names) & terminal_events)

    # Check memory events
    memory_events = {"memory_written", "memory_recalled"}
    has_memory = bool(set(event_names) & memory_events)

    complete = len(missing) == 0 and (has_tool or has_terminal)

    return {
        "complete": complete,
        "missing_events": missing,
        "event_count": len(trace_events),
        "unique_events": unique_events,
        "has_terminal_event": has_terminal,
        "has_tool_event": has_tool,
        "has_memory_events": has_memory,
    }


def validate_trace_order(trace_events: list[dict[str, Any]]) -> dict[str, Any]:
    """Check that events appear in a valid order.

    Returns
    -------
    dict with keys:
        valid: bool — no order violations found
        violations: list[str] — descriptions of order violations
        event_sequence: list[str] — ordered list of event names
    """
    event_names = [_get_event_name(e) for e in trace_events]
    violations: list[str] = []

    # Build event-to-indices map
    event_indices: dict[str, list[int]] = {}
    for idx, name in enumerate(event_names):
        event_indices.setdefault(name, []).append(idx)

    # Check each order constraint
    for before, after in ORDER_CONSTRAINTS:
        before_positions = event_indices.get(before)
        after_positions = event_indices.get(after)
        if before_positions and after_positions:
            # The first occurrence of 'before' should come before the last occurrence of 'after'
            if before_positions[0] > after_positions[-1]:
                violations.append(
                    f"{before} (first at {before_positions[0]}) should come before "
                    f"{after} (last at {after_positions[-1]})"
                )

    return {
        "valid": len(violations) == 0,
        "violations": violations,
        "event_sequence": event_names,
    }


def compute_trace_metrics(all_traces: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """Compute aggregate trace quality metrics across multiple runs.

    Parameters
    ----------
    all_traces: list of trace event lists (one per run)

    Returns
    -------
    dict with aggregate metrics:
        trace_completeness_rate: float — fraction of complete traces
        trace_order_valid_rate: float — fraction with valid ordering
        context_built_coverage: float — fraction with context_built events
        avg_event_count: float — average event count
        memory_event_rate: float — fraction with memory events
    """
    if not all_traces:
        return {
            "trace_completeness_rate": 0.0,
            "trace_order_valid_rate": 0.0,
            "context_built_coverage": 0.0,
            "avg_event_count": 0.0,
            "memory_event_rate": 0.0,
        }

    completeness_results = [validate_trace_completeness(t) for t in all_traces]
    order_results = [validate_trace_order(t) for t in all_traces]

    n = len(all_traces)
    complete_count = sum(1 for r in completeness_results if r["complete"])
    valid_order_count = sum(1 for r in order_results if r["valid"])
    context_built_count = sum(
        1 for t in all_traces if "context_built" in [_get_event_name(e) for e in t]
    )
    memory_count = sum(1 for r in completeness_results if r["has_memory_events"])

    return {
        "trace_completeness_rate": complete_count / n,
        "trace_order_valid_rate": valid_order_count / n,
        "context_built_coverage": context_built_count / n,
        "avg_event_count": sum(len(t) for t in all_traces) / n,
        "memory_event_rate": memory_count / n,
    }
