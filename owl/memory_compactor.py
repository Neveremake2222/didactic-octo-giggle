"""Compact working memory and promote durable records to semantic memory.

`MemoryCompactor` is the bridge between per-run `WorkingMemory` and cross-run
`SemanticMemory`. It removes duplicate working-memory items, promotes stable
file summaries, writes structured run summaries, and detects reusable procedure
candidates for the skill registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .compaction_schema import (
    CompactionSchema,
    build_schema_from_working_memory,
    schema_to_semantic_records,
)
from .memory_config import MIN_OBSERVATIONS_FOR_PROMOTION
from .memory_utils import extract_path_from_observation, file_fingerprint
from .procedure_candidate_detector import ProcedureCandidateDetector
from .semantic_memory import SemanticMemory, SemanticRecord
from .working_memory import WorkingMemory


class MemoryCompactor:
    """Compact working memory and write selected records to semantic memory."""

    def __init__(self):
        self._procedure_detector = ProcedureCandidateDetector()

    def compact_working_memory(self, wm: WorkingMemory) -> dict[str, Any]:
        """Remove duplicate working-memory observations and candidate fields."""
        before_obs = len(wm.recent_observations)
        before_hyp = len(wm.active_hypotheses)

        seen_summaries: set[str] = set()
        unique_obs = []
        for obs in wm.recent_observations:
            key = obs.summary[:100]
            if key not in seen_summaries:
                seen_summaries.add(key)
                unique_obs.append(obs)
        wm.recent_observations = unique_obs

        wm.active_hypotheses = list(dict.fromkeys(wm.active_hypotheses))
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
        """Promote stable file-backed working observations to semantic memory.

        A file summary is promoted when the file has enough observations, has a
        recent read summary, and is not identical to the existing semantic
        record. Promoted records include file fingerprints so later recall can
        reject stale memory.
        """
        promoted_count = 0
        skipped_count = 0
        promoted_items: list[str] = []

        path_obs_count: dict[str, int] = {}
        path_latest_summary: dict[str, str] = {}
        modified_paths: set[str] = set()

        for obs in wm.recent_observations:
            path = self._extract_path_from_observation(obs)
            if not path:
                continue

            path_obs_count[path] = path_obs_count.get(path, 0) + 1

            if "write" in obs.tool_name or "patch" in obs.tool_name:
                modified_paths.add(path)

            if "read" in obs.tool_name:
                path_latest_summary[path] = obs.summary

        for path, count in path_obs_count.items():
            if path in modified_paths:
                skipped_count += 1
                continue

            if count < MIN_OBSERVATIONS_FOR_PROMOTION:
                skipped_count += 1
                continue

            summary = path_latest_summary.get(path, "")
            if not summary or "error" in summary.lower():
                skipped_count += 1
                continue

            record_id = SemanticMemory.make_record_id("file_summary", path)
            existing = sm.get(record_id)

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
        """Compact working memory, then promote eligible file summaries."""
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
        """Extract a repo path from an observation using the shared utility."""
        return extract_path_from_observation(obs)

    def apply_write_intents(
        self,
        semantic_memory: SemanticMemory,
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply semantic side effects described by a write decision.

        This is intentionally narrow: tool execution can invalidate stale
        semantic records immediately, while semantic promotion remains reserved
        for final compaction.
        """
        invalidated_count = 0
        invalidated_paths: list[str] = []

        for path in decision.get("invalidate_paths", []) or []:
            path = str(path).strip()
            if not path:
                continue
            absolute_path = str(decision.get("absolute_path", "")).strip()
            new_version = file_fingerprint(absolute_path or path)
            count = semantic_memory.invalidate_by_file(path, new_version=new_version or None)
            invalidated_count += count
            if count:
                invalidated_paths.append(path)

        return {
            "semantic_invalidated_count": invalidated_count,
            "semantic_invalidated_paths": invalidated_paths,
        }

    def pre_compaction_flush(
        self,
        wm: WorkingMemory,
        run_id: str,
        original_request: str,
    ) -> CompactionSchema:
        """Convert working memory into a structured compaction schema."""
        return build_schema_from_working_memory(wm, run_id, original_request)

    def structured_compaction(
        self,
        schema: CompactionSchema,
        semantic_memory: SemanticMemory,
        workspace_root: str = "",
    ) -> dict[str, Any]:
        """Write structured run-level records from a compaction schema."""
        if not schema.is_meaningful():
            return {
                "written_count": 0,
                "skipped_count": 0,
                "deduped_count": 0,
                "written_items": [],
            }

        records = schema_to_semantic_records(schema)
        written_count = 0
        skipped_count = 0
        deduped_count = 0
        written_items: list[str] = []

        for record_id, category, content, tags in records:
            if not content.strip():
                skipped_count += 1
                continue

            existing = semantic_memory.get(record_id)
            if existing and existing.content == content:
                skipped_count += 1
                continue

            if self._is_near_duplicate(content, category, semantic_memory):
                deduped_count += 1
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
            "deduped_count": deduped_count,
            "written_items": written_items,
            "schema_summary": schema.summary_text,
        }

    @staticmethod
    def _is_near_duplicate(
        content: str,
        category: str,
        semantic_memory: SemanticMemory,
        threshold: float = 0.8,
    ) -> bool:
        """Detect near duplicates in one semantic category by token overlap."""
        content_tokens = set(content.lower().split())
        if not content_tokens:
            return False

        existing_records = semantic_memory.search(category=category, top_k=20)
        for record in existing_records:
            record_tokens = set(record.content.lower().split())
            if not record_tokens:
                continue
            overlap = len(content_tokens & record_tokens) / max(len(content_tokens), len(record_tokens))
            if overlap >= threshold:
                return True
        return False

    def compact_and_promote_v2(
        self,
        wm: WorkingMemory,
        sm: SemanticMemory,
        run_id: str,
        original_request: str,
        workspace_root: str = "",
    ) -> dict[str, Any]:
        """Run the full structured compaction pipeline.

        Steps:

        1. Build a structured schema from working memory.
        2. De-duplicate working memory.
        3. Promote stable file summaries.
        4. Write structured run summary records.
        """
        schema = self.pre_compaction_flush(wm, run_id, original_request)
        compact_report = self.compact_working_memory(wm)
        promote_report = self.promote_to_semantic(wm, sm, workspace_root)
        structured_report = self.structured_compaction(schema, sm, workspace_root)

        return {
            "flush": schema.to_dict(),
            "compaction": compact_report,
            "promotion": promote_report,
            "structured": structured_report,
        }

    def detect_procedure_candidates(
        self,
        wm: WorkingMemory,
        run_id: str,
        registry: Any = None,
    ) -> list:
        """Detect reusable procedure candidates from working memory.

        When a registry is provided, candidates are persisted with their trigger
        conditions, anti-patterns, procedure steps, and applicable repo paths.
        """
        candidates = self._procedure_detector.detect_from_working_memory(wm, run_id)
        if registry and candidates:
            for c in candidates:
                registry.register(
                    pattern_type=c.pattern_type,
                    description=c.description,
                    run_id=run_id,
                    procedure_steps=c.procedure_steps,
                    trigger_conditions=c.trigger_conditions,
                    anti_patterns=c.anti_patterns,
                    applicable_repo_paths=c.applicable_repo_paths,
                )
        return candidates
