"""上下文发现引擎测试。

测试 ContextDiscovery 的发现、去重、渲染和注入逻辑。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from owl.context_sources import ContextSource
from owl.context_discovery import (
    ContextDiscovery,
    MAX_ANCESTOR_WALK,
    _classify,
    _extract_header,
    _sha256,
)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace_dir():
    """创建一个临时目录用于文件系统测试。"""
    d = tempfile.mkdtemp(prefix="owl_test_")
    yield d
    try:
        shutil.rmtree(d)
    except OSError:
        pass


def _make_source(
    base: str,
    rel_path: str,
    content: str,
    category: str = "AGENTS.md",
) -> ContextSource:
    full = Path(base) / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return ContextSource(
        source_id=rel_path,
        absolute_path=str(full),
        discovered_from=str(Path(base) / "src/main.py"),
        content=content,
        header=_extract_header(content),
        fingerprint=_sha256(content),
        category=category,
    )


# ---------------------------------------------------------------------------
# _extract_header
# ---------------------------------------------------------------------------

class TestExtractHeader:
    def test_picks_first_non_empty_non_heading_line(self):
        text = "# Title\n\nSome description here.\n\n## Section"
        assert _extract_header(text) == "Some description here."

    def test_skips_empty_lines(self):
        assert _extract_header("  \n\n  \n# Header\n\nContent") == "Content"

    def test_truncates_long_header(self):
        long_line = "x" * 200
        assert len(_extract_header(long_line)) <= 100


# ---------------------------------------------------------------------------
# _classify
# ---------------------------------------------------------------------------

class TestClassify:
    def test_agents_md(self):
        assert _classify("AGENTS.md") == "AGENTS.md"

    def test_readme_md(self):
        assert _classify("README.md") == "README.md"

    def test_contributing_md(self):
        assert _classify("CONTRIBUTING.md") == "CONTRIBUTING.md"

    def test_rule_file(self):
        assert _classify(".owl") == "rule_file"
        assert _classify(".claude") == "rule_file"
        assert _classify("CLAUDE.md") == "rule_file"

    def test_other(self):
        assert _classify("foo.py") == "other"


# ---------------------------------------------------------------------------
# ContextSource.is_stale
# ---------------------------------------------------------------------------

class TestContextSourceStale:
    def test_not_stale_if_content_unchanged(self):
        src = _make_source(tempfile.mkdtemp(prefix="owl_stale_"), "x/AGENTS.md", "hello world")
        assert src.is_stale("hello world") is False

    def test_stale_if_content_changed(self):
        src = _make_source(tempfile.mkdtemp(prefix="owl_stale_"), "x/AGENTS.md", "hello world")
        assert src.is_stale("goodbye world") is True

    def test_stale_if_content_emptied(self):
        src = _make_source(tempfile.mkdtemp(prefix="owl_stale_"), "x/AGENTS.md", "hello world")
        assert src.is_stale("") is True


# ---------------------------------------------------------------------------
# ContextDiscovery.discover_for_file
# ---------------------------------------------------------------------------

class TestDiscoverForFile:
    def test_finds_agents_md_in_same_dir(self, workspace_dir):
        src_dir = Path(workspace_dir) / "src"
        src_dir.mkdir()
        (src_dir / "main.py").write_text("// main", encoding="utf-8")
        (src_dir / "AGENTS.md").write_text("# Agents\n\nFollow the protocol.", encoding="utf-8")

        discovery = ContextDiscovery(workspace_root=workspace_dir)
        results = discovery.discover_for_file(str(src_dir / "main.py"))

        source_ids = [s.source_id for s in results]
        assert "src/AGENTS.md" in source_ids

    def test_finds_readme_in_parent_dir(self, workspace_dir):
        helper = Path(workspace_dir) / "src" / "utils" / "helper.py"
        helper.parent.mkdir(parents=True)
        helper.write_text("// helper", encoding="utf-8")
        (Path(workspace_dir) / "src" / "README.md").write_text("# Project\n\nRead this.", encoding="utf-8")

        discovery = ContextDiscovery(workspace_root=workspace_dir)
        results = discovery.discover_for_file(str(helper))

        source_ids = [s.source_id for s in results]
        assert "src/README.md" in source_ids

    def test_no_duplication_across_paths(self, workspace_dir):
        src = Path(workspace_dir) / "src"
        src.mkdir()
        (src / "a.py").write_text("a", encoding="utf-8")
        (src / "b.py").write_text("b", encoding="utf-8")
        (src / "AGENTS.md").write_text("# Agents\n\nProto.", encoding="utf-8")

        discovery = ContextDiscovery(workspace_root=workspace_dir)
        r_all = discovery.discover_for_paths([str(src / "a.py"), str(src / "b.py")])

        assert len(r_all) == 1
        assert r_all[0].source_id == "src/AGENTS.md"

    def test_fingerprint_changes_on_content(self, workspace_dir):
        src = Path(workspace_dir) / "src"
        src.mkdir()
        (src / "main.py").write_text("// v1", encoding="utf-8")
        agents = src / "AGENTS.md"
        agents.write_text("version 1", encoding="utf-8")

        discovery = ContextDiscovery(workspace_root=workspace_dir)
        sources_v1 = discovery.discover_for_file(str(src / "main.py"))

        agents.write_text("version 2", encoding="utf-8")
        sources_v2 = discovery.discover_for_file(str(src / "main.py"))

        assert sources_v1[0].fingerprint != sources_v2[0].fingerprint

    def test_extract_header_skips_comment_lines(self, workspace_dir):
        src = Path(workspace_dir) / "src"
        src.mkdir()
        (src / "main.py").write_text("// main", encoding="utf-8")
        agents = src / "AGENTS.md"
        agents.write_text("# Title\n\nFirst real content.\n\n## Section", encoding="utf-8")

        discovery = ContextDiscovery(workspace_root=workspace_dir)
        results = discovery.discover_for_file(str(src / "main.py"))

        assert results[0].header == "First real content."

    def test_max_ancestor_walk_limit(self, workspace_dir):
        deep = Path(workspace_dir) / "a" / "b" / "c" / "d" / "e" / "f" / "deep.py"
        deep.parent.mkdir(parents=True, exist_ok=True)
        deep.write_text("// deep", encoding="utf-8")
        (Path(workspace_dir) / "AGENTS.md").write_text("# Root", encoding="utf-8")

        discovery = ContextDiscovery(workspace_root=workspace_dir)
        results = discovery.discover_for_file(str(deep))

        source_ids = [s.source_id for s in results]
        assert "AGENTS.md" not in source_ids

    def test_render_for_prompt_respects_budget(self, workspace_dir):
        src = Path(workspace_dir) / "src"
        src.mkdir()
        (src / "main.py").write_text("// main", encoding="utf-8")
        (src / "AGENTS.md").write_text("A" * 2000, encoding="utf-8")
        (src / "README.md").write_text("B" * 2000, encoding="utf-8")

        discovery = ContextDiscovery(workspace_root=workspace_dir)
        sources = discovery.discover_for_file(str(src / "main.py"))
        # 2500 chars per source, 2 sources = 5000 total, 2 sources with 600 budget each
        rendered = discovery.render_for_prompt(sources, budget_chars=600)

        # 每个 source 最多 ~600 chars (含 header + 内容截断)
        assert len(rendered) <= 1400  # 元信息 + 两个 source


# ---------------------------------------------------------------------------
# ContextDiscovery.render_for_prompt
# ---------------------------------------------------------------------------

class TestRenderForPrompt:
    def test_empty_sources(self, workspace_dir):
        discovery = ContextDiscovery(workspace_root=workspace_dir)
        assert discovery.render_for_prompt([]) == ""

    def test_renders_category_and_path(self, workspace_dir):
        src = _make_source(workspace_dir, "src/AGENTS.md", "Hello agents.")
        discovery = ContextDiscovery(workspace_root=workspace_dir)
        rendered = discovery.render_for_prompt([src])

        assert "AGENTS.md" in rendered
        assert "src/AGENTS.md" in rendered
        assert "Hello agents." in rendered

    def test_multiple_sources_separated(self, workspace_dir):
        s1 = _make_source(workspace_dir, "AGENTS.md", "agents content", "AGENTS.md")
        s2 = _make_source(workspace_dir, "README.md", "readme content", "README.md")
        discovery = ContextDiscovery(workspace_root=workspace_dir)
        rendered = discovery.render_for_prompt([s1, s2])

        assert "agents content" in rendered
        assert "readme content" in rendered


# ---------------------------------------------------------------------------
# ContextDiscovery.inject_into_prompt
# ---------------------------------------------------------------------------

class TestInjectIntoPrompt:
    def test_injects_at_end(self, workspace_dir):
        discovery = ContextDiscovery(workspace_root=workspace_dir)
        prompt = "Hello world."
        injected = discovery.inject_into_prompt(prompt, "## Local Context Sources\nExtra info.")
        assert injected.startswith("Hello world.")
        assert "## Local Context Sources" in injected

    def test_no_context_text_no_change(self, workspace_dir):
        discovery = ContextDiscovery(workspace_root=workspace_dir)
        prompt = "Hello world."
        assert discovery.inject_into_prompt(prompt, "") == prompt
