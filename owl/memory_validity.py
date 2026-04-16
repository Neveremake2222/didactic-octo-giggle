"""记忆有效性判定与文件指纹追踪。

功能一定义：
  - FileFingerprintTracker：path -> fingerprint 索引
  - 用于判断文件内容是否发生变化（staleness）

功能二定义：
  - SemanticRecordValidityChecker：判断单条 SemanticRecord 是否过期
  - ValidityResult 携带状态和建议操作

这两个类服务于 stale_guard feature：
  - 在工具执行后检查 working memory 中的 observations 是否过期
  - 在 semantic memory 召回时过滤掉已失效的记录
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# ValidityResult
# ---------------------------------------------------------------------------


@dataclass
class ValidityResult:
    """一条记忆的有效性判定结果。"""

    record_id: str
    status: str      # VALID | STALE | SUPERSEDED | INVALIDATED | UNKNOWN
    reason: str
    suggested_action: str  # keep | refresh | invalidate | drop

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "status": self.status,
            "reason": self.reason,
            "suggested_action": self.suggested_action,
        }


# ---------------------------------------------------------------------------
# FileFingerprintTracker
# ---------------------------------------------------------------------------


class FileFingerprintTracker:
    """path -> SHA-256(content) 索引，用于判断文件内容是否发生变化。

    生命周期随 run，不持久化到磁盘。

    使用方式：
      tracker = FileFingerprintTracker()
      tracker.record("/path/to/file.py", "current content")
      is_stale = tracker.check("/path/to/file.py", "new content")  # True = changed
    """

    def __init__(self) -> None:
        # 绝对路径 -> SHA-256(content)
        self._index: dict[str, str] = {}

    def record(self, path: str, content: str) -> str:
        """记录 path 的当前 fingerprint，返回该 fingerprint。"""
        fp = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._index[str(Path(path).resolve())] = fp
        return fp

    def check(self, path: str, current_fp: str) -> bool:
        """判断 current_fp 是否与记录一致。True = 已过期（不一致）。"""
        resolved = str(Path(path).resolve())
        stored = self._index.get(resolved, "")
        if not stored:
            return False  # 没有记录 → 无法判定 → 不标记为过期
        return current_fp != stored

    def check_from_file(self, path: str) -> tuple[bool, str]:
        """读取实际文件，判断内容是否与记录不一致。

        Returns:
            (is_stale, current_fingerprint)
            is_stale=True 表示内容已变化
        """
        resolved = str(Path(path).resolve())
        try:
            content = Path(resolved).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # 文件无法读取 → 视为 stale
            return True, ""
        current_fp = hashlib.sha256(content.encode("utf-8")).hexdigest()
        stored = self._index.get(resolved, "")
        is_stale = bool(stored and current_fp != stored)
        return is_stale, current_fp

    def update(self, path: str, content: str) -> bool:
        """更新记录，返回内容是否发生了变化。"""
        resolved = str(Path(path).resolve())
        current_fp = hashlib.sha256(content.encode("utf-8")).hexdigest()
        stored = self._index.get(resolved, "")
        self._index[resolved] = current_fp
        return current_fp != stored if stored else True

    def get(self, path: str) -> str:
        """获取某路径当前记录的 fingerprint。"""
        return self._index.get(str(Path(path).resolve()), "")

    def __len__(self) -> int:
        return len(self._index)


# ---------------------------------------------------------------------------
# SemanticRecordValidityChecker
# ---------------------------------------------------------------------------


class SemanticRecordValidityChecker:
    """检查单条 SemanticRecord 是否仍然有效。

    判定规则：
      1. 如果 record.invalidated_at 已设置 → INVALIDATED
      2. 如果 record.superseded_by 已设置 → SUPERSEDED
      3. 如果 record.repo_path 存在 → 检查文件指纹是否变化
         - 文件不存在或内容变化 → STALE
         - 内容未变 → VALID
      4. 否则 → VALID（无法判定时默认保留）
    """

    def check_record(self, record: Any, tracker: FileFingerprintTracker) -> ValidityResult:
        """检查一条 SemanticRecord 的有效性。"""
        record_id = getattr(record, "record_id", "")

        # 规则 1: 已显式 invalidate
        if getattr(record, "invalidated_at", ""):
            return ValidityResult(
                record_id=record_id,
                status="INVALIDATED",
                reason="Record was explicitly invalidated.",
                suggested_action="drop",
            )

        # 规则 2: 已被新记录替代
        if getattr(record, "superseded_by", ""):
            return ValidityResult(
                record_id=record_id,
                status="SUPERSEDED",
                reason=f"Replaced by record {getattr(record, 'superseded_by', '')}.",
                suggested_action="drop",
            )

        # 规则 3: 检查文件 fingerprint
        repo_path = getattr(record, "repo_path", "")
        if repo_path:
            is_stale, current_fp = tracker.check_from_file(repo_path)
            if is_stale:
                return ValidityResult(
                    record_id=record_id,
                    status="STALE",
                    reason=f"Underlying file {repo_path} has changed.",
                    suggested_action="refresh",
                )

        # 默认: VALID
        return ValidityResult(
            record_id=record_id,
            status="VALID",
            reason="No invalidation signal detected.",
            suggested_action="keep",
        )
