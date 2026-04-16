"""context_budget 和 context_builder 模块测试。"""

import pytest

from owl.context_budget import (
    BudgetConfig,
    ContextBudget,
    DEFAULT_TOTAL_BUDGET,
    DEFAULT_SECTION_BUDGETS,
    DEFAULT_SECTION_FLOORS,
    DEFAULT_REDUCTION_ORDER,
    _tail_clip,
)
from owl.context_builder import ContextBuilder, BuiltContext, SECTION_ORDER


class TestTailClip:
    def test_no_clip_needed(self):
        assert _tail_clip("hello", 10) == "hello"

    def test_clip_adds_ellipsis(self):
        result = _tail_clip("hello world", 8)
        assert result == "hello..."
        assert len(result) == 8

    def test_zero_limit(self):
        assert _tail_clip("hello", 0) == ""

    def test_small_limit(self):
        assert _tail_clip("hello", 2) == "he"


class TestBudgetConfig:
    def test_defaults(self):
        config = BudgetConfig()
        assert config.total == DEFAULT_TOTAL_BUDGET
        assert "prefix" in config.sections

    def test_section_budget(self):
        config = BudgetConfig()
        assert config.section_budget("prefix") == DEFAULT_SECTION_BUDGETS["prefix"]

    def test_section_floor_default(self):
        config = BudgetConfig()
        floor = config.section_floor("prefix")
        assert floor > 0
        assert floor <= DEFAULT_SECTION_BUDGETS["prefix"]

    def test_apply_overflow_reduction_no_overflow(self):
        config = BudgetConfig()
        budgets = dict(DEFAULT_SECTION_BUDGETS)
        result_budgets, log = config.apply_overflow_reduction(budgets, 0)
        assert log == []

    def test_apply_overflow_reduction_reduces_sections(self):
        config = BudgetConfig(total=5000)
        budgets = dict(DEFAULT_SECTION_BUDGETS)
        total_original = sum(budgets.values())
        overflow = total_original - 5000
        result_budgets, log = config.apply_overflow_reduction(budgets, overflow)
        assert len(log) > 0
        assert sum(result_budgets.values()) < total_original


class TestContextBudget:
    def test_create_default(self):
        budget = ContextBudget()
        assert budget.budget_for("prefix") == DEFAULT_SECTION_BUDGETS["prefix"]

    def test_apply_reduction_no_overflow(self):
        budget = ContextBudget(total=100000)
        texts = {"prefix": "short", "memory": "short", "history": "short", "relevant_memory": "short"}
        result, log = budget.apply_reduction(texts)
        assert log == []
        assert result["prefix"] == "short"

    def test_to_dict(self):
        budget = ContextBudget()
        d = budget.to_dict()
        assert "total" in d
        assert "sections" in d
        assert "floors" in d


class TestContextBuilder:
    def test_build_basic(self):
        builder = ContextBuilder(agent=None, budget=ContextBudget(total=50000))
        prompt, metadata = builder.build(
            user_message="hello",
            prefix_text="You are owl.",
            memory_text="Memory:\n- task: test",
            history=[],
            selected_notes=[],
        )
        assert "You are owl." in prompt
        assert "hello" in prompt
        assert metadata["prompt_chars"] > 0
        assert metadata["history_entries"] == 0

    def test_build_with_history(self):
        builder = ContextBuilder(agent=None, budget=ContextBudget(total=50000))
        history = [
            {"role": "user", "content": "read README.md"},
            {"role": "tool", "name": "read_file", "args": {"path": "README.md"}, "content": "# Hello"},
        ]
        prompt, metadata = builder.build(
            user_message="what did you read?",
            prefix_text="system",
            memory_text="memory",
            history=history,
            selected_notes=[],
        )
        assert "README.md" in prompt
        assert metadata["history_entries"] == 2

    def test_build_with_selected_notes(self):
        builder = ContextBuilder(agent=None, budget=ContextBudget(total=50000))
        notes = [{"text": "deploy key is red"}]
        prompt, metadata = builder.build(
            user_message="what is the deploy key?",
            prefix_text="system",
            memory_text="memory",
            selected_notes=notes,
        )
        assert "deploy key is red" in prompt
        assert metadata["relevant_memory"]["selected_count"] == 1

    def test_build_no_reduce(self):
        builder = ContextBuilder(agent=None, budget=ContextBudget(total=100))
        long_prefix = "x" * 5000
        prompt, metadata = builder.build(
            user_message="hello",
            prefix_text=long_prefix,
            memory_text="memory",
            reduce=False,
        )
        assert metadata["budget_reductions"] == []

    def test_build_with_reduce(self):
        builder = ContextBuilder(
            agent=None,
            budget=ContextBudget(
                total=300,
                section_budgets={"prefix": 100, "memory": 80, "relevant_memory": 50, "history": 70},
                section_floors={"prefix": 20, "memory": 10, "relevant_memory": 10, "history": 10},
            ),
        )
        long_prefix = "x" * 5000
        long_memory = "y" * 5000
        prompt, metadata = builder.build(
            user_message="hello",
            prefix_text=long_prefix,
            memory_text=long_memory,
            reduce=True,
        )
        # prefix 和 memory 被裁剪到远小于 5000
        assert len(prompt) < 500
        assert len(metadata["budget_reductions"]) > 0

    def test_render_history_empty(self):
        builder = ContextBuilder(agent=None)
        result = builder._render_history([])
        assert "empty" in result

    def test_render_relevant_memory_empty(self):
        builder = ContextBuilder(agent=None)
        result = builder._render_relevant_memory([])
        assert "none" in result

    def test_metadata_has_section_layers(self):
        builder = ContextBuilder(agent=None, budget=ContextBudget(total=50000))
        _, metadata = builder.build(
            user_message="hello",
            prefix_text="system",
            memory_text="memory",
        )
        sections = metadata["sections"]
        assert sections["prefix"]["layer"] == "resident"
        assert sections["memory"]["layer"] == "compacted"
        assert sections["history"]["layer"] == "runtime"
