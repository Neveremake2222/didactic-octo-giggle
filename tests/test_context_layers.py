"""context_layers 模块测试。"""

import pytest

from owl.context_layers import (
    ContextBundle,
    ContextItem,
    ContextLayer,
    DEFAULT_LAYER_PRIORITY,
    TRIMMABLE_LAYERS,
    classify_existing_section,
)


class TestContextLayer:
    def test_layer_values(self):
        assert ContextLayer.RESIDENT.value == "resident"
        assert ContextLayer.ON_DEMAND.value == "on_demand"
        assert ContextLayer.RUNTIME.value == "runtime"
        assert ContextLayer.COMPACTED.value == "compacted"
        assert ContextLayer.SYSTEM.value == "system"


class TestContextItem:
    def test_basic_creation(self):
        item = ContextItem(
            content="hello world",
            layer=ContextLayer.RUNTIME,
            name="test_item",
        )
        assert item.content == "hello world"
        assert item.layer == ContextLayer.RUNTIME
        assert item.name == "test_item"
        assert item.raw_chars == 11

    def test_default_priority_from_layer(self):
        item = ContextItem(content="", layer=ContextLayer.RESIDENT, name="x")
        assert item.priority == DEFAULT_LAYER_PRIORITY[ContextLayer.RESIDENT]

    def test_default_trimmable_from_layer(self):
        resident = ContextItem(content="", layer=ContextLayer.RESIDENT, name="x")
        assert not resident.is_trimmable()

        runtime = ContextItem(content="", layer=ContextLayer.RUNTIME, name="x")
        assert runtime.is_trimmable()

    def test_explicit_trimmable_override(self):
        item = ContextItem(content="", layer=ContextLayer.RESIDENT, name="x", trimmable=True)
        assert item.is_trimmable()

    def test_explicit_priority_override(self):
        item = ContextItem(content="", layer=ContextLayer.RUNTIME, name="x", priority=999)
        assert item.priority == 999

    def test_was_trimmed_default_false(self):
        item = ContextItem(content="", layer=ContextLayer.RUNTIME, name="x")
        assert not item.was_trimmed


class TestContextBundle:
    def test_empty_bundle(self):
        bundle = ContextBundle()
        assert bundle.items == []
        assert bundle.total_chars() == 0

    def test_add_item(self):
        bundle = ContextBundle()
        item = bundle.add(ContextItem(content="abc", layer=ContextLayer.RUNTIME, name="test"))
        assert len(bundle.items) == 1
        assert item.name == "test"

    def test_by_layer(self):
        bundle = ContextBundle()
        bundle.add(ContextItem(content="a", layer=ContextLayer.RESIDENT, name="resident_item"))
        bundle.add(ContextItem(content="b", layer=ContextLayer.RUNTIME, name="runtime_item1"))
        bundle.add(ContextItem(content="c", layer=ContextLayer.RUNTIME, name="runtime_item2"))

        resident_items = bundle.by_layer(ContextLayer.RESIDENT)
        assert len(resident_items) == 1
        assert resident_items[0].name == "resident_item"

        runtime_items = bundle.by_layer(ContextLayer.RUNTIME)
        assert len(runtime_items) == 2

    def test_sorted_by_priority(self):
        bundle = ContextBundle()
        bundle.add(ContextItem(content="low", layer=ContextLayer.ON_DEMAND, name="low"))
        bundle.add(ContextItem(content="high", layer=ContextLayer.RESIDENT, name="high"))
        bundle.add(ContextItem(content="mid", layer=ContextLayer.RUNTIME, name="mid"))

        sorted_items = bundle.sorted_by_priority()
        assert sorted_items[0].name == "high"
        assert sorted_items[-1].name == "low"

    def test_sorted_by_priority_trimmable_only(self):
        bundle = ContextBundle()
        bundle.add(ContextItem(content="a", layer=ContextLayer.RESIDENT, name="resident"))  # not trimmable
        bundle.add(ContextItem(content="b", layer=ContextLayer.RUNTIME, name="runtime"))

        trimmable = bundle.sorted_by_priority(trimmable_only=True)
        assert len(trimmable) == 1
        assert trimmable[0].name == "runtime"

    def test_total_chars_includes_current_request(self):
        bundle = ContextBundle(current_request="hello")
        bundle.add(ContextItem(content="abc", layer=ContextLayer.RUNTIME, name="x"))
        assert bundle.total_chars() == 8  # 3 + 5

    def test_to_dict(self):
        bundle = ContextBundle(current_request="test")
        bundle.add(ContextItem(content="abc", layer=ContextLayer.RUNTIME, name="item1"))
        d = bundle.to_dict()
        assert len(d["items"]) == 1
        assert d["items"][0]["name"] == "item1"
        assert d["items"][0]["layer"] == "runtime"
        assert d["current_request_chars"] == 4
        assert d["total_chars"] == 7


class TestClassifyExistingSection:
    def test_prefix_maps_to_resident(self):
        assert classify_existing_section("prefix") == ContextLayer.RESIDENT

    def test_memory_maps_to_compacted(self):
        assert classify_existing_section("memory") == ContextLayer.COMPACTED

    def test_relevant_memory_maps_to_compacted(self):
        assert classify_existing_section("relevant_memory") == ContextLayer.COMPACTED

    def test_history_maps_to_runtime(self):
        assert classify_existing_section("history") == ContextLayer.RUNTIME

    def test_current_request_maps_to_runtime(self):
        assert classify_existing_section("current_request") == ContextLayer.RUNTIME

    def test_unknown_maps_to_runtime(self):
        assert classify_existing_section("unknown_section") == ContextLayer.RUNTIME
