"""统一 trace 事件 schema。

所有 trace 事件都应符合 TraceEvent 的结构。
这样做的好处：
  - 同一类事件字段稳定，新旧 run 可以做基础对比
  - 事件消费方（metrics、report_builder）有统一的解析入口
  - 事件名有常量约束，打字错误在 import 时就能发现

事件分为五大类：
  - 生命周期：run_started, phase_transition, context_built, run_completed, run_failed
  - 工具：tool_selected, tool_executed, tool_retried, tool_blocked
  - 模型：model_requested, model_parsed
  - 记忆：memory_written, memory_recalled
  - 安全：security_event
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 事件名常量
# ---------------------------------------------------------------------------

# 生命周期事件
EVENT_RUN_STARTED = "run_started"
EVENT_PHASE_TRANSITION = "phase_transition"
EVENT_CONTEXT_BUILT = "context_built"
EVENT_RUN_COMPLETED = "run_completed"
EVENT_RUN_FAILED = "run_failed"

# 工具事件
EVENT_TOOL_SELECTED = "tool_selected"
EVENT_TOOL_EXECUTED = "tool_executed"
EVENT_TOOL_RETRIED = "tool_retried"
EVENT_TOOL_BLOCKED = "tool_blocked"

# 模型事件
EVENT_MODEL_REQUESTED = "model_requested"
EVENT_MODEL_PARSED = "model_parsed"

# 记忆事件
EVENT_MEMORY_WRITTEN = "memory_written"
EVENT_MEMORY_RECALLED = "memory_recalled"

# 安全事件
EVENT_SECURITY_EVENT = "security_event"

# 验证事件（目前 benchmark verifier 不走 trace，留作后续扩展）
EVENT_VERIFICATION_PASSED = "verification_passed"
EVENT_VERIFICATION_FAILED = "verification_failed"

# Phase 2 事件
EVENT_CONTEXT_SOURCES_DISCOVERED = "context_sources_discovered"
EVENT_PRECOMPACTION_FLUSHED = "precompaction_flushed"
EVENT_CONTEXT_COMPACTED = "context_compacted"
EVENT_COMPACTION_PROMOTED = "compaction_promoted"
EVENT_MEMORY_SKIPPED_STALE = "memory_skipped_stale"
EVENT_MEMORY_RANKED = "memory_ranked"
EVENT_MEMORY_DEDUPLICATED = "memory_deduplicated"
EVENT_PROCEDURE_CANDIDATES_DETECTED = "procedure_candidates_detected"


# ---------------------------------------------------------------------------
# 工具状态常量
# ---------------------------------------------------------------------------

TOOL_STATUS_OK = "ok"
TOOL_STATUS_ERROR = "error"
TOOL_STATUS_REJECTED = "rejected"
TOOL_STATUS_BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# TraceEvent 数据类
# ---------------------------------------------------------------------------

@dataclass
class TraceEvent:
    """统一 trace 事件结构。

    所有字段，除 metadata 外，都有明确语义。
    metadata 用于存放事件类型特有的额外信息。
    """

    event_name: str
    timestamp: str
    run_id: str
    step_id: int | None = None
    phase: str | None = None
    tool_name: str | None = None
    status: str | None = None
    duration_ms: int | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    error_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_name": self.event_name,
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "phase": self.phase,
            "tool_name": self.tool_name,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "error_type": self.error_type,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TraceEvent:
        # Backward compat: old trace events use "event" instead of "event_name"
        event_name = data.get("event_name") or data.get("event") or ""
        return cls(
            event_name=str(event_name),
            timestamp=str(data.get("timestamp") or data.get("created_at", "")),
            run_id=str(data.get("run_id", "")),
            step_id=int(data["step_id"]) if data.get("step_id") is not None else None,
            phase=str(data["phase"]) if data.get("phase") is not None else None,
            tool_name=str(data.get("tool_name") or data.get("name", None)) if (data.get("tool_name") or data.get("name")) is not None else None,
            status=str(data.get("status") or data.get("tool_status", None)) if (data.get("status") or data.get("tool_status")) is not None else None,
            duration_ms=int(data["duration_ms"]) if data.get("duration_ms") is not None else None,
            input_summary=str(data["input_summary"]) if data.get("input_summary") is not None else None,
            output_summary=str(data["output_summary"]) if data.get("output_summary") is not None else None,
            error_type=str(data.get("error_type") or data.get("security_event_type", None) or "") if (data.get("error_type") is not None or data.get("security_event_type") is not None) else None,
            metadata=dict(data.get("metadata", {})),
        )


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------

def make_event(
    event_name: str,
    run_id: str,
    step_id: int | None = None,
    phase: str | None = None,
    tool_name: str | None = None,
    status: str | None = None,
    duration_ms: int | None = None,
    input_summary: str | None = None,
    output_summary: str | None = None,
    error_type: str | None = None,
    **metadata: Any,
) -> TraceEvent:
    """构造一个 TraceEvent 的便捷工厂函数。"""
    return TraceEvent(
        event_name=event_name,
        timestamp=_now_iso(),
        run_id=run_id,
        step_id=step_id,
        phase=phase,
        tool_name=tool_name,
        status=status,
        duration_ms=duration_ms,
        input_summary=input_summary,
        output_summary=output_summary,
        error_type=error_type,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# JSONL 解析辅助
# ---------------------------------------------------------------------------

def parse_trace_file(path: str | None = None, lines: list[str] | None = None) -> list[TraceEvent]:
    """从 JSONL 行列表解析 TraceEvent 列表。

    支持两种调用方式：
      - parse_trace_file(lines=["{...}", "{...}"])  # 直接传行
      - parse_trace_file(path="/path/to/trace.jsonl")  # 从文件读
    """
    if lines is None and path is not None:
        from pathlib import Path
        lines = Path(path).read_text(encoding="utf-8").splitlines()

    events = []
    for line in (lines or []):
        line = line.strip()
        if not line:
            continue
        import json as _json
        try:
            events.append(TraceEvent.from_dict(_json.loads(line)))
        except Exception:
            # 向后兼容：无法解析的行跳过
            pass
    return events
