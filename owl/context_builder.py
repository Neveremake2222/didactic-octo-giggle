"""将各层上下文组装为最终 prompt 输入。

context_builder 和 context_budget 的区别：
  - context_budget 只负责"在给定预算内决定保留多少"
  - context_builder 负责"把各来源组装成最终 prompt 并填充元数据"

Builder 接收 agent 的各来源数据（prefix、memory、history、relevant notes），
将其包装成 ContextItem，交给 budget 处理，然后组装成最终 prompt 字符串。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .context_layers import (
    ContextBundle,
    ContextItem,
    ContextLayer,
    classify_existing_section,
)
from .context_budget import ContextBudget, _tail_clip

if TYPE_CHECKING:
    pass

# Section 到组装顺序的映射
SECTION_ORDER = ("prefix", "memory", "relevant_memory", "history", "current_request")
CURRENT_REQUEST_SECTION = "current_request"
RELEVANT_MEMORY_LIMIT = 3


@dataclass
class BuiltContext:
    """一次完整的上下文组装结果。"""

    # 最终 prompt 文本
    prompt: str

    # 各 section 的原始文本
    section_texts: dict[str, str]

    # 预算裁剪后的各 section 文本
    rendered_texts: dict[str, str]

    # 裁剪日志
    reduction_log: list[dict[str, Any]]

    # 本轮用户请求
    current_request: str

    # 选中的相关笔记
    selected_notes: list[dict[str, Any]]

    # 使用的 budget 配置
    budget_config: dict[str, Any]

    @property
    def prompt_chars(self) -> int:
        return len(self.prompt)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_chars": self.prompt_chars,
            "current_request_chars": len(self.current_request),
            "section_count": len(self.section_texts),
            "reduction_log": self.reduction_log,
            "budget_config": self.budget_config,
            "selected_notes_count": len(self.selected_notes),
        }


class ContextBuilder:
    """上下文组装器。

    职责：
    1. 接收 agent 的各来源数据
    2. 包装成 ContextBundle
    3. 调用 ContextBudget 进行裁剪
    4. 组装成最终 prompt
    5. 填充元数据
    """

    def __init__(self, agent, budget: ContextBudget | None = None):
        self.agent = agent
        self.budget = budget or ContextBudget()

    # --- 主要入口 ---

    def build(
        self,
        user_message: str,
        prefix_text: str = "",
        memory_text: str = "",
        history: list[dict[str, Any]] | None = None,
        selected_notes: list[dict[str, Any]] | None = None,
        reduce: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        """组装一轮完整 prompt。

        参数：
          user_message   — 本轮用户请求
          prefix_text    — 系统前缀（工具定义、行为规则）
          memory_text    — 工作记忆文本
          history        — 对话历史
          selected_notes — 相关笔记召回结果
          reduce         — 是否启用预算裁剪

        返回：
          (final_prompt, metadata)
        """
        user_message = str(user_message)
        history = history or []
        selected_notes = selected_notes or []

        # 构造各 section 的原始文本
        section_texts = {
            "prefix": str(prefix_text),
            "memory": str(memory_text) if memory_text else "Memory:\n- disabled",
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }

        # 渲染 history
        section_texts["history"] = self._render_history(history)

        # 渲染 relevant_memory
        relevant_raw = self._render_relevant_memory(selected_notes)
        section_texts["relevant_memory"] = relevant_raw

        # 应用预算裁剪
        if reduce:
            section_texts, reduction_log = self.budget.apply_reduction(section_texts, selected_notes)
        else:
            reduction_log = []

        # 组装最终 prompt
        prompt = self._assemble_prompt(section_texts)

        # 构建元数据
        metadata = self._build_metadata(
            prompt=prompt,
            section_texts=section_texts,
            budgets=self.budget.config.sections,
            reduction_log=reduction_log,
            selected_notes=selected_notes,
            user_message=user_message,
            history=history,
        )

        return prompt, metadata

    # --- 内部渲染方法 ---

    def _render_history(self, history: list[dict[str, Any]]) -> str:
        """把 history 列表渲染成一段文本。"""
        if not history:
            return "Transcript:\n- empty"

        lines = ["Transcript:"]
        recent_window = 6
        recent_start = max(0, len(history) - recent_window)

        for index, item in enumerate(history):
            is_recent = index >= recent_start
            line_limit = 900 if is_recent else 60
            lines.extend(self._render_history_item(item, line_limit))

        return "\n".join(lines)

    def _render_history_item(self, item: dict[str, Any], line_limit: int) -> list[str]:
        """渲染单条历史记录。"""
        role = str(item.get("role", ""))
        if role == "tool":
            name = str(item.get("name", ""))
            args_str = json.dumps(item.get("args", {}), sort_keys=True)
            prefix = f"[tool:{name}] {args_str}"
            content = _tail_clip(str(item.get("content", "")), max(20, line_limit))
            return [prefix, content]
        return [f"[{role}] {_tail_clip(str(item.get("content", "")), line_limit)}"]

    def _render_relevant_memory(self, selected_notes: list[dict[str, Any]]) -> str:
        """把相关笔记渲染成一段文本。"""
        lines = ["Relevant memory:"]
        if not selected_notes:
            lines.append("- none")
        else:
            for note in selected_notes:
                text = str(note.get("text", "")).strip()
                if text:
                    lines.append(f"- {text}")
        return "\n".join(lines)

    def _assemble_prompt(self, section_texts: dict[str, str]) -> str:
        """按顺序组装最终 prompt。"""
        ordered = [section_texts.get(sec, "") for sec in SECTION_ORDER]
        return "\n\n".join(line for line in ordered if line).strip()

    def _build_metadata(
        self,
        prompt: str,
        section_texts: dict[str, str],
        budgets: dict[str, int],
        reduction_log: list[dict[str, Any]],
        selected_notes: list[dict[str, Any]],
        user_message: str,
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """构建元数据字典。"""
        # 映射 section name → layer
        section_layers = {
            "prefix": classify_existing_section("prefix"),
            "memory": classify_existing_section("memory"),
            "relevant_memory": classify_existing_section("relevant_memory"),
            "history": classify_existing_section("history"),
            CURRENT_REQUEST_SECTION: classify_existing_section("current_request"),
        }

        sections_meta = {}
        for section, raw_text in section_texts.items():
            layer = section_layers.get(section, ContextLayer.RUNTIME)
            sections_meta[section] = {
                "raw_chars": len(raw_text),
                "budget_chars": budgets.get(section, 0),
                "rendered_chars": len(section_texts.get(section, "")),
                "layer": layer.value,
                "priority": layer.value,
            }

        return {
            "prompt_chars": len(prompt),
            "prompt_budget_chars": self.budget.config.total,
            "prompt_over_budget": len(prompt) > self.budget.config.total,
            "section_order": list(SECTION_ORDER),
            "section_budgets": budgets,
            "sections": sections_meta,
            "budget_reductions": reduction_log,
            "reduction_order": list(self.budget.config.reduction_order),
            "relevant_memory": {
                "limit": RELEVANT_MEMORY_LIMIT,
                "selected_count": len(selected_notes),
                "selected_notes": [note.get("text", "") for note in selected_notes if note.get("text")],
            },
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
            },
            "history_entries": len(history),
        }
