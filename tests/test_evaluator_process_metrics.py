"""compute_metrics 和四层 evaluator 测试。"""

import pytest

from owl.trace_schema import TraceEvent
from owl.metrics import compute_metrics
from owl.evaluators.outcome import OutcomeEvaluator
from owl.evaluators.process import ProcessEvaluator
from owl.evaluators.efficiency import EfficiencyEvaluator
from owl.evaluators.safety import SafetyEvaluator


def _ts(status="completed", stop_reason="final_answer_returned", tool_steps=2, attempts=1):
    return {
        "run_id": "run_001",
        "task_id": "task_001",
        "status": status,
        "stop_reason": stop_reason,
        "final_answer": "Done.",
        "tool_steps": tool_steps,
        "attempts": attempts,
    }


def _events(success=True, repeated=0, blocked=False):
    events = [
        TraceEvent(event_name="run_started", timestamp="2026-04-16T00:00:00+00:00", run_id="run_001"),
    ]
    tools = [("read_file", "ok"), ("patch_file", "ok")]
    if blocked:
        tools.append(("run_shell", "rejected"))
    for i, (name, status) in enumerate(tools):
        for _ in range(1 + (repeated if i == 0 else 0)):
            events.append(TraceEvent(
                event_name="tool_executed",
                timestamp=f"2026-04-16T00:00:0{i+1}+00:00",
                run_id="run_001",
                step_id=i + 1,
                tool_name=name,
                status=status,
                duration_ms=100,
                input_summary=f"file_{i}",
            ))
    if success:
        events.append(TraceEvent(event_name="run_completed", timestamp="2026-04-16T00:00:10+00:00", run_id="run_001"))
    else:
        events.append(TraceEvent(event_name="run_failed", timestamp="2026-04-16T00:00:10+00:00", run_id="run_001"))
    return events


class TestComputeMetrics:
    def test_success_case(self):
        metrics = compute_metrics(_events(), _ts())
        assert metrics["outcome"]["task_success"] is True
        assert metrics["outcome"]["stop_reason"] == "final_answer_returned"

    def test_failure_case(self):
        metrics = compute_metrics(_events(success=False), _ts("failed", "model_error"))
        assert metrics["outcome"]["task_success"] is False

    def test_process_metrics(self):
        metrics = compute_metrics(_events(), _ts())
        assert metrics["process"]["tool_call_count"] == 2
        assert metrics["process"]["unique_tool_count"] == 2
        assert metrics["process"]["repeated_identical_call_count"] == 0

    def test_repeated_calls(self):
        metrics = compute_metrics(_events(repeated=2), _ts())
        assert metrics["process"]["repeated_identical_call_count"] == 2

    def test_efficiency_metrics(self):
        metrics = compute_metrics(_events(), _ts())
        assert metrics["efficiency"]["avg_tool_ms"] == 100.0
        assert metrics["efficiency"]["context_built_count"] == 0

    def test_safety_metrics_clean(self):
        metrics = compute_metrics(_events(), _ts())
        assert metrics["safety"]["policy_block_count"] == 0

    def test_safety_metrics_blocked(self):
        metrics = compute_metrics(_events(blocked=True), _ts())
        assert metrics["safety"]["policy_block_count"] == 1

    def test_all_sections_present(self):
        metrics = compute_metrics(_events(), _ts())
        assert "outcome" in metrics
        assert "process" in metrics
        assert "efficiency" in metrics
        assert "safety" in metrics


# === OutcomeEvaluator ===


class TestOutcomeEvaluator:
    def test_success_score(self):
        ev = OutcomeEvaluator()
        result = ev.evaluate([], _ts(), {"outcome": {}})
        assert result["score"] == 1.0
        assert result["details"]["task_success"] is True

    def test_failure_score(self):
        ev = OutcomeEvaluator()
        result = ev.evaluate([], _ts("failed", "model_error"), {"outcome": {}})
        assert result["score"] < 1.0

    def test_step_limit_score(self):
        ev = OutcomeEvaluator()
        result = ev.evaluate([], _ts("stopped", "step_limit_reached"), {"outcome": {}})
        assert result["score"] == 0.3


# === ProcessEvaluator ===


class TestProcessEvaluator:
    def test_clean_process(self):
        ev = ProcessEvaluator()
        metrics = {"process": {
            "repeated_identical_call_count": 0,
            "no_progress_loop_count": 0,
            "blocked_tool_call_count": 0,
            "failed_tool_call_count": 0,
        }}
        # Provide tool events in the trace so premature_done is not flagged
        tool_events = [
            {"event_name": "tool_executed", "tool_name": "read_file"},
            {"event_name": "tool_executed", "tool_name": "patch_file"},
        ]
        result = ev.evaluate(tool_events, _ts(), metrics)
        assert result["score"] == 1.0
        assert result["details"]["premature_done"] is False

    def test_repeated_calls_penalty(self):
        ev = ProcessEvaluator()
        metrics = {"process": {
            "repeated_identical_call_count": 3,
            "no_progress_loop_count": 0,
            "blocked_tool_call_count": 0,
            "failed_tool_call_count": 0,
        }}
        result = ev.evaluate([], _ts(), metrics)
        assert result["score"] <= 0.5

    def test_premature_done(self):
        ev = ProcessEvaluator()
        metrics = {"process": {
            "repeated_identical_call_count": 0,
            "no_progress_loop_count": 0,
            "blocked_tool_call_count": 0,
            "failed_tool_call_count": 0,
        }}
        result = ev.evaluate([], _ts(), metrics)  # no tool events
        assert result["details"]["premature_done"] is True
        assert result["score"] <= 0.2


# === EfficiencyEvaluator ===


class TestEfficiencyEvaluator:
    def test_efficient_run(self):
        ev = EfficiencyEvaluator()
        metrics = {
            "efficiency": {"total_runtime_ms": 1000, "avg_tool_ms": 100, "context_built_count": 1},
            "process": {"tool_call_count": 2},
        }
        result = ev.evaluate([], _ts(), metrics)
        assert result["score"] == 1.0

    def test_slow_run_penalty(self):
        ev = EfficiencyEvaluator()
        metrics = {
            "efficiency": {"total_runtime_ms": 60000, "avg_tool_ms": 500, "context_built_count": 1},
            "process": {"tool_call_count": 15},
        }
        result = ev.evaluate([], _ts(), metrics)
        assert result["score"] < 1.0


# === SafetyEvaluator ===


class TestSafetyEvaluator:
    def test_clean_run(self):
        ev = SafetyEvaluator()
        metrics = {"safety": {"policy_block_count": 0, "security_event_count": 0, "path_violation_count": 0}}
        result = ev.evaluate([], _ts(), metrics)
        assert result["score"] == 1.0

    def test_single_block(self):
        ev = SafetyEvaluator()
        metrics = {"safety": {"policy_block_count": 1, "security_event_count": 1, "path_violation_count": 0}}
        result = ev.evaluate([], _ts(), metrics)
        assert result["score"] == 0.8

    def test_multiple_blocks(self):
        ev = SafetyEvaluator()
        metrics = {"safety": {"policy_block_count": 3, "security_event_count": 3, "path_violation_count": 2}}
        result = ev.evaluate([], _ts(), metrics)
        assert result["score"] == 0.5
