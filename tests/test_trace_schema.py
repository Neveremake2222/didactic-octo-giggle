"""trace_schema 和 trace_logger 测试。"""

import json
import pytest
from pathlib import Path

from owl.trace_schema import (
    TraceEvent,
    make_event,
    parse_trace_file,
    EVENT_RUN_STARTED,
    EVENT_TOOL_EXECUTED,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_FAILED,
    TOOL_STATUS_OK,
    TOOL_STATUS_ERROR,
)
from owl.trace_logger import TraceLogger


# === TraceEvent ===


class TestTraceEvent:
    def test_create_basic(self):
        event = TraceEvent(
            event_name="run_started",
            timestamp="2026-04-16T00:00:00+00:00",
            run_id="run_001",
        )
        assert event.event_name == "run_started"
        assert event.run_id == "run_001"
        assert event.step_id is None
        assert event.metadata == {}

    def test_to_dict_roundtrip(self):
        event = TraceEvent(
            event_name="tool_executed",
            timestamp="2026-04-16T00:00:00+00:00",
            run_id="run_001",
            step_id=1,
            tool_name="read_file",
            status="ok",
            duration_ms=42,
            metadata={"path": "README.md"},
        )
        d = event.to_dict()
        restored = TraceEvent.from_dict(d)
        assert restored.event_name == event.event_name
        assert restored.run_id == event.run_id
        assert restored.step_id == 1
        assert restored.tool_name == "read_file"
        assert restored.duration_ms == 42
        assert restored.metadata["path"] == "README.md"

    def test_from_dict_with_none_fields(self):
        d = {
            "event_name": "run_started",
            "timestamp": "2026-04-16T00:00:00+00:00",
            "run_id": "run_002",
        }
        event = TraceEvent.from_dict(d)
        assert event.step_id is None
        assert event.tool_name is None

    def test_from_dict_with_all_fields(self):
        d = {
            "event_name": "tool_executed",
            "timestamp": "2026-04-16T00:00:00+00:00",
            "run_id": "run_003",
            "step_id": 2,
            "phase": "tool_executing",
            "tool_name": "patch_file",
            "status": "ok",
            "duration_ms": 100,
            "input_summary": "patch README.md",
            "output_summary": "success",
            "error_type": None,
            "metadata": {"key": "value"},
        }
        event = TraceEvent.from_dict(d)
        assert event.phase == "tool_executing"
        assert event.input_summary == "patch README.md"

    def test_make_event_convenience(self):
        event = make_event(
            "tool_executed",
            run_id="run_004",
            tool_name="read_file",
            status="ok",
        )
        assert event.event_name == "tool_executed"
        assert event.timestamp  # should be auto-generated
        assert event.tool_name == "read_file"

    def test_make_event_with_metadata(self):
        event = make_event(
            "tool_executed",
            run_id="run_005",
            path="README.md",
            extra=True,
        )
        assert event.metadata["path"] == "README.md"
        assert event.metadata["extra"] is True


class TestParseTraceFile:
    def test_parse_lines(self):
        lines = [
            json.dumps({"event_name": "run_started", "timestamp": "2026-04-16T00:00:00+00:00", "run_id": "r1"}),
            json.dumps({"event_name": "tool_executed", "timestamp": "2026-04-16T00:00:01+00:00", "run_id": "r1", "tool_name": "read_file"}),
        ]
        events = parse_trace_file(lines=lines)
        assert len(events) == 2
        assert events[0].event_name == "run_started"
        assert events[1].tool_name == "read_file"

    def test_parse_empty_lines(self):
        events = parse_trace_file(lines=["", "  ", ""])
        assert len(events) == 0

    def test_parse_invalid_json_skipped(self):
        lines = ["not json", json.dumps({"event_name": "ok", "timestamp": "t", "run_id": "r"})]
        events = parse_trace_file(lines=lines)
        assert len(events) == 1


# === TraceLogger ===


class TestTraceLogger:
    def _make_logger(self, tmp_path):
        from owl.run_store import RunStore
        from owl.task_state import TaskState

        rs = RunStore(tmp_path / "runs")
        ts = TaskState.create("task_1", "hello")
        rs.start_run(ts)
        return TraceLogger(rs, ts), rs, ts

    def test_log_basic(self, tmp_path):
        logger, rs, ts = self._make_logger(tmp_path)
        event = logger.log("run_started", step_id=0)
        assert event.event_name == "run_started"
        assert event.run_id == ts.run_id
        assert event.step_id == 0

    def test_log_with_all_fields(self, tmp_path):
        logger, rs, ts = self._make_logger(tmp_path)
        event = logger.log(
            "tool_executed",
            step_id=1,
            tool_name="read_file",
            status="ok",
            duration_ms=42,
            input_summary="README.md",
        )
        assert event.tool_name == "read_file"
        assert event.duration_ms == 42

    def test_log_auto_injects_phase_and_step(self, tmp_path):
        logger, rs, ts = self._make_logger(tmp_path)
        logger.set_phase("tool_executing")
        logger.set_step(3)
        event = logger.log("tool_executed")
        assert event.phase == "tool_executing"
        assert event.step_id == 3

    def test_log_dict_compat(self, tmp_path):
        logger, rs, ts = self._make_logger(tmp_path)
        event = logger.log_dict("tool_executed", {
            "tool_name": "read_file",
            "status": "ok",
            "custom_field": "value",
        })
        assert event.tool_name == "read_file"
        assert event.metadata["custom_field"] == "value"

    def test_writes_to_trace_jsonl(self, tmp_path):
        logger, rs, ts = self._make_logger(tmp_path)
        logger.log("run_started")
        logger.log("tool_executed", tool_name="read_file", status="ok")

        trace_path = rs.trace_path(ts)
        assert trace_path.exists()
        lines = trace_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

    def test_current_step_and_phase(self, tmp_path):
        logger, _, _ = self._make_logger(tmp_path)
        assert logger.current_step() is None
        assert logger.current_phase() is None
        logger.set_step(5)
        logger.set_phase("model_calling")
        assert logger.current_step() == 5
        assert logger.current_phase() == "model_calling"
