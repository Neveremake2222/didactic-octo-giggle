"""程序性经验检测。

从 working memory 中识别出可复用的执行模式：
  - repeated_file_access：同一文件被读取 >= 3 次
  - hypothesis_verification_flow：假设 + 待验证事项同时存在
  - multi_step_completion：连续多步含 fix/patch/update 关键词

这些模式可能值得升级为程序性经验或 skill 候选。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "pattern_type": self.pattern_type,
            "description": self.description,
            "stage": self.stage,
            "confidence": self.confidence,
            "procedure_steps": list(self.procedure_steps),
            "contributing_runs": list(self.contributing_runs),
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


class ProcedureCandidateDetector:
    """从 working memory 中识别可复用的执行模式。

    使用方式：
      detector = ProcedureCandidateDetector()
      candidates = detector.detect_from_working_memory(wm, "run-42")
    """

    def detect_from_working_memory(
        self,
        wm: Any,
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
        """合并新旧候选列表，相同模式时增加 confidence。"""
        by_id: dict[str, ProcedureCandidate] = {
            c.candidate_id: c for c in existing
        }
        for candidate in new:
            if candidate.candidate_id in by_id:
                # 相同候选：增加 confidence
                existing_c = by_id[candidate.candidate_id]
                existing_c.confidence = min(1.0, existing_c.confidence + 0.15)
                existing_c.contributing_runs = list(
                    set(existing_c.contributing_runs + candidate.contributing_runs)
                )
            else:
                by_id[candidate.candidate_id] = candidate
        return list(by_id.values())

    # -------------------------------------------------------------------------
    # 模式检测
    # -------------------------------------------------------------------------

    def _detect_repeated_access(
        self, wm: Any, run_id: str,
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
                ))
        return candidates

    def _detect_hypothesis_flow(
        self, wm: Any, run_id: str,
    ) -> list[ProcedureCandidate]:
        """检测假设 + 待验证事项同时存在的模式。"""
        hypotheses = getattr(wm, "active_hypotheses", [])
        pending = getattr(wm, "pending_verifications", [])

        if not hypotheses or not pending:
            return []

        cid = _make_candidate_id("hypothesis_verification_flow", run_id)
        return [ProcedureCandidate(
            candidate_id=cid,
            pattern_type="hypothesis_verification_flow",
            description=f"Agent maintained {len(hypotheses)} hypotheses with {len(pending)} pending verifications",
            confidence=0.5,
            procedure_steps=[f"Hypothesis: {h[:80]}" for h in hypotheses[:2]]
                       + [f"Verify: {p[:80]}" for p in pending[:2]],
            contributing_runs=[run_id],
        )]

    def _detect_multi_step_completion(
        self, wm: Any, run_id: str,
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

        cid = _make_candidate_id("multi_step_completion", run_id)
        return [ProcedureCandidate(
            candidate_id=cid,
            pattern_type="multi_step_completion",
            description=f"Multi-step completion with {len(completion_steps)} steps (potential reusable procedure)",
            confidence=0.55,
            procedure_steps=completion_steps[:5],
            contributing_runs=[run_id],
        )]

    # -------------------------------------------------------------------------
    # 内部工具
    # -------------------------------------------------------------------------

    @staticmethod
    def _extract_path(summary: str) -> str:
        """从观察摘要中提取文件路径。"""
        if summary.startswith("read "):
            parts = summary.split(":", 1)
            path = parts[0].replace("read ", "").strip()
            if path:
                return path
        for word in summary.split():
            if "/" in word or word.endswith((".py", ".md", ".txt", ".json", ".yaml")):
                clean = word.strip("[]():,.")
                if clean:
                    return clean
        return ""


def _make_candidate_id(pattern_type: str, key: str) -> str:
    """生成稳定的 candidate_id。"""
    raw = f"{pattern_type}:{key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
