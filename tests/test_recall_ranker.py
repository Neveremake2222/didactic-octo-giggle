"""Recall Ranker 多维排序测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from owl.recall_ranker import RecallRanker, RecallRankingResult, RecallReport
from owl.semantic_memory import SemanticRecord, SemanticMemory
from owl.memory_retriever import MemoryRetriever


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_record(content: str, created_at: str = "", importance: float = 1.0) -> SemanticRecord:
    return SemanticRecord(
        record_id=content[:10],
        category="test",
        content=content,
        created_at=created_at or _now_iso(),
        importance_score=importance,
    )


class TestRecallRankerFreshness:
    def test_recent_gets_high_score(self):
        ranker = RecallRanker(freshness_halflife_secs=3600)
        now = _now_iso()
        record = _make_record("test content", created_at=now)
        report = ranker.rank([record], "test content", now, top_k=5)
        assert report.items[0].freshness_score > 0.99

    def test_stale_gets_low_score(self):
        ranker = RecallRanker(freshness_halflife_secs=3600)
        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=10)).isoformat()
        now_iso = now.isoformat()
        record = _make_record("test content", created_at=old)
        report = ranker.rank([record], "test content", now_iso, top_k=5)
        assert report.items[0].freshness_score < 0.1


class TestRecallRankerDeduplication:
    def test_similar_records_deduplicated(self):
        # 使用较低阈值的 ranker 以确保去重触发
        ranker = RecallRanker()
        # 降低相似度阈值以便在短文本上触发去重
        from owl import recall_ranker
        original_threshold = recall_ranker.SIMILARITY_THRESHOLD
        recall_ranker.SIMILARITY_THRESHOLD = 0.7
        try:
            now = _now_iso()
            r1 = _make_record("the quick brown fox jumps over the lazy dog")
            r2 = _make_record("the quick brown fox jumps over the lazy cat")
            report = ranker.rank([r1, r2], "quick brown fox", now, top_k=5)
            assert report.deduplicated_count >= 1
        finally:
            recall_ranker.SIMILARITY_THRESHOLD = original_threshold

    def test_dissimilar_records_not_deduplicated(self):
        ranker = RecallRanker()
        now = _now_iso()
        r1 = _make_record("authentication module handles user login")
        r2 = _make_record("database connection pool configuration settings")
        report = ranker.rank([r1, r2], "authentication database", now, top_k=5)
        assert report.deduplicated_count == 0


class TestRecallRankerCombinedScore:
    def test_weights_sum_correctly(self):
        ranker = RecallRanker(
            weights={"relevance": 0.4, "freshness": 0.25, "importance": 0.2, "diversity": 0.15}
        )
        now = _now_iso()
        # 高 relevance（query 与 content 完全匹配的 token）
        record = _make_record("authentication module implementation", importance=0.8)
        report = ranker.rank([record], "authentication module", now, top_k=5)
        item = report.items[0]
        # combined = 0.4*rel + 0.25*fresh + 0.2*imp + 0.15*div
        expected = (
            0.4 * item.relevance_score
            + 0.25 * item.freshness_score
            + 0.2 * item.importance_score
            + 0.15 * item.diversity_score
        )
        assert abs(item.combined_score - expected) < 0.01


class TestRecallRankerTopK:
    def test_respects_top_k_limit(self):
        ranker = RecallRanker()
        now = _now_iso()
        records = [_make_record(f"document number {i} about testing") for i in range(20)]
        report = ranker.rank(records, "testing document", now, top_k=5)
        assert len(report.items) <= 5
        assert report.total_candidates == 20


class TestRecallRankerIntegration:
    def test_enhanced_recall_through_retriever(self):
        """End-to-end: MemoryRetriever + RecallRanker。"""
        sm = SemanticMemory()
        sm.put(SemanticRecord(
            record_id="rec1", category="file_summary",
            content="authentication module handles user login and token validation",
            tags=["auth"], importance_score=0.9,
        ))
        sm.put(SemanticRecord(
            record_id="rec2", category="file_summary",
            content="database connection pool manages PostgreSQL connections",
            tags=["db"], importance_score=0.7,
        ))

        retriever = MemoryRetriever(quality_recall=True)
        results = retriever.recall_for_task(
            task="fix authentication bug",
            semantic_memory=sm,
            top_k=2,
            now_ts=_now_iso(),
        )

        assert len(results) >= 1
        # authentication 应排在 database 前面
        if len(results) >= 2:
            auth_scores = [r.combined_score for r in results if "auth" in r.content.lower()]
            db_scores = [r.combined_score for r in results if "database" in r.content.lower()]
            if auth_scores and db_scores:
                assert auth_scores[0] >= db_scores[0]
