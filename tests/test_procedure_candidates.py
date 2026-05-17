"""程序性经验检测与 Skill 注册测试。"""

from __future__ import annotations



from owl.procedure_candidate_detector import (
    ProcedureCandidate,
    ProcedureCandidateDetector,
    REPEATED_ACCESS_THRESHOLD,
)
from owl.skill_candidate_registry import (
    SkillCandidate,
    SkillCandidateRegistry,
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

    def test_repeated_file_access_has_trigger_and_anti_pattern(self):
        """repeated_file_access 候选携带 trigger_conditions 和 anti_patterns。"""
        wm = WorkingMemory()
        for _ in range(REPEATED_ACCESS_THRESHOLD):
            wm.add_observation("read_file", "read src/main.py: entry point")
        detector = ProcedureCandidateDetector()
        candidates = detector.detect_from_working_memory(wm, "run-meta")
        repeated = [c for c in candidates if c.pattern_type == "repeated_file_access"]
        assert len(repeated) >= 1
        assert "file_accessed_repeatedly" in repeated[0].trigger_conditions
        assert "single_read_only" in repeated[0].anti_patterns
        assert "src/main.py" in repeated[0].applicable_repo_paths

    def test_merge_candidates_merges_metadata(self):
        """合并时 trigger_conditions / anti_patterns / applicable_repo_paths 也会合并。"""
        detector = ProcedureCandidateDetector()
        c1 = ProcedureCandidate(
            candidate_id="abc123", pattern_type="repeated_file_access",
            description="test", confidence=0.5,
            trigger_conditions=["cond_a"],
            anti_patterns=["anti_a"],
            applicable_repo_paths=["path_a"],
        )
        c2 = ProcedureCandidate(
            candidate_id="abc123", pattern_type="repeated_file_access",
            description="test", confidence=0.5,
            trigger_conditions=["cond_b"],
            anti_patterns=["anti_b"],
            applicable_repo_paths=["path_b"],
        )
        merged = detector.merge_candidates([c1], [c2])
        assert len(merged) == 1
        assert "cond_a" in merged[0].trigger_conditions
        assert "cond_b" in merged[0].trigger_conditions
        assert "anti_a" in merged[0].anti_patterns
        assert "anti_b" in merged[0].anti_patterns


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
            contributing_runs=["run-1", "run-2"],  # 需要 >= 2 个 run 才能晋升到 procedure_candidate
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

    def test_promote_blocked_insufficient_runs(self):
        """只有 1 个 run 时不能晋升到 procedure_candidate。"""
        candidate = SkillCandidate(
            candidate_id="test", pattern_type="test",
            description="test", stage="semantic_fact", confidence=0.5,
            contributing_runs=["run-1"],  # 只有 1 个 run，不够
        )
        result = candidate.promote()
        assert result is False
        assert candidate.stage == "semantic_fact"

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
# SkillCandidateRegistry 持久化
# ---------------------------------------------------------------------------


class TestSkillCandidateRegistryPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        registry = SkillCandidateRegistry()
        registry.register(
            "repeated_file_access", "File X accessed 3 times", "run-1",
            trigger_conditions=["file_accessed_repeatedly"],
            anti_patterns=["single_read_only"],
            applicable_repo_paths=["src/main.py"],
        )
        path = tmp_path / "skill-candidates.json"
        registry.save(path)
        loaded = SkillCandidateRegistry.load(path)
        assert loaded.count() == 1
        c = loaded.all_candidates()[0]
        assert c.pattern_type == "repeated_file_access"
        assert c.trigger_conditions == ["file_accessed_repeatedly"]
        assert c.anti_patterns == ["single_read_only"]
        assert c.applicable_repo_paths == ["src/main.py"]

    def test_load_nonexistent_file_returns_empty_registry(self, tmp_path):
        path = tmp_path / "does_not_exist.json"
        registry = SkillCandidateRegistry.load(path)
        assert registry.count() == 0

    def test_register_merges_trigger_conditions(self, tmp_path):
        registry = SkillCandidateRegistry()
        registry.register(
            "test_type", "test desc", "run-1",
            trigger_conditions=["cond_a"],
        )
        registry.register(
            "test_type", "test desc", "run-2",
            trigger_conditions=["cond_b"],
        )
        c = registry.all_candidates()[0]
        assert "cond_a" in c.trigger_conditions
        assert "cond_b" in c.trigger_conditions

    def test_serialization_includes_new_fields(self, tmp_path):
        """to_dict / from_dict 保留 trigger_conditions / anti_patterns / applicable_repo_paths。"""
        registry = SkillCandidateRegistry()
        registry.register(
            "test_type", "test desc", "run-1",
            trigger_conditions=["cond_x"],
            anti_patterns=["anti_x"],
            applicable_repo_paths=["path_x"],
        )
        data = registry.to_dict()
        assert "candidates" in data
        assert "version" in data
        assert "saved_at" in data
        restored = SkillCandidateRegistry.from_dict(data)
        c = restored.all_candidates()[0]
        assert c.trigger_conditions == ["cond_x"]
        assert c.anti_patterns == ["anti_x"]
        assert c.applicable_repo_paths == ["path_x"]


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

    def test_compactor_passes_metadata_to_registry(self):
        """detect_procedure_candidates 将 trigger_conditions / anti_patterns / applicable_repo_paths 传给 registry。"""
        wm = WorkingMemory()
        for _ in range(REPEATED_ACCESS_THRESHOLD):
            wm.add_observation("read_file", "read src/main.py: entry point")
        compactor = MemoryCompactor()
        registry = SkillCandidateRegistry()
        compactor.detect_procedure_candidates(wm, "run-meta", registry)
        assert registry.count() >= 1
        c = registry.all_candidates()[0]
        assert len(c.trigger_conditions) > 0
        assert len(c.anti_patterns) > 0
        assert len(c.applicable_repo_paths) > 0
