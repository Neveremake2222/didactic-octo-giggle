"""上下文注入去重与文件指纹索引。

功能一：ContextInjectedTracker — 同次 run 内防止同一来源重复注入
功能二：ContextFingerprintIndex — path -> fingerprint 映射，支持 staleness 判定
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class ContextInjectedTracker:
    """同次 run 内的上下文来源去重器。

    在一次 ask() 运行中，同一 source_id 只允许注入一次。
    每次 mark_injected() 返回 True 表示首次注入，False 表示重复注入。

    同时记录已注入来源的 fingerprint，用于后续判断是否过期。
    """

    def __init__(self) -> None:
        # source_id -> fingerprint（注入时的内容指纹）
        self._injected: dict[str, str] = {}

    def mark_injected(self, source: Any) -> bool:
        """将 source 标记为已注入。返回 True（首次）/ False（重复）。"""
        source_id = getattr(source, "source_id", str(source))
        if source_id in self._injected:
            return False  # 重复注入
        fingerprint = getattr(source, "fingerprint", "")
        self._injected[source_id] = fingerprint
        return True

    def get_fingerprint(self, source_id: str) -> str:
        """获取某来源注入时的 fingerprint。"""
        return self._injected.get(source_id, "")

    def is_injected(self, source_id: str) -> bool:
        """该 source_id 是否已注入过。"""
        return source_id in self._injected

    @property
    def injected_count(self) -> int:
        """已注入的来源数量。"""
        return len(self._injected)


class ContextFingerprintIndex:
    """path -> fingerprint 索引，用于判断文件内容是否发生变化。

    主要服务于 staleness 检测：
      - record(path, content)  记录当前 fingerprint
      - check(path, current_fp)  返回 True 表示与记录不一致（已过期）
      - check_from_file(path)    读取实际文件，判断是否过期

    不写入磁盘，仅在内存中维护（生命周期随 run）。
    """

    def __init__(self) -> None:
        # 绝对路径 -> SHA-256(content)
        self._index: dict[str, str] = {}

    def record(self, path: str, content: str) -> str:
        """记录 path 的 fingerprint，返回该 fingerprint。"""
        fp = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._index[str(Path(path).resolve())] = fp
        return fp

    def check(self, path: str, current_fp: str) -> bool:
        """判断 current_fp 是否与记录一致。True = 已过期（不一致）。"""
        resolved = str(Path(path).resolve())
        stored = self._index.get(resolved, "")
        # 没有记录 → 无法判断 → 返回 False（不标记为过期）
        if not stored:
            return False
        return current_fp != stored

    def check_from_file(self, path: str) -> tuple[bool, str]:
        """读取实际文件，判断是否过期。返回 (is_stale, current_fp)。"""
        resolved = str(Path(path).resolve())
        try:
            content = Path(resolved).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False, ""

        current_fp = hashlib.sha256(content.encode("utf-8")).hexdigest()
        stored = self._index.get(resolved, "")
        is_stale = bool(stored and current_fp != stored)
        return is_stale, current_fp

    def update(self, path: str, content: str) -> bool:
        """更新记录，返回内容是否发生变化。"""
        resolved = str(Path(path).resolve())
        current_fp = hashlib.sha256(content.encode("utf-8")).hexdigest()
        stored = self._index.get(resolved, "")
        self._index[resolved] = current_fp
        return current_fp != stored if stored else True

    def get(self, path: str) -> str:
        """获取某路径当前记录 fingerprint。"""
        return self._index.get(str(Path(path).resolve()), "")

    def __len__(self) -> int:
        return len(self._index)
