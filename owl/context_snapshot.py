"""一轮运行开始时的上下文快照。

context_snapshot 的职责：
  - 在每轮 ask() 开始时，记录这次 prompt 是怎么拼出来的
  - 哪些层参与了、每层贡献了多少字符、哪些被裁剪了
  - 快照可以被 trace 引用，也可以单独落盘

它回答的问题是："这一轮 prompt 到底由什么构成，为什么是这个样子？"
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .context_layers import ContextBundle, ContextItem, ContextLayer


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LayerSummary:
    """某一层在快照中的摘要。"""

    layer: ContextLayer
    item_count: int = 0
    total_raw_chars: int = 0
    total_rendered_chars: int = 0
    trimmed_count: int = 0
    item_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "item_count": self.item_count,
            "total_raw_chars": self.total_raw_chars,
            "total_rendered_chars": self.total_rendered_chars,
            "trimmed_count": self.trimmed_count,
            "item_names": self.item_names,
        }


@dataclass
class ContextSnapshot:
    """一轮运行的上下文快照。

    在每轮 ask() 开始时创建，记录这一轮 prompt 的构成信息。
    可以被写入 trace 或单独落盘到 run artifacts。
    """

    # 标识
    run_id: str = ""
    task_id: str = ""

    # 快照时间
    built_at: str = ""

    # 最终 prompt 的字符数
    prompt_chars: int = 0

    # 总预算
    budget_chars: int = 0

    # 是否超预算
    over_budget: bool = False

    # 各 section 的元信息（兼容旧 context_manager 的 section 模型）
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)

    # 五层模型的摘要
    layer_summaries: dict[str, LayerSummary] = field(default_factory=dict)

    # 裁剪日志
    reduction_log: list[dict[str, Any]] = field(default_factory=list)

    # 保留的 item 列表摘要
    retained_items: list[dict[str, Any]] = field(default_factory=list)

    # 被裁剪掉的 item 列表摘要
    trimmed_items: list[dict[str, Any]] = field(default_factory=list)

    # 快照内容的哈希（用于 cache key）
    content_hash: str = ""

    def __post_init__(self):
        if not self.built_at:
            self.built_at = _now_iso()

    # --- 工厂方法 ---

    @classmethod
    def from_build_result(
        cls,
        run_id: str,
        task_id: str,
        prompt: str,
        budget_chars: int,
        metadata: dict[str, Any],
    ) -> ContextSnapshot:
        """从 builder 的输出创建快照。

        参数：
          run_id       — 运行 ID
          task_id      — 任务 ID
          prompt       — 最终 prompt 文本
          budget_chars — 总预算字符数
          metadata     — builder 返回的 metadata 字典
        """
        snapshot = cls(
            run_id=run_id,
            task_id=task_id,
            prompt_chars=len(prompt),
            budget_chars=budget_chars,
            over_budget=len(prompt) > budget_chars,
            sections=metadata.get("sections", {}),
            reduction_log=metadata.get("budget_reductions", []),
            content_hash=hashlib.sha256(prompt.encode("utf-8", errors="surrogatepass")).hexdigest()[:16],
        )

        # 从 sections 中推导出五层摘要
        for section_name, section_meta in metadata.get("sections", {}).items():
            layer_value = section_meta.get("layer", "runtime")
            try:
                layer = ContextLayer(layer_value)
            except ValueError:
                layer = ContextLayer.RUNTIME

            summary = snapshot.layer_summaries.get(layer_value)
            if summary is None:
                summary = LayerSummary(layer=layer)
                snapshot.layer_summaries[layer_value] = summary

            summary.item_count += 1
            summary.total_raw_chars += section_meta.get("raw_chars", 0)
            summary.total_rendered_chars += section_meta.get("rendered_chars", 0)
            summary.item_names.append(section_name)

        return snapshot

    @classmethod
    def from_bundle(
        cls,
        run_id: str,
        task_id: str,
        bundle: ContextBundle,
        prompt: str,
        budget_chars: int,
    ) -> ContextSnapshot:
        """从 ContextBundle 创建快照（基于五层模型）。"""
        snapshot = cls(
            run_id=run_id,
            task_id=task_id,
            prompt_chars=len(prompt),
            budget_chars=budget_chars,
            over_budget=len(prompt) > budget_chars,
            content_hash=hashlib.sha256(prompt.encode("utf-8", errors="surrogatepass")).hexdigest()[:16],
        )

        # 按 layer 汇总
        layer_items: dict[str, list[ContextItem]] = {}
        for item in bundle.items:
            key = item.layer.value
            layer_items.setdefault(key, []).append(item)

        for layer_value, items in layer_items.items():
            summary = LayerSummary(
                layer=items[0].layer,
                item_count=len(items),
                total_raw_chars=sum(it.raw_chars for it in items),
                total_rendered_chars=sum(it.rendered_chars for it in items),
                trimmed_count=sum(1 for it in items if it.was_trimmed),
                item_names=[it.name for it in items],
            )
            snapshot.layer_summaries[layer_value] = summary

        # 记录保留和裁剪的 items
        for item in bundle.items:
            entry = {
                "name": item.name,
                "layer": item.layer.value,
                "raw_chars": item.raw_chars,
                "rendered_chars": item.rendered_chars,
                "was_trimmed": item.was_trimmed,
            }
            if item.was_trimmed:
                snapshot.trimmed_items.append(entry)
            else:
                snapshot.retained_items.append(entry)

        return snapshot

    # --- 查询方法 ---

    def layer_summary(self, layer: ContextLayer) -> LayerSummary | None:
        return self.layer_summaries.get(layer.value)

    def total_trimmed(self) -> int:
        return sum(s.trimmed_count for s in self.layer_summaries.values())

    def total_raw_chars(self) -> int:
        return sum(s.total_raw_chars for s in self.layer_summaries.values())

    def total_rendered_chars(self) -> int:
        return sum(s.total_rendered_chars for s in self.layer_summaries.values())

    def compression_ratio(self) -> float:
        raw = self.total_raw_chars()
        if raw == 0:
            return 0.0
        return (raw - self.total_rendered_chars()) / raw

    # --- 序列化 ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "built_at": self.built_at,
            "prompt_chars": self.prompt_chars,
            "budget_chars": self.budget_chars,
            "over_budget": self.over_budget,
            "content_hash": self.content_hash,
            "total_trimmed": self.total_trimmed(),
            "compression_ratio": round(self.compression_ratio(), 4),
            "layer_summaries": {
                key: summary.to_dict()
                for key, summary in self.layer_summaries.items()
            },
            "sections": self.sections,
            "reduction_log": self.reduction_log,
            "retained_items": self.retained_items,
            "trimmed_items": self.trimmed_items,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextSnapshot:
        snapshot = cls(
            run_id=str(data.get("run_id", "")),
            task_id=str(data.get("task_id", "")),
            built_at=str(data.get("built_at", "")),
            prompt_chars=int(data.get("prompt_chars", 0)),
            budget_chars=int(data.get("budget_chars", 0)),
            over_budget=bool(data.get("over_budget", False)),
            sections=data.get("sections", {}),
            reduction_log=data.get("reduction_log", []),
            retained_items=data.get("retained_items", []),
            trimmed_items=data.get("trimmed_items", []),
            content_hash=str(data.get("content_hash", "")),
        )
        for layer_value, layer_data in data.get("layer_summaries", {}).items():
            try:
                layer = ContextLayer(layer_data.get("layer", layer_value))
            except ValueError:
                layer = ContextLayer.RUNTIME
            snapshot.layer_summaries[layer_value] = LayerSummary(
                layer=layer,
                item_count=int(layer_data.get("item_count", 0)),
                total_raw_chars=int(layer_data.get("total_raw_chars", 0)),
                total_rendered_chars=int(layer_data.get("total_rendered_chars", 0)),
                trimmed_count=int(layer_data.get("trimmed_count", 0)),
                item_names=layer_data.get("item_names", []),
            )
        return snapshot
