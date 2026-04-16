"""上下文注入去重与文件指纹索引。

功能一：ContextInjectedTracker — 同次 run 内防止同一来源重复注入
功能二：ContextFingerprintIndex — 已在 Phase 3 合并为 FileFingerprintTracker（来自 memory_validity）
"""

from __future__ import annotations

from typing import Any

# 导入统一指纹追踪器，保留 ContextFingerprintIndex 别名以兼容已有导入
from .memory_validity import FileFingerprintTracker


# 为了向后兼容，保留别名
# Phase 2/3 现已统一使用 FileFingerprintTracker（memory_validity.py）
# ContextFingerprintIndex = FileFingerprintTracker


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
