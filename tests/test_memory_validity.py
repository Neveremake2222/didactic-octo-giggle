"""记忆有效性 / Staleness 策略测试。

测试 FileFingerprintTracker、SemanticRecordValidityChecker、
StaleObservationGuard 的核心逻辑。
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

import pytest

from owl.memory_validity import (
    FileFingerprintTracker,
    SemanticRecordValidityChecker,
    ValidityResult,
)
from owl.stale_observation_guard import StaleObservationGuard, StaleObservation
from owl.semantic_memory import SemanticRecord, SemanticMemory
from owl.working_memory import WorkingMemory


# ---------------------------------------------------------------------------
# FileFingerprintTracker
# ---------------------------------------------------------------------------


class TestFileFingerprintTracker:
    def test_records_and_retrieves_fingerprint(self):
        tracker = FileFingerprintTracker()
        fp = tracker.record("/path/to/file.py", "hello world")
        assert len(fp) == 64  # SHA-256 hex digest
        assert tracker.get("/path/to/file.py") == fp

    def test_detects_change(self):
        tracker = FileFingerprintTracker()
        tracker.record("/path/to/file.py", "version 1")
        is_stale = tracker.check("/path/to/file.py", hashlib.sha256(b"version 2").hexdigest())
        assert is_stale is True

    def test_no_change_detected(self):
        tracker = FileFingerprintTracker()
        fp = tracker.record("/path/to/file.py", "same content")
        is_stale = tracker.check("/path/to/file.py", fp)
        assert is_stale is False

    def test_check_from_file_reads_actual(self):
        tracker = FileFingerprintTracker()
        tmp = tempfile.mkdtemp(prefix="owl_fp_")
        try:
            f = Path(tmp) / "test.txt"
            f.write_text("initial", encoding="utf-8")
            tracker.record(str(f), "initial")

            # 内容未变
            is_stale, _ = tracker.check_from_file(str(f))
            assert is_stale is False

            # 内容改变
            f.write_text("changed", encoding="utf-8")
            is_stale, _ = tracker.check_from_file(str(f))
            assert is_stale is True
        finally:
            shutil.rmtree(tmp)

    def test_unknown_record_not_stale(self):
        tracker = FileFingerprintTracker()
        # 没有记录 → 无法判定 → 不标记为过期
        is_stale = tracker.check("/nonexistent/file.py", "some_fp")
        assert is_stale is False

    def test_len(self):
        tracker = FileFingerprintTracker()
        assert len(tracker) == 0
        tracker.record("/a.py", "a")
        tracker.record("/b.py", "b")
        assert len(tracker) == 2


# ---------------------------------------------------------------------------
# SemanticRecordValidityChecker
# ---------------------------------------------------------------------------


class TestSemanticRecordValidityChecker:
    def test_valid_record(self):
        tracker = FileFingerprintTracker()
        checker = SemanticRecordValidityChecker()
        record = SemanticRecord(record_id="test", category="test", content="hello")
        result = checker.check_record(record, tracker)
        assert result.status == "VALID"
        assert result.suggested_action == "keep"

    def test_invalidated_record(self):
        tracker = FileFingerprintTracker()
        checker = SemanticRecordValidityChecker()
        record = SemanticRecord(record_id="test", category="test", content="hello")
        record.invalidate()
        result = checker.check_record(record, tracker)
        assert result.status == "INVALIDATED"
        assert result.suggested_action == "drop"

    def test_superseded_record(self):
        tracker = FileFingerprintTracker()
        checker = SemanticRecordValidityChecker()
        record = SemanticRecord(record_id="test", category="test", content="hello")
        record.supersede("new_record_id")
        result = checker.check_record(record, tracker)
        assert result.status == "SUPERSEDED"
        assert result.suggested_action == "drop"

    def test_stale_record_file_changed(self):
        tmp = tempfile.mkdtemp(prefix="owl_validity_")
        try:
            f = Path(tmp) / "module.py"
            f.write_text("version 1", encoding="utf-8")
            tracker = FileFingerprintTracker()
            tracker.record(str(f), "version 1")

            checker = SemanticRecordValidityChecker()
            record = SemanticRecord(
                record_id="test", category="file_summary",
                content="module summary", repo_path=str(f),
            )

            # 文件内容改变
            f.write_text("version 2", encoding="utf-8")
            result = checker.check_record(record, tracker)
            assert result.status == "STALE"
            assert result.suggested_action == "refresh"
        finally:
            shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# SemanticRecord Phase 2 字段
# ---------------------------------------------------------------------------


class TestSemanticRecordPhase2:
    def test_importance_score_roundtrip(self):
        record = SemanticRecord(
            record_id="test", category="test", content="hello",
            importance_score=0.75,
        )
        d = record.to_dict()
        assert d["importance_score"] == 0.75
        record2 = SemanticRecord.from_dict(d)
        assert record2.importance_score == 0.75

    def test_invalidate_sets_timestamp(self):
        record = SemanticRecord(record_id="test", category="test", content="hello")
        assert record.is_active() is True
        record.invalidate()
        assert record.is_active() is False
        assert record.invalidated_at != ""

    def test_supersede_sets_reference(self):
        record = SemanticRecord(record_id="old", category="test", content="hello")
        assert record.is_active() is True
        record.supersede("new_id")
        assert record.is_active() is False
        assert record.superseded_by == "new_id"


# ---------------------------------------------------------------------------
# StaleObservationGuard
# ---------------------------------------------------------------------------


class TestStaleObservationGuard:
    def test_detects_stale_observation(self):
        tmp = tempfile.mkdtemp(prefix="owl_stale_")
        try:
            f = Path(tmp) / "code.py"
            f.write_text("original", encoding="utf-8")

            tracker = FileFingerprintTracker()
            tracker.record(str(f), "original")

            wm = WorkingMemory()
            wm.add_observation("read_file", f"read {f}: summary",
                               file_path=str(f), file_fingerprint=tracker.get(str(f)))

            # 文件内容改变
            f.write_text("modified", encoding="utf-8")

            guard = StaleObservationGuard()
            stale = guard.check_working_memory(wm, tracker)
            assert len(stale) == 1
            assert stale[0].file_path == str(f)
        finally:
            shutil.rmtree(tmp)

    def test_removes_stale_observations(self):
        tmp = tempfile.mkdtemp(prefix="owl_stale_")
        try:
            f = Path(tmp) / "code.py"
            f.write_text("original", encoding="utf-8")

            tracker = FileFingerprintTracker()
            tracker.record(str(f), "original")

            wm = WorkingMemory()
            wm.add_observation("read_file", f"read {f}: old summary",
                               file_path=str(f))
            wm.add_observation("read_file", "read other.py: fresh summary")  # 无指纹

            f.write_text("changed", encoding="utf-8")

            guard = StaleObservationGuard()
            stale = guard.check_working_memory(wm, tracker)
            assert len(stale) == 1

            removed = guard.remove_stale(wm, stale)
            assert removed == 1
            assert len(wm.recent_observations) == 1  # 保留了另一条
        finally:
            shutil.rmtree(tmp)

    def test_no_stale_when_no_change(self):
        tmp = tempfile.mkdtemp(prefix="owl_stale_")
        try:
            f = Path(tmp) / "code.py"
            f.write_text("unchanged", encoding="utf-8")

            tracker = FileFingerprintTracker()
            tracker.record(str(f), "unchanged")

            wm = WorkingMemory()
            wm.add_observation("read_file", f"read {f}: summary",
                               file_path=str(f))

            guard = StaleObservationGuard()
            stale = guard.check_working_memory(wm, tracker)
            assert len(stale) == 0
        finally:
            shutil.rmtree(tmp)

    def test_extracts_path_from_summary(self):
        guard = StaleObservationGuard()
        assert guard._extract_path_from_summary("read src/main.py: entry point") == "src/main.py"
        assert guard._extract_path_from_summary("some random text") == ""
