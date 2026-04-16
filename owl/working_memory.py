"""当前任务的短期工作记忆。

working_memory 和 semantic_memory 的区别：
  - working_memory 生命周期仅限当前 run 或当前 ask
  - semantic_memory 跨任务持久化，可跨 run 复用

working_memory 只保留当前任务高相关的动态内容：
  - 当前 plan（下一步准备做什么）
  - 最近几步关键观察（工具结果摘要）
  - 当前假设（对问题的中间判断）
  - 候选修改点（可能要改的文件/位置）
  - 待验证事项（还需要确认的事情）

它不是完整历史的副本，而是"当前任务最关键的一小撮信息"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


from .memory_config import MAX_OBSERVATIONS, MAX_HYPOTHESES, MAX_CANDIDATES, MAX_PENDING


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 观察记录的上限由 memory_config.MAX_OBSERVATIONS 统一管理


@dataclass
class Observation:
    """一次工具执行的观察记录。"""

    tool_name: str
    summary: str
    created_at: str = ""
    # Phase 2: 文件指纹追踪
    file_path: str = ""
    file_fingerprint: str = ""
    # Phase 2 竞态修复：稳定 ID 替代索引
    observation_id: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.observation_id:
            Observation._counter += 1
            self.observation_id = f"obs_{Observation._counter}"

    _counter: int = 0  # 类级别计数器，确保每个 observation 有唯一 ID

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "summary": self.summary,
            "created_at": self.created_at,
            "file_path": self.file_path,
            "file_fingerprint": self.file_fingerprint,
            "observation_id": self.observation_id,
        }


@dataclass
class WorkingMemory:
    """当前任务的短期工作记忆。

    生命周期仅限当前 run，不跨 session 持久化。
    在 ask() 开始时创建，结束时由 compactor 决定哪些内容沉淀成长期记忆。
    """

    # 当前计划（下一步准备做什么）
    plan: str = ""

    # 最近观察（工具结果摘要）
    recent_observations: list[Observation] = field(default_factory=list)

    # 当前假设（对问题的中间判断）
    active_hypotheses: list[str] = field(default_factory=list)

    # 候选修改点（可能要改的文件/位置）
    candidate_targets: list[str] = field(default_factory=list)

    # 待验证事项（还需要确认的事情）
    pending_verifications: list[str] = field(default_factory=list)

    # 元信息
    created_at: str = ""
    task_summary: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = _now_iso()

    # --- 写入方法 ---

    def set_plan(self, plan: str) -> None:
        """更新当前计划。"""
        self.plan = plan.strip()

    def set_task_summary(self, summary: str) -> None:
        """记录当前任务摘要。"""
        self.task_summary = summary.strip()[:300]

    def add_observation(self, tool_name: str, summary: str, file_path: str = "", file_fingerprint: str = "") -> None:
        """记录一次观察。"""
        summary = summary.strip()[:500]
        if not summary:
            return
        self.recent_observations.append(
            Observation(tool_name=tool_name, summary=summary,
                        file_path=file_path, file_fingerprint=file_fingerprint)
        )
        # 保留最近的 MAX_OBSERVATIONS 条
        if len(self.recent_observations) > MAX_OBSERVATIONS:
            self.recent_observations = self.recent_observations[-MAX_OBSERVATIONS:]

    def add_hypothesis(self, hypothesis: str) -> None:
        """添加一个假设。"""
        hypothesis = hypothesis.strip()
        if not hypothesis:
            return
        # 去重
        if hypothesis in self.active_hypotheses:
            self.active_hypotheses.remove(hypothesis)
        self.active_hypotheses.append(hypothesis)
        if len(self.active_hypotheses) > MAX_HYPOTHESES:
            self.active_hypotheses = self.active_hypotheses[-MAX_HYPOTHESES:]

    def add_candidate(self, target: str) -> None:
        """添加一个候选修改点。"""
        target = target.strip()
        if not target:
            return
        if target in self.candidate_targets:
            self.candidate_targets.remove(target)
        self.candidate_targets.append(target)
        if len(self.candidate_targets) > MAX_CANDIDATES:
            self.candidate_targets = self.candidate_targets[-MAX_CANDIDATES:]

    def add_pending(self, item: str) -> None:
        """添加一个待验证事项。"""
        item = item.strip()
        if not item:
            return
        if item in self.pending_verifications:
            self.pending_verifications.remove(item)
        self.pending_verifications.append(item)
        if len(self.pending_verifications) > MAX_PENDING:
            self.pending_verifications = self.pending_verifications[-MAX_PENDING:]

    def remove_pending(self, item: str) -> None:
        """移除一个待验证事项（已验证完毕）。"""
        self.pending_verifications = [
            p for p in self.pending_verifications if p != item
        ]

    # --- 查询方法 ---

    def is_empty(self) -> bool:
        """是否没有任何实质内容。"""
        return (
            not self.plan
            and not self.recent_observations
            and not self.active_hypotheses
            and not self.candidate_targets
            and not self.pending_verifications
        )

    # --- 渲染 ---

    def render_text(self) -> str:
        """渲染成给模型看的紧凑文本。"""
        lines = ["Memory:"]

        if self.task_summary:
            lines.append(f"- task: {self.task_summary}")
        if self.plan:
            lines.append(f"- plan: {self.plan}")
        if self.recent_observations:
            lines.append(f"- observations({len(self.recent_observations)}):")
            for obs in self.recent_observations[-4:]:
                lines.append(f"  [{obs.tool_name}] {obs.summary}")
        if self.active_hypotheses:
            lines.append(f"- hypotheses: {self.active_hypotheses}")
        if self.candidate_targets:
            lines.append(f"- targets: {self.candidate_targets}")
        if self.pending_verifications:
            lines.append(f"- pending: {self.pending_verifications}")
        if len(lines) == 1:
            lines.append("- empty")

        return "\n".join(lines)

    # --- 序列化 ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "recent_observations": [obs.to_dict() for obs in self.recent_observations],
            "active_hypotheses": list(self.active_hypotheses),
            "candidate_targets": list(self.candidate_targets),
            "pending_verifications": list(self.pending_verifications),
            "created_at": self.created_at,
            "task_summary": self.task_summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkingMemory:
        wm = cls(
            plan=str(data.get("plan", "")),
            active_hypotheses=list(data.get("active_hypotheses", [])),
            candidate_targets=list(data.get("candidate_targets", [])),
            pending_verifications=list(data.get("pending_verifications", [])),
            created_at=str(data.get("created_at", "")),
            task_summary=str(data.get("task_summary", "")),
        )
        for obs_data in data.get("recent_observations", []):
            wm.recent_observations.append(
                Observation(
                    tool_name=str(obs_data.get("tool_name", "")),
                    summary=str(obs_data.get("summary", "")),
                    created_at=str(obs_data.get("created_at", "")),
                    file_path=str(obs_data.get("file_path", "")),
                    file_fingerprint=str(obs_data.get("file_fingerprint", "")),
                    observation_id=str(obs_data.get("observation_id", "")),
                )
            )
        return wm
