from owl.planner import ExecutionPlan, PlanStep
from owl.task_graph_state import (
    STAGE_EXECUTING,
    STAGE_FINISHED,
    STAGE_PLANNING,
    STAGE_STOPPED,
    TaskGraphState,
)


class VerificationLike:
    passed = True
    progress = "yes"
    notes = "completed"


def test_task_graph_state_records_plan_and_tool_observations():
    state = TaskGraphState(run_id="run_1", task_id="task_1", user_request="fix tests")
    plan = ExecutionPlan(steps=[
        PlanStep(step_number=1, tool="read_file", rationale="inspect", description="read failing test"),
    ])

    state.set_execution_plan(plan)
    state.record_tool_observation(
        "patch_file",
        {"path": "owl/runtime.py"},
        "patched successfully",
        step=1,
        status="ok",
    )

    assert state.current_stage == STAGE_EXECUTING
    assert state.execution_plan["steps"][0]["tool"] == "read_file"
    assert state.tool_observations[0]["tool"] == "patch_file"
    assert state.modified_files == ["owl/runtime.py"]
    assert any(item["stage"] == STAGE_PLANNING for item in state.stage_transitions)


def test_task_graph_state_records_bounded_lifecycle_outputs():
    state = TaskGraphState(run_id="run_1", task_id="task_1", user_request="fix tests")

    state.record_context({
        "prompt_budget_chars": 1200,
        "recall_source": "new",
        "new_recall_used": True,
        "selected_files": ["owl/runtime.py"],
    })
    state.record_verification_result(VerificationLike(), tool="read_file")
    state.set_memory_compaction_report({"promotion": {"promoted": 1}})
    state.set_final_answer("done")

    data = state.to_dict()
    restored = TaskGraphState.from_dict(data)

    assert restored.current_stage == STAGE_FINISHED
    assert restored.context_findings["new_recall_used"] is True
    assert restored.selected_files == ["owl/runtime.py"]
    assert restored.verification_results == [{
        "tool": "read_file",
        "is_final": False,
        "passed": True,
        "progress": "yes",
        "notes": "completed",
    }]
    assert restored.memory_compaction_report == {"promotion": {"promoted": 1}}
    assert restored.final_answer == "done"


def test_task_graph_state_can_finish_as_stopped():
    state = TaskGraphState(run_id="run_1", task_id="task_1", user_request="fix tests")

    state.set_final_answer("stopped", stopped=True)

    assert state.current_stage == STAGE_STOPPED
