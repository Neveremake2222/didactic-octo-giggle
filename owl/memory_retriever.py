"""统一记忆召回策略。

memory_retriever 的职责：
  - 不是全量拉回所有记忆
  - 根据当前任务按维度过滤后召回
  - 过滤维度：任务类型、repo 路径、最近性、重要性、相关性

所有记忆召回都应该经过这个模块。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .semantic_memory import SemanticMemory, SemanticRecord
from .working_memory import WorkingMemory
from .recall_ranker import RecallRanker, RecallReport
from .memory_utils import compute_relevance
from .memory_validity import FileFingerprintTracker, SemanticRecordValidityChecker


@dataclass
class RecallResult:
    """一次召回的结果。"""

    # 来源
    source: str  # "working" / "semantic" / "episodic"

    # 内容
    content: str

    # 关联路径
    repo_path: str = ""

    # 相关性得分（越高越相关）
    relevance_score: float = 0.0

    # Phase 4: 多维打分
    combined_score: float = 0.0
    freshness_score: float = 0.0
    importance_score: float = 0.0
    recall_rationale: str = ""

    # 来源记录的元信息
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "content": self.content,
            "repo_path": self.repo_path,
            "relevance_score": self.relevance_score,
            "combined_score": self.combined_score,
            "freshness_score": self.freshness_score,
            "importance_score": self.importance_score,
            "recall_rationale": self.recall_rationale,
            "metadata": dict(self.metadata),
        }


class MemoryRetriever:
    """统一记忆召回策略。

    使用方式：
      retriever = MemoryRetriever()
      results = retriever.recall_for_task(
          task="fix the bug in auth.py",
          working_memory=wm,
          semantic_memory=sm,
          top_k=5,
      )
    """

    def __init__(
        self,
        ranker: RecallRanker | None = None,
        quality_recall: bool = True,
        validity_checker: SemanticRecordValidityChecker | None = None,
        fingerprint_tracker: FileFingerprintTracker | None = None,
    ):
        self.ranker = ranker or RecallRanker()
        self.quality_recall = quality_recall
        self.validity_checker = validity_checker
        self.fingerprint_tracker = fingerprint_tracker

    def recall_for_task(
        self,
        task: str,
        working_memory: WorkingMemory | None = None,
        semantic_memory: SemanticMemory | None = None,
        top_k: int = 5,
        now_ts: str | None = None,
    ) -> list[RecallResult]:
        """根据当前任务召回相关记忆。

        参数：
          task            — 当前任务描述
          working_memory  — 当前 run 的工作记忆
          semantic_memory — 长期语义记忆
          top_k           — 最多返回多少条
          now_ts          — 当前时间 ISO timestamp（用于 freshness 计算）
        """
        results: list[RecallResult] = []

        # 1. 从 working memory 召回
        if working_memory:
            results.extend(self._recall_from_working(working_memory, task))

        # 2. 从 semantic memory 召回
        if semantic_memory:
            results.extend(self._recall_from_semantic(semantic_memory, task, now_ts, top_k))

        # 按相关性排序
        results.sort(key=lambda r: r.combined_score or r.relevance_score, reverse=True)

        return results[:top_k]

    def _recall_from_working(
        self, wm: WorkingMemory, task: str
    ) -> list[RecallResult]:
        """从 working memory 召回。"""
        results: list[RecallResult] = []

        # 任务摘要
        if wm.task_summary:
            score = self._compute_relevance(wm.task_summary, task)
            if score > 0:
                results.append(RecallResult(
                    source="working",
                    content=wm.task_summary,
                    relevance_score=score + 1.0,  # working memory 加权
                    metadata={"kind": "task_summary"},
                ))

        # 最近观察
        for obs in wm.recent_observations:
            score = self._compute_relevance(obs.summary, task)
            if score > 0:
                results.append(RecallResult(
                    source="working",
                    content=obs.summary,
                    relevance_score=score + 0.5,
                    repo_path=getattr(obs, "file_path", ""),
                    metadata={
                        "kind": "observation",
                        "tool_name": getattr(obs, "tool_name", ""),
                        "observation_id": getattr(obs, "observation_id", ""),
                    },
                ))

        # 候选目标
        for target in wm.candidate_targets:
            score = self._compute_relevance(target, task)
            if score > 0:
                results.append(RecallResult(
                    source="working",
                    content=target,
                    repo_path=target,
                    relevance_score=score + 0.3,
                    metadata={"kind": "candidate_target"},
                ))

        return results

    def _recall_from_semantic(
        self, sm: SemanticMemory, task: str, now_ts: str | None = None, top_k: int = 5,
    ) -> list[RecallResult]:
        """从 semantic memory 召回（支持 quality-aware 排序）。"""
        results: list[RecallResult] = []

        raw_records = sm.search(query=task, top_k=top_k * 3 if self.quality_recall and now_ts else 10)
        if self.validity_checker is not None:
            valid_records = []
            for record in raw_records:
                validity = self.validity_checker.check_record(record, self.fingerprint_tracker)
                if validity.suggested_action == "keep":
                    valid_records.append(record)
            raw_records = valid_records

        if self.quality_recall and now_ts:
            # Phase 4: 过量获取，再由 ranker 精排
            report = self.ranker.rank(raw_records, task, now_ts, top_k=top_k)
            for item in report.items:
                results.append(RecallResult(
                    source="semantic",
                    content=item.content,
                    relevance_score=item.relevance_score,
                    combined_score=item.combined_score,
                    freshness_score=item.freshness_score,
                    importance_score=item.importance_score,
                    recall_rationale=item.recall_rationale,
                    metadata={"record_id": item.record_id},
                ))
        else:
            # Phase 1 fallback: 简单 token overlap
            for record in raw_records:
                score = self._compute_relevance(record.content, task)
                if score > 0:
                    results.append(RecallResult(
                        source="semantic",
                        content=record.content,
                        repo_path=record.repo_path,
                        relevance_score=score,
                        metadata={
                            "record_id": record.record_id,
                            "category": record.category,
                            "file_path": record.file_path,
                            "source_run_id": record.source_run_id,
                        },
                    ))

        return results

    @staticmethod
    def _compute_relevance(text: str, query: str) -> float:
        """简单的关键词重叠相关性计算。

        不使用 embedding，只做 token 级别的重叠度。
        已统一使用 memory_utils.compute_relevance()。
        """
        return compute_relevance(text, query)
