"""failure_analyzer 测试。"""

import pytest

from owl.failure_analyzer import (
    classify_failure,
    FAILURE_CATEGORY_POLICY_BLOCKED,
    FAILURE_CATEGORY_BUDGET_EXHAUSTED,
    FAILURE_CATEGORY_REPEATED_TOOL_LOOP,
    FAILURE_CATEGORY_CONTEXT_INSUFFICIENT,
    FAILURE_CATEGORY_VERIFICATION_FAILED,
    FAILURE_CATEGORY_UNKNOWN,
    ALL_FAILURE_CATEGORIES,
)


class TestClassifyFailure:
    def _ts(self, status="completed", stop_reason="final_answer_returned"):
        return {"status": status, "stop_reason": stop_reason, "tool_steps": 0, "attempts": 1}

    def test_success_returns_empty(self):
        ts = self._ts("completed", "final_answer_returned")
        assert classify_failure(ts, []) == ""

    def test_policy_blocked(self):
        ts = self._ts("stopped", "approval_denied")
        events = [
            {"event_name": "tool_executed", "tool_name": "run_shell", "status": "rejected"},
        ]
        result = classify_failure(ts, events)
        assert result == FAILURE_CATEGORY_POLICY_BLOCKED

    def test_budget_exhausted_step_limit(self):
        ts = self._ts("stopped", "step_limit_reached")
        events = []
        assert classify_failure(ts, events) == FAILURE_CATEGORY_BUDGET_EXHAUSTED

    def test_budget_exhausted_retry_limit(self):
        ts = self._ts("stopped", "retry_limit_reached")
        events = []
        assert classify_failure(ts, events) == FAILURE_CATEGORY_BUDGET_EXHAUSTED

    def test_repeated_tool_loop(self):
        ts = self._ts("stopped", "final_answer_returned")
        events = [
            {"event_name": "tool_executed", "tool_name": "read_file", "status": "ok"},
            {"event_name": "tool_executed", "tool_name": "read_file", "status": "ok"},
            {"event_name": "tool_executed", "tool_name": "read_file", "status": "ok"},
            {"event_name": "tool_executed", "tool_name": "read_file", "status": "ok"},
        ]
        result = classify_failure(ts, events)
        assert result == FAILURE_CATEGORY_REPEATED_TOOL_LOOP

    def test_context_insufficient_model_error(self):
        ts = self._ts("failed", "model_error")
        events = []
        assert classify_failure(ts, events) == FAILURE_CATEGORY_CONTEXT_INSUFFICIENT

    def test_verification_failed_event(self):
        ts = self._ts("failed", "final_answer_returned")
        events = [{"event_name": "verification_failed"}]
        result = classify_failure(ts, events)
        assert result == FAILURE_CATEGORY_VERIFICATION_FAILED

    def test_unknown_for_unexpected_status(self):
        ts = {"status": "failed", "stop_reason": "unknown", "tool_steps": 0, "attempts": 1}
        events = []
        assert classify_failure(ts, events) == FAILURE_CATEGORY_UNKNOWN

    def test_with_metrics_policy_block(self):
        ts = self._ts("stopped", "final_answer_returned")
        events = [
            {"event_name": "tool_executed", "tool_name": "write_file", "status": "blocked"},
        ]
        metrics = {"safety": {"policy_block_count": 1}}
        assert classify_failure(ts, events, metrics) == FAILURE_CATEGORY_POLICY_BLOCKED

    def test_priority_policy_over_budget(self):
        ts = self._ts("stopped", "step_limit_reached")
        events = [
            {"event_name": "tool_executed", "tool_name": "write_file", "status": "blocked"},
        ]
        # policy_blocked should come before budget_exhausted
        assert classify_failure(ts, events) == FAILURE_CATEGORY_POLICY_BLOCKED

    def test_with_metrics_repeated_loop(self):
        ts = self._ts("stopped", "final_answer_returned")
        events = []
        metrics = {"process": {"repeated_identical_call_count": 3, "no_progress_loop_count": 0}}
        result = classify_failure(ts, events, metrics)
        assert result == FAILURE_CATEGORY_REPEATED_TOOL_LOOP
