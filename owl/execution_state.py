"""当前 run 的控制面状态。

execution_state 和 task_state 的区别：
  - task_state 是"运行结果摘要"：最终步数、停止原因、最终答案
  - execution_state 是"运行过程控制面"：当前阶段、最近动作、失败信息

execution_state 回答的问题是：系统现在停在哪、刚做了什么、下一步准备做什么。
它只存在于 ask() 的生命周期内，不跨 session 持久化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


# --- Phase 常量 ---

PHASE_INITIALIZING = "initializing"
PHASE_PROMPT_BUILDING = "prompt_building"
PHASE_MODEL_CALLING = "model_calling"
PHASE_PARSING = "parsing"
PHASE_TOOL_EXECUTING = "tool_executing"
PHASE_FINISHED = "finished"
PHASE_STOPPED = "stopped"


@dataclass
class ExecutionState:
    """一次 ask() 运行过程中的控制面状态。

    与 TaskState 的区别：
    - TaskState 记录"结果"：步数、最终答案、停止原因
    - ExecutionState 记录"过程"：当前阶段、最近观察、工具尝试分布

    生命周期：仅存在于单次 ask() 调用内，不跨 session 持久化。
    """

    run_id: str
    task_id: str
    route: str | None = None
    current_phase: str = PHASE_INITIALIZING
    current_step: int = 0
    step_budget: int = 6
    tool_attempts: dict[str, int] = field(default_factory=dict)
    last_tool: str | None = None
    last_observation: str | None = None
    stop_reason: str | None = None
    failure_reason: str | None = None
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    # --- 工厂方法 ---

    @classmethod
    def create(cls, run_id: str = "", task_id: str = "", step_budget: int = 6) -> ExecutionState:
        if not run_id:
            run_id = "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        if not task_id:
            task_id = "task_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        return cls(run_id=run_id, task_id=task_id, step_budget=step_budget)

    # --- 状态转换 ---

    def record_tool_call(self, tool_name: str) -> None:
        """记录一次工具调用，更新 current_step 和 tool_attempts。"""
        self.current_step += 1
        self.tool_attempts[tool_name] = self.tool_attempts.get(tool_name, 0) + 1
        self.last_tool = tool_name

    def transition(self, phase: str) -> None:
        """切换当前阶段。"""
        self.current_phase = phase

    def observe(self, observation: str) -> None:
        """记录最近一次观察（工具结果摘要等）。"""
        self.last_observation = observation

    def mark_stop(self, reason: str, failure_reason: str | None = None) -> None:
        """标记运行停止。"""
        self.stop_reason = reason
        self.failure_reason = failure_reason
        self.current_phase = PHASE_STOPPED

    def is_stopped(self) -> bool:
        """是否已经停止。"""
        return self.current_phase in (PHASE_STOPPED, PHASE_FINISHED)

    def is_over_budget(self) -> bool:
        """是否已超出步数预算。"""
        return self.current_step >= self.step_budget

    # --- 序列化 ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "route": self.route,
            "current_phase": self.current_phase,
            "current_step": self.current_step,
            "step_budget": self.step_budget,
            "tool_attempts": dict(self.tool_attempts),
            "last_tool": self.last_tool,
            "last_observation": self.last_observation,
            "stop_reason": self.stop_reason,
            "failure_reason": self.failure_reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionState:
        return cls(
            run_id=str(data.get("run_id", "")),
            task_id=str(data.get("task_id", "")),
            route=data.get("route"),
            current_phase=str(data.get("current_phase", PHASE_INITIALIZING)),
            current_step=int(data.get("current_step", 0)),
            step_budget=int(data.get("step_budget", 6)),
            tool_attempts=dict(data.get("tool_attempts", {})),
            last_tool=data.get("last_tool"),
            last_observation=data.get("last_observation"),
            stop_reason=data.get("stop_reason"),
            failure_reason=data.get("failure_reason"),
            created_at=str(data.get("created_at", "")),
        )
