from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


STAGE_INITIALIZING = "initializing"
STAGE_CONTEXT = "context"
STAGE_PLANNING = "planning"
STAGE_EXECUTING = "executing"
STAGE_VERIFYING = "verifying"
STAGE_MEMORY_CURATING = "memory_curating"
STAGE_FINISHED = "finished"
STAGE_STOPPED = "stopped"


@dataclass
class TaskGraphState:
    """Structured state for one fixed-role request lifecycle."""

    run_id: str
    task_id: str
    user_request: str
    current_stage: str = STAGE_INITIALIZING
    context_findings: dict[str, Any] = field(default_factory=dict)
    selected_files: list[str] = field(default_factory=list)
    relevant_memory: list[dict[str, Any]] = field(default_factory=list)
    execution_plan: dict[str, Any] = field(default_factory=dict)
    tool_observations: list[dict[str, Any]] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    verification_results: list[dict[str, Any]] = field(default_factory=list)
    final_answer: str = ""
    memory_compaction_report: dict[str, Any] = field(default_factory=dict)
    stage_transitions: list[dict[str, str]] = field(default_factory=list)
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if not self.stage_transitions:
            self.stage_transitions.append(self._transition_entry(self.current_stage))

    @staticmethod
    def _transition_entry(stage: str) -> dict[str, str]:
        return {
            "stage": stage,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def transition(self, stage: str) -> None:
        self.current_stage = stage
        self.stage_transitions.append(self._transition_entry(stage))

    def record_context(self, metadata: dict[str, Any]) -> None:
        self.transition(STAGE_CONTEXT)
        self.context_findings = {
            "prompt_budget_chars": metadata.get("prompt_budget_chars", 0),
            "prompt_chars": metadata.get("prompt_chars", 0),
            "recall_source": metadata.get("recall_source", ""),
            "new_recall_used": bool(metadata.get("new_recall_used", False)),
            "legacy_recall_used": bool(metadata.get("legacy_recall_used", False)),
        }
        selected = metadata.get("selected_files") or metadata.get("included_files") or []
        if isinstance(selected, list):
            self.selected_files = [str(item) for item in selected]
        relevant = metadata.get("relevant_memory") or []
        if isinstance(relevant, list):
            self.relevant_memory = [
                item if isinstance(item, dict) else {"text": str(item)}
                for item in relevant
            ]

    def set_execution_plan(self, plan: Any) -> None:
        self.transition(STAGE_PLANNING)
        if hasattr(plan, "steps"):
            self.execution_plan = {
                "steps": [
                    {
                        "step": step.step_number,
                        "tool": step.tool,
                        "rationale": step.rationale,
                        "description": step.description,
                    }
                    for step in getattr(plan, "steps", [])
                ],
                "no_action_needed": bool(getattr(plan, "no_action_needed", False)),
                "direct_answer": str(getattr(plan, "direct_answer", "")),
            }
            return
        self.execution_plan = dict(plan or {})

    def record_tool_observation(
        self,
        tool: str,
        args: dict[str, Any],
        result: str,
        *,
        step: int = 0,
        status: str = "",
    ) -> None:
        self.transition(STAGE_EXECUTING)
        path = str(args.get("path", "") or args.get("absolute_path", "") or "")
        observation = {
            "tool": str(tool),
            "step": int(step or 0),
            "path": path,
            "status": status,
            "result_preview": str(result)[:500],
        }
        self.tool_observations.append(observation)
        if tool in {"write_file", "patch_file"} and path and path not in self.modified_files:
            self.modified_files.append(path)

    def record_verification_result(self, result: Any, *, tool: str = "", is_final: bool = False) -> None:
        self.transition(STAGE_VERIFYING)
        self.verification_results.append({
            "tool": tool,
            "is_final": bool(is_final),
            "passed": bool(getattr(result, "passed", False)),
            "progress": str(getattr(result, "progress", "")),
            "notes": str(getattr(result, "notes", "")),
        })

    def set_memory_compaction_report(self, report: dict[str, Any]) -> None:
        self.transition(STAGE_MEMORY_CURATING)
        self.memory_compaction_report = dict(report or {})

    def set_final_answer(self, answer: str, *, stopped: bool = False) -> None:
        self.final_answer = str(answer)
        self.transition(STAGE_STOPPED if stopped else STAGE_FINISHED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "user_request": self.user_request,
            "current_stage": self.current_stage,
            "context_findings": dict(self.context_findings),
            "selected_files": list(self.selected_files),
            "relevant_memory": list(self.relevant_memory),
            "execution_plan": dict(self.execution_plan),
            "tool_observations": list(self.tool_observations),
            "modified_files": list(self.modified_files),
            "verification_results": list(self.verification_results),
            "final_answer": self.final_answer,
            "memory_compaction_report": dict(self.memory_compaction_report),
            "stage_transitions": list(self.stage_transitions),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskGraphState:
        state = cls(
            run_id=str(data.get("run_id", "")),
            task_id=str(data.get("task_id", "")),
            user_request=str(data.get("user_request", "")),
            current_stage=str(data.get("current_stage", STAGE_INITIALIZING)),
            context_findings=dict(data.get("context_findings", {})),
            selected_files=[str(item) for item in data.get("selected_files", [])],
            relevant_memory=list(data.get("relevant_memory", [])),
            execution_plan=dict(data.get("execution_plan", {})),
            tool_observations=list(data.get("tool_observations", [])),
            modified_files=[str(item) for item in data.get("modified_files", [])],
            verification_results=list(data.get("verification_results", [])),
            final_answer=str(data.get("final_answer", "")),
            memory_compaction_report=dict(data.get("memory_compaction_report", {})),
            created_at=str(data.get("created_at", "")),
        )
        transitions = data.get("stage_transitions")
        if isinstance(transitions, list) and transitions:
            state.stage_transitions = list(transitions)
        return state
