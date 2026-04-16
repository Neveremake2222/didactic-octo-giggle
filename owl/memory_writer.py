"""统一记忆写入策略。

memory_writer 的职责是"写入决策"，而不是直接裸写：
  - 这条信息值得写吗？
  - 写到哪一层（working / semantic）？
  - 写成原文、摘要还是结构化条目？
  - 是否需要覆盖旧版本？

所有记忆写入都应该经过这个模块的审批。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .semantic_memory import SemanticMemory, SemanticRecord
from .working_memory import WorkingMemory


# 写入目标
WRITE_TARGET_WORKING = "working"
WRITE_TARGET_SEMANTIC = "semantic"
WRITE_TARGET_SKIP = "skip"


class MemoryWriter:
    """统一记忆写入策略。

    使用方式：
      writer = MemoryWriter()
      decision = writer.should_write(tool_name, args, result)
      if decision["target"] == WRITE_TARGET_WORKING:
          writer.write_working(working_memory, decision)
      elif decision["target"] == WRITE_TARGET_SEMANTIC:
          writer.write_semantic(semantic_memory, decision)
    """

    def should_write(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """判断一条信息是否值得写入记忆。

        返回决策字典：
          - target: "working" / "semantic" / "skip"
          - reason: 为什么这样决定
          - content: 待写入的内容
          - category: 写入类别
        """
        context = context or {}
        path = args.get("path", "")

        # 工具结果不写：list_files, search（信息量大且时效性短）
        if tool_name in ("list_files", "search"):
            return {
                "target": WRITE_TARGET_WORKING,
                "reason": "navigation result, keep in working memory as observation",
                "content": str(result)[:300],
                "category": "observation",
                "tool_name": tool_name,
                "args": args,
            }

        # 文件操作写入 working memory
        if tool_name in ("read_file", "write_file", "patch_file"):
            if not path:
                return {"target": WRITE_TARGET_SKIP, "reason": "no path", "content": "", "category": ""}

            # 读文件 → working + 可能 semantic
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
                    "promote_to_semantic": True,
                }

            # 写文件/patch → working（使旧摘要失效）
            return {
                "target": WRITE_TARGET_WORKING,
                "reason": "file modified, invalidate old summaries",
                "content": "",
                "category": "file_modified",
                "tool_name": tool_name,
                "args": args,
                "path": path,
                "promote_to_semantic": False,
            }

        # shell 执行写入 working memory（作为观察）
        if tool_name == "run_shell":
            return {
                "target": WRITE_TARGET_WORKING,
                "reason": "shell execution observation",
                "content": str(result)[:300],
                "category": "observation",
                "tool_name": tool_name,
                "args": args,
            }

        # delegate 结果写入 working memory
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
        """根据决策写入 working memory。"""
        category = decision.get("category", "")
        content = decision.get("content", "")
        tool_name = decision.get("tool_name", "")
        path = decision.get("path", "")

        # Phase 2: 计算文件指纹
        file_fingerprint = ""
        if path and category in ("file_summary", "file_modified"):
            file_fingerprint = _file_fingerprint(path)

        if category == "observation":
            wm.add_observation(tool_name, content)
        elif category == "file_summary":
            wm.add_observation(tool_name, f"read {path}: {content}",
                               file_path=path, file_fingerprint=file_fingerprint)
            if path:
                wm.add_candidate(path)
        elif category == "file_modified":
            if path:
                wm.add_candidate(path)

    def write_semantic(self, sm: SemanticMemory, decision: dict[str, Any]) -> None:
        """根据决策写入 semantic memory。"""
        category = decision.get("category", "")
        content = decision.get("content", "")
        path = decision.get("path", "")

        # 文件被修改时，即使 content 为空，也要删除旧摘要
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
            # Phase 2: 填充 freshness_hash 和 file_version
            fp = _file_fingerprint(path)
            sm.put(SemanticRecord(
                record_id=record_id,
                category="file_summary",
                content=content,
                repo_path=path,
                tags=["file_summary", path],
                source_run_id=decision.get("args", {}).get("run_id", ""),
                freshness_hash=fp,
                file_version=fp,
                importance_score=1.0,
            ))

    def _summarize_result(self, result: str, limit: int = 180) -> str:
        """对工具结果生成简短摘要。"""
        lines = [line.strip() for line in str(result).splitlines() if line.strip()]
        if not lines:
            return "(empty)"
        if lines[0].startswith("# "):
            lines = lines[1:]
        if not lines:
            return "(empty)"
        summary = " | ".join(lines[:3])
        return summary[:limit]


def _file_fingerprint(path: str) -> str:
    """计算文件内容的 SHA-256 fingerprint。文件不存在时返回空字符串。"""
    try:
        content = Path(path).read_text(encoding="utf-8")
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    except (OSError, UnicodeDecodeError):
        return ""
