"""程序性经验检测。

从 working memory 中识别出可复用的执行模式：
  - repeated_file_access：同一文件被读取 >= 3 次
  - hypothesis_verification_flow：假设 + 待验证事项同时存在
  - multi_step_completion：连续多步含 fix/patch/update 关键词

这些模式可能值得升级为程序性经验或 skill 候选。

每个候选携带 trigger_conditions / anti_patterns / applicable_repo_paths，
供后续 SkillCandidateRegistry 和 skill index 使用。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .memory_utils import extract_path_from_text, make_record_id
from .working_memory import WorkingMemory


# ---------------------------------------------------------------------------
# ProcedureCandidate
# ---------------------------------------------------------------------------


@dataclass
class ProcedureCandidate:
    """一次检测到的程序性经验候选。"""

    candidate_id: str
    pattern_type: str     # repeated_file_access | hypothesis_verification_flow | multi_step_completion
    description: str
    stage: str = "semantic_fact"  # semantic_fact → procedure_candidate → skill_candidate → established_skill
    confidence: float = 0.5
    procedure_steps: list[str] = field(default_factory=list)
    contributing_runs: list[str] = field(default_factory=list)
    # 用于后续 skill index 精准命中
    trigger_conditions: list[str] = field(default_factory=list)
    anti_patterns: list[str] = field(default_factory=list)
    applicable_repo_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "pattern_type": self.pattern_type,
            "description": self.description,
            "stage": self.stage,
            "confidence": self.confidence,
            "procedure_steps": list(self.procedure_steps),
            "contributing_runs": list(self.contributing_runs),
            "trigger_conditions": list(self.trigger_conditions),
            "anti_patterns": list(self.anti_patterns),
            "applicable_repo_paths": list(self.applicable_repo_paths),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProcedureCandidate:
        return cls(
            candidate_id=str(data.get("candidate_id", "")),
            pattern_type=str(data.get("pattern_type", "")),
            description=str(data.get("description", "")),
            stage=str(data.get("stage", "semantic_fact")),
            confidence=float(data.get("confidence", 0.5)),
            procedure_steps=list(data.get("procedure_steps", [])),
            contributing_runs=list(data.get("contributing_runs", [])),
            trigger_conditions=list(data.get("trigger_conditions", [])),
            anti_patterns=list(data.get("anti_patterns", [])),
            applicable_repo_paths=list(data.get("applicable_repo_paths", [])),
        )


# ---------------------------------------------------------------------------
# ProcedureCandidateDetector
# ---------------------------------------------------------------------------

# 同一文件被读多少次才视为 repeated_file_access
REPEATED_ACCESS_THRESHOLD = 3

# 多步完成的最少步骤数
MULTI_STEP_MIN = 2

# 完成动作关键词
COMPLETION_KEYWORDS = ("fix", "patch", "update", "wrote", "success", "done")

# 每种模式对应的 trigger_conditions 和 anti_patterns
_PATTERN_META = {
    "repeated_file_access": {
        "trigger_conditions": ["file_accessed_repeatedly"],
        "anti_patterns": ["single_read_only", "error_reading_file"],
    },
    "hypothesis_verification_flow": {
        "trigger_conditions": ["bug_investigation_task", "hypothesis_driven_debugging"],
        "anti_patterns": ["no_hypotheses", "simple_lookup_task"],
    },
    "multi_step_completion": {
        "trigger_conditions": ["multi_file_modification", "refactoring_task"],
        "anti_patterns": ["single_change_only", "no_completion_keywords"],
    },
}


class ProcedureCandidateDetector:
    """从 working memory 中识别可复用的执行模式。

    使用方式：
      detector = ProcedureCandidateDetector()
      candidates = detector.detect_from_working_memory(wm, "run-42")
    """

    def detect_from_working_memory(
        self,
        wm: WorkingMemory,
        run_id: str,
    ) -> list[ProcedureCandidate]:
        """从 working memory 中检测程序性经验候选。

        参数：
          wm      — WorkingMemory 实例
          run_id  — 当前 run 的 ID

        返回检测到的 ProcedureCandidate 列表。
        """
        candidates: list[ProcedureCandidate] = []

        # 模式 1: repeated_file_access
        candidates.extend(self._detect_repeated_access(wm, run_id))

        # 模式 2: hypothesis_verification_flow
        candidates.extend(self._detect_hypothesis_flow(wm, run_id))

        # 模式 3: multi_step_completion
        candidates.extend(self._detect_multi_step_completion(wm, run_id))

        return candidates

    def merge_candidates(
        self,
        existing: list[ProcedureCandidate],
        new: list[ProcedureCandidate],
    ) -> list[ProcedureCandidate]:
        """合并新旧候选列表，相同模式时增加 confidence 并合并元数据。"""
        by_id: dict[str, ProcedureCandidate] = {
            c.candidate_id: c for c in existing
        }
        for candidate in new:
            if candidate.candidate_id in by_id:
                # 相同候选：增加 confidence，合并 runs 和元数据
                existing_c = by_id[candidate.candidate_id]
                existing_c.confidence = min(1.0, existing_c.confidence + 0.15)
                existing_c.contributing_runs = list(
                    set(existing_c.contributing_runs + candidate.contributing_runs)
                )
                # 合并 trigger_conditions / anti_patterns / applicable_repo_paths
                for tc in candidate.trigger_conditions:
                    if tc not in existing_c.trigger_conditions:
                        existing_c.trigger_conditions.append(tc)
                for ap in candidate.anti_patterns:
                    if ap not in existing_c.anti_patterns:
                        existing_c.anti_patterns.append(ap)
                for rp in candidate.applicable_repo_paths:
                    if rp not in existing_c.applicable_repo_paths:
                        existing_c.applicable_repo_paths.append(rp)
            else:
                by_id[candidate.candidate_id] = candidate
        return list(by_id.values())

    # -------------------------------------------------------------------------
    # 模式检测
    # -------------------------------------------------------------------------

    def _detect_repeated_access(
        self, wm: WorkingMemory, run_id: str,
    ) -> list[ProcedureCandidate]:
        """检测同一文件被读取 >= REPEATED_ACCESS_THRESHOLD 次。"""
        observations = getattr(wm, "recent_observations", [])
        file_counts: Counter[str] = Counter()

        for obs in observations:
            summary = getattr(obs, "summary", str(obs)) if hasattr(obs, "summary") else str(obs)
            path = self._extract_path(summary)
            if path:
                file_counts[path] += 1

        candidates = []
        meta = _PATTERN_META["repeated_file_access"]
        for path, count in file_counts.items():
            if count >= REPEATED_ACCESS_THRESHOLD:
                cid = _make_candidate_id("repeated_file_access", path)
                candidates.append(ProcedureCandidate(
                    candidate_id=cid,
                    pattern_type="repeated_file_access",
                    description=f"File {path} accessed {count} times in one run (potential convention or dependency)",
                    confidence=0.6,
                    procedure_steps=[f"Read {path}"] * min(count, 3),
                    contributing_runs=[run_id],
                    trigger_conditions=meta["trigger_conditions"],
                    anti_patterns=meta["anti_patterns"],
                    applicable_repo_paths=[path],
                ))
        return candidates

    def _detect_hypothesis_flow(
        self, wm: WorkingMemory, run_id: str,
    ) -> list[ProcedureCandidate]:
        """检测假设 + 待验证事项同时存在的模式。"""
        hypotheses = getattr(wm, "active_hypotheses", [])
        pending = getattr(wm, "pending_verifications", [])

        if not hypotheses or not pending:
            return []

        meta = _PATTERN_META["hypothesis_verification_flow"]
        cid = _make_candidate_id("hypothesis_verification_flow", run_id)
        # 从 observations 中提取涉及的文件路径
        observed_paths = self._collect_observed_paths(wm)
        return [ProcedureCandidate(
            candidate_id=cid,
            pattern_type="hypothesis_verification_flow",
            description=f"Agent maintained {len(hypotheses)} hypotheses with {len(pending)} pending verifications",
            confidence=0.5,
            procedure_steps=[f"Hypothesis: {h[:80]}" for h in hypotheses[:2]]
                       + [f"Verify: {p[:80]}" for p in pending[:2]],
            contributing_runs=[run_id],
            trigger_conditions=meta["trigger_conditions"],
            anti_patterns=meta["anti_patterns"],
            applicable_repo_paths=observed_paths,
        )]

    def _detect_multi_step_completion(
        self, wm: WorkingMemory, run_id: str,
    ) -> list[ProcedureCandidate]:
        """检测连续多步含完成关键词的模式。"""
        observations = getattr(wm, "recent_observations", [])
        completion_steps = []

        for obs in observations:
            summary = getattr(obs, "summary", str(obs)) if hasattr(obs, "summary") else str(obs)
            if any(kw in summary.lower() for kw in COMPLETION_KEYWORDS):
                completion_steps.append(summary[:120])

        if len(completion_steps) < MULTI_STEP_MIN:
            return []

        meta = _PATTERN_META["multi_step_completion"]
        cid = _make_candidate_id("multi_step_completion", run_id)
        observed_paths = self._collect_observed_paths(wm)
        return [ProcedureCandidate(
            candidate_id=cid,
            pattern_type="multi_step_completion",
            description=f"Multi-step completion with {len(completion_steps)} steps (potential reusable procedure)",
            confidence=0.55,
            procedure_steps=completion_steps[:5],
            contributing_runs=[run_id],
            trigger_conditions=meta["trigger_conditions"],
            anti_patterns=meta["anti_patterns"],
            applicable_repo_paths=observed_paths,
        )]

    # -------------------------------------------------------------------------
    # 内部工具
    # -------------------------------------------------------------------------

    @staticmethod
    def _extract_path(summary: str) -> str:
        """从观察摘要中提取文件路径（委托到 memory_utils）。"""
        return extract_path_from_text(summary)

    @staticmethod
    def _collect_observed_paths(wm: WorkingMemory) -> list[str]:
        """从 working memory 的 observations 中收集所有出现过的文件路径。"""
        paths: list[str] = []
        for obs in getattr(wm, "recent_observations", []):
            summary = getattr(obs, "summary", str(obs)) if hasattr(obs, "summary") else str(obs)
            path = extract_path_from_text(summary)
            if path and path not in paths:
                paths.append(path)
        return paths


def _make_candidate_id(pattern_type: str, key: str) -> str:
    """生成稳定的 candidate_id（委托到 memory_utils）。"""
    return make_record_id(pattern_type, key, length=12)
