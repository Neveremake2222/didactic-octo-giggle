"""execution_state 模块测试。"""

import pytest

from owl.execution_state import (
    ExecutionState,
    PHASE_INITIALIZING,
    PHASE_PROMPT_BUILDING,
    PHASE_TOOL_EXECUTING,
    PHASE_STOPPED,
    PHASE_FINISHED,
)


class TestExecutionStateCreate:
    def test_create_with_defaults(self):
        state = ExecutionState.create()
        assert state.run_id.startswith("run_")
        assert state.task_id.startswith("task_")
        assert state.current_phase == PHASE_INITIALIZING
        assert state.current_step == 0
        assert state.step_budget == 6
        assert state.tool_attempts == {}
        assert state.last_tool is None
        assert state.stop_reason is None
        assert state.failure_reason is None

    def test_create_with_explicit_ids(self):
        state = ExecutionState.create(run_id="run_abc", task_id="task_xyz", step_budget=10)
        assert state.run_id == "run_abc"
        assert state.task_id == "task_xyz"
        assert state.step_budget == 10

    def test_create_has_timestamp(self):
        state = ExecutionState.create()
        assert state.created_at
        assert "T" in state.created_at


class TestExecutionStateToolRecording:
    def test_record_single_tool_call(self):
        state = ExecutionState.create()
        state.record_tool_call("read_file")
        assert state.current_step == 1
        assert state.tool_attempts == {"read_file": 1}
        assert state.last_tool == "read_file"

    def test_record_multiple_tool_calls(self):
        state = ExecutionState.create()
        state.record_tool_call("read_file")
        state.record_tool_call("search")
        state.record_tool_call("read_file")
        assert state.current_step == 3
        assert state.tool_attempts == {"read_file": 2, "search": 1}
        assert state.last_tool == "read_file"

    def test_record_tool_call_updates_last_tool(self):
        state = ExecutionState.create()
        state.record_tool_call("list_files")
        state.record_tool_call("search")
        assert state.last_tool == "search"


class TestExecutionStateTransition:
    def test_transition_changes_phase(self):
        state = ExecutionState.create()
        state.transition(PHASE_PROMPT_BUILDING)
        assert state.current_phase == PHASE_PROMPT_BUILDING

    def test_observe_stores_last_observation(self):
        state = ExecutionState.create()
        state.observe("file has 42 lines")
        assert state.last_observation == "file has 42 lines"

    def test_observe_overwrites_previous(self):
        state = ExecutionState.create()
        state.observe("first observation")
        state.observe("second observation")
        assert state.last_observation == "second observation"


class TestExecutionStateStop:
    def test_mark_stop(self):
        state = ExecutionState.create()
        state.mark_stop("step_limit_reached")
        assert state.stop_reason == "step_limit_reached"
        assert state.failure_reason is None
        assert state.current_phase == PHASE_STOPPED

    def test_mark_stop_with_failure(self):
        state = ExecutionState.create()
        state.mark_stop("model_error", failure_reason="empty response")
        assert state.stop_reason == "model_error"
        assert state.failure_reason == "empty response"
        assert state.current_phase == PHASE_STOPPED

    def test_is_stopped(self):
        state = ExecutionState.create()
        assert not state.is_stopped()
        state.mark_stop("done")
        assert state.is_stopped()

    def test_is_over_budget(self):
        state = ExecutionState.create(step_budget=2)
        assert not state.is_over_budget()
        state.record_tool_call("read_file")
        assert not state.is_over_budget()
        state.record_tool_call("search")
        assert state.is_over_budget()


class TestExecutionStateSerialization:
    def test_to_dict_and_back(self):
        state = ExecutionState.create(run_id="run_1", task_id="task_1", step_budget=8)
        state.record_tool_call("read_file")
        state.transition(PHASE_TOOL_EXECUTING)
        state.observe("observed something")

        d = state.to_dict()
        restored = ExecutionState.from_dict(d)

        assert restored.run_id == state.run_id
        assert restored.task_id == state.task_id
        assert restored.current_phase == PHASE_TOOL_EXECUTING
        assert restored.current_step == 1
        assert restored.step_budget == 8
        assert restored.tool_attempts == {"read_file": 1}
        assert restored.last_tool == "read_file"
        assert restored.last_observation == "observed something"
        assert restored.created_at == state.created_at

    def test_from_dict_handles_missing_fields(self):
        d = {"run_id": "run_x", "task_id": "task_y"}
        state = ExecutionState.from_dict(d)
        assert state.run_id == "run_x"
        assert state.current_phase == PHASE_INITIALIZING
        assert state.current_step == 0
        assert state.tool_attempts == {}
