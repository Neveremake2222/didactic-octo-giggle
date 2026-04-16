"""trace 事件写入器。

trace_logger 的职责是"只记录"，不做任何业务判断。
它封装了 RunStore.append_trace()，并在每条事件上自动注入：
  - timestamp（自动生成）
  - run_id（从 task_state 拿）

使用方式：
    logger = TraceLogger(run_store, task_state)
    logger.log("tool_executed", tool_name="read_file", status="ok", duration_ms=12)
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .trace_schema import TraceEvent


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id(value: Any) -> str:
    if hasattr(value, "run_id"):
        return value.run_id
    return str(value)


class TraceLogger:
    """轻量 trace 写入器，只负责落盘。"""

    def __init__(self, run_store, task_state):
        self._run_store = run_store
        self._task_state = task_state
        self._run_id = _run_id(task_state)
        self._current_step: int | None = None
        self._current_phase: str | None = None

    @property
    def run_id(self) -> str:
        return self._run_id

    def set_phase(self, phase: str) -> None:
        self._current_phase = phase

    def set_step(self, step: int) -> None:
        self._current_step = step

    def current_step(self) -> int | None:
        return self._current_step

    def current_phase(self) -> str | None:
        return self._current_phase

    def log(
        self,
        event_name: str,
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
        """构造并落盘一条 trace 事件。"""
        event = TraceEvent(
            event_name=event_name,
            timestamp=_now_iso(),
            run_id=self._run_id,
            step_id=step_id if step_id is not None else self._current_step,
            phase=phase if phase is not None else self._current_phase,
            tool_name=tool_name,
            status=status,
            duration_ms=duration_ms,
            input_summary=input_summary,
            output_summary=output_summary,
            error_type=error_type,
            metadata=metadata,
        )
        self._write(event)
        return event

    def log_dict(
        self,
        event_name: str,
        payload: dict[str, Any],
        step_id: int | None = None,
        phase: str | None = None,
    ) -> TraceEvent:
        """将一个 dict 转为 trace 事件并落盘（向后兼容模式）。

        用于兼容旧代码中 emit_trace(dict(...)) 的调用方式。
        如果 payload 里包含 TraceEvent 认识的字段，就用这些字段；
        剩余字段全部进 metadata。
        """
        known_fields = {
            "step_id", "phase", "tool_name", "status",
            "duration_ms", "input_summary", "output_summary",
            "error_type",
        }
        known = {k: v for k, v in payload.items() if k in known_fields and v is not None}
        extra = {k: v for k, v in payload.items() if k not in known_fields}

        event = TraceEvent(
            event_name=event_name,
            timestamp=_now_iso(),
            run_id=self._run_id,
            step_id=step_id if step_id is not None else self._current_step,
            phase=phase if phase is not None else self._current_phase,
            tool_name=known.get("tool_name"),
            status=known.get("status"),
            duration_ms=known.get("duration_ms"),
            input_summary=known.get("input_summary"),
            output_summary=known.get("output_summary"),
            error_type=known.get("error_type"),
            metadata=extra,
        )
        self._write(event)
        return event

    def _write(self, event: TraceEvent) -> None:
        """通过 RunStore.append_trace() 落盘。"""
        self._run_store.append_trace(self._task_state, event.to_dict())
