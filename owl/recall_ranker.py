"""多维 Recall 排序器。

对 semantic memory 召回结果进行四维排序：
  - relevance:    token overlap（与查询的语义相关度）
  - freshness:    指数衰减（越新越重要）
  - importance:   直接读取 record.importance_score
  - diversity:    MMR 惩罚（>85% 相似的记录被去重）

排序流程：
  1. 对所有候选记录四维打分
  2. MMR 去重（惩罚与已选记录高度相似的候选项）
  3. 加权求和
  4. 过滤 stale（由 memory_validity 判定）
  5. 返回 top_k

输出不仅包含排序结果，还包含 recall rationale（为什么被召回）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .semantic_memory import SemanticRecord
from .memory_utils import compute_relevance, compute_similarity
from .memory_config import (
    DEFAULT_FRESHNESS_HALFLIFE,
    DEFAULT_MMR_LAMBDA,
    DEFAULT_WEIGHTS,
    SIMILARITY_THRESHOLD,
)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class RecallRankingResult:
    """单条记录的多维排序结果。"""

    record_id: str
    content: str
    relevance_score: float = 0.0
    freshness_score: float = 0.0
    importance_score: float = 0.0
    diversity_score: float = 0.0
    combined_score: float = 0.0
    recall_rationale: str = ""
    deduplicated: bool = False
    skipped_stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "content": self.content[:200],
            "relevance_score": round(self.relevance_score, 4),
            "freshness_score": round(self.freshness_score, 4),
            "importance_score": round(self.importance_score, 4),
            "diversity_score": round(self.diversity_score, 4),
            "combined_score": round(self.combined_score, 4),
            "recall_rationale": self.recall_rationale,
            "deduplicated": self.deduplicated,
            "skipped_stale": self.skipped_stale,
        }


@dataclass
class RecallReport:
    """完整召回报告。"""

    items: list[RecallRankingResult] = field(default_factory=list)
    total_candidates: int = 0
    deduplicated_count: int = 0
    stale_skipped_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_candidates": self.total_candidates,
            "deduplicated_count": self.deduplicated_count,
            "stale_skipped_count": self.stale_skipped_count,
            "items": [item.to_dict() for item in self.items],
        }


# ---------------------------------------------------------------------------
# RecallRanker
# ---------------------------------------------------------------------------


class RecallRanker:
    """多维 recall 排序器。

    使用方式：
      ranker = RecallRanker()
      report = ranker.rank(records, query="fix auth bug", now_ts=now, top_k=5)
    """

    def __init__(
        self,
        freshness_halflife_secs: float = DEFAULT_FRESHNESS_HALFLIFE,
        mmr_lambda: float = DEFAULT_MMR_LAMBDA,
        weights: dict[str, float] | None = None,
    ):
        self.freshness_halflife = freshness_halflife_secs
        self.mmr_lambda = mmr_lambda
        self.weights = weights or dict(DEFAULT_WEIGHTS)

    def rank(
        self,
        records: list[SemanticRecord],
        query: str,
        now_ts: str,
        top_k: int = 5,
    ) -> RecallReport:
        """对候选记录进行四维排序。

        参数：
          records — 候选 SemanticRecord 列表
          query   — 当前任务查询文本
          now_ts  — 当前时间 ISO timestamp
          top_k   — 最多返回多少条
        """
        now_dt = datetime.fromisoformat(now_ts.replace("Z", "+00:00"))
        total_candidates = len(records)

        # 过滤 stale / inactive 记录
        active_records = []
        stale_skipped = 0
        for r in records:
            if not getattr(r, "is_active", lambda: True)():
                stale_skipped += 1
                continue
            active_records.append(r)

        # 四维打分
        scored: list[RecallRankingResult] = []
        for r in active_records:
            rel = self._compute_relevance(r.content, query)
            fresh = self._compute_freshness(r.created_at, now_dt)
            imp = getattr(r, "importance_score", 1.0)
            # diversity 初始为 1.0（在 MMR 阶段更新）
            combined = (
                self.weights.get("relevance", 0.4) * rel
                + self.weights.get("freshness", 0.25) * fresh
                + self.weights.get("importance", 0.2) * imp
                + self.weights.get("diversity", 0.15) * 1.0  # 初始
            )
            rationale = self._build_rationale(rel, fresh, imp)
            scored.append(RecallRankingResult(
                record_id=r.record_id,
                content=r.content,
                relevance_score=rel,
                freshness_score=fresh,
                importance_score=imp,
                diversity_score=1.0,
                combined_score=combined,
                recall_rationale=rationale,
            ))

        # 按 combined_score 降序排序
        scored.sort(key=lambda x: x.combined_score, reverse=True)

        # MMR 去重
        selected: list[RecallRankingResult] = []
        deduplicated_count = 0
        for candidate in scored:
            if len(selected) >= top_k:
                break

            # 计算与已选记录的最大相似度
            max_sim = 0.0
            for sel in selected:
                sim = self._compute_similarity(candidate.content, sel.content)
                max_sim = max(max_sim, sim)

            # 如果与已选记录太相似 → 去重
            if max_sim > SIMILARITY_THRESHOLD and selected:
                deduplicated_count += 1
                candidate.deduplicated = True
                # 不直接跳过，而是降权
                candidate.diversity_score = max(0.0, 1.0 - max_sim)
                candidate.combined_score = (
                    self.weights.get("relevance", 0.4) * candidate.relevance_score
                    + self.weights.get("freshness", 0.25) * candidate.freshness_score
                    + self.weights.get("importance", 0.2) * candidate.importance_score
                    + self.weights.get("diversity", 0.15) * candidate.diversity_score
                )
                # 降权后如果仍然在前 top_k，可以保留
                if candidate.combined_score < 0.1:
                    continue

            selected.append(candidate)

        return RecallReport(
            items=selected[:top_k],
            total_candidates=total_candidates,
            deduplicated_count=deduplicated_count,
            stale_skipped_count=stale_skipped,
        )

    # -------------------------------------------------------------------------
    # 打分函数
    # -------------------------------------------------------------------------

    @staticmethod
    def _compute_relevance(text: str, query: str) -> float:
        """Token overlap 相关性（已统一到 memory_utils）。"""
        return compute_relevance(text, query)

    def _compute_freshness(self, created_at: str, now_dt: datetime) -> float:
        """指数衰减新鲜度。1.0 = 刚创建，~0.5 = 一个半衰期。"""
        if not created_at:
            return 0.5  # 未知时间 → 中等
        try:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_secs = max(0, (now_dt - created_dt).total_seconds())
        except (ValueError, TypeError):
            return 0.5
        if age_secs <= 0:
            return 1.0
        return 0.5 ** (age_secs / self.freshness_halflife)

    @staticmethod
    def _build_rationale(relevance: float, freshness: float, importance: float) -> str:
        """构建召回原因描述。"""
        parts = []
        if relevance > 0.5:
            parts.append("high relevance")
        elif relevance > 0:
            parts.append("partial relevance")
        if freshness > 0.7:
            parts.append("recent")
        elif freshness < 0.3:
            parts.append("stale")
        if importance > 0.8:
            parts.append("high importance")
        return "; ".join(parts) if parts else "low match"

    @staticmethod
    def _compute_similarity(text1: str, text2: str) -> float:
        """Jaccard 相似度（已统一到 memory_utils）。"""
        return compute_similarity(text1, text2)
