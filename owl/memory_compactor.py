"""压缩、整合、沉淀长期记忆。

memory_compactor 的职责：
  - 在 ask() 结束时决定哪些 working memory 内容值得沉淀成长期记忆
  - 压缩冗余信息
  - 删除过期信息（freshness 不匹配的）
  - 防止噪声沉淀（一次性失败、短期中间结果不允许进 semantic memory）

compactor 是 working → semantic 的唯一桥梁。
不允许任意原文直接沉淀进长期记忆。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .semantic_memory import SemanticMemory, SemanticRecord
from .working_memory import WorkingMemory
from .compaction_schema import (
    CompactionSchema,
    build_schema_from_working_memory,
    schema_to_semantic_records,
)
from .procedure_candidate_detector import ProcedureCandidateDetector
from .memory_utils import extract_path_from_observation, file_fingerprint


from .memory_config import MIN_OBSERVATIONS_FOR_PROMOTION


class MemoryCompactor:
    """压缩和整合记忆。

    使用方式：
      compactor = MemoryCompactor()
      report = compactor.compact_and_promote(
          working_memory=wm,
          semantic_memory=sm,
          workspace_root="/path/to/repo",
      )
    """

    def compact_working_memory(self, wm: WorkingMemory) -> dict[str, Any]:
        """压缩 working memory，移除冗余信息。

        返回压缩报告：
          - 移除了多少条目
          - 保留了什么
        """
        before_obs = len(wm.recent_observations)
        before_hyp = len(wm.active_hypotheses)
        before_cand = len(wm.candidate_targets)
        before_pend = len(wm.pending_verifications)

        # 去除重复观察
        seen_summaries: set[str] = set()
        unique_obs = []
        for obs in wm.recent_observations:
            key = obs.summary[:100]
            if key not in seen_summaries:
                seen_summaries.add(key)
                unique_obs.append(obs)
        wm.recent_observations = unique_obs

        # 去除重复假设
        wm.active_hypotheses = list(dict.fromkeys(wm.active_hypotheses))

        # 去除重复候选
        wm.candidate_targets = list(dict.fromkeys(wm.candidate_targets))

        after_obs = len(wm.recent_observations)
        after_hyp = len(wm.active_hypotheses)

        return {
            "observations_before": before_obs,
            "observations_after": after_obs,
            "hypotheses_before": before_hyp,
            "hypotheses_after": after_hyp,
            "removed_duplicates": (before_obs - after_obs) + (before_hyp - after_hyp),
        }

    def promote_to_semantic(
        self,
        wm: WorkingMemory,
        sm: SemanticMemory,
        workspace_root: str = "",
    ) -> dict[str, Any]:
        """将 working memory 中值得长期保留的信息沉淀到 semantic memory。

        条件：
          - 观察中包含文件摘要，且被观察 >= MIN_OBSERVATIONS_FOR_PROMOTION 次
          - 不是错误信息
          - 不是一次性中间结果

        返回沉淀报告。
        """
        promoted_count = 0
        skipped_count = 0
        promoted_items: list[str] = []

        # 统计每个路径被观察的次数
        path_obs_count: dict[str, int] = {}
        path_latest_summary: dict[str, str] = {}

        for obs in wm.recent_observations:
            # 从观察中提取路径信息
            path = self._extract_path_from_observation(obs)
            if not path:
                continue

            path_obs_count[path] = path_obs_count.get(path, 0) + 1

            # 保留最新的摘要
            if "read" in obs.tool_name:
                path_latest_summary[path] = obs.summary

        # 沉淀满足条件的路径
        for path, count in path_obs_count.items():
            if count < MIN_OBSERVATIONS_FOR_PROMOTION:
                skipped_count += 1
                continue

            summary = path_latest_summary.get(path, "")
            if not summary or "error" in summary.lower():
                skipped_count += 1
                continue

            record_id = SemanticMemory.make_record_id("file_summary", path)
            existing = sm.get(record_id)

            # 如果已有记录，检查是否需要更新
            if existing and existing.content == summary:
                skipped_count += 1
                continue

            absolute_path = str((Path(workspace_root) / path).resolve()) if workspace_root else path
            current_fp = file_fingerprint(absolute_path)

            sm.put(SemanticRecord(
                record_id=record_id,
                category="file_summary",
                content=summary,
                repo_path=path,
                file_path=path,
                tags=["file_summary", path],
                freshness_hash=current_fp,
                file_version=current_fp,
                importance_score=1.0,
            ))
            promoted_count += 1
            promoted_items.append(path)

        return {
            "promoted_count": promoted_count,
            "skipped_count": skipped_count,
            "promoted_items": promoted_items,
        }

    def compact_and_promote(
        self,
        working_memory: WorkingMemory,
        semantic_memory: SemanticMemory,
        workspace_root: str = "",
    ) -> dict[str, Any]:
        """一次完整的压缩 + 沉淀流程。

        先压缩 working memory（去重），再尝试沉淀到 semantic memory。
        """
        compact_report = self.compact_working_memory(working_memory)
        promote_report = self.promote_to_semantic(
            working_memory, semantic_memory, workspace_root
        )
        return {
            "compaction": compact_report,
            "promotion": promote_report,
        }

    @staticmethod
    def _extract_path_from_observation(obs: Any) -> str:
        """从观察记录中提取文件路径（委托到 memory_utils）。"""
        return extract_path_from_observation(obs)


# ---------------------------------------------------------------------------
# 两段式压缩（Phase 2）
# ---------------------------------------------------------------------------

    def pre_compaction_flush(
        self,
        wm: WorkingMemory,
        run_id: str,
        original_request: str,
    ) -> CompactionSchema:
        """Phase 1：快照 working memory 高价值状态。

        纯函数，不修改 wm。生成一个 CompactionSchema，
        其中包含 original_request / final_goal / completed_work /
        remaining_tasks 等关键骨架。

        Returns:
            CompactionSchema 实例。
        """
        return build_schema_from_working_memory(wm, run_id, original_request)

    def structured_compaction(
        self,
        schema: CompactionSchema,
        semantic_memory: SemanticMemory,
        workspace_root: str = "",
    ) -> dict[str, Any]:
        """Phase 2：将 CompactionSchema 按固定 schema 写入 semantic memory。

        每个 schema 字段都按 category 生成独立的 SemanticRecord：
          - run_goal
          - completed_work（每条一个）
          - remaining_tasks（每条一个）
          - run_summary

        Returns:
            写入报告，包含 written_count / skipped_count / written_items。
        """
        if not schema.is_meaningful():
            return {
                "written_count": 0,
                "skipped_count": 0,
                "written_items": [],
            }

        records = schema_to_semantic_records(schema)
        written_count = 0
        skipped_count = 0
        written_items: list[str] = []

        for record_id, category, content, tags in records:
            # 跳过空内容
            if not content.strip():
                skipped_count += 1
                continue

            # 检查是否已存在且内容相同（跳过无意义更新）
            existing = semantic_memory.get(record_id)
            if existing and existing.content == content:
                skipped_count += 1
                continue

            semantic_memory.put(SemanticRecord(
                record_id=record_id,
                category=category,
                content=content,
                repo_path=workspace_root,
                tags=tags,
                source_run_id=schema.run_id,
                freshness_hash="",
            ))
            written_count += 1
            written_items.append(f"{category}:{record_id}")

        return {
            "written_count": written_count,
            "skipped_count": skipped_count,
            "written_items": written_items,
            "schema_summary": schema.summary_text,
        }

    def compact_and_promote_v2(
        self,
        wm: WorkingMemory,
        sm: SemanticMemory,
        run_id: str,
        original_request: str,
        workspace_root: str = "",
    ) -> dict[str, Any]:
        """完整两段式压缩流程。

        顺序执行：
          1. pre_compaction_flush — 快照高价值状态
          2. compact_working_memory — 去重压缩 working memory
          3. promote_to_semantic — 沉淀文件摘要到 semantic memory
          4. structured_compaction — 按 schema 写入结构化总结

        比 compact_and_promote() 多执行第 1 步和第 4 步，
        保证长任务跨轮次时上下文不漂移。

        Returns:
            包含四个阶段报告的合并 dict：
              flush: CompactionSchema (as dict)
              compaction: compact_working_memory report
              promotion: promote_to_semantic report
              structured: structured_compaction report
        """
        # Phase 1: 快照
        schema = self.pre_compaction_flush(wm, run_id, original_request)

        # Phase 2: 去重压缩
        compact_report = self.compact_working_memory(wm)

        # Phase 3: 沉淀文件摘要
        promote_report = self.promote_to_semantic(wm, sm, workspace_root)

        # Phase 4: 结构化沉淀
        structured_report = self.structured_compaction(schema, sm, workspace_root)

        return {
            "flush": schema.to_dict(),
            "compaction": compact_report,
            "promotion": promote_report,
            "structured": structured_report,
        }

    # -------------------------------------------------------------------------
    # Phase 2: Procedure Detection
    # -------------------------------------------------------------------------

    def __init__(self):
        self._procedure_detector = ProcedureCandidateDetector()

    def detect_procedure_candidates(
        self,
        wm: WorkingMemory,
        run_id: str,
        registry: Any = None,
    ) -> list:
        """检测 working memory 中的程序性经验候选。

        参数：
          wm       — WorkingMemory 实例
          run_id   — 当前 run ID
          registry — SkillCandidateRegistry（如果提供，自动注册检测到的候选）
        """
        candidates = self._procedure_detector.detect_from_working_memory(wm, run_id)
        if registry and candidates:
            for c in candidates:
                registry.register(c.pattern_type, c.description, run_id, c.procedure_steps)
        return candidates
