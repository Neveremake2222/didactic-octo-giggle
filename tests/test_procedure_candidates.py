"""程序性经验检测与 Skill 注册测试。"""

from __future__ import annotations

import pytest

from owl.procedure_candidate_detector import (
    ProcedureCandidate,
    ProcedureCandidateDetector,
    REPEATED_ACCESS_THRESHOLD,
)
from owl.skill_candidate_registry import (
    SkillCandidate,
    SkillCandidateRegistry,
    STAGES,
)
from owl.working_memory import WorkingMemory
from owl.memory_compactor import MemoryCompactor


# ---------------------------------------------------------------------------
# ProcedureCandidateDetector
# ---------------------------------------------------------------------------


class TestProcedureCandidateDetector:
    def test_detects_repeated_file_access(self):
        wm = WorkingMemory()
        for _ in range(REPEATED_ACCESS_THRESHOLD):
            wm.add_observation("read_file", "read src/main.py: entry point")
        detector = ProcedureCandidateDetector()
        candidates = detector.detect_from_working_memory(wm, "run-1")
        repeated = [c for c in candidates if c.pattern_type == "repeated_file_access"]
        assert len(repeated) >= 1
        assert "src/main.py" in repeated[0].description
        assert repeated[0].confidence >= 0.5

    def test_no_detection_below_threshold(self):
        wm = WorkingMemory()
        wm.add_observation("read_file", "read src/main.py: entry point")
        wm.add_observation("read_file", "read src/main.py: second read")
        # 只有 2 次，不够 REPEATED_ACCESS_THRESHOLD (3)
        detector = ProcedureCandidateDetector()
        candidates = detector.detect_from_working_memory(wm, "run-2")
        repeated = [c for c in candidates if c.pattern_type == "repeated_file_access"]
        assert len(repeated) == 0

    def test_detects_hypothesis_verification_flow(self):
        wm = WorkingMemory()
        wm.add_hypothesis("bug is in auth.py")
        wm.add_pending("verify auth flow works")
        wm.add_observation("read_file", "read auth.py: found issue")
        detector = ProcedureCandidateDetector()
        candidates = detector.detect_from_working_memory(wm, "run-3")
        hyp = [c for c in candidates if c.pattern_type == "hypothesis_verification_flow"]
        assert len(hyp) >= 1

    def test_detects_multi_step_completion(self):
        wm = WorkingMemory()
        wm.add_observation("write_file", "success: fixed the bug in main.py")
        wm.add_observation("write_file", "success: updated tests for main.py")
        detector = ProcedureCandidateDetector()
        candidates = detector.detect_from_working_memory(wm, "run-4")
        multi = [c for c in candidates if c.pattern_type == "multi_step_completion"]
        assert len(multi) >= 1

    def test_merge_candidates_increases_confidence(self):
        detector = ProcedureCandidateDetector()
        c1 = ProcedureCandidate(
            candidate_id="abc123", pattern_type="test",
            description="test pattern", confidence=0.5,
        )
        c2 = ProcedureCandidate(
            candidate_id="abc123", pattern_type="test",
            description="test pattern", confidence=0.5,
        )
        merged = detector.merge_candidates([c1], [c2])
        assert len(merged) == 1
        assert merged[0].confidence > 0.5

    def test_no_patterns_empty_wm(self):
        wm = WorkingMemory()
        detector = ProcedureCandidateDetector()
        candidates = detector.detect_from_working_memory(wm, "run-empty")
        assert len(candidates) == 0


# ---------------------------------------------------------------------------
# SkillCandidateRegistry
# ---------------------------------------------------------------------------


class TestSkillCandidateRegistry:
    def test_register_new_candidate(self):
        registry = SkillCandidateRegistry()
        candidate = registry.register(
            "repeated_file_access", "File X accessed 3 times", "run-1"
        )
        assert candidate.candidate_id
        assert candidate.pattern_type == "repeated_file_access"
        assert registry.count() == 1

    def test_register_updates_existing(self):
        registry = SkillCandidateRegistry()
        c1 = registry.register("test_type", "test desc", "run-1")
        # 第二次注册同一 pattern_type+description → 复用同一对象，confidence 上升
        c2 = registry.register("test_type", "test desc", "run-2")
        assert registry.count() == 1  # 同一个
        assert c1 is c2  # 同一对象
        assert len(c1.contributing_runs) == 2
        assert "run-1" in c1.contributing_runs
        assert "run-2" in c1.contributing_runs

    def test_promote_stage(self):
        candidate = SkillCandidate(
            candidate_id="test", pattern_type="test",
            description="test", stage="semantic_fact", confidence=0.5,
        )
        assert candidate.stage == "semantic_fact"
        candidate.promote()
        assert candidate.stage == "procedure_candidate"
        candidate.promote()
        assert candidate.stage == "skill_candidate"
        candidate.promote()
        assert candidate.stage == "established_skill"
        # 最高阶段，无法再晋升
        result = candidate.promote()
        assert result is False

    def test_record_use_success(self):
        candidate = SkillCandidate(
            candidate_id="test", pattern_type="test",
            description="test", confidence=0.5,
        )
        candidate.record_use(True)
        assert candidate.successful_uses == 1
        assert candidate.confidence > 0.5

    def test_record_use_failure(self):
        candidate = SkillCandidate(
            candidate_id="test", pattern_type="test",
            description="test", confidence=0.5,
        )
        candidate.record_use(False)
        assert candidate.failed_uses == 1
        assert candidate.confidence < 0.5

    def test_by_stage_filter(self):
        registry = SkillCandidateRegistry()
        registry.register("type_a", "desc a", "run-1")
        registry.register("type_b", "desc b", "run-2")
        facts = registry.by_stage("semantic_fact")
        assert len(facts) == 2

    def test_serialization_roundtrip(self):
        registry = SkillCandidateRegistry()
        registry.register("test_type", "test desc", "run-1")
        data = registry.to_dict()
        restored = SkillCandidateRegistry.from_dict(data)
        assert restored.count() == 1
        c = restored.all_candidates()[0]
        assert c.pattern_type == "test_type"


# ---------------------------------------------------------------------------
# MemoryCompactor.detect_procedure_candidates
# ---------------------------------------------------------------------------


class TestCompactorProcedureDetection:
    def test_compactor_detects_and_registers(self):
        wm = WorkingMemory()
        for _ in range(REPEATED_ACCESS_THRESHOLD):
            wm.add_observation("read_file", "read utils.py: helper functions")

        compactor = MemoryCompactor()
        registry = SkillCandidateRegistry()
        candidates = compactor.detect_procedure_candidates(wm, "run-x", registry)

        assert len(candidates) >= 1
        assert registry.count() >= 1
