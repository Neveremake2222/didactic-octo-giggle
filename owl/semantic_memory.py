"""跨任务长期稳定记忆。

semantic_memory 和 working_memory 的区别：
  - working_memory 生命周期仅限当前 run
  - semantic_memory 跨任务持久化，可跨 run 复用

只有满足以下条件的信息，才写入 semantic_memory：
  - 跨任务可能复用
  - 语义稳定（不是一次性中间结果）
  - 不是短期失败噪声

典型例子：
  - repo 的模块职责
  - 测试运行约定
  - 某类错误的稳定修复套路
  - 文件级摘要（经 freshness 校验后仍然有效的）
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SemanticRecord:
    """一条长期记忆记录。"""

    # 唯一标识
    record_id: str

    # 记录类型（如 "file_summary", "module_responsibility", "test_convention"）
    category: str

    # 记录正文
    content: str

    # 关联的 repo 路径（如果有）
    repo_path: str = ""

    # 标签（用于检索过滤）
    tags: list[str] = field(default_factory=list)

    # 来源（哪次 run 写入的）
    source_run_id: str = ""

    # 创建时间
    created_at: str = ""

    # 最后更新时间
    updated_at: str = ""

    # freshness hash（用于判断是否过期）
    freshness_hash: str = ""

    # Phase 2: 有效性字段
    file_version: str = ""       # SHA-256 at write time
    superseded_by: str = ""      # record_id of replacement
    invalidated_at: str = ""     # ISO timestamp when invalidated
    importance_score: float = 1.0  # 0.0-1.0 for recall ranking

    def __post_init__(self):
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.updated_at:
            self.updated_at = self.created_at

    # --- Phase 2: 有效性方法 ---

    def invalidate(self) -> None:
        """标记此记录为已失效。"""
        self.invalidated_at = _now_iso()

    def supersede(self, new_record_id: str) -> None:
        """标记此记录已被新记录替代。"""
        self.superseded_by = new_record_id

    def is_active(self) -> bool:
        """此记录是否仍然有效（未被 invalidate 或 supersede）。"""
        return not self.invalidated_at and not self.superseded_by

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "category": self.category,
            "content": self.content,
            "repo_path": self.repo_path,
            "tags": list(self.tags),
            "source_run_id": self.source_run_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "freshness_hash": self.freshness_hash,
            "file_version": self.file_version,
            "superseded_by": self.superseded_by,
            "invalidated_at": self.invalidated_at,
            "importance_score": self.importance_score,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SemanticRecord:
        return cls(
            record_id=str(data.get("record_id", "")),
            category=str(data.get("category", "")),
            content=str(data.get("content", "")),
            repo_path=str(data.get("repo_path", "")),
            tags=list(data.get("tags", [])),
            source_run_id=str(data.get("source_run_id", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            freshness_hash=str(data.get("freshness_hash", "")),
            file_version=str(data.get("file_version", "")),
            superseded_by=str(data.get("superseded_by", "")),
            invalidated_at=str(data.get("invalidated_at", "")),
            importance_score=float(data.get("importance_score", 1.0)),
        )


class SemanticMemory:
    """跨任务长期稳定记忆。

    最小实现：内存 dict 存储，不追求数据库。
    支持按 category / tags / repo_path 过滤的简单检索。
    """

    def __init__(self, records: list[SemanticRecord] | None = None):
        self._records: dict[str, SemanticRecord] = {}
        if records:
            for record in records:
                self._records[record.record_id] = record

    # --- 写入 ---

    def put(self, record: SemanticRecord) -> SemanticRecord:
        """写入一条记录。如果 record_id 已存在则更新。"""
        if record.record_id in self._records:
            existing = self._records[record.record_id]
            record.created_at = existing.created_at
            record.updated_at = _now_iso()
        self._records[record.record_id] = record
        return record

    def delete(self, record_id: str) -> bool:
        """删除一条记录。返回是否成功删除。"""
        if record_id in self._records:
            del self._records[record_id]
            return True
        return False

    # --- 查询 ---

    def get(self, record_id: str) -> SemanticRecord | None:
        """按 ID 获取记录。"""
        return self._records.get(record_id)

    def search(
        self,
        query: str = "",
        category: str | None = None,
        tags: list[str] | None = None,
        repo_path: str | None = None,
        top_k: int = 5,
    ) -> list[SemanticRecord]:
        """检索记录。

        过滤维度：
          - category  — 精确匹配
          - tags      — 任一 tag 匹配
          - repo_path — 精确匹配
          - query     — 简单关键词匹配
        """
        candidates = list(self._records.values())

        if category:
            candidates = [r for r in candidates if r.category == category]

        if tags:
            tag_set = set(tags)
            candidates = [
                r for r in candidates
                if tag_set & set(r.tags)
            ]

        if repo_path:
            candidates = [r for r in candidates if r.repo_path == repo_path]

        if query:
            # 简单关键词匹配：query 中任一 token 出现在 content/tag/path 中即命中
            query_tokens = {t.lower() for t in query.split() if len(t) > 2}
            if query_tokens:
                candidates = [
                    r for r in candidates
                    if query_tokens & {t.lower() for t in r.content.split()}
                    or query_tokens & {t.lower() for t in r.repo_path.split()}
                    or query_tokens & {t.lower() for tag in r.tags for t in tag.split()}
                ]

        return candidates[:top_k]

    def all_records(self) -> list[SemanticRecord]:
        """返回所有记录。"""
        return list(self._records.values())

    def count(self) -> int:
        """记录总数。"""
        return len(self._records)

    # --- 序列化 ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [r.to_dict() for r in self._records.values()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SemanticMemory:
        records = [
            SemanticRecord.from_dict(r)
            for r in data.get("records", [])
        ]
        return cls(records=records)

    @staticmethod
    def make_record_id(category: str, key: str) -> str:
        """生成一个稳定的 record_id。"""
        raw = f"{category}:{key}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
