"""report_builder 测试。"""

import pytest

from owl.report_builder import build_report


class TestReportBuilder:
    def _ts(self, status="completed", stop_reason="final_answer_returned", tool_steps=2, attempts=1):
        return {
            "run_id": "run_001",
            "task_id": "task_001",
            "status": status,
            "stop_reason": stop_reason,
            "final_answer": "Done.",
            "tool_steps": tool_steps,
            "attempts": attempts,
        }

    def _events(self):
        return [
            {"event_name": "run_started", "timestamp": "2026-04-16T00:00:00+00:00", "run_id": "run_001"},
            {"event_name": "context_built", "timestamp": "2026-04-16T00:00:01+00:00", "run_id": "run_001"},
            {"event_name": "tool_executed", "timestamp": "2026-04-16T00:00:02+00:00", "run_id": "run_001", "tool_name": "read_file", "status": "ok", "step_id": 1},
            {"event_name": "tool_executed", "timestamp": "2026-04-16T00:00:03+00:00", "run_id": "run_001", "tool_name": "patch_file", "status": "ok", "step_id": 2},
            {"event_name": "run_completed", "timestamp": "2026-04-16T00:00:04+00:00", "run_id": "run_001"},
        ]

    def _metrics(self):
        return {
            "outcome": {"task_success": True, "stop_reason": "final_answer_returned", "tool_steps": 2, "attempts": 1},
            "process": {"tool_call_count": 2, "unique_tool_count": 2, "repeated_identical_call_count": 0, "failed_tool_call_count": 0},
            "efficiency": {"total_runtime_ms": 4000, "avg_tool_ms": 500, "tool_count_with_duration": 2, "context_built_count": 1},
            "safety": {"policy_block_count": 0, "security_event_count": 0, "path_violation_count": 0},
        }

    def test_basic_build(self):
        report = build_report(self._ts(), self._events(), self._metrics())
        assert report["run_id"] == "run_001"
        assert report["task_id"] == "task_001"
        assert report["status"] == "completed"
        assert report["tool_steps"] == 2

    def test_outcome_summary(self):
        report = build_report(self._ts(), self._events(), self._metrics())
        outcome = report["outcome_summary"]
        assert outcome["task_success"] is True
        assert outcome["stop_reason"] == "final_answer_returned"

    def test_process_summary(self):
        report = build_report(self._ts(), self._events(), self._metrics())
        process = report["process_summary"]
        assert process["total_tool_calls"] == 2
        assert "read_file" in process["unique_tools"]
        assert "patch_file" in process["unique_tools"]
        assert len(process["tool_sequence"]) == 2

    def test_efficiency_summary(self):
        report = build_report(self._ts(), self._events(), self._metrics())
        efficiency = report["efficiency_summary"]
        assert efficiency["total_runtime_ms"] == 4000

    def test_safety_summary_clean(self):
        report = build_report(self._ts(), self._events(), self._metrics())
        safety = report["safety_summary"]
        assert safety["security_event_count"] == 0
        assert safety["blocked_tool_count"] == 0

    def test_safety_summary_with_events(self):
        events = self._events() + [
            {"event_name": "security_event", "error_type": "path_escape"},
        ]
        report = build_report(self._ts(), events, self._metrics())
        assert report["safety_summary"]["security_event_count"] == 1

    def test_failure_category_included(self):
        report = build_report(self._ts(), self._events(), self._metrics(), failure_category="policy_blocked")
        assert report["failure_category"] == "policy_blocked"

    def test_metrics_embedded(self):
        report = build_report(self._ts(), self._events(), self._metrics())
        assert "outcome" in report["metrics"]
        assert "process" in report["metrics"]
        assert "efficiency" in report["metrics"]
        assert "safety" in report["metrics"]

    def test_failed_task(self):
        ts = self._ts("failed", "model_error")
        report = build_report(ts, self._events(), self._metrics())
        assert report["outcome_summary"]["task_success"] is False

    def test_long_final_answer_truncated(self):
        ts = self._ts()
        ts["final_answer"] = "x" * 300
        report = build_report(ts, self._events(), self._metrics())
        assert len(report["outcome_summary"]["final_answer_preview"]) <= 203  # 200 + "..."
