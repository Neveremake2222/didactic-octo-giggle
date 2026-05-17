"""Memory write policy for tool results.

`MemoryWriter` decides whether a tool result should become a working-memory
observation, a file modification intent, or be skipped. Runtime code should
write tool results to `WorkingMemory`; durable semantic writes are handled by
`MemoryCompactor`.
"""

from __future__ import annotations

from typing import Any

from .memory_utils import file_fingerprint, summarize_result
from .semantic_memory import SemanticMemory, SemanticRecord
from .working_memory import WorkingMemory


WRITE_TARGET_WORKING = "working"
WRITE_TARGET_SEMANTIC = "semantic"
WRITE_TARGET_SKIP = "skip"


class MemoryWriter:
    """Classify tool results and write the working-memory side effects."""

    def should_write(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a write decision for one tool result."""
        context = context or {}
        path = args.get("path", "")

        if tool_name in ("list_files", "search"):
            return {
                "target": WRITE_TARGET_WORKING,
                "reason": "navigation result, keep in working memory as observation",
                "content": str(result)[:300],
                "category": "observation",
                "tool_name": tool_name,
                "args": args,
            }

        if tool_name in ("read_file", "write_file", "patch_file"):
            if not path:
                return {"target": WRITE_TARGET_SKIP, "reason": "no path", "content": "", "category": ""}

            if tool_name == "read_file":
                summary = self._summarize_result(result, limit=180)
                return {
                    "target": WRITE_TARGET_WORKING,
                    "reason": "file read, summarize to working memory",
                    "content": summary,
                    "category": "file_summary",
                    "tool_name": tool_name,
                    "args": args,
                    "path": path,
                    "absolute_path": args.get("absolute_path", ""),
                    "promote_to_semantic": True,
                }

            return {
                "target": WRITE_TARGET_WORKING,
                "reason": "file modified, invalidate old summaries",
                "content": "",
                "category": "file_modified",
                "tool_name": tool_name,
                "args": args,
                "path": path,
                "absolute_path": args.get("absolute_path", ""),
                "promote_to_semantic": False,
                "invalidate_paths": [path],
            }

        if tool_name == "run_shell":
            return {
                "target": WRITE_TARGET_WORKING,
                "reason": "shell execution observation",
                "content": str(result)[:300],
                "category": "observation",
                "tool_name": tool_name,
                "args": args,
            }

        if tool_name == "delegate":
            return {
                "target": WRITE_TARGET_WORKING,
                "reason": "delegate investigation result",
                "content": str(result)[:300],
                "category": "observation",
                "tool_name": tool_name,
                "args": args,
            }

        return {"target": WRITE_TARGET_SKIP, "reason": "unknown tool", "content": "", "category": ""}

    def write_working(self, wm: WorkingMemory, decision: dict[str, Any]) -> None:
        """Apply the working-memory side of a write decision."""
        category = decision.get("category", "")
        content = decision.get("content", "")
        tool_name = decision.get("tool_name", "")
        path = decision.get("path", "")
        absolute_path = decision.get("absolute_path", "")

        file_fingerprint_val = ""
        if path and category in ("file_summary", "file_modified"):
            file_fingerprint_val = file_fingerprint(absolute_path or path)

        if category == "observation":
            wm.add_observation(tool_name, content)
        elif category == "file_summary":
            wm.add_observation(
                tool_name,
                f"read {path}: {content}",
                file_path=path,
                file_fingerprint=file_fingerprint_val,
            )
            if path:
                wm.add_candidate(path)
        elif category == "file_modified":
            if path:
                wm.add_observation(
                    tool_name,
                    f"modified {path}",
                    file_path=path,
                    file_fingerprint=file_fingerprint_val,
                )
                wm.add_candidate(path)

    def write_semantic(self, sm: SemanticMemory, decision: dict[str, Any]) -> None:
        """Compatibility helper for older tests and integrations.

        Runtime code should not call this during tool execution. New semantic
        promotion and invalidation paths belong in `MemoryCompactor`.
        """
        category = decision.get("category", "")
        content = decision.get("content", "")
        path = decision.get("path", "")
        absolute_path = decision.get("absolute_path", "")

        if category == "file_modified" and path:
            record_id = SemanticMemory.make_record_id("file_summary", path)
            existing = sm.get(record_id)
            if existing:
                sm.delete(record_id)
            return

        if not content or not path:
            return

        record_id = SemanticMemory.make_record_id("file_summary", path)

        if category == "file_summary" and decision.get("promote_to_semantic"):
            fp = file_fingerprint(absolute_path or path)
            sm.put(SemanticRecord(
                record_id=record_id,
                category="file_summary",
                content=content,
                repo_path=path,
                file_path=path,
                tags=["file_summary", path],
                source_run_id=decision.get("args", {}).get("run_id", ""),
                freshness_hash=fp,
                file_version=fp,
                importance_score=1.0,
            ))

    def _summarize_result(self, result: str, limit: int = 180) -> str:
        """Summarize a tool result for memory storage."""
        return summarize_result(result, limit)
