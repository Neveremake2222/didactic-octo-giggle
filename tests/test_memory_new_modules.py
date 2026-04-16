"""working_memory, semantic_memory, memory_writer, memory_retriever, memory_compactor 测试。"""

import pytest

from owl.working_memory import WorkingMemory, Observation
from owl.semantic_memory import SemanticMemory, SemanticRecord
from owl.memory_writer import MemoryWriter, WRITE_TARGET_WORKING, WRITE_TARGET_SEMANTIC, WRITE_TARGET_SKIP
from owl.memory_retriever import MemoryRetriever, RecallResult
from owl.memory_compactor import MemoryCompactor
from owl.compaction_schema import CompactionSchema, build_schema_from_working_memory, schema_to_semantic_records


# === WorkingMemory ===


class TestWorkingMemory:
    def test_create_empty(self):
        wm = WorkingMemory()
        assert wm.is_empty()
        assert wm.plan == ""

    def test_set_plan(self):
        wm = WorkingMemory()
        wm.set_plan("read README.md and fix bugs")
        assert wm.plan == "read README.md and fix bugs"

    def test_set_task_summary(self):
        wm = WorkingMemory()
        wm.set_task_summary("fix auth bug")
        assert wm.task_summary == "fix auth bug"

    def test_add_observation(self):
        wm = WorkingMemory()
        wm.add_observation("read_file", "file has 42 lines")
        assert len(wm.recent_observations) == 1
        assert wm.recent_observations[0].tool_name == "read_file"

    def test_add_observation_limit(self):
        wm = WorkingMemory()
        for i in range(15):
            wm.add_observation("tool", f"obs {i}")
        assert len(wm.recent_observations) <= 8

    def test_add_hypothesis(self):
        wm = WorkingMemory()
        wm.add_hypothesis("bug is in line 42")
        assert "bug is in line 42" in wm.active_hypotheses

    def test_add_hypothesis_dedup(self):
        wm = WorkingMemory()
        wm.add_hypothesis("same hypothesis")
        wm.add_hypothesis("same hypothesis")
        assert wm.active_hypotheses.count("same hypothesis") == 1

    def test_add_candidate(self):
        wm = WorkingMemory()
        wm.add_candidate("src/auth.py")
        assert "src/auth.py" in wm.candidate_targets

    def test_add_pending_and_remove(self):
        wm = WorkingMemory()
        wm.add_pending("verify fix works")
        assert "verify fix works" in wm.pending_verifications
        wm.remove_pending("verify fix works")
        assert "verify fix works" not in wm.pending_verifications

    def test_render_text(self):
        wm = WorkingMemory()
        wm.set_task_summary("fix bug")
        wm.add_observation("read_file", "file content")
        text = wm.render_text()
        assert "fix bug" in text
        assert "read_file" in text

    def test_serialization_roundtrip(self):
        wm = WorkingMemory()
        wm.set_plan("test plan")
        wm.add_observation("tool", "obs")
        wm.add_hypothesis("hypothesis")
        wm.add_candidate("file.py")

        d = wm.to_dict()
        restored = WorkingMemory.from_dict(d)

        assert restored.plan == wm.plan
        assert len(restored.recent_observations) == 1
        assert restored.active_hypotheses == wm.active_hypotheses
        assert restored.candidate_targets == wm.candidate_targets


# === SemanticMemory ===


class TestSemanticMemory:
    def test_empty(self):
        sm = SemanticMemory()
        assert sm.count() == 0

    def test_put_and_get(self):
        sm = SemanticMemory()
        record = SemanticRecord(
            record_id="rec_1",
            category="file_summary",
            content="main entry point",
            repo_path="src/main.py",
        )
        sm.put(record)
        assert sm.count() == 1
        assert sm.get("rec_1").content == "main entry point"

    def test_put_updates_existing(self):
        sm = SemanticMemory()
        sm.put(SemanticRecord(record_id="rec_1", category="test", content="v1"))
        sm.put(SemanticRecord(record_id="rec_1", category="test", content="v2"))
        assert sm.count() == 1
        assert sm.get("rec_1").content == "v2"
        assert sm.get("rec_1").created_at  # preserved

    def test_delete(self):
        sm = SemanticMemory()
        sm.put(SemanticRecord(record_id="rec_1", category="test", content="x"))
        assert sm.delete("rec_1")
        assert sm.count() == 0
        assert not sm.delete("rec_1")  # already deleted

    def test_search_by_category(self):
        sm = SemanticMemory()
        sm.put(SemanticRecord(record_id="r1", category="file_summary", content="file A"))
        sm.put(SemanticRecord(record_id="r2", category="module_info", content="module B"))
        results = sm.search(category="file_summary")
        assert len(results) == 1
        assert results[0].category == "file_summary"

    def test_search_by_tags(self):
        sm = SemanticMemory()
        sm.put(SemanticRecord(record_id="r1", category="test", content="x", tags=["auth", "login"]))
        sm.put(SemanticRecord(record_id="r2", category="test", content="y", tags=["db"]))
        results = sm.search(tags=["auth"])
        assert len(results) == 1

    def test_search_by_query(self):
        sm = SemanticMemory()
        sm.put(SemanticRecord(record_id="r1", category="test", content="authentication module"))
        sm.put(SemanticRecord(record_id="r2", category="test", content="database layer"))
        results = sm.search(query="authentication")
        assert len(results) == 1

    def test_make_record_id(self):
        id1 = SemanticMemory.make_record_id("file_summary", "src/main.py")
        id2 = SemanticMemory.make_record_id("file_summary", "src/main.py")
        assert id1 == id2  # deterministic

    def test_serialization_roundtrip(self):
        sm = SemanticMemory()
        sm.put(SemanticRecord(record_id="r1", category="test", content="hello", tags=["a"]))
        d = sm.to_dict()
        restored = SemanticMemory.from_dict(d)
        assert restored.count() == 1
        assert restored.get("r1").content == "hello"


# === MemoryWriter ===


class TestMemoryWriter:
    def test_read_file_goes_to_working(self):
        writer = MemoryWriter()
        decision = writer.should_write("read_file", {"path": "README.md"}, "# Hello\nWorld")
        assert decision["target"] == WRITE_TARGET_WORKING
        assert decision["promote_to_semantic"] is True

    def test_write_file_goes_to_working(self):
        writer = MemoryWriter()
        decision = writer.should_write("write_file", {"path": "test.py"}, "code")
        assert decision["target"] == WRITE_TARGET_WORKING
        assert decision["category"] == "file_modified"

    def test_list_files_goes_to_working(self):
        writer = MemoryWriter()
        decision = writer.should_write("list_files", {"path": "."}, "[F] a.py\n[D] src")
        assert decision["target"] == WRITE_TARGET_WORKING
        assert decision["category"] == "observation"

    def test_unknown_tool_skip(self):
        writer = MemoryWriter()
        decision = writer.should_write("unknown", {}, "")
        assert decision["target"] == WRITE_TARGET_SKIP

    def test_write_working(self):
        writer = MemoryWriter()
        wm = WorkingMemory()
        decision = writer.should_write("read_file", {"path": "README.md"}, "# Hello")
        writer.write_working(wm, decision)
        assert len(wm.recent_observations) == 1
        assert "README.md" in wm.candidate_targets

    def test_write_semantic_promotes_file_summary(self):
        writer = MemoryWriter()
        sm = SemanticMemory()
        # 使用不包含 # 开头的正文，这样摘要会包含实际内容
        decision = writer.should_write("read_file", {"path": "README.md"}, "Project description\nLine two")
        assert decision.get("promote_to_semantic") is True
        writer.write_semantic(sm, decision)
        record = sm.search(query="Project", category="file_summary")
        assert len(record) == 1
        assert record[0].repo_path == "README.md"

    def test_write_semantic_invalidates_on_modify(self):
        writer = MemoryWriter()
        sm = SemanticMemory()
        # 先写一个摘要
        sm.put(SemanticRecord(
            record_id=SemanticMemory.make_record_id("file_summary", "README.md"),
            category="file_summary",
            content="old summary",
            repo_path="README.md",
        ))
        # 然后文件被修改
        decision = writer.should_write("write_file", {"path": "README.md"}, "new content")
        writer.write_semantic(sm, decision)
        # 旧摘要应被删除
        assert sm.count() == 0


# === MemoryRetriever ===


class TestMemoryRetriever:
    def test_recall_from_working(self):
        retriever = MemoryRetriever()
        wm = WorkingMemory()
        wm.set_task_summary("fix authentication bug")
        results = retriever.recall_for_task("fix auth bug", working_memory=wm)
        assert len(results) > 0

    def test_recall_from_semantic(self):
        retriever = MemoryRetriever()
        sm = SemanticMemory()
        sm.put(SemanticRecord(
            record_id="r1",
            category="file_summary",
            content="authentication module with JWT tokens",
            repo_path="src/auth.py",
        ))
        results = retriever.recall_for_task("authentication tokens", semantic_memory=sm)
        assert len(results) > 0
        assert results[0].source == "semantic"

    def test_recall_combined(self):
        retriever = MemoryRetriever()
        wm = WorkingMemory()
        wm.set_task_summary("fix login handler bug")
        sm = SemanticMemory()
        sm.put(SemanticRecord(
            record_id="r1",
            category="file_summary",
            content="login handler module",
            repo_path="src/login.py",
        ))
        results = retriever.recall_for_task("fix login handler", working_memory=wm, semantic_memory=sm)
        assert len(results) >= 2

    def test_recall_top_k(self):
        retriever = MemoryRetriever()
        sm = SemanticMemory()
        for i in range(10):
            sm.put(SemanticRecord(
                record_id=f"r{i}",
                category="test",
                content=f"item {i} with auth keyword",
            ))
        results = retriever.recall_for_task("auth", semantic_memory=sm, top_k=3)
        assert len(results) <= 3

    def test_compute_relevance(self):
        score = MemoryRetriever._compute_relevance("authentication module", "fix authentication bug")
        assert score > 0

        score_zero = MemoryRetriever._compute_relevance("database layer", "fix authentication bug")
        assert score_zero == 0.0


# === MemoryCompactor ===


class TestMemoryCompactor:
    def test_compact_removes_duplicates(self):
        compactor = MemoryCompactor()
        wm = WorkingMemory()
        wm.add_observation("tool", "same observation")
        wm.add_observation("tool", "same observation")
        wm.add_observation("tool", "different observation")
        report = compactor.compact_working_memory(wm)
        assert report["removed_duplicates"] >= 1

    def test_promote_to_semantic(self):
        compactor = MemoryCompactor()
        wm = WorkingMemory()
        sm = SemanticMemory()
        # 模拟多次读取同一文件
        wm.add_observation("read_file", "read src/auth.py: implements JWT auth")
        wm.add_observation("read_file", "read src/auth.py: has login handler")
        report = compactor.promote_to_semantic(wm, sm, "/repo")
        assert sm.count() >= 1

    def test_compact_and_promote_combined(self):
        compactor = MemoryCompactor()
        wm = WorkingMemory()
        sm = SemanticMemory()
        wm.add_observation("read_file", "read main.py: entry point")
        wm.add_observation("read_file", "read main.py: has app setup")
        wm.add_hypothesis("bug is in main.py")
        wm.add_hypothesis("bug is in main.py")
        report = compactor.compact_and_promote(wm, sm)
        assert "compaction" in report
        assert "promotion" in report


# === CompactionSchema (Phase 2) ===


class TestCompactionSchema:
    def test_build_schema_preserves_plan_obs_pending(self):
        wm = WorkingMemory()
        wm.set_plan("fix auth bug")
        wm.add_observation("read_file", "read auth.py: found token check issue")
        wm.add_pending("run tests")
        wm.add_hypothesis("token is None")
        schema = build_schema_from_working_memory(wm, "run-42", "fix auth")
        assert schema.final_goal == "fix auth bug"
        assert schema.original_request == "fix auth"
        assert schema.remaining_tasks == ["run tests"]
        assert "auth.py" in schema.files_observed

    def test_schema_to_semantic_records(self):
        wm = WorkingMemory()
        wm.set_plan("implement feature X")
        wm.add_observation("write_file", "success: wrote feature x.py")
        wm.add_pending("write tests")
        schema = build_schema_from_working_memory(wm, "run-99", "implement feature X")
        records = schema_to_semantic_records(schema)
        categories = [r[1] for r in records]
        assert "run_goal" in categories
        assert "run_summary" in categories
        # remaining_tasks 生成记录
        assert "remaining_tasks" in categories

    def test_compact_and_promote_v2_full_flow(self):
        compactor = MemoryCompactor()
        wm = WorkingMemory()
        sm = SemanticMemory()
        wm.set_plan("refactor utils.py")
        wm.add_observation("read_file", "read utils.py: helper functions")
        wm.add_observation("read_file", "read utils.py: 200 lines")
        wm.add_pending("check edge cases")
        report = compactor.compact_and_promote_v2(
            wm, sm, "run-v2-1", "refactor utils.py", "/tmp"
        )
        # 四个阶段都存在
        assert "flush" in report
        assert "compaction" in report
        assert "promotion" in report
        assert "structured" in report
        # structured 写入了数据
        assert report["structured"]["written_count"] > 0
        assert report["structured"]["written_items"]
        # flush 包含正确 run_id
        assert report["flush"]["run_id"] == "run-v2-1"

    def test_compact_and_promote_v2_skips_empty_schema(self):
        compactor = MemoryCompactor()
        wm = WorkingMemory()
        sm = SemanticMemory()
        report = compactor.compact_and_promote_v2(
            wm, sm, "run-empty", "no action", "/tmp"
        )
        # flush 存在但 structured 不写入
        assert report["structured"]["written_count"] == 0
