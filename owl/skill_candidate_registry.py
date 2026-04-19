"""候选 Skill 注册与晋升管理。

管理从 procedure_candidate_detector 检测到的程序性经验，
按四阶段晋升路径推进：semantic_fact → procedure_candidate → skill_candidate → established_skill。

每个候选记录：
  - 哪个 run 贡献了这个候选
  - 成功/失败使用次数
  - 当前 confidence 和 stage
  - 可复用的 procedure_steps
  - 触发条件 / 反例 / 适用路径（用于后续 skill index 精准命中）

当 confidence 超过阈值时自动晋升到下一阶段。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 晋升阶段
# ---------------------------------------------------------------------------

STAGES = [
    "semantic_fact",
    "procedure_candidate",
    "skill_candidate",
    "established_skill",
]

# 自动晋升的 confidence 阈值
PROMOTE_THRESHOLDS = {
    "semantic_fact": 0.7,
    "procedure_candidate": 0.85,
    "skill_candidate": 0.95,
}

# 默认持久化路径（相对于 .owl/ 目录）
DEFAULT_SKILL_REGISTRY_PATH = "memory/skill-candidates.json"

# 跨 session 候选最低要求：至少贡献自几个不同 run 才能晋升为 procedure_candidate
CANDIDATE_MIN_CONTRIBUTING_RUNS = 2


# ---------------------------------------------------------------------------
# SkillCandidate
# ---------------------------------------------------------------------------


@dataclass
class SkillCandidate:
    """一个候选 Skill 记录。"""

    candidate_id: str
    pattern_type: str
    description: str
    stage: str = "semantic_fact"
    confidence: float = 0.5
    successful_uses: int = 0
    failed_uses: int = 0
    contributing_runs: list[str] = field(default_factory=list)
    procedure_steps: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    # Phase B: 用于精准命中和反例过滤
    trigger_conditions: list[str] = field(default_factory=list)
    anti_patterns: list[str] = field(default_factory=list)
    applicable_repo_paths: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.updated_at:
            self.updated_at = self.created_at

    def promote(self) -> bool:
        """尝试晋升到下一阶段。"""
        idx = STAGES.index(self.stage) if self.stage in STAGES else -1
        if idx < len(STAGES) - 1:
            next_stage = STAGES[idx + 1]
            # 晋升到 procedure_candidate 需要至少 CANDIDATE_MIN_CONTRIBUTING_RUNS 个 run
            if next_stage == "procedure_candidate" and len(self.contributing_runs) < CANDIDATE_MIN_CONTRIBUTING_RUNS:
                return False
            self.stage = next_stage
            self.updated_at = _now_iso()
            return True
        return False

    def record_use(self, success: bool) -> None:
        """记录一次使用结果，调整 confidence。"""
        if success:
            self.successful_uses += 1
            self.confidence = min(1.0, self.confidence + 0.05)
        else:
            self.failed_uses += 1
            self.confidence = max(0.0, self.confidence - 0.1)
        self.updated_at = _now_iso()

        # 自动晋升检查
        threshold = PROMOTE_THRESHOLDS.get(self.stage, 1.0)
        if self.confidence >= threshold:
            self.promote()

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "pattern_type": self.pattern_type,
            "description": self.description,
            "stage": self.stage,
            "confidence": round(self.confidence, 4),
            "successful_uses": self.successful_uses,
            "failed_uses": self.failed_uses,
            "contributing_runs": list(self.contributing_runs),
            "procedure_steps": list(self.procedure_steps),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "trigger_conditions": list(self.trigger_conditions),
            "anti_patterns": list(self.anti_patterns),
            "applicable_repo_paths": list(self.applicable_repo_paths),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillCandidate:
        return cls(
            candidate_id=str(data.get("candidate_id", "")),
            pattern_type=str(data.get("pattern_type", "")),
            description=str(data.get("description", "")),
            stage=str(data.get("stage", "semantic_fact")),
            confidence=float(data.get("confidence", 0.5)),
            successful_uses=int(data.get("successful_uses", 0)),
            failed_uses=int(data.get("failed_uses", 0)),
            contributing_runs=list(data.get("contributing_runs", [])),
            procedure_steps=list(data.get("procedure_steps", [])),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            trigger_conditions=list(data.get("trigger_conditions", [])),
            anti_patterns=list(data.get("anti_patterns", [])),
            applicable_repo_paths=list(data.get("applicable_repo_paths", [])),
        )


# ---------------------------------------------------------------------------
# SkillCandidateRegistry
# ---------------------------------------------------------------------------


class SkillCandidateRegistry:
    """候选 Skill 注册、查询与持久化。

    使用方式：
      registry = SkillCandidateRegistry()
      candidate = registry.register("repeated_file_access", "File X accessed 3 times", "run-42")
      candidates = registry.by_stage("procedure_candidate")
      registry.save(".owl/memory/skill-candidates.json")
    """

    def __init__(self) -> None:
        self._candidates: dict[str, SkillCandidate] = {}

    def register(
        self,
        pattern_type: str,
        description: str,
        run_id: str,
        procedure_steps: list[str] | None = None,
        trigger_conditions: list[str] | None = None,
        anti_patterns: list[str] | None = None,
        applicable_repo_paths: list[str] | None = None,
    ) -> SkillCandidate:
        """注册或更新一个候选。相同 pattern_type+description 合并。"""
        cid = _make_skill_id(pattern_type, description)
        if cid in self._candidates:
            # 已存在：增加 confidence，添加 run，合并元数据
            existing = self._candidates[cid]
            existing.confidence = min(1.0, existing.confidence + 0.15)
            if run_id not in existing.contributing_runs:
                existing.contributing_runs.append(run_id)
            # 合并新增的元数据（不覆盖已有的）
            if trigger_conditions:
                for tc in trigger_conditions:
                    if tc not in existing.trigger_conditions:
                        existing.trigger_conditions.append(tc)
            if anti_patterns:
                for ap in anti_patterns:
                    if ap not in existing.anti_patterns:
                        existing.anti_patterns.append(ap)
            if applicable_repo_paths:
                for rp in applicable_repo_paths:
                    if rp not in existing.applicable_repo_paths:
                        existing.applicable_repo_paths.append(rp)
            existing.updated_at = _now_iso()
            return existing

        candidate = SkillCandidate(
            candidate_id=cid,
            pattern_type=pattern_type,
            description=description,
            contributing_runs=[run_id],
            procedure_steps=procedure_steps or [],
            trigger_conditions=trigger_conditions or [],
            anti_patterns=anti_patterns or [],
            applicable_repo_paths=applicable_repo_paths or [],
        )
        self._candidates[cid] = candidate
        return candidate

    def get(self, candidate_id: str) -> SkillCandidate | None:
        return self._candidates.get(candidate_id)

    def by_stage(self, stage: str) -> list[SkillCandidate]:
        return [c for c in self._candidates.values() if c.stage == stage]

    def all_candidates(self) -> list[SkillCandidate]:
        return list(self._candidates.values())

    def count(self) -> int:
        return len(self._candidates)

    # --- 持久化 ---

    def save(self, path: str | Path) -> None:
        """将 registry 持久化到 JSON 文件。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> SkillCandidateRegistry:
        """从 JSON 文件加载 registry。文件不存在时返回空 registry。"""
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        except (json.JSONDecodeError, OSError):
            return cls()

    # --- 序列化 ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [c.to_dict() for c in self._candidates.values()],
            "version": 1,
            "saved_at": _now_iso(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillCandidateRegistry:
        registry = cls()
        for c_data in data.get("candidates", []):
            candidate = SkillCandidate.from_dict(c_data)
            registry._candidates[candidate.candidate_id] = candidate
        return registry


def _make_skill_id(pattern_type: str, description: str) -> str:
    """生成稳定的 candidate_id。"""
    raw = f"{pattern_type}:{description}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
