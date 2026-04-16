"""context_snapshot 模块测试。"""

import pytest

from owl.context_layers import ContextLayer
from owl.context_snapshot import ContextSnapshot, LayerSummary, _now_iso


class TestLayerSummary:
    def test_create(self):
        summary = LayerSummary(layer=ContextLayer.RESIDENT, item_count=2, total_raw_chars=500)
        assert summary.layer == ContextLayer.RESIDENT
        assert summary.item_count == 2
        assert summary.total_raw_chars == 500

    def test_to_dict(self):
        summary = LayerSummary(layer=ContextLayer.RUNTIME, item_count=1, total_raw_chars=100)
        d = summary.to_dict()
        assert d["layer"] == "runtime"
        assert d["item_count"] == 1


class TestContextSnapshot:
    def test_create_empty(self):
        snapshot = ContextSnapshot(run_id="run_1", task_id="task_1")
        assert snapshot.run_id == "run_1"
        assert snapshot.task_id == "task_1"
        assert snapshot.built_at
        assert snapshot.prompt_chars == 0
        assert not snapshot.over_budget

    def test_from_build_result(self):
        metadata = {
            "sections": {
                "prefix": {
                    "raw_chars": 1000,
                    "rendered_chars": 1000,
                    "layer": "resident",
                },
                "memory": {
                    "raw_chars": 500,
                    "rendered_chars": 300,
                    "layer": "compacted",
                },
            },
            "budget_reductions": [
                {"section": "memory", "before_chars": 500, "after_chars": 300},
            ],
        }
        snapshot = ContextSnapshot.from_build_result(
            run_id="run_1",
            task_id="task_1",
            prompt="hello world",
            budget_chars=10000,
            metadata=metadata,
        )
        assert snapshot.run_id == "run_1"
        assert snapshot.prompt_chars == 11
        assert not snapshot.over_budget
        assert snapshot.content_hash
        assert len(snapshot.reduction_log) == 1

    def test_over_budget_flag(self):
        metadata = {
            "sections": {},
            "budget_reductions": [],
        }
        snapshot = ContextSnapshot.from_build_result(
            run_id="run_1",
            task_id="task_1",
            prompt="x" * 1000,
            budget_chars=500,
            metadata=metadata,
        )
        assert snapshot.over_budget
        assert snapshot.prompt_chars == 1000

    def test_layer_summary(self):
        metadata = {
            "sections": {
                "prefix": {"raw_chars": 100, "rendered_chars": 100, "layer": "resident"},
                "memory": {"raw_chars": 200, "rendered_chars": 100, "layer": "compacted"},
            },
            "budget_reductions": [],
        }
        snapshot = ContextSnapshot.from_build_result(
            run_id="run_1",
            task_id="task_1",
            prompt="test",
            budget_chars=1000,
            metadata=metadata,
        )
        resident = snapshot.layer_summary(ContextLayer.RESIDENT)
        assert resident is not None
        assert resident.total_raw_chars == 100
        assert resident.total_rendered_chars == 100

        compacted = snapshot.layer_summary(ContextLayer.COMPACTED)
        assert compacted is not None
        assert compacted.total_raw_chars == 200

    def test_total_trimmed(self):
        from owl.context_layers import ContextItem
        from owl.context_snapshot import ContextSnapshot as CS2
        snapshot = CS2(run_id="run_1", task_id="task_1")
        snapshot.layer_summaries["runtime"] = LayerSummary(
            layer=ContextLayer.RUNTIME,
            item_count=3,
            total_raw_chars=1000,
            total_rendered_chars=600,
            trimmed_count=2,
            item_names=["a", "b", "c"],
        )
        assert snapshot.total_trimmed() == 2

    def test_compression_ratio(self):
        snapshot = ContextSnapshot(run_id="run_1", task_id="task_1")
        snapshot.layer_summaries["runtime"] = LayerSummary(
            layer=ContextLayer.RUNTIME,
            item_count=1,
            total_raw_chars=1000,
            total_rendered_chars=800,
            trimmed_count=0,
        )
        # 800/1000 = 0.2 compression
        assert 0.19 < snapshot.compression_ratio() < 0.21

    def test_compression_ratio_zero(self):
        snapshot = ContextSnapshot(run_id="run_1", task_id="task_1")
        assert snapshot.compression_ratio() == 0.0

    def test_to_dict_roundtrip(self):
        snapshot = ContextSnapshot(run_id="run_1", task_id="task_1", prompt_chars=42, budget_chars=100)
        d = snapshot.to_dict()
        restored = ContextSnapshot.from_dict(d)
        assert restored.run_id == snapshot.run_id
        assert restored.prompt_chars == snapshot.prompt_chars
        assert restored.budget_chars == snapshot.budget_chars

    def test_content_hash(self):
        snapshot1 = ContextSnapshot.from_build_result(
            run_id="run_1",
            task_id="task_1",
            prompt="hello world",
            budget_chars=1000,
            metadata={"sections": {}, "budget_reductions": []},
        )
        snapshot2 = ContextSnapshot.from_build_result(
            run_id="run_2",
            task_id="task_2",
            prompt="hello world",
            budget_chars=1000,
            metadata={"sections": {}, "budget_reductions": []},
        )
        # Same content should produce same hash
        assert snapshot1.content_hash == snapshot2.content_hash
        # Different content should produce different hash
        snapshot3 = ContextSnapshot.from_build_result(
            run_id="run_3",
            task_id="task_3",
            prompt="different content",
            budget_chars=1000,
            metadata={"sections": {}, "budget_reductions": []},
        )
        assert snapshot1.content_hash != snapshot3.content_hash
